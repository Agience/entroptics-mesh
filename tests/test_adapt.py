"""The default plan: fitted to the audience heard, never cornered by it, never mirroring."""
from __future__ import annotations

import numpy as np
import pytest

from entroptics_mesh.adapt import (FAMILIES, HOP_SNR_DB, MARGIN_DB, MCS, MODES, Radio, _envelope,
                                   _mcs_for, _snr_at, default_plan)
from entroptics_mesh.neighbors import Announced, BandRange, NeighborTable, Position
from entroptics_mesh.providers import Intent

R = Radio(min_rate=0.25e6, max_rate=2.4e6, center_hz=915e6, max_power_dbm=20.0)
ME = Position(49.0, -123.0, 100.0)
B = b"node-b.."


def _table(km, band=None):
    t = NeighborTable()
    t.announced(B, Announced(t=0.0, position=Position(49.0, -123.0 + km / 73.0, 100.0), band=band))
    return t


def test_nothing_known_means_shout():
    plan, _ = default_plan(Intent(to=None, n_bytes=100), NeighborTable(), R, rng=np.random.default_rng(0))
    assert plan.rate == R.max_rate and plan.power_db == 0.0


def test_a_near_neighbour_is_whispered_to():
    plan, code = default_plan(Intent(to=B, n_bytes=100), _table(1.0), R, me=ME, rng=np.random.default_rng(0))
    assert plan.rate == R.max_rate and plan.power_db < -10.0
    assert _snr_at(R, R.max_power_dbm + plan.power_db, 1000.0, plan.rate) >= HOP_SNR_DB + MARGIN_DB - 0.5


def test_a_far_neighbour_gets_a_narrower_band_at_full_power():
    near, _ = default_plan(Intent(to=B, n_bytes=100), _table(5.0), R, me=ME, rng=np.random.default_rng(0))
    far, _ = default_plan(Intent(to=B, n_bytes=100), _table(25.0), R, me=ME, rng=np.random.default_rng(0))
    assert far.rate < near.rate and far.power_db == 0.0


def test_a_narrowband_listener_is_a_clue_not_a_corner():
    """Its announced capture bounds what we send IT; broadcasts are not narrowed by it."""
    t = _table(1.0, band=BandRange(0.25e6, 0.5e6, 915e6))
    to_it, _ = default_plan(Intent(to=B, n_bytes=100), t, R, me=ME, rng=np.random.default_rng(0))
    to_all, _ = default_plan(Intent(to=None, n_bytes=100), t, R, me=ME, rng=np.random.default_rng(0))
    assert to_it.rate <= 0.5e6 and to_all.rate == R.max_rate


def test_a_relay_never_forwards_on_the_band_it_heard():
    t = _table(25.0)
    for s in range(50):
        plan, _ = default_plan(Intent(to=B, n_bytes=100), t, R, me=ME, heard_on=-0.6e6,
                               rng=np.random.default_rng(s))
        assert abs(plan.center - (-0.6e6)) >= plan.rate / 2


def test_the_sub_band_choice_varies():
    t = _table(25.0)
    centers = {default_plan(Intent(to=B, n_bytes=100), t, R, me=ME, rng=np.random.default_rng(s))[0].center
               for s in range(40)}
    assert len(centers) > 1


def test_the_mcs_table_is_an_envelope():
    """Every entry beats every cheaper one on goodput: thresholds and goodputs both rise."""
    assert {m.family for m in MCS} <= set(FAMILIES)
    for a, b in zip(MCS, MCS[1:]):
        assert b.threshold_db > a.threshold_db and b.goodput > a.goodput


@pytest.mark.parametrize("families", [FAMILIES, ("differential",), ("coherent",)])
def test_the_planner_takes_the_best_mode_the_snr_clears(families):
    """Over the measured modes of the allowed families -- not the combined envelope filtered,
    which would leave a differential-only node without DQPSK or D16PSK (coherent dominates them)."""
    allowed = [m for m in MODES if m.family in families]
    for snr in np.arange(-5.0, 40.0, 0.5):
        mode = _mcs_for(float(snr), families)
        assert mode.family in families
        cleared = [m for m in allowed if m.threshold_db <= snr]
        if cleared:
            assert mode.threshold_db <= snr and mode.goodput == max(m.goodput for m in cleared)
        else:
            assert mode == min(allowed, key=lambda m: m.threshold_db)   # the most robust


