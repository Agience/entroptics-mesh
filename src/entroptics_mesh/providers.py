"""
providers.py -- the plug points.  The core ships interfaces and null providers; anything that
encrypts, hops, or detects and avoids interference is supplied by the user.

That split is the design: the core only offers the sockets, a networking SDK, and the end user
designs what goes in them.  Every provider here has a null implementation under which the mesh still works
end to end -- as an open broadcast mesh on the public zero-state aperture.

    CryptoProvider    seals and opens payloads, and supplies the physical layer's key (the seed
                      the waveform's aperture is drawn from).  Null: no sealing, no key -- the open
                      zero state anyone can read.
    AgilityProvider   chooses how each transmission goes out: width (sample rate), centre, power,
                      geometry.  The core's default policy fits the audience it has heard; a
                      provider may steer it, e.g. to hop.  Null: the core default.
    InterferenceProvider   receives what the node observes of the band, and may veto or reshape a plan.
                      Null: observes nothing, changes nothing.

A provider never receives anything that must come back from another node: the mesh is one-way,
so a provider that needed a reply to function could not work with listen-only peers.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from .neighbors import Position


# ── the plan a transmission goes out on ──────────────────────────────────────────────────────

@dataclass(frozen=True)
class TxPlan:
    """How one transmission goes out.  Every field is the sender's choice and every one is measured
    by the receiver; none is agreed in advance.

    ``rate``    the sender's sample rate, Hz -- its occupied width.
    ``center``  offset of the transmission's centre from the radio's tuned frequency, Hz.
    ``power_db``transmit gain relative to the radio's maximum (<= 0).
    ``K, L, M`` modes carried, row width, alphabet size.  ``K`` is L: every mode is lit
                (entroptics-rf research/rank_leak.md -- a spare mode is a noise-only direction a
                key-free observer reads the width from).  None, the default, means L; any other
                value is refused.  A plan re-widened with ``replace(plan, L=...)`` passes
                ``K=None`` with it.
    ``family``  "differential" (M-PSK, ``waveform.compose``) or "coherent" (square M-QAM,
                ``waveform.compose_qam``).
    ``code_rate`` the packet code's rate, "1/2", "2/3" or "3/4" (``fec.PUNCTURE_PATTERNS``).
                Family, alphabet and code rate are not signalled: the receiver reads both families
                and the frame's CRC chooses among the hypotheses (``link.read_frame``).
    """
    rate: float
    center: float = 0.0
    power_db: float = -20.0
    K: int | None = None
    L: int = 64
    M: int = 4
    family: str = "differential"
    code_rate: str = "1/2"

    def __post_init__(self) -> None:
        if self.K is None:
            object.__setattr__(self, "K", int(self.L))
        elif int(self.K) != int(self.L):
            raise ValueError(f"every mode is lit: K must be L ({self.L}), got K={self.K}")


@dataclass(frozen=True)
class Intent:
    """What the node wants to send, before a plan is chosen: to whom (None = broadcast), how much,
    and how urgently.  Providers see this, never the payload."""
    to: bytes | None
    n_bytes: int
    priority: int = 0                    # 0 = routine; higher = more urgent (e.g. command, FPV)


@dataclass(frozen=True)
class PlanContext:
    """The sender's own state when it plans a burst -- the raw material a schedule-seeding agility
    provider needs that is not already in the neighbour table.  The RECEIVER's state is read from
    ``table[intent.to]`` (its last-known ``position``/``velocity``/``band`` and our measured
    ``snr_db`` to it); this carries the SENDER's own ``position``/``velocity`` and a timestamp ``t``.
    A provider takes what it wants and ignores the rest.  Extend here as new seeds are wanted -- the
    core only has to fill the fields it has; it never prescribes how a provider uses them.

    ``node_id`` is the sender's own id, carried so a keyed transmit schedule (``transmit_offset``
    below) keys on the same identity a receiver predicts it by (``predict_offset(who, ...)``) -- the
    receiver runs the same provider and names the sender as ``who``, so both derive one start.

    The flight-state fields below are the raw material for BETTER prediction -- a provider that steers
    a directional beam toward a peer, picks a spread/width config, or derives a keyed schedule can
    extrapolate where it (and, with the peer's own announced trajectory, the peer) will be, not just
    where it is now.  All optional and ``None`` by default: a node fills what it knows (from its flight
    controller / the lab flight guide), a provider uses what it wants, and a node with no flight state
    behaves exactly as before.  ``acceleration`` is the current accel vector; ``trajectory`` is the
    planned/predicted future as ``((t, Position), ...)`` waypoints ahead (the in-flight plan);
    ``history`` is recent ``((t, Position, velocity), ...)`` samples (flight history, for maneuver
    extrapolation).  A receiver gets a peer's trajectory/history the same way it gets position today:
    over the optional announcement (``neighbors`` / ``announce``), never on a link communication
    depends on."""
    me: "Position | None" = None
    velocity: tuple[float, float, float] | None = None
    t: float = 0.0
    node_id: bytes | None = None
    acceleration: tuple[float, float, float] | None = None
    trajectory: tuple | None = None                       # ((t, Position), ...) planned/predicted ahead
    history: tuple | None = None                          # ((t, Position, velocity), ...) recent samples


# ── crypto ───────────────────────────────────────────────────────────────────────────────────

@runtime_checkable
class CryptoProvider(Protocol):
    name: str

    def seal(self, plaintext: bytes, *, context: bytes) -> bytes:
        """Authenticated encryption of ``plaintext``; ``context`` (e.g. the frame header) is bound
        but not hidden."""

    def open(self, sealed: bytes, *, context: bytes) -> bytes | None:
        """The plaintext, or None if it fails authentication or was not sealed for this node."""

    def waveform_key(self, context: bytes):
        """The physical layer's key for a transmission in ``context`` -- the seed its aperture is
        drawn from -- or None for the public zero state.

        Key freshness is this provider's to manage.  Under one key every burst repeats the same
        keyed aperture, reference row, per-row mode rotations and per-row power sequence
        (``waveform.row_radius``); none of them gives the content away, but a key held for a long
        time lets an observer who collects many bursts compare like with like.  How often a key
        changes, and how a receiver comes to hold the next one, is the provider's design -- the
        core asks only for the key in force."""

    def address(self, identity: bytes, *, context: bytes = b"") -> bytes:
        """The on-wire token that stands for ``identity`` (an origin or a destination) in a frame
        header, so that which node talks to which is concealed from a relay and from an observer.

        OPTIONAL metadata-privacy hook, the addressing analogue of the keyed transmit schedule:
        a provider returns a keyed-pseudorandom token of (shared secret, a coarse epoch carried in
        ``context``, ``identity``) -- unlinkable across messages and to the true identity for a
        party without the secret, resolvable by a holder.  The DEFAULT returns ``identity``
        unchanged (raw addressing), so a core that never calls it, or calls it under the null
        provider, is byte-identical to one without the hook.

        Wiring (see PROVIDERS.md 'Pseudonymous addressing'): a sender maps the header origin and
        destination through ``address`` just before the header is sealed and sent, using the same
        ``context`` the destination will reconstruct; the seal binds the tokenized header, so
        ``open`` is given the as-received (tokenized) header and is unaffected.  Routing then runs
        on tokens: a relay de-duplicates and forwards on the token it reads and never learns the
        identity, a destination recognizes a frame as its own by ``token == address(self, ...)``.
        A token must stay fixed over a message's life in the mesh (its bursts share a msg_id), so
        a provider's epoch is coarser than the longest cross-mesh delivery; and because routing,
        the neighbour table and the keyed schedule key on whatever identity the header carries,
        a provider that rotates tokens trades a relay's acquire-once tracking of a neighbour for
        the unlinkability -- a deliberate choice the core leaves to the provider."""

    def resolve(self, token: bytes, *, context: bytes = b"") -> bytes | None:
        """The identity a ``token`` from :meth:`address` stands for, for a holder of the secret, or
        None when this provider cannot (or will not) resolve it.  The DEFAULT returns ``token``
        unchanged.  Optional inverse companion to :meth:`address`; the core's routing does not
        require it (a destination self-checks via ``address``), but a provider or a diagnostic may
        use it to attribute a token it holds the key for."""


class NullCrypto:
    """No sealing and no key: payloads travel as given on the public zero-state aperture.  The mesh
    is fully functional under it, as an open broadcast network."""
    name = "null"

    def seal(self, plaintext: bytes, *, context: bytes) -> bytes:
        return bytes(plaintext)

    def open(self, sealed: bytes, *, context: bytes) -> bytes | None:
        return bytes(sealed)

    def waveform_key(self, context: bytes):
        return None

    def address(self, identity: bytes, *, context: bytes = b"") -> bytes:
        return bytes(identity)

    def resolve(self, token: bytes, *, context: bytes = b"") -> bytes | None:
        return bytes(token)


# ── agility ──────────────────────────────────────────────────────────────────────────────────

@runtime_checkable
class AgilityProvider(Protocol):
    name: str

    def plan(self, intent: Intent, default: TxPlan, table, ctx: "PlanContext") -> TxPlan:
        """The plan to transmit on.  ``default`` is the core's audience-fitted plan; ``table`` is the
        node's :class:`~entroptics_mesh.neighbors.NeighborTable` -- clues, never constraints; ``ctx``
        is the sender's own position/velocity/time, for seeding a keyed schedule (the receiver's
        last-known position/band and our measured SNR to it are in ``table[intent.to]``).  Any of it
        may be ignored; a keyed PRNG is the provider's own.  Must need nothing back from a peer."""

    # ── optional: a keyed transmit SCHEDULE (roadmap 7c) ──────────────────────────────────────
    #
    # These two are OPTIONAL -- a provider that only implements ``plan`` (the agility of what a
    # burst looks like) needs neither, and the node detects their absence and falls back to today's
    # random access.  Implement them to coordinate WHEN a burst goes out as well as how: a keyed
    # schedule the receiver -- running the same provider with the same key -- can reproduce, so the
    # sender's transmit start (random-looking to an eavesdropper) is predictable to the receiver,
    # which feeds the prediction to the receive path's local scan (the §3a lever applied to the
    # whole mesh, not only to a stable-clock sender).  Both return seconds in ``[0, window_s)`` or
    # ``None`` ("no schedule / unknown", degrade to random / to the lock cache).  Like the crypto
    # key schedule, they need nothing back from a peer -- only the key plus a coarse shared clock.

    def transmit_offset(self, ctx: "PlanContext", *, window_s: float) -> float | None:
        """This node's own keyed start offset within its contention window ``[0, window_s)`` for the
        burst it is about to send (seeded on ``ctx`` -- its ``node_id`` and ``t``), or None to draw
        at random as the core does by default."""

    def predict_offset(self, who: bytes, t: float, *, window_s: float) -> float | None:
        """The predicted start offset within ``[0, window_s)`` of sender ``who``'s burst at time
        ``t`` -- the same value ``who`` computed from ``transmit_offset`` -- or None when ``who`` is
        not on a schedule this provider can reproduce.  The receiver pairs it with its last measured
        lock of ``who`` to predict the burst's arrival start phase."""

    # ── optional: COVER traffic (metadata privacy, companion to CryptoProvider.address) ───────────
    #
    # Pseudonymous addressing (``CryptoProvider.address``) protects WHO a message is between; it does
    # not protect the traffic pattern -- when bursts are sent and how many -- which anyone receiving
    # the band can still time and count.  Cover traffic is the scheduling answer to that traffic
    # analysis: decoy bursts that pad the timing and volume of real traffic.  It is cheap here
    # because a decoy and a payload burst have the same structure on the air (both read as noise to
    # a receiver without the key, though both are visible as energy); a decoy carries no deliverable
    # message (keyed-random bytes), so a receiver's CRC fails on it and the frame is dropped -- a
    # no-op to the mesh.  OPTIONAL and absent by default: a provider that does
    # not implement it (and the null provider) emits no cover, byte-identical to today.

    def cover_traffic(self, ctx: "PlanContext", *, sending: bool) -> list[TxPlan]:
        """The decoy bursts, if any, to emit at this transmit opportunity (an empty list for none).
        Called once per transmit opportunity; ``sending`` is True when the node is also sending a
        real burst now, so a policy can pad an idle slot to fake presence, add bursts to mask volume,
        or shape the inter-burst timing -- seeded on ``ctx`` (the node's own id/time/position) and
        the provider's key, needing nothing back from a peer.  Each returned :class:`TxPlan` is
        emitted as a burst of keyed-random bytes on that plan; its width/centre/power should be drawn
        from the same distribution as real traffic so a decoy is not separable from a payload burst.
        A provider wanting a cheap authenticated drop (rather than a CRC failure) at an authorized
        receiver seals a reserved cover marker through its crypto provider; that is the provider's
        design, not the core's."""


