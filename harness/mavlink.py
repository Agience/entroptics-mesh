"""
MAVLink mission export -- the sim/HIL scenario -> real drone flight.

Turns a FlightGuide's per-node missions (flight.FlightGuide.missions) into loadable autopilot files,
dependency-free (no pymavlink needed to WRITE missions; it's just the standard text/JSON formats
QGroundControl / MAVProxy / ArduPilot / PX4 load):

  * QGC WPL 110  -- the universal waypoint text format (.waypoints / .txt).
  * QGC .plan    -- QGroundControl's JSON plan (fileType "Plan", version 1).

MAV frames/commands used: NAV_WAYPOINT=16, NAV_TAKEOFF=22, NAV_RETURN_TO_LAUNCH=20;
frame GLOBAL_RELATIVE_ALT=3 (alt above home). A moving node (lead/relay/follower) gets TAKEOFF -> its
position waypoints -> RTL; a stationary base gets a single waypoint (it is the home/ground station).
Actually commanding the drones later needs pymavlink; this module only emits the mission files.
"""
from __future__ import annotations

import json

NAV_WAYPOINT, NAV_TAKEOFF, NAV_RTL = 16, 22, 20
FRAME_GLOBAL, FRAME_REL_ALT = 0, 3


def _rows(waypoints, role):
    """(frame, command, p1, p2, p3, p4, lat, lon, alt) rows for one vehicle, home first."""
    home = waypoints[0]
    rows = [(FRAME_GLOBAL, NAV_WAYPOINT, 0, 0, 0, 0, home[0], home[1], home[2])]   # seq 0 = home
    moving = role != "base" and len(waypoints) > 1
    if moving:
        rows.append((FRAME_REL_ALT, NAV_TAKEOFF, 0, 0, 0, 0, home[0], home[1], home[2]))
    for lat, lon, alt in waypoints[1:]:                         # home is already seq 0
        rows.append((FRAME_REL_ALT, NAV_WAYPOINT, 0, 0, 0, 0, lat, lon, alt))
    if moving:
        rows.append((FRAME_REL_ALT, NAV_RTL, 0, 0, 0, 0, 0.0, 0.0, 0.0))
    return rows


def to_wpl(waypoints, role: str = "lead") -> str:
    """QGC WPL 110 text for one vehicle."""
    lines = ["QGC WPL 110"]
    for i, (frame, cmd, p1, p2, p3, p4, lat, lon, alt) in enumerate(_rows(waypoints, role)):
        cur = 1 if i == 0 else 0
        lines.append(f"{i}\t{cur}\t{frame}\t{cmd}\t{p1:.6f}\t{p2:.6f}\t{p3:.6f}\t{p4:.6f}\t"
                     f"{lat:.9f}\t{lon:.9f}\t{alt:.6f}\t1")
    return "\n".join(lines) + "\n"


def to_qgc_plan(waypoints, role: str = "lead") -> str:
    """QGC .plan JSON for one vehicle (fileType 'Plan', version 1)."""
    home = waypoints[0]
    items = []
    for i, (frame, cmd, p1, p2, p3, p4, lat, lon, alt) in enumerate(_rows(waypoints, role)):
        if i == 0:
            continue                                            # home goes in plannedHomePosition
        items.append({"type": "SimpleItem", "command": cmd, "frame": frame, "autoContinue": True,
                      "doJumpId": i, "params": [p1, p2, p3, p4, lat, lon, alt]})
    return json.dumps({
        "fileType": "Plan", "version": 1,
        "groundStation": "entroptics-mesh/harness",
        "mission": {"version": 2, "firmwareType": 12, "vehicleType": 2, "cruiseSpeed": 15,
                    "hoverSpeed": 5, "plannedHomePosition": [home[0], home[1], home[2]],
                    "items": items},
        "geoFence": {"circles": [], "polygons": [], "version": 2},
        "rallyPoints": {"points": [], "version": 2},
    }, indent=2)


def write_missions(missions: dict, out_dir: str, fmt: str = "wpl") -> list[str]:
    """Write one file per node. fmt 'wpl' (.waypoints) or 'plan' (.plan). Returns the paths."""
    import os
    os.makedirs(out_dir, exist_ok=True)
    paths = []
    for name, m in missions.items():
        wps, role = m["waypoints"], m["role"]
        if fmt == "plan":
            text, ext = to_qgc_plan(wps, role), "plan"
        else:
            text, ext = to_wpl(wps, role), "waypoints"
        path = os.path.join(out_dir, f"{name}.{ext}")
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
        paths.append(path)
    return paths


def parse_wpl(text: str):
    """Round-trip reader (validation): returns the (lat, lon, alt) of NAV_WAYPOINT rows."""
    out = []
    for line in text.splitlines()[1:]:
        c = line.split("\t")
        if len(c) >= 12 and int(c[3]) == NAV_WAYPOINT:
            out.append((float(c[8]), float(c[9]), float(c[10])))
    return out


__all__ = ["to_wpl", "to_qgc_plan", "write_missions", "parse_wpl"]
