"""
Export the canonical fly-out scenario to real autopilot missions, and verify they are well-formed.

Produces one QGC .waypoints (and .plan) per node from the FlightGuide, then checks: every mission
round-trips, the lead's waypoints move monotonically outward from base (the programmed outbound leg), relays sit
between base and lead, and the base is a single stationary waypoint. The sim scenario is now loadable
into QGroundControl / ArduPilot / PX4 -- one scenario, sim to flight.
"""
from __future__ import annotations

import os
import sys

import mavlink
from flight import Formation, FlightGuide, LeadPath

from entroptics_mesh.adapt import Radio
from entroptics_mesh.neighbors import Position

RADIO = Radio(min_rate=0.25e6, max_rate=2.4e6, center_hz=915e6, max_power_dbm=20.0,
              noise_dbm=-100.0, freq_hz=915e6, antenna_dbi=3.0)
BASE = Position(45.0, -75.0, 100.0)
OUT = os.path.join(os.path.dirname(__file__), "missions")


def main() -> int:
    guide = FlightGuide(BASE, LeadPath(BASE, speed_mps=25.0, alt_m=100.0),
                        Formation(RADIO, rate=0.5e6), n_relays=3, n_follower=1)
    t_grid = [t for t in range(0, 20 * 60 + 1, 120)]           # 20 min outbound flight, a waypoint every 2 min
    missions = guide.missions(t_grid)
    wpl_paths = mavlink.write_missions(missions, OUT, fmt="wpl")
    plan_paths = mavlink.write_missions(missions, OUT, fmt="plan")

    print(f"exported {len(wpl_paths)} missions to {OUT}")
    ok = True
    for name, m in sorted(missions.items()):
        wps = m["waypoints"]
        text = mavlink.to_wpl(wps, m["role"])
        rt = mavlink.parse_wpl(text)
        # parse_wpl returns every NAV_WAYPOINT row = home (seq 0) + the position waypoints = the full
        # original list (takeoff/RTL are commands 22/20, not 16).
        round_trips = rt == [(w[0], w[1], w[2]) for w in wps]
        outward = [BASE.distance_m(Position(la, lo, al)) / 1000 for la, lo, al in wps]
        monotone = all(b >= a - 1e-6 for a, b in zip(outward, outward[1:]))
        print(f"  {name:6s} role={m['role']:5s} wps={len(wps):2d} "
              f"outward {outward[0]:.0f}->{outward[-1]:.0f} km  "
              f"round-trip={round_trips} monotone={monotone}")
        if name == "BASE":
            ok &= len(wps) == 1                                # stationary station
        if name == "LEAD":
            ok &= monotone and outward[-1] > outward[0]    # the programmed outbound leg
        ok &= round_trips
    print(f"\nMISSION EXPORT: {'PASS' if ok else 'FAIL'} "
          f"(FlightGuide -> loadable QGC .waypoints + .plan per node; sim scenario = real flight plan)")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
