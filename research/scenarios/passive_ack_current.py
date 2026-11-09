"""Passive-ack airtime saving on the CURRENT node runtime (re-measure of passive_ack_9km*).

An origin that overhears a symbol of its own message it never sent knows a relay has rebuilt it and
drops whatever it still has queued for it -- zero airtime, nothing sent back.  This measures the
airtime with the acknowledgement on and off on the real node runtime: every node a
:class:`~entroptics_mesh.node.Node` deciding from its own reads (plans, sub-bands, listen before
talk, sibling share), only ``relay.Policy.passive_ack`` changing between arms.

Topology: a 3-node chain O -> A -> F, 9 km hops, one 2400-byte message (k = 6), as
``node_loss_listen``'s ``single_alive`` arm.  ``FleetRun`` composes/reads real entroptics-rf IQ
(current wire format); ``ModelRun`` is the calibrated capture model for volume.

    PYTHONPATH=src python research/scenarios/passive_ack_current.py batch model|real SEEDS PROCS OUT.jsonl [--budgets 3,2,1.5]
    PYTHONPATH=src python research/scenarios/passive_ack_current.py table OUT.jsonl
"""
from __future__ import annotations

import json
import sys
import time
from collections import defaultdict
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from entroptics_mesh import phy, relay  # noqa: E402
from entroptics_mesh.adapt import Radio  # noqa: E402
from entroptics_mesh.emu.air import Air, Radio as AirRadio  # noqa: E402
from entroptics_mesh.emu.fleet_run import FleetRun  # noqa: E402
from entroptics_mesh.emu.link_model import ModelRun  # noqa: E402
from entroptics_mesh.node import Node, Wire  # noqa: E402
from entroptics_mesh.providers import NullCrypto, Providers  # noqa: E402

import math  # noqa: E402

R_EARTH = 6_371_000.0
RX = 2.4e6
TX_DBM, NOISE_DBM = 20.0, -100.0
RADIO = Radio(min_rate=1.2e6, max_rate=RX, center_hz=915e6, max_power_dbm=TX_DBM, noise_dbm=NOISE_DBM)
WARM = [phy.Known(64, 1.0, 0.0), phy.Known(64, 0.5, 0.25), phy.Known(64, 0.5, -0.25)]
HOP, ALT = 9000.0, 100.0
SIZE = 2400
MAX_SLOTS = 400


class KeyOnly(NullCrypto):
    name = "key-only"

    def waveform_key(self, context):
        return 1234


def pos(x, y, z):
    from entroptics_mesh.neighbors import Position
    return Position(math.degrees(y / R_EARTH), math.degrees(x / R_EARTH), z)


def build(seed: int, policy: relay.Policy):
    xy = [(0, 0), (HOP, 0), (2 * HOP, 0)]            # O -> A -> F
    names = ["O", "A", "F"]
    air = Air(rate=RX, seed=seed)
    nodes = []
    for i, (x, y) in enumerate(xy):
        p = np.array([float(x), float(y), ALT])
        air.add(AirRadio(position=p, tx_power_dbm=TX_DBM, noise_dbm=NOISE_DBM))
        nodes.append(Node(names[i].encode().ljust(8, b"_"), RADIO, slot_s=0.0,
                          providers=Providers(crypto=KeyOnly()), policy=policy,
                          accepts=frozenset(), backhaul=None, position=pos(*p),
                          seed=1000 * seed + i, blind_scaled=False, blind_every=1e9,
                          announce_every=None, hops=4))
    for i, a in enumerate(nodes):                    # state after first contact
        for j, b in enumerate(nodes):
            if i != j and air.snr_db(j, i) > 0.0:
                a.on_announcement(b.announcement(0.0), b.node_id, 0.0)
        for g in WARM:
            a.learn(g)
    for i, a in enumerate(nodes):
        a.on_capture(air.capture(i, -1.0, 4096), -1.0)
    nodes[0].router._seq = 1000 * seed
    return air, nodes, names


