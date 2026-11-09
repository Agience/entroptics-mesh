"""Forward error correction for the one-way mesh.

The mesh is one-way: receivers never acknowledge, never ask for a resend and
never handshake, and a listen-only receiver must be able to decode
everything. Reliability therefore comes from two layers, both in this module
and both pure numpy / standard library:

* Within a packet: payload + CRC-32, a rate-1/2 constraint-length-7
  convolutional code (generators 171, 133 octal) with zero-tail termination,
  decoded by a soft-input Viterbi decoder, and a block interleaver so that a
  burst of bad bits on the air is spread thinly across the trellis.
  ``encode_packet`` / ``decode_packet`` wrap the whole chain; a packet that
  does not pass its CRC after decoding is dropped (``None``).

* Across packets: an LT fountain code. A message is cut into ``k`` blocks
  and the sender emits an unbounded stream of encoded symbols, each the XOR
  of a pseudo-random subset of blocks. The subset is a pure function of
  ``(seed, symbol_id)``, so the receiver needs only the id carried in the
  packet header. Any sufficiently large set of received symbols, in any
  order, reconstructs the message; lost packets cost nothing but time.

Soft-value sign convention used throughout: a soft bit (log-likelihood-ratio
like value) is positive when bit 0 is more likely and negative when bit 1 is
more likely; its magnitude is the confidence and 0 means "no information"
(an erasure). Hard bits ``b`` map to soft values ``1 - 2*b``.

Bit order: bytes are expanded to bits most-significant bit first
(``np.unpackbits`` order) everywhere in this module.
"""

from __future__ import annotations

import math
import os
import zlib
from typing import Iterator

import numpy as np

__all__ = [
    "crc32",
    "conv_encode",
    "viterbi_decode",
    "viterbi_decode_hard",
    "viterbi_decode_batch",
    "PUNCTURE_PATTERNS",
    "punctured_length",
    "puncture",
    "depuncture",
    "hard_to_soft",
    "soft_bits_from_differential",
    "soft_bits_from_qam",
    "qam_noise_var",
    "block_interleave",
    "block_deinterleave",
    "interleaver_permutation",
    "interleave",
    "deinterleave",
    "PACKET_INTERLEAVE_COLS",
    "packet_coded_length",
    "encode_packet",
    "decode_packet",
    "decode_packets",
    "robust_soliton",
    "lt_neighbours",
    "fountain_encode",
    "FountainDecoder",
]


# ---------------------------------------------------------------------------
# CRC-32
# ---------------------------------------------------------------------------

def _make_crc_table() -> np.ndarray:
    poly = 0xEDB88320  # IEEE 802.3 polynomial, bit-reflected
    table = np.zeros(256, dtype=np.uint32)
    for i in range(256):
        c = i
        for _ in range(8):
            c = (c >> 1) ^ poly if (c & 1) else (c >> 1)
        table[i] = c
    return table


_CRC_TABLE = [int(v) for v in _make_crc_table()]


def _crc32_py(data: bytes, value: int = 0) -> int:
    """Pure-Python reference CRC-32 (the oracle for :func:`crc32`).

    Kept so the module has a numpy/stdlib-table implementation independent of zlib; the fast path
    (:func:`crc32`) must return exactly this for every input.
    """
    crc = (value ^ 0xFFFFFFFF) & 0xFFFFFFFF
    table = _CRC_TABLE
    for byte in bytes(data):
        crc = (crc >> 8) ^ table[(crc ^ byte) & 0xFF]
    return crc ^ 0xFFFFFFFF


def crc32(data: bytes, value: int = 0) -> int:
    """IEEE CRC-32 (reflected, init and final XOR 0xFFFFFFFF).

    Delegates to the C :func:`zlib.crc32`, which is bit-identical to the pure-Python table loop
    (:func:`_crc32_py`) for every input -- including the ``value`` continuation form -- and far
    faster.  ``value`` is a previous CRC to continue from.
    """
    return zlib.crc32(bytes(data), value & 0xFFFFFFFF)


# ---------------------------------------------------------------------------
# Convolutional code, K = 7, rate 1/2, generators 171 / 133 octal
# ---------------------------------------------------------------------------

K = 7                    # constraint length
N_STATES = 1 << (K - 1)  # 64
G0 = 0o171               # first output bit of each pair
G1 = 0o133               # second output bit of each pair


def _parity(x: int) -> int:
    return bin(x).count("1") & 1


def _build_trellis():
    """Predecessor tables for the Viterbi add-compare-select.

    The 7-bit shift register at step t is ``reg = (u[t] << 6) | state`` where
    ``state`` holds the six previous inputs, most recent in bit 5. Bit 6 of a
    generator taps the current input. The next state is ``reg >> 1``, so a
    next state ``ns`` has input bit ``ns >> 5`` and two predecessors
    ``((ns & 31) << 1) | x`` for x in {0, 1}.
    """
    ns = np.arange(N_STATES)
    pred = np.stack([((ns & 31) << 1) | 0, ((ns & 31) << 1) | 1])  # (2, 64)
    inp = ns >> 5
    # sign (+1 for expected bit 0, -1 for expected bit 1) of each output
    sgn = np.zeros((2, 2, N_STATES))  # [branch x, output j, ns]
    for x in range(2):
        for s_next in range(N_STATES):
            reg = (int(inp[s_next]) << 6) | int(pred[x, s_next])
            sgn[x, 0, s_next] = 1 - 2 * _parity(reg & G0)
            sgn[x, 1, s_next] = 1 - 2 * _parity(reg & G1)
    return pred, inp, sgn


_PRED, _INPUT, _SIGN = _build_trellis()

# Contiguous trellis tables for the compiled kernels (entroptics_mesh._fast); one definition here.
_P0 = _PRED[0].astype(np.int64)
_P1 = _PRED[1].astype(np.int64)
_S00 = np.ascontiguousarray(_SIGN[0, 0])     # input-0 branch, G0 sign, per next-state
_S01 = np.ascontiguousarray(_SIGN[0, 1])     # input-0 branch, G1 sign
_S10 = np.ascontiguousarray(_SIGN[1, 0])     # input-1 branch, G0 sign
_S11 = np.ascontiguousarray(_SIGN[1, 1])     # input-1 branch, G1 sign
_SB0 = np.ascontiguousarray(_SIGN[0, 0, :32])  # batch even-predecessor G0 sign (states 0..31)
_SB1 = np.ascontiguousarray(_SIGN[0, 1, :32])  # batch even-predecessor G1 sign


