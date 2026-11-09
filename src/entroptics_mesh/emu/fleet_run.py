"""
fleet_run.py -- a moving fleet of node runtimes over the emulated air, recorded slot by slot.

The same contract as :class:`~entroptics_mesh.emu.node_run.NodeRun` -- the driver is only each
node's radio; every node decides what to read, relay and send from its own measurements -- with
what a mission needs on top:

  * **Nodes in worker processes.**  Each worker owns a group of :class:`~entroptics_mesh.node.Node`
    objects (gateways sharing a backhaul go in one group, so the wire stays one object).  A slot
    is two round trips: every live node is asked for its burst, the air mixes them, and every
    live node that did not transmit reads its capture and ticks.  The reads -- nearly all of the
    cost -- run in parallel across the workers.
  * **Motion and epochs.**  The scenario places every radio per epoch.  The air holds the true
    positions; ``node.position`` is what the node's own GPS says, which the scenario may spoof.
    Mission time jumps between epochs (as ``research/scenarios/fleet_departure.py`` does): slots
    are milliseconds, a flight is minutes, and a Python node reads ~100-300x slower than real
    time.  Positions hold still within an epoch.
  * **Orders.**  Application traffic enters at a node at a slot: a telemetry record
    (``node.send``), an IP packet (``node.tunnel.send``), or an "I'm here" now (the node's own
    announcement timer, phased by the driver, since a jump in mission time would otherwise fire
    every node's timer in the same slot).
  * **Recording.**  Per slot: each burst (sender, plan, the symbol it carried), each frame a node
    took, whom it named as transmitter and the SNR it measured that burst at, deliveries, relay decisions (forwarding started, passive
    acks, sibling-share cancellations, budget expiries), and changes to each node's distrusted set.

Nothing here is a protocol: the workers call the node's public calls (``next_burst``, or
``start``/``commit`` when the radio listens before talking; ``on_capture``, ``tick``, ``send``), and
name each burst in the record by the symbol the node says it sent (``last_sent``).
"""
from __future__ import annotations

import multiprocessing as mp
import time
from collections import Counter
from dataclasses import dataclass, field, fields

import numpy as np

from .air import Air
from .node_run import slot_samples
from ..node import Node, NodeStats
from ..routing import HOME

_STAT_KEYS = [f.name for f in fields(NodeStats)]


def _hex(b) -> str:
    return bytes(b).hex() if isinstance(b, (bytes, bytearray)) else str(b)


def _queued(nd: Node) -> Counter:
    return Counter((q.header.origin, q.header.seq) for q in nd.queue if q.header.origin != nd.node_id)


def _heard(nd: Node, who) -> float | None:
    """The SNR ``nd`` last measured ``who`` at (its own read of the burst), rounded."""
    n = nd.table.get(who) if who is not None else None
    m = n.measured if n is not None else None
    return None if m is None or m.snr_db is None else round(float(m.snr_db), 2)


def _stats(nd: Node) -> dict:
    return {k: getattr(nd.stats, k) for k in _STAT_KEYS}


