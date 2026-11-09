"""
tunnel.py -- IP over the mesh: packets in, packets out, nothing required back.

A raw IP packet (v4 or v6, opaque bytes) becomes a *record* inside a mesh message; the message is
sealed by the crypto provider and goes out as fountain symbols like any other.  The tunnel adds
three things the mesh does not have, and no more:

    addressing    an IP prefix -> mesh address map, longest prefix wins.  Entries are configured at
                  the node that owns them (a gateway's default route, a drone's own LAN) and
                  carried to everyone else in a ROUTES record; a gateway may also *glean* a
                  drone's source address from the traffic it receives.  Nothing is pre-shared:
                  a drone with an empty map knows nothing until a gateway announces itself.
                  ``0/0 -> HOME`` makes "home" whichever gateway hears (anycast).
    packing       records bound for one mesh address share one message.  A frame costs the same
                  whether its block is full or not, so eight 60-byte packets in one block cost one
                  frame instead of eight.  Packing never holds a packet while the radio is idle:
                  ``pull`` is called when the radio can take a message and packs whatever has
                  queued since -- aggregation is what accumulates behind the transmission ahead,
                  which costs no latency.  When the backlog exceeds one message (``max_blocks``),
                  the message ends where its last block is fullest, and the record that would have
                  spilled into a nearly empty block leads the next one.  (``hold`` > 0 lets a lone
                  small packet wait for company; default 0.)  A packet larger than one message is
                  cut into FRAG records across
                  messages, never IP-fragmented; one larger than ``mtu`` is refused, so the local
                  stack's path-MTU discovery sees the tunnel's real limit.
    two classes   best-effort (UDP, video, anything the end hosts already protect): no state, no
                  ack, fountain redundancy only; a packet still queued past its deadline is
                  dropped, and a ``newest`` flow lets the receiver drop anything older than what it
                  already delivered.  Reliable (opt-in per flow): a 16-bit sequence number, a tiny
                  cumulative + selective ACK from the receiver, retransmission of what the ACK shows
                  missing (or what a short timer gives up on), and nothing kept once a flow is idle.

The reliable class is the only place anything comes back, and it is never needed for delivery:
best-effort traffic and the mesh itself run with no reply at all.  Its state is built to survive
"anything can happen":

    * every data record carries the sender's *low-water mark* (oldest seq it still holds), so a
      receiver that never saw the start of a flow, lost its state, or is a different gateway than
      before (anycast failover) synchronizes from any one record -- no handshake;
    * a sender that gives up on a record (past its deadline) just advances the low-water mark, and
      the receiver stops waiting for it;
    * initial sequence numbers are random, so a rebooted sender is a new epoch, not a duplicate;
    * an ACK is cumulative and carries a 64-seq bitmap, so any one surviving ACK says everything,
      and an out-of-order arrival triggers one at once (the NACK);
    * ACKs piggyback on any message going to the same node, and go alone only after ``ack_delay``.

Gateways share a wire (``Backhaul``): each accepts HOME, the first to decode a message hands it to
the gateway anchoring that drone's flows, and a duplicate decoded elsewhere is dropped.  If the
anchor dies the next gateway to hear the drone becomes its anchor and resynchronizes from the
low-water mark.

Wire format, before sealing (big-endian; ``v`` = unsigned LEB128 varint):

    message   u8 version<<4, then records until the end
    record    u8 tag      bits 7-6 kind (0 PKT, 1 FRAG, 2 ACK, 3 CTRL)
                          bit 5 R reliable   bit 4 S seq present (best-effort "newest")
                          bit 3 O ordered    bit 2 L length follows (absent on the last record)
                          bit 1 A application stream (not an IP packet)   bit 0 U urgent
              v  flow     sender-chosen flow id (CTRL: subtype)
              PKT/FRAG    [u16 seq, v (seq - low) if R | u16 seq if S]
              FRAG        u16 packet id, u8 index, u8 count
              ACK         u16 cum (next seq expected); body = bitmap, bit i = cum+1+i received
              [v length]  body

A best-effort packet therefore costs 2 bytes of tunnel header (tag, flow) when it is the last
record in its message and 3-4 otherwise; a reliable one 5-7.  The sealing context binds the mesh
header (origin, seq, to), so a real AEAD provider rejects a message replayed under another header.
"""
from __future__ import annotations

import ipaddress
import math
import random
from collections import OrderedDict, deque
from dataclasses import dataclass, field

from .link import _SYM
from .providers import NullCrypto
from .routing import BROADCAST, HOME, FloodRouter, Header

VERSION = 1
FRAME = 512                              # protected frame bytes per fountain symbol (emu.link_run)
BLOCK = FRAME - 2 - _SYM.size            # fountain block that fits in a frame: 477

PKT, FRAG, ACK, CTRL = 0, 1, 2, 3
_R, _S, _O, _L, _A, _U = 0x20, 0x10, 0x08, 0x04, 0x02, 0x01
CTRL_ROUTES = 1

_HALF = 0x8000


def _sdiff(a: int, b: int) -> int:
    """Serial difference a - b on 16-bit sequence numbers (RFC 1982): negative if a is before b."""
    d = (a - b) & 0xFFFF
    return d - 0x10000 if d >= _HALF else d


