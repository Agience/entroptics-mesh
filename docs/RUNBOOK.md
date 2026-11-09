# entroptics mesh — operator runbook (end to end)

A deterministic, copy-paste procedure for standing up and validating the entroptics RF-mesh reference
system from a console, in three parts that build on each other:

- **Part A — software, no radio.** Install the stack, run the suites, run the end-to-end mesh demo on
  the emulated air. Proves the build and the protocol with zero RF.
- **Part B — SDR bench (PHY).** Validate the physical layer on the cabled Pluto+/NESDR bench: transmit
  a keyed burst at low gain, capture it, decode it blind, and run the ranging / Doppler / detectability
  measurements. **Transmits** — read the safety box first.
- **Part C — HIL mesh fleet.** Bring up the real multi-hop mesh across the Raspberry-Pi fleet and
  demonstrate node-loss resilience.

Run the parts in order the first time. Each step lists the **command**, the **expected result**, and a
**PASS gate**. If a gate fails, stop and resolve it before continuing — do not skip ahead.

> **Audience:** a human operator/integrator at a console. This is the operational guide; design
> rationale is in `ARCHITECTURE.md`, the protocol contract in `PROTOCOL-SPEC.md`, the lab internals in
> `harness/RUNBOOK.md` and `harness/fleet/README.md`.

---

## 0. Conventions and the determinism contract

Every run in this book is made reproducible by pinning the same environment. **Export these in every
shell** before running anything (they make the numerics and the Python hashing deterministic):

```bash
export OPENBLAS_NUM_THREADS=1      # the one known non-determinism is threaded-BLAS 64-dim reduction
export PYTHONHASHSEED=0            # stable dict/set ordering
export ENTROPTICS_FAST=1           # use compiled kernels if numba is present; falls back to Python
```

- `ENTROPTICS_FAST=0` forces the pure-Python reference path (slower, bit-identical result). Use it to
  rule out a numba-build problem.
- The stack is **seed-driven**: a mesh scenario run twice with the same seed produces bit-identical
  logical output (delivered bytes, slots, burst count). Where a command takes a `--seed`, the same seed
  reproduces the run.
- **Dependency direction is one-way:** `entroptics` ← `entroptics-rf` ← `entroptics-mesh` ← `harness`.
  The `PYTHONPATH` in every command reflects that.

Paths below assume the three repos are siblings under one workspace root (`.../agience/`). Adjust if
yours differ.

---

## Part A — software validation (no radio)

### A1. Create the environment and install the stack

```bash
cd <workspace>/entroptics-rf     && python -m venv .venv && . .venv/Scripts/activate   # (Linux/mac: . .venv/bin/activate)
pip install -U pip
pip install -e <workspace>/entroptics        # core (published: `pip install entroptics`)
pip install -e <workspace>/entroptics-rf     # PHY
pip install -e <workspace>/entroptics-mesh   # mesh
pip install numpy scipy                      # runtime deps (pinned in each pyproject)
pip install numba                            # OPTIONAL: enables ENTROPTICS_FAST compiled kernels
```

**Expected:** three editable installs resolve with no version conflict.
**PASS gate:** `python -c "import entroptics, entroptics_rf, entroptics_mesh; print('ok')"` prints `ok`.

### A2. Run the test suites

From each repo root, with the determinism env exported:

```bash
cd <workspace>/entroptics-rf   && pytest -q          # 412 tests
cd <workspace>/entroptics-mesh && pytest -q          # 330 tests
```

**Expected:** both suites green.
**PASS gate:** `pytest` exit code `0` in both repos. (The printed "N passed" line can be swallowed by a
redirect — **trust the exit code**, `echo $?`.) Run once more with `ENTROPTICS_FAST=0` to confirm the
reference path is also green.

Fast lane (optional): `pytest -m "not slow"` runs the quick subset.

### A3. Run the end-to-end mesh demo

```bash
cd <workspace>/entroptics-mesh
PYTHONPATH=src:../entroptics-rf/src python examples/quickstart.py
```

