"""Every mode through the mesh PHY on the emulated air: delivered goodput and per-burst CPU.

A two-node link on ``emu.air.Air`` (free space, 6 km, the receiver's noise set so the in-band SNR
-- noise over the sender's width -- is 10, 20 or 30 dB).  Each burst carries one link frame (a
fountain symbol: 35-byte header + 475-byte block, CRC, the plan's code rate) in the plan's family
and alphabet, ``phy.transmit_burst``; the receiver holds the sender's geometry (the fast path,
``phy.receive_known``, prior 2 ppm / 5e-4 of the band off) and learns family, alphabet and code
rate from the burst (``link.read_frame``).  Nothing about the mode is passed to the receiver.

Delivered goodput = frames whose symbol came back intact x frame payload bits / complex samples on
the air at the sender's rate (the burst, reference row included).  Two payload counts:
``frame`` = the 512-byte frame (symbol header + block, what ``fec.encode_packet`` protects;
comparable to the planner table, which counts a 500-byte packet) and ``block`` = the 475-byte
fountain block alone.  CPU per burst: the known-geometry read and the frame decode, process CPU on
one thread (OMP/MKL/OPENBLAS threads = 1).

``--old DIR`` runs the same link through a copy of the mesh sources from before the coherent/MCS
wiring (DQPSK, natural labels, rate 1/2 pinned, single-hypothesis decode): today's path.

    python research/mcs/phy_mcs_link.py --rates 2.4e6 --frames 8 --out research/mcs/results_phy_link_24.json
    python research/mcs/phy_mcs_link.py --rates 1.2e6 --frames 8 --out research/mcs/results_phy_link_12.json
    python research/mcs/phy_mcs_link.py --old <copy>/src --frames 8 --out research/mcs/results_phy_link_old.json
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sys
import time
from pathlib import Path

for v in ("OMP_NUM_THREADS", "MKL_NUM_THREADS", "OPENBLAS_NUM_THREADS"):
    os.environ.setdefault(v, "1")

HERE = Path(__file__).resolve().parent
ap = argparse.ArgumentParser()
ap.add_argument("--old", default=None, help="src dir of the pre-MCS mesh (today's path)")
ap.add_argument("--frames", type=int, default=8)
ap.add_argument("--snrs", default="10,20,30")
ap.add_argument("--rates", default="2.4e6,1.2e6")
ap.add_argument("--out", default=None)
A = ap.parse_args()
sys.path.insert(0, A.old or str(HERE.parents[1] / "src"))

import numpy as np  # noqa: E402

from entroptics_mesh import fec, phy  # noqa: E402
from entroptics_mesh import link  # noqa: E402
from entroptics_mesh.emu.air import Air, Radio, fspl_db  # noqa: E402
from entroptics_mesh.providers import TxPlan  # noqa: E402
from entroptics_mesh.routing import Header  # noqa: E402

RX = 2.4e6
OLD = A.old is not None


def modes():
    if OLD:
        return [("differential", 4, "1/2", None, None)]
    from entroptics_mesh import adapt
    env = set(adapt.MCS)
    return [(m.family, m.M, m.rate, m.threshold_db, m.goodput, m in env) for m in adapt.MODES]


def one(family, M, rate, srate, snr, seed):
    msg = bytes(np.random.default_rng(seed).integers(0, 256, 4 * link.BLOCK, dtype=np.uint8))
    sym = link.symbols(Header(b"A" * 8, 1, b"C" * 8, 4), msg, link.BLOCK, 1.0)[seed % 4]
    d, lead = 6000.0, 100
    noise = 20.0 - fspl_db(d, 915e6) - snr + 10 * math.log10(RX / srate)
    air = Air(rate=RX, seed=1000 + seed)
    a = air.add(Radio(position=np.array([0.0, 0, 100]), tx_power_dbm=20.0, noise_dbm=noise))
    b = air.add(Radio(position=np.array([d, 0, 100]), tx_power_dbm=20.0, noise_dbm=noise))
    if OLD:
        plan = TxPlan(rate=srate, M=4)
        iq = phy.transmit_burst(link.frame(sym), plan, 1234, sender=b"A" * 8)
    else:
        plan = TxPlan(rate=srate, M=M, family=family, code_rate=rate)
        iq = phy.transmit_burst(link.frame(sym, rate), plan, 1234, sender=b"A" * 8)
    air.send(a, lead / RX, iq, rate=None if srate == RX else srate)
    y = air.capture(b, 0.0, int(iq.size * RX / srate + 2 * lead + 200))
    known = phy.Known(64, (srate / RX) * (1 + 2e-6), 5e-4)
    t0 = time.process_time()
    rx = phy.receive_known(y, 1234, known)
    t1 = time.process_time()
    if OLD:
        got = link.unframe(rx.soft) if rx.soft.size else None
        mode = ("differential", 4, "1/2") if got is not None else None
    else:
        g = link.read_frame(rx)
        got, mode = (g if g is not None else (None, None))
    t2 = time.process_time()
    ok = got is not None and got.data == sym.data and got.header == sym.header
    return dict(ok=bool(ok), mode=mode, samples=int(iq.size), read_s=t1 - t0, decode_s=t2 - t1)


def main():
    out = []
    frame_bits = 8 * link.FRAME_BYTES
    block_bits = 8 * link.BLOCK
    for srate in [float(x) for x in A.rates.split(",")]:
        for snr in [float(x) for x in A.snrs.split(",")]:
            for m in modes():
                family, M, rate = m[:3]
                rs = [one(family, M, rate, srate, snr, k) for k in range(A.frames)]
                n_ok = sum(r["ok"] for r in rs)
                air = sum(r["samples"] for r in rs)
                row = dict(rate_hz=srate, snr=snr, family=family, M=M, code_rate=rate,
                           threshold=m[3], table_goodput=m[4], envelope=m[5] if len(m) > 5 else None,
                           frames=A.frames, delivered=n_ok,
                           goodput_frame=n_ok * frame_bits / air, goodput_block=n_ok * block_bits / air,
                           read_ms=1e3 * float(np.median([r["read_s"] for r in rs])),
                           decode_ms=1e3 * float(np.median([r["decode_s"] for r in rs])),
                           decode_ms_max=1e3 * float(np.max([r["decode_s"] for r in rs])),
                           wrong_mode=sum(r["ok"] and r["mode"] != (family, M, rate) for r in rs))
                out.append(row)
                print(f"{srate/1e6:.1f}Msps {snr:4.0f}dB {family[:4]} M={M:3d} {rate} thr={m[3]} "
                      f"{n_ok}/{A.frames} G={row['goodput_frame']:.3f} (table {m[4]}) "
                      f"read {row['read_ms']:.0f} ms decode {row['decode_ms']:.0f} ms (max {row['decode_ms_max']:.0f})",
                      flush=True)
                if A.out:
                    Path(A.out).write_text(json.dumps(dict(old=OLD, results=out), indent=1))


if __name__ == "__main__":
    main()
