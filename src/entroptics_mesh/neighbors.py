"""
neighbors.py -- what each sender was measured to be, and what it said about itself.

Two kinds of knowledge, kept apart because they are trusted differently:

    measured    read off this node's own captures of the sender: its scale relative to our clock
                (``scale``), its geometry (L, K, M), carrier, and received level.  This is what the
                physical layer re-uses so a known sender is acquired in milliseconds rather than by a
                blind search -- measured state, never pre-shared.
    announced   what the sender put in an optional "I'm here": position, velocity, the band range
                it can capture, and whom it heard at what SNR.  Clues, never constraints: hearing a
                neighbour talk narrowband does not oblige anyone to go narrow.

Nothing here is required for a link to work.  A sender that never announces still has a measured
entry once it is heard.  A listen-only node never transmits, so it appears in no one's table -- and
needs to, since nothing it receives depends on being known.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field


@dataclass(frozen=True)
class Position:
    lat: float
    lon: float
    alt: float = 0.0                      # metres

    def distance_m(self, other: "Position") -> float:
        """Great-circle distance plus altitude, metres (haversine on a sphere; ample for link
        budgets)."""
        R = 6_371_000.0
        p1, p2 = math.radians(self.lat), math.radians(other.lat)
        dp, dl = p2 - p1, math.radians(other.lon - self.lon)
        a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
        ground = 2 * R * math.asin(min(1.0, math.sqrt(a)))
        return math.hypot(ground, other.alt - self.alt)


@dataclass(frozen=True)
class BandRange:
    """A band a node can capture: sample-rate range (Hz) around a tuned centre (Hz)."""
    min_rate: float
    max_rate: float
    center_hz: float


@dataclass(frozen=True)
class InterferenceBand:
    """A band this node's RECEIVER measures interference on, as an offset + width RELATIVE to the
    node's own announced band centre (never a standalone absolute frequency; a peer resolves it
    against the announced ``BandRange.center_hz``), with the interferer's bearing and level when the
    array / front-end can measure them.

    It is a physical measurement of the local RF environment -- which is the point: the interference
    is local to this node, so a distant peer could not know to avoid it unless told.  To a peer
    sending TO this node it is a clue (steer off the band), never a constraint; ``bearing_deg`` says which
    direction the interference arrives from at this node's array.  It is NOT a decode-quality report: it is read from ambient energy on this
    node's receiver, not from any sender's burst -- so it does not need a transmission to exist and
    stays within the reply-free contract (like the ``heard``/SNR map and ``tx_power``)."""
    offset_hz: float                  # centre of the interfered band, relative to this node's band centre
    width_hz: float
    bearing_deg: float | None = None  # AOA of the interferer in the node's array frame, if measured
    level_db: float | None = None     # interference level, dB, if measured


@dataclass
class Measured:
    """This node's own reads of a sender, from its last capture of it."""
    t: float
    scale: float                          # the sender's rate over ours
    L: int
    K: int
    M: int
    carrier: float                        # cycles per row
    level_db: float                       # received level, dB relative to our receiver's full scale
                                          # (NaN when the read held no quiet row to measure it on)
    snr_db: float | None = None           # our estimate: the burst's own level over the noise in
                                          # its own band (phy.burst_snr, Node._heard_at); local only


@dataclass
class Announced:
    """What the sender said about itself in its last announcement."""
    t: float
    position: Position | None = None
    velocity: tuple[float, float, float] | None = None     # m/s east, north, up
    band: BandRange | None = None
    heard: dict[bytes, float] = field(default_factory=dict)  # node id -> SNR dB
    tx_power_dbm: float | None = None   # the sender's own transmit power -- lets a receiver turn a
                                        # measured RSS into a coarse RANGE (https://github.com/Agience/entroptics-rf/blob/main/research/nav/relative_range.md);
                                        # a capability figure, not feedback
    interference: tuple = field(default_factory=tuple)   # tuple[InterferenceBand]: bands this node's receiver
                                        # measures interference on (relative to its own band centre),
                                        # with the interferer's bearing when known.  A clue to peers
                                        # sending to it.
    gateway: bool = False               # this node accepts HOME (a gateway to other networks); lets a
                                        # peer steer anycast HOME traffic toward the nearest one.


