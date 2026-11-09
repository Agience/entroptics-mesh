"""The detection side of interference handling (``ext.interference.EnergyInterference``): the provider contract, a cell
floor read against noise alone, and end to end over the emulated air -- a tone interferer beside the
destination is detected, announced, and the sender's plan steps off it; a clean band reports
nothing."""
from __future__ import annotations

import math

import numpy as np
import pytest

from entroptics_mesh import phy
from entroptics_mesh.adapt import Radio
from entroptics_mesh.emu.air import Air, Radio as AirRadio, fspl_db
from entroptics_mesh.emu.node_run import NodeRun
from entroptics_mesh.ext.interference import EnergyInterference, noise_spread
from entroptics_mesh.neighbors import Position
from entroptics_mesh.node import Node
from entroptics_mesh.providers import InterferenceProvider, BandObservation, NullCrypto, Providers, TxPlan

RX = 2.4e6
CELL = 1.2e6


def _noise(n, rng, power=1e-10):
    return math.sqrt(power / 2) * (rng.standard_normal(n) + 1j * rng.standard_normal(n))


def _tone(n, off, power, rate=RX):
    return math.sqrt(power) * np.exp(2j * np.pi * off * np.arange(n) / rate)


def _obs(iq, cell=CELL, t=0.0):
    return BandObservation(t, 0.0, 0.0, 0.0, rate=RX, cell=cell, iq=iq)


# ── the provider ─────────────────────────────────────────────────────────────────────────────

def test_energy_interference_passes_the_provider_contract():
    detector = EnergyInterference()
    assert isinstance(detector, InterferenceProvider) and isinstance(detector.name, str)
    base = TxPlan(rate=RX)
    assert detector.review(base) is base                              # detection only: plans pass through
    assert detector.observe(BandObservation(0.0, 0.5, 0.0, 0.0)) is None     # no capture handed over: no read
    assert detector.interference_bands() == []


def test_noise_alone_reports_nothing():
    rng = np.random.default_rng(5)
    for cell in (CELL, 0.6e6, 0.3e6):                           # 2, 4 and 8 cells
        detector = EnergyInterference()
        for k in range(40):
            detector.observe(_obs(_noise(19_000, rng), cell, t=k))
            assert detector.interference_bands() == [], (cell, k)


def test_a_tone_in_one_cell_is_reported_at_that_cell():
    rng = np.random.default_rng(6)
    n = 19_000
    for cell, off in ((CELL, 0.6e6), (0.6e6, -0.9e6), (0.6e6, 0.3e6)):
        detector = EnergyInterference()
        detector.observe(_obs(_noise(n, rng) + _tone(n, off, 4e-10), cell))     # +6 dB over the band noise
        got = detector.interference_bands()
        assert [j.offset_hz for j in got] == [off], (cell, off, got)
        assert got[0].width_hz == cell and got[0].level_db > 3.0


def test_a_burst_that_moves_between_cells_is_not_interference():
    # a burst fills most of a capture in one cell, the next capture in the other: quiet blocks at the
    # head and tail, and the held floor, keep both cells at the noise.
    rng = np.random.default_rng(7)
    n = 19_000
    detector = EnergyInterference()
    for k in range(20):
        burst = np.zeros(n, complex)
        burst[1500:n - 600] = _noise(n - 2100, rng, 1e-8) * np.exp(
            2j * np.pi * (0.6e6 if k % 2 else -0.6e6) * np.arange(n - 2100) / RX)
        detector.observe(_obs(_noise(n, rng) + burst, t=k))
        assert detector.interference_bands() == [], k


def test_the_bound_is_read_from_noise_and_tightens_with_blocks():
    assert noise_spread(150, 2, Node.FLOOR_QUANTILE) > 1.0
    assert noise_spread(150, 2, Node.FLOOR_QUANTILE) < noise_spread(40, 2, Node.FLOOR_QUANTILE)
    assert noise_spread(40, 8, Node.FLOOR_QUANTILE) > noise_spread(40, 2, Node.FLOOR_QUANTILE)


# ── end to end over the emulated air ─────────────────────────────────────────────────────────

RADIO = Radio(min_rate=1.2e6, max_rate=RX, center_hz=915e6, max_power_dbm=20.0, noise_dbm=-100.0)
SUB = [phy.Known(64, 0.5, 0.25), phy.Known(64, 0.5, -0.25)]
TONE = 0.6e6                                    # the interferer's offset: the upper 1.2 MHz sub-band


