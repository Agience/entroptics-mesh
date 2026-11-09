"""Tests for the punctured code rates (2/3, 3/4) in entroptics_mesh.fec."""

import sys
from fractions import Fraction
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from entroptics_mesh.fec import (  # noqa: E402
    PUNCTURE_PATTERNS,
    conv_encode,
    decode_packet,
    depuncture,
    encode_packet,
    hard_to_soft,
    packet_coded_length,
    puncture,
    punctured_length,
    viterbi_decode,
)

RATES = ["1/2", "2/3", "3/4"]


def test_patterns_are_the_80211_ones():
    assert PUNCTURE_PATTERNS["2/3"] == ((1, 1), (1, 0))
    assert PUNCTURE_PATTERNS["3/4"] == ((1, 1, 0), (1, 0, 1))
    # 3/4 sends A1 B1 A2 B3 out of A1 B1 A2 B2 A3 B3
    x = np.arange(12)
    assert puncture(x, "3/4").tolist() == [0, 1, 2, 5, 6, 7, 8, 11]
    assert puncture(x, "2/3").tolist() == [0, 1, 2, 4, 5, 6, 8, 9, 10]


@pytest.mark.parametrize("rate", RATES)
def test_coded_length_formula(rate):
    for frame in (0, 1, 7, 50, 333):
        n = 8 * (2 + frame + 4) + 6
        want = {"1/2": 2 * n,
                "2/3": 3 * (n // 2) + 2 * (n % 2),
                "3/4": 4 * (n // 3) + (0, 2, 3)[n % 3]}[rate]
        assert packet_coded_length(frame, rate) == want
        assert encode_packet(bytes(frame), rate=rate).size == want
        # the effective rate approaches the nominal one for long frames
    big = 8 * (2 + 10000 + 4) + 6
    nominal = float(Fraction(rate))
    assert abs(big / punctured_length(big, rate) - nominal) < 1e-3


def test_rate_spellings_agree():
    for spelling in ("2/3", Fraction(2, 3), 2 / 3, (2, 3)):
        assert packet_coded_length(20, spelling) == packet_coded_length(20, "2/3")
    with pytest.raises(ValueError):
        packet_coded_length(20, "5/6")


def test_default_rate_unchanged():
    payload = b"default-rate packet"
    assert np.array_equal(encode_packet(payload), encode_packet(payload, rate="1/2"))
    assert packet_coded_length(19) == 2 * (8 * 25 + 6)


@pytest.mark.parametrize("rate", RATES)
def test_depuncture_inserts_erasures(rate):
    rng = np.random.default_rng(1)
    c = conv_encode(rng.integers(0, 2, 40))
    p = puncture(hard_to_soft(c), rate)
    d = depuncture(p, rate)
    assert d.size == c.size
    kept = d != 0
    assert np.array_equal(d[kept], hard_to_soft(c)[kept])
    assert int((~kept).sum()) == c.size - p.size


@pytest.mark.parametrize("rate", RATES)
def test_noiseless_round_trip(rate):
    rng = np.random.default_rng(2)
    for n in (0, 1, 13, 200):
        payload = rng.integers(0, 256, n, dtype=np.uint8).tobytes()
        for frame in (None, n + 9):
            coded = encode_packet(payload, frame_bytes=frame, rate=rate)
            assert decode_packet(hard_to_soft(coded), rate=rate) == payload


@pytest.mark.parametrize("rate", RATES)
def test_viterbi_through_depuncture(rate):
    rng = np.random.default_rng(3)
    u = rng.integers(0, 2, 500).astype(np.uint8)
    soft = puncture(hard_to_soft(conv_encode(u)), rate)
    assert np.array_equal(viterbi_decode(depuncture(soft, rate)), u)


@pytest.mark.parametrize("rate,ber", [("1/2", 0.02), ("2/3", 0.005), ("3/4", 0.002)])
def test_corrects_random_errors(rate, ber):
    """Hard-decision BER the code is comfortably below its threshold at."""
    rng = np.random.default_rng(4)
    ok = 0
    trials = 20
    for _ in range(trials):
        payload = rng.integers(0, 256, 100, dtype=np.uint8).tobytes()
        coded = encode_packet(payload, rate=rate)
        flips = rng.random(coded.size) < ber
        assert flips.sum() > 0 or ber * coded.size < 3
        rx = hard_to_soft(coded ^ flips.astype(np.uint8))
        ok += decode_packet(rx, rate=rate) == payload
    assert ok >= trials - 2


def test_soft_awgn_ordering():
    """Same Eb/N0 on BPSK soft values: lower rate decodes at least as well."""
    rng = np.random.default_rng(5)
    es_n0 = 10 ** (1.0 / 10)  # 1 dB per coded bit
    fails = {}
    for rate in RATES:
        f = 0
        for _ in range(30):
            payload = rng.integers(0, 256, 60, dtype=np.uint8).tobytes()
            coded = encode_packet(payload, rate=rate)
            y = hard_to_soft(coded) + rng.normal(0, np.sqrt(1 / (2 * es_n0)), coded.size)
            f += decode_packet(y, rate=rate) != payload
        fails[rate] = f
    assert fails["1/2"] <= fails["2/3"] <= fails["3/4"]
    assert fails["1/2"] <= 3


def test_wrong_length_rejected():
    coded = encode_packet(b"abc", rate="3/4")
    with pytest.raises(ValueError):
        decode_packet(hard_to_soft(coded[:-1]), rate="3/4")