class _Host:
    """One worker's nodes and what it remembers between calls (for deltas)."""

    def __init__(self, nodes: dict[int, Node]):
        self.nodes = nodes
        self.last = {i: _stats(nd) for i, nd in nodes.items()}
        self.inbox = {i: 0 for i in nodes}
        self.ip = {i: 0 for i in nodes}
        self.distrusted = {i: frozenset() for i in nodes}

    def _apply(self, t, claims, orders):
        for i, (pos, vel) in claims.items():
            self.nodes[i].position, self.nodes[i].velocity = pos, vel
        for i, kind, payload in orders:
            nd = self.nodes[i]
            if kind == "send":
                nd.send(payload, HOME, t)
            elif kind == "ip":
                nd.tunnel.send(payload, t, to=HOME)
            elif kind == "announce":
                nd._next_announce = t

    def bursts(self, t, alive, claims, orders):
        """Every live node's burst, sent at the top of the slot without listening."""
        self._apply(t, claims, orders)
        return [b for b in (self._burst(i, t) for i in alive) if b is not None]

    def starts(self, t, alive, claims, orders):
        """Listen before talk, first half: {node: start within the slot (s)} for each node with a
        burst due (``Node.start``)."""
        self._apply(t, claims, orders)
        out = {}
        for i in alive:
            s = self.nodes[i].start(t)
            if s is not None:
                out[i] = s
        return out

    def commit(self, t, i, heard):
        """Second half: node ``i`` hears ``heard`` before its start and sends or defers."""
        return self._burst(i, t, heard, listened=True)

    def _emit(self, nd, t, heard, listened):
        """What goes on the air: (iq, plan) or None."""
        b = nd.commit(t, heard) if listened else nd.next_burst(t)
        return None if b is None else (np.asarray(b[0], np.complex64), b[1])

    def _burst(self, i, t, heard=None, listened=False):
        nd = self.nodes[i]
        cpu = time.process_time()
        b = self._emit(nd, t, heard, listened)
        cpu = time.process_time() - cpu
        if b is None:
            return None
        iq, plan = b
        head = nd.last_sent                          # the symbol that went (interleaving picks it)
        h = head.header
        return dict(i=i, iq=iq, plan=plan, cpu=cpu,
                    origin=_hex(h.origin), seq=int(h.seq), to=_hex(h.to), hops=int(h.hops),
                    sid=int(head.symbol_id), k=int(head.k), own=h.origin == nd.node_id)

    def receive(self, t, alive, captures):
        rep = {}
        for i in alive:
            nd = self.nodes[i]
            r = {}
            cpu = time.process_time()
            if i in captures:
                before = _queued(nd)
                taken = self._hear(nd, captures[i], t)
                if taken:
                    r["took"] = [dict(origin=_hex(s.header.origin), seq=int(s.header.seq),
                                      hops=int(s.header.hops), sid=int(s.symbol_id),
                                      tx=_hex(nd._transmitter(s) or b""), to=_hex(s.header.to),
                                      snr=_heard(nd, nd._transmitter(s)))
                                 for s in taken]
                fwd = _queued(nd) - before
                if fwd:
                    r["forward"] = [dict(origin=_hex(o), seq=int(s), n=int(n)) for (o, s), n in fwd.items()]
            nd.tick(t)
            r["cpu"] = time.process_time() - cpu
            now = _stats(nd)
            d = {k: now[k] - self.last[i][k] for k in _STAT_KEYS if now[k] != self.last[i][k]}
            self.last[i] = now
            if d:
                r["stats"] = d
            if len(nd.inbox) > self.inbox[i]:
                r["inbox"] = [dict(origin=_hex(m.origin), t=float(m.t), payload=bytes(m.payload))
                              for m in nd.inbox[self.inbox[i]:]]
                self.inbox[i] = len(nd.inbox)
            if len(nd.ip_out) > self.ip[i]:
                r["ip"] = [dict(origin=_hex(x.origin), flow=int(x.flow), payload=bytes(x.payload))
                           for x in nd.ip_out[self.ip[i]:]]
                self.ip[i] = len(nd.ip_out)
            ds = frozenset(nd.distrusted)
            if ds != self.distrusted[i]:
                r["distrusted"] = sorted(_hex(x) for x in ds)
                self.distrusted[i] = ds
            r["queue"] = len(nd.queue)
            rep[i] = r
        return rep

    def _hear(self, nd, capture, t):
        return nd.on_capture(capture, t)

    def view(self, i, relay_alt, parallel):
        """What node ``i`` believes: announced positions, links it would use, who is cut off from
        home, the relay slots that would reconnect them, and its verdicts on position claims."""
        nd = self.nodes[i]
        pos, heard = nd.link_map()
        cut = nd.cut_off()
        plan = nd.relay_plan(relay_alt=relay_alt, parallel=parallel) if cut else {}
        verdicts = {_hex(n): dict(ok=bool(ok), excess_m=float(v.excess_m), fast=bool(fast))
                    for n, (ok, v, fast) in nd.verdicts.items()}
        return dict(
            positions={_hex(n): (p.lat, p.lon, p.alt) for n, p in pos.items()},
            heard=[(_hex(a), _hex(b), float(s)) for (a, b), s in heard.items()],
            cut_off=sorted(_hex(n) for n in cut),
            plan={_hex(n): [[(p.lat, p.lon, p.alt) for p in slot] for slot in slots]
                  for n, slots in plan.items()},
            distrusted=sorted(_hex(n) for n in nd.distrusted),
            verdicts=verdicts)


def _serve(conn, nodes):
    host = _Host(nodes)
    while True:
        msg = conn.recv()
        cmd = msg[0]
        if cmd == "stop":
            conn.send(None)
            return
        try:
            conn.send(("ok", getattr(host, cmd)(*msg[1:])))
        except Exception as e:                       # report, keep serving
            import traceback
            conn.send(("err", f"{e!r}\n{traceback.format_exc()}"))


