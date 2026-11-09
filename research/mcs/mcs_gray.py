"""Coded 8/16-PSK with Gray labels done in the FEC layer (fec.gray_to_phy before compose, soft bits
read with gray=True).  Same cells and goodput formula as mcs_table.py, so results compare directly."""
import json, math, sys, time
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path
import numpy as np
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE)); sys.path.insert(0, str(HERE.parents[1] / "src"))
import mcs_table as T
from entroptics_mesh import fec
from entroptics_rf import waveform as wf


def run_cell(args):
    M, rate, snr, n_packets, P = args
    b = int(math.log2(M))
    rng = np.random.default_rng([M, int(T.RATE_VALUE[rate] * 1000), snr, 11])
    n0 = 10 ** (-snr / 10)
    sent = fails = undetected = 0
    for i in range(n_packets):
        payload = rng.integers(0, 256, P, dtype=np.uint8).tobytes()
        tx = fec.encode_packet(payload, frame_bytes=P, rate=rate).astype(np.int64)
        pad = (-tx.size) % b
        txp = np.concatenate([tx, np.zeros(pad, np.int64)])
        iq = wf.compose(fec.gray_to_phy(txp, M), T.K_MODES, T.L, T.SEED, M=M).astype(np.complex128)
        iq = iq + math.sqrt(n0 / 2) * (rng.standard_normal(iq.size) + 1j * rng.standard_normal(iq.size))
        d = wf.read_soft(iq, T.L, T.K_MODES, T.SEED, offset=0, row0=0)
        soft = fec.soft_bits_from_differential(d.ravel()[: txp.size // b], M, weight_by_magnitude=True,
                                               gray=True)[: tx.size]
        out = fec.decode_packet(soft, rate=rate)
        undetected += int(out is not None and out != payload)
        sent += 1; fails += int(out != payload)
        if sent >= 40 and fails == sent:
            break
    per = fails / sent
    return dict(M=M, rate=rate, snr=snr, packets=sent, fails=fails, per=per, undetected=undetected,
                goodput=T.goodput(per, P, rate, M))


if __name__ == "__main__":
    jobs = [(8, r, s, 400, 500) for r in ("1/2", "2/3", "3/4") for s in range(8, 23, 2)]
    jobs += [(16, r, s, 400, 500) for r in ("1/2", "2/3", "3/4") for s in range(12, 29, 2)]
    t0 = time.time(); res = []
    with ProcessPoolExecutor(max_workers=int(sys.argv[1]) if len(sys.argv) > 1 else 8) as ex:
        for r in ex.map(run_cell, jobs):
            res.append(r)
            print(f"M={r['M']:2d} {r['rate']} {r['snr']:2d} dB PER={r['per']:.4f} ({r['fails']}/{r['packets']}) G={r['goodput']:.3f} undetected={r['undetected']}", flush=True)
    (HERE / "results_gray.json").write_text(json.dumps(dict(wall=time.time() - t0, results=res), indent=1))
    print(f"done {time.time()-t0:.0f}s")
