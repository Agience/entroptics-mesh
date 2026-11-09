# Test coverage — entroptics-rf + entroptics-mesh

*Measured 2026-10-17 (pytest-cov 7.1.0, `ENTROPTICS_FAST=0` = pure-Python reference path,
`OPENBLAS_NUM_THREADS=1`). Production punch-list item #2.*

## Headline

| package | statements | missed | coverage |
|---|---|---|---|
| **entroptics-rf** | 1970 | 125 | **94%** |
| **entroptics-mesh** | 4084 | 430 | **89%** |

Both suites pass at both flag states: **ON** (rf 395 / mesh 249) and **OFF** (rf 395 / mesh 249),
combined rc 0. This coverage run (flag OFF) is itself the flag-OFF confirmation for the Phase 2 LA
leanings.

## Reproduce
```sh
# rf
cd entroptics-rf
OPENBLAS_NUM_THREADS=1 ENTROPTICS_FAST=0 PYTHONPATH=src \
  python -m pytest --cov=entroptics_rf --cov-report=term-missing:skip-covered --cov-report=json:cov_rf.json
# mesh (needs entroptics-rf on the path)
cd ../entroptics-mesh
OPENBLAS_NUM_THREADS=1 ENTROPTICS_FAST=0 PYTHONPATH="src;../entroptics-rf/src" \
  python -m pytest --cov=entroptics_mesh --cov-report=term-missing:skip-covered --cov-report=json:cov_mesh.json
```
`cov_rf.json` / `cov_mesh.json` are regenerable artifacts (git-ignored, not committed).

## By module

**entroptics-rf** — `waveform.py` **95%** (1418 stmts; the codec incl. cure (e), the band/joint
reads, `decode_scaled`, row radius — the new wire format is well exercised), `__main__.py` 99%,
`runner.py` 98%, `radio.py` 94% (the missed lines are SDR hardware error paths, not reachable without
a live radio), `screen.py` 100%.

**entroptics-mesh** — adapt / relay / neighbors **99%**, link / providers **97%**, tunnel 96%,
`emu/node_run` & `emu/subband_run` 95%, phy **92%**, fec 90%, announce 90%, **node.py 89%** (the
largest absolute gap — a few runtime branches and some of the new `plan_for`/`PlanContext` lines),
`emu/fleet_run.py` **56%** (the scenario-runner harness — lowest; functional but lightly unit-tested),
9 modules at 100%.

## `_fast.py` — line coverage is not a meaningful metric (numba)

`entroptics_rf/_fast.py` and `entroptics_mesh/_fast.py` (the numba compiled kernels — Viterbi, batch
Viterbi, constant-modulus entropy) show **0%** on the flag-OFF run above (no numba → the kernels never
execute). A flag-ON run (numba venv, `ENTROPTICS_FAST=1`) was done to check: `entroptics_rf/_fast.py`
rises only to **30%** (the dispatch/wrapper lines), and the `@njit` kernel **bodies (lines 33-60) stay
"missed" even though they execute** — because **coverage.py cannot trace into numba-compiled code**
(it is machine code, not interpreted Python). So line coverage of the `_fast` kernels is a measurement
artifact, not a real gap. The kernels are validated the right way: **bit-identical** to the Python
references on adversarial corpora (`research/realtime/port_results.md`), a stronger guarantee than line
coverage. The flag-ON run also confirmed the compiled path runs green (rf fast subset, rc 0).

## Assessment

- 94% (rf) and 89% (mesh) is healthy for a prototype; the codec and the mesh core (routing, relay,
  link, adapt, providers, phy, tunnel) are all ≥89%.
- **Not yet covered:** `emu/fleet_run.py` (scenario harness) and the remaining `node.py` runtime
  branches; the CI templates set no coverage threshold. (The
  `_fast.py` kernels are covered by bit-identity gates, not by coverage.py — see above — so a coverage
  gate should exclude or not penalize them.)
