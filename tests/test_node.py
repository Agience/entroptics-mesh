"""The node runtime: N Nodes over the emulated air, each deciding everything from its own reads."""
from __future__ import annotations

import math

import numpy as np

from entroptics_mesh import phy, relay
from entroptics_mesh.adapt import Radio
from entroptics_mesh.emu.air import Air, Radio as AirRadio
from entroptics_mesh.emu.node_run import NodeRun
from entroptics_mesh.neighbors import Measured, Position
from entroptics_mesh.node import Node, Wire
from entroptics_mesh.providers import NullCrypto, Providers, replace
from entroptics_mesh.routing import HOME
from entroptics_mesh.tunnel import Tunnel

RX = 2.4e6
RADIO = Radio(min_rate=1.2e6, max_rate=RX, center_hz=915e6, max_power_dbm=20.0, noise_dbm=-100.0)
SUB = [phy.Known(64, 0.5, 0.25), phy.Known(64, 0.5, -0.25)]     # the two 1.2 Msps sub-bands
FULL = phy.Known(64, 1.0, 0.0)                                   # full width


class KeyOnly(NullCrypto):
    """No sealing, but a waveform key: the keyed aperture the fast read is measured on."""
    name = "key-only"

    def waveform_key(self, context):
        return 1234


def _pos(x, y=0.0):
    return Position(math.degrees(y / 6_371_000.0), math.degrees(x / 6_371_000.0), 100.0)


def _mesh(xy, seed=1, providers=None, **kw):
    air = Air(rate=RX, seed=seed)
    nodes = []
    for i, (x, y) in enumerate(xy):
        air.add(AirRadio(position=np.array([x, y, 100.0]), tx_power_dbm=20.0, noise_dbm=-100.0))
        p = (providers or {}).get(i) or Providers(crypto=KeyOnly())
        nodes.append(Node(bytes([65 + i]) * 8, RADIO, slot_s=0.0, position=_pos(x, y),
                          providers=p, seed=100 * seed + i, blind_scaled=False,
                          **{k: (v[i] if isinstance(v, dict) else v) for k, v in kw.items()}))
    return air, nodes


def _introduce(nodes):
    """Every node holds every other's announcement (position and band), as after one round of
    "I'm here" -- so the plans narrow to the hop and relays pick their own sub-band."""
    for a in nodes:
        for b in nodes:
            if a is not b:
                a.on_announcement(b.announcement(0.0), b.node_id, 0.0)


def _msg(n, seed=0):
    return bytes(np.random.default_rng(seed).integers(0, 256, n, dtype=np.uint8))


def test_two_nodes_announce_and_deliver_with_no_reply():
    air, (a, b) = _mesh([(0, 0), (9000, 0)], announce_every={0: 10.0, 1: None})
    b.learn(FULL)                                   # first contact already made (shout = full width)
    run = NodeRun(air, [a, b])
    msg = _msg(200)
    a.send(msg, b.node_id, 0.0)
    run.run(20, until=lambda: bool(b.inbox) and b.stats.announcements > 0)
    assert [m.payload for m in b.inbox] == [msg]
    assert b.table.get(a.node_id).announced.position == a.position     # heard over the air
    assert b.table.get(a.node_id).measured is not None                 # and measured
    assert a.stats.captures == 0                    # nothing came back: a never even listened to b


def test_three_node_chain_relays_on_its_own_sub_band():
    air, (a, b, c) = _mesh([(0, 0), (9000, 0), (18000, 0)], seed=2)
    _introduce([a, b, c])
    for n in (a, b, c):
        for g in SUB:
            n.learn(g)
    run = NodeRun(air, [a, b, c])
    msg = _msg(300, 1)
    mid = a.send(msg, c.node_id, 0.0)
    run.run(60, until=lambda: bool(c.inbox) and not b.queue)
    assert [m.payload for m in c.inbox] == [msg]
    sent = [p for _, i, p in run.log if i == 1]
    assert sent and all(p.rate == 1.2e6 for p in sent)                  # narrowed to the hop
    heard = b._heard_on[mid]
    assert all(abs(p.center - heard) >= 0.6e6 for p in sent)           # never the heard sub-band
    assert b.policy is relay.ADOPTED


