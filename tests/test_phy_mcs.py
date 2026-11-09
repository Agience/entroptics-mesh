"""Every planned mode on the emulated air: the burst says nothing of its family, alphabet or code
rate, and the receiver learns all three from the burst (both reads, the CRC choosing)."""
from __future__ import annotations

import math

import numpy as np
import pytest

from entroptics_mesh import adapt, fec, phy
from entroptics_mesh.emu.air import Air, Radio as AirRadio, fspl_db
from entroptics_mesh.emu.node_run import NodeRun
from entroptics_mesh.link import BLOCK, FRAME_BYTES, frame, read_frame, symbols
from entroptics_mesh.providers import TxPlan
from entroptics_mesh.routing import Header

RX = 2.4e6
KEY = 1234
MARGIN = 5.0          # dB above each mode's 1%-PER planning threshold


def _mode(family: str, M: int, rate: str) -> adapt.Mode:
    return next(m for m in adapt.MODES if (m.family, m.M, m.rate) == (family, M, rate))


Q16_12 = _mode("coherent", 16, "1/2")
D8_34 = _mode("differential", 8, "3/4")
DQ_34 = _mode("differential", 4, "3/4")


def _over_the_air(mode: adapt.Mode, rate: float, snr_db: float, *, seed: int = 0, key=KEY,
                  lead: int = 100):
    """One frame in ``mode`` from a sender at ``rate`` into a 2.4 Msps receiver, at ``snr_db``
    in-band (noise over the sender's width).  Returns (symbol sent, capture)."""
    msg = bytes(np.random.default_rng(seed).integers(0, 256, 2400, dtype=np.uint8))
    sym = symbols(Header(b"A" * 8, 1, b"C" * 8, 4), msg, BLOCK, 1.0)[seed % 4]
    d = 6000.0
    rx_dbm = 20.0 - fspl_db(d, 915e6)
    noise = rx_dbm - snr_db + 10 * math.log10(RX / rate)
    air = Air(rate=RX, seed=seed + 7)
    a = air.add(AirRadio(position=np.array([0.0, 0, 100]), tx_power_dbm=20.0, noise_dbm=noise))
    b = air.add(AirRadio(position=np.array([d, 0, 100]), tx_power_dbm=20.0, noise_dbm=noise))
    plan = TxPlan(rate=rate, M=mode.M, family=mode.family, code_rate=mode.rate)
    iq = phy.transmit_burst(frame(sym, mode.rate), plan, key, sender=b"A" * 8, rng=seed)
    air.send(a, lead / RX, iq, rate=None if rate == RX else rate)
    return sym, air.capture(b, 0.0, int(iq.size * RX / rate + 2 * lead + 200))


def _known_read(mode, rate, seed=0, key=KEY):
    sym, y = _over_the_air(mode, rate, mode.threshold_db + MARGIN, seed=seed, key=key)
    # a prior a few ppm off in scale and 5e-4 of the band off in centre, as a receiver holds it
    rx = phy.receive_known(y, key, phy.Known(64, (rate / RX) * (1 + 2e-6), 5e-4),
                           accept=(lambda r: read_frame(r) is not None) if key is None else None)
    return sym, rx, read_frame(rx)


_CASES = ([(m, r) for m in adapt.MCS for r in (RX, 1.0e6)]
          + [(m, 1.0e6) for m in adapt.MODES if m not in adapt.MCS])


@pytest.mark.parametrize("mode,rate", _CASES,
                         ids=[f"{m.family[:4]}{m.M}-{m.rate.replace('/', '')}-{r / 1e6:.1f}" for m, r in _CASES])
def test_every_mode_round_trips_at_a_known_geometry(mode, rate):
    """Every mode of the planner's envelope at matched rate and at 1.0 -> 2.4 Msps, and the rest of
    the measured modes (a one-family node's envelope) at 1.0 -> 2.4: the frame decodes and the CRC
    names the mode it was sent in."""
    sym, rx, got = _known_read(mode, rate)
    assert got is not None, f"no hypothesis passed (rows d={rx.rows('differential')} c={rx.rows('coherent')})"
    assert got[0].data == sym.data
    assert got[1] == (mode.family, mode.M, mode.rate)


