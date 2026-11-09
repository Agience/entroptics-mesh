"""
node_run.py -- N node runtimes (:class:`entroptics_mesh.node.Node`) over the emulated air.

The driver is only the radio for each node.  Slot by slot: every live node is asked for a burst
(``next_burst``); each burst goes on the air at its plan's rate, centre and power; every live node
that did not transmit (half duplex) gets the slot's capture (``on_capture``); then every node
ticks.  The nodes decide everything else -- what to read, what to relay, when to send, on which
sub-band -- from their own measurements.

The slot grid is the driver's sampling, not a shared schedule: each node's own ``slot_s`` happens
to equal it here.  A slot is long enough for one frame at the slowest rate any node may send at,
plus the nodes' contention window when they listen before talking (``listen``, the default): each
node with a burst due draws its own start inside the window, and in start order each listens to
what is already on the air before its start, then sends or defers (``Node.start``/``commit``).

Measured per run: CPU seconds per node per slot (everything the node did in that slot: reads,
relay rules, planning, composing its burst), bursts on the air, and the slot each message was
delivered in.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field

import numpy as np

from .. import adapt, fec, phy
from ..link import FRAME_BYTES
from ..node import Node
from ..providers import TxPlan
from .air import Air


def slot_samples(air_rate: float, min_rate: float, plan: TxPlan | None = None,
                 families=adapt.FAMILIES) -> int:
    """Samples (at the air's rate) in a slot that holds one frame at ``min_rate`` with margin --
    the same sizing ``SubbandRun`` uses.  The frame is ``plan``'s, or else the longest any mode of
    ``families`` makes (the planner's most robust, DBPSK 1/2: 131 rows at K = L = 64)."""
    plans = ([plan] if plan is not None else
             [TxPlan(rate=min_rate, M=m.M, family=m.family, code_rate=m.rate)
              for m in adapt.MODES if m.family in families])
    rows = max(phy.rows_for(fec.packet_coded_length(FRAME_BYTES, p.code_rate), p) for p in plans)
    return int(np.ceil((rows + 1) * 64 * air_rate / min_rate)) + 400


@dataclass
class NodeRun:
    air: Air
    nodes: list[Node]                     # nodes[i] transmits from air radio i
    dies: dict = field(default_factory=dict)       # node index -> slot from which it is gone
    listen: bool = True                   # listen before talk (Node.start / Node.commit)

    def __post_init__(self):
        self.n = slot_samples(self.air.rate, min(nd.radio.min_rate for nd in self.nodes),
                              families={f for nd in self.nodes for f in nd.families})
        if self.listen:
            # room for the latest start any node may draw (its contention window, its own clock)
            self.n += int(np.ceil(max(nd.contention_s for nd in self.nodes) * self.air.rate))
        self.slot_s = self.n / self.air.rate
        for nd in self.nodes:
            nd.slot_s = self.slot_s
        self.slot = 0
        self.bursts = 0
        self.cpu = [[] for _ in self.nodes]         # per node: CPU seconds per slot it was alive
        self.log = []                               # (slot, sender index, plan)

    def now(self, slot: int | None = None) -> float:
        return (self.slot if slot is None else slot) * self.slot_s

    def alive(self, i: int) -> bool:
        d = self.dies.get(i)
        return d is None or self.slot < d

    def step(self) -> list[int]:
        """One slot.  Returns the indices that transmitted."""
        t0 = self.now()
        spent = [0.0] * len(self.nodes)
        senders = []
        starts = []
        for i, nd in enumerate(self.nodes):
            if not self.alive(i):
                continue
            if not self.listen:
                starts.append((0.0, i))
                continue
            c = time.perf_counter()
            s = nd.start(t0)
            spent[i] += time.perf_counter() - c
            if s is not None:
                starts.append((s, i))
        # In start order, each node listens to what is already on the air over the listen before
        # its own start, then sends or defers (its own decision, Node.commit).
        for s, i in sorted(starts):
            nd = self.nodes[i]
            at = t0 + s + 100 / self.air.rate
            c = time.perf_counter()
            if self.listen:
                m = max(1, int(round(nd.listen_s * self.air.rate)))
                b = nd.commit(t0, self.air.capture(i, at - m / self.air.rate, m))
            else:
                b = nd.next_burst(t0)
            spent[i] += time.perf_counter() - c
            if b is None:
                continue
            iq, plan = b
            amp = 10.0 ** (min(plan.power_db, 0.0) / 20.0)
            self.air.send(i, at, iq * amp, rate=plan.rate, center=plan.center)
            self.bursts += 1
            self.log.append((self.slot, i, plan))
            senders.append(i)
        if senders:
            for i, nd in enumerate(self.nodes):
                if i in senders or not self.alive(i):
                    continue
                y = self.air.capture(i, t0, self.n)
                c = time.perf_counter()
                nd.on_capture(y, t0)
                spent[i] += time.perf_counter() - c
        # A slot with nothing on the air is not captured: its capture would be noise alone, which
        # each node's level gate refuses unread -- skipping it saves the emulator the noise draw.
        for i, nd in enumerate(self.nodes):
            if not self.alive(i):
                continue
            c = time.perf_counter()
            nd.tick(t0)
            spent[i] += time.perf_counter() - c
            self.cpu[i].append(spent[i])
        self.air.clear_before(t0)
        self.slot += 1
        return senders

    def busy(self) -> bool:
        return any(self.alive(i) and (nd.queue or nd.due is not None) for i, nd in enumerate(self.nodes))

    def run(self, max_slots: int = 200, until=None) -> int:
        """Step until ``until()`` holds, nothing is queued anywhere, or ``max_slots``; returns the
        slots run."""
        start = self.slot
        while self.slot - start < max_slots:
            self.step()
            if until is not None and until():
                break
            if until is None and not self.busy():
                break
        return self.slot - start

    def cpu_per_slot(self) -> list[dict]:
        """Per node: mean / max / total CPU seconds per slot."""
        out = []
        for c in self.cpu:
            a = np.asarray(c) if c else np.zeros(1)
            out.append({"mean": float(a.mean()), "max": float(a.max()), "total": float(a.sum()),
                        "slots": len(c)})
        return out


__all__ = ["NodeRun", "slot_samples"]
