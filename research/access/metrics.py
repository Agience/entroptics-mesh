"""Access-layer metrics of a fleet-showcase-style record (``fleet_showcase.main`` / ``contention``):
delivery, latency, airtime per delivered message, deferrals, and fairness across origins."""
from __future__ import annotations

from collections import defaultdict

import numpy as np


def summary(rec: dict) -> dict:
    sent = rec["sent"]
    ok = [x for x in sent if x["delivered"]]
    lat = np.array([x["latency_ms"] for x in ok], float)
    slots = [sl for ep in rec["epochs"] for sl in ep["slots"]]
    bursts = sum(len(sl["tx"]) for sl in slots)
    own = sum(1 for sl in slots for tx in sl["tx"] if tx["own"])
    deferred = sum(len(sl.get("deferred", ())) for sl in slots)
    per = defaultdict(lambda: [0, 0])
    for x in sent:
        per[x["origin"]][0] += 1
        per[x["origin"]][1] += bool(x["delivered"])
    frac = np.array([d / n for n, d in per.values()], float)
    jain = float(frac.sum() ** 2 / (len(frac) * (frac ** 2).sum())) if frac.size and (frac ** 2).sum() else 0.0
    within = {f"in{ms}": float(np.sum(lat <= ms)) / max(1, len(sent)) for ms in (50, 100, 200)}
    return dict(sent=len(sent), delivered=len(ok), frac=len(ok) / max(1, len(sent)), **within,
                lat_med=float(np.median(lat)) if lat.size else float("nan"),
                lat_p90=float(np.percentile(lat, 90)) if lat.size else float("nan"),
                bursts=bursts, own=own, per_delivered=bursts / max(1, len(ok)), deferred=deferred,
                slots=len(slots), min_origin=float(frac.min()) if frac.size else 0.0, jain=jain,
                per_origin={k: f"{d}/{n}" for k, (n, d) in sorted(per.items())})


def fmt(name: str, rows: list[dict]) -> str:
    a = lambda k: np.array([r[k] for r in rows], float)
    d, s = a("delivered").sum(), a("sent").sum()
    return (f"{name:34s} delivered {int(d):4d}/{int(s):4d} = {d / s:5.3f} (per seed {a('frac').min():.2f}-{a('frac').max():.2f})  "
            f"lat med {np.nanmean(a('lat_med')):6.1f} p90 {np.nanmean(a('lat_p90')):6.1f} ms  "
            f"bursts/deliv {a('bursts').sum() / max(1, d):5.2f}  bursts {a('bursts').mean():6.1f}  "
            f"deferred {a('deferred').mean():6.1f}  min-origin {a('min_origin').mean():.2f} jain {a('jain').mean():.3f}  "
            f"<=50/100/200ms {a('in50').mean():.2f}/{a('in100').mean():.2f}/{a('in200').mean():.2f}")
