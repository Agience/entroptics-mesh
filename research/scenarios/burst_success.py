"""Probability that a single burst is found blind AND its FEC frame decodes, vs SNR, for the emu's
frames (64 data rows, r=1/2 code).  One SNR per process (run in parallel)."""
import sys, numpy as np
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
from entroptics_mesh.emu.air import Air, Radio, fspl_db
from entroptics_mesh.emu import net
from entroptics_mesh import phy
from entroptics_mesh.routing import Header, BROADCAST
from entroptics_mesh.providers import TxPlan
snr, trials = float(sys.argv[1]), int(sys.argv[2]) if len(sys.argv) > 2 else 100
plan = TxPlan(rate=2e6)
found = framed = 0
for t in range(trials):
    air = Air(seed=1000 + t)
    a = air.add(Radio(position=np.array([0., 0, 0]), tx_power_dbm=20, noise_dbm=-100))
    b = air.add(Radio(position=np.array([1000., 0, 0]), tx_power_dbm=20, noise_dbm=-100))
    air.radios[b].noise_dbm = air.gain_db(a, b) - snr               # set the SNR exactly
    iq = phy.transmit_burst(net.frame_bits(Header(b"A" * 8, t + 1, BROADCAST, 4), b"telemetry", plan, t), plan, 1234, sender=b"A" * 8)
    air.send(a, 64 / 2e6, iq)
    rx = phy.receive_burst(air.capture(b, 0.0, (64 + 1) * 64 + 128), 1234, candidates=range(60, 69))
    if rx.locked and rx.discovery.L == 64:
        found += 1
        if rx.soft.size and net.unframe_soft(rx.soft) is not None:
            framed += 1
print(f"snr {snr:5.1f} dB: geometry found {found}/{trials}, frame decoded {framed}/{trials}", flush=True)
