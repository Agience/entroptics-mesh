"""Tests for steer.py -- the gradient-follow (Reach-in-physical-space) forwarding overlay.

The module is pure arithmetic over positions, so these are fast and deterministic.  They pin the
decision regimes (flood / ignite / descend), the void-recovery helpers, the sender-side ranking, and
the spoof-defence behaviour (trusted-anchor resolution, distrusted exclusion)."""
from __future__ import annotations

import math

from entroptics_mesh import steer
from entroptics_mesh.neighbors import Position, Announced, NeighborTable

INF = steer.INF

# a little east/north helper in metres about a reference
_M_PER_DEG = 111_320.0


def _p(east_m, north_m, lat0=40.0, alt=0.0):
    lat = lat0 + north_m / _M_PER_DEG
    lon = east_m / (_M_PER_DEG * math.cos(math.radians(lat0)))
    return Position(lat, lon, alt)


TARGET = _p(10_000.0, 0.0)


# ---- relay_decision: the three regimes ------------------------------------------------------------

def test_flood_when_no_fix():
    assert steer.relay_decision(None, TARGET, INF).mode is steer.Mode.FLOOD
    assert steer.relay_decision(_p(0, 0), None, INF).mode is steer.Mode.FLOOD
    # FLOOD defers to the caller's own gate: relay is None, not a decision
    assert steer.relay_decision(_p(0, 0), None, INF).relay is None


def test_ignite_stamps_potential():
    me = _p(2_000.0, 0.0)
    d = steer.relay_decision(me, TARGET, INF)
    assert d.mode is steer.Mode.IGNITE
    assert d.relay is True
    assert math.isclose(d.d_here, me.distance_m(TARGET), rel_tol=1e-9)


def test_descend_relays_only_when_closer():
    # a node closer to the target than the sender descends -> relay
    closer = _p(5_000.0, 0.0)          # 5 km from target
    d = steer.relay_decision(closer, TARGET, d_prev=8_000.0)
    assert d.mode is steer.Mode.DESCEND and d.relay is True
    # a node farther than the sender does not descend -> no relay
    farther = _p(1_000.0, 0.0)         # 9 km from target
    assert steer.relay_decision(farther, TARGET, d_prev=8_000.0).relay is False


def test_margin_is_the_progress_floor():
    me = _p(5_000.0, 0.0)              # exactly 5 km from target
    # sender at 5 km + 30 m: progress is only 30 m, below a 50 m floor -> no relay
    assert steer.relay_decision(me, TARGET, d_prev=5_030.0, margin_m=50.0).relay is False
    # widen the gap past the floor -> relay
    assert steer.relay_decision(me, TARGET, d_prev=5_100.0, margin_m=50.0).relay is True


# ---- target_position: trusted-anchor resolution (spoof defence) -----------------------------------

def _table_with(node_id: bytes, pos: Position) -> NeighborTable:
    t = NeighborTable()
    t.announced(node_id, Announced(t=0.0, position=pos))
    return t


def test_target_prefers_trusted_table_fix_over_hint():
    tid = b"target00"
    tbl = _table_with(tid, TARGET)
    hint = _p(9_999.0, 500.0)          # a (different) carried hint
    got = steer.target_position(tid, tbl, hint=hint)
    assert got == TARGET               # authentic table fix wins over the hint


def test_distrusted_target_falls_back_to_hint():
    tid = b"target00"
    tbl = _table_with(tid, _p(9_000.0, 9_000.0))   # a claimed (untrusted) position
    hint = _p(10_000.0, 0.0)
    got = steer.target_position(tid, tbl, hint=hint, distrusted=frozenset({tid}))
    assert got == hint                 # distrusted table fix ignored, hint used


def test_no_fix_returns_none_for_flood():
    assert steer.target_position(b"unknown0", NeighborTable(), hint=None) is None


# ---- void recovery helpers ------------------------------------------------------------------------

def test_best_progress_and_local_minimum():
    me = _p(5_000.0, 0.0)                               # 5 km from target
    # neighbours all farther from the target than me -> local minimum (a void)
    voids = {b"a": _p(4_000.0, 0.0), b"b": _p(4_500.0, 1_000.0)}
    best = steer.best_progress(TARGET, voids)
    assert best >= me.distance_m(TARGET)               # nobody beats me -> stuck
    # a neighbour that does make progress lifts best below mine
    ok = {b"c": _p(7_000.0, 0.0)}
    assert steer.best_progress(TARGET, ok) < me.distance_m(TARGET)