def test_coherent_qam_carries_the_top_of_the_envelope():
    """Differential PSK saturates; above it the envelope is coherent QAM."""
    top_diff = max(m.goodput for m in MODES if m.family == "differential")
    hi = _mcs_for(35.0)
    assert hi.family == "coherent" and hi.goodput > top_diff
    assert _mcs_for(35.0, ("differential",)).goodput == top_diff
    assert MCS == _envelope(MODES)


def test_a_differential_only_node_plans_on_its_own_family_envelope():
    """At the hop target (9 + 3 dB) a differential-only node sends DQPSK 3/4, not the DBPSK the
    combined envelope would leave it once coherent is filtered out."""
    m = _mcs_for(HOP_SNR_DB + MARGIN_DB, ("differential",))
    assert (m.family, m.M, m.rate) == ("differential", 4, "3/4")


def test_the_plan_carries_the_chosen_family():
    t = _table(1.0)
    plan, code = default_plan(Intent(to=B, n_bytes=100), t, R, me=ME, rng=np.random.default_rng(0))
    snr = _snr_at(R, R.max_power_dbm + plan.power_db, 1000.0, plan.rate)
    mode = _mcs_for(snr)
    assert (plan.family, plan.M, code, plan.code_rate) == (mode.family, mode.M, mode.rate, mode.rate)
    plan, code = default_plan(Intent(to=B, n_bytes=100), t, R, me=ME, rng=np.random.default_rng(0),
                              families=("differential",))
    assert plan.family == "differential" and plan.M in (2, 4, 8, 16)
    shout, _ = default_plan(Intent(to=None, n_bytes=100), NeighborTable(), R, rng=np.random.default_rng(0))
    assert (shout.family, shout.M) == (MCS[0].family, MCS[0].M)


def test_a_relay_that_knows_nothing_still_never_forwards_on_the_band_it_heard():
    """No position for the destination: the relay still gets a band other than the one the message
    came in on (half width, so there are two), at full power."""
    for seed in range(20):
        plan, _ = default_plan(Intent(to=None, n_bytes=100), NeighborTable(), R, heard_on=-0.6e6,
                               rng=np.random.default_rng(seed))
        assert plan.rate == R.max_rate / 2 and plan.power_db == 0.0
        assert abs(plan.center - (-0.6e6)) >= plan.rate / 2


def test_a_relay_keeps_clear_of_the_bands_its_siblings_were_heard_on():
    """Two bursts on one sub-band in a slot are both lost; a relay that heard a sibling forwarding
    the same message on a band keeps off it when the band allows (here four quarter-width slots)."""
    R4 = Radio(min_rate=0.25e6, max_rate=2.4e6, center_hz=915e6, max_power_dbm=20.0)
    t = _table(60.0)                                 # far: narrowed
    for seed in range(20):
        plan, _ = default_plan(Intent(to=B, n_bytes=100), t, R4, me=ME, heard_on=-0.9e6,
                               avoid={-0.9e6, 0.3e6}, rng=np.random.default_rng(seed))
        assert plan.rate <= 0.6e6
        for c in (-0.9e6, 0.3e6):
            assert abs(plan.center - c) >= plan.rate / 2


def test_a_provider_that_widens_the_plan_sheds_rate_and_one_that_narrows_keeps_it():
    from entroptics_mesh.adapt import refit
    from entroptics_mesh.providers import TxPlan
    d = TxPlan(rate=0.6e6, power_db=0.0, M=16, family="differential", code_rate="3/4")
    wide = refit(d, TxPlan(rate=2.4e6, power_db=0.0, M=16, family="differential", code_rate="3/4"),
                 ("differential",))
    assert (wide.M, wide.code_rate) != (16, "3/4")                      # 6 dB lost: steps down
    from entroptics_mesh.adapt import MODES
    th = {(m.family, m.M, m.rate): m.threshold_db for m in MODES}
    assert th[(wide.family, wide.M, wide.code_rate)] <= th[("differential", 16, "3/4")] - 10 * np.log10(4) + 1e-9
    narrow = TxPlan(rate=0.3e6, power_db=0.0, M=16, family="differential", code_rate="3/4")
    assert refit(d, narrow) == narrow                                   # only helps: kept
    own = TxPlan(rate=2.4e6, power_db=0.0, M=2, family="differential", code_rate="1/2")
    assert refit(d, own) == own                                         # provider chose: left alone
