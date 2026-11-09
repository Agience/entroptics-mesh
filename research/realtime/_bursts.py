"""Representative bursts and captures for the real-time profiling scripts.

Self-contained: builds a real mesh frame, composes it with the rf codec, and sends it over the
mesh emulator's band-limited air channel (``emu.air.Air`` -- the same path that produced the
187 ms/burst node figure in research/mcs/kL.md and research/scenarios/settle_tolerance.txt) at
matched (2.4->2.4 Msps) and 1.0->2.4 Msps, with noise set to a representative in-band SNR.  No
hardware -- just the inputs ``phy.receive_known`` reads, built the way a node builds them.

Nothing here is edited into the codec; it only exercises it.  Run with
``PYTHONPATH=src;<entroptics-rf>/src``.
"""
from __future__ import annotations

import numpy as np

from entroptics_mesh import phy               # noqa: E402
from entroptics_mesh.link import frame, symbols  # noqa: E402
from entroptics_mesh.routing import Header     # noqa: E402
from entroptics_mesh.providers import TxPlan   # noqa: E402
from entroptics_mesh.emu.air import Air, Radio  # noqa: E402

KEY = 1234
BLOCK = 475            # link block bytes (emu default)


def _coded_bits(plan: TxPlan, n_rows_target: int) -> np.ndarray:
    """A real protected frame's coded bits, long enough to fill about ``n_rows_target`` rows."""
    per_row = plan.K * int(round(np.log2(plan.M)))
    want_bits = n_rows_target * per_row
    msg = bytes(np.random.default_rng(0).integers(0, 256, max(64, want_bits // 8), dtype=np.uint8))
    sym = symbols(Header(b"A" * 8, 1, b"C" * 8, 4), msg, BLOCK, 1.0)[0]
    return frame(sym, plan.code_rate)


def make_burst(family="differential", M=4, code_rate="1/2", rate_hz=1.0e6, n_rows=120):
    plan = TxPlan(rate=rate_hz, M=M, family=family, code_rate=code_rate)
    bits = _coded_bits(plan, n_rows)
    sender = b"A" * 8 if family == "differential" else None
    iq = phy.transmit_burst(bits, plan, KEY, sender=sender)
    return iq, plan


def capture(iq, plan, rx_rate=2.4e6, snr_db=None, delay=90.0, tail_rows=3, seed=0, center_hz=0.0):
    """One capture of the burst over the emulator air channel at ``rx_rate``.  ``rho`` =
    tx_rate / rx_rate (matched = 1.0).  The burst starts ``delay`` received samples in and the
    capture runs ``tail_rows`` sender rows past it (the mesh slot holds a little silence after the
    burst).  ``snr_db`` is in-band SNR over the sender's width; None = noiseless."""
    air = Air(rate=rx_rate, seed=seed)
    a = air.add(Radio(position=np.array([0., 0, 100]), tx_power_dbm=20, noise_dbm=-100))
    b = air.add(Radio(position=np.array([1000., 0, 100]), tx_power_dbm=20, noise_dbm=-100))
    if snr_db is None:
        air.radios[b].noise_dbm = -400.0                 # effectively noiseless
    else:                                                # same convention as settle_tolerance.py
        air.radios[b].noise_dbm = air.gain_db(a, b) - float(snr_db)
    air.send(a, delay / rx_rate, iq, rate=plan.rate, center=center_hz)
    n = int(iq.size / (plan.rate / rx_rate) + delay + tail_rows * plan.L / (plan.rate / rx_rate))
    y = air.capture(b, 0.0, n)
    known = phy.Known(plan.L, plan.rate / rx_rate, center_hz / rx_rate)
    return y, known


# The representative cells the scripts profile.  (label, kwargs for make_burst, rx_rate, snr_db)
CELLS = {
    "dqpsk_1p0_2p4":   (dict(family="differential", M=4, code_rate="1/2", rate_hz=1.0e6), 2.4e6, 8.0),
    "dqpsk_matched":   (dict(family="differential", M=4, code_rate="1/2", rate_hz=2.4e6), 2.4e6, 8.0),
    "qam256_1p0_2p4":  (dict(family="coherent", M=256, code_rate="3/4", rate_hz=1.0e6), 2.4e6, 32.0),
    "qam256_matched":  (dict(family="coherent", M=256, code_rate="3/4", rate_hz=2.4e6), 2.4e6, 29.0),
    "qam16_1p0_2p4":   (dict(family="coherent", M=16, code_rate="1/2", rate_hz=1.0e6), 2.4e6, 12.0),
}


def build(label, center_hz=0.3e6, seed=0):
    bkw, rx_rate, snr = CELLS[label]
    iq, plan = make_burst(**bkw)
    y, known = capture(iq, plan, rx_rate=rx_rate, snr_db=snr, seed=seed, center_hz=center_hz)
    return y, known, plan


if __name__ == "__main__":
    for label in CELLS:
        y, known, plan = build(label)
        print(f"{label:18s} iq_rows~{plan.L} cap={y.size} rho={known.scale:.4f} "
              f"L={known.L} M={plan.M} {plan.family}")
