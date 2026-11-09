# entroptics-mesh/harness

An **independent** flight-emulation harness (hardware-in-the-loop testbed) for the entroptics stack.
It decides *where* each node is and *what role* it plays over time; the real entroptics-rf + mesh code
running on each node just experiences the resulting channel. The dependency points one way
(`harness → entroptics_mesh → entroptics_rf → entroptics`), so the stack never learns it is in a lab.
The harness is entroptics-agnostic — it only shapes signals and geometry.

## What it does
Phase 1, all-virtual (digital emulated air):
- `flight.py` — **flight guide** (programmed lead trajectory) + **self-arranging formation**
  (relays placed each step via `entroptics_mesh.placement.relay_chain`, so the lab formation and the
  mesh's own placement agree) + selectable **roles** (base / lead / relay / follower).
- `run_flyout.py` — `formation_sweep` (overview: shows the formation self-arranging + connectivity vs
  range, instant) and `delivery_snapshot` (the **real** stack at a fly-out geometry: lead → HOME,
  relays decode-and-forward, measured delivery).

Measured (Pluto @915 MHz, 20 dBm, 0 dBi): single hop ≈4.5 km; **3 relays hold the chain to ~12 km**,
then a gap opens — the range/relay tradeoff for that hardware.

## Run
```
PYTHONPATH=<mesh>/src;<rf>/src python run_flyout.py        # sweep + delivery snapshots
PYTHONPATH=<mesh>/src;<rf>/src python run_flyout.py sweep  # just the fast formation sweep
```

## Flight → real drones (MAVLink)
`mavlink.py` + `export_missions.py` turn a FlightGuide into loadable autopilot flight plans (QGC
`.waypoints` and `.plan`), one per node — the same scenario runs in sim/HIL and can be flown on real drones.
```
python export_missions.py        # writes missions/<NODE>.waypoints + .plan, verified
```

## Roadmap
- Larger fleets: more **virtual** nodes on the coordinator host beside the
  physical Pi-4Bs, which join through the duck-typed `Radio` IQ bridge (`bridge.py`, Phase 1b) —
  physical + virtual on one emulated air, exchangeable roles.
- **Doppler** in the channel (air.py models path loss + delay + centre, not velocity Doppler yet).
- Time-varying delivery (re-announce as nodes move) beyond the fixed-geometry snapshot.
- Phase 2: real-RF attenuator rig on the physical subset.

This is a lab/test component; it is not part of the shipping entroptics stack and imposes no
dependency on it.