def test_best_progress_excludes_distrusted():
    cands = {b"spoof": TARGET}                          # claims to be exactly at the target
    assert steer.best_progress(TARGET, cands, distrusted=frozenset({b"spoof"})) == INF


def test_escaped_resumes_descent_past_the_void():
    stuck = _p(5_000.0, 0.0).distance_m(TARGET)
    assert steer.escaped(_p(6_000.0, 0.0), TARGET, stuck, margin_m=50.0) is True
    assert steer.escaped(_p(4_000.0, 0.0), TARGET, stuck, margin_m=50.0) is False


# ---- rank_toward: the sender-side argmax / narrowing ----------------------------------------------

def test_rank_orders_by_progress_and_applies_width():
    cands = {b"near": _p(9_000.0, 0.0), b"mid": _p(6_000.0, 0.0), b"far": _p(2_000.0, 0.0)}
    ranked = steer.rank_toward(TARGET, cands, INF)
    assert [nid for nid, _ in ranked] == [b"near", b"mid", b"far"]   # nearest-to-target first
    assert steer.rank_toward(TARGET, cands, INF, width=1) == ranked[:1]


def test_rank_drops_distrusted_and_applies_floor():
    cands = {b"spoof": TARGET, b"honest": _p(6_000.0, 0.0)}
    ranked = steer.rank_toward(TARGET, cands, INF, distrusted=frozenset({b"spoof"}))
    assert [nid for nid, _ in ranked] == [b"honest"]
    # with a finite d_prev, a non-progressing candidate is below the floor and dropped
    back = {b"back": _p(1_000.0, 0.0)}                 # farther from target than d_prev
    assert steer.rank_toward(TARGET, back, d_prev=5_000.0, margin_m=50.0) == []


# ---- integration: the relay.take seam and the node-level gate -------------------------------------

from entroptics_mesh import relay, announce                           # noqa: E402
from entroptics_mesh.link import Reassembler, BLOCK                   # noqa: E402
from entroptics_mesh.routing import Header, BROADCAST, LOCATE         # noqa: E402
from entroptics_mesh.adapt import Radio                               # noqa: E402
from entroptics_mesh.node import Node, _SPECIAL                       # noqa: E402
from entroptics_mesh.providers import NullCrypto, Providers           # noqa: E402

_RADIO = Radio(min_rate=1.2e6, max_rate=2.4e6, center_hz=915e6, max_power_dbm=20.0, noise_dbm=-100.0)


class _KeyOnly(NullCrypto):
    name = "key-only"

    def waveform_key(self, context):
        return 1234


class _Holder:
    """The minimal relay-holder protocol relay.take needs."""
    def __init__(self, nid):
        self.node_id = nid
        self.queue, self.seen, self.held = [], set(), {}
        self.rx = Reassembler()
        self.accepts = frozenset()


def test_relay_may_relay_gates_only_the_forward():
    src, hdr = _Holder(b"S" * 8), Header(b"S" * 8, 1, b"D" * 8, hops=5)
    syms = relay.originate(src, hdr, b"the quick brown fox " * 3, BLOCK, 2.0, 0.0)

    def forwards(may):
        h = _Holder(b"M" * 8)
        return any(relay.take(h, s, 0.0, relay.ADOPTED, relay.region_of(h.node_id), may_relay=may).forwarding
                   for s in syms)

    assert forwards(False) is False      # steering says no progress -> onward relay suppressed
    assert forwards(None) is True        # flooding default -> forwards
    assert forwards(True) is True        # steering says progress -> forwards


def _node(pos, **kw):
    return Node(b"N" * 8, _RADIO, slot_s=0.0, position=pos,
                providers=Providers(crypto=_KeyOnly()), seed=1, **kw)


