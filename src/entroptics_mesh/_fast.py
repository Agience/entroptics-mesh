"""Compiled (numba) drop-in kernels for the mesh steady-state receive path.

This module holds ``@njit`` ports of the hot, inherently-sequential FEC kernels whose per-step
Python loop a vectorized numpy decode cannot fold away.  Each kernel is a *bit-identical* drop-in
for the pure-Python reference in :mod:`entroptics_mesh.fec`: same trellis tables (read from ``fec``
so there is a single definition), same branch metric, same strict-greater tie rule, same ``-1e300``
floor and renormalization schedule.  ``fastmath=False`` throughout -- fastmath reorders float ops
and would break bit-identity with the oracle.

Nothing here is used unless numba imports cleanly *and* ``fec`` has verified the kernel against the
live Python reference on a fixed adversarial corpus (``fec._gate_viterbi``).  The Python path stays
the default whenever that gate does not pass, and ``ENTROPTICS_FAST=0`` forces it off entirely.

The kernels take the trellis tables as arguments rather than closing over module globals, so there
is exactly one trellis definition (``fec._build_trellis``) and the compiled code is pure.
"""
from __future__ import annotations

import numpy as np
from numba import njit


# ---------------------------------------------------------------------------
# Single-codeword Viterbi (port of fec._viterbi_decode_py)
# ---------------------------------------------------------------------------
@njit(cache=True, fastmath=False)
def _decode(a, b, p0, p1, s00, s01, s10, s11):
    """a, b: the two soft values per step (len n_steps).  Returns info bits (len n_steps-6).

    Mirrors the reference ACS exactly: per next-state j, branch metric from predecessor p0[j]
    (input-0 branch) is ``a*s00[j] + b*s01[j]`` and from p1[j] (input-1 branch) ``a*s10[j] +
    b*s11[j]``; keep the larger with a strict ``>`` tie rule; renormalize by the per-step maximum
    every step (subtract the max, a uniform offset that never changes a decision)."""
    n_steps = a.shape[0]
    ns = p0.shape[0]
    pm = np.full(ns, -1e300)
    pm[0] = 0.0
    nxt = np.empty(ns, np.float64)
    decisions = np.empty((n_steps, ns), np.uint8)
    for t in range(n_steps):
        at = a[t]
        bt = b[t]
        mx = -1e308
        for j in range(ns):
            bm0 = at * s00[j] + bt * s01[j]
            bm1 = at * s10[j] + bt * s11[j]
            m0 = pm[p0[j]] + bm0
            m1 = pm[p1[j]] + bm1
            if m1 > m0:
                decisions[t, j] = 1
                v = m1
            else:
                decisions[t, j] = 0
                v = m0
            nxt[j] = v
            if v > mx:
                mx = v
        for j in range(ns):
            pm[j] = nxt[j] - mx
    out = np.empty(n_steps, np.uint8)
    state = 0
    for t in range(n_steps - 1, -1, -1):
        out[t] = state >> 5
        if decisions[t, state]:
            state = p1[state]
        else:
            state = p0[state]
    return out[: n_steps - 6]


# ---------------------------------------------------------------------------
# Batched Viterbi (port of fec._viterbi_decode_batch_py)
# ---------------------------------------------------------------------------
@njit(cache=True, fastmath=False)
def _decode_batch(A, Bv, s0, s1):
    """A, Bv: (n_steps, H) soft a/b per step per row.  Returns (H, n_steps-6) info bits.

    Reproduces the reference batch exactly, exploiting the same trellis symmetry: for state j in
    0..31 the two predecessors are 2j (even, branch metric +bt) and 2j+1 (odd, -bt); state j+32
    takes the opposite signs.  ``bt = a*s0[j] + b*s1[j]`` with s0=_SIGN[0,0,:32], s1=_SIGN[0,1,:32].
    Path metrics are renormalized by the per-row maximum every 32 steps, as in the reference."""
    n_steps, H = A.shape
    ns = 64
    cur = np.full((H, ns), -1e300)
    for h in range(H):
        cur[h, 0] = 0.0
    new = np.empty((H, ns), np.float64)
    decisions = np.empty((n_steps, H, ns), np.uint8)
    for t in range(n_steps):
        for h in range(H):
            a = A[t, h]
            b = Bv[t, h]
            for j in range(32):
                btj = a * s0[j] + b * s1[j]
                pe = cur[h, 2 * j]
                po = cur[h, 2 * j + 1]
                t0 = pe + btj
                t1 = po - btj
                t2 = pe - btj
                t3 = po + btj
                if t1 > t0:
                    decisions[t, h, j] = 1
                    new[h, j] = t1
                else:
                    decisions[t, h, j] = 0
                    new[h, j] = t0
                if t3 > t2:
                    decisions[t, h, j + 32] = 1
                    new[h, j + 32] = t3
                else:
                    decisions[t, h, j + 32] = 0
                    new[h, j + 32] = t2
        cur, new = new, cur
        if t % 32 == 31:
            for h in range(H):
                mx = cur[h, 0]
                for j in range(1, ns):
                    if cur[h, j] > mx:
                        mx = cur[h, j]
                for j in range(ns):
                    cur[h, j] -= mx
    out = np.empty((H, n_steps), np.uint8)
    for h in range(H):
        state = 0
        for t in range(n_steps - 1, -1, -1):
            out[h, t] = state >> 5
            if decisions[t, h, state]:
                state = ((state & 31) << 1) | 1
            else:
                state = (state & 31) << 1
    return out[:, : n_steps - 6]


# ---------------------------------------------------------------------------
# Python-facing wrappers: same signature/validation as the fec reference
# ---------------------------------------------------------------------------
def viterbi_decode(soft: np.ndarray) -> np.ndarray:
    from . import fec
    llr = np.asarray(soft, dtype=np.float64).ravel()
    if llr.size % 2 or llr.size < 2 * (fec.K - 1):
        raise ValueError("soft length must be even and >= 12")
    pairs = llr.reshape(-1, 2)
    a = np.ascontiguousarray(pairs[:, 0])
    b = np.ascontiguousarray(pairs[:, 1])
    return _decode(a, b, fec._P0, fec._P1, fec._S00, fec._S01, fec._S10, fec._S11)


def viterbi_decode_batch(soft: np.ndarray) -> np.ndarray:
    from . import fec
    llr = np.asarray(soft, dtype=np.float64)
    if llr.ndim == 1:
        llr = llr[None, :]
    H, n2 = llr.shape
    if n2 % 2 or n2 < 2 * (fec.K - 1):
        raise ValueError("soft length must be even and >= 12")
    A = np.ascontiguousarray(llr[:, 0::2].T)      # (n_steps, H)
    Bv = np.ascontiguousarray(llr[:, 1::2].T)
    return _decode_batch(A, Bv, fec._SB0, fec._SB1)