@dataclass
class Neighbor:
    node_id: bytes
    measured: Measured | None = None
    announced: Announced | None = None
    lock: tuple[float, float] | None = None       # this sender's last CRC-passing read, as
                                                   # (start_phase, carrier): start in received
                                                   # samples (taken mod L/scale by the PHY), carrier
                                                   # in cycles per row.  Measured state from a
                                                   # previous burst, never pre-shared.
    lock_prior: tuple[float, float] | None = None  # the prior worth passing as
                                                   # ``receive_known(tracked=...)`` -- the last lock,
                                                   # but only once two consecutive locks agreed on
                                                   # the start phase within the §3a window.  A sender
                                                   # whose start phase holds steady (a stable clock)
                                                   # is read with the local scan; one that jitters
                                                   # its own start every burst (listen-before-talk
                                                   # contention) keeps this None, so the cache
                                                   # degrades to exactly the full scan rather than
                                                   # paying a wasted local scan before the fallback.

    def last_heard(self) -> float:
        return max((x.t for x in (self.measured, self.announced) if x is not None), default=-math.inf)


class NeighborTable:
    """Per-sender state, aged out by the time a node was last heard.  ``max_age`` is how long a
    silent neighbour is still worth keeping; it bounds memory, it decides nothing about a link."""

    def __init__(self, max_age: float = 30.0):
        self.max_age = float(max_age)
        self._n: dict[bytes, Neighbor] = {}

    def _get(self, node_id: bytes) -> Neighbor:
        n = self._n.get(node_id)
        if n is None:
            n = self._n[node_id] = Neighbor(bytes(node_id))
        return n

    def measured(self, node_id: bytes, m: Measured) -> None:
        self._get(node_id).measured = m

    def announced(self, node_id: bytes, a: Announced) -> None:
        self._get(node_id).announced = a

    def get(self, node_id: bytes) -> Neighbor | None:
        return self._n.get(node_id)

    def expire(self, now: float) -> None:
        for k in [k for k, n in self._n.items() if now - n.last_heard() > self.max_age]:
            del self._n[k]

    def __iter__(self):
        return iter(self._n.values())

    def __len__(self) -> int:
        return len(self._n)

    def known_scale(self, node_id: bytes) -> float | None:
        """The scale this node last measured for a sender -- what lets a known sender be read at a
        known scale, in milliseconds, instead of by a blind search."""
        n = self._n.get(node_id)
        return None if n is None or n.measured is None else n.measured.scale

    def locked(self, node_id: bytes, start_phase: float, carrier: float, *,
               ell: float, window: float) -> None:
        """Record a sender's last CRC-passing read as its acquisition prior, and judge whether that
        prior is worth passing to the §3a local scan: it is, only when this lock's start phase
        agrees with the previous one within ``window`` samples on the row torus (``ell`` samples a
        row).  A stable-clock sender (steady start phase) earns the local scan after two agreeing
        locks; a sender that jitters its own start every burst never does, so the cache costs it
        nothing over the full scan."""
        n = self._get(node_id)
        prev = n.lock
        n.lock = (float(start_phase), float(carrier))
        agree = (prev is not None and ell > 0
                 and abs((start_phase - prev[0] + 0.5 * ell) % ell - 0.5 * ell) <= float(window))
        n.lock_prior = n.lock if agree else None

    def tracked(self, node_id: bytes) -> tuple[float, float] | None:
        """The prior to pass as ``receive_known(tracked=...)`` for a sender whose start phase has
        held steady, or None -- never locked, locked only once, or a jittered start -- in which case
        the receive path uses the full scan."""
        n = self._n.get(node_id)
        return None if n is None else n.lock_prior


__all__ = ["Position", "BandRange", "InterferenceBand", "Measured", "Announced", "Neighbor", "NeighborTable"]
