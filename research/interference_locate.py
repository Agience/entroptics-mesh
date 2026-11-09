"""Interference-band report: a node measures interference, announces it, and a peer steers its
transmission off that band. Uses the real announce/neighbors/adapt code.

Run:  PYTHONPATH=src python research/interference_locate.py
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))
import numpy as np

from entroptics_mesh import announce
from entroptics_mesh.adapt import Radio, default_plan
from entroptics_mesh.neighbors import Announced, BandRange, NeighborTable
from entroptics_mesh.providers import BandObservation, Intent
from interference_provider import ThresholdInterference

# ── avoidance: a node MEASURES interference, reports it, a peer steers off it ────────────────
print("== measure interference -> report -> peer steers off the interference band ==")
dest = b"DEST0001"
# the destination's own InterferenceProvider measures high interference on captures centred +0.3 MHz
# within its 0.6 MHz band, and clean elsewhere; it reports the hot band in its announcement.
detector = ThresholdInterference(threshold=10.0, width_hz=0.2e6)
for t in range(5):
    detector.observe(BandObservation(t=float(t), occupancy=0.5, center=+0.3e6, interference=18.0))
    detector.observe(BandObservation(t=float(t), occupancy=0.5, center=-0.6e6, interference=3.0))
bands = detector.interference_bands()
print(f"   provider measured interference: {[(round(j.offset_hz/1e3), round(j.width_hz/1e3), round(j.level_db,1)) for j in bands]} (kHz offset, kHz width, dB)")
blob = announce.pack(dest, band=BandRange(0.3e6, 0.6e6, 915e6), interference=bands)
_, a = announce.unpack(blob, t=0.0)                       # a peer receives + opens the announcement
radio = Radio(min_rate=0.3e6, max_rate=2.4e6, center_hz=915e6, max_power_dbm=20.0)
table = NeighborTable(); table.announced(dest, a)
# resolve the destination's interference band (relative to ITS centre) into our centre-offset space:
dc = a.band.center_hz
interference = [((dc + j.offset_hz - j.width_hz / 2) - radio.center_hz,
           (dc + j.offset_hz + j.width_hz / 2) - radio.center_hz) for j in a.interference]
chosen = [default_plan(Intent(to=dest, n_bytes=512), table, radio,
                       rng=np.random.default_rng(s), interference=interference)[0].center for s in range(300)]
band_lo, band_hi = interference[0]
hits = sum(1 for c in chosen if c - 0.3e6 < band_hi and c + 0.3e6 > band_lo)   # 0.6MHz sub-band
print(f"   interference band (our offset space): [{band_lo/1e6:+.2f}, {band_hi/1e6:+.2f}] MHz")
print(f"   300 plans: sub-bands used {sorted({round(c/1e3) for c in chosen})} kHz; "
      f"landed in the interference band {hits} times")
print(f"   => peer routes around the destination's local interferer: {'PASS' if hits == 0 else 'FAIL'}")
