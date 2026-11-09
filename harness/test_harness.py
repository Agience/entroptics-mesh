"""
Regression guard for the lab harness. The harness depends on the entroptics stack
(entroptics_mesh/_rf); as the stack evolves these catch API/behaviour drift before a field-test run
relies on it. Fast lane: geometry/arithmetic (no decode) + one decode path through the bridge.

Run:  PYTHONPATH=.;<mesh>/src;<rf>/src  python -m pytest test_harness.py -q
"""
from __future__ import annotations

import math

import pytest

from flight import Formation, FlightGuide, LeadPath, east_of
import mavlink
import link_budget_sweep as lb

from entroptics_mesh.neighbors import Position

BASE = Position(45.0, -75.0, 100.0)
RADIO = lb.radio(20.0, 3.0)


# ── flight guide / self-arranging formation (geometry; no decode) ──────────────────────────────

def test_formation_places_relays_between_base_and_lead():
    form = Formation(RADIO, rate=0.5e6)
    lead = east_of(BASE, 24_000.0)
    a = form.assign(BASE, lead, 3)
    xs = [p.distance_m(BASE) for p in a["relays"]]
    assert a["relays"], "relays should be placed when the lead is beyond one hop"
    assert all(0 < x < 24_000.0 for x in xs), "relays sit between base and lead"
    assert xs == sorted(xs), "relays ordered base->lead"


def test_formation_grows_slots_with_range():
    form = Formation(RADIO, rate=0.5e6)
    near = form.assign(BASE, east_of(BASE, 8_000.0), 5)["slots_wanted"]
    far = form.assign(BASE, east_of(BASE, 40_000.0), 5)["slots_wanted"]
    assert far > near, "a longer chain demands more relay slots"


def test_flight_guide_missions_lead_moves_outward():
    guide = FlightGuide(BASE, LeadPath(BASE, speed_mps=25.0), Formation(RADIO, rate=0.5e6),
                        n_relays=3, n_follower=1)
    m = guide.missions(range(0, 20 * 60 + 1, 120))
    lead = m["LEAD"]["waypoints"]
    dr = [BASE.distance_m(Position(la, lo, al)) for la, lo, al in lead]
    assert dr[-1] > dr[0] and dr == sorted(dr), "lead moves monotonically outward from base"
    assert m["BASE"]["role"] == "base" and len(m["BASE"]["waypoints"]) == 1, "base is stationary"


# ── link budget (arithmetic) ───────────────────────────────────────────────────────────────────

def test_link_budget_monotonic_in_power_and_gain():
    hop = lambda p, g: lb.placement.max_hop_m(lb.radio(p, g), 0.5e6, 100.0, 100.0)
    assert hop(27.0, 0.0) > hop(20.0, 0.0), "more power -> longer hop"
    assert hop(20.0, 6.0) > hop(20.0, 0.0), "more antenna gain -> longer hop"
    assert lb.relays_for(25_000.0, hop(20.0, 3.0)) <= 3, "3 dBi reaches 25 km with <=3 relays"


# ── MAVLink export round-trip ────────────────────────────────────────────────────────────────

def test_mavlink_roundtrip_and_structure():
    wps = [(45.0, -75.0, 100.0), (45.0, -74.9, 100.0), (45.0, -74.8, 100.0)]
    text = mavlink.to_wpl(wps, "lead")
    assert text.splitlines()[0] == "QGC WPL 110"
    assert mavlink.parse_wpl(text) == wps, "waypoints round-trip through WPL"
    import json
    plan = json.loads(mavlink.to_qgc_plan(wps, "lead"))
    assert plan["fileType"] == "Plan" and plan["mission"]["plannedHomePosition"] == list(wps[0])


# ── one decode path through the bridge (catches phy/node drift) ────────────────────────────────

def test_bridge_delivers_over_socketpair():
    import validate_bridge as vb
    base_in, base_bursts = vb.local_baseline(listen=True)
    assert base_in == [vb.MSG], "all-local baseline delivers"
    inbox, bursts = vb.distributed("src", listen=True, transport="socketpair")
    assert inbox == base_in and bursts == base_bursts, "socket-remote node bit-identical to in-process"