def test_node_steer_gate_descends_defers_and_resists_spoof():
    T, S = b"target00", b"sender00"
    u = _node(_p(7_000.0, 0.0), steer=True)                    # 3 km from the target
    u.table.announced(T, Announced(t=0.0, position=TARGET))    # target 10 km east
    u.table.announced(S, Announced(t=0.0, position=_p(5_000.0, 0.0)))  # sender 5 km from target
    hd = Header(b"origin00", 1, T, hops=5)

    assert u._steer_relay(hd, S) is True                       # 3 km < 5 km -> descend, relay
    u.position = _p(3_000.0, 0.0)                              # now 7 km from target, behind the sender
    assert u._steer_relay(hd, S) is False                      # no progress -> do not relay

    # special / unknown destinations defer to flooding
    assert u._steer_relay(Header(b"o", 1, BROADCAST, 5), S) is None
    assert u._steer_relay(Header(b"o", 1, b"unknown0", 5), S) is None

    # spoof defence: a distrusted destination claim is ignored -> defer to flooding, never toward it
    u.position = _p(7_000.0, 0.0)
    u.distrusted.add(T)
    assert u._steer_relay(hd, S) is None


# ---- Q2: location gossip (LOCATE) spreads position past the one-hop announcement -------------------

def test_locate_floods_while_announce_terminates_in_one_hop():
    def run(to, hops, accepts):
        src = _Holder(b"O" * 8)
        syms = relay.originate(src, Header(b"O" * 8, 1, to, hops=hops), b"payload " * 4, BLOCK, 2.0, 0.0)
        h = _Holder(b"M" * 8)
        h.accepts = frozenset(accepts)
        delivered = forwarded = False
        for s in syms:
            t = relay.take(h, s, 0.0, relay.ADOPTED, relay.region_of(h.node_id))
            delivered = delivered or (t.delivered is not None)
            forwarded = forwarded or t.forwarding
        return delivered, forwarded

    # LOCATE: delivered to this node AND forwarded onward -> it floods (bounded by the hop budget)
    assert run(LOCATE, 3, accepts=()) == (True, True)
    # ANNOUNCE (anycast): delivered, never forwarded -> stays one hop
    assert run(announce.ADDRESS, 3, accepts={announce.ADDRESS}) == (True, False)


def test_node_ingests_a_located_position_and_can_then_steer_to_it():
    far = _node(_p(0.0, 0.0), steer=True)                      # this node at the origin
    other = b"farther0"
    sealed = announce.seal(far.crypto, other, position=_p(20_000.0, 0.0))   # other's sealed announcement
    # before: far has never been other's neighbour, so it has no fix and would flood
    assert far.table.get(other) is None
    far._deliver(Header(other, 1, LOCATE, hops=2), relay.Taken(delivered=sealed), 0.0)
    nb = far.table.get(other)
    assert nb is not None and nb.announced is not None and nb.announced.position is not None
    # now far holds a fix on a target many hops away -> it steers (a real decision, not flood/None)
    far.table.announced(b"sender00", Announced(t=0.0, position=_p(10_000.0, 0.0)))
    assert far._steer_relay(Header(b"origin00", 1, other, hops=5), b"sender00") is not None


def test_steer_recovery_floods_a_void_but_suppresses_a_dead_end():
    T, S = b"target00", b"sender00"
    u = _node(_p(5_000.0, 0.0), steer=True, steer_recover=True)     # 5 km from the target (10 km east)
    u.table.announced(T, Announced(t=0.0, position=TARGET))
    u.table.announced(S, Announced(t=0.0, position=_p(6_000.0, 0.0)))   # sender 4 km from target
    hd = Header(b"origin00", 1, T, hops=5)
    # u (5 km) is not closer than the sender (4 km) -> it would suppress.
    # DEAD END: u knows a relay closer to the target than the sender -> progress possible -> suppress.
    u.table.announced(b"closer00", Announced(t=0.0, position=_p(8_000.0, 0.0)))   # 2 km from target
    assert u._steer_relay(hd, S) is False
    # VOID: nothing closer than the sender is known -> flood to escape.
    u.table = NeighborTable()
    u.table.announced(T, Announced(t=0.0, position=TARGET))
    u.table.announced(S, Announced(t=0.0, position=_p(6_000.0, 0.0)))
    assert u._steer_relay(hd, S) is None
    # recovery OFF (default) -> the same void just suppresses, flooding behaviour unchanged
    u.steer_recover = False
    assert u._steer_relay(hd, S) is False


