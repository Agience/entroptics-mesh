"""Coded coherent square QAM on the keyed modes: PER vs SNR through the real codec.

Chain: fec.encode_packet (2-byte length + payload + CRC-32, K=7 (171,133), zero tail, punctured,
interleaved -- the frame mcs_table.py measures) -> waveform.compose_qam (Gray square QAM, one keyed
reference row) -> channel -> the codec's coherent read -> fec.soft_bits_from_qam (max-log LLRs, N0
measured off the symbols' decision residuals) -> fec.decode_packet.  Every packet is decoded twice:
with one global N0 (``fails``) and with N0 per mode (``fails_permode``, the table's decoder); at
matched rate a third time with the gains read off the noiseless capture (``fails_genie``, pricing
the single-reference gain read).  A packet fails if its CRC fails (a CRC pass with the wrong payload
also counts as a failure and is tallied as ``undetected``).

Conditions (``COND``):
  m64   matched rate,  L=64,  K=60   (the differential table's geometry)
  m128  matched rate,  L=128, K=124
  r128  rho = 0.514403 (tests/_ratechannel.at_rate: exact band-limited channel), L=128, K=124

Known geometry (the table): the read is handed the true offset/phase/scale and carrier 0, as
mcs_table.py hands read_soft the true geometry: ``_coherent_project`` (the joint read at a scale)
+ ``_dekeyed`` (gains from the single keyed reference row).  So the table measures modulation +
code + the codec's reference-row gain read, not synchronization.

Blind spot-check (``--phase blind``, one job per packet): the same capture, with a carrier, read
twice -- informed (true geometry and carrier) and blind (``discover`` over the mesh's width window
at matched rate, ``_discover_scaled(y, key, (L,), carrier_span=0.3)`` at rho, then
``_coherent_read`` + the payload test + ``_qam_order`` as ``decode`` makes them).  A blind packet
fails if the width is missed, the payload is not named coherent, M is misread, or the CRC fails.
``ok_blind_crc`` is the same read with the frame's CRC choosing M instead of the closures.

SNR: in-band Es/N0 per transmitted complex sample.  The composed burst is unit-RMS.  Matched: complex
AWGN of variance N0 = 10**(-SNR/10) per sample.  At rho the receiver's band is 1/rho wider, so white
noise of variance N0/rho per received sample puts N0 per transmitted sample in the signal's band --
the same SNR the planner predicts (noise over the occupied width).  The rate channel's at_rate
snr_db would be SNR - 10log10(1/rho).

Goodput, as mcs_table.py: G = (1 - PER) * 8P / ((T + 1) * L), T = ceil(ceil(Nc / log2 M) / K).

Run (each phase writes its own json, read by make_coherent_report.py):
    python research/mcs/mcs_coherent.py --phase coarse --packets 60 --workers 16
    python research/mcs/mcs_coherent.py --phase fine --workers 16     # global-N0 waterfalls
    python research/mcs/mcs_coherent.py --phase fine2 --workers 16    # per-mode-N0 waterfalls
    python research/mcs/mcs_coherent.py --phase redo --workers 16     # cells "fine" stopped early
    python research/mcs/mcs_coherent.py --phase blind --conds m128,r128 --workers 16
    python research/mcs/mcs_coherent.py --phase blind --conds m128 --out results_coherent_blind_m128crc.json
    python research/mcs/make_coherent_report.py
(The first fine/fine2 runs stopped a cell at 50 global-N0 failures; the rule is now 50 failures of
both decodes, and ``redo`` reran the cells the old rule had cut short.  The m128 blind rows were rerun
after ``ok_blind_crc`` was added -- same seeds, same captures.)
"""
from __future__ import annotations

import argparse
import json
import math
import sys
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(ROOT / "src"))
# the sibling entroptics-rf checkout, not whatever copy an environment has installed
for rf in (ROOT.parent / "entroptics-rf" / "tests", ROOT.parent / "entroptics-rf" / "src"):
    sys.path.insert(0, str(rf))
sys.path.insert(0, str(HERE))

from entroptics_rf import waveform as wf  # noqa: E402
from entroptics_mesh import fec  # noqa: E402
from _ratechannel import at_rate  # noqa: E402

