"""The mesh's adapter onto entroptics-rf: a burst out, measured and read back with the key alone."""
from __future__ import annotations

import numpy as np

from entroptics_mesh import fec, phy
from entroptics_mesh.providers import TxPlan

PLAN = TxPlan(rate=2.0e6, L=64, M=4)          # K = L: every mode is lit
A, B, C = b"node-a..", b"node-b..", b"node-c.."


def _burst(key, sender=A, rows=80, seed=1):
    bits = np.random.default_rng(seed).integers(0, 2, phy.capacity_bits(rows, PLAN))
    return bits, phy.transmit_burst(bits, PLAN, key, sender=sender)


def test_a_burst_reads_back_with_the_key_alone():
    bits, iq = _burst(1234)
    rx = phy.receive_burst(np.concatenate([np.zeros(37, complex), iq]), 1234, candidates=range(60, 68))
    assert rx.locked and (rx.discovery.L, rx.discovery.K, rx.discovery.M) == (64, 64, 4)
    idx = np.mod(np.rint(np.angle(rx.soft) / (np.pi / 2)).astype(int), 4)
    got = fec.phy_to_gray(((idx[..., None] >> np.arange(1, -1, -1)) & 1).reshape(-1), 4)  # Gray labels
    assert np.array_equal(got[:bits.size], bits[:got.size])


def test_the_open_zero_state_needs_no_key():
    bits, iq = _burst(None)
    rx = phy.receive_burst(iq, None, candidates=range(60, 68))
    assert rx.locked and rx.discovery.L == 64


def test_the_sender_is_named_by_its_free_mark():
    _, iq = _burst(1234, sender=B)
    rx = phy.receive_burst(iq, 1234, candidates=range(60, 68))
    assert phy.sender_of(iq, rx, 1234, [A, B, C]) == B
