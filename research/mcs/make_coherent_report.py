"""Tables for coherent.md and results_coherent.json, from the coherent sweep's phase files and the
differential sweep's results (results.json natural labels for M=2/4, results_gray.json Gray labels
for M=8/16 -- the labels adapt.MCS already uses).

Thresholds are read as make_report.py reads them: the lowest grid SNR from which PER stays <= target
at every higher grid SNR, plus a log10(PER)-linear interpolation between the bracketing points (a
zero-failure cell counts as 0.5/packets for the interpolation only).  The coherent grid is 1 dB.

Run:  python research/mcs/make_coherent_report.py      (prints markdown; writes results_coherent.json)
"""
from __future__ import annotations

import json
import math
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from make_report import threshold  # noqa: E402

RATES = ("1/2", "2/3", "3/4")
QAM = (4, 16, 64, 256)
CONDS = ("m64", "m128", "r128")
COND_NAME = {"m64": "matched, L=64 K=60", "m128": "matched, L=128 K=124",
             "r128": "rho 0.514403, L=128 K=124"}
ENVELOPE_SNRS = (6, 8, 10, 12, 14, 16, 18, 20, 22, 25, 28, 30)


def _load(name):
    p = HERE / name
    return json.loads(p.read_text())["results"] if p.exists() else []


def coherent_cells():
    """(cond, M, rate) -> cells sorted by SNR; a fine cell replaces the coarse one at its SNR."""
    cells = {}
    for c in _load("results_coherent_coarse.json"):
        cells[(c["cond"], c["M"], c["rate"], c["snr"])] = dict(c, phase="coarse")
    for name in ("results_coherent_fine.json", "results_coherent_fine2.json",
                 "results_coherent_redo.json"):
        for c in _load(name):
            cells[(c["cond"], c["M"], c["rate"], c["snr"])] = dict(c, phase="fine")
    out = {}
    for (cond, M, r, _), c in cells.items():
        out.setdefault((cond, M, r), []).append(c)
    return {k: sorted(v, key=lambda c: c["snr"]) for k, v in out.items()}


def peak_goodput(c):
    return c["goodput"] / (1 - c["per"]) if c["per"] < 1 else None


def genie_threshold(cells, target=0.01):
    g = [dict(snr=c["snr"], per=c["fails_genie"] / c["packets"], packets=c["packets"])
         for c in cells if c.get("fails_genie") is not None]
    return threshold(g, target) if g else (None, None)


def permode_threshold(cells, target=0.01):
    g = [dict(snr=c["snr"], per=c["fails_permode"] / c["packets"], packets=c["packets"])
         for c in cells]
    return threshold(g, target)


def differential_modes():
    """The differential ladder: (M, rate, labels, threshold, goodput) from the existing sweeps."""
    nat = [c for c in _load("results.json") + _load("results_15_25.json") if c.get("weight", True)]
    gray = _load("results_gray.json")
    out = []
    for M in (2, 4, 8, 16):
        src, lab = (gray, "Gray") if M in (8, 16) else (nat, "natural")
        for r in RATES:
            cells = sorted((c for c in src if c["M"] == M and c["rate"] == r and c["snr"] % 2 == 0),
                           key=lambda c: c["snr"])
            g, i = threshold(cells, 0.01)
            if g is None:
                continue
            top = max(cells, key=lambda c: c["snr"])
            out.append(dict(family="differential", M=M, rate=r, labels=lab,
                            threshold=round(i if i is not None else g, 1),
                            goodput=round(top["goodput"] / (1 - top["per"]), 3)))
    return out


def _thr(pair):
    """A threshold for planning: the interpolation, else the grid point (where the lowest grid
    point measured already clears, that point is an upper bound)."""
    g, i = pair
    return None if g is None else (round(i, 2) if i is not None else float(g))


def coherent_modes(cells):
    """Per (M, rate): thresholds per condition, and the planning entry -- the worst per-mode-N0
    threshold over the conditions (the planner does not know the receiver's rate) at the L=64 K=60
    goodput.  The per-mode read is fec.soft_bits_from_qam(..., K=K)."""
    out = []
    for M in QAM:
        for r in RATES:
            per_cond = {}
            for cond in CONDS:
                cs = cells.get((cond, M, r))
                if not cs:
                    continue
                g, i = threshold(cs, 0.01)
                g10, i10 = threshold(cs, 0.10)
                top = max(cs, key=lambda c: c["snr"])
                per_cond[cond] = dict(
                    grid=g, interp=None if i is None else round(i, 2),
                    grid10=g10, interp10=None if i10 is None else round(i10, 2),
                    genie=_thr(genie_threshold(cs)) if cond != "r128" else None,
                    permode=_thr(permode_threshold(cs)),
                    goodput=round(peak_goodput(top), 3) if peak_goodput(top) else None,
                    nominal=round(top["nominal"], 3), rows=top["rows"])
            thr = [v["permode"] for v in per_cond.values() if v["permode"] is not None]
            out.append(dict(family="coherent", M=M, rate=r, conds=per_cond,
                            threshold=round(max(thr), 1) if thr else None,
                            goodput=per_cond.get("m64", {}).get("goodput")))
    return out


