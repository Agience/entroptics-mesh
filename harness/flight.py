"""
Flight-emulation harness -- flight guide + self-arranging formation.

INDEPENDENT of the entroptics stack: the harness decides WHERE each node is and WHAT role it has over
time; the entroptics-rf + mesh code running on each node just experiences the resulting channel and
decides everything comms-side on its own.  The dependency points one way (harness -> stack), so the
stack never learns it is in a lab.

The formation reuses ``entroptics_mesh.placement`` -- the exact relay-placement rule the mesh already
measured (hold every hop at >= HOP_SNR_DB + margin, parallel relays per slot, horizon-capped) -- as
the per-step rule, so the lab's formation logic and the mesh's own placement agree by construction.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

from entroptics_mesh import placement
from entroptics_mesh.adapt import Radio
from entroptics_mesh.neighbors import Position

EARTH_M = 6_371_000.0

# Roles are selectable / exchangeable per node (physical Pi or virtual on the coordinator).
ROLES = ("base", "lead", "relay", "follower")


def east_of(base: Position, east_m: float, alt: float | None = None) -> Position:
    """A point ``east_m`` metres east of ``base`` (local flat-earth, fine at the hop scale)."""
    lon = base.lon + math.degrees(east_m / (EARTH_M * math.cos(math.radians(base.lat))))
    return Position(base.lat, lon, base.alt if alt is None else alt)


@dataclass
class LeadPath:
    """The programmed lead trajectory -- the one thing a mission specifies; the formation follows it.
    M1: constant-speed outbound flight on a heading.  (Waypoints / MAVLink missions are a later format.)"""
    base: Position
    speed_mps: float = 25.0
    alt_m: float = 100.0

    def at(self, t: float) -> Position:
        return east_of(self.base, self.speed_mps * float(t), self.alt_m)

    def range_m(self, t: float) -> float:
        return self.base.distance_m(self.at(t))


@dataclass
class Formation:
    """Self-arranging relay formation: given base and the (moving) lead, place the relays to hold the
    chain.  Recomputed every step from ``placement.relay_chain`` -> as the lead flies out the slots
    multiply and shift, i.e. the formation self-arranges.  With fewer relays than slots the chain has
    a GAP (reported), which is exactly the "we have run out of relays for this range" signal."""
    radio: Radio
    rate: float = 0.5e6              # planning rate (lower -> longer hops -> fewer relays for a range)
    relay_alt: float = 100.0
    parallel: int = 1               # relays per slot (1 = a single chain; 2 = redundant per slot)

    def slots(self, base: Position, lead: Position) -> list[list[Position]]:
        return placement.relay_chain(base, lead, self.radio, self.rate,
                                     relay_alt=self.relay_alt, parallel=self.parallel)

    def assign(self, base: Position, lead: Position, n_relays: int) -> dict:
        """Assign ``n_relays`` to the slots nearest base first.  Returns the placed relay positions,
        the slot count wanted, the gap (unfilled slots), and spares (relays beyond the slots)."""
        flat = [p for slot in self.slots(base, lead) for p in slot]
        placed = flat[:n_relays]
        return {"relays": placed, "slots_wanted": len(flat),
                "gap": max(0, len(flat) - n_relays), "spare": max(0, n_relays - len(flat))}

    def max_hop_m(self) -> float:
        return placement.max_hop_m(self.radio, self.rate, self.relay_alt, self.relay_alt)


@dataclass
class FlightGuide:
    """Ties the lead path + formation + roles into node state over time.  ``state(t)`` is what the
    channel core consumes: for every node its position and role at time ``t``."""
    base: Position
    lead: LeadPath
    formation: Formation
    n_relays: int = 3
    n_follower: int = 0                 # extra drones flying with the lead (1-2 carry the payload gen)

    def state(self, t: float) -> dict[str, dict]:
        lead_pos = self.lead.at(t)
        a = self.formation.assign(self.base, lead_pos, self.n_relays)
        out = {"BASE": {"pos": self.base, "role": "base"},
               "LEAD": {"pos": lead_pos, "role": "lead"}}
        for i in range(self.n_follower):                                   # follower drones just behind the lead
            out[f"FOLW{i}"] = {"pos": east_of(lead_pos, -500.0 * (i + 1)), "role": "follower"}
        for i, p in enumerate(a["relays"]):
            out[f"RLY{i}"] = {"pos": p, "role": "relay"}
        out["_meta"] = {"range_m": self.base.distance_m(lead_pos), "gap": a["gap"],
                        "slots_wanted": a["slots_wanted"], "spare": a["spare"]}
        return out

    def missions(self, t_grid) -> dict[str, dict]:
        """Per-node flight mission from the scenario: each node's sequence of positions over ``t_grid``,
        consecutive duplicates collapsed. The base is stationary (one waypoint). This is the handoff to
        a real autopilot -- mavlink.to_wpl / to_qgc_plan turn each into a loadable mission (sim scenario
        -> real flight). ``role`` is carried so the exporter can set takeoff/RTL per vehicle."""
        seqs: dict[str, dict] = {}
        for t in t_grid:
            for name, s in self.state(t).items():
                if name == "_meta":
                    continue
                p = s["pos"]
                m = seqs.setdefault(name, {"role": s["role"], "waypoints": []})
                wp = (round(p.lat, 9), round(p.lon, 9), round(p.alt, 3))
                if not m["waypoints"] or m["waypoints"][-1] != wp:
                    m["waypoints"].append(wp)
        return seqs


__all__ = ["ROLES", "east_of", "LeadPath", "Formation", "FlightGuide"]
