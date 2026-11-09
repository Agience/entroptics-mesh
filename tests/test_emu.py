"""The emulated air with real PHY bursts and reply-free flooding."""
from __future__ import annotations

import numpy as np
import pytest

from entroptics_mesh.emu.air import Air, Radio, fspl_db
from entroptics_mesh.emu.net import FloodRun, Node, frame_bits, unframe
from entroptics_mesh.routing import BROADCAST, FloodRouter, Header


def test_friis_at_915_mhz():
    assert fspl_db(1000.0, 915e6) == pytest.approx(91.7, abs=0.1)


def test_a_frame_round_trips():
    h = Header(b"origin..", 7, BROADCAST, 5)
    assert unframe(frame_bits(h, b"hello")) == (h, b"hello")


def _fleet(spacing_m, n=5, tx_dbm=20.0, key=1234, seed=0):
    air = Air(freq_hz=915e6, rate=2.0e6, seed=seed)
    nodes = []
    for i in range(n):
        idx = air.add(Radio(position=np.array([i * spacing_m, 0.0, 100.0]), tx_power_dbm=tx_dbm,
                            noise_dbm=-100.0))
        nid = bytes([65 + i]) * 8
        nodes.append(Node(nid, idx, FloodRouter(nid), key=key))
    return air, nodes


def test_a_broadcast_floods_down_a_fleet_line_with_real_bursts():
    """Base at 0, drones every 2.5 km at 20 dBm: one hop is heard at 20.4 dB, two at 14.3 dB, so the
    message reaches the end of the line by relays, each node delivering it once."""
    air, nodes = _fleet(2500.0)
    run = FloodRun(air, nodes, seed=1)
    delivered = run.run(origin=0, to=BROADCAST, payload=b"status:ok", hops=6)
    assert set(delivered) == {1, 2, 3, 4}
    for _, _, pl in (n.inbox[0] for n in nodes[1:]):
        assert pl == b"status:ok"
    senders = [s for _, s, _ in run.log]
    assert len(senders) == len(set(senders)), "a node relayed the same message twice"


def test_beyond_reach_nothing_is_invented():
    """Nodes 40 km apart are below the noise: nothing is delivered and nothing is relayed."""
    air, nodes = _fleet(40_000.0, n=3)
    delivered = FloodRun(air, nodes, seed=2).run(origin=0, to=BROADCAST, payload=b"x", hops=4)
    assert delivered == {}
