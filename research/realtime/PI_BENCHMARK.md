# Raspberry Pi benchmark — the real-time go/no-go (Phase 0)

The #1 unknown for real-time is a **real aarch64 measurement**: every ms/burst number in
`port_results.md`/`PLAN.md` is x86 (Tiger Lake) extrapolated to a Pi. This kit runs the actual decode
on the Pi and replaces the extrapolation with a number. **No RF hardware on the Pi** — the Pi is the
decode *compute* target; the benchmark feeds it recorded/synthetic bursts and times the decode.

**Target here is a Pi-4B** (Cortex-A72 @ ~1.5–1.8 GHz) — a *harder* target than the Pi-5 (A76 @
2.4 GHz) the docs extrapolate to (~2–3× slower core, slower memory). So a 4B number is a conservative
floor: if it's workable on a 4B, a 5 is comfortable. Label all results "Pi-4B measured."

## What it measures
- `profile_receive.py` — per-burst decode ms on the canonical cells (dense LA + scan + entropy +
  Viterbi + settle). **The dominant ~70% (dense LA) needs no numba**, so the flag-OFF run alone gives
  the critical aarch64 number.
- `bench_viterbi.py` — the numba Viterbi kernel vs the Python reference (needs numba).
- Both pinned to `OPENBLAS_NUM_THREADS=1` (threaded BLAS hurts the 64-dim path and destabilizes a
  couple of timing tests).

## Prereqs (what you provide)
1. **SSH access** to the Pi (IP/user/key).
2. **64-bit Raspberry Pi OS** (`uname -m` → `aarch64`). 32-bit blocks numba (the flag-OFF LA number
   still works, but confirm 64-bit for the full picture).
3. **Internet on the Pi** for `pip install` — or build the wheels offline and `scp` them (see below).
4. Python 3.10–3.12 (default on current Pi OS), ~1 GB free disk.
5. Optional: a **heatsink/fan** (the 4B throttles under sustained load — the script logs temperature
   so we can see it) and an **inline USB power meter** (turns the provisional energy-per-bit into a
   measured one).

## Transfer the code (from the workspace, once SSH is up)
The benchmark needs both `src` trees + the realtime scripts. From the repo root:
```sh
# NB: --exclude __pycache__ is REQUIRED — shipping x86/pyXY numba cache files makes numba on the Pi
# throw "EOFError: Ran out of input" and the whole flag-ON path fails (looks like a hang).
tar czf /tmp/entroptics-bench.tgz --exclude='__pycache__' \
    entroptics-rf/src entroptics-mesh/src \
    entroptics-mesh/research/realtime/profile_receive.py \
    entroptics-mesh/research/realtime/_bursts.py \
    entroptics-mesh/research/realtime/poc \
    entroptics-mesh/research/realtime/bench_pi.sh
scp /tmp/entroptics-bench.tgz <pi>:~/
ssh <pi> 'mkdir -p ~/entroptics-bench && tar xzf ~/entroptics-bench.tgz -C ~/entroptics-bench'
```
(`entroptics` core comes from PyPI during the pip step; it's pure-Python, so it installs on aarch64.)

## Run
```sh
ssh <pi> 'bash ~/entroptics-bench/entroptics-mesh/research/realtime/bench_pi.sh'
```
`bench_pi.sh` creates a venv, installs `numpy scipy entroptics` (aarch64 wheels, reliable), tries
`numba` (aarch64 wheel — see note), then runs **flag-OFF first** (the always-works critical number)
and **flag-ON + bench_viterbi** if numba installed. Results land in `~/entroptics-bench/results/`.

### numba on aarch64
Recent numba/llvmlite (≥0.45 / the 0.68 line the x86 runs used) publish **aarch64 manylinux wheels**,
so `pip install numba` should fetch a wheel on 64-bit Pi OS — no source build. If pip instead tries to
build `llvmlite` from source and fails (`FileNotFoundError: llvm-config`), the flag-OFF number is
still captured; for the flag-ON half use **miniforge/conda** (`conda install numba`) or a prebuilt
wheel. The script does not fail the whole run if numba is missing.

## Interpreting the result (go/no-go)
Compare the Pi-4B ms/burst to the x86 table in `port_results.md`. The budget is **~2–4 ms/burst** for
DQPSK-class bursts (≤8 ms for a 300-row burst) to sustain 2.4 MS/s per receive chain; the 4B has 4
cores, so the realistic target is one burst/core with BLAS pinned to one thread. Key questions the run
answers:
- The real x86→A72 derating (replaces the ~2–3.5× scalar / ~8–15× BLAS extrapolation).
- Whether a **stable clock holds the burst start phase** well enough for §3a's ±3-sample tracked
  window to fire on real hardware (the emu randomizes it — this is the open question from the §3a
  lock-cache work; a fixed-cadence capture loop on the Pi tests it).
- With the §3a tracked path, the realistic per-burst time for the lighter modes vs the budget.

## Power (optional, if a USB meter is present)
Note idle watts, then watts during a sustained decode loop; energy/burst = (W_decode − W_idle) ×
ms/burst. Feeds `research/power/energy_model.md` (replaces the provisional Pi-5 RX-compute figure).