def envelope(modes, snr):
    ok = [m for m in modes if m["threshold"] is not None and m["threshold"] <= snr]
    return max(ok, key=lambda m: m["goodput"]) if ok else None


def pareto(modes):
    """Modes no other mode beats on both threshold and goodput, in rising goodput."""
    ms = sorted((m for m in modes if m["threshold"] is not None),
                key=lambda m: (m["threshold"], -m["goodput"]))
    out, best = [], -1.0
    for m in ms:
        if m["goodput"] > best:
            out.append(m)
            best = m["goodput"]
    return out


def blind_summary(rows):
    """Per (cond, M, rate, snr): packets, failures informed/blind, why blind failed, cost."""
    import statistics
    g = {}
    for r in rows:
        g.setdefault((r["cond"], r["M"], r["rate"], r["snr"]), []).append(r)
    out = []
    for (cond, M, rate, snr), rs in sorted(g.items()):
        causes = {}
        for r in rs:
            if not r["ok_blind"]:
                causes[r["outcome"]] = causes.get(r["outcome"], 0) + 1
        out.append(dict(cond=cond, M=M, rate=rate, snr=snr, packets=len(rs),
                        fails_informed=sum(not r["ok_informed"] for r in rs),
                        fails_blind=sum(not r["ok_blind"] for r in rs), causes=causes,
                        fails_crc=(sum(not r["ok_blind_crc"] for r in rs)
                                   if all("ok_blind_crc" in r for r in rs) else None),
                        seconds=statistics.median(r["seconds_blind"] for r in rs),
                        scale_err=statistics.median(abs(r["scale_err"]) for r in rs)))
    return out


def name(m):
    if m is None:
        return "none"
    if m["family"] == "differential":
        return f"D{'B' if m['M'] == 2 else ('Q' if m['M'] == 4 else str(m['M']))}PSK {m['rate']}"
    return f"{m['M']}-QAM {m['rate']}"


def _fmt1(fails, packets):
    if fails == 0:
        return f"0/{packets}"
    if fails == packets:
        return f"{packets}/{packets}"
    return f"{fails / packets:.3g}"


def fmt(c):
    """Per-mode-N0 PER, with the global-N0 PER after it when they differ."""
    if c is None:
        return ""
    pm, gl = _fmt1(c["fails_permode"], c["packets"]), _fmt1(c["fails"], c["packets"])
    return pm if pm == gl else f"{pm} ({gl})"