# ── varints ──────────────────────────────────────────────────────────────────────────────────

def _venc(n: int) -> bytes:
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        if n:
            out.append(b | 0x80)
        else:
            out.append(b)
            return bytes(out)


def _vdec(buf: bytes, at: int) -> tuple[int, int]:
    n = shift = 0
    while True:
        if at >= len(buf) or shift > 35:
            raise ValueError("truncated varint")
        b = buf[at]
        at += 1
        n |= (b & 0x7F) << shift
        shift += 7
        if not b & 0x80:
            return n, at


# ── records ──────────────────────────────────────────────────────────────────────────────────

@dataclass
class Record:
    """One item in a tunnel message.  ``seq``/``low`` are set for reliable records (``low`` = the
    sender's low-water mark when the record went out); ``frag`` = (packet id, index, count)."""
    kind: int
    flow: int = 0
    body: bytes = b""
    reliable: bool = False
    newest: bool = False
    ordered: bool = False
    app: bool = False
    urgent: bool = False
    seq: int | None = None
    low: int | None = None
    frag: tuple[int, int, int] | None = None

    def pack(self, last: bool) -> bytes:
        k = self.kind
        tag = (k << 6) | (_R if self.reliable else 0) | (_S if self.newest and not self.reliable else 0) \
            | (_O if self.ordered else 0) | (0 if last else _L) | (_A if self.app else 0) \
            | (_U if self.urgent else 0)
        out = [bytes([tag]), _venc(self.flow)]
        if k in (PKT, FRAG):
            if self.reliable:
                out += [self.seq.to_bytes(2, "big"), _venc((self.seq - self.low) & 0xFFFF)]
            elif self.newest:
                out.append(self.seq.to_bytes(2, "big"))
            if k == FRAG:
                p, i, n = self.frag
                out.append(p.to_bytes(2, "big") + bytes([i, n]))
        elif k == ACK:
            out.append(self.seq.to_bytes(2, "big"))
        if not last:
            out.append(_venc(len(self.body)))
        out.append(self.body)
        return b"".join(out)

    def size(self, last: bool = False) -> int:
        return len(self.pack(last))


def pack_message(records: list[Record]) -> bytes:
    n = len(records)
    return bytes([VERSION << 4]) + b"".join(r.pack(i == n - 1) for i, r in enumerate(records))


def unpack_message(buf: bytes) -> list[Record]:
    """Records of a tunnel message.  Raises ValueError if malformed."""
    if not buf or buf[0] >> 4 != VERSION:
        raise ValueError("not a tunnel message of this version")
    at, out = 1, []
    while at < len(buf):
        tag = buf[at]
        at += 1
        r = Record(tag >> 6, reliable=bool(tag & _R), newest=bool(tag & _S), ordered=bool(tag & _O),
                   app=bool(tag & _A), urgent=bool(tag & _U))
        r.flow, at = _vdec(buf, at)
        if r.kind in (PKT, FRAG):
            if r.reliable:
                r.seq = int.from_bytes(buf[at:at + 2], "big")
                d, at = _vdec(buf, at + 2)
                r.low = (r.seq - d) & 0xFFFF
            elif r.newest:
                r.seq = int.from_bytes(buf[at:at + 2], "big")
                at += 2
            if r.kind == FRAG:
                if at + 4 > len(buf):
                    raise ValueError("truncated fragment header")
                r.frag = (int.from_bytes(buf[at:at + 2], "big"), buf[at + 2], buf[at + 3])
                at += 4
        elif r.kind == ACK:
            r.seq = int.from_bytes(buf[at:at + 2], "big")
            at += 2
        if tag & _L:
            n, at = _vdec(buf, at)
        else:
            n = len(buf) - at
        if at + n > len(buf):
            raise ValueError("truncated record")
        r.body = bytes(buf[at:at + n])
        at += n
        out.append(r)
    return out


# ── IP addressing ────────────────────────────────────────────────────────────────────────────

def ip_dst(packet: bytes):
    """Destination address of a raw IPv4/IPv6 packet, or None."""
    v = packet[0] >> 4 if packet else 0
    if v == 4 and len(packet) >= 20:
        return ipaddress.IPv4Address(bytes(packet[16:20]))
    if v == 6 and len(packet) >= 40:
        return ipaddress.IPv6Address(bytes(packet[24:40]))
    return None


def ip_src(packet: bytes):
    v = packet[0] >> 4 if packet else 0
    if v == 4 and len(packet) >= 20:
        return ipaddress.IPv4Address(bytes(packet[12:16]))
    if v == 6 and len(packet) >= 40:
        return ipaddress.IPv6Address(bytes(packet[8:24]))
    return None


_RANK = {"config": 0, "announced": 1, "gleaned": 2}


@dataclass
class Route:
    network: object                      # ipaddress.IPv4Network / IPv6Network
    to: bytes                            # mesh node id or anycast address
    source: str                          # config | announced | gleaned
    origin: bytes | None = None          # who announced / was gleaned from
    t: float = 0.0


