"""The integrator-facing agility-provider contract (the plug point and the keyed schedule).

An integrator writes an AgilityProvider against promises the core makes, documented in research/PROVIDERS.md
and the providers.py docstrings.  These tests lock those promises so they cannot silently drift:

  * ``plan(intent, default, table, ctx)`` receives the sender's OWN state as a ``PlanContext``
    (me / velocity / t / node_id) plus the node's neighbour table (clues, never constraints);
  * a provider that steers the plan is honoured (the node transmits on the provider's choice);
  * the OPTIONAL keyed transmit schedule (``transmit_offset`` / ``predict_offset``) is
    self-consistent -- a sender and a keyed receiver derive ONE start, deterministically, across
    fresh instances (so separate nodes agree without exchanging anything), keyed (noise-like without
    the key), and guarded (None when there is no id / no schedule, i.e. degrade to random access).

They also cover the node.plan_for ctx branch flagged uncovered in research/coverage.md.
"""
from __future__ import annotations

import pytest

from entroptics_mesh.adapt import Radio
from entroptics_mesh.neighbors import Position
from entroptics_mesh.node import Node
from entroptics_mesh.providers import (BandObservation, CryptoProvider, Intent, NullAgility,
                                       NullInterference, NullCrypto, PlanContext, Providers, TxPlan,
                                       replace)
from entroptics_mesh.schedules import KeyedSchedule

RADIO = Radio(min_rate=1.2e6, max_rate=2.4e6, center_hz=915e6, max_power_dbm=20.0, noise_dbm=-100.0)
ME = Position(49.0, -123.0, 120.0)
VEL = (30.0, 0.0, 1.5)


class Recorder(NullAgility):
    """Records every plan() call, and optionally steers the plan the node transmits on."""
    name = "recorder"

    def __init__(self, steer=None):
        self.calls = []
        self.steer = steer

    def plan(self, intent, default, table, ctx=None):
        self.calls.append((intent, default, table, ctx))
        return self.steer(default) if self.steer is not None else default


def _node(provider, **kw):
    return Node(b"NODE0000", RADIO, slot_s=0.01, providers=Providers(agility=provider),
                position=ME, velocity=VEL, **kw)


# ── the plan() plug point ────────────────────────────────────────────────────────────────────

def test_plan_for_hands_the_provider_the_documented_seeds():
    rec = Recorder()
    n = _node(rec)
    header = n.router.originate(b"DESTeeee", n.hops)
    n.plan_for(header, now=12.5)
    assert rec.calls, "the node must consult the agility provider"
    intent, default, table, ctx = rec.calls[-1]
    assert isinstance(intent, Intent) and intent.to == b"DESTeeee"
    assert isinstance(default, TxPlan)                 # the core's audience-fitted default
    assert table is n.table                            # the neighbour table, handed over as clues
    assert isinstance(ctx, PlanContext)                # the sender's own state, the 7c seed material
    assert ctx.me == ME
    assert ctx.velocity == VEL
    assert ctx.node_id == n.node_id
    assert ctx.t == 12.5


def test_a_steering_provider_is_honoured():
    # a provider that moves only the centre is passed through unchanged (refit sheds nothing when
    # width/power are untouched), so the node transmits on the provider's centre.
    rec = Recorder(steer=lambda p: replace(p, center=0.3e6))
    n = _node(rec)
    header = n.router.originate(b"DESTeeee", n.hops)
    plan = n.plan_for(header, now=0.0)
    assert plan is not None and plan.center == 0.3e6


def test_null_agility_runs_no_schedule():
    # the default provider leaves the plan alone and schedules nothing -> today's random access.
    a = NullAgility()
    base = TxPlan(rate=2.4e6)
    assert a.plan(Intent(to=None, n_bytes=100), base, None) is base
    assert a.transmit_offset(PlanContext(node_id=b"NODE0000", t=1.0), window_s=0.02) is None
    assert a.predict_offset(b"NODE0000", 1.0, window_s=0.02) is None


# ── the optional keyed transmit schedule (7c) ─────────────────────────────────────────────────

def test_keyed_schedule_sender_and_receiver_derive_one_start():
    w = 0.02
    sched = KeyedSchedule(b"flight-key", epoch_s=0.01)
    ctx = PlanContext(node_id=b"NODE0000", t=5.0)
    tx = sched.transmit_offset(ctx, window_s=w)
    pred = sched.predict_offset(b"NODE0000", 5.0, window_s=w)
    assert tx == pred                                  # the sender's start is exactly what the receiver predicts
    assert 0.0 <= tx < w                               # inside the contention window


def test_keyed_schedule_is_deterministic_across_fresh_instances():
    # blake2b, not Python's salted hash: two nodes on separate processes/machines agree with no exchange.
    a = KeyedSchedule(b"flight-key", epoch_s=0.01)
    b = KeyedSchedule(b"flight-key", epoch_s=0.01)
    for t in (0.0, 0.01, 1.234, 42.0):
        assert a.predict_offset(b"DRONE007", t, window_s=1.0) == b.predict_offset(b"DRONE007", t, window_s=1.0)


