"""Frozen copy of the mesh's Viterbi kernel (entroptics_mesh/fec.py, 2026-10-17) as the PoC oracle.

Exactly the reference numpy decode -- K=7, rate-1/2 (171,133), zero-tail, soft-input, same metric
and tie rule -- copied here so the benchmark can compare a port against it without importing the
live module (which the PoC must not edit).  If fec.py changes, re-copy.
"""
from __future__ import annotations
import numpy as np

K = 7
N_STATES = 1 << (K - 1)
G0 = 0o171
G1 = 0o133


def _parity(x: int) -> int:
    return bin(x).count("1") & 1


def _build_trellis():
    ns = np.arange(N_STATES)
    pred = np.stack([((ns & 31) << 1) | 0, ((ns & 31) << 1) | 1])
    inp = ns >> 5
    sgn = np.zeros((2, 2, N_STATES))
    for x in range(2):
        for s_next in range(N_STATES):
            reg = (int(inp[s_next]) << 6) | int(pred[x, s_next])
            sgn[x, 0, s_next] = 1 - 2 * _parity(reg & G0)
            sgn[x, 1, s_next] = 1 - 2 * _parity(reg & G1)
    return pred, inp, sgn


_PRED, _INPUT, _SIGN = _build_trellis()


def viterbi_decode(soft: np.ndarray) -> np.ndarray:
    """Reference ML decode of a zero-tail (171,133) codeword (see fec.viterbi_decode)."""
    llr = np.asarray(soft, dtype=np.float64).ravel()
    if llr.size % 2 or llr.size < 2 * (K - 1):
        raise ValueError("soft length must be even and >= 12")
    n_steps = llr.size // 2
    pairs = llr.reshape(n_steps, 2)
    bm = (pairs[:, None, 0, None] * _SIGN[None, :, 0, :]
          + pairs[:, None, 1, None] * _SIGN[None, :, 1, :])
    p0, p1 = _PRED[0], _PRED[1]
    pm = np.full(N_STATES, -1e300)
    pm[0] = 0.0
    decisions = np.empty((n_steps, N_STATES), dtype=bool)
    for t in range(n_steps):
        m0 = pm[p0] + bm[t, 0]
        m1 = pm[p1] + bm[t, 1]
        d = m1 > m0
        decisions[t] = d
        pm = np.where(d, m1, m0)
        pm -= pm.max()
    out = np.empty(n_steps, dtype=np.uint8)
    state = 0
    pred0 = _PRED[0].tolist()
    pred1 = _PRED[1].tolist()
    for t in range(n_steps - 1, -1, -1):
        out[t] = state >> 5
        state = pred1[state] if decisions[t, state] else pred0[state]
    return out[: n_steps - (K - 1)]