class AddressMap:
    """IP prefix -> mesh address, longest prefix wins.  For one prefix, configured beats announced
    beats gleaned, and the newest wins among equals.  Announced and gleaned entries age out
    (``max_age``, ``glean_age``), so a gateway or drone that vanishes takes its routes with it and
    no withdrawal has to arrive."""

    def __init__(self, max_age: float = 60.0, glean_age: float = 30.0):
        self.max_age, self.glean_age = float(max_age), float(glean_age)
        self._t: dict[tuple[int, int], dict[int, dict[tuple, Route]]] = {}

    def _put(self, r: Route) -> None:
        n = r.network
        slot = self._t.setdefault((n.version, n.prefixlen), {}).setdefault(int(n.network_address), {})
        slot[(r.source, r.origin)] = r

    def configure(self, prefix, to: bytes) -> None:
        self._put(Route(ipaddress.ip_network(prefix), bytes(to), "config"))

    def learn(self, origin: bytes, routes, now: float) -> None:
        """Replace everything ``origin`` announced with ``routes`` [(network, to)]."""
        self._drop(lambda r: r.source == "announced" and r.origin == origin)
        for net, to in routes:
            self._put(Route(ipaddress.ip_network(net), bytes(to), "announced", bytes(origin), now))

    def glean(self, addr, origin: bytes, now: float) -> None:
        if addr is not None:
            self._put(Route(ipaddress.ip_network(addr), bytes(origin), "gleaned", bytes(origin), now))

    def _drop(self, pred) -> None:
        for key, nets in list(self._t.items()):
            for a, slot in list(nets.items()):
                for k in [k for k, r in slot.items() if pred(r)]:
                    del slot[k]
                if not slot:
                    del nets[a]
            if not nets:
                del self._t[key]

    def expire(self, now: float) -> None:
        self._drop(lambda r: (r.source == "announced" and now - r.t > self.max_age)
                   or (r.source == "gleaned" and now - r.t > self.glean_age))

    def lookup(self, addr, now: float | None = None) -> bytes | None:
        if addr is None:
            return None
        addr = ipaddress.ip_address(addr)
        bits = addr.max_prefixlen
        a = int(addr)
        for plen in range(bits, -1, -1):
            nets = self._t.get((addr.version, plen))
            if not nets:
                continue
            slot = nets.get(a >> (bits - plen) << (bits - plen) if plen else 0)
            if slot:
                live = [r for r in slot.values() if now is None or r.source == "config"
                        or now - r.t <= (self.max_age if r.source == "announced" else self.glean_age)]
                if live:
                    return min(live, key=lambda r: (_RANK[r.source], -r.t)).to
        return None

    def __len__(self) -> int:
        return sum(len(s) for nets in self._t.values() for s in nets.values())


def pack_routes(entries) -> bytes:
    """ROUTES body: u8 n, then per entry u8 prefix length, u8 flags (bit0 v6, bit1 to = the sender),
    the prefix's significant bytes, and the 8-byte mesh address unless it is the sender."""
    out = [bytes([len(entries)])]
    for net, to in entries:
        net = ipaddress.ip_network(net)
        nb = (net.prefixlen + 7) // 8
        out.append(bytes([net.prefixlen, (net.version == 6) | ((to is None) << 1)]))
        out.append(net.network_address.packed[:nb])
        if to is not None:
            out.append(bytes(to))
    return b"".join(out)


def unpack_routes(body: bytes, sender: bytes) -> list:
    n, at, out = body[0], 1, []
    for _ in range(n):
        plen, fl = body[at], body[at + 1]
        at += 2
        size = 16 if fl & 1 else 4
        nb = (plen + 7) // 8
        raw = body[at:at + nb] + bytes(size - nb)
        at += nb
        if fl & 2:
            to = sender
        else:
            to, at = body[at:at + 8], at + 8
        addr = ipaddress.IPv6Address(raw) if fl & 1 else ipaddress.IPv4Address(raw)
        out.append((ipaddress.ip_network((addr, plen)), bytes(to)))
    if at != len(body):
        raise ValueError("trailing bytes in ROUTES")
    return out


# ── per-flow state ───────────────────────────────────────────────────────────────────────────

@dataclass
class _Out:
    """A reliable record the sender still holds."""
    seq: int
    body: bytes
    ordered: bool
    app: bool
    urgent: bool
    frag: tuple | None
    deadline: float | None               # explicit give-up time; None = ``lifetime`` after first send
    first: float | None = None
    last: float = -math.inf
    sends: int = 0
    want: bool = True                    # needs (re)transmission
    copies: int = 0                      # extra copies still to send, each in its own message


@dataclass
class SendFlow:
    flow: int
    to: bytes
    next_seq: int
    unacked: "OrderedDict[int, _Out]" = field(default_factory=OrderedDict)
    srtt: float | None = None
    rttvar: float = 0.0
    rto: float = 0.5
    hi_sent: float = -math.inf           # latest send time of anything acknowledged
    loss: float = 0.0                    # share of records that needed a retransmission (EWMA)
    touched: float = 0.0

    @property
    def low(self) -> int:
        return next(iter(self.unacked)) if self.unacked else self.next_seq


