"""
plausibility.py -- is a claimed position consistent with what the air says?

GPS may be unavailable, spoofed, or simply wrong, so an announced position is a claim.  The air gives a
check that needs no one's cooperation, because free space is the best case: a signal can only lose
more than the Friis loss, never less.  A measured SNR therefore caps how far away its sender can be
(at the sender's full power and both antenna gains), and a claim farther than that cap is
inconsistent.  The test is one-sided on purpose -- a weak signal from a near claim proves nothing
(fading, power control, a wall) -- so it never accuses on the lossy side.

A second check needs only the claims themselves: two positions announced ``dt`` apart cannot be
farther apart than the node can fly.

Neither says *who* is wrong when the observer's own position is also a claim.  They flag an
inconsistent pair or set; the caller decides what to distrust (``placement`` already prefers heard
SNR over positions whenever it has one).
"""
from __future__ import annotations

import math
from dataclasses import dataclass

from .adapt import Radio
from .neighbors import Position
from .propagation import C_LIGHT


def max_distance_m(snr_db: float, sender: Radio, rate: float, *, observer_dbi: float | None = None,
                   observer_noise_dbm: float | None = None) -> float:
    """The farthest a sender heard at ``snr_db`` can be: where free space at its full power would
    give exactly that SNR.  ``observer_*`` default to the sender's own radio class."""
    rx_gain = sender.antenna_dbi if observer_dbi is None else observer_dbi
    noise0 = sender.noise_dbm if observer_noise_dbm is None else observer_noise_dbm
    noise = noise0 + 10 * math.log10(rate / sender.max_rate)
    loss = sender.max_power_dbm + sender.antenna_dbi + rx_gain - noise - float(snr_db)
    lam = C_LIGHT / sender.freq_hz
    return lam / (4 * math.pi) * 10 ** (loss / 20)


@dataclass(frozen=True)
class Verdict:
    ok: bool
    excess_m: float          # how far past the tightest cap the claim sits (<= 0 when consistent)
    worst: int | None        # index of the observation that caps it hardest


def check_claim(claim: Position, observations: list[tuple[Position, float]], sender: Radio,
                rate: float, *, tolerance_db: float = 3.0) -> Verdict:
    """``observations`` are (observer position, SNR it measured for this sender[, slack_m]).  The
    claim is consistent when it lies inside every observer's cap, with ``tolerance_db`` of slack
    for the SNR estimate itself (``tolerance_db`` of SNR is a factor 10^(tol/20) in distance),
    plus the observation's own ``slack_m``: how far the two could have moved apart since the SNR
    was measured (a cap holds for the positions at that moment, not for today's claim)."""
    worst, excess = None, -math.inf
    for i, ob in enumerate(observations):
        obs, snr = ob[0], ob[1]
        cap = max_distance_m(snr - tolerance_db, sender, rate) + (float(ob[2]) if len(ob) > 2 else 0.0)
        e = claim.distance_m(obs) - cap
        if e > excess:
            worst, excess = i, e
    return Verdict(excess <= 0.0, float(excess if worst is not None else 0.0), worst)


def speed_ok(before: Position, after: Position, dt_s: float, max_speed_mps: float,
             slack_m: float = 50.0) -> bool:
    """Two claims ``dt_s`` apart are consistent when the node could have flown between them
    (``slack_m`` covers ordinary GPS scatter)."""
    return before.distance_m(after) <= max_speed_mps * max(float(dt_s), 0.0) + slack_m


__all__ = ["Verdict", "check_claim", "max_distance_m", "speed_ok"]
