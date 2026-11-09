"""
phy.py -- the mesh's only contact with entroptics-rf.

A transmission is a burst: one compose call, so it carries its own keyed reference row and is read
on its own.  It goes out in the family the plan names:

  * ``differential``  M-PSK across rows (``waveform.compose``), M in 2/4/8/16, Gray-labelled
                      (``fec.gray_to_phy``).  Marked with the sender's identity for free
                      (``sender=``: a per-mode rotation the differential read cancels).
  * ``coherent``      square M-QAM (``waveform.compose_qam``), M in 4/16/64/256: one keyed reference
                      row measures every mode's gain, the payload rides coherently after it.
                      ``compose_qam`` takes no sender mark, so a coherent burst carries none.

Nothing in a burst names its family, its alphabet or its code rate, and nothing about them is
agreed in advance.  The receiver reads the burst both ways at the geometry it measured -- the
differential read from the burst's first row, the coherent read from its keyed reference row (one
gain for every mode, the row-rate carrier measured off the payload) -- and hands both up
(:class:`RxBurst`).  The frame layer then decodes every (family, alphabet, rate) hypothesis in one
batched Viterbi pass and the frame's CRC chooses (``link.read_frame``).  The closures the reads measure only order the
hypotheses; none of them decides.  Why the CRC and not a header: ``link.read_frame``.

The key is whatever the crypto provider supplies (``waveform_key``); None is the public zero state.
Rate and centre are the radio's business: the burst is written at the sender's rate, and the
receiver measures the ratio of that rate to its own.

Known limits, stated rather than hidden:
  * blind reception (:func:`receive_burst`) uses ``waveform.discover``'s integer read, or the
    continuous-scale search for a narrower sender; both lock poorly on a single short coherent burst
    (research/mcs/coherent.md: 4-QAM width/lock misses at 6-12 dB, cold scale reads at rho != 1).
    A sender heard once is read at its known scale (:func:`receive_known`), which is the fast path.
  * the sender mark is differential and read on the integer screen only.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace

import functools

import os

import numpy as np
from entroptics import occupied_modes, level_shift_scan

from entroptics_rf import waveform as wf

from . import fec
from .providers import TxPlan

FAMILIES = ("differential", "coherent")
DIFFERENTIAL_M = (2, 4, 8, 16)          # the M-PSK alphabets ``waveform.compose`` writes
COHERENT_M = (4, 16, 64, 256)           # the square QAM orders the mesh sends (``compose_qam``)


def capacity_bits(n_rows: int, plan: TxPlan) -> int:
    """Payload bits a burst of ``n_rows`` data rows carries at a plan's geometry."""
    return int(n_rows) * int(plan.K) * int(round(np.log2(plan.M)))


