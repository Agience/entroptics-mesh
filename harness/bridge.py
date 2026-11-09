"""
Physical/virtual node bridge -- a DISTRIBUTED NodeRun.

The emulated air (Air + the slot clock) lives on one coordinator host. Each node is an *endpoint*:
  * LocalEndpoint  -- a Node object in-process (a virtual node on the coordinator).
  * RemoteEndpoint -- a Node running on another host (a physical Pi), reached over a socket; the
                      per-slot calls (next_burst / on_capture / tick) and their IQ are pickled across.
Physical and virtual nodes therefore share ONE emulated air. The transport is transparent: a run with socket-remote endpoints must deliver
bit-identically to the all-local run.

Entroptics-agnostic: the bridge moves IQ and calls the Node's existing per-slot methods; it changes
nothing in the stack. This module imports the stack (harness -> mesh), never the reverse.

M1 drives the ``listen=False`` path (next_burst / on_capture / tick -- the simplest faithful slot
protocol); listen-before-talk (start/commit) is a mechanical follow-on.
"""
from __future__ import annotations

import pickle
import socket
import struct

import numpy as np

from entroptics_mesh.emu.node_run import slot_samples

# ── length-prefixed pickle framing ────────────────────────────────────────────────────────────

def _recvn(sock: socket.socket, n: int) -> bytes:
    buf = bytearray()
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("peer closed")
        buf += chunk
    return bytes(buf)


def send_msg(sock: socket.socket, obj) -> None:
    b = pickle.dumps(obj, protocol=pickle.HIGHEST_PROTOCOL)
    sock.sendall(struct.pack(">Q", len(b)) + b)


def recv_msg(sock: socket.socket):
    (n,) = struct.unpack(">Q", _recvn(sock, 8))
    return pickle.loads(_recvn(sock, n))


# ── the worker that runs on the remote side (the Pi) ──────────────────────────────────────────

def connect(host: str, port: int, timeout: float = 30.0) -> "RemoteEndpoint":
    """Air-server side (the coordinator): open a RemoteEndpoint to a node listening on a Pi."""
    s = socket.create_connection((host, int(port)), timeout=timeout)
    s.settimeout(None)
    s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)       # per-slot RPC: don't Nagle-delay
    return RemoteEndpoint(s)


def serve_listening(node, host: str = "0.0.0.0", port: int = 0) -> int:
    """Pi side: listen, accept ONE air-server connection, serve ``node`` until closed.  Returns the
    bound port (useful with port=0).  Blocks in ``serve`` once connected."""
    lsock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    lsock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    lsock.bind((host, int(port)))
    lsock.listen(1)
    bound = lsock.getsockname()[1]
    print(f"pi_node: listening on {host}:{bound}", flush=True)
    try:
        conn, _ = lsock.accept()
        conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        serve(node, conn)
    finally:
        lsock.close()
    return bound


def serve(node, conn: socket.socket) -> None:
    """Run ``node`` answering RPCs on ``conn`` until told to close.  This is what a physical Pi runs
    (its one Node); the per-slot IQ arrives/leaves as pickled numpy arrays."""
    while True:
        try:
            cmd, *args = recv_msg(conn)
        except (ConnectionError, EOFError):
            return
        if cmd == "close":
            send_msg(conn, "bye")
            return
        if cmd == "next_burst":
            r = node.next_burst(*args)
        elif cmd == "start":
            r = node.start(*args)
        elif cmd == "commit":
            r = node.commit(*args)
        elif cmd == "on_capture":
            node.on_capture(*args); r = None
        elif cmd == "tick":
            node.tick(*args); r = None
        elif cmd == "announcement":
            r = node.announcement(*args)
        elif cmd == "on_announcement":
            node.on_announcement(*args); r = None
        elif cmd == "learn":
            node.learn(*args); r = None
        elif cmd == "send":
            r = node.send(*args)
        elif cmd == "get":
            r = getattr(node, args[0])
        elif cmd == "set":
            setattr(node, args[0], args[1]); r = None
        else:
            r = ("ERR", f"unknown {cmd!r}")
        send_msg(conn, r)


# ── endpoints the server drives ───────────────────────────────────────────────────────────────