@dataclass
class RecvFlow:
    cum: int                             # next seq expected
    got: set = field(default_factory=set)        # received seqs past cum
    held: dict = field(default_factory=dict)     # ordered flows: records waiting for a gap
    ack_due: float | None = None
    pending: int = 0
    touched: float = 0.0


@dataclass
class _Queued:
    rec: Record
    t: float
    deadline: float


@dataclass
class Delivery:
    """A packet (or application segment) handed up: from mesh node ``origin`` on its ``flow``."""
    origin: bytes
    flow: int
    payload: bytes
    reliable: bool = False
    app: bool = False


@dataclass
class Out:
    """A message ready for the air: the mesh header, the sealed bytes, and the fountain redundancy
    the tunnel suggests for it (retransmissions ask for more)."""
    header: Header
    sealed: bytes
    redundancy: float
    records: int

    def blocks(self, block: int = BLOCK) -> int:
        return max(1, math.ceil(len(self.sealed) / block))


@dataclass
class TunnelConfig:
    block: int = BLOCK                   # fountain block of the link below
    max_blocks: int = 8                  # message cap: a packet is only usable once its whole
                                         # message decodes, so big messages add latency
    seal_overhead: int = 0               # bytes the crypto provider adds (0 under NullCrypto)
    mtu: int = 9000                      # largest packet accepted; bigger -> refused (PMTUD)
    hops: int = 8
    hold: float = 0.0                    # max wait for company for a lone small best-effort packet
    deadline: float = 1.0                # default best-effort deadline, s
    lifetime: float = 10.0               # reliable record: give up after, s
    window: int = 256                    # reliable records in flight per flow
    ack_delay: float = 0.05
    ack_every: int = 16
    rto_init: float = 0.5
    rto_min: float = 0.1
    rto_max: float = 2.0
    reorder: float = 0.125               # a hole is a loss once a later record, sent at least
                                         # reorder * srtt after it, was acknowledged
    idle: float = 30.0                   # flow state dropped after this long silent (receiver
                                         # idle must exceed sender lifetime, or a late retransmit
                                         # could deliver twice)
    frag_timeout: float = 2.0
    redundancy: float = 1.5              # fountain redundancy, first transmission
    redundancy_retx: float = 2.0         # retransmissions: they unblock a flow


@dataclass
class TunnelStats:
    messages: int = 0
    bytes_sealed: int = 0
    blocks: int = 0
    records: int = 0
    retransmits: int = 0
    acks: int = 0
    ack_only: int = 0
    expired: int = 0
    abandoned: int = 0
    delivered: int = 0
    duplicates: int = 0
    stale: int = 0
    bad: int = 0
    no_route: int = 0
    too_big: int = 0


# ── the tunnel endpoint ──────────────────────────────────────────────────────────────────────

