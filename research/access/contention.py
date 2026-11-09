"""Dense contention: N origins in one cluster all want to reach home at the same moment.

A gateway G (accepts HOME) and N nodes spread over a disc of ``RADIUS`` m, ``DIST`` m from it
(1 W, 915 MHz, free space, noise -100 dBm over 2.4 MHz: ~13 dB full-band SNR to G, 25-45 dB to each
other).  Every node hears every other, so every node is also a relay of the others' messages (any
node a peer) -- the relay rules are the mesh's own.  Each round, every node is handed one
48-byte record for HOME in the same slot (an event they all react to), and the slots run until
nothing is queued (or ``MAX_SLOTS``).  Rounds are ``GAP`` s apart in mission time.

State at the start is the state after first contact (every announcement heard once, the three
sender geometries the plans use held), as in the fleet showcase, and every radio has heard its
band's noise once (its floor).  No announcements are sent
during the run: the access layer alone is measured.

    PYTHONPATH=src python research/access/contention.py N rounds seed [model|real] [listen 0|1] [variant]
"""
from __future__ import annotations

import json
import math
import struct
import sys
import time
from pathlib import Path

import numpy as np

from entroptics_mesh import phy
from entroptics_mesh.adapt import Radio
from entroptics_mesh.emu.air import Air, Radio as AirRadio
from entroptics_mesh.emu.fleet_run import FleetRun
from entroptics_mesh.emu.link_model import ModelRun
from entroptics_mesh.neighbors import Position
from entroptics_mesh.node import Node
from entroptics_mesh.providers import NullCrypto, Providers
from entroptics_mesh.routing import HOME

HERE = Path(__file__).resolve().parent
R_EARTH = 6_371_000.0
RX = 2.4e6
TX_DBM, NOISE_DBM = 20.0, -100.0
RADIO = Radio(min_rate=1.2e6, max_rate=RX, center_hz=915e6, max_power_dbm=TX_DBM, noise_dbm=NOISE_DBM)
WARM = [phy.Known(64, 1.0, 0.0), phy.Known(64, 0.5, 0.25), phy.Known(64, 0.5, -0.25)]
DIST, RADIUS, ALT = 6000.0, 500.0, 120.0
GAP = 30.0
MAX_SLOTS = 120
REC_BYTES = 48


class KeyOnly(NullCrypto):
    name = "key-only"

    def waveform_key(self, context):
        return 1234


def pos(x, y, z) -> Position:
    return Position(math.degrees(y / R_EARTH), math.degrees(x / R_EARTH), z)


def build(n: int, seed: int, node_kw: dict | None = None):
    rng = np.random.default_rng(10_000 + seed)
    air = Air(rate=RX, seed=seed)
    xyz = [np.array([0.0, 0.0, 30.0])]
    for _ in range(n):
        r, a = RADIUS * math.sqrt(rng.random()), 2 * math.pi * rng.random()
        xyz.append(np.array([DIST + r * math.cos(a), r * math.sin(a), ALT]))
    nodes = []
    for i, p in enumerate(xyz):
        air.add(AirRadio(position=p, tx_power_dbm=TX_DBM, noise_dbm=NOISE_DBM))
        name = b"G" if i == 0 else b"N%02d" % i
        nodes.append(Node(name.ljust(8, b"_"), RADIO, slot_s=0.0, providers=Providers(crypto=KeyOnly()),
                          accepts={HOME} if i == 0 else frozenset(), position=pos(*p), seed=1000 * seed + i,
                          blind_scaled=False, blind_every=1e9, announce_every=None, hops=4,
                          **(node_kw or {})))
    for a in nodes:
        for b in nodes:
            if a is not b:
                a.on_announcement(b.announcement(0.0), b.node_id, 0.0)
        for g in WARM:
            a.learn(g)
    for i, a in enumerate(nodes):
        # The radios were on before the event: each has heard its band's noise (a node with no
        # floor yet cannot tell a busy listen from a quiet one, and would send blind).
        a.on_capture(air.capture(i, -1.0, 4096), -1.0)
    return air, nodes


