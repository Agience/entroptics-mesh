"""Split TCP: each end terminates TCP locally; the byte stream crosses the mesh on the tunnel's
reliable ordered class.  Driven as pure state machines over the lossy message net -- no sockets."""
from __future__ import annotations

import ipaddress
import random

import pytest

from entroptics_mesh.emu.ipnet import IpNet
from entroptics_mesh.routing import HOME
from entroptics_mesh.tcpproxy import (DATA, FIN, OPEN, RST, Close, Connect, Reset, Segment, TcpProxy,
                                      Write)
from entroptics_mesh.tunnel import Backhaul, Tunnel

D, G1, G2 = b"drone-A.", b"gate-1..", b"gate-2.."
SERVER = ipaddress.ip_address("93.184.216.34")


def world(loss=0.0, seed=0, gateways=1, **kw):
    net = IpNet(loss=loss, jitter=0.02, dup=0.05, seed=seed)
    bh = Backhaul()
    d = net.add(Tunnel(D, rng=random.Random(1)))
    d.routes.configure("0.0.0.0/0", HOME)
    gws = [net.add(Tunnel(g, accepts={HOME}, backhaul=bh, rng=random.Random(2 + i)))
           for i, g in enumerate((G1, G2)[:gateways])]
    proxies = {t.node_id: TcpProxy(t, **kw) for t in [d, *gws]}
    return net, bh, proxies


def pump(net, proxies, seconds, acts=None, until=None):
    """Run the net, feed deliveries to each node's proxy, collect socket actions per node."""
    acts = acts if acts is not None else {k: [] for k in proxies}
    end = net.now + seconds
    while net.now < end:
        net.step(0.002)
        for nid, p in proxies.items():
            for d in net.inbox[nid]:
                acts[nid] += p.on_delivery(d, net.now)
            net.inbox[nid].clear()
            acts[nid] += p.poll(net.now)
        if until is not None and until(acts):
            break
    return acts


def stream(acts, key):
    return b"".join(a.data for a in acts if isinstance(a, Write) and a.key == key)


def test_segments_round_trip():
    for s in (Segment(OPEN, 300, True, b"GET /", SERVER, 80),
              Segment(OPEN, 1, False, b"", ipaddress.ip_address("fd00::1"), 22),
              Segment(DATA, 5, False, b"x" * 100), Segment(FIN, 5, True), Segment(RST, 0, False)):
        assert Segment.unpack(s.pack()) == s


@pytest.mark.parametrize("loss", [0.0, 0.2])
def test_a_proxied_connection_carries_both_streams_in_order_and_closes_cleanly(loss):
    net, _, px = world(loss=loss, seed=3)
    dp, gp = px[D], px[G1]
    rng = random.Random(5)
    up = bytes(rng.getrandbits(8) for _ in range(20_000))
    down = bytes(rng.getrandbits(8) for _ in range(60_000))
    key = dp.local_open(SERVER, 80, 0.0, data=up[:500])       # request rides the open
    dp.local_data(key, up[500:], 0.0)
    dp.local_fin(key, 0.0)
    acts = pump(net, px, 10, until=lambda a: any(isinstance(x, Close) for x in a[G1]))
    gkey = (D, key[1])
    assert Connect(gkey, SERVER, 80) in acts[G1]
    assert stream(acts[G1], gkey) == up
    gp.local_data(gkey, down, net.now)                        # the server answers and closes
    gp.local_fin(gkey, net.now)
    acts = pump(net, px, 15, acts, until=lambda a: any(isinstance(x, Close) for x in a[D]))
    assert stream(acts[D], key) == down
    assert Close(key) in acts[D]
    assert not dp.conns and not gp.conns                      # both FINs crossed: no state left
    assert not any(isinstance(x, Reset) for v in acts.values() for x in v)


def test_a_refused_connect_resets_the_drone_side():
    net, _, px = world(seed=4)
    key = px[D].local_open(SERVER, 25, 0.0)
    acts = pump(net, px, 1, until=lambda a: a[G1])
    gkey = (D, key[1])
    px[G1].local_reset(gkey, net.now)                         # the gateway's connect failed
    acts = pump(net, px, 2, acts, until=lambda a: a[D])
    assert Reset(key) in acts[D] and not px[D].conns


def test_a_stream_the_far_end_does_not_hold_is_reset():
    """Anycast failover: the drone's stream reaches a gateway that never saw its OPEN."""
    net, bh, px = world(seed=5, gateways=2)
    key = px[D].local_open(SERVER, 443, 0.0, data=b"hello")
    flow = px[D].tunnel.sending[key[1]]
    acts = pump(net, px, 2, until=lambda a: (a[G1] or a[G2]) and not flow.unacked)
    anchor = bh.anchor[D]                                     # it holds the far end, and acked it
    net.kill(anchor)
    del px[anchor.node_id]
    px[D].local_data(key, b"more", net.now)
    acts = pump(net, px, 8, acts, until=lambda a: Reset(key) in a[D])
    assert Reset(key) in acts[D] and not px[D].conns


def test_an_open_not_yet_acknowledged_fails_over_whole():
    """If the anchor dies before acknowledging the OPEN, the drone's retransmission reaches the
    next gateway with the OPEN still in it, and the connection simply opens there."""
    net, bh, px = world(seed=5, gateways=2)
    key = px[D].local_open(SERVER, 443, 0.0, data=b"hello")
    acts = pump(net, px, 1, until=lambda a: a[G1] or a[G2])
    anchor = bh.anchor[D]
    other = G2 if anchor.node_id == G1 else G1
    net.kill(anchor)
    del px[anchor.node_id]
    px[D].local_data(key, b" more", net.now)
    acts = pump(net, px, 8, acts, until=lambda a: stream(a[other], (D, key[1])) == b"hello more")
    assert Connect((D, key[1]), SERVER, 443) in acts[other]
    assert stream(acts[other], (D, key[1])) == b"hello more" and Reset(key) not in acts[D]


def test_a_silent_connection_is_reset_after_idle():
    net, _, px = world(seed=6, idle=2.0)
    key = px[D].local_open(SERVER, 80, 0.0, data=b"x")
    acts = pump(net, px, 3)
    assert Reset(key) in acts[D] and Reset((D, key[1])) in acts[G1]
    assert not px[D].conns and not px[G1].conns


def test_a_stream_the_tunnel_gives_up_on_is_reset():
    net, _, px = world(seed=7)
    px[D].tunnel.cfg.lifetime = 1.0
    net.kill(px[G1].tunnel)
    key = px[D].local_open(SERVER, 80, 0.0, data=b"x" * 3000)
    acts = pump(net, {D: px[D]}, 2)
    assert Reset(key) in acts[D] and not px[D].conns


def test_backpressure_says_stop_reading_while_the_window_is_full():
    net, _, px = world(seed=8, window=4)
    key = px[D].local_open(SERVER, 80, 0.0)
    assert px[D].writable(key)
    px[D].local_data(key, bytes(1200 * 6), 0.0)
    assert not px[D].writable(key)
    pump(net, px, 1)
    assert px[D].writable(key)


def test_a_gateway_may_open_toward_a_drone():
    net, _, px = world(seed=9)
    px[G1].tunnel.routes.configure("10.1.0.0/24", D)
    key = px[G1].local_open("10.1.0.5", 22, 0.0, data=b"SSH-2.0")
    acts = pump(net, px, 1, until=lambda a: a[D])
    assert Connect((G1, key[1]), ipaddress.ip_address("10.1.0.5"), 22) in acts[D]
    assert stream(acts[D], (G1, key[1])) == b"SSH-2.0"
