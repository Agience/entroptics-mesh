"""numba port of the mesh Viterbi kernel, bit-identical to viterbi_ref.viterbi_decode.

The sequential add-compare-select over 64 states per trellis step is the branchy part a vectorized
numpy decode cannot fold away (each step depends on the last).  numba compiles the whole per-step
loop -- branch-metric gather, ACS, renormalize -- plus the traceback to native code with no Python
per-step overhead.  Output is the same uint8 info bits, same metric and same strict-greater tie
rule, so it is a drop-in for fec.viterbi_decode.

Trellis tables come from viterbi_ref so there is one definition.  Floored at the same -1e300 and
renormalized every step exactly as the reference, so the float path matches to the bit.
"""
from __future__ import annotations
import numpy as np
from numba import njit

from viterbi_ref import _PRED, _INPUT, _SIGN, K, N_STATES

_P0 = _PRED[0].astype(np.int64)
_P1 = _PRED[1].astype(np.int64)
_S00 = np.ascontiguousarray(_SIGN[0, 0])     # branch 0 (input 0), G0 sign, per next-state
_S01 = np.ascontiguousarray(_SIGN[0, 1])     # branch 0, G1 sign
_S10 = np.ascontiguousarray(_SIGN[1, 0])     # branch 1 (input 1), G0 sign
_S11 = np.ascontiguousarray(_SIGN[1, 1])     # branch 1, G1 sign


@njit(cache=True, fastmath=False)
def _decode(a, b, p0, p1, s00, s01, s10, s11):
    """a, b: the two soft values per step (len n_steps).  Returns info bits (len n_steps-6)."""
    n_steps = a.shape[0]
    ns = N_STATES
    pm = np.full(ns, -1e300)
    pm[0] = 0.0
    nxt = np.empty(ns, np.float64)
    decisions = np.empty((n_steps, ns), np.uint8)
    for t in range(n_steps):
        at = a[t]
        bt = b[t]
        mx = -1e308
        for j in range(ns):
            # branch metric into state j = correlation of expected signs with the soft pair
            bm0 = at * s00[j] + bt * s01[j]        # from predecessor p0[j] (input 0 branch)
            bm1 = at * s10[j] + bt * s11[j]        # from predecessor p1[j] (input 1 branch)
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
    return out[: n_steps - (K - 1)]


def viterbi_decode(soft: np.ndarray) -> np.ndarray:
    llr = np.asarray(soft, dtype=np.float64).ravel()
    if llr.size % 2 or llr.size < 2 * (K - 1):
        raise ValueError("soft length must be even and >= 12")
    pairs = llr.reshape(-1, 2)
    a = np.ascontiguousarray(pairs[:, 0])
    b = np.ascontiguousarray(pairs[:, 1])
    return _decode(a, b, _P0, _P1, _S00, _S01, _S10, _S11)