def conv_encode(bits: np.ndarray) -> np.ndarray:
    """Encode bits with the K=7 rate-1/2 (171, 133) code, zero-tail terminated.

    Six zero bits are appended so the encoder ends in state 0; the output has
    ``2 * (len(bits) + 6)`` bits, ordered (G0 output, G1 output) per input
    bit. Returns uint8 0/1.
    """
    u = np.asarray(bits, dtype=np.uint8).ravel() & 1
    n = u.size + (K - 1)
    # padded[t + 6 - j] = u[t - j]  (zeros before the start and as the tail)
    padded = np.zeros(n + (K - 1), dtype=np.uint8)
    padded[K - 1:K - 1 + u.size] = u
    out = np.zeros((n, 2), dtype=np.uint8)
    for col, g in enumerate((G0, G1)):
        acc = np.zeros(n, dtype=np.uint8)
        for j in range(K):          # tap j multiplies u[t - j]
            if (g >> (K - 1 - j)) & 1:
                acc ^= padded[K - 1 - j:K - 1 - j + n]
        out[:, col] = acc
    return out.ravel()


def hard_to_soft(bits: np.ndarray) -> np.ndarray:
    """Map hard bits 0/1 to soft values +1/-1 (positive = bit 0)."""
    return 1.0 - 2.0 * (np.asarray(bits, dtype=np.float64) != 0)


def _viterbi_decode_py(soft: np.ndarray) -> np.ndarray:
    """Maximum-likelihood decode of a zero-tail (171, 133) codeword.

    ``soft`` holds one value per coded bit in transmission order (after
    deinterleaving), positive = bit 0 more likely, magnitude = confidence,
    0 = erased. Its length must be even and at least 2*6. The branch metric
    is the correlation ``sum(sign_expected * soft)``, which is the
    maximum-likelihood metric when ``soft`` is proportional to the LLR and is
    the Hamming metric when ``soft`` is +/-1. The decoder starts and ends in
    state 0 and returns the ``len(soft)//2 - 6`` information bits (uint8).

    The add-compare-select runs over all 64 states at once with numpy; only
    the per-step loop and the traceback are Python.
    """
    llr = np.asarray(soft, dtype=np.float64).ravel()
    if llr.size % 2 or llr.size < 2 * (K - 1):
        raise ValueError("soft length must be even and >= 12")
    n_steps = llr.size // 2
    pairs = llr.reshape(n_steps, 2)
    # branch metrics for all steps at once: (n_steps, 2 branches, 64)
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
    # traceback from state 0 (zero-tail)
    out = np.empty(n_steps, dtype=np.uint8)
    state = 0
    dec_rows = decisions
    pred0 = _PRED[0].tolist()
    pred1 = _PRED[1].tolist()
    for t in range(n_steps - 1, -1, -1):
        out[t] = state >> 5
        state = pred1[state] if dec_rows[t, state] else pred0[state]
    return out[: n_steps - (K - 1)]


def _viterbi_decode_batch_py(soft: np.ndarray) -> np.ndarray:
    """:func:`viterbi_decode` over a batch of codewords of one length: ``(H, 2n)`` -> ``(H, n-6)``.

    Each row is decoded as :func:`viterbi_decode` decodes it (same metric, same tie rule), but the
    add-compare-select of all H rows runs in one numpy pass per trellis step, so the per-step
    interpreter cost -- nearly all of a single decode's time -- is paid once for the batch.  That is
    what makes trying several hypotheses about one burst affordable: every code rate of a fixed
    frame depunctures to the same mother-code length.

    Both generators tap the newest and the oldest register bit, so the two branches into a state
    carry opposite metrics, and so do states that differ only in the newest input: one step's 64x2
    branch metrics are +-32 values, gathered once for every step before the loop.  The loop is then
    eight in-place ufuncs per step; path metrics are renormalized every 32 steps."""
    llr = np.asarray(soft, dtype=np.float64)
    if llr.ndim == 1:
        llr = llr[None, :]
    H, n2 = llr.shape
    if n2 % 2 or n2 < 2 * (K - 1):
        raise ValueError("soft length must be even and >= 12")
    n_steps = n2 // 2
    a, b = llr[:, 0::2].T, llr[:, 1::2].T                      # (n, H)
    s0, s1 = _SIGN[0, 0, :32], _SIGN[0, 1, :32]                # branch x=0 into states 0..31
    B = a[:, :, None] * s0[None, None, :] + b[:, :, None] * s1[None, None, :]   # (n, H, 32)
    pm = np.full((H, N_STATES), -1e300)
    pm[:, 0] = 0.0
    nx = np.empty_like(pm)
    t0, t1, t2, t3 = (np.empty((H, 32)) for _ in range(4))
    decisions = np.empty((n_steps, H, N_STATES), dtype=bool)
    for t in range(n_steps):
        bt = B[t]
        pe, po = pm[:, 0::2], pm[:, 1::2]                     # predecessors 2j and 2j+1 of state j
        np.add(pe, bt, out=t0)                                # into j (input 0), from 2j
        np.subtract(po, bt, out=t1)                           # into j, from 2j+1
        np.subtract(pe, bt, out=t2)                           # into j+32 (input 1), from 2j
        np.add(po, bt, out=t3)                                # into j+32, from 2j+1
        np.greater(t1, t0, out=decisions[t, :, :32])
        np.greater(t3, t2, out=decisions[t, :, 32:])
        np.maximum(t0, t1, out=nx[:, :32])
        np.maximum(t2, t3, out=nx[:, 32:])
        pm, nx = nx, pm
        if t % 32 == 31:
            pm -= pm.max(axis=1, keepdims=True)
    out = np.empty((H, n_steps), dtype=np.uint8)
    state = np.zeros(H, dtype=np.int64)
    rows = np.arange(H)
    for t in range(n_steps - 1, -1, -1):
        out[:, t] = state >> 5
        state = ((state & 31) << 1) | decisions[t, rows, state]
    return out[:, : n_steps - (K - 1)]


# The symmetries viterbi_decode_batch relies on, checked on the trellis itself.
assert np.array_equal(_SIGN[1], -_SIGN[0]) and np.array_equal(_SIGN[:, :, 32:], -_SIGN[:, :, :32])
assert np.array_equal(_PRED[0], ((np.arange(N_STATES) & 31) << 1)) and np.array_equal(_PRED[1], _PRED[0] | 1)