def test_keyed_schedule_is_keyed_and_epoch_varying():
    w = 1.0
    k1 = KeyedSchedule(b"key-one", epoch_s=0.01)
    k2 = KeyedSchedule(b"key-two", epoch_s=0.01)
    # a different key gives a different sequence (noise-like to anyone without it)
    assert k1.predict_offset(b"DRONE007", 3.0, window_s=w) != k2.predict_offset(b"DRONE007", 3.0, window_s=w)
    # successive epochs draw fresh offsets (no fixed start that repeats)
    offs = [k1.predict_offset(b"DRONE007", e * 0.01, window_s=w) for e in range(8)]
    assert len(set(offs)) >= 6                          # essentially all distinct over 8 epochs


def test_keyed_schedule_guards_missing_identity():
    sched = KeyedSchedule(b"flight-key", epoch_s=0.01)
    assert sched.transmit_offset(PlanContext(node_id=None, t=1.0), window_s=0.02) is None
    assert sched.transmit_offset(None, window_s=0.02) is None
    assert sched.predict_offset(None, 1.0, window_s=0.02) is None


def test_keyed_schedule_accepts_bytes_int_or_str_keys():
    # an integrator may key with bytes, an int, or a string; each is stable and gives a valid offset,
    # and the schedule still only coordinates timing (plan() returns the core default untouched).
    base = TxPlan(rate=2.4e6)
    for key in (b"\x01\x02", 1234, "alice"):
        s = KeyedSchedule(key, epoch_s=0.01)
        off = s.predict_offset(b"DRONE007", 2.0, window_s=1.0)
        assert 0.0 <= off < 1.0
        assert s.predict_offset(b"DRONE007", 2.0, window_s=1.0) == off      # deterministic
        assert s.plan(Intent(to=None, n_bytes=100), base, None) is base      # timing only
    # distinct keys that happen to coincide as bytes would collide; these three do not
    offs = {KeyedSchedule(k, epoch_s=0.01).predict_offset(b"DRONE007", 2.0, window_s=1.0)
            for k in (b"\x01\x02", 1234, "alice")}
    assert len(offs) == 3


# ── public-API input contracts (what the SDK promises an integrator who mis-calls it) ───────────────

def test_txplan_every_mode_is_lit_K_must_equal_L():
    assert TxPlan(rate=2.4e6, L=64).K == 64            # K defaults to L
    assert TxPlan(rate=2.4e6, L=32, K=32).K == 32
    with pytest.raises(ValueError, match="every mode is lit"):
        TxPlan(rate=2.4e6, L=64, K=32)                 # a spare mode leaks the width -- refused


def test_node_ids_are_eight_bytes():
    with pytest.raises(ValueError, match="8 bytes"):
        Node(b"short", RADIO, slot_s=0.01)


def test_null_interference_is_a_no_op():
    detector = NullInterference()
    assert detector.observe(BandObservation(t=0.0, occupancy=0.5, center=0.0, interference=0.0)) is None
    base = TxPlan(rate=2.4e6)
    assert detector.review(base) is base                     # observes nothing, changes nothing, never holds


# ── the optional pseudonymous-addressing hook (metadata privacy) ──────────────────────────────

class KeyedPseudonym(NullCrypto):
    """A toy metadata-privacy provider: a header identity becomes a keyed token of (key, epoch,
    identity), unlinkable to anyone without the key; a holder resolves a token by recomputing the
    same function over the identities it knows.  Exists to lock the address/resolve contract -- a
    real deployment's provider rotates faster and manages the member set, but obeys the same shape."""
    name = "keyed-pseudonym"

    def __init__(self, key: bytes, known=()):
        from hashlib import blake2b
        self._key = bytes(key)
        self._blake2b = blake2b
        self.known = list(known)                       # identities this node can attribute

    def address(self, identity: bytes, *, context: bytes = b"") -> bytes:
        return self._blake2b(bytes(identity), key=self._key, salt=bytes(context)[:16], digest_size=8).digest()

    def resolve(self, token: bytes, *, context: bytes = b"") -> bytes | None:
        for ident in self.known:
            if self.address(ident, context=context) == token:
                return ident
        return None


def test_null_crypto_addressing_is_identity_passthrough():
    # the DEFAULT: address and resolve return their argument unchanged -> raw addressing, so a core
    # that routes on address(id) is byte-identical to one that routes on the raw id.
    c = NullCrypto()
    for ident in (b"NODE0000", b"HOME\x00\x00\x00\x00", b""):
        assert c.address(ident) == ident
        assert c.resolve(ident) == ident
    assert c.address(b"NODE0000", context=b"epoch-42") == b"NODE0000"   # context does not perturb the null
    assert isinstance(NullCrypto(), CryptoProvider)                     # structural: NullCrypto IS a CryptoProvider


