"""The roadmap-7c keyed transmit schedule, wired into the live daemon (run_bridge) and the node.

The §3a receive path already reads a tracked sender on a local scan; 7c lets a RECEIVER predict a
sender's start from a shared key so the local scan fires on every burst, not only a stable-clock one
(research/realtime/port_results.md §7c).  The provider (schedules.KeyedSchedule), the node receive
path (Node._predict_tracked / _anchor_schedule) and the neighbour lock cache are built and unit-tested
elsewhere; these tests lock the WIRING the live daemon needed:

  * build() attaches the keyed schedule only when flagged (DEFAULT OFF), keyed on the SAME shared
    waveform key, quantized on the slot cadence; the env var overrides the config;
  * the schedule changes only WHEN a burst is placed (Node.scheduled_tx_offset), never the burst's
    samples -- the transmitted waveform is BYTE-IDENTICAL with the feature off vs on (proved by
    sha256 + max|diff|, like the COARSE_ROWS proof);
  * driven through the real node receive path with the arrival following the keyed offset (the inner
    emu Air carries a sender's intra-slot offset), a scheduled receiver takes the COORDINATED tracked
    path (stats.coord_hits) on every post-first burst, while a null receiver never does -- and both
    deliver the same message (the CRC fallback guarantees a wrong prediction only ever re-scans).
"""
from __future__ import annotations

import hashlib

import numpy as np

from entroptics_mesh import phy
from entroptics_mesh.adapt import Radio
from entroptics_mesh.emu.air import Air, Radio as AirRadio
from entroptics_mesh.link import frame
from entroptics_mesh.neighbors import Position
from entroptics_mesh.node import Node
from entroptics_mesh.providers import NullAgility, NullCrypto, PlanContext, Providers, TxPlan
from entroptics_mesh.run_bridge import build
from entroptics_mesh.schedules import KeyedSchedule

KEY = 0x00E7
RX = 2.4e6
SLOT_S = 0.05
SENDER = b"A" * 8
RECEIVER = b"B" * 8


class _KeyCrypto(NullCrypto):
    """Pins the shared PHY waveform key so both ends' PHY agree (payload sealing stays the null)."""
    name = "key-only"

    def __init__(self, key):
        self._key = key

    def waveform_key(self, context):
        return self._key


def _radio() -> Radio:
    return Radio(min_rate=1.2e6, max_rate=RX, center_hz=915e6, max_power_dbm=20.0, noise_dbm=-100.0)


def _node(keyed: bool, node_id: bytes = SENDER, seed: int = 3) -> Node:
    ag = KeyedSchedule(KEY, epoch_s=SLOT_S) if keyed else NullAgility()
    return Node(node_id, _radio(), slot_s=SLOT_S, providers=Providers(crypto=_KeyCrypto(KEY), agility=ag),
                seed=seed, blind_scaled=False)


# ---- shared mesh config for build() ---------------------------------------------------------------

_CONFIG = {
    "freq_hz": 915e6, "rate": RX, "waveform_key": KEY,
    "radio": {"min_rate": 1.2e6, "max_rate": RX, "center_hz": 915e6,
              "max_power_dbm": 20.0, "noise_dbm": -100.0},
    "radios": [
        {"x": 0.0, "y": 0.0, "z": 100.0, "tx_dbm": 20.0, "noise_dbm": -100.0, "node_id": "AAAAAAAA"},
        {"x": 9000.0, "y": 0.0, "z": 100.0, "tx_dbm": 20.0, "noise_dbm": -100.0, "node_id": "BBBBBBBB"},
    ],
}


class _QTap:
    def __init__(self):
        self.written, self._q = [], []

    def read(self):
        return self._q.pop(0)

    def write(self, f):
        self.written.append(f)


def _build(cfg: dict):
    node, air, bridge, runner = build(cfg, 0, _QTap(), peers=[], bind=("127.0.0.1", 0),
                                      now_fn=lambda: 0.0)
    return node, air, runner


# ---- the flag: default off, config on, env overrides ----------------------------------------------

def test_build_without_flag_attaches_null_agility():
    node, air, runner = _build(_CONFIG)
    try:
        assert isinstance(node.providers.agility, NullAgility)
        # the null provider schedules nothing -> no keyed offset, burst placed exactly as before
        assert node.providers.agility.transmit_offset(PlanContext(node_id=node.node_id, t=1.0),
                                                       window_s=node.contention_s) is None
        assert all(node.scheduled_tx_offset(k * SLOT_S) == 0.0 for k in range(1, 9))
    finally:
        air.close()


def test_build_with_flag_attaches_keyed_schedule_on_the_waveform_key():
    node, air, runner = _build(dict(_CONFIG, keyed_schedule=True))
    try:
        ag = node.providers.agility
        assert isinstance(ag, KeyedSchedule)
        assert ag.epoch_s == runner.slot_s                   # quantized on the slot cadence
        assert node._sched_tx is not None and node._sched_pred is not None
        # keyed on the SAME shared waveform key -> a fresh KeyedSchedule(key) derives the same offset
        ref = KeyedSchedule(_CONFIG["waveform_key"], epoch_s=runner.slot_s)
        cont = node.contention_s
        assert node.scheduled_tx_offset(0.05) == ref.predict_offset(node.node_id, 0.05, window_s=cont)
    finally:
        air.close()