# ---------------------------------------------------------------------------
# Compiled fast path (numba), oracle-gated against the Python reference above
# ---------------------------------------------------------------------------
# The sequential 64-state add-compare-select is the branchy part a vectorized numpy decode cannot
# fold away (step t depends on t-1).  entroptics_mesh._fast compiles it with numba; this is a
# drop-in only when numba imports AND the compiled kernels reproduce the Python reference bit-for-
# bit on a fixed adversarial corpus (``_gate_viterbi``).  ENTROPTICS_FAST=0 forces the Python path.
_fast_viterbi_ok: bool | None = None       # None = not yet gated; cached after the first check


def _fast_disabled() -> bool:
    return os.environ.get("ENTROPTICS_FAST", "").strip().lower() in ("0", "off", "false", "no")


def _gate_viterbi(_fast) -> bool:
    """True iff the compiled single- and batch-Viterbi reproduce the Python reference exactly on a
    fixed adversarial corpus (random scales, erasures, all five frame sizes, incl. near ties)."""
    rng = np.random.default_rng(0xC0DE)
    for _ in range(40):
        fb = int(rng.choice([4, 8, 32, 64, 128]))
        n_steps = 8 * (_HEADER_BYTES + fb + _CRC_BYTES) + (K - 1)
        bits = rng.integers(0, 2, 2 * n_steps)
        s = (1.0 - 2.0 * bits) * rng.uniform(0.3, 6.0) + rng.standard_normal(2 * n_steps)
        if rng.random() < 0.5:
            s[rng.random(s.size) < rng.uniform(0.0, 0.4)] = 0.0
        if not np.array_equal(_viterbi_decode_py(s), _fast.viterbi_decode(s)):
            return False
    for H in (1, 2, 5):
        n_steps = 8 * (_HEADER_BYTES + 32 + _CRC_BYTES) + (K - 1)
        rows = []
        for _ in range(H):
            bits = rng.integers(0, 2, 2 * n_steps)
            rows.append((1.0 - 2.0 * bits) * rng.uniform(0.3, 6.0) + rng.standard_normal(2 * n_steps))
        M = np.stack(rows)
        if not np.array_equal(_viterbi_decode_batch_py(M), _fast.viterbi_decode_batch(M)):
            return False
    return True


def _use_fast_viterbi() -> bool:
    """Whether the compiled Viterbi may be used now: not disabled, numba present, gate passed."""
    global _fast_viterbi_ok
    if _fast_disabled():
        return False
    if _fast_viterbi_ok is None:
        try:
            from . import _fast
            _fast_viterbi_ok = bool(_gate_viterbi(_fast))
        except Exception:
            _fast_viterbi_ok = False
    return _fast_viterbi_ok


def viterbi_decode(soft: np.ndarray) -> np.ndarray:
    """Maximum-likelihood decode of a zero-tail (171, 133) codeword (see :func:`_viterbi_decode_py`).

    Uses the compiled kernel (:mod:`entroptics_mesh._fast`) when it is available and has passed the
    bit-identity gate, otherwise the pure-Python reference; both return identical ``uint8`` bits.
    """
    if _use_fast_viterbi():
        from . import _fast
        return _fast.viterbi_decode(soft)
    return _viterbi_decode_py(soft)


def viterbi_decode_batch(soft: np.ndarray) -> np.ndarray:
    """:func:`viterbi_decode` over a batch of one length (see :func:`_viterbi_decode_batch_py`).

    Compiled kernel when gated-available, else the pure-Python reference; output is identical.
    """
    if _use_fast_viterbi():
        from . import _fast
        return _fast.viterbi_decode_batch(soft)
    return _viterbi_decode_batch_py(soft)


def viterbi_decode_hard(bits: np.ndarray) -> np.ndarray:
    """Viterbi decode hard 0/1 coded bits (Hamming-distance decoding)."""
    return viterbi_decode(hard_to_soft(bits))


# ---------------------------------------------------------------------------
# Puncturing: rates 2/3 and 3/4 from the rate-1/2 mother code
# ---------------------------------------------------------------------------

#: Puncturing patterns, the IEEE 802.11a/g/n/ac ones for the (171, 133) code.
#: Each entry is ``(G0 row, G1 row)`` over a period of P input bits; a 1 keeps
#: that coded bit, a 0 deletes it. In the mother-code output order
#: (A1 B1 A2 B2 ...; A = G0 output, B = G1 output) that is:
#:
#: * 1/2: keep everything (P = 1).
#: * 2/3: G0 [1, 1], G1 [1, 0] -> sends A1 B1 A2, deletes B2 (3 of 4 bits).
#: * 3/4: G0 [1, 1, 0], G1 [1, 0, 1] -> sends A1 B1 A2 B3, deletes B2 A3
#:   (4 of 6 bits).
#:
#: The pattern restarts at the first information bit and runs through the six
#: tail steps, so a codeword of ``n`` trellis steps keeps
#: ``(n // P) * kept_per_period + kept(first n % P steps)`` bits.
PUNCTURE_PATTERNS: dict[str, tuple[tuple[int, ...], tuple[int, ...]]] = {
    "1/2": ((1,), (1,)),
    "2/3": ((1, 1), (1, 0)),
    "3/4": ((1, 1, 0), (1, 0, 1)),
}

_RATE_VALUES = {"1/2": 1 / 2, "2/3": 2 / 3, "3/4": 3 / 4}


def _rate_key(rate) -> str:
    """Normalize a rate given as "2/3", Fraction(2, 3), 2/3 or (2, 3)."""
    if isinstance(rate, str):
        key = rate.replace(" ", "")
        if key in PUNCTURE_PATTERNS:
            return key
    elif isinstance(rate, tuple) and len(rate) == 2:
        return _rate_key(f"{int(rate[0])}/{int(rate[1])}")
    else:
        try:
            r = float(rate)
        except (TypeError, ValueError):
            r = None
        if r is not None:
            for key, v in _RATE_VALUES.items():
                if abs(r - v) < 1e-9:
                    return key
    raise ValueError(f"unsupported code rate {rate!r}; use one of {list(PUNCTURE_PATTERNS)}")


def _puncture_mask(rate) -> np.ndarray:
    """Keep-mask over one period in mother-code order (A1 B1 A2 B2 ...)."""
    g0, g1 = PUNCTURE_PATTERNS[_rate_key(rate)]
    return np.stack([np.asarray(g0), np.asarray(g1)], axis=1).ravel().astype(bool)


