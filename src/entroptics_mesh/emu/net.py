"""
net.py -- nodes on the emulated air, relaying by reply-free flooding, decoded by the real PHY.

A slotted run: in each slot at most the nodes holding something to relay transmit, each a burst
composed by the real PHY adapter; every other node captures the slot and decodes it with the key
alone.  A message is one burst: the routing header and payload,
protected by ``fec`` (CRC-32 and a rate-1/2 code) so a collision or uncorrectable error is dropped
rather than read as a new message.  Slot choice is the simplest reply-free access: a node
with something to send picks a slot at random within a window, so two relays that heard the same
message rarely collide, and when they do the copies that took other paths still arrive.

The run reports what it measured -- who received what, when, after how many transmissions -- so a
scenario is a measurement of the system, never a model of it.
"""
from __future__ import annotations

import struct
from dataclasses import dataclass, field

import numpy as np

from .. import fec, phy
from ..providers import TxPlan
from ..routing import FloodRouter, Header
from .air import Air

_HDR = struct.Struct(">8sI8sBH")         # origin, seq, to, hops, payload length


MIN_ROWS = 64        # a burst carries at least this many data rows: blind discovery needs about L^2
                     # samples to place a screen (entroptics-rf), so a 3-row frame cannot be found
                     # by a receiver that was told nothing.  Short frames are padded; the length
                     # field says where the payload ends.  The padding is the price of acquisition
                     # with nothing pre-shared, and per-sender cached scale (fast path) will cut it.


FRAME_BYTES = 64     # every frame is coded at this protected size, so a receiver knows the coded
                     # length without being told: header + payload up to 64 - 2 bytes.


def frame_bits(h: Header, payload: bytes, plan=None, seed: int = 0) -> np.ndarray:
    """Header + payload, protected: length, CRC-32, rate-1/2 K=7 code, interleaved (``fec``), then
    padded with filler to a burst a blind receiver can find.  A collision or an error that the code
    cannot correct fails the CRC and the frame is dropped -- it can never be read as a new message."""
    raw = _HDR.pack(h.origin, h.seq, h.to, h.hops, len(payload)) + bytes(payload)
    bits = fec.encode_packet(raw, frame_bytes=FRAME_BYTES).astype(np.int64)
    if plan is not None:
        need = phy.capacity_bits(MIN_ROWS, plan) - bits.size
        if need > 0:
            bits = np.concatenate([bits, np.random.default_rng(seed).integers(0, 2, need)])
    return bits


def unframe_soft(soft_d, M: int = 4) -> tuple[Header, bytes] | None:
    """The frame from the PHY's soft differential values, or None if it fails the CRC."""
    n = fec.packet_coded_length(FRAME_BYTES)
    sb = fec.soft_bits_from_differential(np.asarray(soft_d), M=M, weight_by_magnitude=True,
                                         gray=True)       # phy.transmit_burst labels Gray
    if sb.size < n:
        return None
    raw = fec.decode_packet(sb[:n])
    if raw is None or len(raw) < _HDR.size:
        return None
    origin, seq, to, hops, ln = _HDR.unpack_from(raw, 0)
    if _HDR.size + ln > len(raw):
        return None
    return Header(origin, seq, to, hops), raw[_HDR.size:_HDR.size + ln]


def unframe(bits) -> tuple[Header, bytes] | None:
    """As :func:`unframe_soft`, from hard bits."""
    n = fec.packet_coded_length(FRAME_BYTES)
    b = np.asarray(bits)[:n]
    if b.size < n:
        return None
    raw = fec.decode_packet(fec.hard_to_soft(b))
    if raw is None or len(raw) < _HDR.size:
        return None
    origin, seq, to, hops, ln = _HDR.unpack_from(raw, 0)
    return (Header(origin, seq, to, hops), raw[_HDR.size:_HDR.size + ln]) if _HDR.size + ln <= len(raw) else None


@dataclass
class Node:
    node_id: bytes
    index: int
    router: FloodRouter
    key: object = None
    inbox: list = field(default_factory=list)          # (t, Header, payload)
    outbox: list = field(default_factory=list)         # (Header, payload)


@dataclass
class FloodRun:
    air: Air
    nodes: list[Node]
    plan: TxPlan = field(default_factory=lambda: TxPlan(rate=2.0e6))
    window: int = 4                                     # slots a relay picks among
    seed: int = 0
    move: object = None                                 # optional f(t) -> (n, 3) positions, metres
    log: list = field(default_factory=list)            # (slot, sender, Header)

    def __post_init__(self):
        self._rng = np.random.default_rng(self.seed)

    def _slot_samples(self, payload_len: int) -> int:
        nbits = max(fec.packet_coded_length(FRAME_BYTES), phy.capacity_bits(MIN_ROWS, self.plan))
        return (phy.rows_for(nbits, self.plan) + 1) * self.plan.L + 2 * self.plan.L

    def run(self, origin: int, to: bytes, payload: bytes, hops: int, max_slots: int = 64):
        """Flood one message; returns the slot at which each node first delivered it."""
        o = self.nodes[origin]
        o.outbox.append((o.router.originate(to, hops), payload))
        n = self._slot_samples(len(payload))
        due: dict[int, int] = {origin: 0}
        delivered: dict[int, int] = {}
        base_t = getattr(self, "_clock", 0.0)
        for slot in range(max_slots):
            t0 = base_t + slot * n / self.air.rate
            if self.move is not None:                   # the fleet moves between slots
                for r, p in zip(self.air.radios, self.move(t0)):
                    r.position = np.asarray(p, dtype=float)
            senders = [i for i, s in due.items() if s == slot]
            for i in senders:
                h, pl = self.nodes[i].outbox.pop(0)
                self.air.send(i, t0 + self.plan.L / self.air.rate,
                              phy.transmit_burst(frame_bits(h, pl, self.plan, slot), self.plan, self.nodes[i].key,
                                                 sender=self.nodes[i].node_id))
                self.log.append((slot, i, h))
                del due[i]
            if senders:
                for node in self.nodes:
                    if node.index in senders:
                        continue
                    y = self.air.capture(node.index, t0, n)
                    rx = phy.receive_burst(y, node.key, candidates=range(self.plan.L - 4, self.plan.L + 5))
                    if not rx.locked or rx.soft.size == 0:
                        continue
                    got = unframe_soft(rx.soft, self.plan.M)
                    if got is None:
                        continue
                    h, pl = got
                    deliver, relay = node.router.heard(h, t0)
                    if deliver and node.index not in delivered:
                        delivered[node.index] = slot
                        node.inbox.append((t0, h, pl))
                    if relay is not None:
                        node.outbox.append((relay, pl))
                        due[node.index] = slot + 1 + int(self._rng.integers(0, self.window))
                self.air.clear_before(t0)
            if not due:
                break
        self._clock = base_t + (slot + 1) * n / self.air.rate
        return delivered


__all__ = ["frame_bits", "unframe_soft", "unframe", "Node", "FloodRun"]