def test_predictive_agility_uses_flight_state():
    # the reference predictive provider USES the new PlanContext flight fields (velocity/trajectory)
    # for prediction, steers width from motion, computes a beam bearing, and inherits the keyed schedule.
    from entroptics_mesh.predict import PredictiveAgility
    p = PredictiveAgility(b"flight-key", horizon_s=2.0, spread_max=4.0)
    ctx = PlanContext(me=Position(45.0, -75.0, 100.0), velocity=(30.0, 0.0, 0.0), t=0.0, node_id=b"LEAD0000")
    # dead-reckon: 30 m/s east for 2 s -> ~60 m east (works in any consistent frame: GPS or fleet-relative)
    fut = p.predict_position(ctx)
    assert 55.0 < fut.distance_m(ctx.me) < 65.0
    az, el = p.beam_bearing(ctx.me, fut)
    assert abs(az - 90.0) < 1.0 and abs(el) < 1.0            # due east, level
    # trajectory, when given, overrides dead-reckoning
    wp = Position(45.0, -74.9, 100.0)
    ctx2 = replace(ctx, trajectory=((5.0, wp),))
    assert p.predict_position(ctx2, horizon_s=5.0).distance_m(wp) < 1.0
    # width steer: a fast node widens (more spread/margin); the node transmits on the returned plan
    base = TxPlan(rate=1.2e6)
    assert p.plan(Intent(to=None, n_bytes=100), base, None, ctx).rate > base.rate
    # a still node on no special link keeps the default
    still = PlanContext(me=ctx.me, velocity=(0.0, 0.0, 0.0), t=0.0, node_id=b"LEAD0000")
    assert p.plan(Intent(to=None, n_bytes=100), base, None, still) is base
    # inherits the keyed schedule (predict == transmit, deterministic)
    assert p.transmit_offset(ctx, window_s=0.02) == p.predict_offset(b"LEAD0000", 0.0, window_s=0.02)


def test_pseudonym_hides_identity_and_a_holder_resolves_its_own():
    epoch = b"\x00\x00\x00\x2a"
    src, dst, relay = b"DRONE001", b"HOME\x00\x00\x00\x00", b"RELAY007"
    net = KeyedPseudonym(b"flight-key", known=[src, dst, relay])

    to_tok = net.address(dst, context=epoch)
    assert to_tok != dst and len(to_tok) == 8          # the destination is concealed on the wire
    # a destination recognizes a frame as its own by recomputing its own token (the core's self-check)
    assert net.address(dst, context=epoch) == to_tok
    assert net.resolve(to_tok, context=epoch) == dst   # a holder attributes it

    # unlinkable to a party without the key, and across epochs
    outsider = KeyedPseudonym(b"other-key", known=[dst])
    assert outsider.resolve(to_tok, context=epoch) is None
    assert net.address(dst, context=b"\x00\x00\x00\x2b") != to_tok      # next epoch -> fresh token


def test_pseudonym_token_is_stable_within_an_epoch_so_dedup_holds():
    # a message's bursts must carry ONE token for msg_id dedup/for-me to work: within an epoch the
    # token is fixed (what changes is the epoch, coarser than a delivery), so flooding is unaffected.
    net = KeyedPseudonym(b"flight-key")
    epoch = b"\x00\x00\x00\x07"
    assert net.address(b"DRONE001", context=epoch) == net.address(b"DRONE001", context=epoch)


# ── the optional cover-traffic hook (metadata privacy, scheduling side) ───────────────────────

class CoverPolicy(NullAgility):
    """A toy cover-traffic provider: pad an idle slot with one decoy, drawn from the same plan the
    node would use, so presence/timing are masked.  Locks the cover_traffic contract -- a real
    policy keys the decision and the decoy's shape, but obeys the same (ctx, sending)->plans shape."""
    name = "cover"

    def __init__(self, decoy: TxPlan):
        self.decoy = decoy
        self.calls = []

    def cover_traffic(self, ctx, *, sending):
        self.calls.append((ctx, sending))
        return [] if sending else [self.decoy]      # pad only the idle slots (fake presence)


def test_null_agility_emits_no_cover():
    # the default: no decoys in either slot state -> byte-identical to a mesh with no cover hook.
    a = NullAgility()
    ctx = PlanContext(node_id=b"NODE0000", t=1.0)
    assert a.cover_traffic(ctx, sending=True) == []
    assert a.cover_traffic(ctx, sending=False) == []


def test_cover_policy_pads_idle_slots_only():
    decoy = TxPlan(rate=2.4e6, M=4)
    pol = CoverPolicy(decoy)
    ctx = PlanContext(node_id=b"NODE0000", t=3.0)
    assert pol.cover_traffic(ctx, sending=True) == []          # no padding when real traffic flows
    out = pol.cover_traffic(ctx, sending=False)
    assert out == [decoy] and all(isinstance(p, TxPlan) for p in out)
    # the decoy is an ordinary, valid plan (every mode lit) -> on the air it is not separable from
    # a payload burst; it carries keyed-random bytes the node fills, so a receiver CRC-drops it.
    assert out[0].K == out[0].L
    assert pol.plan(Intent(to=None, n_bytes=100), decoy, None) is decoy   # cover-only; plan() untouched
