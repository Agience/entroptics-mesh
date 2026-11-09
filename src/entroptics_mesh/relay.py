"""
relay.py -- the relaying rules every node runs, in one place.

These are the rules the emulation measured (``emu.subband_run``) and the node runtime
(``node.Node``) executes; both call this module, so the two cannot drift.

    decode-and-forward   a relay listens until it has rebuilt a message, then sends fresh fountain
                         symbols of its own (from its own region of symbol ids), ``fresh`` x k of
                         them.  Fresh symbols carry more downstream than repeats of caught ones.
    passive ack          an origin that overhears a symbol of its own message it never sent knows a
                         relay rebuilt it, and drops what it still queues for it -- zero airtime.
    sibling share        a relay that overhears another relay forwarding the same message at the
                         same hop count cancels one queued symbol per fresh symbol heard; on
                         hearing a later hop it cancels all of them.  No handshake: a dead sibling
                         is simply not heard, so the survivors send in full.  Measured: with a
                         budget of x3, 40/40 delivered under relay loss against 28/40 for a fixed
                         x1.5, at equal airtime with nothing lost (the cancellations pay for it).
    hop budget           a relay forwards only while ``hops > 1`` and decrements it.
    time budget          each node subtracts how long it held a message (its own clock, a duration;
                         ``routing.spend``) and drops a message whose budget is spent.
    anycast              a node takes delivery for every address in its ``accepts`` (HOME on every
                         gateway); duplicates among gateways are the backhaul's business.

Nothing here schedules transmissions or touches the radio: callers decide *when* (slots, clocks)
and *how* (plans).  A holder is any object with ``node_id``, ``queue`` (list of
:class:`~entroptics_mesh.link.Symbol`), ``seen`` (set of (origin, seq, symbol id)), ``rx``
(:class:`~entroptics_mesh.link.Reassembler`), ``held`` (msg id -> time the node took the message
on) and ``accepts`` (extra addresses it takes delivery for).  Times are in the caller's own unit.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

from . import fec
from .link import Symbol, symbols, _seed as link_seed
from .routing import BROADCAST, LOCATE, Header, spend


@dataclass(frozen=True)
class Policy:
    """The relay policy.  ``ADOPTED`` is the measured one: sibling share at a x2 budget.

    Budget x2 (research/scenarios/node_loss_listen*): with listen-before-talk on,
    x2 delivers exactly as x3 in every node-loss arm (real IQ 60/60, k=1 and k=6) at ~20% fewer
    relay bursts and ~20% lower showcase latency.  The only place a thin budget mattered was a LONE
    relay (no sibling) carrying a multi-block message: 2k fountain symbols leaves the small-k dense
    code short of full rank ~1-2% of the time (single_alive x2 9/10 at k=6).  ``rank_margin`` fixes
    exactly that -- a relay of a k>=2 message sends at least ``k + rank_margin`` symbols regardless
    of ``fresh`` -- so x2 stays cheap on k=1 telemetry (2 symbols) and safe on multi-block messages."""
    fresh: float = 2.0               # fresh symbols a relay sends per block once it rebuilt a message
    rank_margin: int = 12            # a k>=2 relay sends >= k + this, so a lone relay reaches full rank
    passive_ack: bool = True
    sibling_ack: bool = True


ADOPTED = Policy()

REGION = 1 << 20                     # symbol ids per relay region; an origin's own ids sit in region 0


def region_of(node_id: bytes) -> int:
    """The first symbol id of a node's own region, derived from its id (nothing to agree; two
    nodes share a region with probability 1/4095, which costs efficiency, never correctness)."""
    return (1 + fec.crc32(bytes(node_id)) % 4095) * REGION


@dataclass
class Taken:
    """What one decoded symbol did to a holder."""
    new: bool = False                # not seen before (everything below is False/0/None otherwise)
    decoded: bool = False            # a symbol of someone else's message
    acked: bool = False              # passive ack: queued symbols of my own message were dropped
    cancelled: int = 0               # sibling share: queued relay symbols cancelled
    idle: bool = False               # the queue ran empty by acking/cancelling: unschedule
    delivered: bytes | None = None   # a message for this holder completed
    anycast: bool = False            # ... and it was addressed to one of ``accepts``
    forwarding: bool = False         # fresh relay symbols were queued


def originate(holder, header: Header, message: bytes, block: int, redundancy: float, now) -> list:
    """Queue a new message of this holder's: ``ceil(k * redundancy)`` fountain symbols."""
    out = symbols(header, message, block, redundancy)
    holder.held[header.msg_id] = now
    for s in out:
        holder.queue.append(s)
        holder.seen.add((header.origin, header.seq, s.symbol_id))
    return out


