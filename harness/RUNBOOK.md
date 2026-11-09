# entroptics lab — runbook

How to stand up and run the entroptics RF-mesh hardware-in-the-loop (HIL) lab, and the results it
measured. The harness is **independent** of the entroptics stack (it drives nodes, never
edits the codec); dependency points one way: `harness → entroptics_mesh → entroptics_rf → entroptics`.

## 1. What the lab is

Stationary nodes run the **real** entroptics-rf + mesh stack; an independent emulation harness manages
the signals between them to replicate a flight. One scenario runs across three stages:

- **sim** — all nodes virtual, in one process (fast iteration).
- **HIL** — physical Pi-4B nodes join the same emulated air over TCP (real stack + real decode on real
  hardware); the air server runs on the coordinator host; larger fleets
  are filled with virtual nodes.
- **flight** — the same flight guide exports MAVLink flight plans for real drones.

Channel today is free-space + propagation delay (per-burst centre handles Doppler, which is negligible
— §5). Terrain/horizon and an attenuator RF rig are Phase-2 extensions.

## 2. Hardware

- 5× Pi-4B (fleet nodes) + a coordinator host (air server).
- 6× ADALM-Pluto @915 MHz (5 fleet + 1 coordinator) — only needed for the Phase-2 RF rig; the digital HIL
  needs no radio.
- Antennas: ~3 dBi omni on drones/relays (RHCP for real flight), ~8–10 dBi directional panel
  at the base. Attenuator rig (Phase 2): ~5× Vaunix/Mini-Circuits USB step attenuators + combiners.

## 3. Software layout

`entroptics-mesh/harness/`:
- `flight.py` — flight guide: `LeadPath` + self-arranging `Formation` (via `placement.relay_chain`) +
  `FlightGuide` (positions/roles over time) + `missions()` (MAVLink export source).
- `bridge.py` — distributed NodeRun: `LocalEndpoint` (virtual) / `RemoteEndpoint` (physical, TCP) +
  `DistributedAirRun`; `serve`/`serve_listening`/`connect`.
- `air_server.py` — the fleet runner (mix physical + virtual on one server-driven air).
- `pi_node.py` — deployable per-Pi node server.
- `mavlink.py` + `export_missions.py` — flight-plan export (QGC `.waypoints` / `.plan`).
- analyses: `run_flyout.py`, `link_budget_sweep.py`, `fly_sweep.py`, `multiplex_flight.py`,
  `multiplex_pack.py`, `doppler_check.py`, `validate_bridge.py`.

PYTHONPATH for every run: `entroptics-mesh/src`, `entroptics-rf/src` (+ `entroptics-mesh/harness` for the
harness modules). Env: `OPENBLAS_NUM_THREADS=1 PYTHONHASHSEED=0`; `ENTROPTICS_FAST=1` if numba present.

## 4. Run it

All from `entroptics-mesh/harness/` with the PYTHONPATH above:
```
python run_flyout.py sweep        # formation self-arranges + connectivity vs range (instant)
python link_budget_sweep.py       # fleet sizing: raw/planned hop, relays-for-25km, per power×gain×rate
python fly_sweep.py               # real-PHY delivery vs range, direct vs 3-relay (slow)
python multiplex_pack.py          # N concurrent FPV streams in 2.4 MHz (2/3/4 all deliver)
python doppler_check.py           # flight-Doppler robustness (negligible)
python validate_bridge.py         # bridge parity: socket-remote == in-process (socketpair + TCP)
python export_missions.py         # MAVLink flight plans -> missions/<NODE>.waypoints + .plan
python air_server.py [km] <pi_host:port>       # the full mixed fleet (physical Pi + virtual), server-driven
```

### HIL bring-up (physical Pi + air server on the coordinator host)
Per physical Pi (bundle at `~/entroptics-bench` with mesh+rf src + a venv):
```
ssh pi@<pi-host>
cd ~/entroptics-bench && PYTHONPATH=entroptics-mesh/src:entroptics-rf/src \
  venv/bin/python -u pi_node.py --role lead --id LEAD0000 --port 52001
```
On the coordinator host (current src + harness synced to a working directory, `<lab-dir>`):
```
ssh <user>@<coordinator-host>
cd <lab-dir> && PYTHONPATH=entroptics-mesh/src:entroptics-rf/src:entroptics-mesh/harness \
  <venv>/bin/python -u entroptics-mesh/harness/air_server.py 48 <pi-host>:52001
```
`air_server.py [km] <pi_host:port>` (or `ENTROPTICS_PI=host:port`) runs a virtual base and relay
chain with the lead, `km` out from the base, on that physical Pi; its fleet spec lists which nodes are physical
`(host,port)` vs virtual + their roles — edit it to add Pis. Re-sync `<lab-dir>` after stack edits.
SSH note: launch `pi_node.py` as its own command with `& true` (bundling `pkill; launch` races).

## 5. Results measured (all entroptics-pure; sim/HIL, no RF transmit)

- **Formation** self-arranges (relays reposition as the lead flies out). Pluto 20 dBm, 0 dBi, 0.5 MS/s:
  single hop ≈4.5 km, 3 relays hold ~12 km; with ~3 dBi antennas 3 relays reach ~25 km.
- **Fleet sizing** (link budget): antennas are the cheap lever — ~3 dBi gets the 5-node/3-relay kit to
  the 25 km outbound flight; FPV video rate wants higher gain or a PA. No PAs otherwise needed.
- **Real-PHY delivery vs range**: direct carries to ~32 km (26 dBm EIRP ≈ 3 dBi both ends); relays
  extend it (delivered at 40 km via the relay chain).
- **Throughput / multiplexing**: 2/3/4 concurrent FPV streams on disjoint sub-bands all deliver from one
  capture; linear in #sub-bands; ceiling = total band ÷ per-stream width, then coordinator decode compute.
  (Co-channel key-stacking "MIMO" was a measured dead end; spatial MIMO deferred.)
- **Flight Doppler**: negligible 10–300 m/s (BER 0, carrier offset <0.025 cyc/row, read blind).
- **Bridge**: socket-remote node bit-identical to in-process, over socketpair AND TCP, both access
  modes (listen-before-talk + simple).
- **HIL on real hardware**: physical Pi-4B lead → virtual relay → virtual base, air server on the coordinator
  host, delivered over the LAN.
- **MAVLink**: 6-node flight-plan export round-trips (lead moves outward monotonically, relays between base & lead).
- **Real-time envelope** (from port_results.md): narrowband real-time today (~190 kHz Pi-4B / ~450 kHz
  Pi-5); wideband needs server/FPGA — the coordinator host is the throughput ceiling.

## 6. Next

- More physical Pis in the HIL run — pure replication: one `pi_node.py` each + extend the
  `air_server.py` fleet spec.
- Phase-2 RF rig: Plutos + programmable attenuators on the lead→relays→base chain.
- Larger virtual fleets / continuous-flight runs: a model-channel runtime (fast SINR→success receiver)
  to run the full mesh without real-PHY decode (not yet built).
- Flight guide → autopilot: wire `export_missions.py` output into QGC/ArduPilot; pymavlink to command.
