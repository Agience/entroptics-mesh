"""The fleet showcase (research/showcase/fleet_showcase.py, real IQ) at a given relay budget.

``research/access/real.py showcase adopted_fresh2`` cannot write its record (the Policy object in
``node_kw`` is not JSON); this runs the same ``fleet_showcase.main`` and writes the record with
``node_kw`` as text.

    PYTHONPATH=src python research/scenarios/showcase_budget.py BUDGET SEED WORKERS OUT.json
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "showcase"))
sys.path.insert(0, str(HERE.parent / "access"))

from entroptics_mesh import relay  # noqa: E402
import fleet_showcase as fs  # noqa: E402
from metrics import summary  # noqa: E402

if __name__ == "__main__":
    budget, seed, workers, out = float(sys.argv[1]), int(sys.argv[2]), int(sys.argv[3]), Path(sys.argv[4])
    rec = fs.main(7, workers, seed, None, node_kw=dict(policy=relay.Policy(fresh=budget)))
    rec["node_kw"] = {"policy": repr(relay.Policy(fresh=budget))}
    out.write_text(json.dumps(rec, separators=(",", ":"), default=repr))
    s = summary(rec)
    print(f"budget x{budget:g} seed {seed}", json.dumps({k: v for k, v in s.items() if k != "per_origin"}), flush=True)