def test_a_width_switch_mid_conversation_is_recovered_by_blind_acquisition():
    class Switch:
        """Sends on its sub-band until told, then full width."""
        name = "switch"
        wide = False

        def plan(self, intent, default, table, ctx=None):
            return replace(default, rate=RX, center=0.0) if self.wide else default

    sw = Switch()
    air, (a, b) = _mesh([(0, 0), (9000, 0)], seed=3,
                        providers={0: Providers(crypto=KeyOnly(), agility=sw)})
    _introduce([a, b])
    for g in SUB:
        b.learn(g)
    run = NodeRun(air, [a, b])
    first, second = _msg(150, 2), _msg(150, 3)
    a.send(first, b.node_id, run.now())
    run.run(30, until=lambda: len(b.inbox) == 1 and not a.queue)
    assert [m.payload for m in b.inbox] == [first] and b.stats.blind_tries == 0
    sw.wide = True
    t0 = run.now()
    a.send(second, b.node_id, t0)
    run.run(30, until=lambda: len(b.inbox) == 2)
    assert [m.payload for m in b.inbox] == [first, second]
    assert b.stats.blind_hits >= 1                                     # the fast path missed ...
    assert any(g.how == "blind" and g.known.scale == 1.0 for g in b.geoms)   # ... and learned it


def test_a_relay_lost_mid_message_leaves_its_sibling_to_finish():
    # origin -> two relays 2 km apart (they hear each other) -> far end
    air, nodes = _mesh([(0, 0), (9000, 1000), (9000, -1000), (18000, 0)], seed=4)
    a, r1, r2, c = nodes
    _introduce(nodes)
    for n in nodes:
        for g in SUB:
            n.learn(g)
    run = NodeRun(air, nodes)
    msg = _msg(900, 4)                                                 # k = 2
    a.send(msg, c.node_id, 0.0)
    run.run(80, until=lambda: r1.stats.bursts >= 1)
    run.dies[1] = run.slot                                             # r1 is gone, mid-forward
    run.run(120, until=lambda: bool(c.inbox))
    assert [m.payload for m in c.inbox] == [msg]
    assert r2.stats.bursts > 0


def test_home_anycast_reaches_one_of_two_gateways_once():
    wire = Wire()
    air, (a, g1, g2) = _mesh([(0, 0), (8000, 1500), (8000, -1500)], seed=5,
                             accepts={0: frozenset(), 1: {HOME}, 2: {HOME}},
                             backhaul={0: None, 1: wire, 2: wire})
    for g in (g1, g2):
        g.learn(FULL)
    run = NodeRun(air, [a, g1, g2])
    msg = _msg(200, 5)
    a.send(msg, HOME, 0.0, redundancy=2.0)
    run.run(30)
    assert [m.payload for m in g1.inbox + g2.inbox] == [msg]          # delivered once
    both = all(len(g.rx.done) == 1 for g in (g1, g2))
    assert both and wire.duplicates == 1             # the second gateway's copy died on the wire


def _udp(src, dst, payload):
    import struct
    body = struct.pack(">HHHH", 5000, 6000, 8 + len(payload), 0) + payload
    hdr = struct.pack(">BBHHHBBH4s4s", 0x45, 0, 20 + len(body), 0, 0, 64, 17, 0, bytes(src), bytes(dst))
    return hdr + body


def test_a_tunnel_datagram_crosses_the_mesh():
    air, (d, g) = _mesh([(0, 0), (9000, 0)], seed=6, accepts={0: frozenset(), 1: {HOME}})
    d.attach(Tunnel(d.node_id, crypto=KeyOnly()))
    g.attach(Tunnel(g.node_id, crypto=KeyOnly(), accepts={HOME}))
    g.learn(FULL)
    run = NodeRun(air, [d, g])
    pkt = _udp([10, 0, 0, 2], [192, 168, 1, 1], b"hello over the mesh")
    d.tunnel.send(pkt, 0.0, to=HOME)
    run.run(20, until=lambda: bool(g.ip_out))
    assert [x.payload for x in g.ip_out] == [pkt] and g.ip_out[0].origin == d.node_id


