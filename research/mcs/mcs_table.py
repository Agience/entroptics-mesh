"""Adaptive MCS table for the entroptics-rf differential M-PSK link, measured
through the real codec (waveform.compose -> AWGN -> waveform.read_soft) and the
mesh FEC (entroptics_mesh.fec encode_packet / decode_packet).

Geometry: K=60 keyed modes, L=64 samples per row, seed 1234, true receive
geometry (offset 0, phase 0, carrier 0, row0 0). One packet per burst: the
burst is the keyed reference row plus T payload rows.

Packet: P payload bytes (default 500 = 4000 bits) framed by fec.encode_packet
(2-byte length + payload + CRC-32, K=7 (171,133) code, zero tail, punctured
to the rate, interleaved). The "uncoded" MCS sends the same frame (length +
payload + CRC-32) raw; a packet fails if its CRC fails.

SNR: Es/N0 per complex sample. The composed IQ is unit-RMS over the burst;
complex AWGN of variance N0 = 10**(-SNR/10) is added (N0/2 per component).

Goodput (payload bits per complex sample = b/s/Hz at 1 sample/s/Hz):

    G = (1 - PER) * 8*P / ((T + 1) * L),   T = ceil(ceil(Nc / log2 M) / K)

Nc is the coded length of the packet (packet_coded_length(P, rate), or
8*(P+6) uncoded). This counts the reference row, the 2-byte header, CRC-32,
the 6 tail bits and the partial last row as overhead. The nominal figure
r * log2(M) * K/L is printed alongside.

Run:  python research/mcs/mcs_table.py --packets 1000 --workers 8
      python research/mcs/mcs_table.py --packets 1000 --snrs 15,25 --no-unweighted
          --out research/mcs/results_15_25.json
      python research/mcs/make_report.py
Writes research/mcs/results.json (and results_15_25.json); make_report.py
prints the tables used in results.md.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
import time
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1] / "src"))

from entroptics_rf import waveform as wf  # noqa: E402
from entroptics_mesh import fec  # noqa: E402

K_MODES, L, SEED = 60, 64, 1234
MS = (2, 4, 8, 16)
RATES = ("1/2", "2/3", "3/4", "uncoded")
RATE_VALUE = {"1/2": 0.5, "2/3": 2 / 3, "3/4": 0.75, "uncoded": 1.0}
SNRS = tuple(range(0, 31, 2))


def coded_len(P: int, rate: str) -> int:
    return 8 * (P + 6) if rate == "uncoded" else fec.packet_coded_length(P, rate)


def burst_rows(P: int, rate: str, M: int) -> int:
    """Total rows in one burst, reference row included."""
    b = int(math.log2(M))
    n_sym = -(-coded_len(P, rate) // b)
    return 1 + -(-n_sym // K_MODES)


def goodput(per: float, P: int, rate: str, M: int) -> float:
    return (1 - per) * 8 * P / (burst_rows(P, rate, M) * L)


def _frame_uncoded(payload: bytes) -> np.ndarray:
    body = len(payload).to_bytes(2, "big") + payload
    body += fec.crc32(body).to_bytes(4, "big")
    return np.unpackbits(np.frombuffer(body, dtype=np.uint8))


def _check_uncoded(soft: np.ndarray, payload: bytes) -> bool:
    bits = (soft < 0).astype(np.uint8)
    body = np.packbits(bits).tobytes()
    if fec.crc32(body[:-4]) != int.from_bytes(body[-4:], "big"):
        return False
    return body[2:-4] == payload  # CRC passed; count an undetected error as a failure


def run_cell(args) -> dict:
    M, rate, snr, n_packets, P, weight, stop_after = args
    b = int(math.log2(M))
    rng = np.random.default_rng([M, int(RATE_VALUE[rate] * 1000), snr, int(weight), 7])
    n0 = 10 ** (-snr / 10)
    sent = fails = bit_err = bit_tot = 0
    undetected = 0
    t0 = time.time()
    for i in range(n_packets):
        payload = rng.integers(0, 256, P, dtype=np.uint8).tobytes()
        if rate == "uncoded":
            tx = _frame_uncoded(payload)
        else:
            tx = fec.encode_packet(payload, frame_bytes=P, rate=rate)
        iq = wf.compose(tx, K_MODES, L, SEED, M=M).astype(np.complex128)
        iq = iq + math.sqrt(n0 / 2) * (rng.standard_normal(iq.size)
                                       + 1j * rng.standard_normal(iq.size))
        d = wf.read_soft(iq, L, K_MODES, SEED, offset=0, row0=0)
        n_sym = -(-tx.size // b)
        soft = fec.soft_bits_from_differential(d.ravel()[:n_sym], M,
                                               weight_by_magnitude=weight)[: tx.size]
        bit_err += int(np.sum((soft < 0) != (tx != 0)))
        bit_tot += tx.size
        if rate == "uncoded":
            ok = _check_uncoded(soft, payload)
        else:
            out = fec.decode_packet(soft, rate=rate)
            ok = out == payload
            undetected += int(out is not None and out != payload)
        sent += 1
        fails += int(not ok)
        if stop_after and sent >= stop_after and fails == sent:
            break  # deep below the cliff: PER = 1 is established
    per = fails / sent
    return dict(M=M, rate=rate, snr=snr, weight=weight, packets=sent, fails=fails,
                per=per, ber=bit_err / bit_tot, bits=bit_tot, undetected=undetected,
                goodput=goodput(per, P, rate, M),
                nominal=RATE_VALUE[rate] * b * K_MODES / L,
                rows=burst_rows(P, rate, M), seconds=time.time() - t0)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--packets", type=int, default=400)
    ap.add_argument("--payload", type=int, default=500)
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--stop-after", type=int, default=40)
    ap.add_argument("--out", default=str(HERE / "results.json"))
    ap.add_argument("--snrs", default=None,
                    help="comma-separated SNRs (dB) instead of 0..30 step 2")
    ap.add_argument("--no-unweighted", action="store_true",
                    help="skip the weight_by_magnitude=False comparison cells")
    a = ap.parse_args()
    snrs = tuple(int(x) for x in a.snrs.split(",")) if a.snrs else SNRS

    jobs = [(M, r, s, a.packets, a.payload, True, a.stop_after)
            for M in MS for r in RATES for s in snrs]
    # the magnitude-weighting comparison: M=4, coded rates, without the weight
    if not a.no_unweighted:
        jobs += [(4, r, s, a.packets, a.payload, False, a.stop_after)
                 for r in ("1/2", "2/3", "3/4") for s in snrs]
    # the slowest cells first so the pool drains evenly
    jobs.sort(key=lambda j: (j[1] == "uncoded", -j[2]))
    t0 = time.time()
    results = []
    with ProcessPoolExecutor(max_workers=a.workers) as ex:
        for res in ex.map(run_cell, jobs):
            results.append(res)
            print(f"M={res['M']:2d} {res['rate']:>7s} w={int(res['weight'])} "
                  f"{res['snr']:2d} dB  PER={res['per']:.4f} ({res['fails']}/{res['packets']}) "
                  f"BER={res['ber']:.2e}  G={res['goodput']:.3f}  {res['seconds']:.0f}s",
                  flush=True)
    meta = dict(K=K_MODES, L=L, seed=SEED, payload_bytes=a.payload,
                packets_per_cell=a.packets, stop_after_all_fail=a.stop_after,
                snr_definition="Es/N0 per complex sample, unit-RMS burst",
                wall_seconds=time.time() - t0)
    Path(a.out).write_text(json.dumps(dict(meta=meta, results=results), indent=1))
    print(f"done in {meta['wall_seconds']:.0f} s -> {a.out}")


if __name__ == "__main__":
    main()
