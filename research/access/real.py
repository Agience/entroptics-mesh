"""Real-IQ runs of the access variants (every burst entroptics-rf IQ on the emulated air, every read
the node's own receiver): the fleet showcase and the dense-contention scenario.

    PYTHONPATH=src python research/access/real.py showcase|contention:N variant seed workers [oldgate]

``oldgate`` restores the level gate's floor as it was before (median of the last 16 captures'
reads) -- the code the recorded showcase ran -- to reproduce it.  Writes
``research/access/out/real_<scenario>_<variant>[_oldgate]_<seed>.json`` and prints the summary.
"""
from __future__ import annotations

import json
import math
import sys
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "showcase"))
sys.path.insert(0, str(HERE))

from entroptics_mesh import node as node_mod, phy  # noqa: E402
import contention  # noqa: E402
import fleet_showcase as fs  # noqa: E402
from metrics import summary  # noqa: E402
from variants import VARIANTS  # noqa: E402


def _old_level(self, iq):
    """The gate's floor before the access work: the median of the last 16 reads."""
    n = iq.size // 64 * 64
    p = np.mean(np.abs(iq[:n].reshape(-1, 64)) ** 2, axis=1) if n else np.array([1e-30])
    lo = float(np.percentile(p, 100 * self.FLOOR_QUANTILE)) / phy.noise_quantile(
        64, int(p.size), self.FLOOR_QUANTILE)
    hi = 10 * math.log10(max(float(np.percentile(p, 98)), 1e-30))
    self._floors.append(10 * math.log10(max(lo, 1e-30)))
    del self._floors[:-16]
    self.floor_db = float(np.median(self._floors))
    return hi, self.floor_db, hi - self.floor_db > self.gate_db


class OldGateNode(node_mod.Node):
    _level = _old_level


if __name__ == "__main__":
    scen, name, seed, workers = sys.argv[1], sys.argv[2], int(sys.argv[3]), int(sys.argv[4])
    old = len(sys.argv) > 5 and sys.argv[5] == "oldgate"
    if old:                       # worker processes are spawned: they unpickle this class by name
        fs.Node = OldGateNode
        contention.Node = OldGateNode
    v = VARIANTS[name]
    tag = f"real_{scen.replace(':', '')}_{name}{'_oldgate' if old else ''}_{seed}"
    out = HERE / "out" / f"{tag}.json"
    out.parent.mkdir(exist_ok=True)
    if scen == "showcase":
        rec = fs.main(7, workers, seed, out, listen=v["listen"], node_kw=v["node_kw"])
    else:
        rec = contention.main(int(scen.split(":")[1]), 3, seed, model=False, listen=v["listen"],
                              node_kw=v["node_kw"], workers=workers, out=out, verbose=True)
    s = summary(rec)
    print(tag, json.dumps(s), flush=True)