def test_env_flag_overrides_config(monkeypatch):
    monkeypatch.setenv("ENTROPTICS_KEYED_SCHEDULE", "1")     # env on, config silent
    node, air, _ = _build(_CONFIG)
    try:
        assert isinstance(node.providers.agility, KeyedSchedule)
    finally:
        air.close()
    monkeypatch.setenv("ENTROPTICS_KEYED_SCHEDULE", "0")     # env off overrides config on
    node2, air2, _ = _build(dict(_CONFIG, keyed_schedule=True))
    try:
        assert isinstance(node2.providers.agility, NullAgility)
    finally:
        air2.close()


# ---- byte-identical transmit: the schedule changes WHEN, never the samples ------------------------

def test_keyed_schedule_tx_burst_is_byte_identical():
    off_node, on_node = _node(False), _node(True)
    payload = bytes(range(200))
    off_node.send(payload, RECEIVER, 0.0)
    on_node.send(payload, RECEIVER, 0.0)
    iq_off, plan_off = off_node.next_burst(0.0)
    iq_on, plan_on = on_node.next_burst(0.0)
    assert plan_off == plan_on                               # the schedule leaves the plan untouched
    h_off = hashlib.sha256(np.ascontiguousarray(iq_off).tobytes()).hexdigest()
    h_on = hashlib.sha256(np.ascontiguousarray(iq_on).tobytes()).hexdigest()
    assert h_off == h_on                                     # same bytes on the air
    assert float(np.max(np.abs(iq_off - iq_on))) == 0.0


def test_scheduled_tx_offset_zero_without_schedule_keyed_and_varying_with():
    off_node, on_node = _node(False), _node(True)
    assert off_node.scheduled_tx_offset(0.05) == 0.0
    cont = on_node.contention_s
    offs = [on_node.scheduled_tx_offset(k * SLOT_S) for k in range(1, 9)]
    for k, o in zip(range(1, 9), offs):
        assert 0.0 <= o <= cont
        assert o == on_node.providers.agility.predict_offset(SENDER, k * SLOT_S, window_s=cont)
    assert len(set(offs)) >= 6                               # a fresh offset per slot -> timing is active


# ---- the receive path takes the coordinated tracked path when scheduled ---------------------------

def _sender_burst():
    """One matched-rate full-width DQPSK burst with the sender's mark (origin A), composed exactly as
    a node would, reused for every slot (the payload is the same message)."""
    a = Node(SENDER, _radio(), slot_s=SLOT_S, providers=Providers(crypto=_KeyCrypto(KEY)), seed=1,
             blind_scaled=False)
    a.send(bytes(range(200)), RECEIVER, 0.0)
    sym, _ = a.next_symbol(0.0)
    plan = TxPlan(rate=RX, center=0.0, power_db=0.0, M=4, family="differential", code_rate="1/2")
    iq = phy.transmit_burst(frame(sym, plan.code_rate), plan, KEY, sender=a.node_id)
    return iq, plan


def _run_receiver(keyed: bool, n_bursts: int = 5, seed: int = 0) -> Node:
    iq0, plan = _sender_burst()
    amp = 10.0 ** (min(plan.power_db, 0.0) / 20.0)
    air = Air(rate=RX, seed=seed)
    air.add(AirRadio(position=np.array([0.0, 0.0, 100.0]), tx_power_dbm=20.0, noise_dbm=-100.0))
    air.add(AirRadio(position=np.array([1500.0, 0.0, 100.0]), tx_power_dbm=20.0, noise_dbm=-100.0))
    ag = KeyedSchedule(KEY, epoch_s=SLOT_S) if keyed else NullAgility()
    b = Node(RECEIVER, _radio(), slot_s=SLOT_S, providers=Providers(crypto=_KeyCrypto(KEY), agility=ag),
             position=Position(0.0, 0.0, 100.0), seed=7, blind_scaled=False, geometry_age=1e9)
    b.learn(phy.Known(plan.L, 1.0, 0.0))                     # first contact seeded (matched rate, centre 0)
    cont = b.contention_s
    sched = KeyedSchedule(KEY, epoch_s=SLOT_S)
    rng = np.random.default_rng(1000 + seed)
    n = iq0.size + int(np.ceil(cont * RX)) + 512
    for slot in range(n_bursts):
        t = slot * SLOT_S
        off = (sched.transmit_offset(PlanContext(t=t, node_id=SENDER), window_s=cont)
               if keyed else float(rng.uniform(0.0, cont)))
        air._bursts.clear()
        air.send(0, t + off, iq0 * amp, rate=RX, center=0.0)    # the sender's intra-slot offset -> arrival
        b.on_capture(air.capture(1, t, n), t)
    return b


def test_scheduled_receiver_takes_the_coordinated_tracked_path_null_does_not():
    n = 5
    keyed, null = _run_receiver(True, n_bursts=n), _run_receiver(False, n_bursts=n)

    # every burst decodes at the known geometry on both (the CRC-gated full scan is the floor)
    assert keyed.stats.fast_hits == n and null.stats.fast_hits == n

    # keyed: a prediction on every post-first burst, and the local scan locks it (the §3a tracked path)
    assert keyed.stats.coord_reads == n - 1
    assert keyed.stats.coord_hits >= 1
    assert keyed.stats.coord_hits == keyed.stats.coord_reads

    # null: no provider -> no prediction; a random start never earns the lock-cache prior either
    assert null.stats.coord_reads == 0 and null.stats.coord_hits == 0
    assert null.stats.local_hits == 0

    # delivery is identical with and without the schedule, and equal to what was sent (one message)
    want = bytes(range(200))
    assert [m.payload for m in keyed.inbox] == [want]
    assert [m.payload for m in null.inbox] == [want]