# ── what a decoded burst is heard at ─────────────────────────────────────────────────────────

def _true_snr(air, a, b, rate):
    """In-band SNR at b of a's burst at ``rate``: received power over the noise in its own band."""
    return air.snr_db(a, b) - 10 * math.log10(rate / RX)


def _at_snr(snr_db, rate):
    """Distance (m) at which a 20 dBm sender at ``rate`` is heard at ``snr_db`` in its own band."""
    loss = 20.0 + 100.0 - 10 * math.log10(rate / RX) - snr_db
    return 10 ** ((loss - 20 * math.log10(915e6) + 147.55) / 20)


def test_two_simultaneous_senders_on_two_sub_bands_are_each_heard_at_their_own_snr():
    """Two senders in one slot, one on each 1.2 Msps half of the band, 5 dB apart: each decoded
    burst is measured on its own rows and band, so each reads its own SNR (the whole capture's
    loudest part belongs to the louder one, and read that way the quieter one came out at the
    louder one's level -- 7-22 dB high)."""
    from entroptics_mesh.link import Symbol, frame
    from entroptics_mesh.providers import TxPlan
    from entroptics_mesh.routing import Header
    from entroptics_mesh.emu.node_run import slot_samples
    cases = [(18.0, 0.25, "coherent", 16), (13.0, -0.25, "differential", 4)]
    errs = []
    for seed in (1, 2):
        air = Air(rate=RX, seed=seed)
        air.add(AirRadio(position=np.array([0.0, 0.0, 100.0]), tx_power_dbm=20.0, noise_dbm=-100.0))
        me = Node(b"R" * 8, RADIO, slot_s=0.01, providers=Providers(crypto=KeyOnly()), blind_scaled=False)
        for g in SUB:
            me.learn(g)
        ids = []
        for j, (snr, cf, fam, M) in enumerate(cases):
            d = _at_snr(snr, 1.2e6)
            air.add(AirRadio(position=np.array([d * math.cos(2.0 * j), d * math.sin(2.0 * j), 100.0]),
                             tx_power_dbm=20.0, noise_dbm=-100.0))
            nid = bytes([65 + j]) * 8
            ids.append(nid)
            sym = Symbol(Header(nid, seed, b"Z" * 8, 3), 1, 400, 400, 5, _msg(400, 10 * seed + j))
            iq = phy.transmit_burst(frame(sym, "1/2"), TxPlan(rate=1.2e6, center=cf * RX, M=M, family=fam,
                                                              code_rate="1/2"), 1234)
            air.send(j + 1, 100 / RX, iq, rate=1.2e6, center=cf * RX)
        taken = me.on_capture(air.capture(0, 0.0, slot_samples(RX, 1.2e6)), 0.0)
        assert len(taken) == 2                                         # both decoded
        for j, nid in enumerate(ids):
            m = me.table.get(nid).measured
            errs.append(m.snr_db - _true_snr(air, j + 1, 0, 1.2e6))
    assert all(abs(e) <= 1.0 for e in errs), errs


