"""
node.py -- one mesh node: the parts composed into something a radio can run.

A :class:`Node` is a state machine with no clock and no radio of its own.  Every call takes
``now`` (seconds on the node's own clock -- never compared with another node's), and the radio is
whatever feeds it captures and takes its bursts:

    on_capture(iq, now)       a capture from its radio, at the radio's own rate.  The node reads
                              every burst it can out of it: first the fast known-geometry read for
                              every sender geometry it has measured (``phy.receive_known``); when
                              none of those yields a frame, the same read at each known scale with
                              the band centre measured from the capture itself (a sender that moved
                              sub-band at the same width); and when that fails too, blind
                              acquisition (``phy.receive_burst``) under a time budget.  Every frame
                              that passes its CRC goes to the relay rules (:mod:`relay`), and the
                              geometry it was read at is kept (measured, never pre-shared).
    on_announcement(...)      a sealed "I'm here" (normally arrives over the air, one hop).
    send(payload, to, now)    a message of its own; ``attach(tunnel)`` instead routes IP packets.
    tick(now)                 timers: announcements, ageing, plausibility.
    next_burst(now)           what to transmit now, as (iq at the plan's rate, TxPlan), or None.
                              The plan is :func:`adapt.default_plan`'s, then the agility provider's
                              (last word), then the interference provider's review.

What it runs is the measured relay policy (:mod:`relay`; ``relay.ADOPTED``): decode-and-forward
with fresh fountain symbols on the relay's own sub-band (never the one it heard the message on --
``default_plan``'s ``heard_on``), passive acks at the origin, sibling share among relays at a x3
budget, hop and time budgets, anycast delivery (HOME on gateways) with an optional backhaul
callback for gateway dedupe.  Nothing in the core asks anything of another node: no handshake, no
ack, no schedule.  The only pre-shared thing is whatever key the crypto provider holds.

Access is reply-free and slotted on the node's own clock: after each burst it waits 1..``window``
of its own slots (``slot_s``) at random before the next.  Nothing aligns one node's slots with
another's; the emulation driver happens to sample all of them on one grid.  A radio that can
listen first (``start``/``commit``; the emulators do) gets listen before talk: the node draws its
start at random inside a short contention window, the radio listens just before it, and the node
defers to a later slot (1..``window`` again, at random) when what it heard stands above its own
floor by its gate.  Of the queued symbols it sends the one of the message it has sent fewest of
(``interleave``), so a new message never waits behind older ones' redundancy.  Measured in the
fleet showcase and a dense cluster (research/access/README.md): the collisions that lost 39 of
70 showcase items go, and the relays' sibling share -- which needs them to hear each other --
then cancels most of the relay airtime.

Known limits, stated rather than hidden:
  * Bursts go out in the family, alphabet and code rate ``default_plan`` picks from the measured
    envelope of ``families`` -- differential only by default, since coherent QAM has not yet
    survived the hardware bench at matched rates (research/mcs/coherent.md).  None of the three
    is signalled: every read decodes each hypothesis the burst's length admits and the frame CRC
    chooses (``link.read_frame``), so a node reads either family whatever it sends itself.
    A coherent burst carries no PHY sender mark (``compose_qam`` takes none).
  * The transmitter of a burst is identified by its symbol ids: an origin's own symbols sit in id
    region 0, a relay's fresh ones in a region derived from its id (``relay.region_of``).  The PHY's
    free sender mark is read on the integer screen only (``phy.sender_of``); at a continuous scale
    it would replace this.
  * Blind acquisition of a sender narrower than the receiver is slow in Python (~40 s for one
    1.2 -> 2.4 Msps burst); ``blind_scaled`` and ``blind_every`` bound what a node spends on it.
"""
from __future__ import annotations

import math
import time
from dataclasses import dataclass, field, replace

import numpy as np

from entroptics_rf import waveform as wf

from . import adapt, announce, fec, phy, placement, plausibility, relay, steer
from .link import BLOCK, FRAME_BYTES, Reassembler, frame, read_frame, unframe


def _frames(rx) -> bool:
    """The integrity check the fast read may use to place a burst (CRC-protected frame)."""
    return read_frame(rx) is not None
from .neighbors import BandRange, Measured, NeighborTable, Position
from .providers import BandObservation, Intent, NullInterference, PlanContext, Providers
from .routing import BROADCAST, HOME, LOCATE, FloodRouter, Header

ANNOUNCE = announce.ADDRESS
_SPECIAL = (BROADCAST, HOME, ANNOUNCE, LOCATE)


@dataclass
class Geometry:
    """A sender geometry this node measured: what the fast read needs, who it was (when the symbol
    ids said), and when it last yielded a frame."""
    known: phy.Known
    who: bytes | None = None
    learned: float = 0.0
    last: float = -math.inf
    how: str = "seeded"              # seeded / rescan / blind
    quiet: list = field(default_factory=list)   # (t, levels): rows around bursts read here (noise)


@dataclass
class Delivered:
    """A message handed up: from ``origin`` to ``to`` (this node, BROADCAST or an anycast address)."""
    origin: bytes
    to: bytes
    payload: bytes
    t: float
    msg_id: tuple


@dataclass
class NodeStats:
    captures: int = 0
    gated: int = 0                   # captures with nothing above the measured floor: not read
    frames: int = 0                  # frames that passed their CRC
    fast_reads: int = 0
    fast_hits: int = 0
    local_reads: int = 0             # fast reads given any §3a start prior (lock cache or 7c)
    local_hits: int = 0             # of those, the ones the local scan locked within its window
    coord_reads: int = 0             # fast reads given a keyed-schedule (7c) start prediction
    coord_hits: int = 0             # of those, the ones the local (§3a) scan locked within its window
    rescans: int = 0
    rescan_hits: int = 0
    blind_tries: int = 0
    blind_hits: int = 0
    read_s: float = 0.0              # CPU seconds in known-geometry reads (incl. rescans)
    blind_s: float = 0.0             # CPU seconds in blind acquisition
    bursts: int = 0
    held: int = 0                    # transmissions the interference provider held
    deferred: int = 0                # bursts put off because the listen before them heard the channel busy
    acked: int = 0                   # own messages stopped early by a passive ack
    cancelled: int = 0               # relay symbols cancelled by sibling share
    expired: int = 0                 # symbols dropped: time budget spent
    duplicates: int = 0              # anycast deliveries the backhaul refused
    bad: int = 0                     # messages that did not open
    announced: int = 0
    announcements: int = 0           # announcements heard and opened


class Wire:
    """A gateways' backhaul for anycast dedupe: the first gateway to deliver a message wins, any
    later copy is refused.  Pass the same instance as ``backhaul`` to every gateway node."""

    def __init__(self):
        self.first: dict = {}
        self.duplicates = 0

    def __call__(self, header: Header, payload: bytes, node: "Node") -> bool:
        if header.msg_id in self.first:
            self.duplicates += 1
            return False
        self.first[header.msg_id] = node.node_id
        return True


