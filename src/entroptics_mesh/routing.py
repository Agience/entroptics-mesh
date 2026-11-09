"""
routing.py -- reply-free delivery: controlled flooding with duplicate suppression.

The mesh's DELIVERY MODEL is reply-free -- not its traffic.  Getting any one message through rests
on nothing coming back: no route requests, replies or acknowledgements, because a fast fleet flying
one direction has no guaranteed return path for a given message.  Traffic is still two-way: any node
reaches any node or gateway (a reply is just another message routed the same way), and above this
layer the tunnel's reliable class and the TCP proxy carry UDP/TCP in both directions to the anycast
HOME.  The first router is the one that needs nothing back: every node that hears a message for which
it is not the only destination relays it once, until its hop budget runs out.  Duplicates are suppressed by
message id, so a message crosses each node at most once however many paths deliver it -- and the
many paths are the redundancy: a fleet flying in one direction loses links continually, and a
message that went three ways survives two of them failing.

What this deliberately leaves for later: choosing *which* relays forward (position- and
SNR-aware, from the link map the announcements build), and least-cost routing.  Flooding first,
because it is correct with no knowledge at all; the smarter routers are measured against it.

Message id: (origin node id, origin's sequence number).  The seen-cache is bounded by age, not by
agreement.
"""
from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass

BROADCAST = b"\xff" * 8
HOME = b"HOME\x00\x00\x00\x00"   # anycast: every gateway accepts it, so "home" is whichever one hears
LOCATE = b"LOCATE\x00\x00"       # location gossip: delivered to every node AND flooded (bounded hops),
                                 # so position knowledge spreads past the one-hop announcement


@dataclass(frozen=True)
class Header:
    """What routing reads from every message.  ``to`` is a node id or BROADCAST; ``hops`` is the
    remaining hop budget; ``budget_ms`` the remaining time budget (0: none).

    The time budget needs no shared clock: each node subtracts only how long it held the message,
    measured on its own clock (:func:`spend`), so a relay can drop what can no longer arrive in
    time instead of spending airtime on it."""
    origin: bytes
    seq: int
    to: bytes
    hops: int
    budget_ms: int = 0

    @property
    def msg_id(self) -> tuple[bytes, int]:
        return self.origin, self.seq


def spend(h: Header, held_ms: float) -> Header | None:
    """The header after a node held its message ``held_ms`` (own clock, a duration): the budget
    less that, or None when the budget is spent.  A header with no budget passes unchanged."""
    if not h.budget_ms:
        return h
    left = int(h.budget_ms - max(float(held_ms), 0.0))
    return Header(h.origin, h.seq, h.to, h.hops, left) if left > 0 else None


class FloodRouter:
    """Decides, for each message a node hears, whether to deliver it locally and whether to relay it.

    ``seen_age`` bounds how long a message id is remembered (seconds); it must exceed the time a
    message can take to cross the mesh, or a late copy would be relayed again.
    """

    def __init__(self, node_id: bytes, *, seen_age: float = 60.0):
        self.node_id = bytes(node_id)
        self.seen_age = float(seen_age)
        self._seen: OrderedDict[tuple[bytes, int], float] = OrderedDict()
        self._seq = 0

    def originate(self, to: bytes, hops: int) -> Header:
        """A header for a new message from this node.  Its echoes are recognized by origin, so they
        need no entry in the seen-cache (an entry that never expired would block the cache's
        age-ordered expiry behind it)."""
        self._seq += 1
        return Header(self.node_id, self._seq, bytes(to), int(hops))

    def _expire(self, now: float) -> None:
        while self._seen:
            k, t = next(iter(self._seen.items()))
            if now - t <= self.seen_age:
                break
            self._seen.popitem(last=False)

    def heard(self, h: Header, now: float) -> tuple[bool, Header | None]:
        """``(deliver, relay)``: deliver locally?  and the header to relay with (None = don't)."""
        self._expire(now)
        if h.origin == self.node_id or h.msg_id in self._seen:
            return False, None
        self._seen[h.msg_id] = float(now)
        self._seen.move_to_end(h.msg_id)
        for_me = h.to == self.node_id or h.to == BROADCAST or h.to == LOCATE
        only_me = h.to == self.node_id
        relay = None if only_me or h.hops <= 1 else Header(h.origin, h.seq, h.to, h.hops - 1)
        return for_me, relay


__all__ = ["BROADCAST", "HOME", "LOCATE", "Header", "FloodRouter"]