def overheard(holder, hd: Header) -> tuple[int, bool]:
    """Sibling share.  Another relay is forwarding a message this holder also forwards: one queued
    symbol off per symbol heard at the same hop count, all of them for a later hop.  Returns
    (symbols cancelled, queue ran empty)."""
    mine = [q for q in holder.queue if q.header.msg_id == hd.msg_id]
    if not mine:
        return 0, False
    if hd.hops < mine[0].header.hops:            # a later hop rebuilt it: my part is done
        drop = {id(q) for q in mine}
    elif hd.hops == mine[0].header.hops:         # a sibling relay: one of its symbols for one of mine
        drop = {id(mine[-1])}
    else:
        return 0, False
    holder.queue = [q for q in holder.queue if id(q) not in drop]
    return len(drop), not holder.queue


def take(holder, sym: Symbol, now, policy: Policy, start_id: int,
         may_relay: bool | None = None) -> Taken:
    """One decoded symbol: dedupe, passive ack, sibling share, reassembly, delivery and
    decode-and-forward.  ``start_id`` is the first id of this holder's fresh-symbol region.

    ``may_relay`` is the position-aware steering gate (:mod:`steer`), computed by the caller from its
    neighbour table: ``None`` leaves flooding unchanged (the default and the baseline), ``False``
    suppresses the decode-and-forward for this message (the gradient says this node makes no progress
    toward the destination), ``True`` is the same as flooding.  Delivery, dedup, passive-ack and
    sibling-share are never gated -- only the onward relay is."""
    t = Taken()
    k = (sym.header.origin, sym.header.seq, sym.symbol_id)
    if k in holder.seen:
        return t
    t.new = True
    if policy.passive_ack and sym.header.origin == holder.node_id:
        # A symbol of my own message I never sent: a relay rebuilt it and is forwarding fresh
        # symbols.  That is the acknowledgement, and it cost no airtime.
        before = len(holder.queue)
        holder.queue = [q for q in holder.queue if q.header.msg_id != sym.header.msg_id]
        t.acked = before > len(holder.queue)
        t.idle = not holder.queue
        holder.seen.add(k)
        return t
    holder.seen.add(k)
    t.decoded = True
    hd = sym.header
    if policy.sibling_ack and hd.origin != holder.node_id:
        t.cancelled, t.idle = overheard(holder, hd)
    anycast = hd.to in holder.accepts
    mine = hd.to == holder.node_id or hd.to == BROADCAST or hd.to == LOCATE or anycast
    relays = (hd.to != holder.node_id and not anycast and hd.hops > 1
              and hd.origin != holder.node_id and may_relay is not False)
    msg = holder.rx.add(sym) if (mine or relays) else None
    if msg is not None and mine:
        t.delivered, t.anycast = msg, anycast
    if relays and msg is not None:
        rh = Header(hd.origin, hd.seq, hd.to, hd.hops - 1, hd.budget_ms)
        holder.held[hd.msg_id] = now
        src = fec.fountain_encode(msg, sym.block, link_seed(hd.origin, hd.seq), start_id=start_id)
        n_fresh = int(math.ceil(sym.k * policy.fresh))
        if sym.k >= 2:                               # a lone relay must still reach full fountain rank
            n_fresh = max(n_fresh, sym.k + policy.rank_margin)
        for _ in range(n_fresh):
            sid, data = next(src)
            holder.queue.append(Symbol(rh, sym.k, sym.block, sym.length, sid, data))
        t.forwarding = True
    return t


def pop(holder, now, unit_ms: float, tx_units: float = 1.0) -> tuple[Symbol | None, int]:
    """The next symbol to transmit with its time budget spent, or (None, n) when that message's
    budget ran out -- its ``n`` queued symbols are dropped.  The holding time is ``now`` less when
    the holder took the message on, plus ``tx_units`` for the transmission itself, in units of
    ``unit_ms`` milliseconds (a duration on the holder's own clock; nothing shared)."""
    sym = holder.queue.pop(0)
    h2 = spend(sym.header, (now - holder.held.get(sym.header.msg_id, now) + tx_units) * unit_ms)
    if h2 is None:
        gone = [q for q in holder.queue if q.header.msg_id == sym.header.msg_id]
        holder.queue = [q for q in holder.queue if q.header.msg_id != sym.header.msg_id]
        return None, 1 + len(gone)
    return Symbol(h2, sym.k, sym.block, sym.length, sym.symbol_id, sym.data), 0


__all__ = ["ADOPTED", "Policy", "REGION", "Taken", "originate", "overheard", "pop", "region_of", "take"]
