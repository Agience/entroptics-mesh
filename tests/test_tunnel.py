"""IP over the mesh: addressing, packing, fragmentation, deadlines, and the optional reliable class
under loss, reordering, duplication and node loss -- on whole messages, no RF."""
from __future__ import annotations

import hashlib
import hmac
import ipaddress
import random

import pytest

from entroptics_mesh.emu.ipnet import IpNet
from entroptics_mesh.routing import HOME
from entroptics_mesh.tunnel import (ACK, CTRL, FRAG, PKT, AddressMap, Backhaul, Record, Tunnel,
                                    TunnelConfig, pack_message, pack_routes, unpack_message,
                                    unpack_routes)

A, B, G1, G2 = b"drone-A.", b"drone-B.", b"gate-1..", b"gate-2.."


def ip4(src: str, dst: str, n: int, rng=random.Random(0), proto: int = 17) -> bytes:
    h = bytearray(20)
    h[0], h[9] = 0x45, proto
    h[2:4] = (28 + n).to_bytes(2, "big")
    h[12:16] = ipaddress.IPv4Address(src).packed
    h[16:20] = ipaddress.IPv4Address(dst).packed
    return bytes(h) + bytes(8) + bytes(rng.getrandbits(8) for _ in range(n))


def ip6(src: str, dst: str, n: int) -> bytes:
    h = bytearray(40)
    h[0], h[6] = 0x60, 17
    h[8:24] = ipaddress.IPv6Address(src).packed
    h[24:40] = ipaddress.IPv6Address(dst).packed
    return bytes(h) + bytes(n)


def pair(**net):
    n = IpNet(**net)
    a = n.add(Tunnel(A, rng=random.Random(1)))
    b = n.add(Tunnel(B, rng=random.Random(2)))
    return n, a, b


def payloads(net, t):
    return [d.payload for d in net.inbox[t.node_id]]


# ── wire ──

def test_records_round_trip_and_a_lone_packet_costs_two_bytes():
    recs = [Record(ACK, 3, b"\x05", seq=65535),
            Record(PKT, 7, b"x" * 300, reliable=True, ordered=True, seq=2, low=65534),
            Record(FRAG, 1, b"y" * 10, frag=(9, 1, 3), newest=True, seq=40),
            Record(CTRL, 1, pack_routes([("10.0.0.0/8", None)])),
            Record(PKT, 0, b"z" * 64, urgent=True)]
    assert unpack_message(pack_message(recs)) == recs
    assert len(pack_message([Record(PKT, 0, b"z" * 64)])) == 1 + 2 + 64


def test_routes_body_round_trips_v4_and_v6():
    e = [(ipaddress.ip_network("0.0.0.0/0"), HOME), (ipaddress.ip_network("10.1.2.0/23"), A),
         (ipaddress.ip_network("fd00:1::/48"), B)]
    assert unpack_routes(pack_routes(e), A) == e
    assert unpack_routes(pack_routes([("10.9.0.0/16", None)]), G1)[0][1] == G1


# ── addressing ──

def test_longest_prefix_wins_and_announced_routes_age_out():
    m = AddressMap(max_age=10, glean_age=5)
    m.configure("0.0.0.0/0", HOME)
    m.learn(A, [("10.1.0.0/16", A)], now=0)
    m.learn(B, [("10.1.2.0/24", B)], now=0)
    assert m.lookup("10.1.2.3", 1) == B and m.lookup("10.1.9.9", 1) == A
    assert m.lookup("8.8.8.8", 1) == HOME
    m.glean(ipaddress.ip_address("10.1.2.3"), A, now=2)
    assert m.lookup("10.1.2.3", 3) == A                      # /32 beats /24
    m.learn(B, [], now=4)                                    # B re-announces without the /24
    assert m.lookup("10.1.2.4", 4) == A
    m.expire(20)
    assert m.lookup("10.1.2.3", 20) == HOME and len(m) == 1  # only the configured route is left
    m.configure("fd00::/8", G1)
    assert m.lookup("fd00:1::5", 20) == G1 and m.lookup("2001:db8::1", 20) is None


def test_a_drone_knows_no_route_until_a_gateway_announces_one():
    net = IpNet(seed=1)
    a = net.add(Tunnel(A, rng=random.Random(1)))
    g = net.add(Tunnel(G1, accepts={HOME}, rng=random.Random(2)))
    p = ip4("10.1.0.5", "8.8.8.8", 100)
    assert not a.send(p, 0.0) and a.stats.no_route == 1
    g.announce_routes([("0.0.0.0/0", HOME)], 0.0)
    a.announce_routes([("10.1.0.0/24", None)], 0.0)
    net.run(0.2)
    assert a.send(p, net.now)
    net.run(0.2)
    assert payloads(net, g) == [p]
    assert g.routes.lookup("10.1.0.77", net.now) == A        # and the gateway learned the way back


