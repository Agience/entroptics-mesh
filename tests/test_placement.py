"""Relay placement: which links hold, who is cut off, where relays go."""
from __future__ import annotations

import math

import pytest

from entroptics_mesh import placement as pl
from entroptics_mesh.adapt import HOP_SNR_DB, MARGIN_DB, Radio
from entroptics_mesh.neighbors import Position

R = Radio(min_rate=0.25e6, max_rate=2.4e6, center_hz=0.0, max_power_dbm=30.0, noise_dbm=-100.0,
          freq_hz=915e6)
RATE = 1.0e6
TARGET = HOP_SNR_DB + MARGIN_DB
PLAN = TARGET + pl.FADE_DB          # what a predicted (unmeasured) link must clear


def east(km: float, alt: float = 400.0) -> Position:
    """A point ``km`` east of (45 N, 75 W)."""
    return Position(45.0, -75.0 + km * 1000 / (6_371_000 * math.cos(math.radians(45))) * 180 / math.pi, alt)


def test_the_longest_hop_meets_the_target_exactly_or_stops_at_the_horizon():
    d = pl.max_hop_m(R, RATE, 400.0, 400.0)
    assert d <= pl.horizon_m(400.0, 400.0)
    if d < pl.horizon_m(400.0, 400.0):
        assert pl.predicted_snr_db(R, d, RATE) == pytest.approx(PLAN, abs=1e-6)
    # low and narrow: the horizon binds before the budget does
    assert pl.max_hop_m(R, 0.25e6, 2.0, 2.0) == pytest.approx(pl.horizon_m(2.0, 2.0))


def test_measured_beats_predicted_and_the_weaker_direction_counts():
    pos = {b"A": east(0), b"B": east(5)}                    # predicted: far above the target
    assert pl.links(pos, {}, R, RATE)[0].measured is False
    heard = {(b"A", b"B"): TARGET + 5, (b"B", b"A"): TARGET - 1}
    assert pl.links(pos, heard, R, RATE) == []              # B->A heard too weak: not usable
    heard = {(b"A", b"B"): TARGET + 5}
    (l,) = pl.links(pos, heard, R, RATE)
    assert l.measured and l.snr_db == TARGET + 5


def test_a_spoofed_position_does_not_override_what_was_heard():
    """A node claiming to be next door but heard at 3 dB is not a usable neighbour."""
    pos = {b"A": east(0), b"X": east(0.1)}
    assert pl.links(pos, {(b"X", b"A"): 3.0}, R, RATE) == []


def test_reachability_runs_through_relays_from_any_home():
    hop = pl.max_hop_m(R, RATE, 400.0, 400.0) / 1000
    pos = {b"G1": east(0), b"G2": east(-hop * 0.5), b"R": east(hop * 0.9), b"D": east(hop * 1.8),
           b"Far": east(hop * 3.5)}
    ok = pl.reachable(pl.links(pos, {}, R, RATE), [b"G1", b"G2"])
    assert {b"G1", b"G2", b"R", b"D"} <= ok and b"Far" not in ok


def test_the_plan_reconnects_a_cut_off_node_with_parallel_relays_that_close_every_hop():
    hop = pl.max_hop_m(R, RATE, 400.0, 400.0) / 1000
    pos = {b"G": east(0), b"D": east(hop * 0.9), b"Far": east(hop * 4.2)}
    p = pl.plan(pos, {}, [b"G"], R, RATE, relay_alt=400.0, parallel=2)
    assert set(p) == {b"Far"}
    slots = p[b"Far"]
    assert slots and all(len(s) == 2 for s in slots)
    chain = [pos[b"D"]] + [s[0] for s in slots] + [pos[b"Far"]]
    for a, b in zip(chain, chain[1:]):
        assert pl.predicted_snr_db(R, a.distance_m(b), RATE) >= PLAN
    a, b = slots[0]
    assert a.distance_m(b) == pytest.approx(2000.0, rel=0.02)   # side by side, across the line


def test_no_relays_when_the_target_is_in_reach():
    assert pl.relay_chain(east(0), east(1), R, RATE, relay_alt=400.0) == []


def test_antenna_gain_at_both_ends_lengthens_a_budget_limited_hop():
    """6 dBi at each end is 12 dB of path loss, x10^(12/20) = 3.98 in free-space distance, until the
    horizon binds."""
    import dataclasses
    g = dataclasses.replace(R, antenna_dbi=6.0)
    d0, d6 = pl.max_hop_m(R, RATE, 5000.0, 5000.0), pl.max_hop_m(g, RATE, 5000.0, 5000.0)
    assert d6 / d0 == pytest.approx(10 ** (12 / 20), rel=1e-9)
    assert pl.predicted_snr_db(g, d6, RATE) == pytest.approx(PLAN, abs=1e-6)
    assert pl.predicted_snr_db(g, d6, RATE, peer_dbi=0.0) == pytest.approx(PLAN - 6, abs=1e-6)


def test_a_prediction_pays_the_fade_margin_and_a_measurement_does_not():
    d = pl.max_hop_m(R, RATE, 400.0, 400.0, target_db=TARGET + 5)    # predicted TARGET + 5 dB
    pos = {b"A": east(0), b"B": east(d / 1000)}
    assert pl.links(pos, {}, R, RATE) == []                           # 5 dB short of PLAN
    assert len(pl.links(pos, {(b"A", b"B"): TARGET + 5}, R, RATE)) == 1


def test_a_distrusted_claim_is_not_predicted_from_or_planned_toward():
    """A position plausibility flagged buys no predicted link; heard links still count."""
    pos = {b"G": east(0), b"S": east(3), b"Far": east(400)}
    assert len(pl.links(pos, {}, R, RATE)) == 1                        # G-S predicted
    assert pl.links(pos, {}, R, RATE, distrusted={b"S"}) == []
    assert len(pl.links(pos, {(b"S", b"G"): TARGET + 1}, R, RATE, distrusted={b"S"})) == 1
    assert b"Far" in pl.plan(pos, {}, [b"G"], R, RATE, relay_alt=400.0)
    assert pl.plan(pos, {}, [b"G"], R, RATE, relay_alt=400.0, distrusted={b"Far", b"S"}) == {}