class KeyOnly(NullCrypto):
    name = "key-only"

    def waveform_key(self, context):
        return 1234


class InterferedAir(Air):
    """The emulated air with continuous tone interferers: each adds ``power_dbm`` less the free-space
    loss to the receiver, at ``offset`` Hz from the air's centre, phase-continuous in time."""

    def __init__(self, *a, interferers=(), **k):
        super().__init__(*a, **k)
        self.interferers = list(interferers)

    def capture(self, receiver, t0, n):
        y = super().capture(receiver, t0, n)
        at = self.radios[receiver].position
        for pos, power_dbm, offset in self.interferers:
            d = float(np.linalg.norm(np.asarray(pos, float) - at))
            amp = 10.0 ** ((power_dbm - fspl_db(d, self.freq_hz)) / 20.0)
            y = y + amp * np.exp(2j * np.pi * offset * (t0 + np.arange(int(n)) / self.rate))
        return y


class Watching(EnergyInterference):
    """Keeps what the provider reported after every observation."""

    def __init__(self):
        super().__init__()
        self.reports = []

    def observe(self, obs):
        super().observe(obs)
        self.reports.append(self.interference_bands())


def _pair(interferers, interference):
    # a sends to b over 9 km; b runs the interference provider and announces every 50 ms
    air = InterferedAir(rate=RX, seed=3, interferers=interferers)
    nodes = []
    for i, x in enumerate((0.0, 9000.0)):
        air.add(AirRadio(position=np.array([x, 0.0, 100.0]), tx_power_dbm=20.0, noise_dbm=-100.0))
        p = Providers(crypto=KeyOnly(), interference=interference) if i == 1 else Providers(crypto=KeyOnly())
        nodes.append(Node(bytes([65 + i]) * 8, RADIO, slot_s=0.0,
                          position=Position(0.0, math.degrees(x / 6_371_000.0), 100.0),
                          providers=p, seed=100 + i, blind_scaled=False,
                          announce_every=0.05 if i == 1 else None))
    a, b = nodes
    for x, y in ((a, b), (b, a)):
        x.on_announcement(y.announcement(0.0), y.node_id, 0.0)
    for nd in nodes:
        for g in SUB:
            nd.learn(g)
    return air, a, b


def _centres(node, to, draws=60):
    return {node.plan_for(node.router.originate(to, node.hops)).center for _ in range(draws)}


def _send(run, a, b, k):
    a.send(bytes(np.random.default_rng(k).integers(0, 256, 200, dtype=np.uint8)), b.node_id, run.now())
    run.run(40, until=lambda: len(b.inbox) > k)


@pytest.mark.slow
def test_an_interferer_at_the_destination_is_detected_announced_and_steered_off():
    # a tone 100 m from b, ~7 dB over b's noise in the upper sub-band; far below the noise at a
    detector = Watching()
    air, a, b = _pair([((9000.0, 100.0, 100.0), -25.0, TONE)], detector)
    assert _centres(a, b.node_id) == {-0.6e6, 0.6e6}            # before: either sub-band
    run = NodeRun(air, [a, b])
    _send(run, a, b, 0)                                         # b captures and reads the band ...
    run.run(40, until=lambda: bool(a.table.get(b.node_id).announced.interference))   # ... and announces
    reported = a.table.get(b.node_id).announced.interference          # heard over the air from b
    assert [(j.offset_hz, j.width_hz) for j in reported] == [(TONE, 1.2e6)]
    assert _centres(a, b.node_id) == {-0.6e6}                   # after: only the clear sub-band
    first = len(run.log)
    for k in (1, 2):
        _send(run, a, b, k)
    sent = [p for _, i, p in run.log[first:] if i == 0]
    assert sent and all(p.center == -0.6e6 for p in sent)       # every burst a sends steps off it
    assert len(b.inbox) == 3                                    # and reaches b
    assert all(r and r[0].offset_hz == TONE for r in detector.reports)


@pytest.mark.slow
def test_a_clean_band_reports_no_interference():
    detector = Watching()
    air, a, b = _pair([], detector)
    run = NodeRun(air, [a, b])
    for k in range(3):
        _send(run, a, b, k)
    assert len(b.inbox) == 3
    assert len(detector.reports) >= 3                                 # b read the band on every capture ...
    assert all(r == [] for r in detector.reports)                     # ... and found nothing
    assert a.table.get(b.node_id).announced.interference == ()
    assert _centres(a, b.node_id) == {-0.6e6, 0.6e6}            # nothing to steer off
