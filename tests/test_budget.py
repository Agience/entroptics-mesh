"""The time budget: relays drop what can no longer arrive in time, with no shared clock."""
from __future__ import annotations

import numpy as np

from entroptics_mesh import phy
from entroptics_mesh.emu.air import Air, Radio
from entroptics_mesh.emu.subband_run import SubNode, SubbandRun
from entroptics_mesh.link import Symbol
from entroptics_mesh.providers import TxPlan
from entroptics_mesh.routing import Header, spend


def test_spend_subtracts_held_time_and_stops_at_zero():
    h = Header(b"A" * 8, 1, b"C" * 8, 3, 100)
    assert spend(h, 30.0).budget_ms == 70
    assert spend(h, 100.0) is None
    assert spend(Header(b"A" * 8, 1, b"C" * 8, 3), 1e9).budget_ms == 0      # no budget: never expires


def test_the_budget_travels_in_every_symbol_and_survives_relaying():
    s = Symbol(Header(b"A" * 8, 7, b"C" * 8, 3, 1234), 2, 8, 12, 5, bytes(8))
    r = Symbol.unpack(s.pack())
    assert r.header == s.header and r.header.budget_ms == 1234
    assert r.relayed(2).header.budget_ms == 1234


def _chain(budget_ms, seed=3):
    """origin -> relay -> far, 9 km hops, separate sub-bands; the far end cannot hear the origin."""
    RX, TXR = 2.4e6, 1.0e6
    geom = lambda c: phy.Known(64, TXR / RX, c / RX)
    air = Air(rate=RX, seed=seed)
    cs = [-0.6e6, 0.6e6, 0.0]
    nodes = [SubNode(bytes([65 + i]) * 8,
                     air.add(Radio(position=np.array([9000.0 * i, 0, 100]), tx_power_dbm=20.0,
                                   noise_dbm=-100.0)),
                     TxPlan(rate=TXR, center=c), key=1234) for i, c in enumerate(cs)]
    nodes[1].known = [geom(cs[0]), geom(cs[2])]
    nodes[0].known = [geom(cs[1])]
    nodes[2].known = [geom(cs[1])]
    run = SubbandRun(air, nodes, seed=seed, fresh=1.5, passive_ack=True)
    msg = bytes(np.random.default_rng(0).integers(0, 256, 2400, dtype=np.uint8))
    done = run.send(0, nodes[2].node_id, msg, hops=3, redundancy=1.5, budget_ms=budget_ms)
    return run, done


def test_a_budget_too_short_for_two_hops_is_dropped_on_the_way_not_carried_to_the_end():
    run, done = _chain(budget_ms=40)
    assert 2 not in done and run.expired > 0


def test_a_generous_budget_changes_nothing():
    run, done = _chain(budget_ms=60000)
    assert 2 in done and run.expired == 0