def test_a_gateway_gleans_the_way_back_from_traffic():
    net = IpNet(seed=1)
    a = net.add(Tunnel(A, rng=random.Random(1)))
    g = net.add(Tunnel(G1, accepts={HOME}, glean=True, rng=random.Random(2)))
    a.routes.configure("0.0.0.0/0", HOME)
    a.send(ip4("10.7.7.7", "1.1.1.1", 50), 0.0)
    net.run(0.2)
    assert g.routes.lookup("10.7.7.7", net.now) == A


def test_ipv6_packets_route_by_prefix():
    net, a, b = pair(seed=0)
    a.routes.configure("fd00:b::/64", B)
    p = ip6("fd00:a::1", "fd00:b::2", 300)
    assert a.send(p, 0.0)
    net.run(0.2)
    assert payloads(net, b) == [p]


# ── anycast and the backhaul ──

def test_anycast_home_delivers_each_packet_once_through_whichever_gateway_hears():
    net = IpNet(loss=0.3, seed=4)
    a = net.add(Tunnel(A, rng=random.Random(1)))
    bh = Backhaul()
    g1 = net.add(Tunnel(G1, accepts={HOME}, backhaul=bh, rng=random.Random(2)))
    g2 = net.add(Tunnel(G2, accepts={HOME}, backhaul=bh, rng=random.Random(3)))
    sent = [ip4("10.1.0.5", "8.8.8.8", 400, random.Random(i)) for i in range(60)]
    for i, p in enumerate(sent):
        net.run(0.05)
        a.send(p, net.now, to=HOME)
    net.run(0.5)
    got = payloads(net, g1) + payloads(net, g2)
    assert len(got) == len(set(got))                         # no duplicates upstream
    assert len(got) >= 52                                    # 1 - 0.3^2 = 91% expected
    assert bh.duplicates > 0                                 # both gateways did hear some


def test_reliable_flow_to_home_survives_its_anchor_gateway_dying():
    net = IpNet(loss=0.1, jitter=0.01, seed=2)
    a = net.add(Tunnel(A, rng=random.Random(1)))
    bh = Backhaul()
    g1 = net.add(Tunnel(G1, accepts={HOME}, backhaul=bh, rng=random.Random(2)))
    g2 = net.add(Tunnel(G2, accepts={HOME}, backhaul=bh, rng=random.Random(3)))
    sent = [ip4("10.1.0.5", "8.8.8.8", 300, random.Random(i)) for i in range(200)]
    for i, p in enumerate(sent):
        a.send(p, net.now, to=HOME, reliable=True, flow=5)
        net.run(0.01)
        if i == 80:
            net.kill(bh.anchor[A])
    net.run(5, until=lambda: not a.sending[5].unacked)
    got = set(payloads(net, g1) + payloads(net, g2))
    assert got == set(sent) and not a.sending[5].unacked


# ── packing ──

def test_small_packets_queued_behind_a_transmission_share_one_message():
    t = Tunnel(A, rng=random.Random(1))
    one = ip4("10.0.0.1", "10.0.0.2", 36)                     # 64-byte packet
    t.send(one, 0.0, to=B)
    out = t.pull(0.0)                                         # radio idle: goes at once, alone
    assert out.records == 1 and out.blocks() == 1
    for _ in range(20):
        t.send(one, 0.01, to=B)
    out = t.pull(0.05)
    assert out.records == 20 and out.blocks() == 3          # 20 frames' worth in 3
    assert t.pull(0.05) is None


def test_hold_lets_a_lone_small_packet_wait_for_company():
    t = Tunnel(A, config=TunnelConfig(hold=0.05), rng=random.Random(1))
    t.send(ip4("10.0.0.1", "10.0.0.2", 36), 0.0, to=B)
    assert t.pull(0.01) is None
    assert t.pull(0.06).records == 1


def test_different_destinations_are_never_mixed():
    t = Tunnel(A, rng=random.Random(1))
    t.send(b"\x45" + bytes(63), 0.0, to=B)
    t.send(b"\x45" + bytes(63), 0.0, to=G1)
    assert {t.pull(0.0).header.to, t.pull(0.0).header.to} == {B, G1}


# ── size limits ──