def test_an_honest_dense_formation_is_not_distrusted():
    """A gateway and six drones in a 2 x 3 formation 3-4 km out, every position honest: each
    drone's telemetry burst is measured on its own (several in one slot), every SNR the gateway
    measures is within ~1 dB of the air's, and no honest claim is distrusted.  (Read off the whole
    capture, each frame took the slot's loudest burst's level and the gateway distrusted most of
    the formation before anything was spoofed.)"""
    gw = (0.0, -1500.0)
    form = [(3000.0 - dx, dy) for dx in (0.0, 400.0, 800.0) for dy in (-300.0, 300.0)]
    air, nodes = _mesh([gw] + form, seed=7, accepts={0: {HOME}, **{i: frozenset() for i in range(1, 7)}})
    _introduce(nodes)
    for n in nodes:
        n.learn(FULL)
        for g in SUB:
            n.learn(g)
    g1 = nodes[0]
    run = NodeRun(air, nodes)
    for i, d in enumerate(nodes[1:]):
        d.send(_msg(48, 20 + i), HOME, 0.0)
    run.run(40, until=lambda: len(g1.inbox) == 6 and not any(n.queue for n in nodes))
    heard = [(i, g1.table.get(d.node_id).measured) for i, d in enumerate(nodes[1:], 1)]
    heard = [(i, m) for i, m in heard if m is not None and m.snr_db is not None]
    assert len(heard) >= 4
    for i, m in heard:
        assert abs(m.snr_db - _true_snr(air, i, 0, m.scale * RX)) <= 1.5, (i, m.snr_db)
    assert not g1.distrusted
    assert not any(n.distrusted for n in nodes)


# ── no RF: the node's bookkeeping ────────────────────────────────────────────────────────────

def test_a_position_the_air_contradicts_is_distrusted_and_planned_around():
    _, (me, liar, far) = _mesh([(0, 0), (5000, 0), (60000, 0)])
    # I hear "liar" at 40 dB -- free space caps it well inside 5 km -- but it claims 200 km away.
    me.table.measured(liar.node_id, Measured(0.0, 1.0, 64, 60, 4, 0.0, -60.0, 40.0))
    liar.position = _pos(200_000)
    me.on_announcement(liar.announcement(0.0), liar.node_id, 0.0)
    assert liar.node_id in me.distrusted
    me.on_announcement(far.announcement(0.0), far.node_id, 0.0)
    assert far.node_id not in me.distrusted
    me.homes = {me.node_id}
    assert far.node_id in me.cut_off()                  # 60 km at 1 W: no predicted link
    plan = me.relay_plan(relay_alt=100.0)
    assert far.node_id in plan and liar.node_id not in plan


def test_an_old_measurement_does_not_contradict_a_claim_the_node_could_have_flown_to():
    """A node heard loud 40 s ago, near, now claims to be 5 km out: it could have flown there, so
    the old SNR says nothing against it.  The same SNR measured now does."""
    _, (me, other) = _mesh([(0, 0), (5000, 0)])
    other.position = _pos(5000)
    me.table.measured(other.node_id, Measured(0.0, 1.0, 64, 64, 4, 0.0, -60.0, 40.0))   # cap ~1 km
    me.on_announcement(other.announcement(40.0), other.node_id, 40.0)
    assert other.node_id not in me.distrusted
    me._record(other.node_id, Measured(40.0, 1.0, 64, 64, 4, 0.0, -60.0, 40.0))
    assert other.node_id in me.distrusted


def test_relay_regions_name_the_transmitter():
    _, (a, b) = _mesh([(0, 0), (1000, 0)])
    from entroptics_mesh.link import Symbol
    from entroptics_mesh.routing import Header
    b._know(a.node_id)
    h = Header(b"Z" * 8, 1, b.node_id, 3)
    assert b._transmitter(Symbol(h, 1, 8, 8, 5, bytes(8))) == b"Z" * 8          # an origin's own
    assert b._transmitter(Symbol(h, 1, 8, 8, relay.region_of(a.node_id) + 7, bytes(8))) == a.node_id


def test_memory_is_bounded_by_age_not_by_agreement():
    """A long flight must not grow a node's per-message state without bound: what was last active
    more than ``memory_age`` ago is forgotten on the next tick, and a fresh message is kept."""
    from entroptics_mesh import adapt, relay
    from entroptics_mesh.link import BLOCK
    from entroptics_mesh.node import Node
    from entroptics_mesh.routing import Header
    r = adapt.Radio(min_rate=0.3e6, max_rate=2.4e6, center_hz=0.0, max_power_dbm=20.0)
    n = Node(b"N" * 8, r, slot_s=0.005, memory_age=10.0)
    for seq in range(1, 4):
        relay.originate(n, Header(n.node_id, seq, b"X" * 8, 2), bytes(100), BLOCK, 1.0, now=float(seq))
    n.queue.clear()                                  # sent long ago
    relay.originate(n, Header(n.node_id, 9, b"X" * 8, 2), bytes(100), BLOCK, 1.0, now=30.0)
    n.queue.clear()
    n.tick(35.0)
    assert set(n.held) == {(n.node_id, 9)}
    assert {(k[0], k[1]) for k in n.seen} == {(n.node_id, 9)}


