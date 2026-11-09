"""
tcpproxy.py -- TCP split at the mesh edges.

End-to-end TCP across the mesh would put the mesh's loss and multi-second tails inside every TCP
connection's congestion control: a lost message reads as congestion, the window collapses, and
the RTO backs off to seconds.  So TCP never crosses the mesh.  Each end terminates it locally -- the
drone's proxy accepts the application's connection at once, the gateway's proxy opens the real one
to the destination -- and the byte stream between them rides the tunnel's reliable, ordered class.
Loss is repaired by the tunnel's selective retransmission on a mesh-sized timer, and each local TCP
connection sees a fast, clean LAN.

One connection = one tunnel flow per direction (its own sequence space), so a stalled connection
never blocks another.  What crosses the mesh is a segment, an application record of the tunnel:

    u8  type     bits 3-0: 1 OPEN, 2 DATA, 3 FIN, 4 RST     bit 7 I: the sender initiated it
    v   conn     the initiator's connection id
    OPEN         u8 4|6, address (4|16 bytes), u16 port, then the first data (like TCP Fast Open:
                 a request goes out with the open, not one round trip later)
    DATA         bytes        FIN  nothing        RST  nothing

A connection is keyed (initiator node, conn id), so either end may open one.  There is no open
handshake over the mesh: the far proxy connects on OPEN and answers RST only if it cannot.  State is
minimal -- addresses, two flags, a timestamp -- and goes when both FINs have crossed, on RST, when
the tunnel gives up a segment (a hole in a stream cannot be repaired), or after ``idle`` silence.

After an anycast failover a different gateway receives the stream.  If the OPEN was still
unacknowledged it is still in the retransmission, and the connection simply opens there; if not,
the new gateway has no connection for the stream and answers RST, so the application sees a reset
rather than a hang.  (A proxied connection is only as alive as the gateway holding its far end;
datagram traffic fails over without loss.)

The proxy is a pure state machine: local events in (``local_open``, ``local_data``, ``local_fin``,
``local_reset``), tunnel deliveries in (``on_delivery``), and a list of actions out for whatever
owns the sockets (``Connect``, ``Write``, ``Close``, ``Reset``).  No sockets are touched here.
"""
from __future__ import annotations

import ipaddress
from dataclasses import dataclass, field

from .tunnel import Delivery, Tunnel, _vdec, _venc

OPEN, DATA, FIN, RST = 1, 2, 3, 4
_I = 0x80


@dataclass(frozen=True)
class Segment:
    type: int
    conn: int
    initiator: bool
    data: bytes = b""
    addr: object = None                  # OPEN: ipaddress.IPv4Address / IPv6Address
    port: int = 0

    def pack(self) -> bytes:
        out = [bytes([self.type | (_I if self.initiator else 0)]), _venc(self.conn)]
        if self.type == OPEN:
            a = ipaddress.ip_address(self.addr)
            out.append(bytes([a.version]) + a.packed + self.port.to_bytes(2, "big"))
        out.append(self.data)
        return b"".join(out)

    @classmethod
    def unpack(cls, buf: bytes) -> "Segment":
        if not buf:
            raise ValueError("empty segment")
        t, init = buf[0] & 0x0F, bool(buf[0] & _I)
        if t not in (OPEN, DATA, FIN, RST):
            raise ValueError(f"segment type {t}")
        conn, at = _vdec(buf, 1)
        addr, port = None, 0
        if t == OPEN:
            v = buf[at]
            n = {4: 4, 6: 16}.get(v)
            if n is None or at + 1 + n + 2 > len(buf):
                raise ValueError("bad OPEN address")
            addr = ipaddress.ip_address(bytes(buf[at + 1:at + 1 + n]))
            port = int.from_bytes(buf[at + 1 + n:at + 3 + n], "big")
            at += 3 + n
        return cls(t, conn, init, bytes(buf[at:]), addr, port)


# ── actions for whatever owns the sockets ────────────────────────────────────────────────────

@dataclass(frozen=True)
class Connect:
    key: tuple
    addr: object
    port: int


@dataclass(frozen=True)
class Write:
    key: tuple
    data: bytes


@dataclass(frozen=True)
class Close:
    """The peer finished sending: shut down the local socket's write side."""
    key: tuple


@dataclass(frozen=True)
class Reset:
    key: tuple


@dataclass
class Conn:
    """One proxied connection.  ``key`` = (initiator node, conn id); ``peer`` = the mesh address
    its segments go to; ``flow`` = this end's tunnel flow for it."""
    key: tuple
    peer: bytes
    flow: int
    initiator: bool
    addr: object = None
    port: int = 0
    sent_fin: bool = False
    got_fin: bool = False
    touched: float = 0.0


