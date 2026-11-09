"""Tests for lan_air.py and run_bridge.py -- the LAN-carried emulated air and the slot runner.

The waveform really crosses a UDP socket here (loopback), so these exercise the full path:
serialize a burst -> datagram -> socket -> datagram -> inject -> demodulate.  No TAP and no root are
needed (the TAP and the wall-clock select loop are Linux-only and run on the Pi fleet); the node
slot drive is stepped by hand so the tests are deterministic."""
from __future__ import annotations

import numpy as np

from entroptics_mesh.emu.air import Radio as AirRadio
from entroptics_mesh.emu.lan_air import LanAir, Peer, decode_burst, encode_burst
from entroptics_mesh.l2bridge import ETH_ALEN
from entroptics_mesh.routing import BROADCAST
from entroptics_mesh.run_bridge import build

MAC_A = bytes.fromhex("02aaaaaaaaaa")
MAC_B = bytes.fromhex("02bbbbbbbbbb")
BCAST = b"\xff" * ETH_ALEN


def _frame(dst, src, payload=b"\x08\x00hello"):
    return bytes(dst) + bytes(src) + payload


# ---- wire framing ---------------------------------------------------------------------------------

def test_burst_encode_decode_round_trip():
    iq = (np.random.default_rng(0).standard_normal(300)
          + 1j * np.random.default_rng(1).standard_normal(300))
    dg = encode_burst(3, 1.25, iq, rate=1.2e6, center=-0.25e6)
    sender, t, got, rate, center = decode_burst(dg)
    assert sender == 3 and t == 1.25 and rate == 1.2e6 and center == -0.25e6
    assert got.size == iq.size
    assert np.allclose(got, iq, atol=1e-3)          # complex64 on the wire -> small rounding only


def test_decode_rejects_malformed():
    assert decode_burst(b"") is None
    assert decode_burst(b"nope" + b"\x00" * 40) is None            # wrong magic
    good = encode_burst(0, 0.0, np.ones(10, complex), rate=None, center=0.0)
    assert decode_burst(good[:-4]) is None                          # truncated IQ payload


# ---- the medium across two sockets ----------------------------------------------------------------

def _two_radios():
    return [AirRadio(position=np.array([0.0, 0.0, 100.0]), tx_power_dbm=20.0, noise_dbm=-100.0),
            AirRadio(position=np.array([3000.0, 0.0, 100.0]), tx_power_dbm=20.0, noise_dbm=-100.0)]


def _linked_pair(seed=0):
    radios = _two_radios()
    a = LanAir(radios, local={0}, peers=[], bind=("127.0.0.1", 0), rate=2.4e6, seed=seed)
    b = LanAir(radios, local={1}, peers=[], bind=("127.0.0.1", 0), rate=2.4e6, seed=seed + 1)
    a.peers = [Peer("127.0.0.1", b.sock.getsockname()[1])]
    b.peers = [Peer("127.0.0.1", a.sock.getsockname()[1])]
    return a, b


def test_burst_crosses_the_lan_and_is_heard():
    a, b = _linked_pair()
    try:
        iq = np.ones(400, complex)                  # unit-RMS tone
        a.send(0, 0.0, iq, rate=2.4e6, center=0.0)  # node 0 (on host A) transmits
        assert a.tx == 1
        assert b.pump() == 1 and b.rx == 1          # host B took it off the socket
        y = b.capture(1, 0.0, 1024)                 # and hears it, scaled by the path loss
        heard = 10.0 ** (a.gain_db(0, 1) / 20.0)
        loud = np.sort(np.abs(y))[-200:].mean()     # the 200 in-burst samples (the 400-wide burst)
        assert abs(loud - heard) < heard * 0.3      # amplitude matches the path loss
    finally:
        a.close()
        b.close()


def test_local_send_is_not_echoed_to_peers_when_not_local():
    # A burst from a radio this host does not own is put on the local medium but not re-transmitted.
    radios = _two_radios()
    a = LanAir(radios, local={0}, peers=[], bind=("127.0.0.1", 0))
    try:
        a.send(1, 0.0, np.ones(10, complex), rate=None, center=0.0)   # index 1 is not local to A
        assert a.tx == 0
    finally:
        a.close()


