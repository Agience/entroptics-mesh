"""Decode-and-forward vs plain relaying on the CURRENT waveform (re-measure of the log-only
2/4-vs-3/4 figure, queue 9).

A relay can forward in two ways, both in the shared ``relay`` rules and both exercised here through
``emu.link_run.LinkRun`` -- the one harness that still has a *plain* mode:

    plain relaying      ``LinkRun(fresh=0)``: a relay re-queues every caught symbol and sends it on.
                        Under a shared channel its bursts collide with the origin's next ones (a
                        radio cannot hear while it sends), so it needs a high origin redundancy and
                        still loses messages.
    decode-and-forward  ``LinkRun(fresh>0)``: a relay listens until it has rebuilt the message, then
                        sends ``ceil(k * fresh)`` FRESH fountain symbols from its own id region.
                        This is what the node runtime (``node.Node`` via ``relay.take``) always does.

3-node chain, 6 km hops, 2400-byte message (k = 6), one shared 2.0 Msps band (the collision setup of
PAPER M-5.1).  Every burst is composed and read as real entroptics-rf IQ, so the numbers are on the
current wire format (K = L, keyed row radius, constant-modulus read).

    PYTHONPATH=src python research/scenarios/relay_df_current.py batch SEEDS PROCS OUT.jsonl
    PYTHONPATH=src python research/scenarios/relay_df_current.py table OUT.jsonl
"""
from __future__ import annotations

import json
import sys
import time
from collections import defaultdict
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from entroptics_mesh.emu.air import Air, Radio  # noqa: E402
from entroptics_mesh.emu.link_run import LinkNode, LinkRun  # noqa: E402
from entroptics_mesh.providers import TxPlan  # noqa: E402

HOP = 6000.0
SIZE = 2400
RATE = 2.0e6

# (arm label, fresh, origin redundancy): the two designs at the redundancy each needs.
ARMS = [("plain x3", 0.0, 3.0), ("plain x2", 0.0, 2.0),
        ("decode-forward x1.5", 1.5, 1.5), ("decode-forward x2", 1.5, 2.0)]


def run_one(label: str, fresh: float, red: float, seed: int) -> dict:
    air = Air(rate=RATE, seed=seed)
    nodes = []
    for i, x in enumerate((0.0, HOP, 2 * HOP)):
        idx = air.add(Radio(position=np.array([x, 0.0, 100.0]), tx_power_dbm=20.0, noise_dbm=-100.0))
        nodes.append(LinkNode(bytes([65 + i]) * 8, idx, key=1234))
    msg = bytes(np.random.default_rng(seed).integers(0, 256, SIZE, dtype=np.uint8))
    run = LinkRun(air, nodes, plan=TxPlan(rate=RATE), seed=seed, fresh=fresh)
    t = time.perf_counter()
    done = run.send(origin=0, to=nodes[2].node_id, message=msg, hops=4, redundancy=red)
    ok = 2 in done and nodes[2].got and list(nodes[2].got.values())[0][1] == msg
    return dict(label=label, fresh=fresh, red=red, seed=seed, delivered=bool(ok),
                slot=done.get(2), bursts=run.bursts, decoded=[n.decoded for n in nodes],
                wall_s=round(time.perf_counter() - t, 1))


def _one(args):
    try:
        return run_one(*args)
    except Exception as e:
        import traceback
        return dict(label=args[0], fresh=args[1], red=args[2], seed=args[3],
                    error=repr(e), tb=traceback.format_exc())


def batch(seeds: int, procs: int, out: Path, seed0: int = 1):
    from multiprocessing import Pool
    jobs = [(lbl, f, r, sd) for (lbl, f, r) in ARMS for sd in range(seed0, seed0 + seeds)]
    done = set()
    if out.exists():
        for line in out.read_text().splitlines():
            r = json.loads(line)
            if "error" not in r:
                done.add((r["label"], r["seed"]))
    jobs = [j for j in jobs if (j[0], j[3]) not in done]
    t0 = time.time()
    with Pool(procs) as pool, out.open("a") as f:
        for n, r in enumerate(pool.imap_unordered(_one, jobs), 1):
            f.write(json.dumps(r) + "\n")
            f.flush()
            print(f"[{n}/{len(jobs)} {time.time() - t0:.0f}s] {r.get('label')} seed {r.get('seed')}: "
                  f"{r.get('error') or ('delivered' if r.get('delivered') else 'lost')}", flush=True)


def table(paths) -> str:
    rows = [json.loads(l) for p in paths for l in Path(p).read_text().splitlines() if l.strip()]
    rows = [r for r in rows if "error" not in r]
    g = defaultdict(list)
    for r in rows:
        g[r["label"]].append(r)
    out = ["# Decode-and-forward vs plain relaying, CURRENT waveform (relay_df_current.py).",
           "# 3-node chain, 6 km hops, 2400-byte message (k = 6), shared 2.0 Msps band; real entroptics-rf IQ.",
           "",
           f"{'arm':22s} {'delivered':>10s} {'bursts (mean)':>14s} {'slot med':>9s}"]
    for lbl, _, _ in [(a, b, c) for (a, b, c) in ARMS]:
        rs = g.get(lbl)
        if not rs:
            continue
        d = sum(r["delivered"] for r in rs)
        bursts = np.mean([r["bursts"] for r in rs])
        slots = [r["slot"] for r in rs if r["slot"] is not None]
        out.append(f"{lbl:22s} {d:5d}/{len(rs):<4d} {bursts:14.1f} "
                   f"{(int(np.median(slots)) if slots else 0):9d}")
    return "\n".join(out)


if __name__ == "__main__":
    a = sys.argv[1:]
    if a[0] == "batch":
        batch(int(a[1]), int(a[2]), Path(a[3]), int(a[4]) if len(a) > 4 else 1)
    elif a[0] == "table":
        print(table(a[1:]))
    elif a[0] == "one":
        print(json.dumps(run_one(a[1], float(a[2]), float(a[3]), int(a[4]))))
