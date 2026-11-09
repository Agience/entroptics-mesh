"""Tests for l2bridge.py -- the Ethernet-over-mesh learning bridge (pure logic + wiring, with fakes;
the TAP device and run loop are Linux-only and exercised on the Pi fleet)."""
from __future__ import annotations

import types

from entroptics_mesh.l2bridge import (ETH_ALEN, L2Bridge, MacTable, dst_mac, frame_dest,
                                      is_group_mac, src_mac)
from entroptics_mesh.routing import BROADCAST

MAC_A = bytes.fromhex("02aaaaaaaaaa")      # unicast (locally administered: low bit of octet 0 is 0)
MAC_B = bytes.fromhex("02bbbbbbbbbb")
BCAST = b"\xff" * ETH_ALEN
MCAST = bytes.fromhex("01005e000001")      # IPv4 multicast MAC
NODE_A = b"A" * 8
NODE_B = b"B" * 8


def _frame(dst, src, payload=b"\x08\x00hello world"):
    return bytes(dst) + bytes(src) + payload


def test_group_mac_detection():
    assert is_group_mac(BCAST) and is_group_mac(MCAST)
    assert not is_group_mac(MAC_A)


def test_mac_parsing():
    f = _frame(MAC_A, MAC_B)
    assert dst_mac(f) == MAC_A and src_mac(f) == MAC_B


def test_mactable_learn_lookup_and_age():
    t = MacTable(age_s=10.0)
    t.learn(MAC_A, NODE_A, now=0.0)
    assert t.lookup(MAC_A, 5.0) == NODE_A
    assert t.lookup(MAC_A, 20.0) is None            # aged out, and purged
    assert len(t) == 0


def test_mactable_never_learns_a_group_source():
    t = MacTable()
    t.learn(BCAST, NODE_A, 0.0)
    t.learn(MCAST, NODE_A, 0.0)
    assert len(t) == 0


def test_frame_dest_floods_group_and_unknown_unicasts_the_learned():
    t = MacTable()
    assert frame_dest(_frame(BCAST, MAC_B), t, 0.0) == BROADCAST     # group -> flood (DHCP/ARP)
    assert frame_dest(_frame(MAC_A, MAC_B), t, 0.0) == BROADCAST     # unknown unicast -> flood
    t.learn(MAC_A, NODE_A, 0.0)
    assert frame_dest(_frame(MAC_A, MAC_B), t, 0.0) == NODE_A        # known unicast -> that node


# ---- wiring, with a fake node and tap (no real TAP needed) ----------------------------------------

class _FakeNode:
    def __init__(self, nid):
        self.node_id = nid
        self.on_message = None
        self.sent = []

    def send(self, payload, to, now):
        self.sent.append((to, payload))


class _FakeTap:
    def __init__(self):
        self.written = []
        self._q = []

    def read(self):
        return self._q.pop(0)

    def write(self, frame):
        self.written.append(frame)


def test_bridge_floods_outbound_learns_from_mesh_then_unicasts_the_reply_path():
    node, tap = _FakeNode(NODE_B), _FakeTap()
    br = L2Bridge(node, tap, now_fn=lambda: 0.0)

    # outbound broadcast (a DHCP DISCOVER from the local side) -> floods the mesh
    tap._q.append(_frame(BCAST, MAC_B))
    br.forward()
    assert node.sent[-1][0] == BROADCAST and br.tx == 1

    # a frame arrives from the mesh (src MAC_A, which lives on node A): written out, source learned
    br._on_mesh(types.SimpleNamespace(payload=_frame(MAC_B, MAC_A), origin=NODE_A))
    assert tap.written and br.rx == 1
    assert br.table.lookup(MAC_A, 0.0) == NODE_A

    # now an outbound unicast to MAC_A takes the learned path, not a flood
    tap._q.append(_frame(MAC_A, MAC_B))
    br.forward()
    assert node.sent[-1][0] == NODE_A


# ---- integration: the REAL mesh carries Ethernet frames end-to-end (emulated air) ----------------

import math                                                           # noqa: E402
import numpy as np                                                   # noqa: E402
from entroptics_mesh import phy                                      # noqa: E402
from entroptics_mesh.adapt import Radio                              # noqa: E402
from entroptics_mesh.emu.air import Air, Radio as AirRadio           # noqa: E402
from entroptics_mesh.emu.node_run import NodeRun                     # noqa: E402
from entroptics_mesh.neighbors import Position                       # noqa: E402
from entroptics_mesh.node import Node                                # noqa: E402
from entroptics_mesh.providers import NullCrypto, Providers          # noqa: E402

_RX = 2.4e6
_RADIO = Radio(min_rate=1.2e6, max_rate=_RX, center_hz=915e6, max_power_dbm=20.0, noise_dbm=-100.0)
_SUB = [phy.Known(64, 0.5, 0.25), phy.Known(64, 0.5, -0.25)]


class _KeyOnly(NullCrypto):
    name = "key-only"

    def waveform_key(self, ctx):
        return 1234


class _QTap:
    """A fake TAP: a queue of inbound frames and a list of what was written out."""
    def __init__(self):
        self.written, self._q = [], []

    def read(self):
        return self._q.pop(0)

    def write(self, frame):
        self.written.append(frame)


def _np_pos(x, y=0.0):
    return Position(math.degrees(y / 6_371_000.0), math.degrees(x / 6_371_000.0), 100.0)


def test_ethernet_frame_crosses_the_real_mesh_end_to_end():
    air = Air(rate=_RX, seed=3)
    nodes = []
    for i, (x, y) in enumerate([(0.0, 0.0), (9000.0, 0.0)]):
        air.add(AirRadio(position=np.array([x, y, 100.0]), tx_power_dbm=20.0, noise_dbm=-100.0))
        nodes.append(Node(bytes([65 + i]) * 8, _RADIO, slot_s=0.0, position=_np_pos(x, y),
                          providers=Providers(crypto=_KeyOnly()), seed=10 + i, blind_scaled=False))
    a, b = nodes
    for n in nodes:                                                  # learn bands + exchange geometry
        for g in _SUB:
            n.learn(g)
    for x in nodes:
        for y in nodes:
            if x is not y:
                x.on_announcement(y.announcement(0.0), y.node_id, 0.0)
    tapA, tapB = _QTap(), _QTap()
    brA = L2Bridge(a, tapA, now_fn=lambda: 0.0)
    brB = L2Bridge(b, tapB, now_fn=lambda: 0.0)
    run = NodeRun(air, nodes)

    # B's local side broadcasts a frame (a DHCP DISCOVER would look like this) -> floods to A
    frame = _frame(BCAST, MAC_B, b"\x08\x00discover")
    tapB._q.append(frame)
    brB.forward()
    run.run(80, until=lambda: bool(tapA.written))
    assert tapA.written == [frame]                                   # the frame crossed the mesh to A
    assert brA.table.lookup(MAC_B, 0.0) == b.node_id                 # A learned MAC_B -> node B

    # A's side replies to MAC_B: now a unicast, carried back over the mesh to B
    reply = _frame(MAC_B, MAC_A, b"\x08\x00offer")
    tapA._q.append(reply)
    brA.forward()
    assert a.queue and brA.table.lookup(MAC_B, 0.0) == b.node_id     # addressed to node B, not flooded
    run.run(80, until=lambda: bool(tapB.written))
    assert tapB.written == [reply]                                   # the reply crossed back to B