def main(n: int = 16, rounds: int = 5, seed: int = 1, *, model: bool = True, listen: bool = True,
         node_kw: dict | None = None, workers: int = 0, out: Path | None = None, verbose: bool = False):
    air, nodes = build(n, seed, node_kw)
    N = len(nodes)
    names = ["G"] + [f"N{i:02d}" for i in range(1, N)]
    groups = [list(range(N))] if (model or not workers) else \
        [[0]] + [list(range(1, N))[j::max(1, workers - 1)] for j in range(max(1, workers - 1))]
    groups = [g for g in groups if g]
    run = (ModelRun(air, nodes, groups, listen=listen, seed=seed) if model
           else FleetRun(air, nodes, groups, workers=workers > 0, listen=listen))
    rec = dict(scenario="contention", n=n, seed=seed, model=model, listen=listen, slot_s=run.slot_s,
               node_kw={k: repr(v) for k, v in (node_kw or {}).items()},
               nodes=[dict(id=nd.node_id.hex(), name=names[i]) for i, nd in enumerate(nodes)],
               epochs=[], sent=[], deliveries=[])
    t_wall = time.perf_counter()
    for e in range(rounds):
        t_e = e * GAP
        ep = dict(e=e, t=t_e, slots=[])
        sent_at = {}
        s = 0
        while s < MAX_SLOTS:
            t = t_e + s * run.slot_s
            orders = [(i, "send", (b"R" + struct.pack(">BH", i, e)).ljust(REC_BYTES, b"."))
                      for i in range(1, N)] if s == 0 else []
            if s == 0:
                for i in range(1, N):
                    sent_at[i] = t
            r = run.step(t, orders)
            slot = dict(s=s, t=round(t, 6), tx=r["tx"], nodes={})
            if r.get("deferred"):
                slot["deferred"] = [names[i] for i in r["deferred"]]
            busy = False
            for i, nr in r["nodes"].items():
                busy |= bool(nr.get("queue"))
                for m in nr.get("inbox", []):
                    p = m["payload"]
                    if p[:1] == b"R":
                        who, ep_ = struct.unpack(">BH", p[1:4])
                        rec["deliveries"].append(dict(origin=names[who], epoch=ep_, at=names[i], e=e, s=s,
                                                      t=m["t"]))
            ep["slots"].append(slot)
            s += 1
            if s > 1 and not busy:
                break
        first = {}
        for d in rec["deliveries"]:
            if d["e"] == e and d["origin"] not in first:
                first[d["origin"]] = d
        for i in range(1, N):
            d = first.get(names[i])
            rec["sent"].append(dict(kind="rec", origin=names[i], epoch=e, t=sent_at[i], slot=0,
                                    delivered=d is not None,
                                    latency_ms=None if d is None else round((d["t"] - sent_at[i]) * 1e3, 2)))
        rec["epochs"].append(ep)
        if verbose:
            got = sum(1 for x in rec["sent"] if x["epoch"] == e and x["delivered"])
            print(f"round {e}: slots {len(ep['slots'])} bursts {sum(len(sl['tx']) for sl in ep['slots'])} "
                  f"delivered {got}/{N - 1} [{time.perf_counter() - t_wall:.0f}s wall]", flush=True)
    run.close()
    rec["wall_s"] = round(time.perf_counter() - t_wall, 1)
    if out is not None:
        out.write_text(json.dumps(rec, separators=(",", ":")))
    return rec


if __name__ == "__main__":
    sys.path.insert(0, str(HERE))
    from metrics import summary
    import variants
    a = sys.argv[1:]
    n, rounds, seed = int(a[0]), int(a[1]), int(a[2])
    model = (a[3] if len(a) > 3 else "model") == "model"
    v = variants.VARIANTS[a[4] if len(a) > 4 else "baseline"]
    workers = int(a[5]) if len(a) > 5 else 0
    out = HERE / "out" / f"contention_{n}_{a[4] if len(a) > 4 else 'baseline'}_{'model' if model else 'real'}_{seed}.json"
    out.parent.mkdir(exist_ok=True)
    rec = main(n, rounds, seed, model=model, listen=v["listen"], node_kw=v["node_kw"], workers=workers,
               out=out, verbose=True)
    print(json.dumps({k: v for k, v in summary(rec).items() if k != "per_origin"}))
