"""
steer.py -- gradient-follow forwarding: Reach, in physical space.

Flooding (:mod:`routing`) is diffusion -- correct with no knowledge, every node within the hop
budget relays once.  This is the other half of the pair ("a flood is
diffusion; lightning takes the shortest route"): the Agience **Reach** router (``prism.reach`` +
``ember.optics.next_by_coupling``), ported from abstract coupling-space into physical space so a dense
mesh can steer a message toward a target instead of lighting the whole neighbourhood.

What is kept from Reach, line for line in shape:

  * **One argmax per hop over a measured quantity.**  Reach picks the next hop by the energy a
    candidate basis absorbs from the signal frame.  Here the measured quantity is *progress toward the
    target* -- how much closer this hop gets, gated by whether the hop is physically reachable.  In a
    broadcast RF medium the sender's single argmax becomes "every neighbour above the floor relays",
    which is the branching that carries the redundancy (several leaders, dedup stops re-relay).
  * **A measured floor.**  Reach rejects a hop whose absorbed energy is below ``_float_noise`` -- did
    anything actually couple, or is it rounding?  Here ``margin_m`` is that floor: a hop must get
    meaningfully closer or it does not count, which terminates a stalled front (a dead end) without a
    counter.
  * **Derived termination, never a TTL.**  Reach terminates when nothing couples or the hop is already
    in ``path``.  Here the potential decreases monotonically (self-limiting by geometry) and the
    msg-id seen-set in :mod:`routing` is the ``path``/``fired`` visited-guard -- so there is no fixed
    hop or cost budget, matching the design: always flexible, expect nodes to move, allow very many
    hops to a far target.
  * **The leader emerges from diffusion.**  While no node upstream holds a fix on the target the
    potential is infinite and the message floods; the first node with a fix stamps a finite potential
    and the front converges from there.  So a far target in no routing table is still reached.
  * **Live-first, durable floor.**  Reach delivers live if a receiver is up, else degrades to a
    store-and-forward carrier.  Here: deliver in one hop if the target is a reachable neighbour, else
    forward down the gradient through the mesh (the tunnel's reliable class is the durable floor).

Spoof defence is first-class, not bolted on.  A claimed position is a *claim*; GPS can be spoofed
(meaconing: rebroadcast louder, delayed, to pull a receiver's fix).  The one defence that needs no
cooperation is the air itself: free space is the best case, so a measured SNR caps how far a sender
can be, and a claim beyond that cap is inconsistent (:mod:`plausibility`, wired through
``node._judge`` into the ``distrusted`` set).  The gradient steers **only on trusted positions**: a
node whose claimed position its neighbours' measurements contradict is excluded as an anchor and its
stamped potential is ignored -- so a spoofer announcing "I am next to the target" cannot black-hole
the front it is not physically positioned to earn.  A tampered hint is defended the same way: a node
prefers its own trusted fix of the target over a hint carried on the message, and falls back to
diffusion (never toward an untrusted beacon) when no fix is trustworthy.

This module is pure arithmetic over positions (like :mod:`placement`); it schedules nothing and needs
nothing back.  A position-aware forwarder calls :func:`relay_decision` in place of the flat
hop-budget gate, and :func:`rank_toward` for the sender-side argmax / branch-width narrowing.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from enum import Enum

from .neighbors import Position

INF = float("inf")


class Mode(Enum):
    FLOOD = "flood"       # no trusted target fix: diffuse (defer to the hop-budget flooding gate)
    IGNITE = "ignite"     # first node with a fix: stamp the potential, relay
    DESCEND = "descend"   # a potential is set: relay only while closer than the sender by the floor


@dataclass(frozen=True)
class Decision:
    """Whether to relay, the potential to stamp onward (``d_here``: this node's distance to the
    target, INF when unknown), and which regime produced it.  ``relay`` is None in FLOOD -- the caller
    falls back to its own hop-budget gate rather than this module deciding."""
    relay: bool | None
    d_here: float
    mode: Mode


def target_position(to: bytes, table, hint: Position | None = None,
                    distrusted: frozenset[bytes] = frozenset()) -> Position | None:
    """The target's last-known *trusted* position.

    Preference order, strongest trust first:
      1. the neighbour table's announced position for ``to`` -- authentic (the target's own sealed
         announcement), used unless ``to`` is distrusted;
      2. the ``hint`` carried on the message -- a fallback for a far target not in the table; a node
         uses it only when it has no trusted local fix, so a relay that rewrote the hint cannot
         override what a node measured for itself.

    None when neither is trustworthy -- the node has no fix, so forwarding diffuses (never toward an
    untrusted beacon)."""
    if to not in distrusted and table is not None:
        nb = table.get(to) if hasattr(table, "get") else None
        ann = getattr(nb, "announced", None) if nb is not None else None
        pos = getattr(ann, "position", None) if ann is not None else None
        if pos is not None:
            return pos
    return hint


def nearest_gateway(table, me: Position | None,
                    *, distrusted: frozenset[bytes] = frozenset()) -> Position | None:
    """The trusted announced position of the gateway (a node advertising ``gateway=True`` in its
    announcement) nearest to ``me``, or None.  Anycast HOME traffic steers toward 'any gateway',
    resolved locally to the nearest one each node has heard -- no routing table, nothing pre-shared.
    Distrusted nodes are excluded, so a spoofed gateway claim cannot attract HOME traffic."""
    if me is None or table is None:
        return None
    best, best_d = None, INF
    for nb in table:
        ann = getattr(nb, "announced", None)
        if (ann is None or not getattr(ann, "gateway", False) or ann.position is None
                or nb.node_id in distrusted):
            continue
        d = me.distance_m(ann.position)
        if d < best_d:
            best, best_d = ann.position, d
    return best


def relay_decision(me: Position | None, target: Position | None, d_prev: float,
                   *, margin_m: float = 0.0) -> Decision:
    """The gradient gate for one message at a node -- the ported Reach hop decision.

    ``me``       this node's own position (None: no own fix -> diffuse).
    ``target``   the target's trusted last-known position (None: no fix -> diffuse).
    ``d_prev``   the sender's distance to the target carried on the message (INF: no potential yet).
    ``margin_m`` the progress floor (the ``_float_noise`` analog): the descent a hop must clear to
                 count, which damps thrash near equal ranges and ends a stalled front.

    Returns a :class:`Decision`:
      * ``me``/``target`` unknown           -> FLOOD   (relay is None; caller uses its own gate)
      * ``d_prev`` infinite, target known   -> IGNITE  (relay True; stamp ``d_here``)
      * otherwise                           -> DESCEND (relay iff closer than the sender by the floor)
    """
    if me is None or target is None:
        return Decision(None, INF, Mode.FLOOD)
    d_here = me.distance_m(target)
    if not math.isfinite(d_prev):
        return Decision(True, d_here, Mode.IGNITE)
    return Decision(d_here < d_prev - float(margin_m), d_here, Mode.DESCEND)


def best_progress(target: Position, candidates: dict[bytes, Position],
                  *, distrusted: frozenset[bytes] = frozenset()) -> float:
    """The smallest distance-to-target among trusted candidate neighbours (INF if none).  A node is at
    a local minimum -- a void, where the gradient descent would stall -- when this is not below its own
    distance to the target by the progress margin.  Detecting that locally is what lets the forwarder
    switch to recovery (widen past the void) instead of dying, with no global knowledge."""
    best = INF
    for nid, pos in candidates.items():
        if nid in distrusted or pos is None:
            continue
        best = min(best, pos.distance_m(target))
    return best


def escaped(me: Position, target: Position, d_stuck: float, *, margin_m: float = 0.0) -> bool:
    """In recovery around a void at distance ``d_stuck``: this node has escaped once it is closer to
    the target than the stuck point by the margin, and descent can resume from here."""
    return me.distance_m(target) < d_stuck - float(margin_m)


def rank_toward(target: Position, candidates: dict[bytes, Position], d_prev: float = INF,
                *, margin_m: float = 0.0, distrusted: frozenset[bytes] = frozenset(),
                width: int | None = None) -> list[tuple[bytes, float]]:
    """The sender-side argmax of Reach: candidate neighbours scored by progress toward ``target``
    (``d_prev - distance(candidate, target)``), best first, keeping only those above the ``margin_m``
    floor.  Distrusted candidates are dropped (spoof defence).  ``width`` caps how many are kept -- the
    branch-width / redundancy knob: ``None`` keeps every progressing neighbour (widest, most robust),
    ``1`` is a single best hop (narrowest).  With ``d_prev`` infinite every reachable candidate scores
    by negative distance, so the nearest-to-target rank first.

    This is the narrowing companion to :func:`relay_decision`: the receiver-side gate gives correct
    branching on its own; ``rank_toward`` lets a position-aware sender prune to the best ``width``
    relays when a dense field makes the full branch wasteful."""
    base = d_prev if math.isfinite(d_prev) else 0.0
    scored = []
    for nid, pos in candidates.items():
        if nid in distrusted or pos is None:
            continue
        progress = base - pos.distance_m(target)
        if math.isfinite(d_prev) and progress <= float(margin_m):
            continue
        scored.append((nid, float(progress)))
    scored.sort(key=lambda kv: kv[1], reverse=True)
    return scored if width is None else scored[:max(0, int(width))]


__all__ = ["INF", "Mode", "Decision", "target_position", "nearest_gateway", "relay_decision",
           "best_progress", "escaped", "rank_toward"]
