import sys, time, numpy as np
from entroptics_mesh import phy
from entroptics_mesh.emu import link_run as lr
from entroptics_mesh.emu.air import Air, Radio
from entroptics_mesh.link import symbols
from entroptics_mesh.routing import Header
msg = bytes(np.random.default_rng(0).integers(0, 256, 2400, dtype=np.uint8))
sym = symbols(Header(b"A"*8, 1, b"C"*8, 4), msg, lr.BLOCK, 1.0)[0]
iq = phy.transmit_burst(lr._frame(sym), lr.TxPlan(rate=1.0e6), 1234, sender=b"A"*8)
g = phy.Known(64, 1.0/2.4, 0.3e6/2.4e6)
caps = {}
for snr in (5.0, 6.0):
    for seed in range(30):
        air = Air(rate=2.4e6, seed=seed)
        a = air.add(Radio(position=np.array([0., 0, 100]), tx_power_dbm=20, noise_dbm=-100))
        b = air.add(Radio(position=np.array([1000., 0, 100]), tx_power_dbm=20, noise_dbm=-100))
        air.radios[b].noise_dbm = air.gain_db(a, b) - snr     # SNR over the receiver's 2.4 MHz
        air.send(a, (100 + 7 * seed) / 2.4e6, iq, rate=1.0e6, center=0.3e6)
        caps[(snr, seed)] = air.capture(b, 0.0, int(iq.size * 2.4 + 600))
for xt in (1e-9, 1e-6, 1e-4, 1e-2, 1e-1, 1.0):
    phy.SETTLE_FTOL = xt
    t = time.perf_counter(); ok = {5.0: 0, 6.0: 0}
    for (snr, seed), y in caps.items():
        got = lr._unframe(phy.receive_known(y, 1234, g).soft)
        ok[snr] += got is not None and got.data == sym.data
    print(f"ftol {xt:g}: ok@5dB {ok[5.0]}/30 ok@6dB {ok[6.0]}/30  {1e3*(time.perf_counter()-t)/len(caps):.0f} ms/burst", flush=True)
# The node's call at the adopted tolerance: the frame check handed in (accept=), so a burst that
# decodes as the settle left it is not refined further; the CRC picks family, alphabet and rate.
from entroptics_mesh.link import read_frame
phy.SETTLE_FTOL = 1e-2
t = time.perf_counter(); ok = {5.0: 0, 6.0: 0}
for (snr, seed), y in caps.items():
    got = read_frame(phy.receive_known(y, 1234, g, accept=lambda r: read_frame(r) is not None))
    ok[snr] += got is not None and got[0].data == sym.data
print(f"node path (accept=, read_frame) ftol 0.01: ok@5dB {ok[5.0]}/30 ok@6dB {ok[6.0]}/30  "
      f"{1e3*(time.perf_counter()-t)/len(caps):.0f} ms/burst (receive + frame decode)", flush=True)
