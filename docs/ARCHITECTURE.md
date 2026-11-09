# entroptics RF mesh — system architecture

*Orientation for engineers and integrators new to the three-repo stack.*

## What this is

A **software** mesh network — a radio physical layer plus mesh logic — that runs on general-purpose
compute beside a software-defined radio front end. There is no entroptics-specific RF hardware: no
modem chip, no custom antenna, no amplifier. The signal is pulled out of the air by a keyed optical
aperture, not demodulated against pilots or a preamble. The whole stack is designed around four
refusals (below), which make it parameter-free, reply-free, noise-like in structure (no added on-air structure), and
pluggable.

## The three layers

```
entroptics  ──►  entroptics-rf  ──►  entroptics-mesh
 (core)           (PHY)              (mesh + IP)
```

- **`entroptics` (core, on PyPI, ≥ 0.2.10).** The mathematics: the keyed aperture / mode basis, the
  entropy and geometry reads, the least-squares screen. General-purpose, not RF-specific: it carries
  no RF logic, and the RF layers consume it as a dependency rather than forking it.
- **`entroptics-rf` (the physical layer).** Turns bits into a keyed-aperture waveform and pulls them
  back out of a capture **blind**. `waveform.compose`/`compose_qam` (differential M-PSK or coherent
  square M-QAM on keyed orthonormal modes, keyed per-row rotations and power, a reference row, no
  FFT/pilots/guard/cyclic-prefix); `discover` (blind acquisition — measures rate ratio, offset,
  carrier, width, alphabet with nothing pre-shared but an optional key); the constant-modulus
  geometry read and the joint/band least-squares reads; `radio` (Pluto/RTL-SDR drivers for the
  bench). See [entroptics-rf README.md](https://github.com/Agience/entroptics-rf/blob/main/README.md) and
  [entroptics-rf research/PAPER.md](https://github.com/Agience/entroptics-rf/blob/main/research/PAPER.md).
- **`entroptics-mesh` (mesh + IP).** Everything above one link — see the module list in
  [README.md](../README.md). Providers, neighbours/announce, reply-free routing, FEC + fountain, the
  fast known-geometry read wrapper, MCS adaptation, decode-and-forward relaying, the node runtime,
  placement, plausibility, and an IP tunnel (`tunnel` + `tcpproxy`).

## The end-to-end data path

**Transmit.** `Intent` (to / size / priority) → core fits an audience `TxPlan` (width, centre,
power, alphabet, code rate) → **`AgilityProvider.plan(…, ctx)`** may re-choose it (e.g. a keyed
hop) → **`InterferenceProvider.review`** may reshape or hold → payload sealed by the **`CryptoProvider`**
→ cut into FEC packets (K=7 r=½…¾ convolutional + CRC + interleave) and fountain symbols →
`waveform.compose` draws the burst from the key the crypto provider supplies → radiated. Every
transmitted sample is payload (no preamble/pilot/guard/CP).

**Receive.** Capture → **blind acquisition** (`discover`: measure the sender's scale/width/carrier/
alphabet; once per sender, cached in `neighbors`) → per-burst **known-geometry read** (the
constant-modulus geometry read + the joint/band least-squares reads) → soft bits → **Viterbi**
decode → **CRC** arbitrates family/M/rate (nothing is signalled) → fountain reassembles the message
from any spanning subset → delivered, or decode-and-forwarded by a relay. No acks, ever.

**Two-way, not one-way traffic.** The *delivery model* is reply-free, but traffic is two-way: the IP
tunnel's datagram + reliable classes and the TCP proxy carry UDP/TCP in both directions to the
anycast HOME (any gateway). "One-way" never meant one-directional.

## The four refusals (the invariants every change must keep)

1. **Nothing pre-shared.** No synced clocks, schedules or channel plans; the receiver measures
   everything. Only a key (via a provider) may exist. [parameter-free]
2. **Nothing required back.** No acks, handshakes, echoes or quality reports; a listen-only node
   decodes as well as any other. Reliability is **forward**: FEC + fountain + multi-path relay.
3. **Entroptics-pure PHY.** No pilots, preamble, guard band, equalizer, resampling or FFT in the
   codec path. Bench diagnostics may use anything; nothing of that kind enters the codec.
4. **Pluggable core.** Encryption, frequency agility and interference handling are **pluggable providers**
   (interfaces + null defaults), never in the core. Under the null providers the mesh runs fully as
   an open broadcast network.

## The provider model

Three plug points, all with working null defaults, are where an integrator adds their own
parts: **`CryptoProvider`** (seals payloads, supplies the PHY key; key freshness is its job),
**`AgilityProvider`** (chooses each transmission's width/centre/power/geometry — the place for a
keyed schedule / hop; it is handed the neighbour table and a `PlanContext` of the sender's own
GPS/velocity/time to seed on), and **`InterferenceProvider`** (observes the band, vetoes or reshapes a
plan). The core supplies the raw material; you supply the algorithm. Outside the core,
`entroptics_mesh.ext` holds two working providers, selected explicitly: `AeadCrypto` (end-to-end
AEAD sealing under a pre-shared key; relays forward bytes they cannot open) and `EnergyInterference`
(detects sub-bands with interference for the announcement, which a sender's planner steers off). Full guide, seed-source table
and worked sketches: **[research/PROVIDERS.md](../research/PROVIDERS.md)**. A keyed schedule can let a
coordinated receiver *predict* a sender and skip most of the blind search — approaching incumbent
receive cost while keeping the pure core (roadmap; see the coordination-prior note in
[research/PROVIDERS.md](../research/PROVIDERS.md)).

## Where the documents live

| Topic | Document |
|---|---|
| Mesh design + results (paper) | [research/PAPER.md](../research/PAPER.md) |
| PHY design + results (paper) | [entroptics-rf research/PAPER.md](https://github.com/Agience/entroptics-rf/blob/main/research/PAPER.md) |
| Writing crypto/agility/interference providers | [research/PROVIDERS.md](../research/PROVIDERS.md) |
| Install / build / three-repo graph | [research/PACKAGING.md](../research/PACKAGING.md) |
| Real-time receive port (scope + measured) | [research/realtime/PLAN.md](../research/realtime/PLAN.md), [research/realtime/port_results.md](../research/realtime/port_results.md) |
| Out-of-band emission vs RSS-247 | [entroptics-rf research/emission.md](https://github.com/Agience/entroptics-rf/blob/main/research/emission.md) |
| Energy-per-bit model vs incumbents | [research/power/energy_model.md](../research/power/energy_model.md) |
| Coded MCS envelope | [research/mcs/](../research/mcs/) |
| Operator runbook (software + bench + fleet, end to end) | [RUNBOOK.md](RUNBOOK.md) |
| Protocol specification (mesh↔RF interface, wire formats, invariants) | [PROTOCOL-SPEC.md](PROTOCOL-SPEC.md) |

## Build and test

Install in dependency order (`entroptics` → `entroptics-rf` → `entroptics-mesh`); full CPU-only and
with-SDR sequences in [research/PACKAGING.md](../research/PACKAGING.md). Each repo runs its suite with
`pytest` from its root (`pytest -m "not slow"` for a quick subset). Suites run with
`OPENBLAS_NUM_THREADS=1`.

## Current maturity and limitations

entroptics-mesh is a **measured research prototype**, not a shipping product — characterized on a
cabled two-SDR bench and in simulation; not flown.

- **Real-time:** measured on a Raspberry Pi 4B (aarch64, the conservative floor — a Pi-5 is
  ~2–3× faster). The compiled kernels (Viterbi, CRC, entropy) are bit-identical to the Python
  reference; the dense-linear-algebra receive path uses the tracked local scan, a parabola settle and
  coarse-row geometry. **Entroptics decodes in real time for a narrowband channel** — one Pi-4B core
  ≈ 44 kS/s, 4 cores ≈ 180 kS/s (~180 kHz), a Pi-5 ≈ 450 kHz — the IoT/telemetry/control regime where the
  energy story also wins. **Wideband (MHz-class FPV/MANET) is not real-time on a Pi** and needs a
  multi-core server or FPGA offload of the 64-dim least-squares reads. Full numbers:
  [research/realtime/port_results.md](../research/realtime/port_results.md).
- **SISO.** One data stream; parallel-stream multiplexing is a roadmap lever for aggregate
  throughput (not necessarily MIMO).
- **Emission:** measured ≥ ~22 dB below the in-band peak at all occupancies on a cabled bench, under the
  RSS-247 §6.6 20 dB line; not a certified measurement. The
  only sub-20 dB readings are a discrete SDR image spur on commensurate sample-rate ratios (a
  hardware-cal item, absent on coprime). See emission.md.
- **Coherent QAM above 64-QAM** needs ≤ 0.5 band occupancy on the bench (analog band-edge roll-off).
- **Not flown / emulated air:** all mesh results use the real PHY over an emulated channel (no
  Doppler, drift, multipath, terrain).
- **Open mode is public:** under the null crypto provider anyone can read the mesh.
