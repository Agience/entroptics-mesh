"""
The core's plug points, neighbour state, announcements and reply-free routing.
"""
from __future__ import annotations

import math

import pytest

from entroptics_mesh import announce
from entroptics_mesh.neighbors import Announced, BandRange, Measured, NeighborTable, Position
from entroptics_mesh.providers import (AgilityProvider, InterferenceProvider, CryptoProvider, Intent,
                                       NullAgility, NullInterference, NullCrypto, Providers, TxPlan)
from entroptics_mesh.routing import BROADCAST, FloodRouter, Header

A, B, C, D, E = (bytes([i]) * 8 for i in range(1, 6))


# ── providers ────────────────────────────────────────────────────────────────────────────────

def test_the_null_providers_satisfy_their_interfaces():
    p = Providers()
    assert isinstance(p.crypto, CryptoProvider)
    assert isinstance(p.agility, AgilityProvider)
    assert isinstance(p.interference, InterferenceProvider)


def test_null_crypto_is_an_open_broadcast():
    """No sealing and no physical-layer key: the public zero state, readable by anyone."""
    c = NullCrypto()
    assert c.open(c.seal(b"payload", context=b"hdr"), context=b"hdr") == b"payload"
    assert c.waveform_key(b"anything") is None


def test_null_agility_and_interference_leave_the_plan_alone():
    plan = TxPlan(rate=1.2e6)
    assert NullAgility().plan(Intent(to=None, n_bytes=100), plan, NeighborTable()) == plan
    assert NullInterference().review(plan) == plan


# ── announcements ─────────────────────────────────────────────────────────────────────────────

def test_an_announcement_round_trips_and_carries_snr_not_decode_quality():
    data = announce.pack(A, position=Position(49.25, -123.1, 120.0), velocity=(30.0, 0.0, 1.5),
                         band=BandRange(0.52e6, 2.4e6, 915e6), heard={B: 18.4, C: -3.6})
    nid, a = announce.unpack(data, t=5.0)
    assert nid == A and a.t == 5.0
    assert a.position.lat == pytest.approx(49.25) and a.position.alt == pytest.approx(120.0)
    assert a.velocity == pytest.approx((30.0, 0.0, 1.5))
    assert a.band == BandRange(0.52e6, 2.4e6, 915e6)
    assert a.heard == {B: 18.0, C: -4.0}


def test_every_field_is_optional():
    nid, a = announce.unpack(announce.pack(A), t=0.0)
    assert nid == A and a.position is None and a.band is None and a.heard == {}


def test_a_malformed_announcement_is_refused_not_misread():
    data = announce.pack(A, heard={B: 1.0})
    for bad in (data[:-1], data + b"\x00", b"\x02" + data[1:]):
        with pytest.raises(ValueError):
            announce.unpack(bad, t=0.0)


def test_a_sealed_announcement_must_name_its_sender():
    c = NullCrypto()
    sealed = announce.seal(c, A, position=Position(0.0, 0.0))
    assert announce.open_sealed(c, A, sealed, t=1.0)[0] == A
    assert announce.open_sealed(c, B, sealed, t=1.0) is None


# ── neighbours ────────────────────────────────────────────────────────────────────────────────

def test_measured_scale_is_kept_per_sender_and_silent_neighbours_age_out():
    t = NeighborTable(max_age=10.0)
    t.measured(A, Measured(t=0.0, scale=0.514403, L=64, K=60, M=4, carrier=0.06, level_db=-30.0))
    t.announced(B, Announced(t=8.0, position=Position(0.0, 0.0)))
    assert t.known_scale(A) == pytest.approx(0.514403)
    assert t.known_scale(B) is None
    t.expire(now=12.0)
    assert t.get(A) is None and t.get(B) is not None


def test_distance_is_ground_and_altitude():
    p = Position(0.0, 0.0, 0.0)
    q = Position(0.0, 0.01, 100.0)                       # ~1112 m east, 100 m up
    assert p.distance_m(q) == pytest.approx(math.hypot(1111.95, 100.0), rel=1e-3)


# ── routing ───────────────────────────────────────────────────────────────────────────────────

def _flood(links, origin, to, hops):
    """Deliver one message over an undirected link set by flooding; returns who delivered it and
    how many transmissions it took."""
    routers = {n: FloodRouter(n) for n in {x for l in links for x in l}}
    nbr = {n: [b if a == n else a for a, b in links if n in (a, b)] for n in routers}
    first = routers[origin].originate(to, hops)
    queue, delivered, sent, now = [(origin, first)], [], 0, 0.0
    while queue:
        sender, h = queue.pop(0)
        sent += 1
        for n in nbr[sender]:
            now += 0.001
            deliver, relay = routers[n].heard(h, now)
            if deliver:
                delivered.append(n)
            if relay is not None:
                queue.append((n, relay))
    return delivered, sent


def test_a_message_crosses_a_chain_without_any_reply():
    delivered, sent = _flood([(A, B), (B, C), (C, D), (D, E)], A, E, hops=8)
    assert delivered == [E]
    assert sent == 4                                     # A, B, C, D each transmit once


def test_two_paths_deliver_once_and_relay_once():
    """A diamond: the message reaches D by two paths; D delivers once and relays once."""
    delivered, sent = _flood([(A, B), (A, C), (B, D), (C, D), (D, E)], A, E, hops=8)
    assert delivered == [E]
    assert sent == 4                                     # A, B, C, D -- E is the destination


def test_the_hop_budget_binds():
    delivered, _ = _flood([(A, B), (B, C), (C, D), (D, E)], A, E, hops=3)
    assert delivered == []


def test_a_broadcast_reaches_everyone_once():
    delivered, _ = _flood([(A, B), (A, C), (B, D), (C, D), (D, E)], A, BROADCAST, hops=8)
    assert sorted(delivered) == sorted([B, C, D, E])


def test_an_own_echo_is_never_relayed_and_the_seen_cache_expires():
    r = FloodRouter(A, seen_age=5.0)
    own = r.originate(BROADCAST, 4)
    assert r.heard(own, now=1.0) == (False, None)
    other = Header(B, 1, BROADCAST, 4)
    assert r.heard(other, now=1.0)[0] is True
    assert r.heard(other, now=2.0) == (False, None)      # duplicate suppressed
    assert r.heard(Header(B, 2, BROADCAST, 4), now=10.0)[0] is True
    assert other.msg_id not in r._seen                   # aged out behind newer traffic
