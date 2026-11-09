"""
adapt.py -- the core's default plan for a transmission: width, centre, power and alphabet, fitted
to the audience the node has heard.  An agility provider gets the last word (``providers``).

The rules, each from a measurement or a stated design decision:

  * Heard information is a clue, never a constraint.  Hearing a neighbour narrowband never forces
    narrowband; it bounds nothing but what that neighbour said it can capture.
  * Never mirror a heard band.  A relay forwards on a different sub-band from the one it heard the
    message on -- measured on a 9 km-hop chain: 8/8 delivered on a separate sub-band against 6/8 on
    the same one -- and the choice among the others varies, so no fixed pattern is added to the air.
  * Narrow when the margin is thin, wide when it is not.  At equal power, halving the width raises
    the in-band SNR by ~3 dB (measured: 1.0 Msps senders into 2.4 Msps receivers decoded the origin
    directly where 2.4 Msps senders could not).
  * Power for the hop, not for the world: aim the intended receiver at ``HOP_SNR_DB`` (~9 dB gave
    >= 99% per-burst delivery in the emulation) plus a margin, from a path-loss estimate when
    positions are known; full power when nothing is known ("shout").
  * The modulation family, alphabet and code rate come from the measured goodput envelope of the
    families the node may send (``research/mcs/kL.md``: both families through the mesh's own
    receive path): the highest-goodput mode whose 1%-PER threshold the predicted SNR clears.  The
    plan carries all three (``TxPlan.family``, ``M``, ``code_rate``); nothing about them is sent or
    agreed -- the receiver reads both families and the frame's CRC picks (``link.read_frame``).

Nothing here needs a reply from anyone.  With an empty neighbour table it returns the shout plan.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, replace

import numpy as np

from .propagation import fspl_db
from .neighbors import NeighborTable, Position
from .providers import Intent, TxPlan

HOP_SNR_DB = 9.0          # per-hop target: >= 99% of bursts decoded in the emulation at 9 dB
MARGIN_DB = 3.0           # over the target, for fading and position error
FAMILIES = ("differential", "coherent")

@dataclass(frozen=True)
class Mode:
    """One modulation and coding scheme as measured: the in-band SNR (Es/N0 per transmitted complex
    sample) from which PER <= 1% for one mesh frame, the family and alphabet, the code rate, and
    the goodput it then delivers (frame bits per complex sample of the burst, reference row,
    framing, CRC and code rate included; K = L = 64)."""
    threshold_db: float
    family: str            # "differential" (M-PSK) or "coherent" (square M-QAM)
    M: int
    rate: str              # convolutional code rate, fec.encode_packet's ``rate``
    goodput: float


# Every mode measured through the chain the mesh runs (research/mcs/kL.md, results_kL_fp.json;
# script mcs_kl.py): link.frame (the 512-byte protected frame) -> phy.transmit_burst (every mode
# lit, K = L = 64, keyed per-row power) -> the emulated air -> phy.receive_known (the fast path:
# constant-modulus geometry read with the burst's run of rows, one coherent gain for every mode,
# ``_row_turn``, each differential alphabet's own turn) -> link.read_frame (the CRC picks family,
# alphabet and code rate; nothing passed).  Each threshold is the 1%-PER point of a binomial fit to
# the waterfall (90% interval ~+-0.1-0.2 dB), the worse of a matched 2.4 Msps link and a 1.0 Msps
# sender into a 2.4 Msps receiver, since the plan does not know the receiver's rate.  Goodput =
# 4096 frame bits / burst samples at PER 0.  Differential bursts are Gray-labelled
# (phy.transmit_burst).  The geometry read no longer sets any threshold (0-17% of losses near
# threshold read with raw BER > 0.2, against up to 96% before); the previous table, where it set
# D16PSK 1/2, 4-QAM and dense QAM at 1.0 -> 2.4, is kL.md's first section (results_kL.json).
MODES = [
    Mode(4.0, "differential", 2, "1/2", 0.489),
    Mode(5.6, "differential", 2, "2/3", 0.646),
    Mode(6.4, "differential", 2, "3/4", 0.727),
    Mode(7.2, "differential", 4, "1/2", 0.97),
    Mode(9.0, "differential", 4, "2/3", 1.28),
    Mode(10.1, "differential", 4, "3/4", 1.422),
    Mode(11.3, "differential", 8, "1/2", 1.422),
    Mode(13.7, "differential", 8, "2/3", 1.882),
    Mode(15.1, "differential", 8, "3/4", 2.133),
    Mode(15.9, "differential", 16, "1/2", 1.882),
    Mode(19.0, "differential", 16, "2/3", 2.462),
    Mode(20.7, "differential", 16, "3/4", 2.783),
    Mode(4.4, "coherent", 4, "1/2", 0.97),
    Mode(6.5, "coherent", 4, "2/3", 1.28),
    Mode(7.7, "coherent", 4, "3/4", 1.422),
    Mode(10.2, "coherent", 16, "1/2", 1.882),
    Mode(12.8, "coherent", 16, "2/3", 2.462),
    Mode(14.4, "coherent", 16, "3/4", 2.783),
    Mode(16.0, "coherent", 64, "1/2", 2.783),
    Mode(18.4, "coherent", 64, "2/3", 3.556),
    Mode(20.0, "coherent", 64, "3/4", 4.0),
    Mode(19.3, "coherent", 256, "1/2", 3.556),
    Mode(24.1, "coherent", 256, "2/3", 4.571),
    Mode(26.3, "coherent", 256, "3/4", 5.333),
]


def _envelope(modes) -> list:
    """The modes no other beats on both threshold and goodput, in rising goodput."""
    out = []
    for m in sorted(modes, key=lambda m: (m.threshold_db, -m.goodput)):
        if not out or m.goodput > out[-1].goodput:
            out.append(m)
    return out


# The measured envelope over both families: every mode no other mode beats on both threshold and
# goodput, in rising goodput -- DBPSK 1/2, then coherent only: 4-QAM 1/2-3/4, 16-QAM 1/2-3/4,
# 64-QAM 2/3 and 3/4, 256-QAM 2/3 and 3/4.  Coherent QAM sits ~2.8 dB under the differential mode
# of the same goodput (4-QAM 1/2 4.4 dB against DQPSK 1/2 7.2), the textbook coherent/differential
# gap.  Dominated and so absent: every differential mode above DBPSK 1/2, 64-QAM 1/2 (= 16-QAM 3/4's
# goodput), 256-QAM 1/2 (= 64-QAM 2/3's).  A node that may send one family only plans on that
# family's own envelope (``_mcs_for``): differential alone -- DBPSK 1/2-3/4, DQPSK 1/2-3/4, D8PSK
# 2/3 and 3/4, D16PSK 2/3 and 3/4 -- tops out at D16PSK 3/4 (2.783 at 20.7 dB); coherent alone
# starts at 4-QAM 1/2 (4.4 dB), below which it has nothing that clears.
MCS = _envelope(MODES)


@dataclass(frozen=True)
class Radio:
    """What this node's own radio can do: its rate range, centre, and power range."""
    min_rate: float
    max_rate: float
    center_hz: float
    max_power_dbm: float
    min_power_dbm: float = -40.0
    noise_dbm: float = -100.0          # over max_rate
    freq_hz: float = 915e6
    antenna_dbi: float = 0.0           # this node's antenna gain; planning assumes a peer's matches
                                       # unless told otherwise (``peer_dbi``)