def test_the_burst_length_is_read_and_prunes_the_hypotheses():
    """The family's own read measures the payload rows (exact or one short); the frame layer then
    decodes only the hypotheses that length admits."""
    for mode in (adapt.MCS[0], Q16_12, adapt.MCS[-1]):
        _, rx, got = _known_read(mode, RX)
        n = fec.packet_coded_length(FRAME_BYTES, mode.rate)
        n_sym = -(-n // int(math.log2(mode.M)))
        rows = -(-n_sym // 64)                 # every mode lit: K = L = 64
        assert rx.rows(mode.family) in (rows - 1, rows)
        assert got is not None and got[1] == (mode.family, mode.M, mode.rate)


def test_open_mode_reads_both_families():
    """The public zero state (no key): the differential read needs the frame check to place the
    burst's first row; the coherent read finds its reference row on its own."""
    for mode in (DQ_34, Q16_12):
        sym, rx, got = _known_read(mode, 1.0e6, key=None)
        assert got is not None and got[0].data == sym.data and got[1][0] == mode.family


_DENSE = [m for m in adapt.MCS if m.family == "coherent" and m.M >= 64]


@pytest.mark.parametrize("mode", _DENSE, ids=[f"{m.M}qam-{m.rate.replace('/', '')}" for m in _DENSE])
def test_open_mode_levels_each_row_of_a_dense_coherent_burst(mode):
    """Open, each row's power is the sender's own draw (rf ``row_radius``), so the receiver levels
    the rows itself (``waveform._row_levels``) before the LLRs: a ~6% per-row gain error left on
    would sink 64- and 256-QAM.  Matched and 1.0 -> 2.4 Msps."""
    for rate in (RX, 1.0e6):
        sym, rx, got = _known_read(mode, rate, key=None)
        assert not rx.keyed
        assert got is not None and got[0].data == sym.data
        assert got[1] == (mode.family, mode.M, mode.rate)


@pytest.mark.parametrize("mode", [Q16_12, D8_34], ids=["16qam-12", "d8psk-34"])
def test_blind_reception_learns_the_mode_from_the_burst(mode):
    """No geometry known either: discovery, then both reads, then the CRC."""
    sym, y = _over_the_air(mode, RX, mode.threshold_db + 6.0, seed=1, lead=37)
    rx = phy.receive_burst(y, KEY, candidates=range(60, 69))
    got = read_frame(rx)
    assert rx.locked and got is not None and got[0].data == sym.data
    assert got[1] == (mode.family, mode.M, mode.rate)


def test_batched_viterbi_matches_the_single_decoder():
    rng = np.random.default_rng(0)
    for _ in range(10):
        s = rng.normal(0.3, 1.0, 2 * int(rng.integers(8, 400)))
        assert np.array_equal(fec.viterbi_decode(s), fec.viterbi_decode_batch(s)[0])
    x = fec.encode_packet(b"rate", frame_bytes=64, rate="2/3")
    soft = fec.hard_to_soft(x)
    assert fec.decode_packets([(soft, "1/2"), (soft, "3/4"), (soft, "2/3")]) == [None, None, b"rate"]


def test_a_node_pair_delivers_over_a_coherent_plan():
    """Two nodes that send coherent QAM only: the planner picks the coherent mode the hop's SNR
    clears, the receiver is told nothing about it, and the message arrives.  (With both families
    allowed the planner now picks coherent at a whispered hop's ~12 dB anyway -- 16-QAM 1/2 at
    10.2 dB, research/mcs/kL.md -- so restricting the family keeps this test about the coherent
    plan whatever the envelope.)"""
    from test_node import KeyOnly, RADIO, SUB, _introduce, _msg, _pos
    from entroptics_mesh.node import Node
    from entroptics_mesh.providers import Providers

    air = Air(rate=RX, seed=5)
    nodes = []
    for i, x in enumerate((0.0, 9000.0)):
        air.add(AirRadio(position=np.array([x, 0.0, 100.0]), tx_power_dbm=20.0, noise_dbm=-100.0))
        nodes.append(Node(bytes([65 + i]) * 8, RADIO, slot_s=0.0, position=_pos(x),
                          providers=Providers(crypto=KeyOnly()), seed=50 + i, blind_scaled=False,
                          families=("coherent",)))
    a, b = nodes
    _introduce(nodes)
    for g in SUB:
        b.learn(g)
    run = NodeRun(air, nodes)
    msg = _msg(300, 7)
    a.send(msg, b.node_id, run.now())
    run.run(30, until=lambda: bool(b.inbox))
    assert [m.payload for m in b.inbox] == [msg]
    sent = [p for _, i, p in run.log if i == 0]
    assert sent and all(p.family == "coherent" for p in sent)
    assert b.table.get(a.node_id).measured.M == sent[-1].M          # the alphabet the CRC named


_Q256_34 = next(m for m in adapt.MCS if m.family == "coherent" and m.M == 256 and m.rate == "3/4")


def test_the_fourth_moment_measures_the_row_turn_the_fourth_power_leaves():
    """A residual row turn of 1e-3 cycles/row (what the fourth-power read leaves on a 13-row
    256-QAM burst) is taken off by the burst's own amplitude-weighted fourth moment -- no
    alphabet, no decision -- at every order, under noise."""
    from entroptics_rf import waveform as wf
    rng = np.random.default_rng(2)
    t = np.arange(1, 14)[:, None]
    for M in phy.COHERENT_M:
        Y = wf._qam(M)[rng.integers(0, M, (13, 64))]
        n = 10 ** (-32 / 20) * (rng.standard_normal(Y.shape) + 1j * rng.standard_normal(Y.shape)) / np.sqrt(2)
        got = phy._row_turn(Y * np.exp(2j * np.pi * 1e-3 * t) + n)
        assert np.mean(np.abs(got - Y) ** 2) < 3 * np.mean(np.abs(n) ** 2), M


@pytest.mark.parametrize("seed", [0, 9, 10])
def test_dense_qam_at_matched_rate_reads_under_its_fast_path_threshold(seed):
    """256-QAM 3/4 at matched rate, 28 dB -- 2 dB under the planner's fast-path threshold: these
    bursts failed with the fourth-power row carrier alone (measured 9/12 vs 11/12 at 28 dB)."""
    sym, y = _over_the_air(_Q256_34, RX, 28.0, seed=seed)
    rx = phy.receive_known(y, KEY, phy.Known(64, 1 + 2e-6, 5e-4))
    got = read_frame(rx)
    assert got is not None and got[0].data == sym.data and got[1] == ("coherent", 256, "3/4")


def test_the_fast_read_stops_at_the_first_frame_that_passes(monkeypatch):
    """With the frame check handed in, a burst that decodes as the settle left it is returned
    then: the carrier and start are not measured again (no leak read), and the check runs once."""
    mode = Q16_12                                         # 5 dB over its threshold
    sym, y = _over_the_air(mode, 1.0e6, mode.threshold_db + MARGIN, seed=3)
    calls, reads = [], []
    real_rows = phy.wf._scaled_rows

    def rows(*a, **k):
        reads.append(a[-1] if len(a) > 8 else k.get("max_rows"))
        return real_rows(*a, **k)
    monkeypatch.setattr(phy.wf, "_scaled_rows", rows)

    def accept(r):
        calls.append(r)
        return read_frame(r) is not None
    rx = phy.receive_known(y, KEY, phy.Known(64, (1.0e6 / RX) * (1 + 2e-6), 5e-4), accept=accept)
    got = read_frame(rx)
    assert got is not None and got[0].data == sym.data
    assert len(calls) == 1 and calls[0] is rx
    assert phy.REF_ROWS not in reads, "the geometry was refined after the frame passed"


def test_a_hypothesis_is_read_only_when_it_is_tried(monkeypatch):
    """The frame layer makes each hypothesis's soft bits when it decodes it: a differential burst
    that passes first never has its coherent read levelled and sliced."""
    mode = DQ_34
    sym, rx, _ = _known_read(mode, 1.0e6, key=None)
    rx.cache.clear()
    tried = []
    real = phy.RxBurst.soft_bits

    def soft_bits(self, family, M, n):
        tried.append(family)
        return real(self, family, M, n)
    monkeypatch.setattr(phy.RxBurst, "soft_bits", soft_bits)
    got = read_frame(rx)
    assert got is not None and got[1][0] == "differential"
    assert rx.family == "differential" and "coherent" not in tried


# ── the fast path's geometry, near the bottom of each mode (research/mcs/kL.md, fast path) ──────


def _capture_at(mode, rate, snr_db, *, seed, lead=100.0, tail_rx=None, key=KEY):
    """One frame in ``mode`` from a sender at ``rate``; the capture ends ``tail_rx`` received
    samples after the burst (default: as :func:`_over_the_air`)."""
    msg = bytes(np.random.default_rng(seed).integers(0, 256, 2400, dtype=np.uint8))
    sym = symbols(Header(b"A" * 8, 1, b"C" * 8, 4), msg, BLOCK, 1.0)[seed % 4]
    d = 6000.0
    noise = 20.0 - fspl_db(d, 915e6) - snr_db + 10 * math.log10(RX / rate)
    air = Air(rate=RX, seed=seed + 11)
    a = air.add(AirRadio(position=np.array([0.0, 0, 100]), tx_power_dbm=20.0, noise_dbm=noise))
    b = air.add(AirRadio(position=np.array([d, 0, 100]), tx_power_dbm=20.0, noise_dbm=noise))
    plan = TxPlan(rate=rate, M=mode.M, family=mode.family, code_rate=mode.rate)
    iq = phy.transmit_burst(frame(sym, mode.rate), plan, key, sender=b"A" * 8, rng=seed)
    air.send(a, lead / RX, iq, rate=None if rate == RX else rate)
    end = (lead + d / 299792458.0 * RX) + iq.size * RX / rate
    n = int(end + (2 * lead + 200 - lead - 48 if tail_rx is None else tail_rx))
    return sym, air.capture(b, 0.0, n)


def _fast(y, rate, key=KEY):
    return phy.receive_known(y, key, phy.Known(64, (rate / RX) * (1 + 2e-6), 5e-4),
                             accept=lambda r: read_frame(r) is not None)


@pytest.mark.parametrize("family,M,rate_code,rate,snr,seeds",
                         [("coherent", 4, "1/2", RX, 7.5, (1, 4, 5)),
                          ("coherent", 4, "1/2", 1.0e6, 8.0, (0, 1, 3)),
                          ("differential", 16, "1/2", 1.0e6, 16.0, (1, 2, 3))],
                         ids=["4qam-12-2.4", "4qam-12-1.0", "d16psk-12-1.0"])
def test_low_snr_modes_read_their_geometry(family, M, rate_code, rate, snr, seeds):
    """Modes whose fast-path thresholds the read once set rather than the code (kL.md, first
    table: 4-QAM 1/2 at 10.8 dB, D16PSK 1/2 at 17.7), well below those, on frames that read lost:
    the coherent gain is one for every mode, the burst's first row comes from its run of rows
    rather than the 16th-power closure, and the differential turn is read under each alphabet's
    own M."""
    mode = adapt.Mode(0.0, family, M, rate_code, 0.0)
    for seed in seeds:
        sym, y = _capture_at(mode, rate, snr, seed=seed)
        got = read_frame(_fast(y, rate))
        assert got is not None and got[0].data == sym.data and got[1] == (family, M, rate_code), seed


def test_a_burst_a_row_into_the_capture_is_read_from_its_own_first_row():
    """The scan places its row grid in the capture's first row.  When the burst begins a hair
    under one row in, that grid starts before the capture's first readable sample, and the rows
    read from it came back one short at the front (``waveform._scaled_rows`` drops a row it does
    not hold whole): the reference was read on the row of noise ahead of the burst.  The grid now
    moves a row on.  1.0 -> 2.4 Msps (a row is 153.6 received samples), bursts starting at
    153.35-153.55."""
    mode = _mode("coherent", 256, "2/3")
    for seed, lead in ((0, 105.35), (1, 105.45), (2, 105.5)):
        sym, y = _capture_at(mode, 1.0e6, 34.0, seed=seed, lead=lead)
        got = read_frame(_fast(y, 1.0e6))
        assert got is not None and got[0].data == sym.data, (seed, lead)


def test_a_capture_that_ends_with_the_burst_still_reads_it():
    """No row of silence after the burst (the capture ends 0.4 of a row past it): the rank edge
    of the row levels then falls between two of the burst's own rows, which sent the frame layer
    past the true hypothesis.  The length is checked against the burst's run of rows: it is the
    count (or one short), or None -- never another length -- and the frame decodes."""
    mode = _Q256_34
    n = fec.packet_coded_length(FRAME_BYTES, mode.rate)
    true = -(-(-(-n // 8)) // 64)
    for seed in range(4):
        sym, y = _capture_at(mode, 1.0e6, 40.0, seed=seed, tail_rx=60)
        rx = _fast(y, 1.0e6)
        assert rx.rows("coherent") in (None, true - 1, true), seed
        got = read_frame(rx)
        assert got is not None and got[0].data == sym.data, seed


def test_each_differential_alphabet_reads_its_own_turn():
    """A 16-PSK differential turned 0.02 cycles/row comes back on the lattice when read as 16-PSK;
    the turn is measured under the hypothesis's own M, not one the start closure named."""
    rng = np.random.default_rng(4)
    x = np.exp(2j * np.pi * rng.integers(0, 16, 4096) / 16)
    n = 0.05 * (rng.standard_normal(x.size) + 1j * rng.standard_normal(x.size))
    d = x * np.exp(2j * np.pi * 0.02) + n
    got = phy._diff_turn(d, 16)
    assert np.mean(np.abs(np.angle(got * np.conj(x)))) < 0.06
    assert np.mean(np.abs(np.angle(d * np.conj(x)))) > 0.1


def test_the_run_finds_a_burst_among_rows_of_noise():
    """Rows of noise read the null (1, spread ~0.16 at 64 modes), the burst's rows below it, and a
    message's short final row above it: the run is the burst's rows, short row excluded."""
    rng = np.random.default_rng(5)
    cm = np.concatenate([1 + 0.16 * rng.standard_normal(3), 0.5 + 0.1 * rng.standard_normal(11),
                         [1.48], 1 + 0.16 * rng.standard_normal(30)])
    stat, a, b = phy._run(cm)
    assert (int(a), int(b)) == (3, 14) and stat > 0
    assert phy._onset(cm, phy.START_REACH) == 3