**Expected:** a three-node chain `A → B → C` (15 km hops; C is beyond A's direct reach) delivers A's
message to C via relay B, on the **emulated air** (no radio). It demonstrates the four refusals
(nothing pre-shared, nothing required back, no added structure, pluggable security) and a custom
`SharedKeyCrypto` provider. Takes a few seconds (the first burst from each sender is a full blind
acquisition).
**PASS gate:** the script prints that C received A's message (delivered), and exits `0`.

> Part A proves the protocol and your build end to end before you ever key a transmitter.

---

## Part B — SDR bench, physical layer

> ### ⚠ TRANSMIT SAFETY — read before any step in Part B
> - **Cabled only.** Every transmit in this part goes into a **coax cable through attenuators**, never
>   an antenna. Both Pluto TX jacks radiate (mirrored lanes): **TX1 → 30 dB pad → NESDR**, **TX2 → 30
>   dB pad → Pluto RX2**. Confirm every SMA is tight and the pads are in line before transmitting.
> - **Low gain only.** Keep TX gain at **−26 dB** and never above **−20 dB**.
> - **Verify TX off after every run.** Each procedure ends by reading the RX with TX stopped and
>   confirming the level is back at the noise floor (~2–3 counts). Never leave a bench session without
>   this check.
> - If the physical cabling differs from the above, stop and re-cable first.

Bench settings: Pluto `ip:<pluto-address>` (a factory Pluto answers at `ip:pluto.local`), `FC=915 MHz`, `KEY=1234`, `L=64`, TX gain −26 dB,
NESDR gain 20. The capture scripts for B2–B4 are not part of this repository; each step states the measurement and its
expected result, to be reproduced with capture tooling built on `entroptics_rf.radio`, with the rf venv active.

### B1. Reachability + TX-off probe (no transmit)

Confirm the radio is reachable and quiet **before** transmitting:

```bash
cd <workspace>/entroptics-rf && . .venv/Scripts/activate
PYTHONPATH=src python - <<'PY'
from entroptics_rf.radio import PlutoRadio
import numpy as np
rx = PlutoRadio("ip:pluto.local", sample_rate=2_400_000, center_freq=915_000_000)   # your Pluto's URI
rx.stop_transmit()
import contextlib
with contextlib.suppress(Exception):
    r1, r2 = rx.receive_both(1<<15)
    print("RX1", float(np.std(r1.iq)), "RX2", float(np.std(r2.iq)))
rx.close()
PY
```

**Expected:** the Pluto is reachable; with TX off both lanes read near the noise floor (~1–3 counts).
`receive_both` may refuse to declare the lanes "live" below its 2.0-count threshold **with no signal
present** — that is normal with TX off, not a fault.
**PASS gate:** the script connects (no `UNREACHABLE`) and reports low RX levels. If unreachable, the
bench is powered off or on another IP — resolve before continuing.

### B2. Blind decode across clocks (the core PHY result)

Transmit the DQPSK rate-pair buffer and decode it blind through the real TX→NESDR chain. **Critical
gotcha:** the TX buffer is ~9000 rows and the cyclic capture starts at a random row, so the blind read's
`join_rows` **must be ≥ 9001** — the default 4096 leaves the reference row unfound and the lock is
declined (closure near null). Pass `join_rows=9001`.

**Expected:** across the eight sender/receiver rate pairs (harmonic, coprime, matched), the receiver —
told only the key — locks blind and decodes. Representative: TX 2.0 → RX 2.4 Msps recovers
`L=64, M=4 (DQPSK), scale=0.8333` (the exact clock ratio, read blind), closure-significance ≈ 96,
uncoded BER ≈ 1e-4.
**PASS gate:** at least the matched and the 2.0→2.4 pairs lock with closure-significance ≥ 20 and
BER < 0.05.

### B3. Shared-clock loopback decode (RX2) + arrival-time floor

A zero-drift, phase-coherent path (Pluto TX → its own RX2, one clock). This also measures the
arrival-time (ToA) ranging floor on hardware. Transmit a keyed burst, decode it from RX2, then stop TX
and read the post-TX RX level.

**Expected:** with TX on, `receive_both` reports RX2 std ≈ 35 (signal present); the burst decodes
(`L=64, M=4`, carrier ≈ 0, closure ≈ 0.47, sig ≈ 294, BER ≈ 1.1%); the shared-clock ToA jitter is
≈ 0.0007 sample ≈ 0.09 m one-way at 2.4 MHz. The script then stops TX and prints the post-TX RX level.
**PASS gate:** it decodes (sig ≥ 20) **and** the final line shows post-TX RX std back at the noise
floor (~1–3 counts) — i.e. **TX verified off**. If the post-TX level is not at noise, power-cycle the
Pluto and re-check before leaving the bench.

### B4. (optional) Ranging / Doppler / detectability measurements

With the same safety rules:

- range-rate: the recovered carrier tracks an imposed Doppler-equivalent offset (~1–3 m/s);
- emission vs TX backoff (image-spur characterization);
- out-of-band emission against the RSS-247 §6.6 20 dB line (a bench measurement, not a certified one).

Each **transmits** — the same cabled/low-gain/verify-off rules apply. Expected numbers are in
entroptics-rf `research/nav/doppler_hw.md` and `research/emission.md`.

### B5. End of bench session — mandatory TX-off confirmation

Re-run B1's probe. **PASS gate:** both RX lanes at the noise floor with no script transmitting. Only
then leave the bench.

---

## Part C — HIL mesh fleet (real multi-hop mesh on the Pi fleet)

Brings the **real** entroptics-rf + mesh stack up on the physical Pi-4B fleet
(the nodes named in the fleet roster), joined over an emulated-air bridge driven by a coordinator. No RF is
transmitted in this part (the air is emulated over the LAN); it exercises the real stack and real
decode on real hardware.

### C0. Prerequisites

- Fleet roster: a JSON file mapping each node's name to its host, `{"pi-0": "<host>", "pi-1": "<host>", ...}`,
  at `~/.entroptics/lab_roster.json` or the path in `ENTROPTICS_LAB_ROSTER`. Every fleet script takes its
  node list from it through `harness/fleet/roster.py`; it holds only names and hosts. `lab_run.py` and
  `robustness.py` place `pi-0`…`pi-3` in fixed roles (lead, relays, base).
- Credentials in `~/.ssh/pi_lab.env` as `PI_USER=...` / `PI_PASS=...` (same login on every node).
  **Never echo this file.** Scripts read it via paramiko; none prints the password.
- `pip install paramiko` on the orchestrator.

### C1. Survey the fleet (read-only)

```bash
cd <workspace>/entroptics-mesh/harness/fleet
python classify_nodes.py          # model / SoC / RAM / OS / SPI of each node
python survey_nodes.py            # python / pip / venv / disk / net / existing deploy
```

**PASS gate:** every roster node answers and reports a 64-bit OS (`aarch64`) with Python ≥ 3.11.

### C2. Build the deploy bundle and deploy

The bundle is a one-liner (**must strip `__pycache__`** — stale cross-arch numba caches break the Pi):

```bash
cd <workspace>
STAGE=/tmp/lab_bundle; rm -rf "$STAGE"; mkdir -p "$STAGE/lab/harness"
cp -r entroptics-rf/src/entroptics_rf     "$STAGE/lab/"
cp -r entroptics-mesh/src/entroptics_mesh "$STAGE/lab/"
cp    entroptics-mesh/harness/*.py        "$STAGE/lab/harness/"
cp    entroptics-mesh/examples/quickstart.py "$STAGE/lab/quickstart_mesh.py"
find "$STAGE" -type d -name __pycache__ -exec rm -rf {} +      # CRITICAL
tar czf entroptics-mesh/harness/fleet/lab_bundle.tgz -C "$STAGE" lab   # where deploy_lab.py looks

cd entroptics-mesh/harness/fleet
python deploy_lab.py              # SFTP the bundle -> ~/entroptics-lab, build venv, pip install, verify
```

**Expected:** each node ends with `entroptics`, numpy and scipy installed (the verify step prints their
versions) and the stack importing.
**PASS gate:** `deploy_lab.py` reports success on every roster node.

### C3. Per-node decode micro-bench (uniformity check)

```bash
python bench_decode.py            # per-node decode timing/correctness (run via the deploy path)
```

**PASS gate:** every node decodes correctly and within the expected Pi-4B envelope (~0.2–0.8 s/burst
depending on mode). Uniform across nodes.

### C4. Distributed multi-hop mesh

```bash
python lab_run.py                 # listeners on the Pis + air_server over the bridge: lead -> relay -> base
```

**Expected:** a message originated on the lead node is delivered to the base node via a relay, decoded
on the real Pis.
**PASS gate:** `lab_run.py` reports delivered end to end. (Listener readiness is read from the Pi-side
log `grep 'listening on' ~/pi_node_<port>.log` — **do not** TCP-probe a listener; a connect+close
consumes its single accept and kills it.)

### C5. Node-loss resilience

```bash
python robustness.py              # base + 2 redundant relays + lead; drop relays and re-test
```

**Expected (all four scenarios):** full → delivered; drop relay B → delivered via relay A (auto-adapts,
no route repair); drop relay A → delivered via relay B; drop **both** relays → not delivered (the relay
is genuinely load-bearing — the mesh adapts to loss, it does not invent a path).
**PASS gate:** 3/4 deliver and the both-relays-down case correctly reports no delivery.

### C6. Teardown

Stop the Pi-side listeners and the coordinator. Listeners launched detached (`setsid … &`) exit when their single
connection closes; confirm none linger (`pgrep -af pi_node` on each Pi).

---

## Quick reference — gates at a glance

| Step | Command (abbrev.) | PASS gate |
|---|---|---|
| A1 | `pip install -e` ×3 | `import` of all three prints `ok` |
| A2 | `pytest -q` (rf, mesh) | exit `0` both, both flag states |
| A3 | `examples/quickstart.py` | C receives A's message, exit `0` |
| B1 | RX-only probe | reachable, lanes at noise (TX off) |
| B2 | blind rate-pair decode | matched & 2.0→2.4 lock, sig ≥ 20, BER < 0.05 |
| B3 | RX2 loopback decode | decodes **and** post-TX RX at noise (TX off) |
| B5 | RX-only probe | both lanes at noise (TX off) |
| C2 | `deploy_lab.py` | success on every node |
| C4 | `lab_run.py` | delivered lead→relay→base |
| C5 | `robustness.py` | 3/4 deliver; both-down = no delivery |

## Safety invariants (always true)

1. **Transmit only cabled, at low gain (≤ −20 dB; pinned −26 dB), and verify TX off after every run.**
2. **Never echo `~/.ssh/pi_lab.env`.**
3. **Strip `__pycache__`** from any bundle sent to the Pi fleet.