class LocalEndpoint:
    """A virtual node: the Node object itself, in-process."""
    def __init__(self, node):
        self.node = node

    def next_burst(self, t):          return self.node.next_burst(t)
    def start(self, t):               return self.node.start(t)
    def commit(self, t, iq):          return self.node.commit(t, iq)
    def on_capture(self, iq, t):      self.node.on_capture(iq, t)
    def tick(self, t):                self.node.tick(t)
    def announcement(self, t):        return self.node.announcement(t)
    def on_announcement(self, a, w, t): self.node.on_announcement(a, w, t)
    def learn(self, g):               self.node.learn(g)
    def send(self, payload, to, t):   return self.node.send(payload, to, t)
    def get(self, attr):              return getattr(self.node, attr)
    def set(self, attr, v):           setattr(self.node, attr, v)


class RemoteEndpoint:
    """A physical node, reached over a socket; mirrors LocalEndpoint's interface via RPC."""
    def __init__(self, conn: socket.socket):
        self.conn = conn

    def _call(self, *msg):
        send_msg(self.conn, msg)
        return recv_msg(self.conn)

    def next_burst(self, t):          return self._call("next_burst", float(t))
    def start(self, t):               return self._call("start", float(t))
    def commit(self, t, iq):          return self._call("commit", float(t), iq)
    def on_capture(self, iq, t):      self._call("on_capture", iq, float(t))
    def tick(self, t):                self._call("tick", float(t))
    def announcement(self, t):        return self._call("announcement", float(t))
    def on_announcement(self, a, w, t): self._call("on_announcement", a, w, float(t))
    def learn(self, g):               self._call("learn", g)
    def send(self, payload, to, t):   return self._call("send", payload, to, float(t))
    def get(self, attr):              return self._call("get", attr)
    def set(self, attr, v):           self._call("set", attr, v)
    def close(self):                  self._call("close")


# ── the distributed run (listen=False path) ───────────────────────────────────────────────────

class DistributedAirRun:
    """Like NodeRun, but each node is an endpoint (local or remote).  endpoints[i] transmits from air
    radio i; the server owns the Air and the slot clock.  Mirrors NodeRun.step for both access modes:
    ``listen=True`` is listen-before-talk (each node draws a start in its contention window via
    ``start``, the server plays it the listen window, then ``commit`` sends or defers) -- the faithful
    mesh behavior; ``listen=False`` is the simpler all-at-slot-top path."""
    def __init__(self, air, endpoints, *, min_rate, families, listen=True):
        self.air = air
        self.eps = list(endpoints)
        self.listen = bool(listen)
        self.n = slot_samples(air.rate, min_rate, families=families)
        if self.listen:
            cs = [e.get("contention_s") for e in self.eps]
            self.n += int(np.ceil(max(cs) * air.rate))              # room for the latest start drawn
            self.listen_s = [e.get("listen_s") for e in self.eps]
        self.slot_s = self.n / air.rate
        for e in self.eps:
            e.set("slot_s", self.slot_s)
        self.slot = 0
        self.bursts = 0
        self.senders_seen: set[int] = set()        # endpoint indices that ever transmitted

    def now(self):
        return self.slot * self.slot_s

    def _emit(self, i, at):
        e = self.eps[i]
        if self.listen:
            m = max(1, int(round(self.listen_s[i] * self.air.rate)))
            b = e.commit(self.now(), self.air.capture(i, at - m / self.air.rate, m))
        else:
            b = e.next_burst(self.now())
        if b is None:
            return False
        iq, plan = b
        amp = 10.0 ** (min(plan.power_db, 0.0) / 20.0)
        self.air.send(i, at, iq * amp, rate=plan.rate, center=plan.center)
        self.bursts += 1
        return True

    def step(self):
        t0 = self.now()
        if self.listen:
            starts = [(s, i) for i, e in enumerate(self.eps) if (s := e.start(t0)) is not None]
        else:
            starts = [(0.0, i) for i in range(len(self.eps))]
        senders = []
        for s, i in sorted(starts):
            if self._emit(i, t0 + s + 100 / self.air.rate):
                senders.append(i)
        self.senders_seen.update(senders)
        if senders:
            for i, e in enumerate(self.eps):
                if i not in senders:
                    e.on_capture(self.air.capture(i, t0, self.n), t0)
        for e in self.eps:
            e.tick(t0)
        self.air.clear_before(t0)
        self.slot += 1

    def run(self, max_slots=120, until=None):
        for _ in range(int(max_slots)):
            self.step()
            if until is not None and until():
                break
        return self.slot


__all__ = ["send_msg", "recv_msg", "serve", "serve_listening", "connect",
           "LocalEndpoint", "RemoteEndpoint", "DistributedAirRun"]