SEED, P = 1234, 500
RHO = 0.514403
COND = {"m64": (64, 60, 1.0), "m128": (128, 124, 1.0), "r128": (128, 124, RHO)}
MS = (4, 16, 64, 256)
RATES = ("1/2", "2/3", "3/4")
RATE_VALUE = {"1/2": 0.5, "2/3": 2 / 3, "3/4": 0.75}
SNR_RANGE = {4: range(2, 14), 16: range(7, 21), 64: range(13, 28), 256: range(19, 34)}
DELAY = 0.37                 # sub-sample start of the rate channel's capture
PAD_ROWS = 2                 # silence either side of the burst (the joint read's window needs it)
BLIND_PAD_ROWS = 2           # the blind capture: silence either side of the burst ...
BLIND_LEAD = 37              # ... and the burst off the capture's row grid.  Measured: with 8 silent
                             # rows each side of a 12-row burst, matched discovery locks on noise
                             # (the dark mode averages over every row the capture holds)


def burst_rows(rate: str, M: int, K: int) -> int:
    b = int(math.log2(M))
    n_sym = -(-fec.packet_coded_length(P, rate) // b)
    return 1 + -(-n_sym // K)


def goodput(per: float, rate: str, M: int, L: int, K: int) -> float:
    return (1 - per) * 8 * P / (burst_rows(rate, M, K) * L)


def _noise(rng, n, var):
    return math.sqrt(var / 2) * (rng.standard_normal(n) + 1j * rng.standard_normal(n))


def capture(tx_iq, L: int, rho: float, n0: float, rng, carrier: float = 0.0,
            pad_rows: int = PAD_ROWS, lead: int = 0):
    """The received capture and the true geometry of the reference row.  ``carrier`` is in
    cycles per received sample.  Returns (y, y_clean, off, phase, r): the integer read at matched
    rate takes ``offset`` modulo L, so the reference is row ``r`` of its screen; the continuous read
    starts at ``off - phase`` itself, so there ``r`` is 0.  ``lead`` extra silent samples put the
    burst off a row boundary of the capture."""
    pre = pad_rows * L + int(lead)
    tx = np.concatenate([np.zeros(pre, dtype=complex), tx_iq, np.zeros(pad_rows * L, dtype=complex)])
    if rho == 1.0:
        clean = tx * np.exp(2j * np.pi * carrier * np.arange(tx.size)) if carrier else tx
        s0, r = float(int(lead) % L), pad_rows + int(lead) // L
    else:
        n = int((tx.size - 1 - DELAY) / rho)
        clean = at_rate(tx, rho, n, delay=DELAY, carrier=carrier)
        s0, r = (pre - DELAY) / rho, 0
    y = clean + _noise(rng, clean.size, n0 / rho)
    off = int(math.ceil(s0))
    return y, clean, off, off - s0, r


def informed_Y(y, L, K, rho, off, phase, r, carrier_row=0.0, clean=None):
    """De-keyed symbols at the true geometry; with ``clean`` the reference gains are read off the
    noiseless capture instead (the genie, to price the single-reference gain read)."""
    A = wf._coherent_project(y, L, K, SEED, off, phase, carrier_row, scale=rho)
    Y = wf._dekeyed(A, r, K, SEED, carrier_row, L=L)
    if clean is None:
        return Y, None
    Ac = wf._coherent_project(clean, L, K, SEED, off, phase, carrier_row, scale=rho)
    A2 = A.copy()
    A2[r] = Ac[r]
    return Y, wf._dekeyed(A2, r, K, SEED, carrier_row, L=L)


def _decode(sym, M, rate, n_bits, payload, per_mode_K=None):
    # N0 measured off the burst's own symbols: per mode (symbol i on mode i % K) or one for all
    soft = fec.soft_bits_from_qam(sym, M, K=per_mode_K)[:n_bits]
    out = fec.decode_packet(soft, rate=rate)
    return out == payload, out is not None and out != payload, soft


def run_cell(args) -> dict:
    cond, M, rate, snr, n_packets, stop_after, tag = args
    L, K, rho = COND[cond]
    b = int(math.log2(M))
    rng = np.random.default_rng([MS.index(M), RATES.index(rate), snr + 100,
                                 list(COND).index(cond), tag])
    n0 = 10 ** (-snr / 10)
    sent = fails = fails_mode = fails_genie = undetected = bit_err = bit_tot = 0
    nv_ratio = []
    genie = rho == 1.0
    t0 = time.time()
    for _ in range(n_packets):
        payload = rng.integers(0, 256, P, dtype=np.uint8).tobytes()
        tx = fec.encode_packet(payload, frame_bytes=P, rate=rate)
        iq = wf.compose_qam(tx, K, L, SEED, M=M).astype(np.complex128)
        y, clean, off, ph, r0 = capture(iq, L, rho, n0, rng)
        Y, Yg = informed_Y(y, L, K, rho, off, ph, r0, clean=clean if genie else None)
        n_sym = -(-tx.size // b)
        sym = Y.ravel()[:n_sym]
        ok, und, soft = _decode(sym, M, rate, tx.size, payload)
        okm, _, _ = _decode(sym, M, rate, tx.size, payload, per_mode_K=K)
        nv_ratio.append(float(fec.qam_noise_var(sym, M)) / n0)
        bit_err += int(np.sum((soft < 0) != (tx != 0)))
        bit_tot += tx.size
        if genie:
            okg, _, _ = _decode(Yg.ravel()[:n_sym], M, rate, tx.size, payload)
            fails_genie += int(not okg)
        sent += 1
        fails += int(not ok)
        fails_mode += int(not okm)
        undetected += int(und)
        if stop_after and sent >= stop_after and fails == sent:
            break
        if sent >= 100 and min(fails, fails_mode) >= 50:
            break          # both decodes well above 1%: PER known to ~15%, all a threshold needs
    per = fails / sent
    return dict(cond=cond, L=L, K=K, rho=rho, M=M, rate=rate, snr=snr, packets=sent, fails=fails,
                per=per, fails_permode=fails_mode, fails_genie=fails_genie if genie else None,
                undetected=undetected, ber=bit_err / bit_tot,
                n0_measured_over_true=float(np.median(nv_ratio)),
                goodput=goodput(per, rate, M, L, K), rows=burst_rows(rate, M, K),
                nominal=RATE_VALUE[rate] * b * K / L, seconds=time.time() - t0)


# ---------------------------------------------------------------------------
# blind spot-check
# ---------------------------------------------------------------------------

CARRIER = {1.0: 3e-4, RHO: 2e-4}       # cycles per received sample


def blind_cell(args) -> dict:
    """One packet, read twice on the same capture: informed (true geometry and carrier) and blind
    (discovery, then the coherent read and order read exactly as ``decode`` makes them).  Both
    decode with per-mode N0 LLRs, as the table does."""
    cond, M, rate, snr, k = args
    L, K, rho = COND[cond]
    b = int(math.log2(M))
    rng = np.random.default_rng([MS.index(M), RATES.index(rate), snr + 100,
                                 list(COND).index(cond), 77, k])
    n0 = 10 ** (-snr / 10)
    f = CARRIER[rho]
    c_row = f * L / rho                   # cycles per row, the module's carrier unit
    payload = rng.integers(0, 256, P, dtype=np.uint8).tobytes()
    tx = fec.encode_packet(payload, frame_bytes=P, rate=rate)
    iq = wf.compose_qam(tx, K, L, SEED, M=M).astype(np.complex128)
    y, _, off, ph, r0 = capture(iq, L, rho, n0, rng, carrier=f, pad_rows=BLIND_PAD_ROWS,
                                lead=BLIND_LEAD)
    n_sym = -(-tx.size // b)
    Y, _ = informed_Y(y, L, K, rho, off, ph, r0, carrier_row=c_row)
    ok_i = _decode(Y.ravel()[:n_sym], M, rate, tx.size, payload, per_mode_K=K)[0]
    t = time.time()
    if rho == 1.0:
        d = wf.discover(y, SEED, range(L - 4, L + 5))   # the mesh receiver's width window
    else:
        d = wf._discover_scaled(y, SEED, (L,), carrier_span=0.3)
    res = dict(cond=cond, M=M, rate=rate, snr=snr, k=k, ok_informed=bool(ok_i), ok_blind=False,
               outcome="", L_read=d.L, K_read=None, M_read=None, scale_err=float(d.scale - rho),
               ok_blind_crc=False, M_crc=None, undetected_crc=0)
    if not (d.locked and d.L == L):
        res["outcome"] = "width/lock"
    else:
        K_occ = int(d.K) + (wf.BEACON_MODES if d.beacon else 0)
        Yb, _, _ = wf._coherent_read(y, d.L, K_occ, SEED, d.offset, d.phase, d.carrier,
                                     d.scale)
        res["K_read"] = K_occ
        # The frame arbitrates instead: decode the coherent read at every square order and keep
        # the one whose CRC passes -- an exact test, no level -- whatever the fourth-power and
        # lattice closures said.  (The frame length is fixed per order by encode_packet.)
        for Mh in MS:
            bh = int(math.log2(Mh))
            nh = fec.packet_coded_length(P, rate)
            if Yb.size * bh < nh:
                continue
            soft = fec.soft_bits_from_qam(Yb.ravel()[:-(-nh // bh)], Mh, K=K_occ)[:nh]
            out = fec.decode_packet(soft, rate=rate)
            if out is not None:
                res["M_crc"] = int(Mh)
                res["ok_blind_crc"] = out == payload
                res["undetected_crc"] = int(out != payload)
                break
        if wf._fourth_closure(Yb) <= d.closure:
            res["outcome"] = "not coherent"
        else:
            Mb = wf._qam_order(Yb)[0]
            res["M_read"] = int(Mb)
            if Mb != M:
                res["outcome"] = "M misread"
            elif Yb.size < n_sym:
                res["outcome"] = "short"
            else:
                ok_b = _decode(Yb.ravel()[:n_sym], M, rate, tx.size, payload, per_mode_K=K_occ)[0]
                res["ok_blind"] = bool(ok_b)
                res["outcome"] = "ok" if ok_b else "crc"
    res["seconds_blind"] = time.time() - t
    return res


# ---------------------------------------------------------------------------

def _run(jobs, fn, workers, label):
    out = []
    with ProcessPoolExecutor(max_workers=workers) as ex:
        for r in ex.map(fn, jobs):
            out.append(r)
            print(f"{label} {r['cond']} M={r['M']:3d} {r['rate']} {r['snr']:3d} dB  "
                  f"PER={r['per']:.4f} ({r['fails']}/{r['packets']}) mode={r['fails_permode']} "
                  f"genie={r['fails_genie']} BER={r['ber']:.2e} n0={r['n0_measured_over_true']:.2f} "
                  f"G={r['goodput']:.3f} {r['seconds']:.0f}s", flush=True)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--phase", choices=("coarse", "fine", "fine2", "redo", "blind"), required=True)
    ap.add_argument("--workers", type=int, default=16)
    ap.add_argument("--packets", type=int, default=None)
    ap.add_argument("--conds", default=",".join(COND))
    ap.add_argument("--out", default=None, help="blind phase: output file name in research/mcs")
    a = ap.parse_args()
    conds = a.conds.split(",")
    t0 = time.time()
    if a.phase == "coarse":
        n = a.packets or 100
        jobs = [(c, M, r, s, n, 20, 1) for c in conds for M in MS for r in RATES
                for s in SNR_RANGE[M]]
        jobs.sort(key=lambda j: (-j[1], -j[3]))
        res = _run(jobs, run_cell, a.workers, "coarse")
        out = HERE / "results_coherent_coarse.json"
    elif a.phase == "fine":
        n = a.packets or 500
        coarse = json.loads((HERE / "results_coherent_coarse.json").read_text())["results"]
        jobs = []
        for c in conds:
            for M in MS:
                for r in RATES:
                    cells = sorted((x for x in coarse if (x["cond"], x["M"], x["rate"]) == (c, M, r)),
                                   key=lambda x: x["snr"])
                    # from the last SNR where most packets failed, to two points past the first
                    # SNR with no failures: the waterfall and its foot
                    hi_fail = max((x["snr"] for x in cells if x["per"] >= 0.5), default=cells[0]["snr"] - 1)
                    zero = [x["snr"] for x in cells if x["fails"] == 0 and x["snr"] > hi_fail]
                    top = (zero[0] + 2) if zero else cells[-1]["snr"]
                    jobs += [(c, M, r, s, n, 40, 2) for s in range(hi_fail + 1, top + 1)]
        jobs.sort(key=lambda j: (j[0] != "r128", -j[1]))
        res = _run(jobs, run_cell, a.workers, "fine")
        out = HERE / "results_coherent_fine.json"
    elif a.phase == "fine2":
        # the per-mode-N0 decode's waterfall sits lower than the global one's, partly below the
        # range "fine" chose: the same rule on fails_permode, at SNRs "fine" did not run
        n = a.packets or 500
        have = {}
        for name in ("results_coherent_coarse.json", "results_coherent_fine.json"):
            for x in json.loads((HERE / name).read_text())["results"]:
                have[(x["cond"], x["M"], x["rate"], x["snr"])] = dict(x, fine=name.endswith("fine.json"))
        jobs = []
        for c in conds:
            for M in MS:
                for r in RATES:
                    cells = sorted((x for k, x in have.items() if k[:3] == (c, M, r)),
                                   key=lambda x: x["snr"])
                    pm = lambda x: x["fails_permode"] / x["packets"]
                    hi_fail = max((x["snr"] for x in cells if pm(x) >= 0.5), default=cells[0]["snr"] - 1)
                    zero = [x["snr"] for x in cells if x["fails_permode"] == 0 and x["snr"] > hi_fail]
                    top = (zero[0] + 2) if zero else cells[-1]["snr"]
                    jobs += [(c, M, r, s_, n, 40, 3) for s_ in range(hi_fail + 1, top + 1)
                             if not have.get((c, M, r, s_), {}).get("fine")]
        jobs.sort(key=lambda j: (j[0] != "r128", -j[1]))
        res = _run(jobs, run_cell, a.workers, "fine2")
        out = HERE / "results_coherent_fine2.json"
    elif a.phase == "redo":
        # cells "fine"/"fine2" stopped at 50 global-N0 failures while the per-mode decode had
        # fewer: rerun on the same packets (same tag) until both decodes reach 50 or 500 packets
        n = a.packets or 500
        jobs = []
        for name, tag in (("results_coherent_fine.json", 2), ("results_coherent_fine2.json", 3)):
            for x in json.loads((HERE / name).read_text())["results"]:
                if x["cond"] in conds and x["packets"] < n and x["fails_permode"] < 50                         and x["fails"] < x["packets"]:
                    jobs.append((x["cond"], x["M"], x["rate"], x["snr"], n, 40, tag))
        jobs.sort(key=lambda j: (j[0] != "r128", -j[1]))
        res = _run(jobs, run_cell, a.workers, "redo")
        out = HERE / "results_coherent_redo.json"
    else:
        # per packet, so a slow discovery holds one worker, not a whole cell; at the grid point
        # where the table's per-mode PER reaches 1%, and above it (and below it at matched rate,
        # where discovery is cheap).  Discovery at rho costs minutes per capture.
        from make_coherent_report import coherent_cells, permode_threshold
        cells = coherent_cells()
        plan = {"m128": ((-1, 0, 2), a.packets or 10), "r128": ((0, 2), a.packets or 4)}
        jobs = []
        for c in conds:
            if c not in plan:
                continue
            offs, n = plan[c]
            for M in MS:
                for r in RATES:
                    thr = permode_threshold(cells[(c, M, r)])[0]
                    jobs += [(c, M, r, thr + o, k) for o in offs for k in range(n)]
        jobs.sort(key=lambda j: (j[0] != "r128", -j[1]))
        out = HERE / (a.out or "results_coherent_blind.json")
        res = []
        with ProcessPoolExecutor(max_workers=a.workers) as ex:
            futs = [ex.submit(blind_cell, j) for j in jobs]
            for fu in as_completed(futs):
                r = fu.result()
                res.append(r)
                print(f"blind {r['cond']} M={r['M']:3d} {r['rate']} {r['snr']:3d} dB #{r['k']}  "
                      f"informed {'ok' if r['ok_informed'] else 'FAIL'}  blind {r['outcome']}  "
                      f"L={r['L_read']} K={r['K_read']} M={r['M_read']} dscale={r['scale_err']:.1e} "
                      f"{r['seconds_blind']:.0f}s", flush=True)
                out.write_text(json.dumps(dict(meta=dict(partial=True), results=res), indent=1))
    meta = dict(phase=a.phase, conds={c: COND[c] for c in conds}, seed=SEED, payload_bytes=P,
                snr_definition="in-band Es/N0 per transmitted complex sample, unit-RMS burst",
                wall_seconds=time.time() - t0)
    out.write_text(json.dumps(dict(meta=meta, results=res), indent=1))
    print(f"done in {meta['wall_seconds']:.0f} s -> {out}")


if __name__ == "__main__":
    main()