class Tunnel:
    """One node's tunnel endpoint.  Pure state machine: ``send`` queues packets, ``pull(now)`` hands
    the next message to put on the air (call it whenever the radio can take one), ``receive``
    takes a decoded mesh message and returns what to hand up.  No sockets, no threads, no clock of
    its own -- every call takes ``now`` (local seconds; never compared with another node's)."""

    def __init__(self, node_id: bytes, *, crypto=None, routes: AddressMap | None = None,
                 config: TunnelConfig | None = None, accepts=frozenset(), router: FloodRouter | None = None,
                 glean: bool = False, backhaul: "Backhaul | None" = None, rng=None):
        self.node_id = bytes(node_id)
        self.crypto = crypto or NullCrypto()
        self.routes = routes if routes is not None else AddressMap()
        self.cfg = config or TunnelConfig()
        self.accepts = frozenset(accepts)
        self.router = router or FloodRouter(self.node_id)
        self.glean = glean
        self.backhaul = backhaul
        self.rng = rng or random.Random(int.from_bytes(self.node_id, "big") ^ random.getrandbits(32))
        self.stats = TunnelStats()
        self.sending: dict[int, SendFlow] = {}
        self.receiving: dict[tuple[bytes, int], RecvFlow] = {}
        self._queue: dict[bytes, deque] = {}
        self._frags: dict[tuple, tuple[float, int, dict]] = {}
        self._newest: dict[tuple[bytes, int], tuple[int, float]] = {}
        self._seen: OrderedDict = OrderedDict()
        self._flow_ids: dict = {}
        self._last_seq: dict[int, int] = {}
        self._nseq: dict = {}
        self.backhauled: list[Delivery] = []   # deliveries another gateway handed us (anchor)
        self.broken: set[int] = set()   # ordered flows that gave up a record (consumer clears)
        self._next_flow = self.rng.randrange(64)
        self._pkt_id = self.rng.randrange(1 << 16)
        if backhaul is not None:
            backhaul.join(self)

    # ── sending ──

    @property
    def budget(self) -> int:
        """Plaintext bytes one message may carry."""
        return self.cfg.max_blocks * self.cfg.block - self.cfg.seal_overhead - 1

    def new_flow(self) -> int:
        """A flow id not in use here.  Small ids keep the varint at one byte."""
        while True:
            f = self._next_flow
            self._next_flow = (self._next_flow + 1) % 0x3FFF
            if f not in self.sending:
                return f

    def flow_for(self, key) -> int:
        f = self._flow_ids.get(key)
        if f is None:
            f = self._flow_ids[key] = self.new_flow()
        return f

    def send(self, payload: bytes, now: float, *, to: bytes | None = None, reliable: bool = False,
             ordered: bool = False, flow: int | None = None, deadline: float | None = None,
             newest: bool = False, urgent: bool = False, app: bool = False) -> bool:
        """Queue one packet.  ``to`` defaults to the address map's answer for the packet's
        destination.  False if there is no route or the packet exceeds the MTU."""
        payload = bytes(payload)
        if len(payload) > self.cfg.mtu:
            self.stats.too_big += 1
            return False
        if to is None:
            to = self.routes.lookup(ip_dst(payload), now)
            if to is None:
                self.stats.no_route += 1
                return False
        to = bytes(to)
        chunks = self._cut(payload, reliable, newest)
        if reliable:
            if flow is None:
                flow = self.flow_for((to, _five_tuple(payload)))
            f = self.sending.get(flow)
            if f is None:
                f = self.sending[flow] = SendFlow(flow, to, self._isn(flow), rto=self.cfg.rto_init)
            f.to, f.touched = to, now
            dl = deadline                         # None: ``lifetime`` from the first send
            for fr, body in chunks:
                f.unacked[f.next_seq] = _Out(f.next_seq, body, ordered, app, urgent, fr, dl)
                f.next_seq = (f.next_seq + 1) & 0xFFFF
            return True
        flow = 0 if flow is None else flow
        dl = now + self.cfg.deadline if deadline is None else deadline
        q = self._queue.setdefault(to, deque())
        seq = None
        if newest:
            seq = self._nseq[(to, flow)] = (self._nseq.get((to, flow), self._isn(flow)) + 1) & 0xFFFF
        for fr, body in chunks:
            q.append(_Queued(Record(FRAG if fr else PKT, flow, body, newest=newest, app=app,
                                    urgent=urgent, seq=seq, frag=fr), now, dl))
        return True

    def _isn(self, flow: int) -> int:
        """A flow's first sequence number: random, and for a reused flow id half the sequence space
        away from where the last one stopped, so a receiver still holding the old state sees a new
        epoch, never a run of duplicates."""
        last = self._last_seq.pop(flow, None)
        if last is None:
            return self.rng.randrange(1 << 16)
        return (last + 0x6000 + self.rng.randrange(0x4000)) & 0xFFFF

    def _cut(self, payload: bytes, reliable: bool, newest: bool):
        """[(frag or None, body)]: the packet whole if it fits one message, else FRAG pieces."""
        room = self.budget - 12                      # worst-case record header, reliable FRAG
        if len(payload) <= room:
            return [(None, payload)]
        n = math.ceil(len(payload) / room)
        if n > 255:
            raise ValueError("packet needs more than 255 fragments; raise max_blocks or lower mtu")
        size = math.ceil(len(payload) / n)
        pid = self._pkt_id = (self._pkt_id + 1) & 0xFFFF
        return [((pid, i, n), payload[i * size:(i + 1) * size]) for i in range(n)]

    def announce_routes(self, entries, now: float) -> None:
        """Queue a ROUTES record to everyone: ``entries`` [(prefix, mesh address or None = me)]."""
        self._queue.setdefault(BROADCAST, deque()).append(
            _Queued(Record(CTRL, CTRL_ROUTES, pack_routes(entries)), now, now + self.cfg.deadline))

    # ── timers ──

    def poll(self, now: float) -> None:
        """Run timers: drop stale best-effort packets, give up on dead reliable records, mark
        timed-out ones for retransmission, and forget idle flows."""
        for to, q in list(self._queue.items()):
            keep = deque(x for x in q if x.deadline >= now)
            self.stats.expired += len(q) - len(keep)
            if keep:
                self._queue[to] = keep
            else:
                del self._queue[to]
        for fid, f in list(self.sending.items()):
            for s in [s for s, o in f.unacked.items()
                      if (o.deadline if o.deadline is not None
                          else math.inf if o.first is None else o.first + self.cfg.lifetime) < now]:
                if f.unacked.pop(s).ordered:
                    self.broken.add(fid)          # a hole in a stream: its user must reset it
                self.stats.abandoned += 1
            late = [o for o in f.unacked.values() if o.sends and not o.want and now - o.last >= f.rto]
            if late:
                # Silence: probe with the oldest message's worth, not the whole window.  Its ACK
                # carries the bitmap, and the bitmap says exactly what else is missing.
                room = self.budget
                for o in late:
                    room -= len(o.body) + 8
                    if room < 0 and o is not late[0]:
                        break
                    self._resend(f, o)
                f.rto = min(self.cfg.rto_max, f.rto * 2)
            if not f.unacked and now - f.touched > self.cfg.idle:
                self._last_seq[fid] = f.next_seq
                del self.sending[fid]
        for k, r in list(self.receiving.items()):
            if now - r.touched > self.cfg.idle:
                del self.receiving[k]
        for k, (t, _, _) in list(self._frags.items()):
            if now - t > self.cfg.frag_timeout:
                del self._frags[k]
        for k, (_, t) in list(self._newest.items()):
            if now - t > self.cfg.idle:
                del self._newest[k]
        while self._seen and now - next(iter(self._seen.values())) > self.router.seen_age:
            self._seen.popitem(last=False)
        self.routes.expire(now)

    # ── building a message ──

    def _acks_for(self, to: bytes, now: float, due_only: bool) -> list[Record]:
        out = []
        for (origin, fid), r in self.receiving.items():
            if origin != to or r.ack_due is None or (due_only and r.ack_due > now):
                continue
            bits = 0
            for s in r.got:
                d = _sdiff(s, r.cum) - 1
                if 0 <= d < 64:
                    bits |= 1 << d
            out.append(Record(ACK, fid, bits.to_bytes((bits.bit_length() + 7) // 8, "little"),
                              seq=r.cum))
        return out

    def _ready(self, f: SendFlow) -> list[_Out]:
        low = f.low
        return [o for o in f.unacked.values() if o.want and _sdiff(o.seq, low) < self.cfg.window]

    def pull(self, now: float) -> Out | None:
        """The next message to transmit, or None if nothing is ready.  Picks the destination with
        the most urgent work (ACKs due, retransmissions, urgent packets), then the oldest packet,
        and packs everything else bound there that fits."""
        self.poll(now)
        cand = {}
        for to, q in self._queue.items():
            if q:
                cand[to] = [any(x.rec.urgent for x in q), q[0].t, sum(len(x.rec.body) for x in q)]
        for f in self.sending.values():
            rd = self._ready(f)
            if rd:
                c = cand.setdefault(f.to, [False, math.inf, 0])
                c[0] = c[0] or any(o.sends or o.urgent for o in rd)
                c[1] = min(c[1], now if any(o.sends for o in rd) else f.touched)
                c[2] += sum(len(o.body) for o in rd)
        for (origin, _), r in self.receiving.items():
            if r.ack_due is not None and r.ack_due <= now:
                c = cand.setdefault(origin, [False, math.inf, 0])
                c[0] = True
                c[1] = min(c[1], r.ack_due)
        live = {to: c for to, c in cand.items()
                if c[0] or self.cfg.hold <= 0 or c[2] >= self.cfg.block // 2 or now - c[1] >= self.cfg.hold}
        if not live:
            return None
        to = min(live, key=lambda k: (not live[k][0], live[k][1]))
        # Candidates in priority order (ACKs, retransmissions, new reliable records, best-effort).
        # If they all fit under the cap they all go.  If not, the prefix that fills its blocks
        # best goes: a frame costs the same full or not, so stopping just before a record that
        # would spill into a new, mostly empty block saves airtime, and that record leads the
        # next message instead (the radio is busy until then anyway).
        cand, room = [], self.budget
        acks = self._acks_for(to, now, due_only=False)
        for a in acks:
            cand.append((a, None))
        flows = [f for f in self.sending.values() if f.to == to]
        for f in flows:
            for o in sorted(self._ready(f), key=lambda o: (not o.sends, not o.urgent, _sdiff(o.seq, f.low))):
                cand.append((Record(FRAG if o.frag else PKT, f.flow, o.body, reliable=True,
                                    ordered=o.ordered, app=o.app, urgent=o.urgent, seq=o.seq,
                                    low=f.low, frag=o.frag), o))
        q = self._queue.get(to)
        if q:
            for x in sorted(q, key=lambda x: not x.rec.urgent):
                cand.append((x.rec, x))
        if not cand:
            return None
        used, best, best_fill, capped = 1 + self.cfg.seal_overhead, 1, -1.0, False
        for i, (r, _) in enumerate(cand):
            used += r.size(last=False)
            if used > self.budget + 1 + self.cfg.seal_overhead and i:
                capped = True
                break
            fill = used / (math.ceil(used / self.cfg.block) * self.cfg.block)
            if fill >= best_fill - 1e-9 or i < len(acks):
                best, best_fill = i + 1, fill
        if not capped:
            best = len(cand)
        take = cand[:best]
        recs = [r for r, _ in take]
        sent = [x for _, x in take if isinstance(x, _Out)]
        for _, x in take:
            if isinstance(x, _Queued):
                q.remove(x)
        if q is not None and not q:
            del self._queue[to]
        acks = [r for r in recs if r.kind == ACK]
        retx = False
        for (origin, fid), r in self.receiving.items():
            if origin == to and r.ack_due is not None:
                r.ack_due, r.pending = None, 0
        for o in sent:
            retx |= o.sends > 0
            self.stats.retransmits += o.sends > 0
            o.sends += 1
            o.first = now if o.first is None else o.first
            o.last, o.want = now, o.copies > 0
            o.copies = max(0, o.copies - 1)
        for f in flows:
            f.touched = max(f.touched, now)
        h = self.router.originate(to, self.cfg.hops)
        # A message of best-effort packets only carries their latest deadline as the mesh time
        # budget, so relays drop it once it can no longer arrive (``routing.spend``).  Anything
        # reliable, an ACK or control rides without one: those have their own give-up rules.
        # The budget changes per hop, so it is not part of the sealed context.
        dls = [x.deadline for _, x in take if isinstance(x, _Queued)]
        if dls and len(dls) == len(take) and all(r.kind in (PKT, FRAG) for r in recs):
            h = Header(h.origin, h.seq, h.to, h.hops,
                       min(0xFFFF, max(1, math.ceil(1e3 * (max(dls) - now)))))
        sealed = self.crypto.seal(pack_message(recs), context=_context(h))
        st = self.stats
        st.messages += 1
        st.bytes_sealed += len(sealed)
        st.records += len(recs)
        st.acks += len(acks)
        st.ack_only += len(acks) == len(recs)
        out = Out(h, sealed, self.cfg.redundancy_retx if retx else self.cfg.redundancy, len(recs))
        st.blocks += out.blocks(self.cfg.block)
        return out

    # ── receiving ──

    def addresses(self) -> frozenset:
        return self.accepts | {self.node_id, BROADCAST}

    def receive(self, header: Header, sealed: bytes, now: float) -> list[Delivery]:
        """A decoded mesh message.  Returns what to hand up (to the TUN device or the TCP proxy)."""
        if header.to not in self.addresses() or header.origin == self.node_id:
            return []
        if header.to in self.accepts and self.backhaul is not None:
            return self.backhaul.deliver(self, header, sealed, now)
        return self.accept(header, sealed, now)

    def accept(self, header: Header, sealed: bytes, now: float) -> list[Delivery]:
        if header.msg_id in self._seen:
            self.stats.duplicates += 1
            return []
        self._seen[header.msg_id] = now
        plain = self.crypto.open(sealed, context=_context(header))
        try:
            recs = unpack_message(plain) if plain is not None else None
        except ValueError:
            recs = None
        if recs is None:
            self.stats.bad += 1
            return []
        out = []
        for r in recs:
            if r.kind == ACK:
                self._on_ack(header.origin, r, now)
            elif r.kind == CTRL:
                if r.flow == CTRL_ROUTES:
                    try:
                        self.routes.learn(header.origin, unpack_routes(r.body, header.origin), now)
                    except (ValueError, IndexError):
                        self.stats.bad += 1
            elif r.reliable:
                out += self._on_reliable(header.origin, r, now)
            else:
                if r.newest:
                    k = (header.origin, r.flow)
                    prev = self._newest.get(k)
                    if prev is not None and _sdiff(r.seq, prev[0]) < 0 and r.kind == PKT:
                        self.stats.stale += 1
                        continue
                    self._newest[k] = (r.seq if prev is None or _sdiff(r.seq, prev[0]) > 0 else prev[0], now)
                out += self._hand_up(header.origin, r, now)
        return out

    def _hand_up(self, origin: bytes, r: Record, now: float) -> list[Delivery]:
        body = r.body
        if r.kind == FRAG:
            pid, i, n = r.frag
            key = (origin, r.flow, pid)
            t, cnt, parts = self._frags.get(key, (now, n, {}))
            parts[i] = body
            if len(parts) < cnt:
                self._frags[key] = (t, cnt, parts)
                return []
            self._frags.pop(key, None)
            body = b"".join(parts[j] for j in range(cnt))
        if self.glean and not r.app:
            self.routes.glean(ip_src(body), origin, now)
        self.stats.delivered += 1
        return [Delivery(origin, r.flow, body, r.reliable, r.app)]

    def _on_reliable(self, origin: bytes, r: Record, now: float) -> list[Delivery]:
        key = (origin, r.flow)
        rf = self.receiving.get(key)
        if rf is None or abs(_sdiff(r.low, rf.cum)) > 4 * self.cfg.window:
            rf = self.receiving[key] = RecvFlow(r.low)       # new flow, or a new epoch of it
        rf.touched = now
        out = []
        if _sdiff(r.low, rf.cum) > 0:                        # the sender gave up below low
            out += self._skip_to(origin, rf, r.low, now)
        d = _sdiff(r.seq, rf.cum)
        if d < 0 or r.seq in rf.got:
            self.stats.duplicates += 1
            rf.ack_due = now                                 # it may be missing our ACK
            return out
        rf.got.add(r.seq)
        rf.pending += 1
        if r.ordered:
            rf.held[r.seq] = r
        else:
            out += self._hand_up(origin, r, now)
        out += self._advance(origin, rf, now)
        if d > 0 or rf.got:                                  # a gap: say so now (the NACK)
            rf.ack_due = now
        elif rf.pending >= self.cfg.ack_every:
            rf.ack_due = now
        else:
            rf.ack_due = min(rf.ack_due if rf.ack_due is not None else math.inf, now + self.cfg.ack_delay)
        return out

    def _advance(self, origin, rf: RecvFlow, now) -> list[Delivery]:
        out = []
        while rf.cum in rf.got:
            rf.got.discard(rf.cum)
            h = rf.held.pop(rf.cum, None)
            if h is not None:
                out += self._hand_up(origin, h, now)
            rf.cum = (rf.cum + 1) & 0xFFFF
        return out

    def _skip_to(self, origin, rf: RecvFlow, low: int, now) -> list[Delivery]:
        out = []
        while _sdiff(low, rf.cum) > 0:
            if rf.cum in rf.got:
                rf.got.discard(rf.cum)
                h = rf.held.pop(rf.cum, None)
                if h is not None:
                    out += self._hand_up(origin, h, now)
            rf.cum = (rf.cum + 1) & 0xFFFF
        return out + self._advance(origin, rf, now)

    def _on_ack(self, origin: bytes, r: Record, now: float) -> None:
        f = self.sending.get(r.flow)
        if f is None:                     # any node may ACK: after an anycast failover it is
            return                        # a different gateway than the one that started
        bits = int.from_bytes(r.body, "little")
        acked = [s for s in f.unacked if _sdiff(s, r.seq) < 0
                 or (_sdiff(s, r.seq) >= 1 and bits >> (_sdiff(s, r.seq) - 1) & 1)]
        for s in acked:
            o = f.unacked.pop(s)
            f.loss = 0.9 * f.loss + 0.1 * (o.sends > 1)
            if o.sends == 1 and o.first is not None:          # Karn: time first sends only
                self._rtt(f, now - o.first)
            if o.sends:
                f.hi_sent = max(f.hi_sent, o.last)
        if acked:
            f.rto = max(self.cfg.rto_min, min(self.cfg.rto_max, (f.srtt or self.cfg.rto_init) + 4 * f.rttvar))
        top = (r.seq + bits.bit_length()) & 0xFFFF            # highest seq the receiver holds
        slack = self.cfg.reorder * (f.srtt or 0.0)
        for o in f.unacked.values():
            if (o.sends and not o.want and _sdiff(o.seq, top) < 0 and bits
                    and o.last < f.hi_sent - slack):
                self._resend(f, o)
        f.touched = now

    @staticmethod
    def _resend(f: SendFlow, o: _Out) -> None:
        """Mark a record for retransmission -- in more than one message when this flow has been
        losing many (each copy is an independent chance; latency matters more than airtime)."""
        o.want = True
        o.copies = 0 if f.loss < 0.1 else 1 if f.loss < 0.3 else 2

    def _rtt(self, f: SendFlow, rtt: float) -> None:
        if f.srtt is None:
            f.srtt, f.rttvar = rtt, rtt / 2
        else:
            f.rttvar = 0.75 * f.rttvar + 0.25 * abs(f.srtt - rtt)
            f.srtt = 0.875 * f.srtt + 0.125 * rtt


def _context(h: Header) -> bytes:
    return b"tun" + h.origin + h.seq.to_bytes(4, "big") + h.to


def _five_tuple(p: bytes):
    """(src, dst, proto, sport, dport) of an IP packet, as far as it parses; flows are keyed on it."""
    v = p[0] >> 4 if p else 0
    if v == 4 and len(p) >= 20:
        ihl = (p[0] & 15) * 4
        proto, src, dst, at = p[9], p[12:16], p[16:20], ihl
    elif v == 6 and len(p) >= 40:
        proto, src, dst, at = p[6], p[8:24], p[24:40], 40
    else:
        return None
    ports = p[at:at + 4] if proto in (6, 17) else b""
    return bytes(src), bytes(dst), proto, bytes(ports)


# ── the gateways' wire ───────────────────────────────────────────────────────────────────────

class Backhaul:
    """Gateways sharing a wire.  Every gateway accepts HOME; the first to decode a message hands it
    to the gateway anchoring the message's origin, and a copy decoded by another gateway is dropped.
    The anchor is the first live gateway that heard the origin, so all of one drone's reliable flows
    keep their state in one place; when it dies the next gateway to hear the drone takes over and
    resynchronizes from the low-water mark.  Best-effort traffic needs no anchor, but rides the same
    path so that dedupe is one table."""

    def __init__(self, seen_age: float = 60.0):
        self.seen_age = float(seen_age)
        self.gateways: list[Tunnel] = []
        self.anchor: dict[bytes, Tunnel] = {}
        self._seen: OrderedDict = OrderedDict()
        self.duplicates = 0

    def join(self, gw: Tunnel) -> None:
        if gw not in self.gateways:
            self.gateways.append(gw)

    def leave(self, gw: Tunnel) -> None:
        """A gateway is gone (dead or unplugged): it anchors nothing any more."""
        if gw in self.gateways:
            self.gateways.remove(gw)
        for k in [k for k, a in self.anchor.items() if a is gw]:
            del self.anchor[k]

    def deliver(self, gw: Tunnel, header: Header, sealed: bytes, now: float) -> list[Delivery]:
        while self._seen and now - next(iter(self._seen.values())) > self.seen_age:
            self._seen.popitem(last=False)
        if header.msg_id in self._seen:
            self.duplicates += 1
            return []
        self._seen[header.msg_id] = now
        a = self.anchor.get(header.origin)
        if a is None or a not in self.gateways:
            a = self.anchor[header.origin] = gw
        got = a.accept(header, sealed, now)
        if a is gw:
            return got
        a.backhauled += got               # the anchor's owner drains these
        return []


__all__ = ["VERSION", "FRAME", "BLOCK", "PKT", "FRAG", "ACK", "CTRL", "Record", "pack_message",
           "unpack_message", "ip_dst", "ip_src", "Route", "AddressMap", "pack_routes", "unpack_routes",
           "SendFlow", "RecvFlow", "Delivery", "Out", "TunnelConfig", "TunnelStats", "Tunnel", "Backhaul"]