@dataclass
class TcpProxy:
    tunnel: Tunnel
    idle: float = 120.0                  # silent this long -> reset
    segment: int = 1200                  # bytes of stream per segment (fits one tunnel message)
    window: int = 64                     # unacked segments before ``writable`` says stop reading
    conns: dict = field(default_factory=dict)

    @property
    def me(self) -> bytes:
        return self.tunnel.node_id

    # ── local side ──

    def local_open(self, addr, port: int, now: float, data: bytes = b"", to: bytes | None = None) -> tuple:
        """A local application connected to ``addr:port`` (the drone's transparent proxy accepted
        it, or a gateway is opening a connection toward a drone).  Returns the connection key."""
        addr = ipaddress.ip_address(addr)
        if to is None:
            to = self.tunnel.routes.lookup(addr, now)
            if to is None:
                raise LookupError(f"no mesh route to {addr}")
        flow = self.tunnel.new_flow()
        key = (self.me, flow)
        c = self.conns[key] = Conn(key, bytes(to), flow, True, addr, int(port), touched=now)
        first, rest = bytes(data[:self.segment]), bytes(data[self.segment:])
        self._send(c, Segment(OPEN, flow, True, first, addr, int(port)), now)
        if rest:
            self.local_data(key, rest, now)
        return key

    def local_data(self, key, data: bytes, now: float) -> None:
        c = self.conns.get(key)
        if c is None or c.sent_fin:
            return
        for i in range(0, len(data), self.segment):
            self._send(c, Segment(DATA, key[1], c.initiator, bytes(data[i:i + self.segment])), now)

    def local_fin(self, key, now: float) -> list:
        c = self.conns.get(key)
        if c is None or c.sent_fin:
            return []
        c.sent_fin = True
        self._send(c, Segment(FIN, key[1], c.initiator), now)
        return self._maybe_done(c)

    def local_reset(self, key, now: float) -> None:
        """The local socket failed or was reset (on a gateway: the connect was refused)."""
        c = self.conns.pop(key, None)
        if c is not None:
            self._send(c, Segment(RST, key[1], c.initiator), now)

    def writable(self, key) -> bool:
        """Backpressure: False while this connection has ``window`` segments unacknowledged -- the
        socket owner stops reading the local socket, and local TCP flow control does the rest."""
        c = self.conns.get(key)
        f = self.tunnel.sending.get(c.flow) if c else None
        return c is not None and (f is None or len(f.unacked) < self.window)

    def _send(self, c: Conn, s: Segment, now: float) -> None:
        c.touched = now
        self.tunnel.send(s.pack(), now, to=c.peer, reliable=True, ordered=True, flow=c.flow, app=True,
                         urgent=s.type in (OPEN, FIN, RST))

    # ── mesh side ──

    def on_delivery(self, d: Delivery, now: float) -> list:
        """A tunnel delivery; non-application ones are ignored.  Returns socket actions."""
        if not d.app:
            return []
        try:
            s = Segment.unpack(d.payload)
        except (ValueError, IndexError):
            return []
        key = (d.origin, s.conn) if s.initiator else (self.me, s.conn)
        c = self.conns.get(key)
        if s.type == OPEN:
            if c is not None:
                return []                    # a repeat of an OPEN already acted on
            c = self.conns[key] = Conn(key, d.origin, self.tunnel.new_flow(), False, s.addr, s.port,
                                       touched=now)
            out = [Connect(key, s.addr, s.port)]
            if s.data:
                out.append(Write(key, s.data))
            return out
        if c is None:
            if s.type != RST:                # a stream we do not hold (lost state, or failover)
                self.tunnel.send(Segment(RST, s.conn, not s.initiator).pack(), now, to=d.origin,
                                 reliable=True, ordered=True, flow=self.tunnel.new_flow(), app=True,
                                 urgent=True)
            return []
        c.touched = now
        c.peer = d.origin if not c.initiator else c.peer
        if s.type == DATA:
            return [Write(key, s.data)] if s.data else []
        if s.type == FIN:
            c.got_fin = True
            return [Close(key)] + self._maybe_done(c)
        self.conns.pop(key, None)
        return [Reset(key)]

    def _maybe_done(self, c: Conn) -> list:
        if c.sent_fin and c.got_fin:
            self.conns.pop(c.key, None)
        return []

    def poll(self, now: float) -> list:
        """Timeouts: connections idle too long, or whose stream the tunnel gave up on, are reset at
        both ends (the RST to the peer is best effort -- its own idle timer covers it if lost)."""
        out = []
        broken = self.tunnel.broken
        for key, c in list(self.conns.items()):
            if c.flow in broken or now - c.touched > self.idle:
                self.local_reset(key, now)
                out.append(Reset(key))
        broken.clear()
        return out


def state_size(c: Conn) -> int:
    """Bytes of essential per-connection state on the wire-format scale: key (8 + 2), peer (8),
    flow (2), address (16) + port (2), flags (1), timestamp (4)."""
    return 8 + 2 + 8 + 2 + 16 + 2 + 1 + 4


__all__ = ["OPEN", "DATA", "FIN", "RST", "Segment", "Connect", "Write", "Close", "Reset", "Conn",
           "TcpProxy", "state_size"]
