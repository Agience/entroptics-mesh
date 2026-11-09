"""The access layer: the level gate's floor in a busy band, listen before talk, interleaving, reading
a burst wherever it starts in a capture, and the capture model the access variants are ranked on."""
from __future__ import annotations

import math

import numpy as np

from entroptics_mesh import link, phy
from entroptics_mesh.adapt import Radio
from entroptics_mesh.emu.air import Air, Radio as AirRadio, _at_rx
from entroptics_mesh.emu.link_model import ModelRun, read_probability
from entroptics_mesh.emu.node_run import NodeRun
from entroptics_mesh.link import Symbol, frame
from entroptics_mesh.neighbors import Position
from entroptics_mesh.node import Node
from entroptics_mesh.providers import NullCrypto, Providers, TxPlan
from entroptics_mesh.routing import HOME, Header

RX = 2.4e6
RADIO = Radio(min_rate=1.2e6, max_rate=RX, center_hz=915e6, max_power_dbm=20.0, noise_dbm=-100.0)
FULL = phy.Known(64, 1.0, 0.0)
SUB = [phy.Known(64, 0.5, 0.25), phy.Known(64, 0.5, -0.25)]
N = 19216                                   # a slot's capture with the contention window


class KeyOnly(NullCrypto):
    name = "key-only"

    def waveform_key(self, context):
        return 1234


def _pos(x, y=0.0):
    return Position(math.degrees(y / 6_371_000.0), math.degrees(x / 6_371_000.0), 100.0)


def _node(i=0, x=0.0, y=0.0, **kw):
    return Node(bytes([65 + i]) * 8, RADIO, slot_s=0.008, position=_pos(x, y),
                providers=Providers(crypto=KeyOnly()), seed=i, blind_scaled=False, **kw)


def _noise(rng, n=N):
    return (rng.standard_normal(n) + 1j * rng.standard_normal(n)) / math.sqrt(2)


def _burst(sender: Node, to: bytes, plan: TxPlan, seq=1):
    sym = Symbol(Header(sender.node_id, seq, to, 4), 1, link.BLOCK, 10, 0, bytes(link.BLOCK))
    return phy.transmit_burst(frame(sym, plan.code_rate), plan, 1234, sender=sender.node_id)


def _place(iq, plan: TxPlan, start: int, snr_db: float, n=N):
    """``iq`` as the air delivers it into an n-sample capture from ``start``, at ``snr_db`` in its band."""
    if plan.rate < RX:
        seg = _at_rx(iq, plan.rate / RX, n, -start * plan.rate / RX)
    else:
        seg = np.concatenate([np.zeros(start), iq, np.zeros(n)])[:n]
    seg = seg * np.exp(2j * np.pi * plan.center * np.arange(n) / RX)
    return 10 ** (snr_db / 20) * math.sqrt(plan.rate / RX) * seg


# ── the level gate's floor ───────────────────────────────────────────────────────────────────

def test_the_floor_reads_the_noise_alone():
    rng = np.random.default_rng(0)
    nd = _node()
    for _ in range(40):
        nd.on_capture(_noise(rng), 0.0)
    assert abs(nd.floor_db) < 0.6                           # noise is unit power: 0 dB


def test_a_busy_band_does_not_lift_the_floor_to_its_loudest_neighbour():
    # A drone in a formation: most captures hold a neighbour's burst that fills the slot at 30 dB.
    # One capture in the window with a quiet stretch is enough to hold the floor at the noise, and
    # a 12 dB burst from someone else is then read, not gated (the median floor gated it).
    rng = np.random.default_rng(1)
    nd = _node()
    nd.on_capture(_noise(rng), 0.0)
    for _ in range(20):
        loud = _noise(rng)
        loud[150:] += 10 ** (30 / 20) * _noise(rng, N - 150)
        nd.on_capture(loud, 0.0)
    assert nd.floor_db < 1.0
    level, floor, present = nd._level(_noise(rng) + np.r_[np.zeros(200), 10 ** (12 / 20) * _noise(rng, N - 200)])
    assert present and level - floor > 10


# ── listen before talk ───────────────────────────────────────────────────────────────────────

def test_a_listen_hears_a_burst_as_busy_and_noise_as_clear():
    rng = np.random.default_rng(2)
    nd = _node()
    for _ in range(4):
        nd.on_capture(_noise(rng), 0.0)
    m = int(round(nd.listen_s * RX))
    assert m == Node.LISTEN
    assert not nd.busy(_noise(rng, m))
    assert nd.busy(_noise(rng, m) + 10 ** (5 / 20) * _noise(rng, m))     # a 5 dB sender
    assert not _node(1).busy(_noise(rng, m) * 100)                     # no floor yet: send


def test_a_busy_listen_defers_and_a_clear_one_sends():
    rng = np.random.default_rng(3)
    a = _node(0)
    for _ in range(4):
        a.on_capture(_noise(rng), 0.0)
    a.send(b"x" * 40, HOME, 0.0)
    s = a.start(0.0)
    assert s is not None and 0.0 <= s < a.contention_s
    m = int(round(a.listen_s * RX))
    assert a.commit(0.0, _noise(rng, m) + 10 * _noise(rng, m)) is None
    assert a.stats.deferred == 1 and a.stats.bursts == 0
    assert a.due > 0.0 and a.start(0.0) is None                       # not due again this slot
    t = a.due
    assert a.start(t) is not None
    iq, plan = a.commit(t, _noise(rng, m))
    assert a.stats.bursts == 1 and iq.size > 0


