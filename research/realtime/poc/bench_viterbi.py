"""Benchmark the numba Viterbi port against the frozen numpy reference: identical output, speedup.

Representative length: the mesh's 512-byte protected frame decoded at rate 1/2 (the mother code's
full length -- the most trellis steps, so the Viterbi's worst case and the one on the node fast
path).  Also a short 64-byte frame, to show the fixed/per-step split.

Correctness is exact: the port must return the same uint8 info bits as the reference on every
input, including adversarial all-zero (erasure) and near-tie soft values.
"""
from __future__ import annotations
import sys, os, time
import numpy as np

sys.path.insert(0, os.path.dirname(__file__))
import viterbi_ref as ref
import viterbi_numba as nb


def n_steps_for(frame_bytes, header=2, crc=4, tail=6):
    return 8 * (header + frame_bytes + crc) + tail


def rand_soft(n_steps, rng, scale=4.0, erasures=0.0):
    """Soft LLRs for a 2*n_steps mother codeword: a noisy BPSK-like metric with some erasures."""
    bits = rng.integers(0, 2, 2 * n_steps)
    s = (1.0 - 2.0 * bits) * scale + rng.standard_normal(2 * n_steps)
    if erasures:
        s[rng.random(s.size) < erasures] = 0.0
    return s


def check_identical(trials=200):
    rng = np.random.default_rng(0)
    worst = 0
    for i in range(trials):
        fb = int(rng.choice([32, 64, 128, 475, 512]))
        n = n_steps_for(fb)
        s = rand_soft(n, rng, scale=rng.uniform(0.3, 6.0), erasures=rng.uniform(0.0, 0.4))
        r = ref.viterbi_decode(s)
        g = nb.viterbi_decode(s)
        if not np.array_equal(r, g):
            diff = int(np.sum(r != g))
            worst = max(worst, diff)
            print(f"  MISMATCH trial {i} fb={fb}: {diff}/{r.size} bits differ")
    print(f"identical on {trials} trials: {'YES' if worst == 0 else 'NO'}")
    return worst == 0


def bench(frame_bytes, reps=200):
    n = n_steps_for(frame_bytes)
    rng = np.random.default_rng(1)
    soft = rand_soft(n, rng)
    # warm the JIT (compiles, not timed)
    nb.viterbi_decode(soft)
    assert np.array_equal(ref.viterbi_decode(soft), nb.viterbi_decode(soft))

    t = time.perf_counter()
    for _ in range(reps):
        ref.viterbi_decode(soft)
    t_ref = (time.perf_counter() - t) / reps * 1e3

    t = time.perf_counter()
    for _ in range(reps):
        nb.viterbi_decode(soft)
    t_nb = (time.perf_counter() - t) / reps * 1e3

    print(f"  frame {frame_bytes:4d} B  ({n:5d} trellis steps):  "
          f"ref {t_ref:7.2f} ms   numba {t_nb:6.2f} ms   speedup {t_ref / t_nb:5.1f}x")
    return t_ref, t_nb


if __name__ == "__main__":
    print("=== Viterbi PoC: numba vs frozen numpy reference ===")
    ok = check_identical()
    print()
    for fb in (64, 475, 512):
        bench(fb)
    print("\n(numba first-call JIT warm is one-time per process; steady-state times above)")
    sys.exit(0 if ok else 1)