def run_one(budget: float, passive: bool, seed: int, model: bool) -> dict:
    # ``budget`` is the ORIGIN's redundancy (what the sender budgets against node loss); the relay
    # runs the adopted policy (fresh x2 + rank_margin 12).  Passive ack lets the origin stop once it
    # overhears the relay rebuild the message.
    policy = relay.Policy(passive_ack=passive)
    air, nodes, names = build(seed, policy)
    N = len(nodes)
    run = (ModelRun(air, nodes, [list(range(N))], seed=seed) if model
           else FleetRun(air, nodes, [list(range(N))], workers=False))
    payload = bytes(np.random.default_rng(seed).integers(0, 256, SIZE, dtype=np.uint8))
    to = nodes[-1].node_id
    t_wall = time.perf_counter()
    got = None
    k = None
    slot = 0
    while slot < MAX_SLOTS:
        t = slot * run.slot_s
        if slot == 0:
            nodes[0].send(payload, to, t, redundancy=float(budget))
        r = run.step(t)
        for tx in r["tx"]:
            k = tx["k"] if k is None else k
        for m in nodes[-1].inbox:
            if m.payload == payload and got is None:
                got = slot
        slot += 1
        if not any(nd.queue or nd.due is not None for nd in nodes):
            break
    run.close()
    return dict(budget=budget, passive=passive, seed=seed, model=model, k=k,
                delivered=got is not None, slot=got,
                latency_ms=round(got * run.slot_s * 1e3, 2) if got is not None else None,
                bursts=run.bursts, origin_bursts=nodes[0].stats.bursts,
                relay_bursts=nodes[1].stats.bursts, acked=sum(nd.stats.acked for nd in nodes),
                wall_s=round(time.perf_counter() - t_wall, 1))


def _one(args):
    try:
        return run_one(*args)
    except Exception as e:
        import traceback
        return dict(budget=args[0], passive=args[1], seed=args[2], model=args[3],
                    error=repr(e), tb=traceback.format_exc())


def batch(model: bool, seeds: int, procs: int, out: Path, budgets, seed0: int = 1):
    from multiprocessing import Pool
    jobs = [(b, pa, sd, model) for b in budgets for pa in (False, True)
            for sd in range(seed0, seed0 + seeds)]
    done = set()
    if out.exists():
        for line in out.read_text().splitlines():
            r = json.loads(line)
            if "error" not in r:
                done.add((float(r["budget"]), bool(r["passive"]), r["seed"], r["model"]))
    jobs = [j for j in jobs if (float(j[0]), bool(j[1]), j[2], j[3]) not in done]
    t0 = time.time()
    with Pool(procs) as pool, out.open("a") as f:
        for n, r in enumerate(pool.imap_unordered(_one, jobs), 1):
            f.write(json.dumps(r) + "\n")
            f.flush()
            print(f"[{n}/{len(jobs)} {time.time() - t0:.0f}s] x{r.get('budget')} "
                  f"passive={r.get('passive')} seed {r.get('seed')}: "
                  f"{r.get('error') or r.get('bursts')}", flush=True)


def table(paths) -> str:
    rows = [json.loads(l) for p in paths for l in Path(p).read_text().splitlines() if l.strip()]
    rows = [r for r in rows if "error" not in r]
    g = defaultdict(list)
    for r in rows:
        g[(r["model"], float(r["budget"]), bool(r["passive"]))].append(r)
    out = ["# Passive-ack airtime, CURRENT node runtime (passive_ack_current.py).",
           "# 3-node chain O->A->F, 9 km hops, 2400-byte message (k=6).", ""]
    for model in sorted({k[0] for k in g}, reverse=True):
        out.append(f"\n## {'real IQ (FleetRun)' if not model else 'capture model (ModelRun)'}\n")
        out.append(f"{'origin redundancy':18s} {'passive ack':>11s} {'delivered':>10s} "
                   f"{'bursts (mean)':>14s} {'origin b.':>9s} {'lat med ms':>10s}")
        for b in sorted({k[1] for k in g if k[0] == model}, reverse=True):
            for pa in (False, True):
                rs = g.get((model, b, pa))
                if not rs:
                    continue
                d = sum(r["delivered"] for r in rs)
                bursts = np.mean([r["bursts"] for r in rs])
                ob = np.mean([r["origin_bursts"] for r in rs])
                lat = [r["latency_ms"] for r in rs if r["latency_ms"] is not None]
                out.append(f"{'x%g' % b:18s} {('on' if pa else 'off'):>11s} {d:5d}/{len(rs):<4d} "
                           f"{bursts:14.1f} {ob:9.1f} {(np.median(lat) if lat else 0):10.0f}")
    return "\n".join(out)


if __name__ == "__main__":
    a = sys.argv[1:]
    if a[0] == "batch":
        opts = dict(zip(a[5::2], a[6::2]))
        batch(a[1] == "model", int(a[2]), int(a[3]), Path(a[4]),
              [float(x) for x in opts.get("--budgets", "3,2,1.5").split(",")],
              int(opts.get("--seed0", "1")))
    elif a[0] == "table":
        print(table(a[1:]))
    elif a[0] == "one":
        print(json.dumps(run_one(float(a[1]), a[2] == "on", int(a[3]), a[4] == "model")))