def test_steer_width_narrows_to_best_and_resists_spoofed_rank():
    # candidate relays for a hop = the nodes the SENDER announced HEARING (the measured neighbour set);
    # geometry/range is irrelevant to the ranking, only progress toward the target.
    def pa(x):
        return _p(x, 0.0, 40.0, 100.0)
    T, S = b"target00", b"sender00"
    tgt = pa(20_000.0)
    u = _node(pa(1_500.0), steer=True, steer_width=1)              # 18.5 km from target
    u.table.announced(T, Announced(t=0.0, position=tgt))
    u.table.announced(S, Announced(t=0.0, position=pa(0.0),         # sender heard A and B (+ u)
                                   heard={b"siblingA": -60.0, b"siblingB": -60.0}))
    u.table.announced(b"siblingA", Announced(t=0.0, position=pa(1_800.0)))   # nearer target (better)
    u.table.announced(b"siblingB", Announced(t=0.0, position=pa(1_000.0)))   # farther (worse)
    hd = Header(b"origin00", 1, T, hops=5)
    assert u._steer_relay(hd, S) is False                          # width 1 -> only siblingA relays
    u.steer_width = 2
    assert u._steer_relay(hd, S) is True                           # width 2 -> u is 2nd best, relays

    # spoof resistance: u is the genuine best; a spoofer claims an even better spot to bump it out
    u.steer_width = 1
    u.table = NeighborTable()
    u.table.announced(T, Announced(t=0.0, position=tgt))
    u.table.announced(S, Announced(t=0.0, position=pa(0.0), heard={b"spoofer0": -60.0}))
    u.position = pa(1_800.0)                                        # u genuinely the best
    u.table.announced(b"spoofer0", Announced(t=0.0, position=pa(1_950.0)))   # claims a better spot
    assert u._steer_relay(hd, S) is False                          # spoofer counted -> u bumped out
    u.distrusted.add(b"spoofer0")
    assert u._steer_relay(hd, S) is True                           # spoofer rejected -> u is best, relays


def test_anchor_self_check_fails_safe_under_coherent_spoof():
    from entroptics_mesh.placement import predicted_snr_db
    from entroptics_mesh.neighbors import Measured

    def pa(x):
        return _p(x, 0.0, 40.0, 100.0)
    A, T, S = b"anchor00", b"target00", b"sender00"
    u = _node(pa(8_000.0), steer=True, anchors={A: pa(0.0)})       # BELIEVES it is 8 km from the anchor
    snr2km = predicted_snr_db(u.radio, 2_000.0, u.rx_rate)         # but the air says ~2 km (true range)
    u.table.measured(A, Measured(0.0, 1.0, 64, 60, 4, 0.0, 0.0, snr2km))
    u.table.announced(T, Announced(t=0.0, position=pa(20_000.0)))
    u.table.announced(S, Announced(t=0.0, position=pa(0.0)))
    hd = Header(b"origin00", 1, T, hops=5)
    assert u._own_fix_trusted() is False                          # believed 8 km >> SNR-implied cap
    assert u._steer_relay(hd, S) is None                          # fail safe -> flood, never mis-steer
    # without anchors, nothing contradicts the spoofed fix -> it steers by it
    u.anchors = {}
    assert u._own_fix_trusted() is True
    assert u._steer_relay(hd, S) is not None
    # an honest fix (believed == true) passes the anchor check
    u.anchors = {A: pa(0.0)}
    u.position = pa(2_000.0)
    assert u._own_fix_trusted() is True


def test_tick_emits_a_bounded_locate_beacon_when_enabled():
    n = _node(_p(0.0, 0.0), locate_hops=3, announce_every=1.0)
    n._next_announce = 0.0
    n.tick(10.0)
    beacons = [q.header for q in n.queue if q.header.to == LOCATE]
    assert beacons and all(h.hops == 3 for h in beacons)          # LOCATE beacon, bounded to 3 hops
    assert any(q.header.to == announce.ADDRESS and q.header.hops == 1 for q in n.queue)  # announce stays 1-hop

    # disabled by default: no beacon, behaviour unchanged
    m = _node(_p(0.0, 0.0), announce_every=1.0)                   # locate_hops defaults to 0
    m._next_announce = 0.0
    m.tick(10.0)
    assert not any(q.header.to == LOCATE for q in m.queue)
