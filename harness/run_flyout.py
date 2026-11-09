"""
Fleet fly-out on the digital harness (Phase 1, all-virtual first).

Two things, deliberately split so the fast part is instant and the slow part is opt-in:
  * ``formation_sweep`` -- overview arithmetic (placement only): as the lead flies out, show the
    formation self-arranging (relay count/positions), whether the chain stays connected, and where it
    runs out of relays (the gap).  Instant.
  * ``delivery_snapshot`` -- the REAL entroptics stack at one geometry: base (HOME gateway) + relays +
    a lead FPV source; the lead sends to HOME, the mesh relays decode-and-forward, measure delivery.
    This is the integration check that the harness drives the real codec.  Slow (real-PHY decode).

Run:  PYTHONPATH=<mesh>/src;<rf>/src  python run_flyout.py            # sweep + one delivery snapshot
      ... python run_flyout.py sweep                                  # just the fast sweep
"""
from __future__ import annotations

import math
import sys

import numpy as np

from flight import FlightGuide, Formation, LeadPath, east_of

from entroptics_mesh import phy, placement
from entroptics_mesh.adapt import Radio
from entroptics_mesh.emu.air import Air, Radio as AirRadio
from entroptics_mesh.emu.node_run import NodeRun
from entroptics_mesh.neighbors import Position
from entroptics_mesh.node import Node
from entroptics_mesh.providers import NullCrypto, Providers
from entroptics_mesh.routing import HOME

# The fleet radio = an ADALM-Pluto at 915 MHz: 20 dBm (100 mW), 0 dBi, the lab's real device.
RADIO = Radio(min_rate=0.25e6, max_rate=2.4e6, center_hz=915e6, max_power_dbm=20.0,
              noise_dbm=-100.0, freq_hz=915e6)
BASE = Position(45.0, -75.0, 100.0)
PLAN_RATE = 0.5e6
SUB = [phy.Known(64, 0.5, 0.25), phy.Known(64, 0.5, -0.25)]


class KeyOnly(NullCrypto):
    name = "key-only"

    def waveform_key(self, context):
        return 0xA17C0DE


def formation_sweep(n_relays: int = 3, max_km: float = 40.0, step_km: float = 4.0) -> None:
    form = Formation(RADIO, rate=PLAN_RATE)
    guide = FlightGuide(BASE, LeadPath(BASE, speed_mps=25.0), form, n_relays=n_relays)
    hop = form.max_hop_m() / 1000.0
    print(f"radio: Pluto @915 MHz, {RADIO.max_power_dbm:.0f} dBm, plan rate {PLAN_RATE/1e6:.2f} MS/s")
    print(f"max single hop (pred, horizon-capped): {hop:.1f} km   relays available: {n_relays}\n")
    print(f"{'t(s)':>5} {'range(km)':>9} {'slots':>5} {'relays':>6} {'gap':>3} {'connected':>9}  relay x (km)")
    for km in np.arange(0.0, max_km + 1e-9, step_km):
        t = km * 1000.0 / guide.lead.speed_mps
        st = guide.state(t)
        meta = st["_meta"]
        pos = {k.encode(): v["pos"] for k, v in st.items() if k != "_meta"}
        usable = placement.links(pos, {}, RADIO, PLAN_RATE)
        connected = b"LEAD" in placement.reachable(usable, [b"BASE"])
        xs = [f"{p.distance_m(BASE)/1000:.1f}" for k, p in
              sorted(((k, v["pos"]) for k, v in st.items() if k.startswith("RLY")))]
        print(f"{t:5.0f} {km:9.1f} {meta['slots_wanted']:5d} {min(n_relays, meta['slots_wanted']):6d} "
              f"{meta['gap']:3d} {str(connected):>9}  {' '.join(xs)}")


def _mesh_at(positions: dict[bytes, Position], seed: int = 1):
    """Build base (HOME gateway) + the given relay/lead nodes at fixed positions (one fly-out snapshot)."""
    air = Air(freq_hz=915e6, rate=2.4e6, seed=seed)
    nodes, ids = [], list(positions)
    for i, nid in enumerate(ids):
        p = positions[nid]
        # air radio at the node's metric position (east/north from BASE), 20 dBm
        east = (p.lon - BASE.lon) * math.pi / 180 * 6_371_000.0 * math.cos(math.radians(BASE.lat))
        air.add(AirRadio(position=np.array([east, 0.0, p.alt]), tx_power_dbm=20.0, noise_dbm=-100.0))
        n = Node(nid, RADIO, slot_s=0.0, providers=Providers(crypto=KeyOnly()), seed=100 + i,
                 position=p, blind_scaled=False, accepts=({HOME} if nid == b"BASE0000" else frozenset()))
        nodes.append(n)
    for a in nodes:
        for b in nodes:
            if a is not b:
                a.on_announcement(b.announcement(0.0), b.node_id, 0.0)
        for g in SUB:
            a.learn(g)
    return air, nodes, ids


def delivery_snapshot(km: float, n_relays: int = 3, seed: int = 1) -> dict:
    """Real stack at a fly-out snapshot: lead FPV -> HOME (base), relays decode-and-forward."""
    form = Formation(RADIO, rate=PLAN_RATE)
    lead = east_of(BASE, km * 1000.0)
    a = form.assign(BASE, lead, n_relays)
    positions = {b"BASE0000": BASE, b"LEAD0000": lead}
    for i, p in enumerate(a["relays"]):
        positions[b"RLY%05d" % i] = p
    air, nodes, ids = _mesh_at(positions, seed=seed)
    base = nodes[ids.index(b"BASE0000")]
    leadn = nodes[ids.index(b"LEAD0000")]
    msg = bytes(np.random.default_rng(seed).integers(0, 256, 300, dtype=np.uint8))
    leadn.send(msg, HOME, 0.0)
    run = NodeRun(air, nodes)
    run.run(120, until=lambda: bool(base.inbox))
    delivered = [m.payload for m in base.inbox] == [msg]
    return {"km": km, "relays_placed": len(a["relays"]), "gap": a["gap"],
            "delivered": delivered, "bursts": run.bursts}


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "all"
    print("=== formation sweep (overview, placement only) ===")
    formation_sweep()
    if mode != "sweep":
        print("\n=== delivery snapshot (real entroptics stack) ===")
        for km in (6.0, 12.0):
            r = delivery_snapshot(km)
            print(f"range {r['km']:.0f} km: relays placed {r['relays_placed']} gap {r['gap']} "
                  f"-> delivered {r['delivered']} in {r['bursts']} bursts")