@dataclass
class FleetRun:
    """N nodes in ``groups`` (lists of node indices; each group one worker) over ``air``.
    ``nodes[i]`` transmits from air radio ``i``.  ``workers=0`` runs every group in-process."""
    air: Air
    nodes: list[Node]
    groups: list[list[int]]
    workers: bool = True
    listen: bool = True                  # listen before talk (Node.start / Node.commit)
    slot_s: float = field(init=False)

    def __post_init__(self):
        self.n = slot_samples(self.air.rate, min(nd.radio.min_rate for nd in self.nodes),
                              families={f for nd in self.nodes for f in nd.families})
        if self.listen:
            # room for the latest start any node may draw (its contention window, its own clock)
            self.n += int(np.ceil(max(nd.contention_s for nd in self.nodes) * self.air.rate))
        self.slot_s = self.n / self.air.rate
        for nd in self.nodes:
            nd.slot_s = self.slot_s
        self.dead: set[int] = set()
        self.owner = {i: g for g, grp in enumerate(self.groups) for i in grp}
        self._conns, self._procs, self._local = [], [], []
        for grp in self.groups:
            nodes = {i: self.nodes[i] for i in grp}
            if self.workers:
                a, b = mp.Pipe()
                p = mp.get_context("spawn").Process(target=_serve, args=(b, nodes), daemon=True)
                p.start()
                self._conns.append(a)
                self._procs.append(p)
            else:
                self._local.append(_Host(nodes))
        self.bursts = 0

    # ── plumbing ──

    def _call_all(self, per_group):
        """Send each group its call, then collect -- the groups run in parallel."""
        if not self.workers:
            return [getattr(self._local[g], c[0])(*c[1:]) for g, c in enumerate(per_group)]
        for conn, c in zip(self._conns, per_group):
            conn.send(c)
        out = []
        for conn in self._conns:
            status, val = conn.recv()
            if status != "ok":
                raise RuntimeError(val)
            out.append(val)
        return out

    def _call(self, g, *c):
        if not self.workers:
            return getattr(self._local[g], c[0])(*c[1:])
        self._conns[g].send(c)
        status, val = self._conns[g].recv()
        if status != "ok":
            raise RuntimeError(val)
        return val

    def close(self):
        for c in self._conns:
            c.send(("stop",))
            c.recv()
        for p in self._procs:
            p.join(timeout=10)

    # ── one slot ──

    def alive(self, i: int) -> bool:
        return i not in self.dead

    def step(self, t: float, orders=(), claims=None) -> dict:
        """One slot at mission time ``t``.  ``orders`` [(node, kind, payload)], ``claims``
        {node: (Position, velocity)} -- what each node's GPS now says.  Returns the slot's record."""
        claims = claims or {}
        G = range(len(self.groups))
        alive = [[i for i in self.groups[g] if self.alive(i)] for g in G]
        calls = [(t, alive[g], {i: c for i, c in claims.items() if self.owner[i] == g},
                  [o for o in orders if self.owner[o[0]] == g]) for g in G]
        txs, deferred = [], []
        sent = []

        def put(b, start):
            p = b["plan"]
            amp = 10.0 ** (min(p.power_db, 0.0) / 20.0)
            self.air.send(b["i"], t + start + 100 / self.air.rate, b["iq"] * amp, rate=p.rate, center=p.center)
            self.bursts += 1
            sent.append(b)
            txs.append(dict(node=b["i"], rate=float(p.rate), center=float(p.center), power_db=float(p.power_db),
                            family=p.family, M=int(p.M), code=p.code_rate, origin=b["origin"], seq=b["seq"],
                            to=b["to"], hops=b["hops"], sid=b["sid"], k=b["k"], own=b["own"],
                            cpu=round(b["cpu"], 4), start=round(start, 7)))

        if not self.listen:
            for grp in self._call_all([("bursts",) + c for c in calls]):
                for b in grp:
                    put(b, 0.0)
        else:
            # Each node with a burst due drew its own start; in start order, each listens to what is
            # already on the air over the listen before its start (the air holds every burst begun
            # earlier, delayed by its propagation), then sends or defers -- its own decision.
            starts = sorted((s, i) for grp in self._call_all([("starts",) + c for c in calls])
                            for i, s in grp.items())
            for s, i in starts:
                nd_listen = self.nodes[i].listen_s
                m = max(1, int(round(nd_listen * self.air.rate)))
                t0 = t + s + 100 / self.air.rate - m / self.air.rate
                heard = self.air.capture(i, t0, m)
                b = self._call(self.owner[i], "commit", t, i, heard)
                if b is None:
                    deferred.append(i)
                else:
                    put(b, s)
        senders = {b["i"] for b in sent}
        caps = [{} for _ in G]
        if senders:
            for i in range(len(self.nodes)):
                if i in senders or not self.alive(i):
                    continue
                caps[self.owner[i]][i] = self.air.capture(i, t, self.n)
        reps = self._call_all([("receive", t, alive[g], caps[g]) for g in G])
        self.air.clear_before(t)
        nodes = {}
        for r in reps:
            nodes.update(r)
        out = dict(t=t, tx=txs, nodes=nodes)
        if deferred:
            out["deferred"] = deferred
        return out

    def view(self, i: int, relay_alt: float = 400.0, parallel: int = 2) -> dict:
        return self._call(self.owner[i], "view", i, relay_alt, parallel)


__all__ = ["FleetRun"]