def test_starts_are_drawn_at_random_not_on_a_grid():
    a = _node(0)
    a.send(b"x" * 40, HOME, 0.0)
    s = [a.start(0.0) for _ in range(200)]
    assert len(set(np.round(s, 9))) == 200
    assert 0.0 <= min(s) and max(s) < a.contention_s
    assert np.std(s) > 0.25 * a.contention_s                           # spread over the window


def test_backoff_doubles_only_when_asked_to():
    rng = np.random.default_rng(4)
    for cap, want in ((1, 1), (8, 8)):
        a = _node(0, backoff_max=cap)
        a.send(b"x" * 40, HOME, 0.0)
        for _ in range(5):
            a.defer(0.0)
        assert a._backoff == want


def test_two_senders_that_hear_each_other_take_turns_and_both_arrive():
    # A and B 1 km apart want G (9 km) in the same slot.  Listening, the later start hears the
    # earlier one and defers; without listening they collide on the full band.
    def run(listen):
        air = Air(rate=RX, seed=5)
        nodes = []
        for i, (x, y) in enumerate([(0, 0), (0, 1000), (9000, 500)]):
            air.add(AirRadio(position=np.array([x, y, 100.0]), tx_power_dbm=20.0, noise_dbm=-100.0))
            nodes.append(_node(i, x, y, accepts={HOME} if i == 2 else frozenset(), hops=1))
        for i, nd in enumerate(nodes):
            nd.learn(FULL)
            nd.on_capture(air.capture(i, -1.0, N), 0.0)   # the radios were on before: a floor each
        r = NodeRun(air, nodes, listen=listen)
        a, b, g = nodes
        a.send(b"from a" * 8, HOME, 0.0, redundancy=1.0)
        b.send(b"from b" * 8, HOME, 0.0, redundancy=1.0)
        r.run(12)
        return sorted(m.payload[:6] for m in g.inbox), r
    got, r = run(True)
    assert got == [b"from a", b"from b"]
    slots = [s for s, i, _ in r.log]
    assert len(slots) == len(set(slots)) == 2                          # never in the same slot
    assert sum(nd.stats.deferred for nd in r.nodes) >= 1
    got, _ = run(False)
    assert got == []                                                   # both lost to the collision


# ── reading a burst wherever it starts ───────────────────────────────────────────────────────

def test_a_burst_late_in_its_capture_is_read():
    rng = np.random.default_rng(6)
    a, b = _node(0), _node(1, 9000)
    for g in [FULL] + SUB:
        b.learn(g)
    for _ in range(3):
        b.on_capture(_noise(rng), 0.0)
    for k, (plan, start, snr) in enumerate([
            (TxPlan(rate=RX, center=0.0, power_db=0, M=2, family="differential"), 2100, 6.0),
            (TxPlan(rate=1.2e6, center=-6e5, power_db=0, M=2, family="differential"), 1800, 5.0)]):
        y = _place(_burst(a, b.node_id, plan, seq=k + 1), plan, start, snr) + _noise(rng)
        got = b.on_capture(y, 0.0)
        assert got and got[0].header.seq == k + 1
        assert b._onset() > 1000


# ── the capture model ────────────────────────────────────────────────────────────────────────

def test_the_capture_model_matches_its_calibration():
    # measured on the recorded showcase: 0.01 / 0.58 / 0.91 / 0.97-1.00 read at margins
    # [-6,-3) / [-3,0) / [0,3) / >= 3 dB
    assert read_probability(-4.5) < 0.03
    assert 0.4 < read_probability(-1.5) < 0.6
    assert 0.8 < read_probability(1.5) < 0.98
    assert read_probability(4.5) > 0.99


def test_listening_in_the_model_delivers_a_dense_cluster_that_collides_without_it():
    def run(listen, seed=1):
        rng = np.random.default_rng(seed)
        air = Air(rate=RX, seed=seed)
        pts = [(0.0, 0.0, 30.0)] + [(6000 + 300 * rng.standard_normal(), 300 * rng.standard_normal(), 120.0)
                                    for _ in range(8)]
        nodes = []
        for i, p in enumerate(pts):
            air.add(AirRadio(position=np.array(p), tx_power_dbm=20.0, noise_dbm=-100.0))
            nodes.append(_node(i, p[0], p[1], accepts={HOME} if i == 0 else frozenset(), hops=4))
        for a in nodes:
            for b in nodes:
                if a is not b:
                    a.on_announcement(b.announcement(0.0), b.node_id, 0.0)
        r = ModelRun(air, nodes, [list(range(len(nodes)))], listen=listen, seed=seed)
        orders = [(i, "send", bytes([i]) * 48) for i in range(1, len(nodes))]
        r.step(0.0, orders)
        for s in range(1, 120):
            r.step(s * r.slot_s)
        got = {m.payload[:1] for m in nodes[0].inbox}
        return len(got), r.bursts
    heard, bursts = run(True)
    assert heard == 8
    heard0, bursts0 = run(False)
    assert heard0 < 8 and bursts < bursts0


def test_interleaving_puts_a_new_message_before_the_redundancy_of_an_old_one():
    a = _node(0)
    a.send(b"old" * 10, HOME, 0.0, redundancy=3.0)
    a.next_symbol(0.0)                                                 # old: one of three sent
    a.send(b"new" * 10, HOME, 0.0, redundancy=3.0)
    a.due = 0.0
    sym, _ = a.next_symbol(0.0)
    assert sym.header.seq == 2                                         # the new one's first symbol
    plain = _node(1, interleave=False)
    plain.send(b"old" * 10, HOME, 0.0, redundancy=3.0)
    plain.next_symbol(0.0)
    plain.send(b"new" * 10, HOME, 0.0, redundancy=3.0)
    plain.due = 0.0
    assert plain.next_symbol(0.0)[0].header.seq == 1                   # FIFO: the old one's second
