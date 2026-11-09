"""
ipnet.py -- tunnel endpoints joined by a lossy message channel, in pure Python.

The tunnel works on whole mesh messages, so it is tested on a model of what the mesh does to them,
not on RF: each message either decodes at a receiver or does not (``loss``, drawn per receiver, so
two gateways lose different messages), may arrive twice (``dup``: two paths both delivered and the
dedupe missed it), and arrives after ``delay`` plus up to ``jitter`` (so messages reorder).  Each
node's radio carries ``rate`` fountain blocks per second at the suggested redundancy, so a node
that is busy transmitting queues what arrives meanwhile -- which is where packing happens.  Nodes
can die (``kill``): no transmission, no reception, and a gateway leaves its backhaul.
"""
from __future__ import annotations

import heapq
import math
import random
from dataclasses import dataclass, field

from ..tunnel import Tunnel


@dataclass
class IpNet:
    loss: float = 0.0
    dup: float = 0.0
    delay: float = 0.02
    jitter: float = 0.0
    rate: float = 200.0                  # blocks per second per radio
    seed: int = 0
    nodes: list = field(default_factory=list)

    def __post_init__(self):
        self.rng = random.Random(self.seed)
        self.dead: set = set()
        self.busy: dict = {}
        self.inbox: dict = {}
        self.blocks_on_air = 0
        self.messages = 0
        self._q: list = []
        self._n = 0
        self.now = 0.0

    def add(self, t: Tunnel) -> Tunnel:
        self.nodes.append(t)
        self.inbox[t.node_id] = []
        return t

    def kill(self, t: Tunnel) -> None:
        self.dead.add(t.node_id)
        if t.backhaul is not None:
            t.backhaul.leave(t)

    def _transmit(self, src: Tunnel, out) -> None:
        self.messages += 1
        on_air = math.ceil(out.blocks(src.cfg.block) * out.redundancy)
        self.blocks_on_air += on_air
        self.busy[src.node_id] = self.now + on_air / self.rate
        for dst in self.nodes:
            if dst is src or dst.node_id in self.dead or out.header.to not in dst.addresses():
                continue
            for _ in range(1 + (self.rng.random() < self.dup)):
                if self.rng.random() < self.loss:
                    continue
                t = self.now + on_air / self.rate + self.delay + self.rng.random() * self.jitter
                self._n += 1
                heapq.heappush(self._q, (t, self._n, dst, out.header, out.sealed))

    def step(self, dt: float) -> None:
        """Advance ``dt`` seconds: deliver what has arrived, then let every idle radio transmit."""
        self.now += dt
        while self._q and self._q[0][0] <= self.now:
            _, _, dst, h, sealed = heapq.heappop(self._q)
            if dst.node_id not in self.dead:
                self.inbox[dst.node_id] += dst.receive(h, sealed, self.now)
        for t in self.nodes:
            if t.backhauled:
                self.inbox[t.node_id] += t.backhauled
                t.backhauled.clear()
        for t in self.nodes:
            if t.node_id in self.dead or self.busy.get(t.node_id, 0.0) > self.now:
                continue
            out = t.pull(self.now)
            if out is not None:
                self._transmit(t, out)

    def run(self, seconds: float, dt: float = 0.002, until=None) -> None:
        end = self.now + seconds
        while self.now < end:
            self.step(dt)
            if until is not None and until():
                return


__all__ = ["IpNet"]