def rows_for(n_bits: int, plan: TxPlan) -> int:
    per = int(plan.K) * int(round(np.log2(plan.M)))
    return -(-int(n_bits) // per)


def transmit_burst(bits, plan: TxPlan, key, sender: bytes | None = None, *, rng=None) -> np.ndarray:
    """The burst's IQ at the sender's own rate: unit-RMS complex64, reference row first, in the
    plan's family and alphabet.  ``bits`` are already coded (``link.frame`` at the plan's code
    rate).  A differential burst is Gray-labelled: a symbol's bits fill to the symbol boundary with
    keyed filler (as ``compose`` itself would), then map through ``fec.gray_to_phy``.  ``sender``
    marks a differential burst; a coherent one carries no mark.  ``rng`` feeds the open mode's
    per-row radius (``waveform.row_radius``, the sender's own draw when ``key`` is None); None
    draws fresh entropy.  Keyed, the radius comes from the key and ``rng`` is unused."""
    bits = np.asarray(bits, dtype=np.int64).ravel()
    M = int(plan.M)
    if plan.family == "coherent":
        return wf.compose_qam(bits, plan.K, plan.L, key, M=M, rng=rng)
    if plan.family != "differential":
        raise ValueError(f"unknown family {plan.family!r}; use one of {FAMILIES}")
    b = int(round(np.log2(M)))
    pad = (-bits.size) % b
    if pad:
        bits = np.concatenate([bits, (wf._keyed_uniform(wf._D_FILL, key, pad) < 0.5).astype(np.int64)])
    return wf.compose(fec.gray_to_phy(bits, M), plan.K, plan.L, key, M=M, sender=sender, rng=rng)


_EMPTY = np.zeros((0, 0), dtype=complex)
_EMPTY_POWER = np.zeros((0, 0))


def _unkeys(K: int, key, rows: int) -> np.ndarray:
    """``conj(waveform.mode_keys(K, key, first=0, rows=rows))``, kept across bursts: every burst
    from a sender is de-keyed from row 0 with the same key and a few mode counts, so the keystream
    is drawn once per (K, key) and sliced (the keys are row-nested: a longer draw starts with the
    shorter one).  Drawing it per call was ~7 ms of a burst."""
    rows = max(int(rows), 0)
    return _unkeys_block(int(K), key, -(-max(rows, 1) // 64) * 64)[:rows]


@functools.lru_cache(maxsize=64)
def _unkeys_block(K: int, key, rows: int) -> np.ndarray:
    a = np.conj(wf.mode_keys(K, key, None, first=0, rows=rows))
    a.flags.writeable = False
    return a


TURN_WEIGHT = 8         # the amplitude power the row-turn read weighs each symbol's fourth power by
TURN_PASSES = 3         # the row-turn read's passes (a bound on the iterations, not a decision)


def _row_turn(Y, passes: int = TURN_PASSES) -> np.ndarray:
    """Coherent rows ``Y`` (``Y[i]`` is i+1 rows after the reference) with the residual row turn
    the burst's own fourth moment measures taken off.  No decision, no alphabet.

    The codec reads the row-rate carrier with the fourth power (``waveform._row_carrier``), which
    needs no alphabet but carries the data's own scatter: a 256-QAM row of 60 symbols leaves its
    fourth-power sum ~40% off its mean, so a 13-row burst reads the turn ~1e-3 cycles/row off even
    noiseless (measured: -40 dB on the first row, -23 dB by the tenth).  What is left is read here
    on the same statistic, weighted toward the points that carry it: a square QAM's fourth moment
    is negative real, and each symbol's fourth power scatters about it less the further out the
    symbol sits (a corner's is exactly on it), so each row's sum ``z_t = -sum_k |y|^p (y/|y|)^4``
    with p = ``TURN_WEIGHT`` leans on the outer points.  ``angle(z_t) / 4`` is the row's residual
    turn; a slope through the reference (row 0, phase 0), weighted by |z_t|, is taken off and the
    read repeated.  A moment of the received symbols, not a fit to sliced ones: nothing is decided.

    It replaces a decision-directed read (each row's turn against its nearest lattice points under
    the hypothesis's M), ruled out of bounds.  Measured on the mesh's fast path, 32 bursts a cell
    (lab rank_leak/adopted/mesh/turn_compare.py --final), frames delivered at 26 / 27 / 28 / 29 /
    30 dB, 256-QAM 3/4: matched none 10/20/25/29/30, decision-directed 15/25/31/32/32, this
    18/27/29/32/32; 1.0 -> 2.4 Msps none 6/15/23/26/27, decision-directed 13/23/32/32/32, this
    12/23/31/32/32.  256-QAM 2/3: matched 29-31 of 32 with nothing and 31/32 with either read (one
    burst lost to the scan); 1.0 -> 2.4 Msps 30/32 at 26 dB with nothing or this, 31/32
    decision-directed, 32/32 above.
    On the earlier exploratory grid, p = 4 (the codec's own weighting) delivered less (matched
    3/4 15/23/28/29/29 against p = 8's 17/25/27/29/29) and p = 12 no more than p = 8."""
    Y = np.asarray(Y)
    if Y.ndim != 2 or Y.shape[0] < 2:
        return Y
    t = np.arange(1, Y.shape[0] + 1, dtype=float)
    for _ in range(int(passes)):
        a = np.abs(Y)
        z = -np.sum(a ** TURN_WEIGHT * (Y / np.maximum(a, 1e-300)) ** 4, axis=1)
        w = np.abs(z)
        den = float(np.sum(w * t * t))
        if den <= 0:
            break
        Y = Y * np.exp(-1j * (float(np.sum(w * t * np.angle(z))) / den / 4.0) * t)[:, None]
    return Y


@dataclass(frozen=True)
class RxBurst:
    """What a receiver measured of one burst, read both ways.

    ``discovery``  the geometry, with the differential read's start, mode count and closure.
    ``soft``       the de-keyed differential from the burst's first row, ``(rows, K)``: its angle
                   carries the symbols and its magnitude the confidence.
    ``qam``        the coherent read: de-keyed, gain-corrected symbols after the keyed reference
                   row, ``(rows, K)``, the row-rate carrier off.
    ``coherent``   that read's own Discovery (K, reference row, carrier, fourth-power closure), or
                   None when it was not made.
    ``cache``      the frame layer's decode of this burst (``link.read_frame``), kept so a burst
                   whose frame was already checked is not decoded twice.
    ``keyed``      whether ``qam`` came back with each row's power already off.  Keyed, the
                   codec divides the per-row radius out exactly (``waveform.row_radius``); open,
                   the radius is the sender's own draw and the rows come back at ``r_t / r_0``,
                   so they are levelled by their own power once the rows and the order are known
                   (``waveform._row_levels``).
    ``power``      |coordinate|^2 of every row the capture holds at the read's geometry, ``(rows,
                   L)`` -- the burst's own rows and the rows of the same band around it -- for
                   :func:`burst_snr`; empty when the read kept none.
    ``first``      the row of ``power`` the differential read takes as the burst's first (the
                   coherent read's is ``coherent.reference``)."""
    discovery: "wf.Discovery"
    soft: np.ndarray
    qam: np.ndarray = field(default_factory=lambda: _EMPTY)
    coherent: "wf.Discovery | None" = None
    cache: dict = field(default_factory=dict, compare=False, repr=False)
    keyed: bool = True
    power: np.ndarray = field(default_factory=lambda: _EMPTY_POWER, compare=False, repr=False)
    first: int = 0

    @property
    def locked(self) -> bool:
        return self.discovery.locked

    @property
    def closure(self) -> float:
        """The better of the two reads' closures: how firmly this burst reads at all."""
        c = float(self.discovery.closure) if self.discovery.locked else 0.0
        return max(c, float(self.coherent.closure) if self.coherent is not None else 0.0)

    @property
    def family(self) -> str:
        """The family the closures favour -- an ordering of hypotheses, not a decision."""
        if self.coherent is not None and (not self.soft.size
                                          or self.coherent.closure > self.discovery.closure):
            return "coherent"
        return "differential"

    def hypotheses(self):
        """``(family, M)`` pairs this burst could carry, in the order the reads favour them: the
        family that closes better first, and within a family the alphabet its own read names
        first (the differential closure's M; the square lattice the coherent symbols close on,
        ``waveform._qam_order``, over the burst's measured rows) -- weak near threshold, which is
        why it only orders and the CRC decides."""
        fams = (self.family,) + tuple(f for f in FAMILIES if f != self.family)
        out = []
        for f in fams:
            if f == "differential" and self.soft.size:
                lead = int(self.discovery.M) if self.discovery is not None else 4
                out += [(f, M) for M in sorted(DIFFERENTIAL_M, key=lambda m: m != lead)]
            elif f == "coherent" and self.qam.size:
                rows = self.rows("coherent") or self.qam.shape[0]
                q = self.qam[:rows] if self.keyed else wf._row_levels(self.qam[:rows])
                c = wf._qam_order(q, COHERENT_M)[3]
                out += [(f, M) for _, M in sorted(zip(-np.asarray(c), COHERENT_M))]
        return out

    def rows(self, family: str) -> int | None:
        """How many payload rows this family's read of the burst holds, measured: the rank edge of
        the per-row level (``entroptics.occupied_modes`` -- the read ``waveform`` uses for the mode
        count, here on the row axis), the level being each row's median over modes of |soft| or
        |qam|^2.  None when the read has no edge to give: the rows above it are not one run from
        the burst's first row (the capture ends inside the burst, or something else is in it).
        A last row carrying few modes reads as noise, so the count can be one short.

        The edge is the largest step in the level, which exists whether or not the capture holds a
        row past the burst.  Where it does not -- a capture ending under a row after the burst --
        the step falls between two of the burst's own rows (a full final row read at the capture's
        edge reads a lower level), and a count from it sent the frame layer past the true
        hypothesis: measured, 256-QAM 3/4 at 50 dB, 1.0 -> 2.4 Msps, 9-11 of 80 frames lost.  So
        the edge is checked against the burst's run of rows (:func:`_run` on each row's
        constant-modulus read, which is blind to level): a run ending at or before the edge
        confirms it, a run ending one row past it is the count (the edge was one of the burst's
        own rows), and a run reaching further means the rows past the edge are the burst's -- no
        length to give.  With a row of silence after the burst the two agree (measured over 8
        modes, 3.5-50 dB, keyed and open, both rates: every count as before)."""
        a = self.soft if family == "differential" else self.qam
        if a.ndim != 2 or a.shape[0] < 2:
            return None
        level = np.median(np.abs(a) ** (1 if family == "differential" else 2), axis=1)
        if not np.all(np.isfinite(level)) or level.max() <= 0:
            return None
        k = int(occupied_modes(level).k)
        top = np.argsort(-level, kind="stable")[:k]
        if not (k < level.size and int(top.max()) == k - 1):
            return None
        b = int(_run(wf._cm_rows(a.real ** 2 + a.imag ** 2), 1)[2])
        return k if b <= k else (b if b == k + 1 else None)

    def soft_bits(self, family: str, M: int, n_bits: int) -> np.ndarray | None:
        """The first ``n_bits`` soft bits (positive = 0) of the burst read as ``family``/``M``, or
        None when the read holds too few symbols.  Differential: the angular metric weighted by
        magnitude, Gray labels.  Coherent: max-log LLRs with N0 measured per mode off the
        hypothesis's own symbols (``fec.soft_bits_from_qam(..., K=K)``) -- so the rows after the
        burst do not enter the noise read.  Open, the hypothesis's rows are first levelled by
        their own power and fitted to its lattice (``waveform._row_levels``)."""
        b = int(round(np.log2(int(M))))
        n_sym = -(-int(n_bits) // b)
        if family == "differential":
            d = self.soft.ravel()
            if d.size < n_sym:
                return None
            return fec.soft_bits_from_differential(_diff_turn(d[:n_sym], int(M)), M=int(M),
                                                   weight_by_magnitude=True, gray=True)[:n_bits]
        if self.qam.size < n_sym:
            return None
        K = int(self.qam.shape[1])
        Y = self.qam[:-(-n_sym // K)]
        if not self.keyed:
            Y = wf._row_levels(Y, int(M), n=n_sym, L=self.discovery.L)
        y = _row_turn(Y).ravel()
        return fec.soft_bits_from_qam(y[:n_sym], int(M), K=K)[:n_bits]


def _diff_turn(d, M: int) -> np.ndarray:
    """Differential symbols ``d`` read as M-PSK, with the residual row turn their own M-th power
    measures taken off: arg(mean u^M) / M, u the unit phasors, on the branch nearest no turn.

    A carrier left on the geometry turns every row's differential by the same angle, and M-PSK
    raised to M leaves only that turn -- a moment of the received symbols under the hypothesis's
    own alphabet, no decision.  It is read per hypothesis, on the symbols the hypothesis spans,
    because the alphabet is the frame check's to name: read once with the M the start closure
    favoured, a 16-PSK burst near its threshold (where |mean u^16| sits near its own noise) was
    turned with M = 2, 4 or 8 -- a random turn of up to a quarter cycle -- and lost (measured,
    D16PSK 1/2 at 16.6 dB: 2 of 40 frames lost that way, 0 of 100 read per hypothesis).  The
    settled carrier leaves this turn small (p90 0.003-0.007 cycles/row, D8PSK/D16PSK near
    threshold), so the branch nearest no turn is the branch."""
    u = d / np.maximum(np.abs(d), 1e-300)
    z = complex(np.mean(u ** int(M))) if d.size else 0j
    return d * np.exp(-1j * np.angle(z) / int(M)) if abs(z) > 0 else d


def _closure(dd) -> tuple[float, int]:
    """How firmly a de-keyed differential closes on an M-PSK alphabet, and on which: the largest
    |mean u^M| over the alphabets ``compose`` writes (u the unit phasors).  A true M-PSK closes at
    its own M and above; the lowest power is the least noisy, so the max lands on it."""
    u = dd / np.maximum(np.abs(dd), 1e-300)
    best, at, p = -1.0, 2, u
    for M in DIFFERENTIAL_M:
        p = p * p if M > 2 else u * u
        c = float(np.abs(np.mean(p))) if p.size else 0.0
        if c > best:
            best, at = c, M
    return best, at


def _start_scores(rows_of, Ks, key, carrier: float, reach: int = 8) -> dict:
    """Differential closure of every (mode count, first row) candidate: ``{(K, r): (closure, M)}``.

    The differential is de-keyed from the keystream's first row at each candidate start: at the
    true start the constellation closes, anywhere else it does not.  Every mode is lit, so the
    mode count is the width's (``Ks`` is ``(L,)``); there is no count to search.  ``reach`` bounds
    how many leading rows are considered; it decides nothing."""
    scores = {}
    for K in Ks:
        A = rows_of(K)
        mk = _unkeys(K, key, max(A.shape[0] - 1, 0))
        rot = np.exp(-2j * np.pi * carrier)
        for r in range(min(int(reach), A.shape[0] - 2)):
            dd = A[r + 1:] * np.conj(A[r:-1]) * rot
            scores[(K, r)] = _closure(dd * mk[:dd.shape[0]])
    return scores


def _best(scores: dict):
    """The (K, r) with the highest closure; the first such in search order on a tie."""
    at, best = None, -1.0
    for k, (c, _) in scores.items():
        if c > best:
            at, best = k, c
    return at


def _coherent_rows(A, K: int, key, carrier: float, L: int):
    """The coherent read of rows already projected (all modes): the keyed reference row located
    (``waveform._reference_at``), each mode's gain off it, the row-rate carrier read off the
    payload (``waveform._row_carrier``, placed from ``carrier``) and taken off.  Returns
    ``(Y, nu, r)``.

    The carrier has two halves (``waveform``'s coherent note): the row-rate half is read here with
    the burst's rows as lever; the column half is the projection's, so ``carrier`` must already be
    close (``receive_known`` measures it again before this read).  The fourth power cannot see a
    quarter turn per row, so the row read lands on the branch nearest ``carrier``.  ``L`` is the
    width, which keys the per-row radius the read divides out (``waveform._dekeyed``)."""
    A = A[:, :int(K)]
    if A.shape[0] < 2 or K < 1:
        return np.zeros((0, max(int(K), 0)), dtype=complex), float(carrier), 0
    r = wf._reference_at(A, int(K), key)
    nu = wf._row_carrier(_common_gain(A, r, int(K), key, 0.0, L), float(carrier))
    return _common_gain(A, r, int(K), key, nu, L), float(nu), int(r)


def _common_gain(A, r: int, K: int, key, carrier: float, L: int) -> np.ndarray:
    """``waveform._dekeyed`` with one gain for every mode: the mean of the keyed reference row's
    per-mode reads, rather than each mode's own.

    Every keyed mode spans the whole band, so a channel reaches each mode's own coordinate with
    nearly the same gain -- the band's mean response, give or take its spread over sqrt(L) -- and
    everything else it does is mixing between modes, which no per-mode gain undoes.  What a single
    reference row reads per mode is that gain plus the row's noise and its own share of the mixing,
    and dividing it out of every payload row spends both again on each symbol.  The mean over the
    modes measures the one gain there is with the noise of L reads.  Measured at the true geometry
    (40 frames a cell, 1.0 -> 2.4 Msps unless noted), decoded per-mode / common: 4-QAM 1/2 8.6 dB
    34/40 / 40/40 (matched 32 / 40), 4-QAM 2/3 10 dB 37 / 40, 16-QAM 1/2 12 dB 30 / 40, 64-QAM 2/3
    19.6 dB 29 / 40, 256-QAM 2/3 27.5 dB 40 / 40 (raw BER 0.019 / 0.004).  Noiseless, with a
    3-sample echo of 0.1 / 0.3 added at the receiver, the common gain still reads the payload 2.8 /
    3.0 dB cleaner (-20 / -11 dB against -17 / -8): the per-mode spread it ignores is mixing, not
    gain.

    The rf codec's own coherent read now takes this gain (``waveform._dekeyed``), so this is that
    read; kept under its name for the callers here."""
    return wf._dekeyed(A, r, K, key, carrier, L=L)


def _coherent_discovery(d, Y, nu, r, K) -> "wf.Discovery":
    return wf.replace(d, K=int(K), closure=wf._fourth_closure(Y), carrier=float(nu), row0=0,
                      aligned=False, beacon=False, payload="coherent", reference=int(r))


def _burst_start(iq, d, key, reach: int = 8) -> int:
    """The row the burst begins on inside the capture, at the measured geometry.

    The codec's row alignment covers a receiver that joined a stream late, not one whose capture
    began before the burst: a leading row of noise is read as the stream's first row and the
    keystream index comes out wrong (measured: one leading row -> BER 0.50; none -> 0.00).  The
    differential also cancels the sender's mark, which a plain correlation against the reference
    row does not.  Every mode is lit and the mesh sends no beacon, so the start is searched on all
    L modes whatever the discovery's alphabet read named (a leading noise row can make it prefer a
    beacon hypothesis, K = L - 3)."""
    S = wf.screen(np.asarray(iq), d.L, offset=d.offset)
    rows_of = lambda K: S @ wf.mode_basis(K, d.L, key, None, d.phase, d.carrier).conj().T
    return _best(_start_scores(rows_of, (int(d.L),), key, d.carrier, reach))[1]


def _read(iq, key, candidates):
    d = wf.discover(iq, key, candidates)
    if not d.locked:
        return RxBurst(d, _EMPTY)
    soft = wf.read_soft(iq, d.L, d.K, key, offset=d.offset, phase=d.phase, carrier=d.carrier,
                        row0=d.row0, skip=wf.BEACON_MODES if d.beacon else 0, scale=d.scale)
    # The coherent read sees every occupied mode: a coherent burst closes on no differential
    # hypothesis, so a beacon hypothesis can win and take three modes (decode's give-back).
    K_occ = int(d.K) + (wf.BEACON_MODES if d.beacon else 0)
    Y, nu, r = wf._coherent_read(iq, d.L, K_occ, key, d.offset, d.phase, d.carrier, d.scale)
    return RxBurst(d, soft, Y, _coherent_discovery(d, Y, nu, r, K_occ), keyed=key is not None)


def _scaled_burst(iq, key, candidates, reach: int = 8) -> RxBurst:
    """A sender narrower than the receiver: discovery at a continuous scale, then the burst's first
    row found by the same closure argmax as :func:`_burst_start`, on the continuous geometry, and
    the coherent read on the same rows."""
    d = wf._discover_scaled(iq, key, candidates)
    if not d.locked:
        return RxBurst(d, _EMPTY)
    ell = d.L / d.scale
    s0 = d.offset - d.phase
    rows = wf._scaled_rows(iq, d.L, key, None, d.offset, d.phase, d.scale, d.carrier)  # read once
    scores = _start_scores(lambda K: rows[:, :K], (int(d.L),), key, d.carrier, reach)
    atK, at = _best(scores) or (d.L, 0)
    s = s0 + at * ell
    off = int(np.ceil(s))
    soft = wf.read_soft(iq, d.L, atK, key, offset=off, phase=off - s, carrier=d.carrier, row0=0,
                        scale=d.scale)
    Y, nu, r = _coherent_rows(rows, d.L, key, d.carrier, d.L)
    c, M = scores.get((atK, at), (d.closure, d.M))
    dd = wf.replace(d, K=atK, offset=off, phase=off - s, row0=0, closure=float(c), M=int(M),
                    beacon=False)
    return RxBurst(dd, soft, Y, _coherent_discovery(d, Y, nu, r, d.L), keyed=key is not None,
                   power=rows.real ** 2 + rows.imag ** 2, first=int(at))


def _with_power(rx: RxBurst, iq, key, first: int) -> RxBurst:
    """An integer-screen read with its rows' power kept (:attr:`RxBurst.power`), ``first`` the
    burst's first row among them."""
    d = rx.discovery
    try:
        S = wf.screen(np.asarray(iq), d.L, offset=d.offset)
        A = S @ wf.mode_basis(int(d.L), d.L, key, None, d.phase, d.carrier).conj().T
    except (ValueError, IndexError):
        return rx
    return replace(rx, power=A.real ** 2 + A.imag ** 2, first=int(first))


def burst_snr(rx: RxBurst, family: str, rows: int, prior=()):
    """The burst's own signal-to-noise ratio, measured on the read that decoded it: ``(snr_db,
    signal, noise, quiet)`` -- signal and noise per coordinate, ``quiet`` the levels of the rows
    the noise was read on here (for a caller to pass as ``prior`` to the next read at this
    geometry) -- or None when no row outside the burst is held, here or in ``prior``.

    The read projects every row the capture holds onto the sender's own band at the sender's own
    geometry (:attr:`RxBurst.power`), so each coordinate holds that band's share of whatever was on
    the air then: a sender on another sub-band, or a louder burst earlier in the slot, is not in
    it.  The burst's rows (its first, ``rows`` long -- reference row and payload, the length the
    frame the CRC chose fills) hold its signal plus the band's noise.  The other rows hold the
    noise, except:

      * the row on either side of the burst holds its edge (the row read reaches a little past
        its own row: measured, the row after a 1.2 Msps DBPSK burst heard at 30-39 dB read 4-45x
        the noise, the next one the noise), so those two are left out while any other row is held;
      * some rows carry what a sender on the neighbouring sub-band spills over the shared band
        edge (there is no guard band: measured, a 1.2 Msps burst on one half of 2.4 MHz spills ~13
        dB below its own level into the other half -- at 14 dB it lifts that half's rows 2.2-2.4x
        over the noise while it lasts; the median over every row outside the burst read a 16-QAM
        burst at 18 dB 1 dB low beside it).

    Noise: a low quantile of those rows' levels (each row's mean over its L coordinates) over the
    same quantile of what as many rows of noise alone read -- each a mean of L exponential powers,
    Gamma(L, N / L) -- which is exact on rows of noise and is not lifted while fewer than three
    rows in four carry something else.  A burst that fills the capture leaves one or two such
    rows (measured: a 1.2 Msps DBPSK burst in a node's slot, one row -- +-0.55 dB on its own, and
    1-3 dB low where it was not clean), so ``prior`` adds the quiet rows of earlier reads at the
    same geometry (same band, same coordinates): the band's noise, read around the last bursts too.
    Signal: the mean over the burst's rows less that noise (the final row is left out: a message's
    last row may light fewer modes).  Nothing here is a threshold or a pre-shared level.  The ratio
    is the in-band SNR the planner and the plausibility check use (a narrow sender's whole power
    over the noise in its own band); a neighbour's spill inside the burst's own rows is counted as
    signal (~13 dB below the neighbour: +0.3 dB for a neighbour 5 dB louder over half the burst)."""
    P = rx.power
    if P.ndim != 2 or P.shape[0] < 2 or P.shape[1] < 1:
        return None
    a = (int(rx.coherent.reference) if family == "coherent" and rx.coherent is not None
         else int(rx.first))
    b = min(P.shape[0], a + max(int(rows), 1))
    a = max(a, 0)
    if b <= a:
        return None
    quiet = np.concatenate([P[:max(a - 1, 0)], P[b + 1:]]).mean(axis=1)
    quiet = quiet[np.isfinite(quiet)]
    prior = np.asarray(prior, dtype=float).ravel()
    level = np.concatenate([quiet, prior[np.isfinite(prior)]])
    if level.size == 0:                 # only the rows next to the burst are held: use them
        level = np.concatenate([P[:a], P[b:]]).mean(axis=1)
        level = level[np.isfinite(level)]
    if level.size == 0:
        return None
    noise = (float(np.percentile(level, 100 * NOISE_QUANTILE))
             / noise_quantile(int(P.shape[1]), int(level.size), NOISE_QUANTILE))
    inside = P[a:b - 1] if b - a >= 3 else P[a:b]
    inside = inside[np.isfinite(inside)]
    if inside.size == 0 or noise <= 0:
        return None
    signal = float(np.mean(inside)) - noise
    snr = float(10 * np.log10(signal / noise)) if signal > 0 else None
    return snr, signal, noise, quiet


NOISE_QUANTILE = 0.25   # burst_snr's quantile of the rows around a burst: robust while fewer than
                        # 3 rows in 4 carry anything but noise


@functools.lru_cache(maxsize=1024)
def noise_quantile(L: int, n: int, q: float) -> float:
    """What the ``q`` quantile of ``n`` levels of noise alone reads, over the noise, each level a
    mean of ``L`` exponential powers (a row's ``L`` coordinates, or ``L`` complex samples):
    Gamma(L, 1/L).  The sample quantile of a few levels sits off the distribution's (the 0.25
    quantile of two rows of 64: +5%), so it is computed for the count in hand -- a fixed draw,
    the same every call."""
    if n >= 512:
        from scipy.stats import gamma
        return float(gamma.ppf(float(q), int(L), scale=1.0 / int(L)))
    g = np.random.default_rng(0).gamma(int(L), 1.0 / int(L), size=(4096, max(int(n), 1)))
    return float(np.mean(np.percentile(g, 100 * float(q), axis=1)))


@dataclass(frozen=True)
class Known:
    """A geometry this receiver measured before: a sender's width, scale and band centre (cycles
    per received sample).  Clock ratios and chosen centres persist, so a sender heard once is read
    next time at its known scale -- measured state, never pre-shared."""
    L: int
    scale: float
    center: float


SETTLE_XTOL = 1e-4      # start (samples) and carrier (cycles/row) settle tolerance of the known-geometry read
START_STEP = 0.05       # samples: the start's second settle, at the payload-measured carrier, reads a
                        # parabola at +-this ...
START_SPAN = 0.25       # ... and moves at most this from the first (measured off by up to ~0.05)
SCAN_ROWS = 8           # the known-geometry scan reads the rows the capture holds, at least this many
SCAN_ROWS_MAX = 16      # ... and at most this many.  On the constant-modulus read a dense QAM row is
                        # weak evidence (256-QAM reads ~0.5 of the null at the true start, a PSK row 0),
                        # and a capture holds a few rows of silence ahead of its burst, so the dark-mode
                        # scan's eight rows lose starts: measured (32 bursts, lab rank_leak/adopted/mesh/
                        # scan_cost.py), matched 256-QAM 3/4 at 28 dB delivered 27 frames on 8 rows, 29
                        # on 16 (29 on 16 rows of all 64 modes, at 2.6x the CPU); matched 2/3 at 30 dB
                        # 31 on 16 rows (32 on all modes).  Read CPU ~100 ms a burst at 16 rows.
SCAN_KEEP = 3           # the scan's best starts per carrier probe, ranked again on every mode of every
                        # row (receive_known): on 16 modes a dense QAM burst of 12-14 rows can rank a
                        # few places down among ~3000 starts and carriers (measured, 256-QAM 2/3 at
                        # 1.0 -> 2.4 Msps: the true start read 0.82 of the null where a chance one read
                        # 0.77; on all 64 modes it stands far clear)
RANK_ROWS = 32          # rows each shortlisted start is ranked on (all of them in a shorter capture)
START_REACH = 8         # rows from the read's first in which the burst's first row is sought
REF_ROWS = 1            # rows the start/carrier re-read takes of a coherent burst: its keyed reference row
RESETTLE_START = True   # re-settle the start on that re-read after the carrier (False: keep the settle's)
CARRIER_REACH = 0.2     # cycles/row: carrier branches further than this from the settle's are not tried
TRACK_WINDOW = 3.0      # default half-window (received samples) of the LOCAL scan when a sender is
                        # tracked (``receive_known(tracked=...)``): the quarter-sample grid is
                        # searched this far either side of the previous burst's locked start phase.
                        # A stable clock holds a tracked sender's start phase steady across bursts, so
                        # a few samples cover the slot-to-slot jitter and the settle's half sample; a
                        # wider prior drift falls back to the full scan on the edge check.  See
                        # research/realtime/port_results.md (§3a) for the measured window-vs-hit-rate.
SETTLE_FTOL = 1e-2      # relative score tolerance of the same settle.  Measured on the dark read (60
                        # bursts at 5/6 dB, 1.0 -> 2.4 Msps): 1e-9 and 1e-2 decode the same bursts (25/30,
                        # 29/30) at 333 vs 164-206 ms; 1.0 starts to lose them (24/30); no settle at all,
                        # 10/30 and 16/30.

# The known-geometry settle of (start, carrier) is, by the profile, ~24% of a canonical burst on a
# Pi (202 ms) -- scipy's bounded Powell runs the exact row read a few hundred times to converge its
# two coordinates to SETTLE_XTOL.  But the objective is a bowl in each coordinate (the row read is
# continuous in the start, ``waveform._row_window``, and in the carrier branch it rides), so its
# floor is placed by the same hand-rolled parabola (:func:`_bowl`) the coherent re-read already uses
# -- a fixed handful of reads per coordinate instead of Powell's hundreds.  Phase 3 of
# research/realtime/PLAN.md.  The parabola does not reproduce Powell's exact floats, so the Powell
# path is kept as the oracle (and the fallback) behind the flag; what is gated is that the parabola
# finds a lock that decodes the SAME frame on the fixed burst corpus (the CRC gives the margin).
SETTLE_PARABOLA_START_STEP = 0.1    # samples: the start parabola reads a bowl at +-this around the
                                    # scan's quarter-sample pick (true start within ~0.125 of it)
SETTLE_PARABOLA_CARRIER_STEP = 0.05  # cycles/row: the carrier parabola, half the 0.1 scan-probe pitch
SETTLE_PARABOLA_START_SPAN = 0.5    # the start may move at most this (Powell's bound)
SETTLE_PARABOLA_CARRIER_SPAN = 0.1  # ... and the carrier this (Powell's bound)
SETTLE_PARABOLA_ROUNDS = 2          # alternating (start, carrier) passes; <=4 reads a coordinate/pass


def _use_parabola_settle() -> bool:
    """Whether the hand-rolled parabola settle replaces bounded Powell on the known-geometry read.

    Enabled by default (the Phase-3 production direction); ``ENTROPTICS_PARABOLA_SETTLE=0`` (or
    off/false/no) forces the scipy Powell oracle, so the reference the parabola is gated against
    stays runnable.  Independent of ``ENTROPTICS_FAST`` (the compiled-kernel flag) -- this is an
    algorithmic change in pure numpy, not a numba kernel."""
    return os.environ.get("ENTROPTICS_PARABOLA_SETTLE", "").strip().lower() not in (
        "0", "off", "false", "no")


def _coarse_rows() -> int | None:
    """§3b lever: how many rows the per-burst GEOMETRY estimate (the rank score and the settle
    objective) reads, at most.  Unset (the default) reads every row the capture holds, which is
    byte-identical to before.

    The geometry is only three parameters -- scale (known here), start and carrier -- and is
    heavily over-determined, so a subset of the burst's rows pins (start, carrier) as precisely as
    all of them, at a proportionally cheaper dense-LA read per settle/rank evaluation (the settle
    runs the read a dozen times, so it is the dominant read cost on a long burst).  The PAYLOAD
    read (:func:`waveform._scaled_rows`) stays on ALL rows -- the symbols need the full data; only
    the geometry fit is decimated, which is decimation of the FIT, not of the signal (no
    resampling).  ``ENTROPTICS_COARSE_ROWS=N`` caps the geometry read at N rows (floor 2); a
    coarse estimate that is off its full-row value only costs the CRC-gated full re-scan the
    public wrapper already runs, never a wrong lock.  See research/realtime/port_results.md (§3b)
    for the smallest N that stays 0-flip on the corpus."""
    v = os.environ.get("ENTROPTICS_COARSE_ROWS", "").strip()
    if not v:
        return None
    try:
        n = int(v)
    except ValueError:
        return None
    return n if n >= 2 else None


def _first_readable(rho: float) -> float:
    """The least real start whose first row the capture holds whole at scale ``rho``: the row read
    takes the samples from half a transmitted sample before the start (``waveform._row_window``),
    and the first of them must be the capture's first or later."""
    return 0.5 / float(rho) - 1.0 + 1e-6


def _run(R, reach=None):
    """Where a burst sits among rows read one by one: ``(stat, a, b)`` over the leading axes of the
    per-row constant-modulus reads ``R`` (..., rows) -- the run of rows ``a .. b-1`` (``a < reach``
    when given) that reads most firmly below the null.

    A row of noise reads 1 on average (``waveform._cm_rows`` is scaled to its null) and a row of the
    burst reads below it, by an amount the alphabet and the SNR set and nothing here knows.  The
    deviation below the null, ``1 - R``, is the profile whose best positive-sum run the generic scan
    statistic locates -- ``entroptics.reads.level_shift_scan``, the classic scan for a level shift of
    unknown size AND location, with no threshold and no level to give.  Rows the capture does not
    hold (NaN) count as nothing; ``stat`` is NaN where no row is held."""
    return level_shift_scan(1.0 - np.asarray(R, dtype=float), reach=reach)


def _run_score(R):
    """The burst's run (:func:`_run`) as a score to minimize: ``reduce`` for
    ``waveform._scaled_cm``.  A mean over every row would weigh the rows of noise around a short
    burst as much as the burst's own, and a message's short final row (lit on a few modes, it reads
    up to 1.48 of the null) as much as five rows of noise -- measured, a 14-row 256-QAM burst read
    on 16 rows lost its start to a chance one that way."""
    return -_run(R)[0]


def _run_mean(R) -> float:
    """The mean read over the burst's run of rows (:func:`_run`): ~0 locked noiseless, ~1 on noise
    -- the discovery's score, on the scale ``waveform.Discovery.score`` describes."""
    stat, a, b = _run(R)
    if not np.isfinite(stat) or b <= a:
        return 1.0
    return float(np.mean(np.asarray(R, dtype=float)[int(a):int(b)]))


def _onset(cm, reach: int) -> int:
    """The burst's first row among the capture's rows: where its run (:func:`_run`) begins, within
    ``reach`` rows of the first.

    The differential closure placed it before, and still offers its own row as the second
    candidate.  But the closure reads |mean u^M| on the de-keyed differential, and at 16-PSK and
    8-PSK near their thresholds that M-th power sits at its own noise (|mean u^16| ~0.04-0.06 at
    the true row of a D16PSK 1/2 burst at 16.6 dB, 0.014-0.045 at the wrong ones), so the row was
    a guess: measured, 4 of 7 D16PSK 1/2 losses at 16.6 dB and all 6 D8PSK 1/2 losses at 10.6 dB
    started 1-7 rows off.  Each row's constant-modulus read has 64 modes behind it and needs no
    alphabet: a PSK row reads ~0.05 of the null there, a QAM row ~0.5, the keyed reference row
    ~0.  The onset placed every one of those bursts."""
    c = np.asarray(cm, dtype=float)
    if c.size < 2:
        return 0
    return int(_run(c, reach)[1])


def _soft_from_rows(A, r: int, K: int, key, carrier: float) -> np.ndarray:
    """The de-keyed differential of rows already read (all modes), from row ``r`` as the burst's
    first: what ``waveform.read_soft`` computes, without reading the rows again."""
    A = A[int(r):, :int(K)]
    if A.shape[0] < 2:
        return np.zeros((0, int(K)), dtype=complex)
    d = A[1:] * np.conj(A[:-1])
    d = d * _unkeys(int(K), key, d.shape[0])
    return d * np.exp(-2j * np.pi * float(carrier))


def _memo(fn):
    """``fn`` of a tuple of floats, each value computed once (a settle reads its end points again)."""
    seen = {}

    def g(z):
        k = tuple(float(v) for v in z)
        if k not in seen:
            seen[k] = fn(k)
        return seen[k]
    return g


def _bowl(fn, x0: float, h: float, span: float) -> float:
    """Where a bowl-shaped ``fn`` is least near ``x0``: the parabola through ``x0`` and ``x0 +- h``,
    and its vertex (within ``span`` of ``x0``); the least of the four reads wins.  Measured: the
    same 256-QAM 3/4 deliveries at 28 dB, 1.0 and 1.2 -> 2.4 Msps (16/16, 15/16) as a bounded Brent
    search to 5e-3 samples (~8 reads) and as a second parabola at half the step."""
    pts = {}

    def at(x):
        x = float(np.clip(x, x0 - span, x0 + span))
        if x not in pts:
            pts[x] = fn(x)
        return pts[x]

    a, b, c = at(x0 - h), at(x0), at(x0 + h)
    curv = a - 2 * b + c
    if np.isfinite(curv) and curv > 0:
        at(x0 + 0.5 * h * (a - c) / curv)
    return min(pts, key=pts.get)


def _settle_parabola(f, s0: float, c0: float) -> tuple[float, float]:
    """Settle ``(start, carrier)`` on the bowl objective ``f`` by alternating single-coordinate
    parabolas (:func:`_bowl`), in place of scipy's bounded Powell.

    Each coordinate's floor is placed by three reads and a vertex (:func:`_bowl`), a couple of
    rounds alternating between them -- a fixed handful of reads against Powell's hundreds.  The
    start bowl's x0 on the first round is the scan's estimate, so ``f((s0, c0))`` is among the reads
    and the result never scores worse than where it started (the row read is monotone in the moves a
    convex bowl makes), the same "kept only if it improves" guarantee Powell's caller enforced.  The
    two coordinates are read on the same rows, so the carrier is re-placed after the start moves it
    and vice versa, exactly as the coherent re-read's :func:`refine` does."""
    s, c = float(s0), float(c0)
    for _ in range(SETTLE_PARABOLA_ROUNDS):
        s = _bowl(lambda z: f((z, c)), s, SETTLE_PARABOLA_START_STEP, SETTLE_PARABOLA_START_SPAN)
        c = _bowl(lambda z: f((s, z)), c, SETTLE_PARABOLA_CARRIER_STEP, SETTLE_PARABOLA_CARRIER_SPAN)
    return s, c


def receive_known(iq, key, known: Known, *, carrier_span: float = 0.2,
                  accept=None, tracked=None) -> RxBurst:
    """Read a burst at a known scale and centre (see :func:`_receive_known`).

    ``tracked`` is the §3a lever: a prior ``(start_phase, carrier[, window])`` from a previous
    burst's lock of this sender restricts the scan to a local search around it.  A correct prior is
    a pure speed-up (byte-identical to the full scan -- the surviving candidate scores are an exact
    subset).  A STALE prior (the true start drifted past the window) can land the local scan on a
    chance interior minimum, so when a frame check is given and the tracked read's frame does not
    pass it, the read is redone with the full scan -- the exact CRC is the confidence gate, so a
    tracked read never loses a burst the full scan would have caught; a stale prior costs only a
    re-scan."""
    rx = _receive_known(iq, key, known, carrier_span=carrier_span, accept=accept, tracked=tracked)
    if tracked is not None and accept is not None and not accept(rx):
        rx = _receive_known(iq, key, known, carrier_span=carrier_span, accept=accept, tracked=None)
    return rx


def _receive_known(iq, key, known: Known, *, carrier_span: float = 0.2,
                   accept=None, tracked=None) -> RxBurst:
    """Read a burst at a known scale and centre: one constant-modulus scan per carrier probe, a
    short settle of start and carrier, then both reads on the rows read once -- the differential
    from the burst's first row, the coherent from the keyed reference row.  Every mode is lit, so
    the mode count is the width.  Whether it worked is for the caller's integrity check (the frame
    CRC) to say -- an exact test, so this read needs no threshold of its own.

    Where the burst sits among the capture's rows is read the same way at every step: each row's
    constant-modulus read, and the run of rows that reads most firmly below the null (:func:`_run`).
    The scan and the settle score a geometry by that run, not by the mean over every row they read,
    so rows of noise around a short burst -- a node's slot holds the longest frame at the slowest
    rate -- do not dilute it; the run's first row is where the burst begins (:func:`_onset`).

    ``accept(rx)`` -- the frame check -- makes the read stop at the first frame that passes: the
    burst as the settle left it is offered first, from each candidate first row (the run's, then
    the differential closure's when it names another), and the coherent read's start and carrier
    are measured again (below) only when none passes.  In open mode the frame check also places
    the burst's first row among the rest, which the differential closure cannot (below).

    ``tracked`` -- a prior ``(start_phase, carrier[, window])`` from a previous burst's lock of the
    same sender (its start phase in ``[0, L/scale)`` and carrier in cycles/row) -- restricts the scan
    to a LOCAL search around that prior instead of re-searching the whole row every burst (the §3a
    lever).  Absent (the default) the scan is the full one, unchanged.  The local read keeps only the
    full grid's positions near the prior, so each surviving candidate's score is bit-for-bit the full
    scan's; it falls back to the full scan here only when the window kept no readable start, and the
    public wrapper re-scans in full when the local lock's frame fails its check (:func:`receive_known`
    -- the CRC is the confidence gate).  So a correct prior is a pure speed-up and a stale one costs
    only a re-scan, never a wrong lock."""
    from scipy.optimize import minimize
    iq = np.asarray(iq)
    L, rho = int(known.L), float(known.scale)
    ell = L / rho
    nu0 = known.center * L / rho
    scan_rows = max(SCAN_ROWS, min(SCAN_ROWS_MAX, int(iq.size * rho // L) - 2))
    carriers = nu0 + np.arange(-carrier_span, carrier_span + 1e-9, 0.1)
    found = None
    if tracked is not None:
        s_prior = float(tracked[0])
        win = float(tracked[2]) if len(tracked) > 2 else TRACK_WINDOW
        loc = [x for x in wf._scaled_scan(iq, L, key, None, rho, scan_rows, carriers,
                                          keep=SCAN_KEEP, start0=s_prior % ell, start_win=win)
               if np.isfinite(x[0])]
        # The local scan is used when the window kept a readable start.  A stale prior (the true
        # start drifted out of the window) can still leave a chance minimum inside it; that is caught
        # by the public wrapper's frame check, which re-scans in full -- so no window-edge heuristic
        # is needed here, only the exact CRC to decide the local lock was wrong.
        if loc:
            found = loc
    if found is None:
        found = [x for x in wf._scaled_scan(iq, L, key, None, rho, scan_rows, carriers,
                                            keep=SCAN_KEEP) if np.isfinite(x[0])]
    if not found:
        return RxBurst(wf.Discovery(None, 0, 0, 0, float("inf"), 1.0, 0.0), _EMPTY)
    # The scan places the row grid anywhere in the capture's first row.  A start whose first row
    # begins before the capture does is not held whole, and ``waveform._scaled_rows`` returns the
    # rows it holds -- without that one, so every row index after it would be one short (measured:
    # a burst beginning a row into a 1.0 -> 2.4 Msps capture had its reference read on the row of
    # noise before it).  The grid moves a row on instead, with room for the settle's half sample.
    lo = _first_readable(rho)
    held = lambda s: max(2, int((iq.size - s - 2) // ell) - 1)

    def objective(s, T):
        return _memo(lambda z: wf._scaled_cm(iq, L, key, None, z[0], rho, z[1], T, reduce=_run_score))
    # The scan's candidates ranked on every mode, by the burst's run (:func:`_run`) -- the settle's
    # own objective, where a dense QAM row is firm evidence -- each distinct start once, at the
    # carrier probe it scanned best on, over RANK_ROWS rows: room for a start within the reach and
    # a run long enough to stand clear of chance.
    cands = {}
    for v, s, cc in sorted(found):
        s = s + ell if s - 0.5 < lo else s
        if all(abs(s - t) >= 0.5 for t in cands):
            cands[s] = cc
    # The geometry (start, carrier) is over-determined, so the rank score and the settle objective
    # may read a coarse subset of the rows (:func:`_coarse_rows`); default (None) reads all of them
    # -- byte-identical to before.  The payload read below stays on every row regardless.
    coarse = _coarse_rows()
    rank_cap = RANK_ROWS if coarse is None else min(RANK_ROWS, coarse)
    _, s0, c = min((objective(s, min(held(s), rank_cap))((s, cc)), s, cc) for s, cc in cands.items())
    T = held(s0)
    T_geom = T if coarse is None else min(T, coarse)
    f = objective(s0, T_geom)
    if _use_parabola_settle():
        # Phase 3: the bowl objective's floor placed by a hand-rolled parabola (:func:`_settle_parabola`)
        # -- a few reads a coordinate rather than Powell's hundreds.  Gated decode-identical against
        # the Powell oracle on the burst corpus (research/realtime/port_results.md).
        s0, c = _settle_parabola(f, s0, c)
    else:
        try:
            r = minimize(f, [s0, c], method="Powell", bounds=[(s0 - 0.5, s0 + 0.5), (c - 0.1, c + 0.1)],
                         options={"xtol": SETTLE_XTOL, "ftol": SETTLE_FTOL})
            s0, c = (r.x if f(r.x) <= f((s0, c)) else (s0, c))
        except ValueError:      # a flat or non-finite objective leaves bounded Powell no direction;
            pass                # keep the scan's estimate -- the frame CRC decides either way
    s0, c = float(s0), float(c)
    off = int(np.ceil(s0))
    rows = wf._scaled_rows(iq, L, key, None, off, off - s0, rho, c)     # all modes
    score = lambda z: wf._scaled_cm(iq, L, key, None, z[0], rho, z[1], T_geom, reduce=_run_mean)
    d = wf.Discovery(L, off, L, 4, float(score((s0, c))), 1.0, 0.0, float(off - s0), c, scale=rho)
    keyed = key is not None
    scores = _start_scores(lambda KK: rows[:, :KK], (L,), key, c)
    power = rows.real ** 2 + rows.imag ** 2
    first = _onset(wf._cm_rows(power), START_REACH) if rows.shape[0] else 0
    by_closure = _best(scores)
    starts = [first] + ([by_closure[1]] if by_closure and by_closure[1] != first else [])
    Y0, nu, ref0 = _coherent_rows(rows, L, key, c, L)
    coh0 = _coherent_discovery(d, Y0, nu, ref0, L)

    def burst(rr, Y=None, coh=None):
        """The burst read from row ``rr`` as its first: the differential at the settled geometry
        (each hypothesis reads its own residual turn, :meth:`RxBurst.soft_bits`), and the coherent
        read ``Y`` riding along when given."""
        st = s0 + rr * ell
        o = int(np.ceil(st))
        cl, M = scores.get((L, rr), (0.0, 4))
        dd = wf.replace(d, K=L, M=int(M), offset=o, phase=o - st, closure=float(cl))
        soft = _soft_from_rows(rows, rr, L, key, c)
        return (RxBurst(dd, soft, keyed=keyed, power=power, first=int(rr)) if Y is None
                else RxBurst(dd, soft, Y, coh, keyed=keyed, power=power, first=int(rr)))

    probe = burst(starts[0], Y0, coh0)
    burst_here = (probe.rows("differential") is not None or probe.rows("coherent") is not None
                  or (rows.shape[0] > 1 and int(_run(wf._cm_rows(rows.real ** 2 + rows.imag ** 2))[2])
                      >= rows.shape[0] - 2))
    # Neither read finds a burst's length, and the rows' run does not reach the capture's end
    # (nothing burst-shaped at this geometry: a known sender that did not transmit, or moved): the
    # coarse read stands, and nothing is spent refining it.  A burst the capture ends with gives no
    # length but is a burst all the same; its run stops up to two rows short of the end (the
    # final row is often short and reads high, and the row before it carries its edge -- measured,
    # a 256-QAM 3/4 burst at 50 dB whose refine this skipped read at raw BER 0.07).
    if not burst_here:
        return probe

    # The first frame that passes ends the read: the burst as the settle left it, offered before
    # anything is spent refining it (at the SNRs the fast path runs, most bursts pass here).  The
    # coherent read does not depend on the differential's first row, so it rides with the first
    # offer only.
    tried = set()
    if accept is not None:
        for j, rr in enumerate(starts):
            tried.add(rr)
            early = probe if j == 0 else burst(rr)
            if accept(early):
                return early

    # The coherent read's start and carrier, measured again on its keyed reference row.  The
    # settle leaves dense QAM a floor: the carrier up to ~0.04 cycles/row off (it stops on its
    # tolerance near a scan probe) and the start a few hundredths of a sample off (measured,
    # 256-QAM at 1.0 -> 2.4 Msps: N0 -34 dB at the true start, -31 at 0.02 sample off, -26 at 0.05,
    # -24 at 0.1).  The reference row is unit modulus on every mode by construction, so its
    # constant-modulus read (``waveform._cm_rows``) falls to its least at the true geometry, where
    # only the noise is left in it: a geometry error mixes each coordinate with its neighbours',
    # which spreads the row's energy unevenly over the modes.  The reference row and not the
    # payload: a QAM row's own levels already read 0.44-0.52 of the null (16- to 256-QAM,
    # noiseless), and measured on a 256-QAM burst its read moved 0.731 -> 0.757 between a start 0
    # and 0.1 sample off, where the reference row is a bowl from its noise floor (on more rows
    # matched 256-QAM 3/4 at 28 dB fell from 29 to 13-17 of 32 bursts).  The carrier: the fourth
    # power measures the row turn modulo a quarter cycle per row; among those branches it is the
    # one that reads best -- a reading of the column half, which the row turn's symmetry does not
    # touch.  Then the start, alone at that carrier; then the rows are read once more, there.
    #
    # A differential burst is not read again: the settle places it within ~0.01 cycles/row and a
    # few hundredths of a sample on every row it has (measured p90 0.003-0.007 cycles/row, D8PSK
    # and D16PSK near threshold), and each alphabet hypothesis reads its own residual turn off its
    # own symbols (:meth:`RxBurst.soft_bits`), so no carrier branch is chosen for it here.
    def spread_on(row, nread):
        @_memo
        def g(z):
            st = z[0] + row * ell
            if st < lo:             # its first row is not held whole: nothing to read there
                return float(wf._cm_missing(L))
            o = int(np.ceil(st))
            A = wf._scaled_rows(iq, L, key, None, o, o - st, rho, z[1], nread)
            return (float(np.mean(wf._cm_rows(A.real ** 2 + A.imag ** 2))) if A.shape[0]
                    else float(wf._cm_missing(L)))
        return g

    def refine(spread, cs, s, cc):
        """The carrier among a read's own branches, then the start, alternately (below)."""
        # The branches within CARRIER_REACH of the settled carrier: the settle stays within 0.1
        # of its scan probe, and the probes are 0.1 apart, so a branch further off is not the
        # carrier (each read already lands on the branch nearest the settle's).
        cs = [v for v in cs if abs(v - cc) <= CARRIER_REACH] or cs
        cc = min(cs, key=lambda v: spread((s, v)))
        if RESETTLE_START:
            # At every scale: the read is continuous in the start (``waveform._row_window``), at or
            # near scale 1 too, where the row read stands in for the joint read.  The read is a
            # bowl around the true start, so a parabola through three reads places its floor; the
            # least read is kept.  The start and the carrier are read on the same rows, and a start
            # a tenth of a sample off can make a wrong carrier branch read better (measured on an
            # open 256-QAM burst: picked at the settle's start the carrier went to -0.040 where the
            # truth was ~0, residual -18 dB against -28): so the carrier is picked again at the
            # settled start, and the start settled again if that moved it.
            s = _bowl(lambda z: spread((z, cc)), s, START_STEP, START_SPAN)
            c2 = min(cs, key=lambda v: spread((s, v)))
            if c2 != cc:
                cc = c2
                s = _bowl(lambda z: spread((z, cc)), s, START_STEP, START_SPAN)
        return s, cc

    sc, cc = refine(spread_on(ref0, REF_ROWS), [nu + j / 4 for j in (-1, 0, 1)], s0, c)
    oc = int(np.ceil(sc))
    Y, nu, ref = _coherent_rows(wf._scaled_rows(iq, L, key, None, oc, oc - sc, rho, cc), L, key, cc, L)
    coh = _coherent_discovery(wf.replace(d, offset=oc, phase=float(oc - sc)), Y, nu, ref, L)

    if not keyed and accept is not None:
        # No per-row keys in the public state, so a start a row late closes as well as the true
        # one and the closure cannot place the burst.  The frame's own integrity check can: try
        # the starts the run and the closure did not name, in closure order, and keep the first
        # whose frame passes.  The refined coherent read rides with the first offer only -- and
        # the later starts are tried only while the differential read is the better-closing one.
        out = burst(starts[0], Y, coh)
        if accept(out) or coh.closure > out.discovery.closure:
            return out
        order = sorted((r for (k, r) in scores if k == L and r not in tried),
                       key=lambda r: -scores[(L, r)][0])
        for rr in order:
            cand = burst(rr)
            if accept(cand):
                return RxBurst(cand.discovery, cand.soft, Y, coh, cand.cache, keyed=False,
                               power=cand.power, first=cand.first)
        return out
    return burst(starts[0], Y, coh)


def receive_burst(iq, key, *, candidates=None) -> RxBurst:
    """Measure a burst from the capture and the key alone and return both reads of it.

    Read as captured, and -- where the reference row sits later in the capture -- again from the
    reference row; the read that closes better is kept (discover's own rule for choosing between
    candidates: compare them on the evidence, never accept one by a cut)."""
    iq = np.asarray(iq)
    bracket = wf._coarse_scale(iq)
    narrow = bracket is not None and bracket[1] < 1.0     # the occupancy read says narrower
    first = _read(iq, key, candidates)
    if narrow:
        # Occupancy is a clue, not proof: a waveform that is not spectrally white (the public
        # zero-state aperture measured 0.45-0.52 occupied at full width) reads narrow too.  So the
        # equal-rate read runs as well, and the read that closes better is kept.
        scaled = _scaled_burst(iq, key, candidates)
        if not (first.locked and first.closure >= scaled.closure):
            return scaled
    if not first.locked:
        return first
    r = _burst_start(iq, first.discovery, key)
    if r == 0:
        return _with_power(first, iq, key, 0)
    tail = iq[first.discovery.offset + r * first.discovery.L:]
    second = _read(tail, key, candidates)
    return (_with_power(second, tail, key, 0) if second.locked and second.closure > first.closure
            else _with_power(first, iq, key, r))


def sender_of(iq, burst: RxBurst, key, candidates) -> bytes | None:
    """Which of ``candidates`` marked this burst, against the read's own permutation null; None if
    none resolves.  Equal-rate differential bursts only for now (see the module note)."""
    d = burst.discovery
    if not d.locked or d.scale != 1.0 or not candidates:
        return None
    sig = wf.sender_signature(iq, d.L, d.K, key, offset=d.offset, phase=d.phase, carrier=d.carrier)
    name, _ = wf.sender_resolve(sig, list(candidates))
    return name


__all__ = ["COHERENT_M", "DIFFERENTIAL_M", "FAMILIES", "Known", "capacity_bits", "rows_for",
           "transmit_burst", "RxBurst", "burst_snr", "receive_burst", "receive_known", "sender_of"]
