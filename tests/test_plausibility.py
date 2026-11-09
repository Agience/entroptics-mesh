"""GPS plausibility: claims checked against the air (free space caps distance) and against speed."""
from __future__ import annotations

import math

import pytest

from entroptics_mesh import plausibility as gp
from entroptics_mesh.adapt import Radio, _snr_at
from entroptics_mesh.neighbors import Position

R = Radio(min_rate=0.25e6, max_rate=2.4e6, center_hz=0.0, max_power_dbm=30.0, noise_dbm=-100.0,
          freq_hz=915e6)
RATE = 1.0e6


def east(km: float, alt: float = 200.0) -> Position:
    return Position(45.0, -75.0 + km * 1000 / (6_371_000 * math.cos(math.radians(45))) * 180 / math.pi, alt)


def test_the_cap_inverts_the_free_space_prediction():
    snr = _snr_at(R, R.max_power_dbm, 12_000.0, RATE)
    assert gp.max_distance_m(snr, R, RATE) == pytest.approx(12_000.0, rel=1e-9)


def test_an_honest_claim_passes_even_through_extra_loss():
    """Fading only lowers the SNR, which only raises the cap: honest claims never fail."""
    true = east(10)
    obs = [(east(0), _snr_at(R, R.max_power_dbm, 10_000.0, RATE) - 15.0),   # 15 dB of fading
           (east(18), _snr_at(R, R.max_power_dbm, 8_000.0, RATE))]
    assert gp.check_claim(true, obs, R, RATE).ok


def test_a_far_claim_from_a_loud_sender_is_flagged_and_the_observer_named():
    """Heard loud at 2 km by observer 1, but claiming to be 40 km away: inconsistent."""
    obs = [(east(-30), _snr_at(R, R.max_power_dbm, 30_000.0, RATE) - 20.0),
           (east(0), _snr_at(R, R.max_power_dbm, 2_000.0, RATE))]
    v = gp.check_claim(east(40), obs, R, RATE)
    assert not v.ok and v.worst == 1 and v.excess_m > 30_000


def test_the_tolerance_absorbs_snr_estimate_error():
    snr = _snr_at(R, R.max_power_dbm, 10_000.0, RATE) + 2.0       # read 2 dB high
    assert gp.check_claim(east(10), [(east(0), snr)], R, RATE, tolerance_db=3.0).ok
    assert not gp.check_claim(east(10), [(east(0), snr)], R, RATE, tolerance_db=0.0).ok


def test_claims_must_be_flyable():
    assert gp.speed_ok(east(0), east(3), dt_s=10.0, max_speed_mps=300.0)      # 300 m/s, 3 km
    assert not gp.speed_ok(east(0), east(10), dt_s=10.0, max_speed_mps=300.0)
