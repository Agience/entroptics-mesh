"""Soft bits for coherent square QAM: max-log LLRs, Gray-labelled as the rf codec maps them, with
the noise variance measured off the symbols themselves."""
from __future__ import annotations

import math

import numpy as np
import pytest

from entroptics_mesh import fec
from entroptics_rf import waveform as wf

K, L, SEED = 64, 64, 1234          # every mode is lit (entroptics-rf research/rank_leak.md)


def _noisy(M, n, snr_db, seed=0):
    rng = np.random.default_rng(seed)
    idx = rng.integers(0, M, n)
    n0 = 10 ** (-snr_db / 10)
    y = wf._qam(M)[idx] + math.sqrt(n0 / 2) * (rng.standard_normal(n) + 1j * rng.standard_normal(n))
    return idx, y, n0


@pytest.mark.parametrize("M", [4, 16, 64, 256])
def test_signs_are_the_codecs_own_qam_decisions(M):
    """The hard decision of every LLR is the bit the rf slicer reads: same lattice, same Gray
    labels, same bit order."""
    _, y, _ = _noisy(M, 3000, 12.0)
    soft = fec.soft_bits_from_qam(y, M)
    assert np.array_equal((soft < 0).astype(np.int64), wf._qam_bits(y, M))


@pytest.mark.parametrize("M", [4, 16, 64, 256])
def test_noiseless_symbols_give_their_labels_and_finite_llrs(M):
    idx = np.arange(M)
    soft = fec.soft_bits_from_qam(wf._qam(M)[idx], M)
    b = int(math.log2(M))
    bits = ((idx[:, None] >> np.arange(b - 1, -1, -1)) & 1).ravel()
    assert np.all(np.isfinite(soft))
    assert np.array_equal((soft < 0).astype(np.int64), bits)


def test_four_qam_llr_is_the_exact_gaussian_llr():
    """For 4-QAM max-log is exact: bit 0 of the in-phase axis is the level -1/sqrt2, so the LLR
    is -2*sqrt2*Re(y)/N0, and likewise on the quadrature axis."""
    _, y, _ = _noisy(4, 200, 6.0, seed=3)
    soft = fec.soft_bits_from_qam(y, 4, noise_var=0.25).reshape(-1, 2)
    assert np.allclose(soft[:, 0], -2 * math.sqrt(2) * y.real / 0.25)
    assert np.allclose(soft[:, 1], -2 * math.sqrt(2) * y.imag / 0.25)


@pytest.mark.parametrize("M,snr", [(4, 12.0), (16, 18.0), (64, 24.0), (256, 30.0)])
def test_the_noise_variance_is_measured_off_the_symbols(M, snr):
    """Decision residuals read the true N0 to within a few percent where symbol errors are rare,
    globally and per mode."""
    _, y, n0 = _noisy(M, 20000, snr, seed=5)
    assert abs(fec.qam_noise_var(y, M) / n0 - 1) < 0.08
    per_mode = fec.qam_noise_var(y.reshape(-1, 50), M, axis=0)
    assert per_mode.shape == (50,) and np.all(np.abs(per_mode / n0 - 1) < 0.35)
    # a per-mode variance broadcasts against a (rows, K) array of symbols
    soft = fec.soft_bits_from_qam(y.reshape(-1, 50), M, noise_var=per_mode)
    assert soft.size == y.size * int(math.log2(M))


@pytest.mark.parametrize("M,rate,snr", [(16, "1/2", 13.0), (64, "3/4", 22.0)])
def test_a_packet_decodes_through_the_codec(M, rate, snr):
    """encode_packet -> compose_qam -> AWGN -> the codec's informed coherent read -> LLRs ->
    decode_packet.  Nothing about the noise is passed to the receiver.  Each SNR sits where a
    500-byte frame's PER is a few percent or less (measured at K = L = 64: 16-QAM 1/2 0.043 at
    12 dB, 0.93 at 10; 64-QAM 3/4 0.017 at 22 dB -- lab rank_leak/adopted/out/per_new.jsonl)."""
    rng = np.random.default_rng(11)
    payload = rng.integers(0, 256, 200, dtype=np.uint8).tobytes()
    tx = fec.encode_packet(payload, rate=rate)
    iq = wf.compose_qam(tx, K, L, SEED, M=M).astype(np.complex128)
    n0 = 10 ** (-snr / 10)
    iq = iq + math.sqrt(n0 / 2) * (rng.standard_normal(iq.size) + 1j * rng.standard_normal(iq.size))
    A = wf._coherent_project(iq, L, K, SEED, 0, 0.0, 0.0)
    Y = wf._dekeyed(A, 0, K, SEED, 0.0, L=L)
    n_sym = -(-tx.size // int(math.log2(M)))
    soft = fec.soft_bits_from_qam(Y.ravel()[:n_sym], M)[: tx.size]
    assert fec.decode_packet(soft, rate=rate) == payload


def test_rejects_orders_that_are_not_square():
    with pytest.raises(ValueError):
        fec.soft_bits_from_qam(np.zeros(4, complex), 8)
    with pytest.raises(ValueError):
        fec.soft_bits_from_qam(np.zeros(4, complex), 2)


def test_per_mode_noise_is_read_mode_by_mode():
    """With K, symbol i is on mode i % K (compose_qam's order, a short last row included): the
    per-mode read equals the column read of the full rows, and modes with more noise get it."""
    rng = np.random.default_rng(9)
    Kq, rows, M = 12, 300, 16
    idx = rng.integers(0, M, rows * Kq + 5)                    # a short last row of 5
    sig = np.linspace(0.004, 0.03, Kq)                          # N0 rising across the modes (15-24 dB)
    n0 = sig[np.arange(idx.size) % Kq]
    y = wf._qam(M)[idx] + np.sqrt(n0 / 2) * (rng.standard_normal(idx.size)
                                             + 1j * rng.standard_normal(idx.size))
    per = fec.qam_noise_var(y, M, K=Kq)
    full = fec.qam_noise_var(y[:rows * Kq].reshape(rows, Kq), M, axis=0)
    assert per.shape == (Kq,)
    assert np.allclose(per[5:], full[5:]) and np.all(np.abs(per / sig - 1) < 0.3)
    soft = fec.soft_bits_from_qam(y, M, K=Kq)
    explicit = fec.soft_bits_from_qam(y, M, noise_var=per[np.arange(y.size) % Kq])
    assert np.allclose(soft, explicit)