class NullAgility:
    """Takes the core's plan unchanged, runs no schedule and emits no cover (random access, today's
    default)."""
    name = "null"

    def plan(self, intent: Intent, default: TxPlan, table, ctx: "PlanContext | None" = None) -> TxPlan:
        return default

    def transmit_offset(self, ctx: "PlanContext", *, window_s: float) -> float | None:
        return None

    def predict_offset(self, who: bytes, t: float, *, window_s: float) -> float | None:
        return None

    def cover_traffic(self, ctx: "PlanContext", *, sending: bool) -> list[TxPlan]:
        return []


# ── interference ─────────────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class BandObservation:
    """What the node measured of its band in one capture: the occupied share and its centre as the
    capture's own reads give them, and the interference read (``waveform.interference``).  Raw
    measurements only -- interpretation is the provider's.

    ``iq``, ``rate`` and ``cell`` are the capture itself, its sample rate (Hz, centred on the
    node's band centre), and the narrowest width the node plans a burst at (its radio's minimum
    rate) -- the resolution at which a sender can step off a band.  None when the caller has no
    capture to hand over."""
    t: float
    occupancy: float
    center: float
    interference: float
    rate: float | None = None
    cell: float | None = None
    iq: object = field(default=None, compare=False, repr=False)


@runtime_checkable
class InterferenceProvider(Protocol):
    name: str

    def observe(self, obs: BandObservation) -> None:
        """Called with every band observation the node makes."""

    def review(self, plan: TxPlan) -> TxPlan | None:
        """A plan to transmit instead, or None to hold the transmission."""

    def interference_bands(self) -> list:
        """The bands this node's RECEIVER is currently measuring interference on, to put in its
        state announcement (``neighbors.InterferenceBand``: offset+width RELATIVE to this node's band centre,
        with the interferer's bearing/level when the array can measure them).  A peer sending to
        this node treats them as a clue to steer off.  A physical measurement of the local RF environment, not feedback.

        OPTIONAL: a provider that does not implement it (and the null provider) reports no interference
        bands, so the announcement is byte-identical to one without the hook.  WHICH bands count as
        interference, and the bearing read, are this provider's measurement policy -- the core only
        transports the list and, as a clue, may steer a transmission off a destination's reported
        bands."""
        return []


class NullInterference:
    """Observes nothing and changes nothing, and reports no interference bands."""
    name = "null"

    def observe(self, obs: BandObservation) -> None:
        return None

    def review(self, plan: TxPlan) -> TxPlan | None:
        return plan

    def interference_bands(self) -> list:
        return []


@dataclass
class Providers:
    """The three plug points a node is built with.  Defaults are the null providers."""
    crypto: CryptoProvider = field(default_factory=NullCrypto)
    agility: AgilityProvider = field(default_factory=NullAgility)
    interference: InterferenceProvider = field(default_factory=NullInterference)


__all__ = ["TxPlan", "Intent", "PlanContext", "CryptoProvider", "NullCrypto", "AgilityProvider",
           "NullAgility", "BandObservation", "InterferenceProvider", "NullInterference", "Providers", "replace"]
