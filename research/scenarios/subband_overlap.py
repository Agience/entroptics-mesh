"""Two senders in one slot: which sub-band layouts survive?  1.2 Msps senders into a 2.4 Msps
receiver 9 km away, both 2 km apart, same slot, known geometries, frame CRC as the arbiter.
Layouts: disjoint (-0.6/+0.6 MHz), half-overlap (0/+0.6 MHz), same band (+0.6/+0.6 MHz).

Usage: PYTHONPATH=src python research/scenarios/subband_overlap.py [seeds]  ->  subband_overlap.txt"""
import sys
import numpy as np
from entroptics_mesh import link, phy
from entroptics_mesh.emu.air import Air, Radio
from entroptics_mesh.link import symbols
from entroptics_mesh.providers import TxPlan
from entroptics_mesh.routing import Header

seeds = int(sys.argv[1]) if len(sys.argv) > 1 else 6
acc = lambda v: bool(v.size) and link.unframe(v) is not None
rnd = lambda s: bytes(np.random.default_rng(s).integers(0, 256, 300, dtype=np.uint8))
sa = symbols(Header(b"A" * 8, 1, b"C" * 8, 4), rnd(1), link.BLOCK, 1.0)[0]
sb = symbols(Header(b"B" * 8, 1, b"C" * 8, 4), rnd(2), link.BLOCK, 1.0)[0]
RX, TXR = 2.4e6, 1.2e6
for ca, cb, label in ((-0.6e6, 0.6e6, "disjoint"), (0.0, 0.6e6, "half-overlap"), (0.6e6, 0.6e6, "same band")):
    ok = {"A": 0, "B": 0}
    for seed in range(seeds):
        air = Air(rate=RX, seed=seed)
        A = air.add(Radio(position=np.array([0., 1000, 100]), tx_power_dbm=20, noise_dbm=-100))
        B = air.add(Radio(position=np.array([0., -1000, 100]), tx_power_dbm=20, noise_dbm=-100))
        C = air.add(Radio(position=np.array([9000., 0, 100]), tx_power_dbm=20, noise_dbm=-100))
        ia = phy.transmit_burst(link.frame(sa), TxPlan(rate=TXR), 1234, sender=b"A" * 8)
        ib = phy.transmit_burst(link.frame(sb), TxPlan(rate=TXR), 1234, sender=b"B" * 8)
        air.send(A, (100 + 7 * seed) / RX, ia, rate=TXR, center=ca)
        air.send(B, (130 + 5 * seed) / RX, ib, rate=TXR, center=cb)
        y = air.capture(C, 0.0, int(max(ia.size, ib.size) * RX / TXR + 900))
        for s, c, nm in ((sa, ca, "A"), (sb, cb, "B")):
            rx = phy.receive_known(y, 1234, phy.Known(64, TXR / RX, c / RX), accept=acc)
            got = link.unframe(rx.soft) if rx.soft.size else None
            ok[nm] += got is not None and got.data == s.data
    print(f"{label:12s}: A {ok['A']}/{seeds}  B {ok['B']}/{seeds}", flush=True)
