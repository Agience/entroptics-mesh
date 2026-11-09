"""
subband_run.py -- relaying when each sender chooses its own sub-band.

Every node transmits at its own rate and centre (``TxPlan.rate`` / ``TxPlan.center``); every
receiver captures the air at the air's rate and reads, from each capture, every burst it can: for
each geometry it has measured before (``phy.Known``: a width, a scale, a band centre) it runs the
fast read, and every frame that passes its CRC is kept.  Two senders on different sub-bands in the
same slot therefore cost nothing -- both are read from one capture.

The caches start in the steady state a node reaches after first contact: each node holds the
geometries of the senders it can hear, as it would after one blind acquisition each (blind scaled
acquisition is minutes per burst in Python; it is the first-contact path, never the per-burst one).
Nothing here is pre-shared between nodes -- each cache is what that node measured.

Relaying is decode-and-forward (``link_run``'s rule): a relay listens until it has rebuilt a
message, then sends fresh fountain symbols of its own on its own sub-band.  The relaying rules
themselves (dedupe, passive ack, sibling share, decode-and-forward, hop and time budget) live in
:mod:`entroptics_mesh.relay`, which the node runtime runs too; this module only schedules slots.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from .. import fec, phy, relay
from ..link import Reassembler
from ..providers import TxPlan
from ..routing import Header
from .air import Air
from .link_run import BLOCK, FRAME_BYTES, _frame, _unframe


@dataclass
class SubNode:
    node_id: bytes
    index: int
    plan: TxPlan
    key: object = None
    known: list = field(default_factory=list)          # phy.Known geometries this node measured
    queue: list = field(default_factory=list)
    seen: set = field(default_factory=set)
    rx: Reassembler = field(default_factory=Reassembler)
    got: dict = field(default_factory=dict)
    decoded: int = 0
    dies_at: int | None = None                         # slot from which this node is gone (no tx, no rx)
    held: dict = field(default_factory=dict)           # msg_id -> slot it reached this node
    accepts: frozenset = frozenset()                   # extra addresses this node takes delivery
                                                       # for (anycast: HOME on every gateway)


@dataclass
class SubbandRun:
    air: Air
    nodes: list[SubNode]
    window: int = 4
    fresh: float = 1.5
    seed: int = 0
    passive_ack: bool = False        # an origin that overhears fresh symbols of its own message
                                     # knows a relay rebuilt it, and drops what it still queues
    sibling_ack: bool = False        # a relay that overhears another relay forwarding the same
                                     # message cancels one queued symbol per symbol heard (same
                                     # hop count), and all of them on hearing a later hop; a relay
                                     # that dies is simply not heard, so the survivors send in full

    @property
    def policy(self) -> relay.Policy:
        return relay.Policy(self.fresh, self.passive_ack, self.sibling_ack)

    def __post_init__(self):
        self._rng = np.random.default_rng(self.seed)
        self._seq = {}
        self.bursts = 0
        self.multi = 0
        self.acked = 0                                  # messages an origin stopped early on
        self.cancelled = 0                              # relay symbols cancelled by overhearing
        self.backhaul = {}                              # msg_id -> first gateway to deliver it (the
                                                        # wired dedupe among nodes sharing an address)
        self.duplicates = 0                             # anycast deliveries the backhaul dropped
        self.expired = 0                                # symbols dropped: their time budget ran out

    def _slot_samples(self) -> int:
        slowest = min(n.plan.rate for n in self.nodes)
        rows = phy.rows_for(fec.packet_coded_length(FRAME_BYTES, self.nodes[0].plan.code_rate),
                            self.nodes[0].plan)
        return int(np.ceil((rows + 1) * 64 * self.air.rate / slowest)) + 400

    def send(self, origin: int, to: bytes, message: bytes, hops: int, redundancy: float,
             max_slots: int = 200, budget_ms: int = 0) -> dict:
        o = self.nodes[origin]
        seq = self._seq[origin] = self._seq.get(origin, 0) + 1
        h = Header(o.node_id, seq, bytes(to), int(hops), int(budget_ms))
        relay.originate(o, h, message, BLOCK, redundancy, 0)
        n = self._slot_samples()
        slot_ms = 1e3 * n / self.air.rate
        due, done = {origin: 0}, {}
        for slot in range(max_slots):
            t0 = slot * n / self.air.rate
            gone = {n.index for n in self.nodes if n.dies_at is not None and slot >= n.dies_at}
            for i in gone:
                due.pop(i, None)
            senders = [i for i, s in due.items() if s == slot]
            for i in list(senders):
                node = self.nodes[i]
                # The time budget: what this node held the message for, its own transmit slot
                # included -- a duration on its own clock, nothing shared.
                sym, dropped = relay.pop(node, slot, slot_ms, 1)
                if sym is None:
                    self.expired += dropped
                    del due[i]
                    senders.remove(i)
                    if node.queue:
                        due[i] = slot + 1
                    continue
                self.air.send(i, t0 + 100 / self.air.rate,
                              phy.transmit_burst(_frame(sym, node.plan.code_rate), node.plan, node.key,
                                                 sender=node.node_id),
                              rate=node.plan.rate, center=node.plan.center)
                self.bursts += 1
                del due[i]
                if node.queue:
                    due[i] = slot + 1 + int(self._rng.integers(0, self.window))
            if not senders:
                if not due:
                    break
                continue
            for node in self.nodes:
                if node.index in senders or node.index in gone:
                    continue
                y = self.air.capture(node.index, t0, n)
                frames = 0
                for g in node.known:
                    rx = phy.receive_known(y, node.key, g, accept=lambda v: _unframe(v) is not None)
                    sym = _unframe(rx) if rx.locked else None
                    if sym is None:
                        continue
                    frames += 1
                    self._take(node, sym, slot, due, done)
                self.multi += frames > 1
            self.air.clear_before(t0)
            if not due:
                break
        return done

    def _take(self, node, sym, slot, due, done):
        t = relay.take(node, sym, slot, self.policy, start_id=1_000_000 * (node.index + 1))
        if not t.new:
            return
        self.acked += t.acked
        self.cancelled += t.cancelled
        node.decoded += t.decoded
        if t.idle:
            due.pop(node.index, None)
        hd = sym.header
        if t.delivered is not None and node.index not in done:
            if t.anycast and hd.msg_id in self.backhaul:  # another gateway already delivered it
                self.duplicates += 1
            else:
                if t.anycast:
                    self.backhaul[hd.msg_id] = node.index
                done[node.index] = slot
                node.got[hd.msg_id] = (slot, t.delivered)
        if t.forwarding and node.index not in due:
            due[node.index] = slot + 1 + int(self._rng.integers(0, self.window))

    def _overheard(self, node, hd, due):
        """Another relay is forwarding a message this node also forwards: share the work
        (:func:`entroptics_mesh.relay.overheard`)."""
        n, idle = relay.overheard(node, hd)
        self.cancelled += n
        if idle:
            due.pop(node.index, None)


__all__ = ["SubNode", "SubbandRun"]