def test_oversize_burst_is_dropped_and_counted():
    radios = _two_radios()
    a = LanAir(radios, local={0}, peers=[Peer("127.0.0.1", 9)], bind=("127.0.0.1", 0))
    try:
        a.send(0, 0.0, np.ones(20000, complex), rate=None, center=0.0)   # > one datagram
        assert a.tx == 0 and a.dropped_tx == 1
    finally:
        a.close()


# ---- end to end: an Ethernet frame crosses the LAN-carried mesh -----------------------------------

class _QTap:
    def __init__(self):
        self.written, self._q = [], []

    def read(self):
        return self._q.pop(0)

    def write(self, frame):
        self.written.append(frame)


_CONFIG = {
    "freq_hz": 915e6, "rate": 2.4e6, "waveform_key": 0x00E7,
    "radio": {"min_rate": 1.2e6, "max_rate": 2.4e6, "center_hz": 915e6,
              "max_power_dbm": 20.0, "noise_dbm": -100.0},
    "radios": [
        {"x": 0.0, "y": 0.0, "z": 100.0, "tx_dbm": 20.0, "noise_dbm": -100.0, "node_id": "AAAAAAAA"},
        {"x": 9000.0, "y": 0.0, "z": 100.0, "tx_dbm": 20.0, "noise_dbm": -100.0, "node_id": "BBBBBBBB"},
    ],
}


def test_ethernet_frame_crosses_the_lan_mesh_end_to_end():
    tapA, tapB = _QTap(), _QTap()
    nodeA, airA, brA, runA = build(_CONFIG, 0, tapA, peers=[], bind=("127.0.0.1", 0),
                                   now_fn=lambda: 0.0)
    nodeB, airB, brB, runB = build(_CONFIG, 1, tapB, peers=[], bind=("127.0.0.1", 0),
                                   now_fn=lambda: 0.0)
    airA.peers = [Peer("127.0.0.1", airB.sock.getsockname()[1])]
    airB.peers = [Peer("127.0.0.1", airA.sock.getsockname()[1])]
    try:
        # seed each other's geometry (as the single-process e2e test does), isolating the transport
        nodeA.on_announcement(nodeB.announcement(0.0), nodeB.node_id, 0.0)
        nodeB.on_announcement(nodeA.announcement(0.0), nodeA.node_id, 0.0)

        n, slot_s = runA.n, runA.slot_s
        hosts = [(nodeA, airA, 0), (nodeB, airB, 1)]

        def slot(now):
            txd = {}
            for node, air, idx in hosts:                 # 1. transmit what is due
                g = node.next_burst(now)
                txd[idx] = g is not None
                if g:
                    iq, plan = g
                    amp = 10.0 ** (min(plan.power_db, 0.0) / 20.0)
                    air.send(idx, now, iq * amp, rate=plan.rate, center=plan.center)
            airA.pump()                                  # 2. carry bursts across the LAN
            airB.pump()
            for node, air, idx in hosts:                 # 3. the quiet host hears and reads
                if not txd[idx]:
                    node.on_capture(air.capture(idx, now, n), now)
            for node, air, idx in hosts:                 # 4. tick + age the medium
                node.tick(now)
                air.clear_before(now)

        # B's local side broadcasts a frame (a DHCP DISCOVER looks like this) -> floods to A
        frame = _frame(BCAST, MAC_B, b"\x08\x00discover")
        tapB._q.append(frame)
        brB.forward()
        for i in range(120):
            slot(i * slot_s)
            if tapA.written:
                break
        assert tapA.written == [frame]                   # the frame crossed the LAN-carried mesh
        assert brA.table.lookup(MAC_B, 0.0) == nodeB.node_id

        # A replies to MAC_B: now a unicast on the learned path, carried back to B
        reply = _frame(MAC_B, MAC_A, b"\x08\x00offer")
        tapA._q.append(reply)
        brA.forward()
        start = len(tapB.written)
        for i in range(120, 300):
            slot(i * slot_s)
            if len(tapB.written) > start:
                break
        assert tapB.written[-1] == reply                 # the reply crossed back to B
    finally:
        airA.close()
        airB.close()
