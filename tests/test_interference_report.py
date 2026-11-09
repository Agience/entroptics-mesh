"""Interference-band report: announced (relative, optional bearing/level), used as a clue to
steer a transmission off a destination's band with local interference. Null provider = byte-identical."""
import numpy as np

from entroptics_mesh import announce
from entroptics_mesh.adapt import Radio, default_plan
from entroptics_mesh.neighbors import Announced, BandRange, InterferenceBand, NeighborTable
from entroptics_mesh.providers import Intent, NullInterference

NID = b"NODE0001"


def test_announce_interference_roundtrip():
    bands = [InterferenceBand(offset_hz=300_000.0, width_hz=200_000.0, bearing_deg=42.5, level_db=-61.0),
           InterferenceBand(offset_hz=-500_000.0, width_hz=100_000.0)]            # bearing/level unknown
    blob = announce.pack(NID, band=BandRange(1e6, 2.4e6, 915e6), interference=bands)
    nid, a = announce.unpack(blob, t=1.0)
    assert nid == NID
    assert len(a.interference) == 2
    j0, j1 = a.interference
    assert j0.offset_hz == 300_000.0 and j0.width_hz == 200_000.0
    assert abs(j0.bearing_deg - 42.5) < 1e-3 and abs(j0.level_db - (-61.0)) < 1e-3
    assert j1.offset_hz == -500_000.0 and j1.bearing_deg is None and j1.level_db is None


def test_no_interference_is_byte_identical():
    """No interference bands (the null-provider default) packs exactly as an announcement without the field."""
    band = BandRange(1e6, 2.4e6, 915e6)
    assert announce.pack(NID, band=band) == announce.pack(NID, band=band, interference=[])
    assert announce.pack(NID, band=band) == announce.pack(NID, band=band, interference=None)


def test_null_interference_reports_no_bands():
    assert NullInterference().interference_bands() == []


def test_default_plan_steers_off_an_interfered_subband():
    """With the destination reporting interference on one sub-band, the planner never lands a burst there (but is
    not cornered if every sub-band were interference)."""
    radio = Radio(min_rate=0.3e6, max_rate=2.4e6, center_hz=915e6, max_power_dbm=20.0)
    dest = b"DEST0001"
    table = NeighborTable()
    # destination captures only 0.6 MHz -> 4 sub-bands of our 2.4 MHz; shout branch (no position)
    table.announced(dest, Announced(t=0.0, band=BandRange(0.3e6, 0.6e6, 915e6)))
    intent = Intent(to=dest, n_bytes=512)
    interference = [(0.0, 0.6e6)]            # our-offset space: overlaps exactly the +0.3 MHz sub-band
    seen = set()
    for s in range(200):
        plan, _ = default_plan(intent, table, radio, rng=np.random.default_rng(s), interference=interference)
        seen.add(round(plan.center / 1e3))
        assert not (plan.center - plan.rate / 2 < 0.6e6 and plan.center + plan.rate / 2 > 0.0), \
            f"burst at center {plan.center} overlaps the interference [0,0.6MHz]"
    assert len(seen) > 1, "planner should still spread across the clear sub-bands"


def test_default_plan_not_cornered_when_all_interfered():
    """If every sub-band has interference, the planner still returns a plan (degraded beats silent)."""
    radio = Radio(min_rate=0.3e6, max_rate=2.4e6, center_hz=915e6, max_power_dbm=20.0)
    dest = b"DEST0001"
    table = NeighborTable()
    table.announced(dest, Announced(t=0.0, band=BandRange(0.3e6, 0.6e6, 915e6)))
    intent = Intent(to=dest, n_bytes=512)
    interference = [(-2.4e6, 2.4e6)]         # the whole band
    plan, _ = default_plan(intent, table, radio, rng=np.random.default_rng(0), interference=interference)
    assert plan.rate > 0 and np.isfinite(plan.center)