def _snr_at(radio: Radio, power_dbm: float, distance_m: float, rate: float,
            peer_dbi: float | None = None) -> float:
    """Predicted in-band SNR at a receiver ``distance_m`` away: power plus both antenna gains minus
    free-space loss, against noise scaled to the occupied width."""
    noise = radio.noise_dbm + 10 * math.log10(rate / radio.max_rate)
    gain = radio.antenna_dbi + (radio.antenna_dbi if peer_dbi is None else peer_dbi)
    return power_dbm + gain - fspl_db(distance_m, radio.freq_hz) - noise


def _mcs_for(snr_db: float, families=FAMILIES) -> Mode:
    """The highest-goodput mode of an allowed family whose threshold ``snr_db`` clears (the lower
    threshold on a tie); the most robust allowed mode when none does."""
    allowed = _envelope([m for m in MODES if m.family in families]) or MCS
    ok = [m for m in allowed if m.threshold_db <= snr_db]
    return max(ok, key=lambda m: m.goodput) if ok else min(allowed, key=lambda m: m.threshold_db)


def refit(default: TxPlan, plan: TxPlan, families=FAMILIES) -> TxPlan:
    """The mode for ``plan`` after a provider changed ``default``'s width or power.

    The default's mode cleared its threshold, so the receiver's SNR was at least that.  Widening
    by a factor w costs 10 log10 w dB of in-band SNR, and a power change moves it dB for dB; the
    mode falls to the best one that still clears the threshold less that loss.  A change that only
    helps keeps the mode (conservative: the true margin is unknown).  A provider that set its own
    alphabet or code rate is left alone."""
    if (plan.family, plan.M, plan.code_rate) != (default.family, default.M, default.code_rate):
        return plan
    loss = 10 * math.log10(plan.rate / default.rate) + (default.power_db - plan.power_db)
    if loss <= 0:
        return plan
    was = next((m for m in MODES if (m.family, m.M, m.rate) == (default.family, default.M, default.code_rate)), None)
    if was is None:
        return plan
    m = _mcs_for(was.threshold_db - loss, families)
    return replace(plan, family=m.family, M=int(m.M), code_rate=m.rate)