def _mask_for_steps(rate, n_steps: int) -> np.ndarray:
    period = _puncture_mask(rate)
    p = period.size // 2
    reps = -(-n_steps // p)
    return np.tile(period, reps)[: 2 * n_steps]


def punctured_length(n_steps: int, rate="1/2") -> int:
    """Coded bits kept from a mother codeword of ``n_steps`` trellis steps."""
    period = _puncture_mask(rate)
    p = period.size // 2
    q, r = divmod(int(n_steps), p)
    return int(q * period.sum() + period[: 2 * r].sum())


def puncture(coded: np.ndarray, rate="1/2") -> np.ndarray:
    """Delete coded bits per ``PUNCTURE_PATTERNS[rate]``.

    ``coded`` is a rate-1/2 mother codeword (even length, A/B interleaved as
    ``conv_encode`` returns it). Works on hard or soft values.
    """
    c = np.asarray(coded).ravel()
    if c.size % 2:
        raise ValueError("mother codeword length must be even")
    return c[_mask_for_steps(rate, c.size // 2)]


def depuncture(soft: np.ndarray, rate="1/2", n_steps: int | None = None) -> np.ndarray:
    """Re-insert the deleted positions as zeros (erasures) before Viterbi.

    ``n_steps`` is the mother-code trellis length; if omitted it is inferred
    from ``len(soft)`` (each trellis step keeps at least one bit under every
    supported pattern, so the inference is unique). Returns float64 of
    length ``2 * n_steps``.
    """
    s = np.asarray(soft, dtype=np.float64).ravel()
    if n_steps is None:
        n_steps = _steps_for_length(s.size, rate)
    mask = _mask_for_steps(rate, n_steps)
    if int(mask.sum()) != s.size:
        raise ValueError(f"{s.size} soft values do not match {n_steps} steps at rate {rate}")
    out = np.zeros(2 * n_steps, dtype=np.float64)
    out[mask] = s
    return out


def _steps_for_length(n: int, rate) -> int:
    """Inverse of ``punctured_length``; ValueError if no step count fits."""
    period = _puncture_mask(rate)
    p, kept = period.size // 2, int(period.sum())
    q = n // kept
    for steps in range(max(0, (q - 1) * p), (q + 1) * p + 1):
        if punctured_length(steps, rate) == n:
            return steps
    raise ValueError(f"{n} is not a valid punctured length at rate {rate}")


# ---------------------------------------------------------------------------
# PHY soft values -> soft bits
# ---------------------------------------------------------------------------

def _wrap(a: np.ndarray) -> np.ndarray:
    return (a + np.pi) % (2 * np.pi) - np.pi


def soft_bits_from_differential(d: np.ndarray, M: int = 4,
                                weight_by_magnitude: bool = False, gray: bool = False) -> np.ndarray:
    """Per-bit soft values from the PHY's complex differential values.

    The PHY decides a symbol as ``index = round(angle(d) / (2*pi/M)) mod M``
    with natural binary labels, bits most-significant first. For each bit
    position this function returns the signed angular distance (radians)
    from ``angle(d)`` to the nearest decision boundary for that bit:
    positive when the angle lies in the region where the bit is 0, negative
    where it is 1. No noise model is assumed; the value is purely geometric.

    Decision boundaries sit midway between adjacent constellation points,
    at ``(m + 1/2) * 2*pi/M``; a boundary counts for a bit only if the bit
    differs between the two points it separates. For M=4 (points at 0,
    pi/2, pi, -pi/2 labelled 00, 01, 10, 11):

    * MSB: 0 on points 0 and pi/2, boundaries at -pi/4 and 3pi/4, so the
      value is ``pi/2 - |wrap(theta - pi/4)|`` in [-pi/2, pi/2].
    * LSB: 0 on points 0 and pi, boundaries at +/-pi/4 and +/-3pi/4, so the
      value is ``(pi/2 - |wrap(2*theta)|) / 2`` in [-pi/4, pi/4].

    The LSB is less reliable than the MSB under natural binary labelling,
    and its smaller range says so. Signs agree with the PHY's hard decision.

    ``gray=True`` reads the bits under Gray labels -- the information bits of a symbol whose PHY
    index is n are ``gray(n) = n ^ (n >> 1)`` -- for bits that were mapped with
    :func:`gray_to_phy` before transmission.  Neighbouring points then differ in one bit, so a
    near miss costs one bit, not up to log2(M).

    If ``weight_by_magnitude`` is True each symbol's values are multiplied by
    ``|d|`` normalized by the median ``|d|``, so faded symbols count less.
    The default is the pure angle metric.

    ``d`` may have any shape; symbols are taken in C order (for the PHY's
    (rows-1, K) array: row by row, mode by mode). Returns a float array of
    length ``d.size * log2(M)``.
    """
    bps = int(round(math.log2(M)))
    if 1 << bps != M:
        raise ValueError("M must be a power of two")
    z = np.asarray(d).ravel()
    theta = np.angle(z)
    step = 2 * np.pi / M
    idx = np.mod(np.round(theta / step), M).astype(np.int64)
    out = np.empty((z.size, bps), dtype=np.float64)
    m = np.arange(M)
    labels = (m ^ (m >> 1)) if gray else m
    for b in range(bps):
        shift = bps - 1 - b
        lab = (labels >> shift) & 1
        edges = [(mm + 0.5) * step for mm in range(M) if lab[mm] != lab[(mm + 1) % M]]
        edges = np.asarray(edges)
        dist = np.min(np.abs(_wrap(theta[:, None] - edges[None, :])), axis=1)
        bit = (labels[idx] >> shift) & 1
        out[:, b] = np.where(bit == 0, dist, -dist)
    if weight_by_magnitude:
        mag = np.abs(z)
        med = np.median(mag) if mag.size else 1.0
        out *= (mag / (med if med > 0 else 1.0))[:, None]
    return out.ravel()


def gray_to_phy(bits, M: int) -> np.ndarray:
    """Map information bits, log2(M) at a time, to the bits the PHY should send so that the symbol
    it puts on the air has Gray label equal to the information: the PHY sends natural binary, so
    each group v is replaced by inverse-Gray(v).  The wire format is untouched; only which index
    carries which information changes.  Inverse of reading with ``soft_bits_from_differential(...,
    gray=True)`` (or :func:`phy_to_gray` on hard bits)."""
    bps = int(round(math.log2(M)))
    b = np.asarray(bits, dtype=np.int64).reshape(-1, bps)
    w = 1 << np.arange(bps - 1, -1, -1)
    v = b @ w
    n = v.copy()
    shift = v >> 1
    while np.any(shift):
        n ^= shift
        shift >>= 1
    return ((n[:, None] >> np.arange(bps - 1, -1, -1)) & 1).reshape(-1)


def phy_to_gray(bits, M: int) -> np.ndarray:
    """Hard-bit inverse of :func:`gray_to_phy`: PHY index bits -> Gray-label information bits."""
    bps = int(round(math.log2(M)))
    b = np.asarray(bits, dtype=np.int64).reshape(-1, bps)
    n = b @ (1 << np.arange(bps - 1, -1, -1))
    g = n ^ (n >> 1)
    return ((g[:, None] >> np.arange(bps - 1, -1, -1)) & 1).reshape(-1)


# ---------------------------------------------------------------------------
# Coherent square QAM -> soft bits
# ---------------------------------------------------------------------------

def _qam_axis(M: int):
    """Per-axis levels and Gray labels of square M-QAM at unit average power, the layout
    ``entroptics_rf.waveform.compose_qam`` writes: the symbol index is ``(gray(i) << h) | gray(q)``
    with ``h = log2(sqrt M)``, i/q the level numbers on the in-phase/quadrature axes, levels
    ``(2i - (s - 1)) / norm``.  So the first h bits of a symbol (MSB first) are the in-phase axis's
    Gray label and the last h the quadrature's, and each axis is read on its own."""
    M = int(M)
    s = int(round(math.sqrt(M)))
    h = int(round(math.log2(s))) if s > 1 else 0
    if s * s != M or (1 << h) != s or h < 1:
        raise ValueError(f"M must be a square power of two >= 4 (4, 16, 64, 256, 1024); got {M}")
    i = np.arange(s)
    norm = math.sqrt(2.0 * (s * s - 1) / 3.0)          # RMS of the unnormalized lattice
    levels = (2.0 * i - (s - 1)) / norm
    labels = i ^ (i >> 1)
    return levels, labels, h


def _qam_nearest(y, M: int) -> np.ndarray:
    """Nearest square M-QAM point to each value, one axis at a time."""
    levels, _, _ = _qam_axis(M)
    step, lo, s = levels[1] - levels[0], levels[0], levels.size
    pick = lambda x: levels[np.clip(np.rint((x - lo) / step), 0, s - 1).astype(np.int64)]
    z = np.asarray(y)
    return pick(np.real(z)) + 1j * pick(np.imag(z))


def qam_noise_var(y, M: int, axis=None, K: int | None = None):
    """Complex noise variance ``E|n|^2`` measured off the symbols themselves: the mean squared
    distance from each value to its nearest M-QAM point (decision residuals).  Nothing is assumed
    or shared -- the capture is the only input.

    ``axis`` is passed to the mean, so ``axis=0`` on a ``(rows, K)`` array measures each mode on
    its own.  ``K`` does the same for a flat C-order run of symbols (symbol i on mode i % K, the
    order ``compose_qam`` writes, a short last row included) and returns ``(K,)``.  Per mode is the
    read that matters for a burst: every mode's gain comes from one noisy reference symbol, so the
    modes' noise differs.  Weighting each mode by its own N0 lowered the measured 1%-PER thresholds
    by 1.0-2.3 dB at 4-QAM and 0.2-0.8 dB at 16-QAM against one global N0, and moved them
    by under 0.5 dB either way at 64/256-QAM
    (research/mcs/coherent.md).

    The read is decision-directed, so where symbols cross decision boundaries (high symbol error
    rates) it reads low; it is exact in the limit of few symbol errors.  A max-log Viterbi is
    invariant to one common scale, so a single global N0 sets the LLRs' calibration, not the
    decoder's decisions."""
    z = np.asarray(y)
    r2 = np.abs(z - _qam_nearest(z, M)) ** 2
    if K is None:
        return np.mean(r2, axis=axis)
    r2 = r2.ravel()
    mode = np.arange(r2.size) % int(K)
    n = np.bincount(mode, minlength=int(K))
    return np.bincount(mode, r2, minlength=int(K)) / np.maximum(n, 1)


def soft_bits_from_qam(y, M: int, noise_var=None, K: int | None = None) -> np.ndarray:
    """Max-log per-bit LLRs for coherent square M-QAM symbols ``y`` (de-keyed, gain-corrected, as
    ``entroptics_rf.waveform._dekeyed`` returns them), Gray-labelled as the rf codec maps them
    (:func:`_qam_axis`).

    For each bit the value is ``(min_{s: bit=1} |y-s|^2 - min_{s: bit=0} |y-s|^2) / N0``: positive
    when bit 0 is more likely (this module's convention), with ``N0`` the complex noise variance.
    The square lattice factors into two PAM axes, so each bit's minimum runs over one axis's
    ``sqrt(M)`` levels only.

    ``noise_var`` (N0) defaults to :func:`qam_noise_var` measured on ``y`` itself: per mode when
    ``K`` is given (symbol i on mode i % K), otherwise one value for all.  An explicit array is
    broadcast against ``y`` -- e.g. a ``(K,)`` read for a ``(rows, K)`` ``y``.  Symbols are taken
    in C order; returns ``y.size * log2(M)`` values, bits most-significant first per symbol."""
    levels, labels, h = _qam_axis(M)
    z = np.asarray(y, dtype=np.complex128)
    if noise_var is None:
        noise_var = qam_noise_var(z, M, K=K)
        if K is not None:
            noise_var = noise_var[np.arange(z.size) % int(K)].reshape(z.shape)
    n0 = np.broadcast_to(np.asarray(noise_var, dtype=np.float64), z.shape).ravel()
    # a noiseless read has N0 = 0: floor it far below any noise a capture holds so the LLRs stay
    # finite (their signs, the hard decisions, are unaffected)
    step = float(levels[1] - levels[0])
    n0 = np.maximum(n0, 1e-12 * step * step)
    z = z.ravel()
    out = np.empty((z.size, 2 * h), dtype=np.float64)
    for a, x in enumerate((np.real(z), np.imag(z))):
        d2 = (x[:, None] - levels[None, :]) ** 2                     # (n, sqrt M)
        for b in range(h):
            one = ((labels >> (h - 1 - b)) & 1).astype(bool)
            out[:, a * h + b] = (np.min(d2[:, one], axis=1) - np.min(d2[:, ~one], axis=1)) / n0
    return out.ravel()


# ---------------------------------------------------------------------------
# Interleaving
# ---------------------------------------------------------------------------

def block_interleave(x: np.ndarray, rows: int, cols: int) -> np.ndarray:
    """Write ``x`` (length rows*cols) row by row, read it column by column.

    Two symbols adjacent in the output were ``cols`` apart in the input, so a
    burst of up to ``rows`` consecutive bad output symbols lands on input
    positions spaced ``cols`` apart.
    """
    x = np.asarray(x)
    if x.size != rows * cols:
        raise ValueError("length must equal rows*cols")
    return x.reshape(rows, cols).T.ravel()


def block_deinterleave(y: np.ndarray, rows: int, cols: int) -> np.ndarray:
    """Inverse of ``block_interleave``."""
    y = np.asarray(y)
    if y.size != rows * cols:
        raise ValueError("length must equal rows*cols")
    return y.reshape(cols, rows).T.ravel()


def interleaver_permutation(n: int, cols: int) -> np.ndarray:
    """Pruned block interleaver of any length ``n``.

    Input is written row by row into ``ceil(n/cols)`` rows of ``cols``; the
    output reads column by column and skips the empty cells of the last row.
    Returns ``perm`` such that ``out = x[perm]``.
    """
    rows = -(-n // cols)
    grid = np.full(rows * cols, -1, dtype=np.int64)
    grid[:n] = np.arange(n)
    perm = grid.reshape(rows, cols).T.ravel()
    return perm[perm >= 0]


def interleave(x: np.ndarray, cols: int) -> np.ndarray:
    """Apply the pruned block interleaver (see ``interleaver_permutation``)."""
    x = np.asarray(x)
    return x[interleaver_permutation(x.size, cols)]


def deinterleave(y: np.ndarray, cols: int) -> np.ndarray:
    """Inverse of ``interleave``; works on hard or soft values."""
    y = np.asarray(y)
    perm = interleaver_permutation(y.size, cols)
    out = np.empty_like(y)
    out[perm] = y
    return out


# ---------------------------------------------------------------------------
# Packet layer
# ---------------------------------------------------------------------------

#: Interleaver width for packets. Adjacent transmitted bits (the two bits of
#: one DQPSK symbol) end up 16 coded bits = 8 trellis steps apart, beyond the
#: code's constraint length.
PACKET_INTERLEAVE_COLS = 16

_HEADER_BYTES = 2
_CRC_BYTES = 4


def packet_coded_length(frame_bytes: int, rate="1/2") -> int:
    """Coded bits produced for a padded payload of ``frame_bytes`` bytes.

    With ``n = 8 * (2 + frame_bytes + 4) + 6`` trellis steps (header, frame,
    CRC, zero tail) the length is ``2n`` at rate 1/2, and
    ``punctured_length(n, rate)`` otherwise: ``3*(n//2) + 2*(n%2)`` at 2/3,
    ``4*(n//3) + (0, 2, 3)[n%3]`` at 3/4.
    """
    info_bits = 8 * (_HEADER_BYTES + frame_bytes + _CRC_BYTES)
    return punctured_length(info_bits + K - 1, rate)


def encode_packet(payload: bytes, frame_bytes: int | None = None,
                  cols: int = PACKET_INTERLEAVE_COLS, rate="1/2") -> np.ndarray:
    """Protect a payload for one physical packet.

    Layout of the protected part: 2-byte big-endian payload length, the
    payload padded with zero bytes to ``frame_bytes`` (default: no padding),
    then the CRC-32 of everything before it (4 bytes, big-endian). That is
    convolutionally encoded (zero tail), punctured to ``rate`` ("1/2",
    "2/3" or "3/4"; see ``PUNCTURE_PATTERNS``) and interleaved with
    ``PACKET_INTERLEAVE_COLS`` columns. Returns uint8 bits of length
    ``packet_coded_length(frame_bytes, rate)``.

    How the receiver knows the length: the coded length fixes the padded
    size, and the length header (inside the CRC) says how many of those
    bytes are payload. A mesh using fixed-size frames passes the same
    ``frame_bytes`` for every packet so the PHY frame size never changes.
    """
    payload = bytes(payload)
    if len(payload) > 0xFFFF:
        raise ValueError("payload longer than 65535 bytes")
    if frame_bytes is None:
        frame_bytes = len(payload)
    if len(payload) > frame_bytes:
        raise ValueError("payload longer than frame_bytes")
    body = len(payload).to_bytes(2, "big") + payload + bytes(frame_bytes - len(payload))
    body += crc32(body).to_bytes(4, "big")
    bits = np.unpackbits(np.frombuffer(body, dtype=np.uint8))
    return interleave(puncture(conv_encode(bits), rate), cols)


def decode_packet(soft: np.ndarray, cols: int = PACKET_INTERLEAVE_COLS,
                  rate="1/2") -> bytes | None:
    """Recover a payload from soft (or +/-1 hard) values; ``None`` if CRC fails.

    ``soft`` must be exactly the coded length of the packet (the receiver
    knows it from the fixed frame size), and ``rate`` the one it was sent
    at. Sign convention: positive = bit 0. To pass hard bits, convert them
    with ``hard_to_soft`` first. Punctured positions are restored as zeros
    (erasures) after deinterleaving, then Viterbi decodes the mother code.
    """
    llr = np.asarray(soft, dtype=np.float64).ravel()
    n = llr.size
    try:
        steps = _steps_for_length(n, rate)
    except ValueError:
        raise ValueError(f"{n} is not a valid packet coded length") from None
    if (steps - (K - 1)) <= 0 or (steps - (K - 1)) % 8:
        raise ValueError(f"{n} is not a valid packet coded length")
    info = viterbi_decode(depuncture(deinterleave(llr, cols), rate, steps))
    return _packet_body(np.packbits(info).tobytes())


def _packet_body(body: bytes) -> bytes | None:
    """The payload of a decoded packet body, or None if its CRC or length field refuses it."""
    if len(body) < _HEADER_BYTES + _CRC_BYTES:
        return None
    if crc32(body[:-_CRC_BYTES]) != int.from_bytes(body[-_CRC_BYTES:], "big"):
        return None
    length = int.from_bytes(body[:_HEADER_BYTES], "big")
    if length > len(body) - _HEADER_BYTES - _CRC_BYTES:
        return None
    return body[_HEADER_BYTES:_HEADER_BYTES + length]


def decode_packets(candidates, cols: int = PACKET_INTERLEAVE_COLS) -> list:
    """Decode several readings of packets at once: ``candidates`` is a sequence of ``(soft, rate)``,
    each as :func:`decode_packet` takes it; returns one payload-or-None per candidate, in order.

    Candidates whose mother codewords have the same length (every rate of one frame size) share
    one :func:`viterbi_decode_batch` pass.  This is how a receiver lets the frame's CRC choose
    among hypotheses about a burst -- its code rate, alphabet, family -- that nothing told it."""
    out: list = [None] * len(candidates)
    groups: dict = {}
    for i, (soft, rate) in enumerate(candidates):
        llr = np.asarray(soft, dtype=np.float64).ravel()
        try:
            steps = _steps_for_length(llr.size, rate)
        except ValueError:
            continue
        if (steps - (K - 1)) <= 0 or (steps - (K - 1)) % 8:
            continue
        groups.setdefault(steps, []).append((i, depuncture(deinterleave(llr, cols), rate, steps)))
    for steps, items in groups.items():
        info = (viterbi_decode(items[0][1])[None, :] if len(items) == 1     # one: the leaner loop
                else viterbi_decode_batch(np.stack([m for _, m in items])))
        for (i, _), bits in zip(items, info):
            out[i] = _packet_body(np.packbits(bits).tobytes())
    return out


# ---------------------------------------------------------------------------
# LT fountain code
# ---------------------------------------------------------------------------

#: Robust soliton parameters. Of the settings tried (c in 0.03..0.3, delta in
#: 0.05..0.5; 100 trials for k = 16 and 64, 25 for k = 256), c = 0.05,
#: delta = 0.05 gave the lowest overhead with the default Gaussian-elimination
#: decoder; the peeling decoder does somewhat better with larger delta. The
#: overhead test in tests/test_fec.py measures the values in use.
LT_C = 0.05
LT_DELTA = 0.05

_M64 = (1 << 64) - 1


def _splitmix64(state: int) -> tuple[int, int]:
    state = (state + 0x9E3779B97F4A7C15) & _M64
    z = state
    z = ((z ^ (z >> 30)) * 0xBF58476D1CE4E5B9) & _M64
    z = ((z ^ (z >> 27)) * 0x94D049BB133111EB) & _M64
    return state, z ^ (z >> 31)


def robust_soliton(k: int, c: float = LT_C, delta: float = LT_DELTA) -> list[float]:
    """Cumulative robust soliton distribution over degrees 1..k.

    Ideal soliton rho(1) = 1/k, rho(d) = 1/(d(d-1)); plus Luby's tau with
    R = c * ln(k/delta) * sqrt(k): tau(d) = R/(d k) for d < k/R,
    tau(k/R) = R ln(R/delta)/k (spike at floor(k/R)), 0 above. Normalized
    and returned as a CDF (list of k floats, last = 1.0). Computed in plain
    Python floats so sender and receiver get bit-identical tables.
    """
    if k < 1:
        raise ValueError("k must be >= 1")
    if k == 1:
        return [1.0]
    R = c * math.log(k / delta) * math.sqrt(k)
    spike = max(1, min(k, int(math.floor(k / R)))) if R > 0 else k
    mu = []
    for d in range(1, k + 1):
        rho = 1.0 / k if d == 1 else 1.0 / (d * (d - 1))
        if d < spike:
            tau = R / (d * k)
        elif d == spike:
            tau = R * math.log(R / delta) / k if R > delta else 0.0
        else:
            tau = 0.0
        mu.append(rho + max(tau, 0.0))
    z = sum(mu)
    cdf, acc = [], 0.0
    for v in mu:
        acc += v / z
        cdf.append(acc)
    cdf[-1] = 1.0
    return cdf


_CDF_CACHE: dict[tuple[int, float, float], list[float]] = {}


DENSE_K = 64        # up to this many blocks the code is systematic with dense repair, decoded by
                    # Gaussian elimination (the peeling decoder cannot untangle dense symbols)


def lt_neighbours(symbol_id: int, k: int, seed: int,
                  c: float = LT_C, delta: float = LT_DELTA) -> list[int]:
    """Source-block indices XORed into encoded symbol ``symbol_id``.

    Deterministic in ``(seed, symbol_id, k, c, delta)``. The generator is a
    self-contained SplitMix64 in Python integers rather than numpy's
    Generator, whose output streams numpy does not promise to keep across
    versions; sender and receiver may run different versions. The degree is
    drawn from the robust soliton CDF; the neighbours by a partial
    Fisher-Yates shuffle.
    """
    if k <= DENSE_K and symbol_id < k:
        # Systematic (small messages only): the first k symbols are the source blocks themselves,
        # so a clean link completes on exactly k symbols.  For large k it measured worse with LT
        # repair (1.043 against 1.011 at k = 256), so the large-message code stays pure LT.
        return [int(symbol_id)]
    if k <= DENSE_K:
        # Small messages: a dense repair symbol (each block in with probability 1/2).  The robust
        # soliton is tuned for large k; at k = 6 it left the decoder short after 9-10 distinct
        # symbols (emulated relay chain), where a dense code needs about k + 2.
        _, h = _splitmix64(seed & _M64)
        state = h ^ (symbol_id & _M64)
        out = []
        for j in range(k):
            state, r = _splitmix64(state)
            if r >> 63:
                out.append(j)
        if not out:
            state, r = _splitmix64(state)
            out = [int(r % k)]
        return out
    key = (k, c, delta)
    cdf = _CDF_CACHE.get(key)
    if cdf is None:
        cdf = _CDF_CACHE[key] = robust_soliton(k, c, delta)
    _, h = _splitmix64(seed & _M64)
    state = h ^ (symbol_id & _M64)
    state, r = _splitmix64(state)
    u = (r >> 11) * (1.0 / (1 << 53))
    lo, hi = 0, k - 1          # first index with cdf > u
    while lo < hi:
        mid = (lo + hi) // 2
        if cdf[mid] > u:
            hi = mid
        else:
            lo = mid + 1
    degree = lo + 1
    pool = list(range(k))
    for i in range(degree):
        state, r = _splitmix64(state)
        j = i + r % (k - i)
        pool[i], pool[j] = pool[j], pool[i]
    return pool[:degree]


def _split_blocks(message: bytes, block_size: int) -> list[int]:
    n_blocks = max(1, -(-len(message) // block_size))
    padded = bytes(message) + bytes(n_blocks * block_size - len(message))
    return [int.from_bytes(padded[i * block_size:(i + 1) * block_size], "big")
            for i in range(n_blocks)]


def fountain_encode(message: bytes, block_size: int, seed: int,
                    start_id: int = 0, c: float = LT_C,
                    delta: float = LT_DELTA) -> Iterator[tuple[int, bytes]]:
    """Endless stream of LT-encoded symbols ``(symbol_id, block_size bytes)``.

    The message is zero-padded to ``k = ceil(len/block_size)`` blocks
    (at least 1). Symbol ``i`` is the XOR of the blocks
    ``lt_neighbours(i, k, seed)``. The caller sends ``symbol_id`` (and, so
    the receiver can size its decoder and trim padding, ``k`` and the
    message length) in the packet header and stops when it chooses.
    """
    blocks = _split_blocks(message, block_size)
    k = len(blocks)
    sid = start_id
    while True:
        acc = 0
        for j in lt_neighbours(sid, k, seed, c, delta):
            acc ^= blocks[j]
        yield sid, acc.to_bytes(block_size, "big")
        sid += 1


class FountainDecoder:
    """Receiver for ``fountain_encode``.

    ``add`` accepts symbols in any order, ignores duplicates of an id
    already seen, and returns True once all ``n_blocks`` blocks are known.

    Two methods:

    * ``"peel"``: the classic LT belief-propagation (peeling) decoder. A
      symbol whose unknown neighbours drop to one releases that block, which
      is XORed out of every other symbol that contains it.
    * ``"gauss"`` (default): incremental Gaussian elimination over GF(2).
      Each arriving symbol is reduced against the pivot rows held so far and
      kept as a new pivot if anything remains; when there are k pivots the
      system is back-substituted. The decoder therefore completes as soon as
      the received symbols have full rank, which is maximum-likelihood
      erasure decoding and needs fewer symbols than peeling, most visibly
      for small k. Rows are Python integers used as bit masks (coefficients)
      and as block data, so each row operation is one big-integer XOR.
    """

    def __init__(self, n_blocks: int, block_size: int, seed: int,
                 method: str = "gauss", c: float = LT_C,
                 delta: float = LT_DELTA):
        if method not in ("gauss", "peel"):
            raise ValueError("method must be 'gauss' or 'peel'")
        self.k = int(n_blocks)
        self.block_size = int(block_size)
        self.seed = int(seed)
        self.method = method
        self.c = float(c)
        self.delta = float(delta)
        self.blocks: list[int | None] = [None] * self.k
        self.n_known = 0
        self.n_received = 0
        self._seen: set[int] = set()
        # peeling state
        self._pending: dict[int, list] = {}          # sym -> [set(nbrs), data]
        self._by_block: dict[int, set[int]] = {}     # block -> pending syms
        self._next_sym = 0
        # elimination state: pivot bit -> (mask, data); mask's lowest bit = pivot
        self._pivots: dict[int, tuple[int, int]] = {}

    @property
    def complete(self) -> bool:
        return self.n_known == self.k

    def add(self, symbol_id: int, data: bytes) -> bool:
        """Add one received symbol; returns True when the message is complete."""
        if self.complete:
            return True
        if symbol_id in self._seen:
            return False
        self._seen.add(symbol_id)
        self.n_received += 1
        value = int.from_bytes(bytes(data), "big")
        nbrs = set(lt_neighbours(symbol_id, self.k, self.seed, self.c, self.delta))
        for j in list(nbrs):
            b = self.blocks[j]
            if b is not None:
                nbrs.discard(j)
                value ^= b
        if not nbrs:
            return self.complete
        if self.method == "gauss":
            self._eliminate(nbrs, value)
        else:
            self._peel_add(nbrs, value)
        return self.complete

    # -- peeling -----------------------------------------------------------
    def _peel_add(self, nbrs: set[int], value: int) -> None:
        sid = self._next_sym
        self._next_sym += 1
        self._pending[sid] = [nbrs, value]
        for j in nbrs:
            self._by_block.setdefault(j, set()).add(sid)
        ripple = [sid] if len(nbrs) == 1 else []
        while ripple:
            s = ripple.pop()
            entry = self._pending.pop(s, None)
            if entry is None or len(entry[0]) != 1:
                if entry is not None:
                    self._pending[s] = entry
                continue
            (j,) = entry[0]
            self._by_block.get(j, set()).discard(s)
            if self.blocks[j] is not None:
                continue
            self._set_block(j, entry[1])
            for s2 in self._by_block.pop(j, set()):
                e2 = self._pending.get(s2)
                if e2 is None:
                    continue
                e2[0].discard(j)
                e2[1] ^= entry[1]
                if len(e2[0]) == 1:
                    ripple.append(s2)
                elif not e2[0]:
                    self._pending.pop(s2)

    def _set_block(self, j: int, value: int) -> None:
        self.blocks[j] = value
        self.n_known += 1

    # -- Gaussian elimination ----------------------------------------------
    def _eliminate(self, nbrs: set[int], value: int) -> None:
        mask = 0
        for j in nbrs:
            mask |= 1 << j
        pivots = self._pivots
        while mask:
            p = (mask & -mask).bit_length() - 1
            row = pivots.get(p)
            if row is None:
                pivots[p] = (mask, value)
                break
            mask ^= row[0]
            value ^= row[1]
        if len(pivots) == self.k:
            self._back_substitute()

    def _back_substitute(self) -> None:
        pivots = self._pivots
        for p in sorted(pivots, reverse=True):
            mask, value = pivots[p]
            rest = mask & ~(1 << p)
            while rest:
                q = (rest & -rest).bit_length() - 1
                rest &= rest - 1
                value ^= self.blocks[q]
            self._set_block(p, value)
        pivots.clear()

    def message(self, length: int | None = None) -> bytes:
        """The decoded message (padded to k*block_size unless ``length``)."""
        if not self.complete:
            raise RuntimeError("message not yet complete")
        out = b"".join(b.to_bytes(self.block_size, "big") for b in self.blocks)
        return out if length is None else out[:length]
