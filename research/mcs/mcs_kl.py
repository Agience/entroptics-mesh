"""Every planner mode re-measured on the current waveform (every mode lit, K = L): coded PER vs SNR
through the chain the mesh runs, the 1%-PER threshold of each, and its goodput.

Chain (one burst per frame, as ``node.py`` sends and reads it):
  link.symbols -> link.frame(sym, rate)    one fountain symbol (35-byte header + 475-byte block) in
                                           the 512-byte protected frame: 2-byte length, CRC-32,
                                           K=7 (171,133) code, punctured, interleaved
  phy.transmit_burst(plan, key)            TxPlan(rate, M, family, code_rate), K = L = 64, keyed
                                           per-row power, keyed reference row
  emu.air.Air                              free space, 6 km; the receiver's noise is set so the
                                           in-band SNR (noise over the sender's width) is the cell's
  phy.receive_known(..., accept=frames)    the fast path at a known geometry (prior 2 ppm in scale,
                                           5e-4 of the band off in centre), the constant-modulus
                                           read, ``_row_turn`` -- exactly ``node.Node._read``
  link.read_frame                          family, alphabet and code rate learned from the burst:
                                           every hypothesis, the CRC picks; nothing is passed

A frame counts as delivered when the symbol comes back byte-exact.  A CRC pass with the wrong
symbol is tallied as ``undetected``; a pass on another mode's hypothesis as ``wrong_mode``.

Rates: ``matched`` = 2.4 -> 2.4 Msps; ``r1024`` = a 1.0 Msps sender into a 2.4 Msps receiver
(the band-limited emulator channel).  The burst's start is drawn uniformly over 80-120 received
samples (plus the 6 km delay, 48 samples), so its sub-sample position and its row phase in the
capture vary; the capture ends TAIL_ROWS sender rows plus 72-112 received samples after the burst.

Key: ``keyed`` = key 1234 (the mesh's keyed mode); ``open`` = key None (sender-random row power,
public aperture).

Phases (each appends JSON lines to ``--out``):
  coarse  per (mode, rate): SNR = old threshold + (-1, -0.5, 0, .5, 1, 1.5, 2, 3) dB, 100 frames,
          a cell stops at 25 failures
  fine    per (mode, rate): from the coarse cells, 0.2 dB steps from the highest SNR with PER >= 20%
          to 0.6 dB past the lowest from which every coarse cell lost <= 2% (at most 4 dB), plus
          that point + 1, 1.5 and 2 dB (the tail), 600 frames, a cell stops at 60 failures
  low     where a mode's waterfall lies below the coarse grid (its lowest cell lost < 20%): cells
          1-6 dB below its lowest, coarse-sized; then rerun fine, which places its grid from all cells
  open    spot check in open mode (selected modes, both rates), 0.25 dB steps around the keyed
          threshold, 300 frames, stop at 40 failures

    python research/mcs/mcs_kl.py --phase coarse --workers 16 --out research/mcs/kl_cells_fp.jsonl
    python research/mcs/mcs_kl.py --phase low    --workers 16 --out research/mcs/kl_cells_fp.jsonl
    python research/mcs/mcs_kl.py --phase fine   --workers 16 --out research/mcs/kl_cells_fp.jsonl
    python research/mcs/mcs_kl.py --phase open   --workers 16 --out research/mcs/kl_cells_fp.jsonl
    python research/mcs/mcs_kl.py --report research/mcs/kl_cells_fp.jsonl --json research/mcs/results_kL_fp.json

Threshold: per (mode, rate, key), a binomial maximum-likelihood fit of log10 PER = a + b*SNR to the
waterfall cells (observed PER < 0.3, up to 1 dB past the last failure), solved for PER = 1%; its
spread is a parametric bootstrap (each cell's failures redrawn from its fitted PER, 200 draws).
The old method (linear in log10 PER between the bracketing cells) is reported alongside.  The
planning threshold is the worse of the two rates, rounded up to 0.1 dB.

Goodput: 8 * FRAME_BYTES (the protected frame, 4096 bits) per complex sample of the burst at the
sender's rate (reference row included), at PER = 0 -- the planner's convention.  ``goodput_block``
counts the 475-byte fountain block alone.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sys
import time
import zlib
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

for v in ("OMP_NUM_THREADS", "MKL_NUM_THREADS", "OPENBLAS_NUM_THREADS"):
    os.environ.setdefault(v, "1")

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1] / "src"))

import numpy as np  # noqa: E402

RX = 2.4e6
RATES_HZ = {"matched": 2.4e6, "r1024": 1.0e6}
KEY = 1234
TAIL_ROWS = 2   # rows of silence the capture holds after the burst.  The first table (kl_cells.jsonl)
                # needed it: with under ~1 row (240 received samples after a 1.0 Msps burst, 0.5-0.7
                # of a row) the fast path's length read (RxBurst.rows) misread a dense-QAM burst and
                # read_frame skipped or lost the true hypothesis (256-QAM 3/4 at 50 dB, 1.0 -> 2.4
                # Msps, 19/80 frames lost).  RxBurst.rows now checks its edge against the burst's
                # run of rows (0/80 there, kL.md); the tail is kept so the two tables compare.
COARSE = (-1.0, -0.5, 0.0, 0.5, 1.0, 1.5, 2.0, 3.0)
OPEN_MODES = (("differential", 2, "1/2"), ("differential", 4, "3/4"), ("coherent", 16, "1/2"),
              ("coherent", 64, "2/3"), ("coherent", 256, "3/4"))


# The planner table this run re-measured (adapt.MODES before it, K=60 tables: results.md,
# results_gray.json, coherent.md): (family, M, rate, threshold dB, goodput).  The coarse grid sits
# around these thresholds.
OLD_MODES = [
    ("differential", 2, "1/2", 3.2, 0.456), ("differential", 2, "2/3", 5.0, 0.607),
    ("differential", 2, "3/4", 5.7, 0.679), ("differential", 4, "1/2", 8.2, 0.906),
    ("differential", 4, "2/3", 9.4, 1.202), ("differential", 4, "3/4", 10.8, 1.33),
    ("differential", 8, "1/2", 10.9, 1.33), ("differential", 8, "2/3", 13.1, 1.786),
    ("differential", 8, "3/4", 14.7, 1.953), ("differential", 16, "1/2", 14.4, 1.786),
    ("differential", 16, "2/3", 18.8, 2.315), ("differential", 16, "3/4", 19.8, 2.604),
    ("coherent", 4, "1/2", 6.7, 0.906), ("coherent", 4, "2/3", 8.8, 1.202),
    ("coherent", 4, "3/4", 9.7, 1.33), ("coherent", 16, "1/2", 12.3, 1.786),
    ("coherent", 16, "2/3", 14.9, 2.315), ("coherent", 16, "3/4", 16.5, 2.604),
    ("coherent", 64, "1/2", 18.5, 2.604), ("coherent", 64, "2/3", 21.0, 3.472),
    ("coherent", 64, "3/4", 21.9, 3.676), ("coherent", 256, "1/2", 21.9, 3.472),
    ("coherent", 256, "2/3", 27.0, 4.464), ("coherent", 256, "3/4", 30.2, 4.808),
]


def modes():
    return list(OLD_MODES)


def _seed(*parts) -> int:
    return zlib.crc32(repr(parts).encode())


def one(family, M, rate, srate, snr, key, i):
    from entroptics_mesh import link, phy
    from entroptics_mesh.emu.air import Air, Radio, fspl_db
    from entroptics_mesh.providers import TxPlan
    from entroptics_mesh.routing import Header

    rng = np.random.default_rng(_seed(family, M, rate, srate, round(snr, 3), key is None, i))
    msg = bytes(rng.integers(0, 256, 4 * link.BLOCK, dtype=np.uint8))
    sym = link.symbols(Header(b"A" * 8, int(rng.integers(1, 1 << 30)), b"C" * 8, 4), msg,
                       link.BLOCK, 1.0)[i % 4]
    d = 6000.0
    lead = float(rng.uniform(80, 120))
    noise = 20.0 - fspl_db(d, 915e6) - snr + 10 * math.log10(RX / srate)
    air = Air(rate=RX, seed=int(rng.integers(1 << 31)))
    a = air.add(Radio(position=np.array([0.0, 0, 100]), tx_power_dbm=20.0, noise_dbm=noise))
    b = air.add(Radio(position=np.array([d, 0, 100]), tx_power_dbm=20.0, noise_dbm=noise))
    plan = TxPlan(rate=srate, M=M, family=family, code_rate=rate)
    bits = link.frame(sym, rate)
    iq = phy.transmit_burst(bits, plan, key, sender=b"A" * 8, rng=rng)
    air.send(a, lead / RX, iq, rate=None if srate == RX else srate)
    # The capture holds the whole burst and at least TAIL_ROWS rows (at the sender's width) of
    # silence after it: the fast path reads the burst's length off that silence (see TAIL_ROWS).
    y = air.capture(b, 0.0, int((iq.size + TAIL_ROWS * 64) * RX / srate + 240))
    known = phy.Known(64, (srate / RX) * (1 + 2e-6), 5e-4)
    t0 = time.process_time()
    try:
        rx = phy.receive_known(y, key, known, accept=lambda r: link.read_frame(r) is not None)
    except Exception as e:     # a receive-path exception is a lost frame, tallied apart (``errors``)
        return dict(ok=False, undetected=False, wrong_mode=False, sync=True, skipped=False,
                    error=f"{type(e).__name__}: {e}"[:200], samples=int(iq.size),
                    read_s=time.process_time() - t0, decode_s=0.0)
    t1 = time.process_time()
    g = link.read_frame(rx) if rx.locked else None
    t2 = time.process_time()
    got, mode = g if g is not None else (None, None)
    ok = got is not None and got.data == sym.data and got.header == sym.header
    # Why a frame was lost (diagnostic only, it uses what was sent): the true hypothesis's own soft
    # bits -- raw BER > 0.2 means the geometry was lost (start/carrier), not the code; a pinned
    # decode that passes means read_frame skipped the true hypothesis (the length rule).
    sync = skipped = False
    if not ok:
        from entroptics_mesh import fec
        sb = rx.soft_bits(family, M, bits.size) if rx.locked else None
        sync = sb is None or float(np.mean((sb < 0) != (bits != 0))) > 0.2
        skipped = sb is not None and fec.decode_packet(sb, rate=rate) == sym.pack()
    return dict(ok=bool(ok), undetected=bool(got is not None and not ok),
                wrong_mode=bool(ok and mode != (family, M, rate)), sync=bool(sync),
                skipped=bool(skipped), samples=int(iq.size), read_s=t1 - t0, decode_s=t2 - t1)


def run_cell(job):
    family, M, rate, rname, snr, keyname, n_max, stop = job
    key = KEY if keyname == "keyed" else None
    srate = RATES_HZ[rname]
    t0 = time.time()
    n = fails = und = wm = sync = skipped = 0
    errors = []
    samples, rd, dc = set(), [], []
    for i in range(n_max):
        r = one(family, M, rate, srate, snr, key, i)
        n += 1
        fails += not r["ok"]
        und += r["undetected"]
        wm += r["wrong_mode"]
        sync += r["sync"]
        if "error" in r:
            errors.append((i, r["error"]))
        skipped += r["skipped"]
        samples.add(r["samples"])
        rd.append(r["read_s"])
        dc.append(r["decode_s"])
        if fails >= stop:
            break
    return dict(family=family, M=M, rate=rate, rx=rname, snr=round(snr, 3), key=keyname,
                frames=n, fails=fails, undetected=und, wrong_mode=wm, sync=sync, skipped=skipped,
                errors=errors,
                samples=sorted(samples),
                read_ms=1e3 * float(np.median(rd)), decode_ms=1e3 * float(np.median(dc)),
                wall_s=time.time() - t0)


def load(path):
    p = Path(path)
    if not p.exists():
        return []
    return [json.loads(x) for x in p.read_text().splitlines() if x.strip()]


def merged(cells):
    """Cells pooled per (family, M, rate, rx, key, snr)."""
    out = {}
    for c in cells:
        k = (c["family"], c["M"], c["rate"], c["rx"], c["key"], round(c["snr"], 3))
        o = out.setdefault(k, dict(frames=0, fails=0, undetected=0, wrong_mode=0, sync=0, skipped=0,
                                   diagnosed=0, errors=0, samples=set(), read_ms=[], decode_ms=[]))
        for f in ("frames", "fails", "undetected", "wrong_mode"):
            o[f] += c[f]
        o["errors"] += len(c.get("errors", ()))
        if "sync" in c:                      # cells run before the diagnostic was added lack it
            o["sync"] += c["sync"]
            o["skipped"] += c["skipped"]
            o["diagnosed"] += c["fails"]
        o["samples"] |= set(c["samples"])
        o["read_ms"].append(c["read_ms"])
        o["decode_ms"].append(c["decode_ms"])
    return out


def jobs_coarse():
    out = []
    for fam, M, rate, thr, _ in modes():
        for rname in RATES_HZ:
            for dx in COARSE:
                out.append((fam, M, rate, rname, round(thr + dx, 3), "keyed", 100, 25))
    return out


def jobs_fine(cells):
    m = merged(cells)
    out = []
    for fam, M, rate, thr, _ in modes():
        for rname in RATES_HZ:
            pts = sorted((k[5], v["fails"] / v["frames"]) for k, v in m.items()
                         if k[:5] == (fam, M, rate, rname, "keyed"))
            if not pts:
                continue
            lo = max([s for s, p in pts if p >= 0.2], default=pts[0][0] - 1.0)
            # the lowest SNR from which every coarse cell lost <= 2%
            hi = min([s for s, _ in pts if s > lo and all(p <= 0.02 for t, p in pts if t >= s)],
                     default=pts[-1][0] + 1.0)
            grid = list(np.arange(lo, min(hi + 0.6, lo + 4.0) + 1e-9, 0.2)) + [hi + 1.0, hi + 1.5, hi + 2.0]
            for s in grid:
                out.append((fam, M, rate, rname, round(float(s), 3), "keyed", 600, 60))
    return out


def jobs_low(cells):
    """Below the coarse grid, where a mode's waterfall lies lower than the old threshold - 1 dB
    (the coarse grid's floor): for every (mode, rate) whose lowest keyed cell lost under 20%,
    cells 1, 2, ... 6 dB below it, coarse-sized.  The fine phase then places its grid from all
    the cells."""
    m = merged(cells)
    out = []
    for fam, M, rate, thr, _ in modes():
        for rname in RATES_HZ:
            pts = sorted((k[5], v["fails"] / v["frames"]) for k, v in m.items()
                         if k[:5] == (fam, M, rate, rname, "keyed"))
            if not pts or pts[0][1] >= 0.2:
                continue
            for dx in range(1, 7):
                out.append((fam, M, rate, rname, round(pts[0][0] - dx, 3), "keyed", 100, 25))
    return out


def jobs_open(cells):
    th = {}
    for k, r in thresholds(merged(cells), "keyed").items():
        th[k] = r["fit"] if r["fit"] is not None else r["interp"]
    out = []
    for fam, M, rate in OPEN_MODES:
        for rname in RATES_HZ:
            t = th.get((fam, M, rate, rname))
            if t is None:
                continue
            for dx in np.arange(-0.5, 1.51, 0.25):
                out.append((fam, M, rate, rname, round(float(t + dx), 3), "open", 300, 40))
    return out


def _fit(s, n, k):
    """Binomial ML fit of log10 PER = a + b s (PER capped at 1); returns (a, b)."""
    from scipy.optimize import minimize
    s, n, k = (np.asarray(x, float) for x in (s, n, k))
    c = float(np.mean(s))
    t = s - c                                     # centred, for conditioning
    LN = math.log(10)

    def nll(z):
        a, b = z
        lp = np.minimum((a + b * t) * LN, -1e-12)
        q = -np.expm1(lp)
        f = -float(np.sum(k * lp + (n - k) * np.log(q)))
        # d/dlp of the term: k - (n - k) * p / q
        g = -(k - (n - k) * np.exp(lp) / q) * LN * ((a + b * t) * LN < -1e-12)
        return f, np.array([np.sum(g), np.sum(g * t)])
    y = np.log10((k + 0.5) / (n + 1.0))
    b0 = float(np.polyfit(t, y, 1)[0]) if len(t) > 1 and np.ptp(t) > 0 else -1.0
    b0 = min(b0, -0.1)
    a0 = float(np.mean(y))
    r = minimize(nll, [a0, b0], jac=True, method="L-BFGS-B")
    a, b = r.x
    return a - b * c, b


def _window(pts):
    """The waterfall cells: PER < 0.3, up to 1 dB past the last failure."""
    last = max([s for s, n, k in pts if k > 0], default=None)
    return [(s, n, k) for s, n, k in pts if k / n < 0.3 and (last is None or s <= last + 1.0)]


def _interp(pts):
    """The old method: linear in log10 PER between the bracketing cells (0 failures -> 0.5/n)."""
    for (s1, n1, k1), (s2, n2, k2) in zip(pts, pts[1:]):
        p1, p2 = k1 / n1, k2 / n2
        if p1 > 0.01 >= p2:
            l1, l2 = math.log10(p1), math.log10(max(p2, 0.5 / n2))
            return s1 + (s2 - s1) * (l1 + 2) / (l1 - l2) if l1 != l2 else s2
    return None


def thresholds(m, keyname, boots=200):
    out = {}
    groups = {}
    for k, v in m.items():
        if k[4] == keyname:
            groups.setdefault(k[:4], []).append((k[5], v["frames"], v["fails"]))
    rng = np.random.default_rng(7)
    for g, pts in groups.items():
        pts = sorted(pts)
        w = _window(pts)
        fit = ci = slope = None
        # A fit needs cells on both sides of 1%: one that lost more (>= 2%), one that lost less.
        if len(w) >= 3 and any(k / n >= 0.02 for _, n, k in w) and any(k / n <= 0.01 for _, n, k in w):
            a, b = _fit(*zip(*w))
            if b < 0 and min(x[0] for x in w) - 0.5 <= (-2 - a) / b <= max(x[0] for x in w):
                fit, slope = (-2 - a) / b, b
                s = np.array([x[0] for x in w])
                n = np.array([x[1] for x in w])
                p = np.minimum(10 ** (a + b * s), 1.0)
                bs = []
                for _ in range(boots):
                    kk = rng.binomial(n, p)
                    if kk.sum() == 0:
                        continue
                    a2, b2 = _fit(s, n, kk)
                    if b2 < 0:
                        bs.append((-2 - a2) / b2)
                ci = (float(np.percentile(bs, 5)), float(np.percentile(bs, 95))) if bs else None
        out[g] = dict(fit=None if fit is None else float(fit), ci90=ci,
                      slope_dec_per_db=None if slope is None else float(slope),
                      interp=_interp(pts), cells=pts)
    return out


def report(path, out_json):
    from entroptics_mesh import link
    cells = load(path)
    m = merged(cells)
    old = {(f, M, r): (t, g) for f, M, r, t, g in modes()}
    th = {k: thresholds(m, k) for k in ("keyed", "open")}
    rows = []
    for (fam, M, rate), (t_old, g_old) in old.items():
        per_rate = {}
        for rname in RATES_HZ:
            r = th["keyed"].get((fam, M, rate, rname))
            per_rate[rname] = r
        samp = sorted({s for k, v in m.items() if k[:3] == (fam, M, rate) for s in v["samples"]})
        n_samp = samp[0] if samp else None
        vals = [per_rate[x]["fit"] if per_rate[x] and per_rate[x]["fit"] is not None
                else (per_rate[x]["interp"] if per_rate[x] else None) for x in RATES_HZ]
        plan = (math.ceil(max(vals) * 10 - 1e-6) / 10) if all(v is not None for v in vals) else None
        reads = [x for k, v in m.items() if k[:3] == (fam, M, rate) and k[4] == "keyed" for x in v["read_ms"]]
        decs = [x for k, v in m.items() if k[:3] == (fam, M, rate) and k[4] == "keyed" for x in v["decode_ms"]]
        und = sum(v["undetected"] for k, v in m.items() if k[:3] == (fam, M, rate))
        wm = sum(v["wrong_mode"] for k, v in m.items() if k[:3] == (fam, M, rate))
        nfr = sum(v["frames"] for k, v in m.items() if k[:3] == (fam, M, rate))
        opn = {x: th["open"].get((fam, M, rate, x)) for x in RATES_HZ}
        rows.append(dict(
            family=fam, M=M, rate=rate, old_threshold=t_old, old_goodput=g_old,
            threshold={x: (per_rate[x] or {}).get("fit") for x in RATES_HZ},
            threshold_ci90={x: (per_rate[x] or {}).get("ci90") for x in RATES_HZ},
            threshold_interp={x: (per_rate[x] or {}).get("interp") for x in RATES_HZ},
            slope_dec_per_db={x: (per_rate[x] or {}).get("slope_dec_per_db") for x in RATES_HZ},
            planning_threshold=plan, burst_samples=n_samp, burst_samples_all=samp,
            goodput=None if not n_samp else round(8 * link.FRAME_BYTES / n_samp, 3),
            goodput_block=None if not n_samp else round(8 * link.BLOCK / n_samp, 3),
            frames=nfr, undetected=und, wrong_mode=wm,
            read_ms_median=float(np.median(reads)) if reads else None,
            decode_ms_median=float(np.median(decs)) if decs else None,
            open_threshold={x: (None if opn[x] is None else (opn[x]["fit"] if opn[x]["fit"] is not None
                                                              else opn[x]["interp"])) for x in RATES_HZ}))
    cells_out = [dict(family=k[0], M=k[1], rate=k[2], rx=k[3], key=k[4], snr=k[5], frames=v["frames"],
                      fails=v["fails"], undetected=v["undetected"], wrong_mode=v["wrong_mode"],
                      diagnosed=v["diagnosed"], sync_lost=v["sync"], length_skipped=v["skipped"],
                      receive_errors=v["errors"],
                      burst_samples=sorted(v["samples"]))
                 for k, v in sorted(m.items())]
    meta = dict(L=64, K=64, key=KEY, frame_bytes=link.FRAME_BYTES, block_bytes=link.BLOCK,
                rates_hz=RATES_HZ, rx_hz=RX, snr="in-band Es/N0 per transmitted complex sample",
                chain="link.frame -> phy.transmit_burst -> emu.air.Air -> phy.receive_known(accept) "
                      "-> link.read_frame", script="research/mcs/mcs_kl.py")
    Path(out_json).write_text(json.dumps(dict(meta=meta, modes=rows, cells=cells_out), indent=1))
    for r in rows:
        t = r["threshold"]
        print(f"{r['family'][:4]} M={r['M']:3d} {r['rate']}  old {r['old_threshold']:5.1f} "
              f"G {r['old_goodput']:.3f} | matched {t['matched']} r1024 {t['r1024']} "
              f"-> plan {r['planning_threshold']} G {r['goodput']} | open {r['open_threshold']}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--phase", choices=("coarse", "low", "fine", "open"))
    ap.add_argument("--workers", type=int, default=16)
    ap.add_argument("--out", default=str(HERE / "kl_cells_fp.jsonl"))
    ap.add_argument("--report", default=None)
    ap.add_argument("--json", default=str(HERE / "results_kL_fp.json"))
    ap.add_argument("--only", default=None, help="family:M:rate filter (comma list)")
    a = ap.parse_args()
    if a.report:
        report(a.report, a.json)
        return
    cells = load(a.out)
    jobs = {"coarse": jobs_coarse, "low": lambda: jobs_low(cells), "fine": lambda: jobs_fine(cells),
            "open": lambda: jobs_open(cells)}[a.phase]()
    done = {(c["family"], c["M"], c["rate"], c["rx"], round(c["snr"], 3), c["key"]) for c in cells}
    jobs = [j for j in jobs if (j[0], j[1], j[2], j[3], round(j[4], 3), j[5]) not in done]
    if a.only:
        keep = {tuple(x.split(":")) for x in a.only.split(",")}
        jobs = [j for j in jobs if (j[0], str(j[1]), j[2]) in keep]
    # Slowest first: high SNR (all frames run), coherent dense alphabets.
    jobs.sort(key=lambda j: (-j[6], -j[4]))
    print(f"{a.phase}: {len(jobs)} cells", flush=True)
    t0 = time.time()
    with ProcessPoolExecutor(max_workers=a.workers) as ex, open(a.out, "a") as fh:
        futs = [ex.submit(run_cell, j) for j in jobs]
        for f in as_completed(futs):
            r = f.result()
            fh.write(json.dumps(r) + "\n")
            fh.flush()
            print(f"{r['family'][:4]} M={r['M']:3d} {r['rate']} {r['rx']:7s} {r['key']} {r['snr']:6.2f} dB "
                  f"{r['fails']}/{r['frames']} und={r['undetected']} wm={r['wrong_mode']} "
                  f"read {r['read_ms']:.0f} dec {r['decode_ms']:.0f} ms  {r['wall_s']:.0f}s "
                  f"[{time.time() - t0:.0f}s]", flush=True)


if __name__ == "__main__":
    main()
