"""Tests for entroptics_mesh.fec.

Run with ``-s`` to see the measured numbers (BER tables, decode time,
fountain overhead); the assertions use loose bounds around them.
"""

import sys
import time
import zlib
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from entroptics_mesh.fec import DENSE_K  # noqa: E402
from entroptics_mesh.fec import (  # noqa: E402
    FountainDecoder,
    PACKET_INTERLEAVE_COLS,
    block_deinterleave,
    block_interleave,
    conv_encode,
    crc32,
    decode_packet,
    deinterleave,
    encode_packet,
    fountain_encode,
    hard_to_soft,
    interleave,
    lt_neighbours,
    packet_coded_length,
    soft_bits_from_differential,
    viterbi_decode,
    viterbi_decode_hard,
)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def dqpsk_channel(bits, snr_db, rng):
    """Differential QPSK over AWGN, returning the PHY-style differential values.

    Bit pairs (MSB first) give index = 2*b0 + b1 and a phase step of
    index * pi/2. Absolute symbols exp(i*phi) get complex Gaussian noise with
    Es/N0 = snr_db; d[t] = r[t+1] * conj(r[t]) as the PHY's read_soft does.
    """
    b = np.asarray(bits, dtype=np.int64).reshape(-1, 2)
    idx = 2 * b[:, 0] + b[:, 1]
    phi = np.concatenate([[0.0], np.cumsum(idx * np.pi / 2)])
    n0 = 10 ** (-snr_db / 10)
    noise = np.sqrt(n0 / 2) * (rng.standard_normal(phi.size) + 1j * rng.standard_normal(phi.size))
    r = np.exp(1j * phi) + noise
    return r[1:] * np.conj(r[:-1])


def hard_from_differential(d):
    idx = np.mod(np.round(np.angle(d) / (np.pi / 2)), 4).astype(np.int64)
    return np.stack([(idx >> 1) & 1, idx & 1], axis=1).ravel().astype(np.uint8)


# ---------------------------------------------------------------------------
# CRC
# ---------------------------------------------------------------------------

def test_crc32_matches_zlib():
    rng = np.random.default_rng(0)
    cases = [b"", b"a", b"123456789", b"hello mesh"]
    cases += [rng.integers(0, 256, n, dtype=np.uint8).tobytes() for n in (1, 7, 64, 1000)]
    for data in cases:
        assert crc32(data) == zlib.crc32(data)
    assert crc32(b"123456789") == 0xCBF43926
    # continuation
    assert crc32(b"world", crc32(b"hello ")) == zlib.crc32(b"hello world")


# ---------------------------------------------------------------------------
# convolutional code + Viterbi
# ---------------------------------------------------------------------------

def test_conv_known_vector_and_length():
    # impulse response of the (171, 133) code: generator taps interleaved
    out = conv_encode(np.array([1], dtype=np.uint8))
    assert out.size == 2 * 7
    g0 = [int(c) for c in format(0o171, "07b")]
    g1 = [int(c) for c in format(0o133, "07b")]
    assert out[0::2].tolist() == g0
    assert out[1::2].tolist() == g1


def test_conv_roundtrip_clean():
    rng = np.random.default_rng(1)
    for n in (1, 8, 100, 1000):
        u = rng.integers(0, 2, n).astype(np.uint8)
        c = conv_encode(u)
        assert c.size == 2 * (n + 6)
        assert np.array_equal(viterbi_decode_hard(c), u)
        assert np.array_equal(viterbi_decode(hard_to_soft(c) * 3.7), u)


def test_viterbi_speed():
    rng = np.random.default_rng(2)
    u = rng.integers(0, 2, 10_000).astype(np.uint8)
    soft = hard_to_soft(conv_encode(u)) + 0.3 * rng.standard_normal(2 * (10_000 + 6))
    viterbi_decode(soft[:200])  # warm-up
    t0 = time.perf_counter()
    out = viterbi_decode(soft)
    dt = time.perf_counter() - t0
    print(f"\nViterbi decode of 10,000 info bits: {dt * 1000:.1f} ms")
    assert np.array_equal(out, u)
    assert dt < 1.0


@pytest.mark.parametrize("p", [1e-2, 3e-2])
def test_viterbi_hard_bsc(p):
    rng = np.random.default_rng(int(p * 1e4))
    n_blocks, block = 20, 10_000          # 200k information bits
    raw_err = raw_n = err = n = 0
    for _ in range(n_blocks):
        u = rng.integers(0, 2, block).astype(np.uint8)
        c = conv_encode(u)
        flips = rng.random(c.size) < p
        raw_err += int(flips.sum())
        raw_n += c.size
        err += int((viterbi_decode_hard(c ^ flips) != u).sum())
        n += u.size
    raw_ber, ber = raw_err / raw_n, err / n
    print(f"\nhard BSC p={p:g}: channel BER {raw_ber:.4g}, decoded BER {ber:.3g} "
          f"({err} errors / {n} bits)")
    assert ber < raw_ber / 20


