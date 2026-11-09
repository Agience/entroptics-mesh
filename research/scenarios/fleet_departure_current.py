"""Fleet departure on the CURRENT node runtime (re-measure of fleet_departure.py/.json).

A lead drone flies out at V m/s and drops relays at D, 2D, 3D; it sends telemetry home every T_MSG
seconds.  Measures delivery to the base over distance.  The old run used the flood prototype
(``emu.net.FloodRun``); this one is the current runtime -- every node a
:class:`~entroptics_mesh.node.Node` with sub-bands, listen before talk, decode-and-forward relays,
the adopted policy -- over ``FleetRun`` (real entroptics-rf IQ) or ``ModelRun`` (capture model).

Geometry (free space, 20 dBm, -100 dBm noise over 2.4 MHz): base at x=0; relay i holds at min(lead,
i*D); the lead at V*t.  Each message epoch is evaluated at the lead's position for that epoch and
run to completion (the lead moves ~V*T_MSG between epochs, << a hop, and a message delivers in tens
of ms, so positions are held within an epoch, as in the original).  Hop budget 8.

    PYTHONPATH=src python research/scenarios/fleet_departure_current.py run model|real [N_MSG] [SEED] [OUT.jsonl]
"""
from __future__ import annotations

import json
import math
import sys
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from entroptics_mesh import phy, relay  # noqa: E402
from entroptics_mesh.adapt import Radio  # noqa: E402
from entroptics_mesh.emu.air import Air, Radio as AirRadio  # noqa: E402
from entroptics_mesh.emu.fleet_run import FleetRun  # noqa: E402
from entroptics_mesh.emu.link_model import ModelRun  # noqa: E402
from entroptics_mesh.neighbors import Position  # noqa: E402
from entroptics_mesh.node import Node  # noqa: E402
from entroptics_mesh.providers import NullCrypto, Providers  # noqa: E402

R_EARTH = 6_371_000.0
RX = 2.4e6
TX_DBM, NOISE_DBM = 20.0, -100.0
RADIO = Radio(min_rate=1.2e6, max_rate=RX, center_hz=915e6, max_power_dbm=TX_DBM, noise_dbm=NOISE_DBM)
WARM = [phy.Known(64, 1.0, 0.0), phy.Known(64, 0.5, 0.25), phy.Known(64, 0.5, -0.25)]
V, D, R, T_MSG = 60.0, 2500.0, 3, 25.0
ALT_BASE, ALT = 10.0, 100.0
MAX_SLOTS = 60


class KeyOnly(NullCrypto):
    name = "key-only"

    def waveform_key(self, context):
        return 1234


def pos(x, y, z):
    return Position(math.degrees(y / R_EARTH), math.degrees(x / R_EARTH), z)


def positions(t):
    lead = V * t
    xs = [0.0] + [min(lead, i * D) for i in range(1, R + 1)] + [lead]
    zs = [ALT_BASE] + [ALT] * R + [ALT]
    return xs, zs


def build(t: float, seed: int):
    xs, zs = positions(t)
    names = ["BASE"] + [f"R{i}" for i in range(1, R + 1)] + ["LEAD"]
    air = Air(rate=RX, seed=seed)
    nodes = []
    for i, (x, z) in enumerate(zip(xs, zs)):
        p = np.array([float(x), 0.0, float(z)])
        air.add(AirRadio(position=p, tx_power_dbm=TX_DBM, noise_dbm=NOISE_DBM))
        nodes.append(Node(names[i].encode().ljust(8, b"_"), RADIO, slot_s=0.0,
                          providers=Providers(crypto=KeyOnly()), policy=relay.ADOPTED,
                          accepts=frozenset(), backhaul=None, position=pos(x, 0.0, z),
                          seed=1000 * seed + i, blind_scaled=False, blind_every=1e9,
                          announce_every=None, hops=8))
    for i, a in enumerate(nodes):                    # state after first contact among those that hear
        for j, b in enumerate(nodes):
            if i != j and air.snr_db(j, i) > 0.0:
                a.on_announcement(b.announcement(0.0), b.node_id, 0.0)
        for g in WARM:
            a.learn(g)
    for i, a in enumerate(nodes):
        a.on_capture(air.capture(i, -1.0, 4096), -1.0)
    return air, nodes, names


def run_epoch(m: int, seed: int, model: bool) -> dict:
    t_mission = m * T_MSG
    air, nodes, names = build(t_mission, seed)
    N = len(nodes)
    lead, base = N - 1, 0
    nodes[lead].router._seq = 1000 * seed + m
    run = (ModelRun(air, nodes, [list(range(N))], seed=seed + m) if model
           else FleetRun(air, nodes, [list(range(N))], workers=False))
    payload = f"telemetry {m}".encode().ljust(48, b"\x00")
    to = nodes[base].node_id
    got = None
    slot = 0
    while slot < MAX_SLOTS:
        t = slot * run.slot_s
        if slot == 0:
            nodes[lead].send(payload, to, t)
        run.step(t)
        for msg in nodes[base].inbox:
            if msg.payload == payload and got is None:
                got = slot
        slot += 1
        if not any(nd.queue or nd.due is not None for nd in nodes):
            break
    run.close()
    xs, _ = positions(t_mission)
    return dict(m=m, t=t_mission, lead_km=round(xs[lead] / 1e3, 2), seed=seed, model=model,
                delivered=got is not None, slot=got,
                latency_ms=round(got * run.slot_s * 1e3, 1) if got is not None else None,
                bursts=run.bursts,
                snr_lead_base=round(air.snr_db(lead, base), 1),
                snr_lead_r3=round(air.snr_db(lead, R), 1),
                snr_r3_base=round(air.snr_db(R, base), 1))


if __name__ == "__main__":
    a = sys.argv[1:]
    model = a[1] == "model"
    n_msg = int(a[2]) if len(a) > 2 else 20
    seed = int(a[3]) if len(a) > 3 else 1
    out = Path(a[4]) if len(a) > 4 else Path(__file__).with_name(
        f"fleet_departure_current_{'model' if model else 'real'}.jsonl")
    recs = []
    t0 = time.perf_counter()
    with out.open("w") as f:
        for m in range(n_msg):
            r = run_epoch(m, seed, model)
            recs.append(r)
            f.write(json.dumps(r) + "\n")
            f.flush()
            print(f"t={r['t']:5.0f}s lead {r['lead_km']:5.2f} km: base "
                  f"{'GOT' if r['delivered'] else '---'} (slot {r['slot']}, {r['bursts']} bursts) "
                  f"snr lead->base {r['snr_lead_base']:6.1f}  lead->R{R} {r['snr_lead_r3']:5.1f}  "
                  f"R{R}->base {r['snr_r3_base']:5.1f} dB", flush=True)
    delivered = [r for r in recs if r["delivered"]]
    last = max((r["lead_km"] for r in delivered), default=0.0)
    print(f"\n{len(delivered)}/{len(recs)} delivered; last delivery at {last:.2f} km "
          f"[{time.perf_counter() - t0:.0f}s]")