def default_plan(intent: Intent, table: NeighborTable, radio: Radio, *, me: Position | None = None,
                 heard_on: float | None = None, rng=None, families=FAMILIES, avoid=(),
                 interference=()) -> TxPlan:
    """The core's plan for one transmission.

    ``heard_on``  the centre offset (Hz) the message was heard on, when relaying: never reused.
    ``rng``       a numpy Generator for the sub-band choice (varies; never a fixed pattern).
    ``avoid``     other centres (Hz) this message was heard on -- sibling relays forwarding it.
                  Kept clear where the band allows: two bursts on one sub-band in a slot, or on
                  half-overlapping ones, are both lost (measured, 1.2 MHz in 2.4 MHz: disjoint 6/6
                  each, half-overlap 0/6, same band 0/6); disjoint ones are both read.
    ``families``  the modulation families this node may send.  Both by default; ("differential",)
                  where coherent QAM is not yet trusted on the hardware path (on the bench,
                  coherent did not survive equal sender/receiver rates: https://github.com/Agience/entroptics-rf/blob/main/research/coherent_blind.md).
    ``interference`` bands the DESTINATION reported its receiver measures interference on (``neighbors.InterferenceBand``),
                  resolved by the caller to ``(lo, hi)`` ranges in THIS node's centre-offset space
                  (the same space as the chosen ``center``).  A clue: a candidate sub-band whose
                  occupied width overlaps one is dropped, so the burst lands where the destination
                  can still hear it.  Soft, like ``heard_on``/``avoid``: if every sub-band is
                  interfered it is kept anyway (degraded beats silent -- the sender is never cornered).
    """
    rng = rng if rng is not None else np.random.default_rng()
    n = table.get(intent.to) if intent.to is not None else None
    a = n.announced if n is not None else None
    # The widest this audience can take: its announced capture range, else our own maximum.
    cap = min(radio.max_rate, a.band.max_rate) if (a is not None and a.band is not None) else radio.max_rate
    dist = (me.distance_m(a.position) if (me is not None and a is not None and a.position is not None)
            else None)

    if dist is None:
        rate, power = cap, radio.max_power_dbm            # nothing known: shout, full width
        if heard_on is not None and rate / 2 >= radio.min_rate:
            # Relaying with nothing known of the destination: at full width there is one sub-band,
            # the one the message came in on, so the never-the-heard-band rule could not apply.
            # Half the width opens a second and buys ~3 dB of in-band SNR -- reach, when the
            # distance is unknown.
            rate = cap / 2
    else:
        # Narrow until the full-power SNR clears the target, then spend the rest as width.
        rate = cap
        while rate / 2 >= radio.min_rate and \
                _snr_at(radio, radio.max_power_dbm, dist, rate) < HOP_SNR_DB + MARGIN_DB:
            rate /= 2
        need = HOP_SNR_DB + MARGIN_DB - (_snr_at(radio, radio.max_power_dbm, dist, rate) - radio.max_power_dbm)
        power = float(np.clip(need, radio.min_power_dbm, radio.max_power_dbm))

    # Sub-band: the slots of this width across our own band, minus the one the message came in on.
    slots = max(1, int(radio.max_rate // rate))
    centers = (np.arange(slots) - (slots - 1) / 2) * rate
    if heard_on is not None and slots > 1:
        far = np.abs(centers - heard_on) >= rate / 2
        centers = centers[far] if far.any() else centers
    if len(avoid) and slots > 1:
        everything = (np.arange(slots) - (slots - 1) / 2) * rate
        clear = np.all(np.abs(centers[:, None] - np.asarray(list(avoid), float)[None, :]) >= rate / 2, axis=1)
        if clear.any():
            centers = centers[clear]
        else:
            # Every band is taken -- the one it came in on and the siblings'.  Two relays that both
            # fell back to "the one not heard on" would collide every time (measured: 1.2 MHz
            # sub-bands, both relays on the same band in every burst).  So the choice spreads over
            # every band, which breaks the tie between siblings at the cost of sometimes reusing
            # the heard band -- a soft rule, against a certain collision.
            centers = everything
    # A destination-reported band with interference is a clue: drop sub-bands whose occupied width overlaps
    # one, so the burst lands where the destination can still receive.  Soft -- if that would leave
    # nothing, keep what we had (degraded beats silent; the sender is never cornered).
    if len(interference) and len(centers) > 1:
        clear = np.array([all((c - rate / 2) >= hi or (c + rate / 2) <= lo for lo, hi in interference)
                          for c in centers])
        if clear.any():
            centers = centers[clear]
    center = float(rng.choice(centers))

    snr = (_snr_at(radio, power, dist, rate) if dist is not None else None)
    mode = _mcs_for(snr if snr is not None else -math.inf, families)
    plan = TxPlan(rate=float(rate), center=center, power_db=float(power - radio.max_power_dbm),
                  M=int(mode.M), family=mode.family, code_rate=mode.rate)
    return plan, mode.rate


__all__ = ["FAMILIES", "HOP_SNR_DB", "MARGIN_DB", "MCS", "MODES", "Mode", "Radio", "default_plan", "refit"]
