"""Diagnose a fleet-showcase recording's losses: who sent what, when, on which band, what overlapped;
burst counts by kind; every burst's in-band SINR margin at every receiver against what it read (the
calibration of ``emu.link_model``), split by whether the receiver's level gate let the capture through.

    python research/access/diagnose.py research/showcase/fleet_showcase_before.json
"""
from __future__ import annotations

import collections
import json
import sys

import numpy as np

SPILL_DB = -13.0
THR = {("differential", 2, "1/2"): 4.0}       # the only mode the showcase plans (DBPSK 1/2)


def margins(rec):
    """(margin dB, read, gated, bursts in slot, own, receiver role letter, sender, message, to) for every
    burst x receiver that did not transmit in that slot."""
    nm = [n["name"] for n in rec["nodes"]]
    full = rec["rx_rate"]
    out = []
    for ep in rec["epochs"]:
        snr = np.array([[np.nan if v is None else v for v in row] for row in ep["snr"]])
        for sl in ep["slots"]:
            txs = sl["tx"]
            senders = {t["node"] for t in txs}
            for r in range(len(nm)):
                nr = sl["nodes"].get(nm[r])
                if r in senders or nr is None:
                    continue
                gated = nr.get("stats", {}).get("gated", 0) > 0
                took = {(x["origin"], x["seq"], x["sid"]) for x in nr.get("took", [])}
                for t in txs:
                    lo, hi = t["center"] - t["rate"] / 2, t["center"] + t["rate"] / 2
                    n_in = t["rate"] / full
                    sig = 10 ** ((snr[t["node"], r] + t["power_db"]) / 10)
                    inter = 0.0
                    for o in txs:
                        if o is t:
                            continue
                        ov = max(0.0, min(hi, o["center"] + o["rate"] / 2) - max(lo, o["center"] - o["rate"] / 2)) / o["rate"]
                        inter += 10 ** ((snr[o["node"], r] + o["power_db"]) / 10) * (ov if ov > 0 else 10 ** (SPILL_DB / 10))
                    m = 10 * np.log10(sig / (n_in + inter)) - THR.get((t["family"], t["M"], t["code"]), 4.0)
                    out.append((m, (t["origin"], t["seq"], t["sid"]) in took, gated, len(txs), t["own"],
                                nm[r][0], nm[t["node"]], (ep["e"], t["origin"], t["seq"]), t["to"]))
    return out


def main(path):
    rec = json.loads(open(path).read())
    nm = [n["name"] for n in rec["nodes"]]
    home, ann = rec["home"], "494d484552450000"
    kinds, per_slot = collections.Counter(), []
    for ep in rec["epochs"]:
        for sl in ep["slots"]:
            per_slot.append(len(sl["tx"]))
            for tx in sl["tx"]:
                k = "announce" if tx["to"] == ann else ("own" if tx["own"] else f"relay by {nm[tx['node']][0]}")
                kinds[f"{k} @{tx['rate'] / 1e6:.1f} MHz"] += 1
    sent = rec["sent"]
    print(f"delivered {sum(x['delivered'] for x in sent)}/{len(sent)}; bursts {sum(per_slot)} in {len(per_slot)} slots")
    print("bursts by kind:", dict(kinds))
    print("bursts per slot:", dict(sorted(collections.Counter(per_slot).items())))
    rows = margins(rec)
    print("\nin-band SINR margin over the mode's threshold vs read (every burst x receiver):")
    for lo in range(-12, 33, 3):
        sel = [x for x in rows if lo <= x[0] < lo + 3]
        ng = [x for x in sel if not x[2]]
        if sel:
            print(f"  [{lo:3d},{lo + 3:3d}) dB  n={len(sel):4d}  gated {np.mean([x[2] for x in sel]):.2f}  "
                  f"read when not gated {np.mean([x[1] for x in ng]) if ng else float('nan'):.2f} (n={len(ng)})")
    own = collections.defaultdict(list)
    for x in rows:
        if x[4] and x[8] == home:
            own[x[7]].append(x)
    alone = collections.Counter()
    for ep in rec["epochs"]:
        for sl in ep["slots"]:
            for t in sl["tx"]:
                if t["own"] and t["to"] == home:
                    read = any(t["origin"] == k["origin"] and t["sid"] == k["sid"] and t["seq"] == k["seq"]
                               for nr in sl["nodes"].values() for k in nr.get("took", []))
                    alone[("alone" if len(sl["tx"]) == 1 else "with others", read)] += 1
    print("\norigin bursts (HOME), read by anyone:", dict(alone))
    cause = collections.Counter()
    for key, xs in own.items():
        if any(x[1] for x in xs):
            cause["read by someone"] += 1
        elif any(x[0] >= 0 and x[2] for x in xs):
            cause["lost: decodable but gated"] += 1
        elif any(x[0] >= 0 for x in xs):
            cause["lost: margin >= 0 somewhere, not read"] += 1
        else:
            cause["lost: SINR below threshold at every receiver"] += 1
    print("HOME messages by fate of their origin bursts:", dict(cause))


if __name__ == "__main__":
    main(sys.argv[1])