def _context(h: Header) -> bytes:
    """What a sealed message is bound to: its id and destination (not the hop or time budget,
    which relays change)."""
    return b"msg" + h.origin + h.seq.to_bytes(4, "big") + h.to


class Node:
    """One mesh node.  See the module note for the calls and the rules."""

    CONTENTION = 2048               # samples at the capture rate: the window a burst's start is drawn in
    LISTEN = 64                     # samples at the capture rate listened to before a start

    def __init__(self, node_id: bytes, radio: adapt.Radio, *, slot_s: float,
                 providers: Providers | None = None, policy: relay.Policy = relay.ADOPTED,
                 accepts=frozenset(), backhaul=None, homes=(), position: Position | None = None,
                 velocity=None, window: int = 4, hops: int = 8, redundancy: float = 1.5,
                 announce_every: float | None = None, widths=(64,), blind_every: float = 0.0,
                 blind_scaled: bool = True, geometry_age: float = 60.0, gate_db: float = 2.5,
                 max_speed: float = 150.0, families=("differential",), seed: int = 0,
                 on_message=None, memory_age: float = 120.0, contention_s: float | None = None,
                 listen_s: float | None = None, backoff_max: int = 1, interleave: bool = True,
                 steer: bool = False, steer_margin_m: float = 100.0, locate_hops: int = 0,
                 steer_recover: bool = False, steer_width: int = 0, anchors=None,
                 anchor_tol_db: float = 3.0):
        if len(node_id) != 8:
            raise ValueError("node ids are 8 bytes")
        self.node_id = bytes(node_id)
        self.radio = radio
        self.rx_rate = float(radio.max_rate)          # the node captures at its radio's full width
        self.slot_s = float(slot_s)
        self.providers = providers or Providers()
        self.crypto = self.providers.crypto
        self.key = self.crypto.waveform_key(b"phy")
        # Optional keyed transmit schedule (roadmap 7c): a provider may also derive WHEN a burst
        # goes out, not only how it looks.  Absent (the default / a provider that only plans the
        # waveform) -> today's random access, byte-identical.  ``_sched`` anchors each coordinated
        # sender's prediction: who -> (offset this provider gave for the slot of the last lock,
        # that lock's measured start phase, its carrier).  The arrival start phase is predicted as
        # the anchor's start phase plus the keyed change in transmit offset since it (in received
        # samples), which cancels the unknown propagation/clock constant -- so a sender whose start
        # jitters every burst is still read local after one blind/lock-cache contact.
        self._sched_tx = getattr(self.providers.agility, "transmit_offset", None)
        self._sched_pred = getattr(self.providers.agility, "predict_offset", None)
        self._sched: dict[bytes, tuple[float, float, float, int]] = {}
        self._last_onset = 0
        self.policy = policy
        self.accepts = frozenset(accepts) | {ANNOUNCE}
        self.backhaul = backhaul
        self.homes = set(homes) | ({self.node_id} if HOME in self.accepts else set())
        self.position, self.velocity = position, velocity
        self.window, self.hops, self.redundancy = int(window), int(hops), float(redundancy)
        # listen before talk (``start`` / ``commit``): a random start inside the contention window,
        # a listen of ``listen_s`` before it, a backoff that doubles per deferral in a row
        self.contention_s = (self.CONTENTION / self.rx_rate if contention_s is None else float(contention_s))
        self.listen_s = self.LISTEN / self.rx_rate if listen_s is None else float(listen_s)
        self.backoff_max = int(backoff_max)
        self.interleave = bool(interleave)
        # position-aware forwarding (steer.py): off by default -> plain flooding, byte-identical.  When
        # on and this node knows where the destination is (a trusted announced position), it relays a
        # message only while it descends the gradient toward it; otherwise it diffuses (floods).
        self.steer = bool(steer)
        self.steer_margin_m = float(steer_margin_m)
        # location gossip: 0 = off (no beacon, behaviour unchanged).  > 0 floods this node's sealed
        # announcement to that many hops on the LOCATE address, so position knowledge spreads past the
        # one-hop announcement and a far node can steer toward a target it has never been a neighbour of.
        self.locate_hops = int(locate_hops)
        # void recovery: when steering would suppress a relay, suppress only if a closer node is known
        # (progress is possible elsewhere); at a local minimum where nothing closer is known, flood to
        # escape instead of trapping the message.  Off by default -> plain descend (suppress).
        self.steer_recover = bool(steer_recover)
        # narrowing: 0 = every descending node relays (widest, most redundant).  > 0 keeps only the best
        # that many descending neighbours of the sender (by progress toward the target), cutting relays
        # in a dense field.  Judged from trusted positions, so a spoofer cannot claim a rank it is not
        # physically positioned for.
        self.steer_width = int(steer_width)
        # authoritative anchors (surveyed ground stations): node id -> known-good Position, trusted as
        # ground truth (not a learned claim).  Empty = no anchoring.  When set, this node checks its OWN
        # believed position against the SNR-implied range to each anchor it has heard; if the air says it
        # cannot be where it thinks, its fix is dropped and steering fails safe to flooding -- the defence
        # against a coherent regional GPS spoof that moves a whole area and so passes pairwise checks.
        self.anchors = dict(anchors or {})
        self.anchor_tol_db = float(anchor_tol_db)
        self._sent: dict = {}                        # msg id -> symbols of it this node has sent
        self.last_sent = None                        # the last symbol next_symbol handed out
        self._backoff = 1
        self.announce_every = announce_every
        self.widths = tuple(widths)
        self.blind_every, self.blind_scaled = float(blind_every), bool(blind_scaled)
        self.geometry_age, self.gate_db = float(geometry_age), float(gate_db)
        self.max_speed = float(max_speed)
        # How long a message is remembered after it was last active.  It must exceed the time a
        # message can take to cross the mesh, or a late copy would be taken and relayed again --
        # the same rule as ``FloodRouter.seen_age``.
        self.memory_age = float(memory_age)
        self._touched: dict = {}                     # msg id -> last time it was active here
        self.families = tuple(families)
        self.rng = np.random.default_rng(seed)
        self.on_message = on_message
        # relay holder state (see relay.py)
        self.queue: list = []
        self.seen: set = set()
        self.rx = Reassembler()
        self.held: dict = {}
        self.region = relay.region_of(self.node_id)
        # node state
        self.router = FloodRouter(self.node_id)
        self.table = NeighborTable(max_age=max(30.0, geometry_age))
        self.geoms: list[Geometry] = []
        self.distrusted: set = set()
        self.verdicts: dict = {}
        self.inbox: list[Delivered] = []
        self.ip_out: list = []                       # tunnel deliveries that are IP packets
        self.tunnel = None
        self.stats = NodeStats()
        self.due: float | None = None
        self.floor_db: float | None = None
        self._floors: list = []                      # the last captures' floor reads (dB)
        self._last_blind = -math.inf
        self._next_announce = 0.0 if announce_every else math.inf
        self._heard_on: dict = {}                    # msg id -> centre (Hz) the message came in on
        self._heard_bands: dict = {}                 # msg id -> every centre (Hz) it was heard on
        self._regions: dict[int, bytes] = {}         # symbol-id region -> node id
        self._know(self.node_id)

    # ── what the node knows ──────────────────────────────────────────────────────────────────

    def _know(self, nid: bytes) -> None:
        if nid not in _SPECIAL:
            self._regions.setdefault(relay.region_of(nid), bytes(nid))

    def learn(self, known: phy.Known, who: bytes | None = None, now: float = 0.0,
              how: str = "seeded") -> Geometry:
        """Hold a sender geometry -- normally measured here (rescan or blind); called directly to
        start a node in the state it reaches after first contact."""
        for g in self.geoms:
            k = g.known
            if (k.L == known.L and abs(k.scale - known.scale) <= 1e-3 * known.scale
                    and abs(k.center - known.center) < 0.01):
                g.known, g.who = known, who or g.who
                return g
        g = Geometry(known, who, now, -math.inf, how)
        self.geoms.append(g)
        if who is not None:
            self._know(who)
        return g

    def _transmitter(self, sym) -> bytes | None:
        if sym.symbol_id < relay.REGION:
            return sym.header.origin
        return self._regions.get(sym.symbol_id // relay.REGION * relay.REGION)

    # ── input: captures ──────────────────────────────────────────────────────────────────────

    FLOOR_QUANTILE = 0.02           # of a capture's 64-sample blocks: the quiet part of a busy slot
    FLOOR_CAPTURES = 32             # captures the floor is the least of (a sliding window)

    def _level(self, iq) -> tuple[float, float, bool]:
        """Capture level (its busier part) and the noise floor, both measured -- for the gate
        only: what a decoded burst is heard at is read on the burst itself (``phy.burst_snr``).

        The busier part is the 98th percentile of the 64-sample blocks: a slot holds the longest
        frame any mode makes at the slowest rate, so the shortest burst (256-QAM 3/4 at full
        width) fills ~5% of it.  The floor: each capture's 2nd percentile of the blocks, divided by
        what that quantile of noise alone reads (a block of noise is a mean of 64 exponential
        powers, ``phy.noise_quantile``) -- so a capture of noise reads its noise, not the ~1.3 dB
        below it the bare quantile sits -- and the floor is the least of the last
        ``FLOOR_CAPTURES`` such reads.  The least, not the median: in a busy band most captures
        hold a burst that fills the slot (one 1.2 Msps DBPSK 1/2 frame leaves ~1.5% of its blocks
        quiet), so most reads are burst level, and a median floor sat at the loudest neighbour's
        level -- measured in the fleet showcase, the gate then refused a third to three quarters
        of the captures holding a burst decodable at 6-27 dB over the noise.  One capture with a
        quiet stretch in the window is enough for the least.  On noise alone the least of 32 sits
        ~0.2 dB below the noise (per-capture spread ~0.1 dB), and being a sliding window it
        follows a band whose noise rises within ``FLOOR_CAPTURES`` captures (a minimum over all
        time could only fall)."""
        n = iq.size // 64 * 64
        p = np.mean(np.abs(iq[:n].reshape(-1, 64)) ** 2, axis=1) if n else np.array([1e-30])
        lo = float(np.percentile(p, 100 * self.FLOOR_QUANTILE)) / phy.noise_quantile(
            64, int(p.size), self.FLOOR_QUANTILE)
        hi = 10 * math.log10(max(float(np.percentile(p, 98)), 1e-30))
        self._blocks = p
        self._floors.append(10 * math.log10(max(lo, 1e-30)))
        del self._floors[:-self.FLOOR_CAPTURES]
        self.floor_db = float(min(self._floors))
        return hi, self.floor_db, hi - self.floor_db > self.gate_db

    def on_capture(self, iq, now: float) -> list:
        """Read every burst this capture holds; returns the symbols taken (new ones)."""
        iq = np.asarray(iq)
        self.stats.captures += 1
        _, _, present = self._level(iq)
        if not isinstance(self.providers.interference, NullInterference):
            self._observe(iq, now)
        if not present:
            self.stats.gated += 1
            return []
        onset = self._onset()
        self._last_onset = onset           # kept for the 7c start prediction (trims the start phase)
        iq = iq[onset:]
        got = []
        t0 = time.perf_counter()
        for g in list(self.geoms):
            self.stats.fast_reads += 1
            # A sender read before (its geometry carries ``who``, set once a frame of it passed the
            # CRC) is read with the §3a local scan around a prior; one never read is the full scan.
            # The prior is, in order of preference: the 7c keyed-schedule prediction (a coordinated
            # sender, usable on every burst including a jittered start); else the lock cache (the
            # previous lock, usable only for a steady start); else None -> full scan.  A stale or
            # wrong prior only costs a re-scan (``phy.receive_known``'s CRC fallback), never a wrong
            # lock.
            pred = self._predict_tracked(g, now) if g.who is not None else None
            tracked = pred if pred is not None else (
                self.table.tracked(g.who) if g.who is not None else None)
            if tracked is not None:
                self.stats.local_reads += 1
            if pred is not None:
                self.stats.coord_reads += 1
            sym, rx = self._read(iq, g.known, tracked)
            if sym is not None:
                self.stats.fast_hits += 1
                if tracked is not None and self._within(rx, g, tracked):
                    self.stats.local_hits += 1
                    if pred is not None:
                        self.stats.coord_hits += 1
                g.last = now
                d = rx.discovery
                g.known = phy.Known(g.known.L, g.known.scale, float(d.carrier) * g.known.scale / g.known.L)
                got.append((sym, g, rx))
        if not got and self.geoms:
            got += self._rescan(iq, now)
        self.stats.read_s += time.perf_counter() - t0
        if not got:
            got += self._blind(iq, now)
        taken = []
        for sym, g, rx in got:
            if self._frame_in(sym, g, rx, now):
                taken.append(sym)
        return taken

    ONSET_MARGIN = 256              # samples of the capture kept before the burst's energy onset

    def _onset(self) -> int:
        """Where the reads start in the capture just gated: ``ONSET_MARGIN`` samples before the
        burst's energy onset, or 0.

        A burst starts anywhere in a capture -- a listening sender draws its start at random in its
        contention window -- but the known-geometry read seeks a burst's first row within a few
        rows of the capture's first (``phy.START_REACH``, ``SCAN_ROWS_MAX``): measured, a full-width
        DBPSK burst at 13 dB read at offsets of 100 and 500 samples and not at 1000 or later.  So
        the read starts near the onset: the run of 64-sample blocks reading above the floor
        (``phy._run``, the scan statistic for a level shift of unknown size -- no threshold, as the
        burst's own run of rows is found), its first block less the margin, which keeps the rows of
        noise before the burst the reads delimit it by (as many as before at full width, where
        bursts began ~100 samples in).  Bursts of other senders that begin later in the same
        capture (a sender out of earshot of the first) start further in than the read reaches."""
        p = getattr(self, "_blocks", None)
        if p is None or self.floor_db is None or p.size < 4:
            return 0
        stat, a, b = phy._run(2.0 - p / 10.0 ** (self.floor_db / 10.0))
        if not np.isfinite(stat) or b <= a:
            return 0
        return max(0, int(a) * 64 - self.ONSET_MARGIN)

    def _predict_tracked(self, g: Geometry, now: float):
        """The 7c prior ``(start_phase, carrier, window)`` for a coordinated sender, or None.

        The schedule provider predicts ``who``'s transmit start offset for this slot
        (``predict_offset``); paired with the anchor kept from ``who``'s last CRC-passing lock, the
        arrival start phase is the anchor's phase plus the change in transmit offset since, in
        received samples (``offset_s * rx_rate``).  The constant propagation/clock delay cancels in
        the difference.  One correction: each capture is trimmed to its burst's energy onset in
        whole 64-sample blocks (:meth:`_onset`), which at a scale whose row is not a whole 64 samples
        shifts the measured start phase; the anchor keeps its own trim, so the change in trim between
        the two captures is subtracted here (it is zero at a matched rate, where a row is 64 samples).
        The carrier is the anchor's (a stable clock holds it).  None when the provider has no schedule
        for ``who`` or no anchor yet (first contact still goes through the lock cache / full scan); a
        wrong prediction is caught by the CRC fallback, so this only ever accelerates."""
        if self._sched_pred is None:
            return None
        anchor = self._sched.get(g.who)
        if anchor is None:
            return None
        try:
            s_now = self._sched_pred(g.who, float(now), window_s=self.contention_s)
        except Exception:                               # a prediction is best effort; never a lock
            return None
        if s_now is None:
            return None
        s_anchor, start_phase, carrier, onset_anchor = anchor
        ell = g.known.L / g.known.scale
        pred = (start_phase + (s_now - s_anchor) * self.rx_rate
                - (self._last_onset - onset_anchor)) % ell
        return (float(pred), float(carrier), phy.TRACK_WINDOW)

    def _anchor_schedule(self, who: bytes, rx, g: Geometry, now: float) -> None:
        """Record this CRC-passing read as ``who``'s 7c prediction anchor: the schedule's offset for
        this slot, the start phase just measured, the carrier, and the capture's onset trim (so the
        next burst's prediction can correct for a different trim).  Only when the provider runs a
        schedule for ``who`` (else the lock cache carries a steady-start sender as before)."""
        if self._sched_pred is None:
            return
        try:
            s = self._sched_pred(who, float(now), window_s=self.contention_s)
        except Exception:
            s = None
        if s is None:
            return
        sd = rx.discovery
        self._sched[who] = (float(s), float(sd.offset) + float(sd.phase), float(sd.carrier),
                            int(self._last_onset))

    @staticmethod
    def _within(rx, g: Geometry, tracked) -> bool:
        """Whether the locked start fell inside the prior's §3a window -- i.e. the local scan found
        it, so this burst took the fast path and not the full re-scan fallback.  ``tracked`` is the
        prior that was passed (lock cache ``(start, carrier)`` or 7c ``(start, carrier, window)``)."""
        ell = g.known.L / g.known.scale
        sd = rx.discovery
        sp = float(sd.offset) + float(sd.phase)
        win = float(tracked[2]) if len(tracked) > 2 else phy.TRACK_WINDOW
        return abs((sp - tracked[0] + 0.5 * ell) % ell - 0.5 * ell) <= win

    def _read(self, iq, known: phy.Known, tracked=None):
        """Read a burst at a known geometry.  ``tracked`` is a prior ``(start_phase, carrier)`` from
        a previous CRC-passing read of the same sender (``NeighborTable.tracked``): it restricts the
        scan to the §3a local window around that start, a pure speed-up on a tracked sender.  None
        (first contact, or a sender never locked) is the full scan, byte-identical to before; a
        stale prior whose local lock fails the frame check is re-scanned in full by
        ``phy.receive_known`` (the CRC is the confidence gate, so a prior is never a wrong lock)."""
        rx = phy.receive_known(iq, self.key, known, accept=_frames, tracked=tracked)
        sym = unframe(rx) if rx.locked else None
        return sym, rx

    def _rescan(self, iq, now):
        """A known sender that moved sub-band at the same width: its scale is known, the band
        centre is measured from this capture (``waveform._coarse_center``)."""
        c = wf._coarse_center(iq)
        if c is None:
            return []
        out = []
        for L, scale in {(g.known.L, g.known.scale) for g in self.geoms}:
            if any(g.known.L == L and g.known.scale == scale and abs(g.known.center - c) < 0.01
                   for g in self.geoms):
                continue
            self.stats.rescans += 1
            sym, rx = self._read(iq, phy.Known(L, scale, float(c)))
            if sym is not None:
                self.stats.rescan_hits += 1
                d = rx.discovery
                g = self.learn(phy.Known(L, scale, float(d.carrier) * scale / L), None, now, "rescan")
                g.last = now
                out.append((sym, g, rx))
                break
        return out

    def _blind(self, iq, now):
        """First contact, or a sender that changed width: discovery from the capture and the key
        alone.  Bounded: at most once per ``blind_every`` seconds, and a sender narrower than this
        receiver (the slow continuous-scale search) only when ``blind_scaled``."""
        if now - self._last_blind < self.blind_every:
            return []
        if not self.blind_scaled:
            br = wf._coarse_scale(iq)
            if br is not None and br[1] < 1.0:
                return []
        self._last_blind = now
        self.stats.blind_tries += 1
        t0 = time.perf_counter()
        cands = sorted({w + d for w in self.widths for d in range(-4, 5)})
        try:
            rx = phy.receive_burst(iq, self.key, candidates=cands)
        except (ValueError, np.linalg.LinAlgError):
            rx = None
        self.stats.blind_s += time.perf_counter() - t0
        if rx is None or not rx.locked or (rx.soft.size == 0 and rx.qam.size == 0):
            return []
        sym = unframe(rx)
        if sym is None:
            return []
        self.stats.blind_hits += 1
        d = rx.discovery
        scale = float(d.scale)
        g = self.learn(phy.Known(int(d.L), scale, float(d.carrier) * scale / int(d.L)), None, now, "blind")
        g.last = now
        return [(sym, g, rx)]

    def _observe(self, iq, now):
        try:
            br = wf._coarse_scale(iq)
            c = wf._coarse_center(iq)
            inter = wf.interference(iq, self.widths[0])
            strehl = float(getattr(inter, "strehl", 0.0))
        except Exception:                  # the reads are best effort; the capture is still handed over
            br, c, strehl = None, None, 0.0
        occ = 0.5 * (br[0] + br[1]) if br is not None else 0.0
        self.providers.interference.observe(BandObservation(now, occ, 0.0 if c is None else float(c), strehl,
                                                       rate=self.rx_rate, cell=float(self.radio.min_rate),
                                                       iq=iq))

    NOISE_MEMORY_S = 1.0            # s: quiet rows of earlier reads at a geometry kept for its noise

    def _heard_at(self, rx, g: Geometry | None = None, now: float = 0.0):
        """``(level_db, snr_db)`` of one decoded burst, read on the burst itself: its own rows at
        its own geometry, over the rows of the same band around it (``phy.burst_snr``) -- never
        the capture's loudest part, which another sender in the same slot may own.  The burst's
        length is the frame's: the reference row and the rows the mode the CRC picked fills.  The
        band's noise is read on the quiet rows of this capture and of the reads at the same
        geometry in the last ``NOISE_MEMORY_S`` (a burst that fills the slot leaves one quiet row
        of its own).  ``level_db`` is the burst's power per coordinate, ~ its power per received
        sample (dB of the receiver's full scale).  None for either when no quiet row is held."""
        got = read_frame(rx)                         # cached: the mode the CRC picked
        if got is None or rx.power.size == 0:
            return None, None
        family, M, rate = got[1]
        n_sym = -(-fec.packet_coded_length(FRAME_BYTES, rate) // int(round(math.log2(int(M)))))
        prior = []
        if g is not None:
            g.quiet = [(t, q) for t, q in g.quiet if now - t <= self.NOISE_MEMORY_S]
            prior = [q for _, q in g.quiet]
        b = phy.burst_snr(rx, family, 1 + -(-n_sym // int(rx.power.shape[1])),
                          np.concatenate(prior) if prior else ())
        if b is None:
            return None, None
        if g is not None and b[3].size:
            g.quiet.append((now, b[3]))
        if b[1] <= 0 or b[0] is None:
            return None, None
        return 10 * math.log10(b[1]), b[0]

    def _frame_in(self, sym, g: Geometry, rx, now) -> bool:
        self.stats.frames += 1
        hd = sym.header
        self._know(hd.origin)
        who = self._transmitter(sym)
        if who is not None and who != self.node_id:
            g.who = who
            d = rx.discovery
            got = read_frame(rx)                     # cached: the mode the CRC picked
            if got is not None and got[1][0] == "coherent" and rx.coherent is not None:
                d = rx.coherent
            M = got[1][1] if got is not None else int(d.M)
            level, snr = self._heard_at(rx, g, now)
            self._record(who, Measured(now, g.known.scale, g.known.L, int(d.K), int(M), float(d.carrier),
                                       math.nan if level is None else float(level), snr))
            # Cache the scan's own start and carrier (``rx.discovery``, what the next read's scan
            # reuses -- not the coherent re-settle in ``d``) as this sender's §3a prior.  The table
            # passes it to the local scan only once this sender's start phase has held steady across
            # two bursts (``ell`` samples a row); a sender that jitters its start keeps the full scan.
            sd = rx.discovery
            self.table.locked(who, float(sd.offset) + float(sd.phase), float(sd.carrier),
                              ell=g.known.L / g.known.scale, window=phy.TRACK_WINDOW)
            # 7c: if this sender is on a keyed schedule, anchor its start prediction here too, so the
            # next burst is read local even if its start jitters (the lock cache alone could not).
            self._anchor_schedule(who, rx, g, now)
        return self._take(sym, g.known.center * self.rx_rate, now, who)

    def _take(self, sym, center_hz: float, now, who: bytes | None = None) -> bool:
        """One decoded symbol, heard on ``center_hz`` from ``who`` (the measured transmitter, when
        known), through the relay rules: what it changes in this node's queue, schedule and inbox.
        Returns whether it was new."""
        hd = sym.header
        self._touched[sym.header.msg_id] = now
        self._heard_bands.setdefault(hd.msg_id, set()).add(float(center_hz))
        may = self._steer_relay(hd, who) if self.steer else None
        t = relay.take(self, sym, now, self.policy, self.region, may_relay=may)
        if not t.new:
            return False
        self.stats.acked += t.acked
        self.stats.cancelled += t.cancelled
        if t.idle:
            self.due = None
        if t.forwarding:
            self._heard_on[hd.msg_id] = float(center_hz)
            if self.due is None:
                self.due = now + self.slot_s * (1 + int(self.rng.integers(0, self.window)))
        if t.delivered is not None:
            self._deliver(hd, t, now)
        return True

    def _own_fix_trusted(self) -> bool:
        """Whether this node's believed position is consistent with the air -- the coherent-spoof check.
        For each authoritative anchor this node has heard, the measured SNR caps how far the anchor can
        be (free space is the best case, :func:`plausibility.max_distance_m`); if the believed position
        puts the anchor farther than that cap, the fix cannot be real (a spoof moved this node), so
        steering falls back to flooding.  No anchors, or none heard yet, leaves the fix trusted.  A
        common offset over a whole area -- the coherent spoof that passes pairwise checks -- cannot stay
        consistent with anchors in different directions, so a ring of anchors catches any offset."""
        if not self.anchors or self.position is None:
            return True
        for aid, apos in self.anchors.items():
            nb = self.table.get(aid)
            snr = nb.measured.snr_db if (nb is not None and nb.measured is not None) else None
            if snr is None:
                continue
            cap = plausibility.max_distance_m(snr - self.anchor_tol_db, self.radio, self.rx_rate)
            if self.position.distance_m(apos) > cap:
                return False
        return True

    def _steer_relay(self, hd: Header, who: bytes | None) -> bool | None:
        """The position-aware forwarding gate (:mod:`steer`).  Returns ``None`` to defer to flooding
        (no trusted fix on the destination, or no own position), ``True``/``False`` to steer: relay
        only while this node descends the gradient toward the destination.

        Everything it reads is already local and trust-checked: this node's own position, the
        destination's trusted announced position (``distrusted`` excludes a claim the air contradicts),
        and the immediate sender ``who``'s trusted announced position to measure the sender's distance
        to the destination -- so a spoofed position cannot steer the mesh, and the sender's progress is
        recomputed here rather than carried on the wire."""
        me = self.position if self._own_fix_trusted() else None
        if me is None:                               # no fix, or a spoofed one -> diffuse (flood)
            return None
        distrusted = frozenset(self.distrusted)
        if hd.to == HOME:
            target = steer.nearest_gateway(self.table, me, distrusted=distrusted)
        elif hd.to in _SPECIAL:                      # BROADCAST, ANNOUNCE: everyone is a destination
            return None
        else:
            target = steer.target_position(hd.to, self.table, distrusted=distrusted)
        if target is None:
            return None                              # destination unknown here -> diffuse
        d_prev = steer.INF
        spos = None
        if who is not None and who not in distrusted:
            nb = self.table.get(who)
            spos = nb.announced.position if (nb is not None and nb.announced is not None) else None
            if spos is not None:
                d_prev = spos.distance_m(target)
        dec = steer.relay_decision(me, target, d_prev, margin_m=self.steer_margin_m)
        if dec.mode is steer.Mode.FLOOD:
            return None
        if dec.relay:
            if self.steer_width > 0 and who is not None:
                return True if self._in_steer_width(hd, target, d_prev, distrusted, who) else False
            return True
        if self.steer_recover:                         # would suppress: escape a void, never a dead end
            cands = {nb.node_id: nb.announced.position for nb in self.table
                     if nb.announced is not None and nb.announced.position is not None
                     and nb.node_id not in distrusted and nb.node_id != hd.to}
            if steer.best_progress(target, cands) >= d_prev - self.steer_margin_m:
                return None                            # local minimum: nothing closer known -> flood
        return False

    def _in_steer_width(self, hd: Header, target, d_prev: float, distrusted, who: bytes) -> bool:
        """Whether this node is among the best ``steer_width`` descending neighbours of the sender (by
        progress toward the target), so narrowing keeps the branch small in a dense field.  The sender's
        neighbours -- the nodes that could also carry this hop -- are the ones the sender announced
        HEARING (its heard-map), which is the measured set, not a conservative planning radius; this node
        is always included.  Candidates are filtered to trusted known positions, so a spoofer cannot
        claim a rank its measured position does not support."""
        nb_s = self.table.get(who)
        heard = nb_s.announced.heard if (nb_s is not None and nb_s.announced is not None) else {}
        cands = {self.node_id: self.position}
        for nid in heard:
            if (nid in distrusted or nid == self.node_id or nid == hd.to or nid == hd.origin):
                continue
            nb = self.table.get(nid)
            pos = nb.announced.position if (nb is not None and nb.announced is not None) else None
            if pos is not None:
                cands[nid] = pos
        ranked = steer.rank_toward(target, cands, d_prev, margin_m=self.steer_margin_m,
                                   width=self.steer_width)
        return self.node_id in {nid for nid, _ in ranked}

    def _deliver(self, h: Header, t: relay.Taken, now: float) -> None:
        payload = t.delivered
        if h.to == ANNOUNCE or h.to == LOCATE:        # a gossiped position: ingest, never to the inbox
            self.on_announcement(payload, h.origin, now)
            return
        if t.anycast and self.backhaul is not None and not self.backhaul(h, payload, self):
            self.stats.duplicates += 1
            return
        if self.tunnel is not None:
            for d in self.tunnel.receive(h, payload, now):
                self._hand_up(d, now)
            return
        plain = self.crypto.open(payload, context=_context(h))
        if plain is None:
            self.stats.bad += 1
            return
        m = Delivered(h.origin, h.to, plain, now, h.msg_id)
        self.inbox.append(m)
        if self.on_message is not None:
            self.on_message(m)

    def _hand_up(self, d, now):
        if d.app:
            m = Delivered(d.origin, self.node_id, d.payload, now, (d.origin, d.flow))
            self.inbox.append(m)
            if self.on_message is not None:
                self.on_message(m)
        else:
            self.ip_out.append(d)

    # ── input: announcements ─────────────────────────────────────────────────────────────────

    def on_announcement(self, sealed: bytes, sender: bytes, now: float):
        """A sealed "I'm here" from ``sender``: into the table as announced (clues, never
        constraints), then the position claim judged against what the air says."""
        got = announce.open_sealed(self.crypto, sender, sealed, now)
        if got is None:
            self.stats.bad += 1
            return None
        nid, a = got
        if nid == self.node_id:
            return None
        n = self.table.get(nid)
        prev = n.announced if n is not None else None
        self.table.announced(nid, a)
        self.stats.announcements += 1
        self._know(nid)
        for h in a.heard:
            self._know(h)
        self._judge(nid, prev, now=now)
        return a

    def _record(self, who: bytes, m: Measured) -> None:
        """Keep a measurement of ``who``.  It can contradict (or vindicate) a position claimed
        earlier, so the claim is judged again now, keeping the last speed verdict -- a measurement
        says nothing about how the node flew."""
        self.table.measured(who, m)
        if m.snr_db is not None:
            last = self.verdicts.get(who)
            self._judge(who, fast_prior=last[2] if last is not None else None, now=m.t)

    def _judge(self, nid: bytes, prev=None, fast_prior: bool | None = None,
               now: float | None = None) -> None:
        """Judge ``nid``'s position claim against every SNR measured of it (``plausibility``).

        A measured SNR caps the distance between the two at the moment it was measured.  The
        claim was made at its own moment (``a.t``, when it was announced) and the observer's
        position is from another (now for this node's own position; the announcement for another
        observer's), and either end may have flown between them -- measured in the fleet showcase
        before this: drones distrusted their own formation, a claim announced one, two, three
        epochs earlier judged against a fresh SNR and the observer's new position, the excess
        growing 2.4 km an epoch, exactly the fleet's motion.  So each cap is widened by
        ``max_speed`` times the time from the measurement to the claim plus the time from the
        measurement to the observer's position.  This node's own reads carry their time; an
        announced ``heard`` value does not, and is at most the announcer's table age
        (``table.max_age``, the bound ``announcement`` keeps) older than its announcement."""
        n = self.table.get(nid)
        a = n.announced if n is not None else None
        if a is None or a.position is None:
            return
        now = a.t if now is None else float(now)
        v = self.max_speed
        obs = []
        m = n.measured
        if self.position is not None and m is not None and m.snr_db is not None:
            obs.append((self.position, m.snr_db, v * (abs(a.t - m.t) + abs(now - m.t))))
        for o in self.table:
            oa = o.announced
            if (o.node_id in (nid, self.node_id) or o.node_id in self.distrusted or oa is None
                    or oa.position is None or nid not in oa.heard):
                continue
            obs.append((oa.position, oa.heard[nid],
                        v * (abs(a.t - oa.t) + 2.0 * self.table.max_age)))
        rate = m.scale * self.rx_rate if m is not None else self.radio.max_rate
        v = (plausibility.check_claim(a.position, obs, self.radio, rate) if obs
             else plausibility.Verdict(True, 0.0, None))
        if prev is None and fast_prior is not None:
            fast = fast_prior
        else:
            fast = (prev is None or prev.position is None
                    or plausibility.speed_ok(prev.position, a.position, a.t - prev.t, self.max_speed))
        ok = v.ok and fast
        self.verdicts[nid] = (ok, v, fast)
        if ok:
            self.distrusted.discard(nid)
        else:
            self.distrusted.add(nid)

    def announcement(self, now: float) -> bytes:
        """This node's sealed state & capability announcement: position, velocity, capture band,
        transmit power, and whom it heard at what SNR (physical measurements and capability figures;
        never decode quality).  TX power lets a receiver turn a measured RSS into a coarse range."""
        heard = {nb.node_id: nb.measured.snr_db for nb in self.table
                 if nb.measured is not None and nb.measured.snr_db is not None
                 and now - nb.measured.t <= self.table.max_age}
        band = BandRange(self.radio.min_rate, self.radio.max_rate, self.radio.center_hz)
        return announce.seal(self.crypto, self.node_id, position=self.position,
                             velocity=self.velocity, band=band, tx_power_dbm=self.radio.max_power_dbm,
                             interference=self.providers.interference.interference_bands(),
                             heard=dict(list(heard.items())[:255]), gateway=HOME in self.accepts)

    # ── input: own traffic ───────────────────────────────────────────────────────────────────

    def attach(self, tunnel) -> None:
        """Host a :class:`~entroptics_mesh.tunnel.Tunnel`: its messages go out as this node's
        (one sequence space), decoded messages go to it, and it takes over ``send``."""
        if tunnel.node_id != self.node_id:
            raise ValueError("the tunnel belongs to another node")
        tunnel.router = self.router
        self.tunnel = tunnel
        self.accepts = self.accepts | tunnel.accepts
        if HOME in self.accepts:
            self.homes.add(self.node_id)

    def send(self, payload: bytes, to: bytes, now: float, *, hops: int | None = None,
             redundancy: float | None = None, budget_ms: int = 0):
        """Queue a message.  Returns its id (None when a hosted tunnel took it as an application
        record, False when the tunnel refused it)."""
        if self.tunnel is not None:
            ok = self.tunnel.send(payload, now, to=to, app=True)
            self._pull(now)
            return None if ok is not False else False
        h = self.router.originate(to, self.hops if hops is None else hops)
        if budget_ms:
            h = Header(h.origin, h.seq, h.to, h.hops, int(budget_ms))
        sealed = self.crypto.seal(bytes(payload), context=_context(h))
        relay.originate(self, h, sealed, BLOCK, self.redundancy if redundancy is None else redundancy, now)
        if self.due is None:
            self.due = now
        return h.msg_id

    def _pull(self, now: float) -> None:
        """Let the tunnel pack whatever queued behind the radio, when no message of ours waits."""
        if self.tunnel is None or any(q.header.origin == self.node_id for q in self.queue):
            return
        out = self.tunnel.pull(now)
        if out is not None:
            relay.originate(self, out.header, out.sealed, BLOCK, out.redundancy, now)
            if self.due is None:
                self.due = now

    def tick(self, now: float) -> None:
        self.table.expire(now)
        self._forget(now)
        self.geoms = [g for g in self.geoms
                      if g.how == "seeded" or now - max(g.last, g.learned) <= self.geometry_age]
        if self.tunnel is not None:
            for d in self.tunnel.backhauled:
                self._hand_up(d, now)
            self.tunnel.backhauled.clear()
            self._pull(now)
        if now >= self._next_announce:
            self._next_announce = now + float(self.announce_every)
            sealed = self.announcement(now)
            h = self.router.originate(ANNOUNCE, 1)
            relay.originate(self, h, sealed, BLOCK, 1.0, now)
            if self.locate_hops > 0:                   # also gossip position a bounded number of hops
                # sealed again: a sealing provider's nonce is per seal, so a receiver that opens
                # both copies does not refuse the second as a replay
                hl = self.router.originate(LOCATE, self.locate_hops)
                relay.originate(self, hl, self.announcement(now), BLOCK, 1.0, now)
            self.stats.announced += 1
            if self.due is None:
                self.due = now

    def _forget(self, now: float) -> None:
        """Bound memory by age: everything held for a message idle longer than ``memory_age``
        goes -- its seen symbols, holding time, heard-on band and reassembly state.  A message
        still queued for sending is kept (its own time budget ends it)."""
        for m, t in self.held.items():               # messages this node originated
            self._touched.setdefault(m, t)
        queued = {q.header.msg_id for q in self.queue}
        old = {m for m, t in self._touched.items()
               if now - t > self.memory_age and m not in queued}
        if not old:
            return
        self.seen = {k for k in self.seen if (k[0], k[1]) not in old}
        for m in old:
            self.held.pop(m, None)
            self._sent.pop(m, None)
            self._heard_on.pop(m, None)
            self._heard_bands.pop(m, None)
            del self._touched[m]
        self.rx.forget(old)

    # ── output ───────────────────────────────────────────────────────────────────────────────

    def plan_for(self, header: Header, now: float = 0.0):
        """The plan for one burst of a message: the core default fitted to its audience, then the
        agility provider, then the interference review (None: hold)."""
        dest = header.to if (header.to not in _SPECIAL and header.to not in self.distrusted
                             and header.to not in self.accepts) else None
        intent = Intent(to=dest, n_bytes=FRAME_BYTES)
        heard_on = self._heard_on.get(header.msg_id) if header.origin != self.node_id else None
        # A destination's reported interference bands are RELATIVE to its own band centre; resolve them to
        # this node's centre-offset space (where the plan's ``center`` lives) so the planner can
        # steer off them -- the clue only applies when we know who the destination is.
        interference = ()
        dn = self.table.get(dest) if dest is not None else None
        da = dn.announced if dn is not None else None
        if da is not None and da.band is not None and da.interference:
            dc = da.band.center_hz
            interference = tuple(((dc + j.offset_hz - j.width_hz / 2) - self.radio.center_hz,
                            (dc + j.offset_hz + j.width_hz / 2) - self.radio.center_hz)
                           for j in da.interference)
        got = adapt.default_plan(intent, self.table, self.radio, me=self.position,
                                 heard_on=heard_on, rng=self.rng, families=self.families,
                                 avoid=self._heard_bands.get(header.msg_id, ()), interference=interference)
        plan = got[0] if isinstance(got, tuple) else got
        ctx = PlanContext(me=self.position, velocity=self.velocity, t=now, node_id=self.node_id)
        chosen = self.providers.agility.plan(intent, plan, self.table, ctx)
        chosen = adapt.refit(plan, chosen, self.families)    # a wider or quieter plan sheds rate
        return self.providers.interference.review(chosen)

    def next_symbol(self, now: float):
        """``(Symbol, TxPlan)`` to transmit now -- taken off the queue and counted as sent -- or
        None.  ``next_burst`` composes it; an emulator that abstracts the PHY away sends it as is."""
        self._pull(now)
        if self.due is None or now + 1e-9 < self.due:
            return None
        if not self.queue:
            self.due = None
            return None
        if self.interleave:
            self._interleave()
        plan = self.plan_for(self.queue[0].header, now)
        if plan is None:
            self.stats.held += 1
            self.due = now + self.slot_s
            return None
        sym, dropped = relay.pop(self, now, 1000.0, self.slot_s)
        if sym is None:
            self.stats.expired += dropped
            self.due = now + self.slot_s if self.queue else None
            return None
        self._sent[sym.header.msg_id] = self._sent.get(sym.header.msg_id, 0) + 1
        self.last_sent = sym
        self.stats.bursts += 1
        self._backoff = 1
        self.due = (now + self.slot_s * (1 + int(self.rng.integers(0, self.window)))
                    if self.queue else None)
        return sym, plan

    def _interleave(self) -> None:
        """Bring to the front the queued symbol of the message this node has sent the fewest
        symbols of (the earliest queued among equals): a message's first symbol goes before
        another's second, so a new message -- its own or one it relays -- does not wait behind
        the redundancy of older ones, and each message's symbols spread out in time."""
        best = min(range(len(self.queue)), key=lambda j: (self._sent.get(self.queue[j].header.msg_id, 0), j))
        if best:
            self.queue.insert(0, self.queue.pop(best))

    def next_burst(self, now: float):
        """``(iq, TxPlan)`` to transmit now, or None.  The iq is at ``plan.rate``, unit RMS; the
        radio applies ``plan.power_db`` and ``plan.center``.  This is the call without listening
        first; a radio that listens uses ``start`` and ``commit``."""
        got = self.next_symbol(now)
        if got is None:
            return None
        sym, plan = got
        return phy.transmit_burst(frame(sym, plan.code_rate), plan, self.key, sender=self.node_id), plan

    # ── listen before talk ───────────────────────────────────────────────────────────────────
    #
    # A burst that is due does not go at the top of the node's slot.  The node draws a start at
    # random inside its contention window (``contention_s``, its own clock), the radio listens over
    # the ``listen_s`` before that start, and the node sends only if what it heard is not above
    # its own measured floor by more than its gate (``gate_db`` -- the same test that decides
    # whether a capture is worth reading).  Busy: it defers to a later slot, drawn at random from
    # a window that doubles with each deferral in a row (``backoff_max`` times ``window`` at
    # most) and resets once it sends.  Everything is local -- its own clock, its own floor, its
    # own draws; nothing is agreed, nothing is sent to anyone, and the start times carry no shared
    # grid.  A node with no floor yet (it has heard nothing) sends.

    def start(self, now: float) -> float | None:
        """When, within this slot, the burst due now would start (seconds after ``now``), or None
        when nothing is due.  The radio listens for ``listen_s`` up to that moment and passes
        what it heard to ``commit``."""
        self._pull(now)
        if self.due is None or now + 1e-9 < self.due or not self.queue:
            return None
        if self.contention_s <= 0:
            return 0.0
        s = self._scheduled_start(now)                  # a keyed schedule's offset, or None
        if s is not None:
            return float(min(max(s, 0.0), self.contention_s))
        return float(self.rng.uniform(0.0, self.contention_s))

    def scheduled_tx_offset(self, now: float) -> float:
        """The keyed transmit start offset (seconds) within the contention window for a burst placed
        now, clamped to ``[0, contention_s]`` -- or ``0.0`` when no keyed schedule is attached (the
        default), which places the burst exactly as before (byte-identical).  A driver that composes a
        burst with :meth:`next_burst` (rather than the listen-before-talk :meth:`start`/:meth:`commit`)
        calls this to position the burst at the keyed offset a keyed receiver predicts; it changes only
        WHEN the burst is placed, never the burst's samples (the schedule leaves the plan untouched)."""
        s = self._scheduled_start(now)
        if s is None:
            return 0.0
        return float(min(max(s, 0.0), self.contention_s))

    def _scheduled_start(self, now: float) -> float | None:
        """This node's keyed transmit offset for the burst due now (roadmap 7c), or None to draw at
        random.  None under the null agility provider (and any that only plans the waveform), so the
        default access is byte-identical; a keyed schedule makes the start random-looking yet
        predictable to a keyed receiver, which reads the burst on its local scan."""
        if self._sched_tx is None:
            return None
        try:
            ctx = PlanContext(me=self.position, velocity=self.velocity, t=now, node_id=self.node_id)
            return self._sched_tx(ctx, window_s=self.contention_s)
        except Exception:                               # best effort; fall back to the random draw
            return None

    def busy(self, heard) -> bool:
        """Whether a listen heard the channel taken: its mean power over this node's floor."""
        heard = np.asarray(heard)
        if heard.size == 0:
            return False
        return self.busy_db(10 * math.log10(max(float(np.mean(np.abs(heard) ** 2)), 1e-30)))

    def busy_db(self, level_db: float) -> bool:
        """The decision ``busy`` makes, on a level already measured (dB, the capture's scale)."""
        return self.floor_db is not None and level_db - self.floor_db > self.gate_db

    def defer(self, now: float) -> None:
        """The listen heard the channel taken: try again in a later slot, at random, from a window
        that doubles with each deferral in a row."""
        self.stats.deferred += 1
        w = self.window * self._backoff
        self._backoff = min(2 * self._backoff, self.backoff_max)
        self.due = now + self.slot_s * (1 + int(self.rng.integers(0, w)))

    def commit(self, now: float, heard=None):
        """``next_burst`` after a listen: ``heard`` is what the radio captured over the
        ``listen_s`` before the start ``start`` gave (None: it did not listen).  Busy -> the
        node defers and returns None."""
        if heard is not None and self.busy(heard):
            self.defer(now)
            return None
        return self.next_burst(now)

    # ── placement ────────────────────────────────────────────────────────────────────────────

    def link_map(self):
        """(positions, heard) as this node knows them: announced positions (and its own), and
        heard[(a, b)] = the SNR b heard a at -- from announcements' heard lists and its own reads."""
        pos = {nb.node_id: nb.announced.position for nb in self.table
               if nb.announced is not None and nb.announced.position is not None}
        if self.position is not None:
            pos[self.node_id] = self.position
        heard = {}
        for nb in self.table:
            if nb.announced is not None:
                for a, snr in nb.announced.heard.items():
                    heard[(a, nb.node_id)] = snr
            if nb.measured is not None and nb.measured.snr_db is not None:
                heard[(nb.node_id, self.node_id)] = nb.measured.snr_db
        return pos, heard

    def cut_off(self, homes=None, rate: float | None = None) -> set:
        """Nodes with a known position that no usable link connects to any home."""
        pos, heard = self.link_map()
        homes = set(self.homes if homes is None else homes)
        usable = placement.links(pos, heard, self.radio, rate or self.radio.max_rate,
                                 distrusted=frozenset(self.distrusted))
        ok = placement.reachable(usable, homes)
        return {n for n in pos if n not in ok}

    def relay_plan(self, homes=None, *, relay_alt: float, rate: float | None = None,
                   parallel: int = 2) -> dict:
        """Relay slots that would reconnect every cut-off node (``placement.plan``)."""
        pos, heard = self.link_map()
        homes = set(self.homes if homes is None else homes)
        return placement.plan(pos, heard, homes, self.radio, rate or self.radio.max_rate,
                              relay_alt=relay_alt, parallel=parallel,
                              distrusted=frozenset(self.distrusted))


__all__ = ["ANNOUNCE", "Delivered", "Geometry", "Node", "NodeStats", "Wire"]
