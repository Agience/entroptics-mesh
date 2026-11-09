"""
Real-PHY fly sweep: delivery vs range with the self-arranging formation, DIRECT (0 relays) vs the
3-relay formation -- confirming the analytic link-budget prediction against actual decode and showing
where direct fails and relays carry it.

Antenna gain (~3 dBi both ends, the recommended fleet config) is folded into the air's tx power
(air.py has no antenna model): effective tx = node 20 dBm + 2x3 dBi = 26 dBm, kept consistent with the
planner (adapt/placement run at antenna_dbi=3). Slow (real multi-hop decode per cell); run backgrounded.
"""
from __future__ import annotations

import math

import numpy as np

from flight import Formation, east_of

from entroptics_mesh import phy
from entroptics_mesh.adapt import Radio
from entroptics_mesh.emu.air import Air, Radio as AirRadio
from entroptics_mesh.emu.node_run import NodeRun
from entroptics_mesh.neighbors import Position
from entroptics_mesh.node import Node
from entroptics_mesh.providers import NullCrypto, Providers
from entroptics_mesh.routing import HOME

GAIN = 3.0
RADIO = Radio(min_rate=0.25e6, max_rate=2.4e6, center_hz=915e6, max_power_dbm=20.0,
              noise_dbm=-100.0, freq_hz=915e6, antenna_dbi=GAIN)
AIR_TX = 20.0 + 2 * GAIN                      # fold both antenna gains (air has no antenna model)
BASE = Position(45.0, -75.0, 100.0)
PLAN_RATE = 0.5e6
SUB = [phy.Known(64, 0.5, 0.25), phy.Known(64, 0.5, -0.25)]
FULL = phy.Known(64, 1.0, 0.0)


class KeyOnly(NullCrypto):
    name = "key-only"

    def waveform_key(self, context):
        return 0xA17C0DE


def _east_m(p: Position) -> float:
    return (p.lon - BASE.lon) * math.pi / 180 * 6_371_000.0 * math.cos(math.radians(BASE.lat))


def deliver(km: float, n_relays: int, seed: int = 1):
    form = Formation(RADIO, rate=PLAN_RATE)
    lead = east_of(BASE, km * 1000.0)
    relays = form.assign(BASE, lead, n_relays)
    positions = {b"BASE0000": BASE}
    for i, p in enumerate(relays["relays"]):
        positions[b"RLY%05d" % i] = p
    positions[b"LEAD0000"] = lead
    air = Air(freq_hz=915e6, rate=2.4e6, seed=seed)
    nodes, ids = [], list(positions)
    for i, nid in enumerate(ids):
        p = positions[nid]
        air.add(AirRadio(position=np.array([_east_m(p), 0.0, p.alt]), tx_power_dbm=AIR_TX, noise_dbm=-100.0))
        nodes.append(Node(nid, RADIO, slot_s=0.0, providers=Providers(crypto=KeyOnly()), seed=100 + i,
                          position=p, blind_scaled=False,
                          accepts=({HOME} if nid == b"BASE0000" else frozenset())))
    for a in nodes:
        for b in nodes:
            if a is not b:
                a.on_announcement(b.announcement(0.0), b.node_id, 0.0)
        for g in (FULL, *SUB):
            a.learn(g)
    base = nodes[0]
    lead_node = nodes[ids.index(b"LEAD0000")]
    msg = bytes(np.random.default_rng(seed).integers(0, 256, 300, dtype=np.uint8))
    lead_node.send(msg, HOME, 0.0)
    run = NodeRun(air, nodes, listen=True)
    run.run(150, until=lambda: bool(base.inbox))
    delivered = [m.payload for m in base.inbox] == [msg]
    return delivered, len(relays["relays"]), relays["gap"], run.bursts


if __name__ == "__main__":
    print(f"fly sweep: 915 MHz, nodes ~{GAIN:.0f} dBi (air tx {AIR_TX:.0f} dBm EIRP+RX folded), "
          f"plan {PLAN_RATE/1e6:.2f} MS/s, listen-before-talk")
    print(f"{'range(km)':>9} | {'direct (0 relays)':>18} | {'formation (<=3 relays)':>24}")
    for km in (8, 16, 24, 32, 40):
        d0, _, _, b0 = deliver(km, 0)
        d3, nr, gap, b3 = deliver(km, 3)
        print(f"{km:9d} | {str(d0):>11} ({b0:2d} bursts) | "
              f"{str(d3):>11} ({nr} relays, gap {gap}, {b3:2d} bursts)", flush=True)
    print("\nexpected: direct holds to the raw link range then fails; the formation carries further "
          "until relays run out (gap>0). Confirms link_budget_sweep against real decode.")
