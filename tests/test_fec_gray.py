"""Gray labels over the PHY's natural-binary symbols, done in the FEC layer."""
from __future__ import annotations

import numpy as np
import pytest

from entroptics_mesh import fec


@pytest.mark.parametrize("M", [2, 4, 8, 16])
def test_gray_mapping_round_trips(M):
    bps = int(np.log2(M))
    bits = np.random.default_rng(M).integers(0, 2, 4000 // bps * bps)
    assert np.array_equal(fec.phy_to_gray(fec.gray_to_phy(bits, M), M), bits)


@pytest.mark.parametrize("M", [4, 8, 16])
def test_neighbouring_phases_differ_in_one_information_bit(M):
    bps = int(np.log2(M))
    info = [fec.phy_to_gray(((n >> np.arange(bps - 1, -1, -1)) & 1), M) for n in range(M)]
    for n in range(M):
        assert int(np.sum(info[n] != info[(n + 1) % M])) == 1


@pytest.mark.parametrize("M", [4, 8, 16])
def test_soft_gray_signs_match_the_hard_gray_decision(M):
    rng = np.random.default_rng(7)
    d = np.exp(1j * rng.uniform(-np.pi, np.pi, 500))
    soft = fec.soft_bits_from_differential(d, M=M, gray=True)
    n = np.mod(np.rint(np.angle(d) * M / (2 * np.pi)).astype(int), M)
    bps = int(np.log2(M))
    hard = fec.phy_to_gray(((n[:, None] >> np.arange(bps - 1, -1, -1)) & 1).reshape(-1), M)
    assert np.array_equal((soft < 0).astype(int), hard)