def main():
    cells = coherent_cells()
    diff = differential_modes()
    coh = coherent_modes(cells)

    for cond in CONDS:
        print(f"### PER, {COND_NAME[cond]}\n")
        for M in QAM:
            snrs = sorted({c["snr"] for r in RATES for c in cells.get((cond, M, r), [])})
            if not snrs:
                continue
            idx = {(r, c["snr"]): c for r in RATES for c in cells.get((cond, M, r), [])}
            print(f"{M}-QAM\n")
            print("| SNR dB | raw BER (1/2) | PER 1/2 | PER 2/3 | PER 3/4 |")
            print("|---|---|---|---|---|")
            for s in snrs:
                b = idx.get(("1/2", s))
                print(f"| {s} | {b['ber']:.2e} | " if b else f"| {s} | | ", end="")
                print(" | ".join(fmt(idx.get((r, s))) for r in RATES) + " |")
            print()

    print("### Thresholds, PER <= 1% (dB, interpolated): per-mode N0 / one global N0\n")
    print("| mode | " + " | ".join(COND_NAME[c] for c in CONDS)
          + " | genie gains, m64 / m128 | goodput L=64 | goodput L=128 | planning threshold |")
    print("|---|" + "---|" * (len(CONDS) + 4))
    for m in coh:
        row = []
        for cond in CONDS:
            v = m["conds"].get(cond)
            row.append("" if v is None else
                       f"{v['permode']} / {v['interp'] if v['interp'] is not None else v['grid']}")
        c = m["conds"]
        print(f"| {name(m)} | " + " | ".join(row)
              + f" | {c.get('m64', {}).get('genie')} / {c.get('m128', {}).get('genie')} | "
              f"{m['goodput']} | {c.get('m128', {}).get('goodput')} | {m['threshold']} |")
    print()

    print("### Differential ladder used (existing sweeps)\n")
    for m in diff:
        print(f"- {name(m)} ({m['labels']}): {m['threshold']} dB, {m['goodput']} b/s/Hz")
    print()

    both = pareto(diff + [m for m in coh if m["goodput"]])
    only = pareto(diff)
    print("### Envelope (PER <= 1% thresholds; goodput at L=64 K=60)\n")
    print("| SNR dB | differential only | b/s/Hz | with coherent | b/s/Hz | gain |")
    print("|---|---|---|---|---|---|")
    for s in ENVELOPE_SNRS:
        a, b = envelope(only, s), envelope(both, s)
        ga, gb = (a["goodput"] if a else 0.0), (b["goodput"] if b else 0.0)
        print(f"| {s} | {name(a)} | {ga:.3f} | {name(b)} | {gb:.3f} | "
              + (f"x{gb / ga:.2f}" if ga else "") + " |")
    print()
    print("### Pareto ladder (for adapt.MCS)\n")
    for m in both:
        print(f"    Mode({m['threshold']}, \"{m['family']}\", {m['M']}, \"{m['rate']}\", {m['goodput']}),")
    und = sum(c.get("undetected", 0) for cs in cells.values() for c in cs)
    print(f"\nundetected (CRC pass, wrong payload), all coherent cells: {und}")

    rows = _load("results_coherent_blind.json")
    crc = _load("results_coherent_blind_m128crc.json")       # m128 rerun with the CRC arbitration
    if crc:
        rows = [r for r in rows if r["cond"] != "m128"] + crc
    blind = blind_summary(rows)
    if blind:
        print("\n### Blind spot-check (same captures, informed vs blind; per-mode N0)\n")
        print("| cond | mode | SNR dB | packets | informed fails | blind fails | blind failure causes "
              "| blind fails, CRC picks M | median s/discovery | median abs scale error |")
        print("|---|---|---|---|---|---|---|---|---|---|")
        for b in blind:
            causes = ", ".join(f"{k} {v}" for k, v in sorted(b["causes"].items())) or "-"
            print(f"| {b['cond']} | {b['M']}-QAM {b['rate']} | {b['snr']} | {b['packets']} | "
                  f"{b['fails_informed']} | {b['fails_blind']} | {causes} | "
                  f"{'' if b['fails_crc'] is None else b['fails_crc']} | {b['seconds']:.0f} | "
                  f"{b['scale_err']:.1e} |")
        tot = lambda c, k: sum(b[k] for b in blind if b["cond"] == c)
        for c in ("m128", "r128"):
            if any(b["cond"] == c for b in blind):
                crc_ = [b for b in blind if b["cond"] == c and b["fails_crc"] is not None]
                print(f"\n{c}: informed fails {tot(c, 'fails_informed')}/{tot(c, 'packets')}, "
                      f"blind fails {tot(c, 'fails_blind')}/{tot(c, 'packets')}"
                      + (f", with the CRC picking M {sum(b['fails_crc'] for b in crc_)}/"
                         f"{sum(b['packets'] for b in crc_)}" if crc_ else ""))

    out = dict(
        meta=dict(sources=["results_coherent_coarse.json", "results_coherent_fine.json",
                           "results_coherent_fine2.json", "results_coherent_redo.json",
                           "results_coherent_blind.json", "results.json", "results_gray.json"],
                  threshold_rule="lowest 1 dB grid SNR from which PER <= 1% at every higher point; "
                                 "interp = log10(PER)-linear between bracketing points",
                  planning_threshold="max over conditions of the interpolated 1% threshold, "
                                     "per-mode N0 LLRs",
                  goodput="(1-PER)*8P/((T+1)L) at PER 0, P=500 bytes",
                  snr="in-band Es/N0 per transmitted complex sample"),
        coherent=coh, differential=diff, ladder=both,
        envelope=[dict(snr=s, differential_only=(envelope(only, s) or {}).get("goodput", 0.0),
                       differential_mode=name(envelope(only, s)),
                       combined=(envelope(both, s) or {}).get("goodput", 0.0),
                       combined_mode=name(envelope(both, s))) for s in ENVELOPE_SNRS],
        blind=blind,
        cells={f"{k[0]}|{k[1]}|{k[2]}": v for k, v in cells.items()})
    (HERE / "results_coherent.json").write_text(json.dumps(out, indent=1))


if __name__ == "__main__":
    main()
