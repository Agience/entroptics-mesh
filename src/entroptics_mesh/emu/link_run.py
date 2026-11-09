"""
link_run.py -- messages of any size on the emulated air, as fountain symbols relayed hop by hop.

A message becomes ``ceil(k * redundancy)`` fountain symbols (``link.symbols``); each symbol rides
one burst in its own FEC/CRC frame.  Every node that decodes a symbol it has not seen before passes
it to its reassembler and -- if it is not the only destination and the hop budget allows -- queues
it to relay once.  A destination rebuilds the message from any spanning set of symbols, whichever
paths they took.  Nothing is acknowledged.

Access is the same reply-free slotted scheme as ``net.py``: a node with something queued picks a
slot at random within a window, so neighbours rarely collide and a collision costs symbols, not the
message.

What a run reports is measured: when each destination completed, how many bursts went on the air,
and how many symbols each node decoded.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from .. import fec, phy
from ..link import BLOCK, FRAME_BYTES, Reassembler, Symbol, symbols, _seed as link_seed
from ..link import frame as _frame, unframe as _unframe
from ..providers import TxPlan
from ..routing import Header
from .air import Air


@dataclass
class LinkNode:
    node_id: bytes
    index: int
    key: object = None
    queue: list = field(default_factory=list)          # symbols to send / relay, in order
    seen: set = field(default_factory=set)             # (origin, seq, symbol id)
    rx: Reassembler = field(default_factory=Reassembler)
    got: dict = field(default_factory=dict)            # msg id -> (slot, message)
    decoded: int = 0


@dataclass
class LinkRun:
    air: Air
    nodes: list[LinkNode]
    plan: TxPlan = field(default_factory=lambda: TxPlan(rate=2.0e6))
    window: int = 4
    seed: int = 0
    fresh: float = 0.0               # decode-and-forward: once a relay has rebuilt a message it
                                     # drops the caught symbols it still holds for it and sends
                                     # ceil(k * fresh) NEW symbols of its own (0 = plain relaying)

    def __post_init__(self):
        self._rng = np.random.default_rng(self.seed)
        self._seq = {}
        self.bursts = 0

    def _slot_samples(self) -> int:
        rows = phy.rows_for(fec.packet_coded_length(FRAME_BYTES, self.plan.code_rate), self.plan)
        return (rows + 1) * self.plan.L + 3 * self.plan.L

    def send(self, origin: int, to: bytes, message: bytes, hops: int, redundancy: float,
             max_slots: int = 400) -> dict:
        """Send one message; returns {node index: slot completed} for every node that rebuilt it."""
        o = self.nodes[origin]
        seq = self._seq[origin] = self._seq.get(origin, 0) + 1
        h = Header(o.node_id, seq, bytes(to), int(hops))
        for s in symbols(h, message, BLOCK, redundancy):
            o.queue.append(s)
            o.seen.add((h.origin, h.seq, s.symbol_id))
        n = self._slot_samples()
        due = {origin: 0}
        done: dict[int, int] = {}
        for slot in range(max_slots):
            t0 = slot * n / self.air.rate
            senders = [i for i, s in due.items() if s == slot]
            for i in senders:
                sym = self.nodes[i].queue.pop(0)
                self.air.send(i, t0 + self.plan.L / self.air.rate,
                              phy.transmit_burst(_frame(sym, self.plan.code_rate), self.plan, self.nodes[i].key,
                                                 sender=self.nodes[i].node_id))
                self.bursts += 1
                del due[i]
                if self.nodes[i].queue:              # more to send: next pick
                    due[i] = slot + 1 + int(self._rng.integers(0, self.window))
            if senders:
                for node in self.nodes:
                    if node.index in senders:
                        continue
                    rx = phy.receive_burst(self.air.capture(node.index, t0, n), node.key,
                                           candidates=range(self.plan.L - 4, self.plan.L + 5))
                    if not rx.locked:
                        continue
                    sym = _unframe(rx)
                    if sym is None:
                        continue
                    k = (sym.header.origin, sym.header.seq, sym.symbol_id)
                    if k in node.seen:
                        continue
                    node.seen.add(k)
                    node.decoded += 1
                    hd = sym.header
                    mine = hd.to == node.node_id or hd.to == b"\xff" * 8
                    relays = hd.to != node.node_id and hd.hops > 1 and hd.origin != node.node_id
                    msg = node.rx.add(sym) if (mine or (relays and self.fresh)) else None
                    if msg is not None and mine and node.index not in done:
                        done[node.index] = slot
                        node.got[hd.msg_id] = (slot, msg)
                    if relays and self.fresh and msg is not None:
                        # rebuilt here: new symbols carry more downstream than repeats of the
                        # caught ones, and they come from this relay's own region of symbol ids
                        rh = Header(hd.origin, hd.seq, hd.to, hd.hops - 1, hd.budget_ms)
                        src = fec.fountain_encode(msg, sym.block, link_seed(hd.origin, hd.seq),
                                                  start_id=1_000_000 * (node.index + 1))
                        for _ in range(int(np.ceil(sym.k * self.fresh))):
                            sid, data = next(src)
                            node.queue.append(Symbol(rh, sym.k, sym.block, sym.length, sid, data))
                    elif relays and not self.fresh:
                        # plain relaying.  Under decode-and-forward a relay only listens until it
                        # has rebuilt the message: sending caught symbols meanwhile would cost it
                        # the origin's next ones (a radio cannot hear while it sends).
                        node.queue.append(sym.relayed(hd.hops - 1))
                    if node.queue and node.index not in due:
                        due[node.index] = slot + 1 + int(self._rng.integers(0, self.window))
                self.air.clear_before(t0)
            if not due:
                break
        return done


__all__ = ["FRAME_BYTES", "BLOCK", "LinkNode", "LinkRun"]