def test_soft_bits_signs_and_values():
    # exact constellation points: hard bits as the PHY decides them, max confidence
    pts = np.exp(1j * np.pi / 2 * np.arange(4))
    s = soft_bits_from_differential(pts).reshape(4, 2)
    expect_bits = np.array([[0, 0], [0, 1], [1, 0], [1, 1]])
    assert np.array_equal((s < 0).astype(int), expect_bits)
    assert np.allclose(np.abs(s[:, 0]), np.pi / 4)   # MSB: pi/4 from -pi/4 / 3pi/4
    assert np.allclose(np.abs(s[:, 1]), np.pi / 4)   # LSB: pi/4 from +/-pi/4
    # MSB is most confident midway between points 0 and pi/2
    s = soft_bits_from_differential(np.exp(1j * np.pi / 4)).reshape(2)
    assert np.isclose(s[0], np.pi / 2) and np.isclose(s[1], 0.0, atol=1e-12)
    # agreement with the PHY's hard decision on random values
    rng = np.random.default_rng(3)
    d = rng.standard_normal(5000) + 1j * rng.standard_normal(5000)
    s = soft_bits_from_differential(d.reshape(50, 100))
    assert s.size == 10_000
    assert np.array_equal((s < 0).astype(np.uint8), hard_from_differential(d))


def test_viterbi_soft_beats_hard_on_dqpsk():
    rng = np.random.default_rng(4)
    block, n_blocks = 10_000, 10          # 100k information bits per SNR
    print("\nDQPSK over AWGN, K=7 r=1/2, interleaved (cols=16); 100k info bits per row")
    print(" Es/N0 dB | uncoded BER | coded hard | coded soft (angle) | coded soft (angle*|d|)")
    rows = []
    for snr in (6.0, 7.0, 8.0, 9.0, 10.0):
        e_unc = n_unc = e_h = e_s = e_m = n = 0
        for _ in range(n_blocks):
            u = rng.integers(0, 2, block).astype(np.uint8)
            tx = interleave(conv_encode(u), PACKET_INTERLEAVE_COLS)
            d = dqpsk_channel(tx, snr, rng)
            hard = hard_from_differential(d)
            e_unc += int((hard != tx).sum())
            n_unc += tx.size
            soft = soft_bits_from_differential(d)
            softm = soft_bits_from_differential(d, weight_by_magnitude=True)
            e_h += int((viterbi_decode_hard(deinterleave(hard, PACKET_INTERLEAVE_COLS)) != u).sum())
            e_s += int((viterbi_decode(deinterleave(soft, PACKET_INTERLEAVE_COLS)) != u).sum())
            e_m += int((viterbi_decode(deinterleave(softm, PACKET_INTERLEAVE_COLS)) != u).sum())
            n += block
        r = (snr, e_unc / n_unc, e_h / n, e_s / n, e_m / n)
        rows.append(r)
        print(f" {r[0]:7.1f} | {r[1]:11.4g} | {r[2]:10.3g} | {r[3]:18.3g} | {r[4]:.3g}")
    for snr, unc, hard, soft, softm in rows:
        assert soft <= hard
        if unc < 0.06:              # raw BER in the range seen on hardware and a bit beyond
            assert soft < unc / 50
    # soft decoding strictly better where hard decoding still leaves errors
    assert any(soft < hard for _, _, hard, soft, _ in rows if hard > 0)


# ---------------------------------------------------------------------------
# interleaver
# ---------------------------------------------------------------------------

def test_interleaver_roundtrip():
    x = np.arange(6 * 5)
    y = block_interleave(x, 6, 5)
    assert y[:6].tolist() == [0, 5, 10, 15, 20, 25]
    assert np.array_equal(block_deinterleave(y, 6, 5), x)
    for n in (1, 15, 16, 17, 333, 1000):
        x = np.arange(n) * 1.5
        y = interleave(x, 16)
        assert sorted(y.tolist()) == x.tolist()
        assert np.array_equal(deinterleave(y, 16), x)


def test_interleaver_spreads_burst():
    n = packet_coded_length(32)
    pos = deinterleave(np.arange(n), PACKET_INTERLEAVE_COLS)
    # a burst of 8 consecutive transmitted bits lands on coded positions >= 16 apart
    burst = np.sort(np.flatnonzero((pos >= 100) & (pos < 108)))
    assert np.min(np.diff(burst)) >= PACKET_INTERLEAVE_COLS


# ---------------------------------------------------------------------------
# packet layer
# ---------------------------------------------------------------------------

