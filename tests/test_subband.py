"""Senders narrower than the receiver, anywhere in its band, read at a known scale and centre."""
from __future__ import annotations

import numpy as np
import pytest

from entroptics_mesh import phy
from entroptics_mesh.emu import link_run as lr
from entroptics_mesh.emu.air import Air, Radio
from entroptics_mesh.link import symbols
from entroptics_mesh.routing import Header


@pytest.mark.parametrize("center", [0.0, 0.5e6, -0.5e6])
def test_a_sub_band_burst_reads_at_its_known_scale_and_centre(center):
    """A 1.0 Msps sender placed at 0 / +-0.5 MHz in a 2.4 Msps receiver's band (+-32 cycles per row
    of carrier), read with a prior a few ppm and 0.002 of the band off -- as a receiver would hold
    it from an earlier lock."""
    msg = bytes(np.random.default_rng(0).integers(0, 256, 2400, dtype=np.uint8))
    s = symbols(Header(b"A" * 8, 1, b"C" * 8, 4), msg, lr.BLOCK, 1.0)[0]
    air = Air(rate=2.4e6, seed=3)
    a = air.add(Radio(position=np.array([0., 0, 100]), tx_power_dbm=20, noise_dbm=-100))
    b = air.add(Radio(position=np.array([6000., 0, 100]), tx_power_dbm=20, noise_dbm=-100))
    iq = phy.transmit_burst(lr._frame(s), lr.TxPlan(rate=1.0e6), 1234, sender=b"A" * 8)
    air.send(a, 100 / 2.4e6, iq, rate=1.0e6, center=center)
    y = air.capture(b, 0.0, int(iq.size * 2.4 + 600))
    rx = phy.receive_known(y, 1234, phy.Known(64, (1.0 / 2.4) * (1 + 2e-6), center / 2.4e6 + 0.002))
    got = lr._unframe(rx.soft)
    assert got is not None and got.data == s.data


def _relay_with_queue(hops: int, n: int):
    from entroptics_mesh.emu.subband_run import SubNode, SubbandRun
    from entroptics_mesh.link import Symbol
    from entroptics_mesh.providers import TxPlan
    air = Air(rate=2.4e6, seed=0)
    nodes = [SubNode(bytes([65 + i]) * 8, air.add(Radio(position=np.array([i * 1e3, 0., 100]))),
                     TxPlan(rate=1.0e6)) for i in range(3)]
    run = SubbandRun(air, nodes, sibling_ack=True)
    h = Header(b"A" * 8, 1, b"C" * 8, hops)
    nodes[1].queue = [Symbol(h, 6, lr.BLOCK, 2400, 1_000_000 + i, b"") for i in range(n)]
    return run, nodes[1]


def test_a_relay_hands_one_symbol_to_each_it_hears_from_a_sibling():
    """A sibling forwarding the same message at the same hop count takes one symbol off my queue
    per symbol heard -- and only once per symbol (duplicates are dropped before this rule)."""
    run, relay = _relay_with_queue(hops=3, n=18)
    due = {relay.index: 5}
    run._overheard(relay, Header(b"A" * 8, 1, b"C" * 8, 3), due)
    run._overheard(relay, Header(b"A" * 8, 1, b"C" * 8, 3), due)
    assert len(relay.queue) == 16 and run.cancelled == 2 and relay.index in due


def test_a_relay_stops_when_a_later_hop_forwards_and_ignores_other_messages():
    run, relay = _relay_with_queue(hops=3, n=18)
    due = {relay.index: 5}
    run._overheard(relay, Header(b"B" * 8, 7, b"C" * 8, 2), due)       # another message: no effect
    run._overheard(relay, Header(b"A" * 8, 1, b"C" * 8, 4), due)       # an earlier hop: no effect
    assert len(relay.queue) == 18
    run._overheard(relay, Header(b"A" * 8, 1, b"C" * 8, 2), due)       # a later hop rebuilt it
    assert relay.queue == [] and relay.index not in due