def test_a_claim_is_judged_again_when_the_air_later_contradicts_it():
    """Announced first, measured later: the measurement alone moves the verdict, both ways."""
    _, (me, other) = _mesh([(0, 0), (5000, 0)])
    other.position = _pos(200_000)
    me.on_announcement(other.announcement(0.0), other.node_id, 0.0)
    assert other.node_id not in me.distrusted            # nothing heard yet: nothing contradicts it
    me._record(other.node_id, Measured(1.0, 1.0, 64, 60, 4, 0.0, -60.0, 40.0))
    assert other.node_id in me.distrusted                 # heard loud: it cannot be 200 km away
    me._record(other.node_id, Measured(2.0, 1.0, 64, 60, 4, 0.0, -110.0, -20.0))
    assert other.node_id not in me.distrusted             # heard faint: the claim fits again


def test_steer_suppresses_a_backward_relay_but_still_delivers():
    # a -> b -> c (destination); d sits behind the source, a dead end toward c.  With steering on,
    # d makes no progress toward c and stays silent, while b (on the way) relays and c is delivered.
    air, nodes = _mesh([(0, 0), (9000, 0), (18000, 0), (-6000, 0)], seed=5, steer=True)
    a, b, c, d = nodes
    _introduce(nodes)
    for n in nodes:
        for g in SUB:
            n.learn(g)
    run = NodeRun(air, nodes)
    msg = _msg(300, 5)
    a.send(msg, c.node_id, 0.0)
    run.run(150)                                           # a fixed window: d has every chance to burst
    assert [m.payload for m in c.inbox] == [msg]          # delivered down the gradient
    assert b.stats.bursts > 0                              # b was on the way -> relayed
    assert d.stats.bursts == 0                             # d made no progress toward c -> silent


def test_flooding_lights_the_backward_relay_steer_removes():
    # the same geometry with steering OFF (the default): flooding lights d too -- the waste steering cuts.
    air, nodes = _mesh([(0, 0), (9000, 0), (18000, 0), (-6000, 0)], seed=5)
    a, b, c, d = nodes
    _introduce(nodes)
    for n in nodes:
        for g in SUB:
            n.learn(g)
    run = NodeRun(air, nodes)
    msg = _msg(300, 5)
    a.send(msg, c.node_id, 0.0)
    run.run(150)                                           # same window
    assert [m.payload for m in c.inbox] == [msg]          # still delivered
    assert d.stats.bursts > 0                              # flooding relays through the dead end


def test_steer_home_routes_toward_the_gateway_not_the_dead_end():
    # a -> b -> g (a gateway advertising HOME); d is a dead end away from the gateway.  HOME steering
    # resolves "any gateway" to the nearest advertised one; d makes no progress toward it and is silent.
    air, nodes = _mesh([(0, 0), (9000, 0), (18000, 0), (-6000, 0)], seed=7, steer=True,
                       accepts={0: frozenset(), 1: frozenset(), 2: {HOME}, 3: frozenset()},
                       backhaul={0: None, 1: None, 2: Wire(), 3: None})
    a, b, g, d = nodes
    _introduce(nodes)                                      # everyone learns g advertises gateway=True
    assert g.table is not None and a.table.get(g.node_id).announced.gateway is True
    for n in nodes:
        for gg in SUB:
            n.learn(gg)
    run = NodeRun(air, nodes)
    msg = _msg(200, 7)
    a.send(msg, HOME, 0.0, redundancy=2.0)
    run.run(150)
    assert [m.payload for m in g.inbox] == [msg]          # delivered to the gateway
    assert b.stats.bursts > 0                              # relayed on the way
    assert d.stats.bursts == 0                             # dead end stayed silent
