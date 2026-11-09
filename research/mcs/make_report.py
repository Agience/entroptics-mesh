"""Print the markdown tables for results.md from results*.json.

PER thresholds are read off the 2 dB grid: the lowest grid SNR from which PER
stays <= target at every higher grid SNR. An interpolated estimate is given
too (linear in SNR on log10 PER between the bracketing grid points; a cell with
zero failures counts as PER = 0.5/packets for the interpolation only).
"""

from __future__ import annotations

import json
import math
from pathlib import Path

HERE = Path(__file__).resolve().parent
RATES = ("1/2", "2/3", "3/4", "uncoded")
MS = (2, 4, 8, 16)


def load():
    main = json.loads((HERE / "results.json").read_text())
    rows = list(main["results"])
    extra = HERE / "results_15_25.json"
    if extra.exists():
        rows += json.loads(extra.read_text())["results"]
    return main["meta"], rows


def name(M, r):
    mod = {2: "DBPSK", 4: "DQPSK", 8: "D8PSK", 16: "D16PSK"}[M]
    return f"{mod} {r}"


def cell_index(rows, weight=True):
    idx = {}
    for c in rows:
        if c["weight"] == weight:
            idx[(c["M"], c["rate"], c["snr"])] = c
    return idx


def threshold(cells, target):
    """cells: list sorted by snr (grid only)."""
    grid = None
    for i in range(len(cells)):
        if all(c["per"] <= target for c in cells[i:]):
            grid = cells[i]["snr"]
            break
    if grid is None:
        return None, None
    j = [c["snr"] for c in cells].index(grid)
    if j == 0:
        return grid, None
    lo, hi = cells[j - 1], cells[j]
    f = lambda c: math.log10(max(c["per"], 0.5 / c["packets"]))
    y0, y1, yt = f(lo), f(hi), math.log10(target)
    if y0 == y1:
        return grid, float(hi["snr"])
    x = lo["snr"] + (yt - y0) * (hi["snr"] - lo["snr"]) / (y1 - y0)
    return grid, min(max(x, lo["snr"]), hi["snr"])


def fmt_per(c):
    if c is None:
        return ""
    if c["fails"] == 0:
        return f"0/{c['packets']}"
    if c["fails"] == c["packets"]:
        return f"1 ({c['packets']})" if c["packets"] < 1000 else "1"
    return f"{c['per']:.3g}"


def main():
    meta, rows = load()
    idx = cell_index(rows, True)
    grid = sorted({c["snr"] for c in rows if c["snr"] % 2 == 0})
    allsnr = sorted({c["snr"] for c in rows})
    print("meta:", meta)
    print()
    # PER tables: one table, rows = SNR, columns = MCS (per M block)
    for M in MS:
        print(f"### M = {M}\n")
        hdr = "| SNR dB | raw BER | " + " | ".join(f"PER {r}" for r in RATES) + " |"
        print(hdr)
        print("|" + "---|" * (2 + len(RATES)))
        for s in allsnr:
            ber = idx.get((M, "uncoded", s))
            line = f"| {s} | {ber['ber']:.2e} | " if ber else f"| {s} | | "
            line += " | ".join(fmt_per(idx.get((M, r, s))) for r in RATES) + " |"
            print(line)
        print()

    print("### Thresholds and nominal rates\n")
    print("| MCS | b/s/Hz at PER 0 | PER<=10% grid (interp) | PER<=1% grid (interp) |")
    print("|---|---|---|---|")
    thr = {}
    for M in MS:
        for r in RATES:
            cells = [idx[(M, r, s)] for s in grid]
            g10, i10 = threshold(cells, 0.10)
            g1, i1 = threshold(cells, 0.01)
            thr[(M, r)] = (g10, i10, g1, i1)
            peak = cells[-1]["goodput"] / (1 - cells[-1]["per"]) if cells[-1]["per"] < 1 else float("nan")
            fm = lambda g, i: "not reached" if g is None else (f"{g} ({i:.1f})" if i is not None else f"<= {g}")
            print(f"| {name(M, r)} | {peak:.3f} | {fm(g10, i10)} | {fm(g1, i1)} |")
    print()

    print("### Goodput envelope (best measured MCS per SNR)\n")
    print("| SNR dB | best MCS | its PER | goodput b/s/Hz | best with PER<=1% | goodput | Shannon log2(1+SNR) |")
    print("|---|---|---|---|---|---|---|")
    for s in allsnr:
        cand = [c for c in rows if c["weight"] and c["snr"] == s]
        best = max(cand, key=lambda c: c["goodput"])
        safe = [c for c in cand if c["per"] <= 0.01]
        sb = max(safe, key=lambda c: c["goodput"]) if safe else None
        sh = math.log2(1 + 10 ** (s / 10))
        print(f"| {s} | {name(best['M'], best['rate'])} | {fmt_per(best)} | {best['goodput']:.3f} | "
              + (f"{name(sb['M'], sb['rate'])} | {sb['goodput']:.3f}" if sb else "none | 0")
              + f" | {sh:.2f} |")
    print()

    print("### weight_by_magnitude False vs True, M = 4\n")
    un = cell_index(rows, False)
    print("| SNR dB | " + " | ".join(f"{r} True | {r} False" for r in ("1/2", "2/3", "3/4")) + " |")
    print("|" + "---|" * 7)
    for s in grid:
        if (4, "1/2", s) not in un:
            continue
        print(f"| {s} | " + " | ".join(f"{fmt_per(idx[(4, r, s)])} | {fmt_per(un[(4, r, s)])}"
                                        for r in ("1/2", "2/3", "3/4")) + " |")
    print()
    print("thresholds (weighted False, M=4):")
    for r in ("1/2", "2/3", "3/4"):
        cells = [un[(4, r, s)] for s in grid]
        print(r, "10%:", threshold(cells, 0.10), "1%:", threshold(cells, 0.01))
    und = sum(c.get("undetected", 0) for c in rows)
    print("\nundetected errors (CRC passed, payload wrong), all cells:", und)


if __name__ == "__main__":
    main()