def test_packet_roundtrip_clean_and_padded():
    payload = b"one-way mesh payload"
    bits = encode_packet(payload)
    assert bits.size == packet_coded_length(len(payload))
    assert decode_packet(hard_to_soft(bits)) == payload
    bits = encode_packet(payload, frame_bytes=64)
    assert bits.size == packet_coded_length(64)
    assert decode_packet(hard_to_soft(bits)) == payload
    assert decode_packet(hard_to_soft(encode_packet(b""))) == b""
    with pytest.raises(ValueError):
        decode_packet(np.ones(101))


def test_packet_corrects_errors_and_crc_rejects():
    rng = np.random.default_rng(5)
    ok = 0
    trials = 200
    for _ in range(trials):
        payload = rng.integers(0, 256, 48, dtype=np.uint8).tobytes()
        bits = encode_packet(payload, frame_bytes=48)
        flips = rng.random(bits.size) < 0.03
        ok += decode_packet(hard_to_soft(bits ^ flips)) == payload
    print(f"\npackets (48 B payload, {packet_coded_length(48)} coded bits) at hard BER 3%: "
          f"{ok}/{trials} recovered")
    assert ok >= 0.95 * trials

    # a burst of 24 consecutive flipped transmitted bits is spread and corrected
    payload = b"burst test payload" * 2
    bits = encode_packet(payload)
    bits[200:224] ^= 1
    assert decode_packet(hard_to_soft(bits)) == payload

    # heavy corruption: decoder output fails CRC and the packet is dropped
    accepted_wrong = rejected = 0
    for _ in range(200):
        payload = rng.integers(0, 256, 48, dtype=np.uint8).tobytes()
        bits = encode_packet(payload, frame_bytes=48)
        out = decode_packet(hard_to_soft(bits ^ (rng.random(bits.size) < 0.25)))
        if out is None:
            rejected += 1
        elif out != payload:
            accepted_wrong += 1
    print(f"packets at hard BER 25%: {rejected}/200 rejected by CRC, "
          f"{accepted_wrong} wrong payloads accepted")
    assert accepted_wrong == 0
    assert rejected >= 190


def test_packet_from_dqpsk_soft_values():
    rng = np.random.default_rng(6)
    payload = rng.integers(0, 256, 100, dtype=np.uint8).tobytes()
    bits = encode_packet(payload)
    d = dqpsk_channel(bits, 9.0, rng)
    assert decode_packet(soft_bits_from_differential(d)) == payload


# ---------------------------------------------------------------------------
# fountain code
# ---------------------------------------------------------------------------

def test_fountain_determinism():
    msg = bytes(range(256)) * 3
    a = [s for _, s in zip(range(50), fountain_encode(msg, 32, seed=7))]
    b = [s for _, s in zip(range(50), fountain_encode(msg, 32, seed=7))]
    c = [s for _, s in zip(range(50), fountain_encode(msg, 32, seed=8))]
    assert a == b
    assert a != c
    assert lt_neighbours(123, 64, 9) == lt_neighbours(123, 64, 9)
    for sid in range(200):
        nb = lt_neighbours(sid, 64, 9)
        assert len(set(nb)) == len(nb) and all(0 <= j < 64 for j in nb)


def _fountain_trial(k, block_size, seed, rng, method, loss=0.3):
    msg = rng.integers(0, 256, k * block_size - 3, dtype=np.uint8).tobytes()
    stream = fountain_encode(msg, block_size, seed)
    sent = [next(stream) for _ in range(8 * k)]
    kept = [sent[i] for i in np.flatnonzero(rng.random(len(sent)) >= loss)]
    order = rng.permutation(len(kept))
    dec = FountainDecoder(k, block_size, seed, method=method)
    for i in order:
        sid, data = kept[i]
        if dec.add(sid, data):
            break
    assert dec.complete, "ran out of symbols"
    assert dec.message(len(msg)) == msg
    return dec.n_received / k


@pytest.mark.parametrize("k,trials", [(16, 300), (64, 200), (256, 50)])
def test_fountain_overhead_with_erasures(k, trials):
    rng = np.random.default_rng(k)
    stats = {}
    for method in (("gauss", "peel") if k > DENSE_K else ("gauss",)):
        ov = np.array([_fountain_trial(k, 8, 1000 + t, rng, method) for t in range(trials)])
        stats[method] = ov
        print(f"\nLT k={k:3d} {method:5s}: received/k mean {ov.mean():.3f}, "
              f"median {np.median(ov):.3f}, 95th pct {np.percentile(ov, 95):.3f}, "
              f"max {ov.max():.3f}  ({trials} trials, 30% erasures, random order)")
    assert stats["gauss"].mean() < 1.35
    if "peel" in stats:
        assert stats["gauss"].mean() <= stats["peel"].mean()