def test_a_large_packet_is_cut_across_messages_and_rebuilt_in_any_order():
    net, a, b = pair(jitter=0.05, seed=3)
    p = ip4("10.0.0.1", "10.0.0.2", 8000)
    assert a.send(p, 0.0, to=B)
    net.run(0.5)
    assert payloads(net, b) == [p] and a.stats.messages >= 3


def test_a_packet_larger_than_the_mtu_is_refused_not_fragmented():
    t = Tunnel(A, config=TunnelConfig(mtu=1500), rng=random.Random(1))
    assert not t.send(ip4("10.0.0.1", "10.0.0.2", 1600), 0.0, to=B) and t.stats.too_big == 1


def test_a_best_effort_packet_missing_a_fragment_is_dropped_and_forgotten():
    a, b = Tunnel(A, rng=random.Random(1)), Tunnel(B, config=TunnelConfig(frag_timeout=1.0))
    a.send(ip4("10.0.0.1", "10.0.0.2", 5000), 0.0, to=B)
    outs = [o for o in iter(lambda: a.pull(0.0), None)]
    got = [d for o in outs[1:] for d in b.receive(o.header, o.sealed, 0.1)]
    assert got == [] and len(b._frags) == 1
    b.poll(2.0)
    assert not b._frags


def test_a_reliable_fragmented_packet_survives_loss():
    net, a, b = pair(loss=0.3, seed=5)
    p = ip4("10.0.0.1", "10.0.0.2", 6000)
    a.send(p, 0.0, to=B, reliable=True, ordered=True)
    net.run(5, until=lambda: payloads(net, b))
    assert payloads(net, b) == [p]


# ── best-effort deadlines ──

def test_stale_best_effort_packets_are_dropped_before_they_go():
    t = Tunnel(A, rng=random.Random(1))
    t.send(b"\x45" + bytes(63), 0.0, to=B, deadline=0.1)
    assert t.pull(0.2) is None and t.stats.expired == 1


def test_a_newest_flow_drops_what_arrives_after_something_newer():
    a, b = Tunnel(A, rng=random.Random(1)), Tunnel(B)
    outs = []
    for i in range(3):
        a.send(bytes([0x45, i]) + bytes(62), 0.0, to=B, flow=9, newest=True)
        outs.append(a.pull(0.0))
    got = [d.payload[1] for o in (outs[0], outs[2], outs[1]) for d in b.receive(o.header, o.sealed, 0.1)]
    assert got == [0, 2] and b.stats.stale == 1


# ── the reliable class ──

@pytest.mark.parametrize("loss", [0.0, 0.2, 0.4])
def test_reliable_flow_delivers_everything_once_and_in_order_under_loss_reorder_and_duplication(loss):
    net, a, b = pair(loss=loss, dup=0.1, jitter=0.03, seed=7)
    sent = [ip4("10.0.0.1", "10.0.0.2", 200, random.Random(i)) for i in range(150)]
    for p in sent:
        a.send(p, 0.0, to=B, reliable=True, ordered=True, flow=3)
    net.run(10, until=lambda: not a.sending[3].unacked)
    assert payloads(net, b) == sent
    assert a.stats.abandoned == 0


def test_unordered_reliable_flow_delivers_each_once():
    net, a, b = pair(loss=0.3, dup=0.2, jitter=0.05, seed=8)
    sent = [ip4("10.0.0.1", "10.0.0.2", 100, random.Random(i)) for i in range(100)]
    for p in sent:
        a.send(p, 0.0, to=B, reliable=True, flow=4)
    net.run(10, until=lambda: not a.sending[4].unacked)
    got = payloads(net, b)
    assert sorted(got) == sorted(sent)


def test_receiver_stops_waiting_for_what_the_sender_gave_up():
    """Message 1 never arrives; once the sender's lifetime for it passes, its low-water mark moves
    and the ordered receiver releases what it held behind the hole."""
    cfg = TunnelConfig(lifetime=0.5, max_blocks=1)
    a, b = Tunnel(A, config=cfg, rng=random.Random(1)), Tunnel(B, config=cfg)
    pk = [bytes([0x45, i]) + bytes(400) for i in range(3)]
    for p in pk:
        a.send(p, 0.0, to=B, reliable=True, ordered=True, flow=1)
    first = a.pull(0.0)                                       # lost
    got = [d for o in (a.pull(0.0), a.pull(0.0)) for d in b.receive(o.header, o.sealed, 0.01)]
    assert got == []                                          # held behind the hole
    for o in (b.pull(0.05),):                                 # the NACK arrives
        a.receive(o.header, o.sealed, 0.06)
    t = 0.06
    while t < 0.7:                                            # retransmissions are lost too
        a.pull(t)
        t += 0.05
    a.send(bytes([0x45, 9]) + bytes(10), t, to=B, reliable=True, ordered=True, flow=1)
    o = a.pull(t)
    got = [d.payload[1] for d in b.receive(o.header, o.sealed, t)]
    assert got == [1, 2, 9] and a.stats.abandoned == 1 and first is not None


