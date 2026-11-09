"""Screen access variants with the capture model (emu.link_model) over many seeds.

    PYTHONPATH=src python research/access/screen.py showcase|contention:N seeds variant [variant ...]
"""
from __future__ import annotations

import json
import os
import sys
import time
from multiprocessing import Pool
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "showcase"))
sys.path.insert(0, str(HERE))

import contention  # noqa: E402
import fleet_showcase as fs  # noqa: E402
from metrics import fmt, summary  # noqa: E402
from variants import VARIANTS  # noqa: E402


def one(args):
    scen, name, seed = args
    v = VARIANTS[name]
    if scen == "showcase":
        rec = fs.main(7, 0, seed, None, listen=v["listen"], model=True, node_kw=v["node_kw"], verbose=False)
    else:
        rec = contention.main(int(scen.split(":")[1]), 5, seed, model=True, listen=v["listen"], node_kw=v["node_kw"])
    return name, seed, summary(rec)


if __name__ == "__main__":
    scen, seeds = sys.argv[1], int(sys.argv[2])
    names = sys.argv[3:]
    t0 = time.time()
    with Pool(int(os.environ.get("PROCS", "8"))) as pool:
        res = pool.map(one, [(scen, n, s) for n in names for s in range(1, seeds + 1)])
    for n in names:
        print(fmt(n, [r for nn, _, r in res if nn == n]), flush=True)
    print(f"[{scen}, {seeds} seeds, {time.time() - t0:.0f} s]")
    (HERE / "out").mkdir(exist_ok=True)
    (HERE / "out" / f"screen_{scen.replace(':', '')}_{'_'.join(names)}.json"[:200]).write_text(json.dumps(res))