def test_a_rebooted_sender_is_a_new_epoch_not_a_run_of_duplicates():
    net, a, b = pair(seed=9)
    for i in range(5):
        a.send(bytes([0x45, i]) + bytes(50), 0.0, to=B, reliable=True, flow=2)
    net.run(0.5)
    a2 = Tunnel(A, rng=random.Random(99))                     # same node, state gone
    net.nodes[0] = a2
    for i in range(5):
        a2.send(bytes([0x45, 10 + i]) + bytes(50), net.now, to=B, reliable=True, flow=2)
    net.run(1.0, until=lambda: not a2.sending[2].unacked)
    assert [p[1] for p in payloads(net, b)] == [0, 1, 2, 3, 4, 10, 11, 12, 13, 14]


def test_acks_piggyback_on_reverse_traffic():
    net, a, b = pair(seed=10)
    for i in range(40):
        a.send(bytes([0x45]) + bytes(100), net.now, to=B, reliable=True, flow=1)
        b.send(bytes([0x45]) + bytes(100), net.now, to=A)
        net.run(0.02)
    net.run(0.3)
    assert b.stats.acks >= 10 and b.stats.ack_only < b.stats.acks / 2


def test_idle_flow_state_is_dropped_at_both_ends():
    net, a, b = pair(seed=11)
    a.cfg.idle = b.cfg.idle = 1.0
    a.send(bytes([0x45]) + bytes(100), 0.0, to=B, reliable=True, flow=1)
    net.run(0.3)
    assert a.sending and b.receiving
    net.run(1.5)
    assert not a.sending and not b.receiving


def test_a_dead_receiver_costs_bounded_state_and_time():
    net, a, b = pair(seed=12)
    a.cfg.lifetime = 1.0
    net.kill(b)
    for i in range(10):
        a.send(bytes([0x45]) + bytes(100), 0.0, to=B, reliable=True, flow=1)
    net.run(1.5)
    assert a.stats.abandoned == 10 and not a.sending[1].unacked
    assert a.stats.retransmits <= 40                          # probes, not the window every RTO


# ── crypto stays a provider ──

class _ToyMac:
    """Test-only provider: appends an HMAC over context + plaintext (not encryption)."""
    name = "toy"

    def seal(self, plaintext, *, context):
        return plaintext + hmac.new(b"k", context + plaintext, hashlib.sha256).digest()[:8]

    def open(self, sealed, *, context):
        p, tag = sealed[:-8], sealed[-8:]
        ok = hmac.compare_digest(tag, hmac.new(b"k", context + p, hashlib.sha256).digest()[:8])
        return p if ok else None

    def waveform_key(self, context):
        return None


def test_payloads_are_sealed_by_the_provider_and_bound_to_the_mesh_header():
    from entroptics_mesh.routing import Header
    a = Tunnel(A, crypto=_ToyMac(), rng=random.Random(1))
    b = Tunnel(B, crypto=_ToyMac())
    p = ip4("10.0.0.1", "10.0.0.2", 64)
    a.send(p, 0.0, to=B)
    o = a.pull(0.0)
    moved = Header(o.header.origin, o.header.seq + 1, o.header.to, o.header.hops)
    assert b.receive(moved, o.sealed, 0.1) == [] and b.stats.bad == 1
    assert [d.payload for d in b.receive(o.header, o.sealed, 0.1)] == [p]


def test_best_effort_messages_carry_their_deadline_as_the_mesh_time_budget():
    """Relays can only drop stale video if the deadline travels in the clear mesh header."""
    t = Tunnel(A, rng=random.Random(5))
    t.send(ip4("10.0.0.1", "10.9.0.1", 100), 10.0, to=G1, deadline=10.25, urgent=True)
    out = t.pull(10.0)
    assert out.header.budget_ms == 250
    t.send(ip4("10.0.0.1", "10.9.0.1", 100), 20.0, to=G1, reliable=True, urgent=True)
    assert t.pull(20.0).header.budget_ms == 0                     # reliable: its own give-up rules
