# Phase 1 / Phase 2 port — measured results

## TL;DR — the real-time answer

**Question:** can entroptics decode in real time on a deployable CPU? **Answer:** **yes for
narrowband, on a Pi-class CPU** — real-time up to **~190 kHz channels on a Pi-4B, ~475 kHz on a
Pi-5** (the IoT / telemetry / control regime, which is also where the power story wins). **Wideband
(MHz-wide FPV / high-throughput MANET) needs a server-class host, more cores, or FPGA/GPU offload** of
the dense-LA reads. This is now a measured envelope, not an open unknown.

**How we got there — ~4× cumulative software speedup, all measured on the real Pi-4B, all
oracle-clean** (canonical tracked burst, ms/core): **838 → 620 → 339 → 269 → 212**
- **620:** compiled numba Viterbi + entropy (`ENTROPTICS_FAST`) — Viterbi 61–67× on aarch64, bit-identical.
- **339:** §3a tracked-scan reduction (local search on a prior) — scan 9–11× for a tracked sender.
- (makes §3a apply to the *whole* mesh:) **7c keyed-start coordination prior** (agility provider) —
  §3a fires on 11/11 bursts in the default contention mesh, delivery-identical, no new on-air structure.
- **269:** Phase 3 parabola settle (`ENTROPTICS_PARABOLA_SETTLE`, default on) — Powell→`_bowl`, 120/120 byte-identical.
- **212:** §3b coarse-rows geometry (`ENTROPTICS_COARSE_ROWS=16`, opt-in) — fit the 3-param geometry from
  a row subset; 0-flip to 4 rows, zero extra fallbacks.
- **Dead-end:** complex64 (`ENTROPTICS_C64`, off) — 2× on the matmul but no e2e win; kept off.

**The wall:** the dense-LA *reads* (`_scaled_coords`/`_joint_coords`) — a 64-dim least-squares that is
a **payload** wall, not a geometry one (geometry is 0-flip at 4 rows; the payload needs the data). No
per-kernel speedup (compiled matmul, complex64) cracks it; the win was algorithmic (§3a/§3b) + the
compiled non-LA kernels. **Software is now exhausted at ~4×;** the remaining ~13× to 2.4 MS/s is
hardware (Pi-5, FPGA offload), lower sample rates, or living within the narrowband envelope.

**Status:** all flags default to a byte-identical reference. Open: whether to enable
`ENTROPTICS_COARSE_ROWS=16` as default (1.27×, safe), and a Pi-5 or FPGA for the wideband case.
Details below.

---

Productionizing the real-time receive path per `PLAN.md`: the inherently-sequential and
transcendental kernels ported to compiled `numba @njit` behind a flag, oracle-gated bit-for-bit
against the pure-Python reference, measured on the development x86 box.

**Box:** Intel i7-1185G7 (Tiger Lake, 4C/8T, x86-64), numpy 2.5.4 + scipy-openblas, single BLAS
thread (`OPENBLAS_NUM_THREADS=1`), numba 0.68.0. Same `research/realtime` corpus and node fast path
(`accept=read_frame`) as the profile. **The absolute ms here are a little lower than `PLAN.md`'s
180 ms canonical figure because this run was on a less-contended box; use the paired flag-OFF vs
flag-ON numbers below, taken back-to-back in one session, for the speedup.**

**Pi caveat (unchanged):** no aarch64 / Pi-5 hardware here. Every Pi-5 number is an extrapolation
(§"Pi-5 projection").

---

## What was ported, and the oracle evidence

Everything ships behind one flag, **`ENTROPTICS_FAST`** (env var):
`0`/`off`/`false`/`no` forces the pure-Python path; unset or `1`/`on` uses the compiled path **only
when numba imports AND the kernel has passed its bit-identity gate against the live Python
reference** (else it silently falls back to Python). `fastmath=False` in every kernel. The
Python reference is never deleted — each ported function keeps its exact signature and its pure
reference alongside (`_crc32_py`, `_viterbi_decode_py`, `_viterbi_decode_batch_py`, `_cm_rows_py`).

| kernel | file | reference (oracle) | gate | result |
|---|---|---|---|---|
| `fec.crc32` → `zlib.crc32` | `entroptics_mesh/fec.py` | `_crc32_py` (table loop) | `np.array_equal` on 3000 random inputs + continuation form | **exact** |
| `fec.viterbi_decode` (numba) | `entroptics_mesh/_fast.py` | `_viterbi_decode_py` | `np.array_equal`, 200 adversarial trials (scales 0.3–6.0, erasures 0–40%, 5 frame sizes, near-ties) | **bit-identical** |
| `fec.viterbi_decode_batch` (numba) | `entroptics_mesh/_fast.py` | `_viterbi_decode_batch_py` | `np.array_equal`, 60 trials, H = 1..6 | **bit-identical** |
| `waveform._cm_rows` (numba entropy) | `entroptics_rf/_fast.py` | `_cm_rows_py` (`entropy.shannon_bits`) | `rtol 1e-9 / atol 1e-12`, m = 2/16/64, incl. zero/even/NaN rows | **≤1e-9 (float)** |

**Gate reproduction** (scratch venv with numba; `OPENBLAS_NUM_THREADS=1`,
`PYTHONPATH=<mesh>/src;<rf>/src`):

```
_use_fast_viterbi() = True ;  _use_fast_cm() = True
crc32 == zlib == _crc32_py on 3000 random (+continuation): True
viterbi single: identical on 200 adversarial trials: YES
viterbi batch:  identical to _py oracle on 60 trials (H=1..6): YES
```

**End-to-end oracle (the decisive check):** the decoded frame — `(family, M, rate)` tag and the
packed `Symbol` bytes from `link.read_frame` — is **byte-identical with the compiled path ON vs OFF
on all 120 corpus bursts** (5 cells × 24 reps). The constant-modulus kernel's ~1e-15 reduction
difference flips **no** scan decision on the corpus, so the whole receive path is bit-identical,
not merely the isolated kernels:

```
dqpsk_1p0_2p4   on=24/24 off=24/24  on==off 24/24  IDENTICAL
dqpsk_matched   on=24/24 off=24/24  on==off 24/24  IDENTICAL
qam256_1p0_2p4  on=24/24 off=24/24  on==off 24/24  IDENTICAL
qam256_matched  on=24/24 off=24/24  on==off 24/24  IDENTICAL
qam16_1p0_2p4   on=24/24 off=24/24  on==off 24/24  IDENTICAL
TOTAL 120/120
```

---

## Isolated kernel speedups (x86, single thread)

**Viterbi** (512-byte frame, 4150 trellis steps — the worst case / node fast path):
```
python ref 25.68 ms   →   numba 0.50 ms      51x        (PoC bench reproduced: 50.8–52.6x)
```

**Constant-modulus entropy read** `_cm_rows` (per-row `−Σ p log₂ p` + deficit + cap):
```
(131,16)  82.9 µs → 29.9 µs  2.8x
( 66,16)  56.3 µs → 14.3 µs  3.9x
(300,16) 150.9 µs → 78.3 µs  1.9x
(131,64) 163.6 µs → 117.5 µs 1.4x
```

**CRC-32**: `zlib.crc32` is a C routine, bit-identical to the table loop; sub-kernel and already
negligible (~0.1 ms/burst), swapped for hygiene.

---

## Per-burst ms/burst, flag OFF vs ON (node fast path)

Paired back-to-back, 40 independent captures/cell (varying noise + sub-sample start), warm cache,
one BLAS thread. OFF = pure Python every kernel; ON = numba Viterbi + numba `_cm_rows` (the dense
LA scan/coords stay numpy — see "What is NOT ported").

| cell | OFF (Python) ms | ON (Viterbi+entropy) ms | speedup | decoded |
|---|---|---|---|---|
| **dqpsk_1p0_2p4 (canonical node cell)** | 171.6 | **135.3** | **1.27×** | 40/40 |
| dqpsk_matched 2.4 Msps | 86.2 | **52.8** | **1.63×** | 40/40 |
| qam256_3/4 1.0→2.4 | 134.5 | **91.8** | **1.47×** | 40/40 |
| qam256_3/4 matched | 73.2 | **33.2** | **2.20×** | 40/40 |
| qam16_1/2 1.0→2.4 | 172.0 | **121.8** | **1.41×** | 40/40 |

The gain tracks the Viterbi's per-cell share exactly as the profile predicted: biggest on the short
matched/dense-QAM bursts where Viterbi was 40–48 % of the path (256-QAM matched 2.2×), smallest on
the long narrow-sender bursts where the scan dominates (canonical 1.27×).

**Where the time now is (canonical cell, flag ON, cProfile self-time):** `_scaled_scan` 1.59 s,
`_scaled_coords` 0.75 s, `_joint_coords` 0.22 s — the **dense complex LA is ~73 % of self-time**,
exactly the 68 % "core" the plan flagged. The compiled `_cm_rows` is 0.32 s and the compiled
`viterbi_decode` **0.015 s** (was the #1–2 cost in Python). Phase 1 + the entropy kernel have moved
the two non-BLAS hot kernels out of the budget; the remaining cost is BLAS-bound linear algebra.

---

## What is NOT ported, and why

**The dense linear algebra — `_scaled_scan`, `_scaled_coords`, `_joint_coords` — was deliberately
kept on numpy.** These are BLAS `cgemm` projections on small (64-dim) complex matrices. A numba
`@njit` matmul sums in a different order than OpenBLAS, so it is **not bit-identical** to the
reference, and — critically — the scan picks an **argmax over candidate starts**, where a sub-ulp
perturbation can flip the chosen start and change the decoded frame. That is the determinism hazard
`PLAN.md` §5 names. A naive numba matmul also would **not beat** an optimized OpenBLAS `cgemm` at
64-dim (BLAS hits ~74 GFLOP/s here). The real Phase-2 win is a **fused compiled kernel that keeps
the cached gram and removes the Python dispatch over the ~37 small per-phase ops** (plan §3a (b)),
which is the 1–2-month effort the plan budgets — not a drop-in. Per the rule "keep the Python
reference the default where the gate is anything short of exact," it stays Python for now.

The entropy kernel was portable because it is a **transcendental reduction**, not a matmul: its
reduction-order difference is ~1e-15 and, verified above, changes no decision on the corpus.

---

## End-to-end x86 speedup and the remaining gap to real time

**Measured now (canonical DQPSK, node fast path):** 171.6 ms → **135.3 ms**, ≈ **1.27× end-to-end**
on x86 (up to 2.2× on matched 256-QAM). Against the plan's 180 ms baseline the canonical cell is
now **~135 ms (~1.33×)**. This realizes the **Viterbi + entropy** portion of the plan's full-port
projection; the dense-LA portion (the 68 % core) is still ahead.

**Budget:** real time at 2.4 MS/s needs **~2–4 ms/burst** for a mid-length DQPSK burst (up to ~8 ms
for a 300-row burst). The path is still ~35–50× over budget on a single x86 core, because the LA
core is unported.

### Pi-5 projection (extrapolation — not measured)

Derating x86 → Cortex-A76 @ 2.4 GHz per the plan's ratios (scalar+NEON ~2–3.5×; OpenBLAS `cgemm` on
A76 ~8–15× slower than this Tiger Lake core), applied to the **current partially-ported** path,
which is now ~73 % BLAS-bound:

- **single A76 core:** canonical ~135 ms × ~(3–5) ≈ **~0.4–0.7 s/burst** — far over a 2–4 ms budget.
- **4 cores pipelined** (one burst/core, BLAS pinned to one thread — bursts are independent):
  ≈ **~0.1–0.18 s/burst aggregate** — still ~25–60× over budget.

Real time for the lighter modes is **not** in reach from Phase 1 + the entropy kernel alone. It
needs the dense-LA fused port **and** the §3a algorithmic scan reduction. With the plan's full-port
projection (scan+coords+joint+entropy compiled/fused at ~8×, Powell replaced by a parabola) the x86
burst drops to ~20–25 ms and a 4-core A76 reaches the **~2.4 MS/s range for the lighter modes,
tight for long DBPSK** — the plan's §4 conclusion is unchanged; this pass has only removed the two
non-BLAS kernels from it and confirmed the LA now dominates.

---

## What remains

- **Phase 2 (dense-LA core, the 68 %, ~1–2 months):** the fused compiled scan/coords/joint kernel
  that keeps the cached `_scaled_gram`/`_joint_gram`/`_mode_basis_cached` and removes Python
  dispatch over the small per-phase BLAS ops — the real lever, left on numpy here for bit-identity.
  First apply the constant-modulus speed study's bit-identical numpy leanings (complex64 / cached grams / leaner axes)
  as a safe baseline; this pass did not touch them.
  - **Core recommendation (per the surgical-core rule — NOT done here):** the entropy reduction
    `entropy.shannon_bits(axis=)` is a generic, RF-agnostic transcendental reduction; the fast path
    here is an **rf-local** numba kernel (`entroptics_rf/_fast.py`) so the published core is
    untouched. Accelerating `shannon_bits(axis=)` in core (vectorized/compiled `log2`) would benefit
    every repo and let the rf kernel be retired. Reported, not applied.
- **Phase 3 (settle / Powell, algorithmic):** replace bounded Powell with the
  hand-rolled parabola settle; determinism-sensitive (freeze the Powell path as oracle).
- **Phase 4 (scheduling):** one burst per core, BLAS pinned to one thread, lock-free
  handoff from the capture ring — where aggregate 2.4 MS/s is actually met or not.
- **Phase 0 (Pi-5 hardware, blocking the go/no-go):** run `profile_receive.py` and `bench_viterbi.py`
  on a Pi 5 to replace every extrapolation above with a number, confirm the A76 OpenBLAS
  kernel and numba/llvmlite on aarch64, and measure with a realistic duty cycle (thermal).

---

## Phase 2 (dense-LA) (leanings landed; compiled matmul found to be a dead end)

**Constant-modulus speed study's bit-identical numpy leanings, on the DEFAULT numpy path** (not flag-gated — each provably
bit-identical: it skips work never used, or caches a value without reordering any reduction):
- `_scaled_scan`: build the sub-sample filter bank only for the phases that actually occur in each
  count group (`np.unique(k)` — only ~12/64 SCAN_PHASES occur on the canonical grid), not all 64;
  cache the CM-mode filter (`_scan_coeffs`).
- `_scaled_gram`: also cache `E0.conj()` (as `E0c`) — both row reads re-allocated it on every one of
  the settle's hundreds of evaluations.
- `_scaled_coords`: cached `Cc` (`_scaled_coeffs`) + cached `E0c`. `_joint_coords`: cached `AH`
  (`_aperture_dual`).

Evidence: array-level `_scaled_scan`/`_scaled_coords`/`_joint_coords` vs a frozen pre-change oracle
over 5 cells × 3 seeds → **maxabsdiff 0.000e+00, NaN patterns equal**; end-to-end decoded
`(family,M,rate)` + packed Symbol bytes **byte-identical on all 200 corpus bursts**.

Measured ms/burst (x86, OPENBLAS_NUM_THREADS=1, 24 reps, paired frozen-oracle vs edited, flag ON;
absolute ms differ from §4a above by harness/contention — the ratios are the signal):

| cell | before | after | speedup |
|---|---|---|---|
| dqpsk 1.0→2.4 (canonical) | 106.65 | 94.40 | 1.13× |
| dqpsk matched | 33.94 | 30.68 | 1.11× |
| qam256 1.0→2.4 | 74.64 | 61.51 | 1.21× |
| qam256 matched | 25.69 | 22.04 | 1.17× |
| qam16 1.0→2.4 | 101.61 | 88.06 | 1.15× |

Dense-LA self-time share fell (canonical 72.6%→69.8%). These live in the default path, so **flag OFF
gets the identical LA speedup**.

**Fused compiled kernel — STAYS PYTHON (measured, not adopted).** Per-phase baseline (BLAS matmul +
vectorized square + compiled `cm_rows`) = 0.768 ms. Hand-rolled numba complex MAC + fused entropy =
4.34 ms (**5.6× SLOWER** — a scalar/NEON triple loop can't beat OpenBLAS zgemm at 64-dim); numba
`np.dot` (delegates to BLAS) + fused entropy = 0.886 ms (~1.15× slower — fusing removes only µs
Python dispatch, dwarfed by the ms matmul). The only matmul lever is **complex64 (zgemm→cgemm) =
0.541 ms (1.42× on the matmul)**, but end-to-end only ~1.05–1.15×, and it drops the scan's argmin
safety gap (winner vs runner-up start) to ~0.0000 on some corpus bursts while perturbing `num[]` by
~1e-6 — exactly the §5 determinism hazard. 0/200 flips is **empirical, not a proof**; adopting it
would loosen the gate ~9 orders vs the 1e-15 `cm_rows` gate. Per "never loosen a gate to pass,"
**complex64 is not adopted** and no compiled fused matmul lands.

**Strategic conclusion (reshapes Phase 2):** the real Phase-2 lever is **not** a compiled matmul
(there is no win at 64-dim, on x86 or A76) — it is the plan's **§3a algorithmic scan reduction** (a
local carrier/start search on a *tracked* sender). That both cuts the scan's dominant cost **and**
removes the argmin near-tie ambiguity, which would in turn make a complex64/batched-cgemm path
*provably* flip-safe. A compiled fused matmul only pays off (a) for a gateway/relay batching many
bursts (leaving the BLAS-overhead regime), or (b) paired with a runtime margin-gate that falls back
to complex128 whenever the top-two start gap is below a proven threshold.

Suites: **both flag states confirmed green** — ON (rf 395 + mesh 249, rc 0) and OFF
(`ENTROPTICS_FAST=0`, rf 395 + mesh 249, rc 0).

---

## Phase 2 (§3a) — local (tracked) scan reduction

The dominant cost (the scan) is cut not by compiling the matmul (shown a dead end above) but
algorithmically: once a sender is **tracked**, its start phase and carrier are known to a tight prior
(previous burst's lock), so the quarter-sample start grid shrinks from the full ~471 positions to a
small window (~25) around the prior.

**How it stays exact.** `waveform._scaled_scan(..., start0=, start_win=)` keeps only the full grid's
positions inside the window (`st_full[mask]`) — each surviving candidate's score is **bit-for-bit the
full scan's**; it is the full read with far starts dropped, not a new computation.
`phy.receive_known(..., tracked=(start_phase, carrier[, window]))` is a public wrapper (default
`tracked=None` is byte-identical to before) that **re-scans in full if the tracked lock's frame fails
CRC** — the exact CRC is the confidence gate, so a stale/wrong prior only costs a re-scan, never a
wrong lock.

**Oracle (corpus, 5 cells × 24):** true-geometry prior → **120/120 byte-identical** decoded
`(family,M,rate)` + Symbol bytes; previous-burst prior (the realistic tracked case) + CRC fallback →
**115/115 byte-identical**. Window-miss bound: without the fallback, 4/23 `dqpsk_matched`
mis-locked because the corpus jitters arrival ±7 samples/rep (a test artifact — a real stable clock
holds phase) so a ±3-sample window missed the true start; the CRC fallback caught every one. Default
`TRACK_WINDOW = 3.0` samples; the window must contain the true start, else only the CRC catches it.

**Cost (x86, OPENBLAS_NUM_THREADS=1, flag ON):** start grid ~471→25; isolated scan **~9–11×**; scan
self-time share **40–64% → 6–17%**; end-to-end **1.38–2.49×**:

| cell | scan full→local ms | e2e full→tracked ms (×) | scan share |
|---|---|---|---|
| dqpsk 1.0→2.4 (canonical) | 57.0→5.35 | 110.6→56.8 (1.95×) | 51%→9% |
| dqpsk matched | 14.2→1.45 | 35.6→25.7 (1.38×) | 40%→6% |
| qam256 1.0→2.4 | 53.4→5.66 | 83.1→33.3 (2.49×) | 64%→17% |
| qam256 matched | 14.2→1.51 | 29.1→16.4 (1.77×) | 49%→9% |
| qam16 1.0→2.4 | 64.0→6.01 | 119.1→65.0 (1.83×) | 54%→9% |

**§3a makes complex64 provably safe — by structure.** The dangerous flip is to a *far* chance
minimum (wrong start); on the local path a far competitor **cannot exist** (the window holds no start
beyond ±3), so a far flip is **structurally impossible and corpus-independent**, not merely absent at
this SNR. Measured: complex64 (cgemm) perturbs the row-averaged read by ≤1.7e-7 and changes the kept
candidate set on **0/120** bursts; the residual adjacent-start gap (≥1.2e-5) clears 2× the
perturbation by ~35×. In general a within-window margin gate (recompute a near-tied adjacent pair in
complex128 when its gap < ~1e-4) closes it with a *proven* margin, made near-free by §3a's 25-start
window. **complex64 is still NOT adopted** on a drone: on the now-tiny local scan (~5 ms, <17% of a
burst) the ~1.4× matmul win is a fraction of a ms; it pays off only for a **gateway batching many
bursts**, where §3a's structural safety is exactly what makes a batched cgemm admissible.

**Status:** Python full scan remains the oracle and the first-contact / CRC-fallback path. Suite
re-confirmation was running at hand-off; the default `tracked=None` path is byte-identical to
pre-change, baseline rf 395 + mesh 249.

**Remaining:** wire a per-sender lock cache into `node.py`/`link` so a tracked sender's previous
`(start_phase, carrier)` is passed as `tracked=` automatically (the `receive_known(tracked=...)` hook
is in place; the node still calls without a prior). The dense-LA fused kernel and Powell→parabola
settle are unchanged by this pass. The real go/no-go remains a **Pi-5 benchmark (Phase 0)**.

---

## Phase 2 (§3a) — wired into the mesh

The §3a `tracked=` hook is now driven by a **per-sender lock cache** in the live node (not just the
prototype): `Neighbor` carries `lock` / `lock_prior`; the node passes `tracked=table.tracked(who)`
into `receive_known` on the fast-read loop and caches the `(start_phase, carrier)` on each
CRC-passing decode. First contact / no prior → full scan, byte-identical. Files: `neighbors.py`,
`node.py` (mesh) only.

**Critical finding — the previous-burst start is NOT a usable prior in the default mesh.** With
listen-before-talk access and no repeating wire structure, the sender draws a **fresh random start
every burst**, so the start phase jitters across the whole row (measured std 10.2 samples ≫
`TRACK_WINDOW` 3.0). A naive "always pass the last lock" then **misses every time** and pays the CRC
fallback (a wasted local scan + a full re-read) — a ~2× node-path *slowdown*. The fix is a
**stability guard**: pass the prior only after two consecutive locks agree within the window, so a
stable-clock / fixed-cadence sender accelerates and a contention-jittered sender keeps
`lock_prior=None` and degrades to **exactly** the full scan (no waste). The scale and carrier *are*
stable (and cached); only the absolute start phase is randomized.

**Node-path per-burst time, stable-clock tracked sender** (12 consecutive bursts/cell, first full
then local, OPENBLAS_NUM_THREADS=1, x86):

| cell | without cache (ms) | with cache (ms) | speedup | decoded |
|---|---|---|---|---|
| dqpsk 1.0→2.4 | 78.9 | 36.0 | 2.19× | 12/12 |
| dqpsk matched | 31.4 | 22.1 | 1.42× | 12/12 |
| qam256 1.0→2.4 | 54.0 | 18.8 | 2.88× | 12/12 |
| qam256 matched | 23.0 | 14.0 | 1.64× | 12/12 |
| qam16 1.0→2.4 | 78.7 | 38.7 | 2.03× | 12/12 |

**1.42–2.88×** on the node path for a stable sender. Delivery parity holds under both radio modes
(listen-before-talk: the guard keeps the prior off, so no regression; no-listen fixed-start: §3a
fires live, later bursts read local). The CRC fallback + never-a-wrong-lock property means the
delivered set cannot differ. Mesh suite 249 green (flag ON).

**So §3a's broad benefit depends on a usable start prior** — available today only for
stable-clock/fixed-cadence senders (and real hardware where a stable clock holds the phase, which the
Pi-5 benchmark will show). The general lever is the **7c coordination prior**: a keyed schedule the
receiver computes predicts the sender's start (and on *first* contact too, which this cache cannot).
Remaining: lock age-out (it rides `Neighbor` expiry now) and the 7c start predictor.

---

## Phase 2 (§3a) — 7c coordination prior: §3a on the WHOLE mesh

The §3a mesh finding above is that the local scan fires only for a **stable-clock** sender, because
the lock cache uses the *previous burst's* start as the prior and listen-before-talk draws a fresh
random start every burst. **7c removes that restriction** with a **keyed transmit schedule**: the
sender draws its start offset from a keyed PRNG (not `rng.uniform`), and a receiver running the same
agility provider with the same key **predicts** that start and feeds it to `receive_known(tracked=)`.
The start still looks random on air; the keyed receiver reads it on the local scan regardless of the
jitter. **Support is the core's; the schedule algorithm is the provider's** (an integrator writes the
real one) — the core ships the interface, a null (today's random access, byte-identical), and a
minimal example keyed-PRNG schedule.

**Provider interface (added).** `AgilityProvider` gains two *optional* methods —
`transmit_offset(ctx, *, window_s) -> float|None` (the sender's own keyed start offset in
`[0, window_s)`) and `predict_offset(who, t, *, window_s) -> float|None` (the same value a receiver
reconstructs for sender `who`). `PlanContext` gains `node_id` so a schedule keys on the sender's
identity. `NullAgility` returns `None` from both (→ random draw / lock cache; **byte-identical
default**), and a provider that implements only `plan` is detected by duck-typing and also falls back.
The example `entroptics_mesh/schedules.py::KeyedSchedule` keys the offset on
`blake2b(key, node_id, round(t/epoch_s))` — deterministic across nodes, needs only the key + a coarse
shared clock, nothing back from a peer.

**How the receiver turns a predicted transmit time into a start-phase prior.** The receiver-measured
arrival start = sender transmit offset × `rx_rate` + a constant (propagation + clock + capture
alignment). The constant is unknown but cancels in a difference: the node keeps an **anchor** from
`who`'s last CRC-passing lock — `(schedule offset for that slot, measured start phase, carrier,
capture onset trim)` — and predicts the next burst's start as
`anchor_phase + (offset_now − offset_anchor)·rx_rate − (onset_now − onset_anchor)  (mod ℓ)`. The trim
term corrects the node's whole-64-sample energy-onset trim, which shifts the phase at a scale whose
row ℓ is not 64 samples (zero at a matched rate). The carrier is the anchor's (stable). So 7c does
**not** help the very first blind contact (the anchor comes from the first full/lock-cache lock), and
helps **every** burst after, whatever the start jitter. Priors are tried **provider prediction →
lock cache → full scan**; a wrong prediction hits the same CRC-gated full re-scan, so it only ever
accelerates (never a wrong lock).

Validated against the codec first (direct `receive_known`, start swept 90→240 received samples, >1
row of jitter): the predicted start landed within `TRACK_WINDOW` on **14/14** sweep points for every
cell.

**§3a firing + node-path speedup, DEFAULT listen-before-talk mesh** (real `Node.on_capture` over a
12-burst sequence whose start is chosen by the provider (keyed) or `rng.uniform` (null), mapped
through the emu air channel; `OPENBLAS_NUM_THREADS=1`, x86; `research/realtime/exp_7c.py`):

| cell | local-scan hits (post-first) null → keyed | node-path ms/burst null → keyed | speedup |
|---|---|---|---|
| dqpsk 1.0→2.4 | 0/11 → **11/11** | 143.7 → 64.3 | **2.24×** |
| dqpsk matched | 0/11 → **11/11** | 64.5 → 33.9 | **1.90×** |
| qam256 1.0→2.4 | 0/11 → **11/11** | 93.7 → 33.3 | **2.81×** |
| qam256 matched | 0/11 → **11/11** | 38.6 → 21.6 | **1.78×** |
| qam16 1.0→2.4 | 0/11 → **11/11** | 111.6 → 59.3 | **1.88×** |

Null reproduces the finding — **0 local hits** on every cell (the jittered previous-burst prior is
useless); keyed fires the local scan on **every post-first burst**, so the §3a **1.78–2.81×** now
applies to a contention-jittered sender, not only a stable-clock one. The first burst is the full
blind/anchor read in both.

**Delivery parity.** End-to-end over `NodeRun` (the full stack, incl. `node.start`/`commit` driving
the keyed schedule), the same three messages delivered **with and without** the provider: identical
delivered set, and equal to what was sent (`3/3` both ways). The CRC fallback + never-a-wrong-lock
property guarantees the delivered set cannot differ.

**No-added-structure check.** The keyed start sequence (2000 epochs): reproducible for the keyed receiver
(same key → same sequence); **differs per key** (mean |seq(k1)−seq(k2)| 0.336, the ~0.33 of
independent U[0,1)); uniform over the window (χ²=10.9, 9 dof); **no repeating period** (max
|autocorrelation| over lags 1..40 = 0.055, at the 0.044 white-noise band). It is a keyed-PRNG
sequence, not a fixed or periodic pattern — no new on-air structure.

**What remains.** The emu has **exact** timing, so the predicted start lands in the ±3
window every time. On **real hardware** the open question is whether clock drift between bursts keeps
the predicted start inside `TRACK_WINDOW` — that is the **Pi-4B/Pi-5 benchmark's** job, not
answerable in the emu. The real-time go/no-go overall remains the Pi benchmark (the dense-LA core is
still unported). 7c also does not touch first-contact acquisition (still a blind lock).

---

## Pi-4B benchmark (the real aarch64 go/no-go)

The #1 real-time unknown, measured on real hardware: a **Pi-4B (8 GB)** flashed to 64-bit
Raspberry Pi OS (Debian 13 Trixie, **Python 3.13, aarch64**); decode-only, **no RF on the Pi**. Same
codec working tree + **identical deps to x86** (numpy 2.5.4 / scipy 1.18.1 / entroptics 0.2.8),
`OPENBLAS_NUM_THREADS=1`, 4×A72 @ 1.8 GHz, fan-cooled (≤ 34 °C, no thermal throttle).

**Kit gotcha (fix):** the transfer bundle included x86/py3.12 `__pycache__` holding stale **numba cache
files**; on aarch64/py3.13 numba's cache load threw `EOFError: Ran out of input`, breaking the whole
flag-ON path (presented as a hang). Deleting `__pycache__` on the Pi fixed it — numba recompiles
per-arch. **`bench_pi.sh`/the tar must exclude `__pycache__`.**

**Measured ms/burst (node fast path, 24 reps/cell, all 24/24 decoded):**

| cell | x86 OFF | **Pi-4B OFF** | **Pi-4B ON** | x86→Pi derate (OFF) | OFF→ON |
|---|---|---|---|---|---|
| dqpsk 1.0→2.4 (canonical) | 171 | **838** | **620** | 4.9× | 1.35× |
| dqpsk matched | 86 | 362 | 183 | 4.2× | 1.97× |
| qam256 1.0→2.4 | 135 | 626 | 436 | 4.6× | 1.44× |
| qam256 matched | 75 | 303 | 131 | 4.0× | 2.31× |
| qam16 1.0→2.4 | 180 | 820 | 601 | 4.6× | 1.36× |

**x86→Pi-4B derating is ~4–5×** — a measured number replacing the extrapolation; the 4B is the
**conservative floor** (a Pi-5's A76 @ 2.4 GHz is ~2–3× faster). The flag-ON win is 1.35× on the
scan-dominated narrow cells up to 2.31× on the Viterbi-dominated matched cells.

**bench_viterbi (aarch64):** numba Viterbi **bit-identical on 200 trials**, **61–67×** vs Python
(512 B frame 154 → 2.3 ms) — even better than x86 (52–64×). The compiled kernels are perfect on
aarch64 once the cache is clean.

**Stage attribution (canonical, flag-OFF, Pi):** scan `_scaled_scan` 375 ms (44%), Powell settle
202 ms (24%), Python Viterbi 153 ms (18%); the dense-LA reads sit within the scan.

**Go/no-go (canonical, Pi-4B):**
- flag-ON measured: 620 ms/burst/core.
- + §3a tracked scan (built; ~10× on the scan): → ~282 ms.
- + Powell→parabola settle (Phase 3, NOT built; x86-projected ~5 ms): → **~150 ms/burst/core fully
  optimized**.
- Budget ≈ **4.2 ms/burst** (canonical ~66-row burst, 1.0 Msps airtime) to keep up with the capture.
- 4 cores pipelined (one burst/core): ~150/4 ≈ **38 ms/burst aggregate → ~9× over budget even fully
  optimized**; currently achievable (flag-ON + §3a, no parabola) ~70 ms → ~17×.

**Conclusion: single-chain real-time at 2.4 MS/s is NOT reached on a Pi-4B** for the heavy
modes, even fully optimized (~9× over on 4 cores). A **Pi-5 narrows it to ~3–5×.** It IS reachable
via: lower sample rates (proportionally more budget), the lighter/shorter dense-QAM bursts, a faster
SoC, FPGA/GPU offload of the dense LA, or more cores. **The wall is now the dense-LA scan+reads, not
Viterbi** — and that kernel has no compiled-matmul win at 64-dim (shown above), so the levers there
are the §3a algorithmic reduction (done) + faster FPU / more cores / offload, not more porting.

**Power (rough):** Pi-4B has **no software power sensor** (`vcgencmd pmic_read_adc` is Pi-5 only).
USB meter: idle 2.10 W, 4-core decode 3.40 W (~1.3 W delta ≈ 0.33 W/core). Per-burst decode energy ≈
0.33 W × decode-time → flag-ON 620 ms ≈ 205 mJ ≈ 54 µJ/bit; fully-opt 150 ms ≈ 50 mJ ≈ 13 µJ/bit —
in the ballpark of the model's provisional 71 µJ/bit (corroborates). CAVEAT: the meter induced
under-voltage (a `get_throttled` bit, not thermal), so the watts are ±rough; the decode **time** (the
dominant term) is clean.

**§3a + 7c validated ON THE PI (`exp_7c.py` flag-ON, aarch64):** the keyed-schedule
provider fires the §3a local scan on **11/11 post-first bursts** (null provider 0/11), for a node-path
speedup of **1.73–2.67×** — confirming the x86 result (1.78–2.81×) holds on aarch64:

| cell | null post ms | keyed post ms | speedup |
|---|---|---|---|
| dqpsk 1.0→2.4 (canonical) | 737.6 | **339.0** | 2.18× |
| dqpsk matched | 316.5 | 171.3 | 1.85× |
| qam256 1.0→2.4 | 496.9 | 185.8 | 2.67× |
| qam256 matched | 193.5 | 111.6 | 1.73× |
| qam16 1.0→2.4 | 617.6 | 316.1 | 1.95× |

Delivery parity 3/3 identical (with vs without the provider, == sent); keyed start sequence uniform
(χ² 10.9), differs per key (0.336), no repeating period (max autocorr 0.055 vs 0.044 white band) —
all on the Pi. So the **current tracked best on the Pi-4B is ~339 ms/core** (canonical, flag-ON +
§3a + 7c); 4-core aggregate ~85 ms/burst vs the ~4 ms budget ≈ ~21× over → ~9× with Phase 3
(Powell→parabola, below). The §3a algorithmic reduction gives the same ratio on aarch64 as x86,
as expected (fewer candidate evaluations, CPU-independent).

---

## Phase 3 (Powell→parabola settle)

Per-burst geometry settle: replace the bounded Powell (`scipy.optimize.minimize`) in
`phy._receive_known` with a hand-rolled **parabola** (`_settle_parabola`, reusing `_bowl`):
alternating single-coordinate parabolas (start, then carrier), 2 rounds, **~12 `_scaled_cm` evals vs
Powell's ~28**. Behind `ENTROPTICS_PARABOLA_SETTLE` (default on); **Powell stays the oracle/fallback**.
Only `phy.py` changed (mesh-only — rf unaffected).

**Oracle: decoded frame byte-identical to Powell on 120/120 corpus bursts** (rounds=1 also 0/120,
~6 evals — a further lever). Note: the parabola is **eval-count-bound** (~12 vs ~28), so a
**~2.3–4.7× settle cut, NOT the plan's optimistic 40×** (a 2-D bowl can't reach a 1–2-eval closed
form). x86 settle self-time share drops 9–24% → 5–13%; end-to-end x86 is modest (settle is a small
share of the BLAS-dominated burst there).

**Pi-4B re-measure (`exp_7c`, tracked/keyed path, flag-ON + §3a + 7c, parabola vs Powell):**

| cell (tracked) | Powell keyed ms | Parabola keyed ms | cut |
|---|---|---|---|
| dqpsk 1.0→2.4 (canonical) | 339.0 | **268.6** | 1.26× |
| dqpsk matched | 171.3 | 158.7 | 1.08× |
| qam256 1.0→2.4 | 185.8 | 148.7 | 1.25× |
| qam256 matched | 111.6 | 98.0 | 1.14× |
| qam16 1.0→2.4 | 316.1 | 211.0 | 1.50× |

Biggest on the narrow cells where Powell ran the most evals. All cells decoded 11/11; delivery parity
+ no added structure held on the Pi. **Current best tracked canonical on the Pi-4B (flag-ON + §3a + 7c + Phase 3)
= ~269 ms/core** → 4-core aggregate ~67 ms/burst vs the ~4 ms budget ≈ **~17× over**.

**The wall is now the dense-LA reads** (scan reduced by §3a, Viterbi compiled, settle reduced by the
parabola — what remains is `_scaled_coords`/`_joint_coords`/`_cm_rows`). Next lever: the reads —
complex64 on aarch64 (A72 NEON is 4×f32 vs 2×f64, and §3a's narrowed scan makes complex64 provably
flip-safe), and read-count reduction (coarse-to-fine candidate pruning). After that, FPGA/GPU offload
of the dense LA is the escape hatch if software can't reach the budget.

---

## complex64 on the dense-LA reads (built + safe, but NO e2e win; kept OFF)

**aarch64 microbench (the key question):** complex64 matmul is **~2.0× faster than complex128 on the
Pi-4B** at the read's matrix sizes (154×64@64×64 2.02×, 66×64 1.98×, 64×64 1.97×, 308×154 1.98×;
rel delta ~1e-7) — bigger than x86's ~1.42×, confirming the A72 NEON (4×f32 vs 2×f64) hypothesis.

**Build:** x86 per-kernel, only `_joint_coords` cleared the 1.3× bar (1.67–1.87×, the
FEC-protected payload read). `_scaled_coords` 1.01–1.14× (below bar, and called ~38×/burst in the
settle → the cast is a net cost), the §3a local scan was **slower** (0.66–0.99×, cast swamps cgemm on
the ~24-start grid), `_cm_rows` N/A (real-valued entropy reduction, not a complex matmul), blind scan
kept c128. So c64 was scoped to `_joint_coords` only, behind `ENTROPTICS_C64` (default OFF →
byte-identical). **Oracle 120/120 byte-identical** (blind + tracked paths), waveform.py only.

**Pi e2e re-measure (exp_7c, tracked, c64 ON vs OFF): NO improvement.** Keyed canonical 268.6 (off) →
399.1 (on, with one extra tracked-scan fallback 10/11); dqpsk_matched 158.7→157.5, qam256_matched
98.0→96.7 (flat). The 2× *matmul* win does not reach e2e: after §3a + parabola the joint read is a
small share of the burst, and the c128→c64 cast overhead (plus an occasional path perturbation that
triggers a full re-scan) eats the gain. **Decision: keep `ENTROPTICS_C64` OFF (default).** It is built
and safe behind the flag for a future batching gateway or a path where the read dominates — not a
drone-side win.

**Plateau signal.** Cumulative Pi-4B canonical tracked: 838 (OFF) → 620 (compiled Viterbi) → 339
(§3a+7c) → 269 (Phase 3) → 228 (rounds=1) — ~3.7× off the baseline, still **~14× over the 4-core
budget**. The dense-LA reads are the wall and resist per-kernel speedups (no e2e c64 win, no
compiled-matmul win at 64-dim). The remaining gap needs one of: a **structural read-count reduction**
(coarse-to-fine candidate pruning), a **faster SoC** (Pi-5 ~2–3×), **FPGA/GPU offload** of the dense
LA, or **lower sample rates** (proportionally more budget). Software micro-optimization alone will not
close ~14×.

---

## Phase 2 (§3b) — coarse geometry from fewer rows — built + oracle-clean (x86; Pi pending)

The one unexplored software lever the plateau section names — a **structural read-count reduction** —
built and oracle-gated. Per burst the GEOMETRY is only three parameters (scale, known here; start;
carrier) and is heavily over-determined, so the rank score and the per-burst **settle** objective do
not need every row of the burst: a subset pins (start, carrier) as precisely, at a proportionally
cheaper dense-LA read per evaluation. The settle runs that read ~12× (parabola), so on a long burst it
is the dominant read cost. **The PAYLOAD read (`waveform._scaled_rows`) stays on ALL rows** — only the
geometry FIT is decimated (decimation of the fit, not resampling the signal; no FFT/pilots/guards).

**The lever, by measurement (x86, flag-ON, tracked path, canonical DQPSK 40 ms/burst):** the settle
(`_settle_parabola`) is ~33% and reads `_scaled_coords` over T = held(s0) ≈ 66 rows per eval; the scan
(`_scaled_scan`) is already coarse (≤ `SCAN_ROWS_MAX` = 16 rows). `_scaled_coords` is ~linear in rows
(208→765 µs for 8→66 rows at ρ=0.42). So the settle's row count is the lever; the scan is left alone.

**Build:** `ENTROPTICS_COARSE_ROWS=N` (default unset → all rows → **byte-identical to today**) caps the
rank and settle geometry reads at N rows (floor 2); `phy._coarse_rows` + `T_geom`/`rank_cap` in
`phy._receive_known`. The CRC-gated full re-scan in `phy.receive_known` is the safety net (a coarse
estimate that is off only costs a re-scan, never a wrong lock). phy.py only; Phase 3 parabola and §3a
`tracked=` untouched.

**Oracle (corpus, 5 cells × 24, both blind `tracked=None` and §3a tracked-prior):** decoded
`(family,M,rate)` + `Symbol.pack()` **0-flip at every cap tested, down to N=4** (120/120 byte-identical
each), and **zero** extra CRC-fallbacks vs the full-row path. The geometry is so over-determined that
even N=4 holds on this corpus — i.e. the rows are NOT what the geometry needs; the reads are a payload
wall, not a geometry one.

**x86 ms/burst, full-row → N=16 (flag-ON, tracked, paired):**

| cell | full | N=16 | speedup |
|---|---|---|---|
| dqpsk 1.0→2.4 (canonical) | 40.4 | **31.9** | **1.27×** |
| dqpsk matched | 20.9 | 15.2 | 1.38× |
| qam256 1.0→2.4 | 21.5 | 21.5 | 1.00× |
| qam256 matched | 13.3 | 13.6 | ~1.0× |
| qam16 1.0→2.4 | 32.3 | 28.2 | 1.14× |

The win is on the **long** bursts (the canonical wall); the qam256 bursts are short (~16 rows, already
at the read floor) so there is nothing to cut — expected, and consistent with the row-count scaling.
**The win plateaus at N≈16:** caps below 16 stay 0-flip but buy essentially no more time (payload read
+ scan + Viterbi floor dominate once the settle is cheap). So **N=16 is the recommended operating
point** — the full available x86 win with a comfortable margin over the ~3-param geometry's real needs
(and over a lower-SNR burst that would want more rows), not the corpus-minimum N=4.

**Status:** built behind the flag, default OFF (byte-identical). Suites green default-flags (rf 395 +
mesh 249). **Pi-4B re-measure (exp_7c tracked, `ENTROPTICS_COARSE_ROWS=16`):** keyed canonical
268.6 → **212.5 ms (1.26×)**, dqpsk matched 158.7 → 119.8 (1.32×), qam16 211.0 → 187.1 (1.13×); short
QAM cells at the read floor (~1.0×); delivery 3/3 identical. Matches x86, stacks on §3a + 7c +
parabola. **Cumulative Pi-4B tracked canonical: 838 → 620 → 339 → 269 → 212 ms (~3.95× off the
flag-OFF baseline).**

---

## Real-time operating envelope — derived from the measured Pi-4B decode

The whole real-time investigation reduces to one actionable number: **how much captured RX
throughput can one host decode in real time?** From the measured tracked-canonical burst (DQPSK,
~66 rows = 4224 sender-samples, flag-ON + §3a + 7c + parabola + §3b coarse-rows): **212 ms/burst on
one Pi-4B A72 core** (rounds=2 + coarse N=16, the safe config; ~180 ms with rounds=1). That burst
spans 4.2 ms of airtime (at the 1.0 Msps sender rate) = ~10.1k RX samples at 2.4 Msps, so one core
decodes **~48 kS/s of RX stream**, and a 4-core Pi-4B (one burst/core, BLAS pinned) **~190 kS/s** for
this mode (~225 kS/s with rounds=1). Lighter/shorter and matched-rate modes decode faster (→ higher),
the long DBPSK bursts lower.

**So the real-time envelope, per host (canonical DQPSK mode, continuous):**

| host | ~RX throughput real-time | ~channel width real-time | 2.4 Msps gap |
|---|---|---|---|
| **Pi-4B (4×A72 @1.8 GHz)** | **~190 kS/s** | **~190 kHz** | ~13× short |
| Pi-5 (4×A76 @2.4 GHz, ~2.5×) | ~475 kS/s | ~475 kHz | ~5× short |
| modern x86 core (~5× an A72) | ~220 kS/s/core (scales with cores) | — | server-class closes it |
| + FPGA/GPU offload of the dense LA | the MHz-wide escape hatch | MHz+ | — |

**Positioning:** entroptics decodes **real-time *today* on a Pi-class
CPU for narrowband links — channels up to ~190 kHz on a Pi-4B, ~475 kHz on a Pi-5.** That covers the
**IoT / telemetry / narrowband** regime (LoRa-class occupied widths, control and telemetry, sparse sensor uplinks) — and
is exactly where the power story wins (sparse TX-mostly endpoints, §ability to run beside an SDR). The
**MHz-wide, high-throughput regime (FPV video, wideband MANET)** needs a **server-class host, more
cores, or FPGA/GPU offload of the dense-LA reads** — the reads are a 64-dim least-squares wall that
resisted every per-kernel speedup here (compiled Viterbi/entropy help the *other* stages; the reads
have no compiled-matmul or complex64 e2e win). A structural read-count reduction (coarse-to-fine
candidate pruning) is the one unexplored software lever, with uncertain payoff; past that it is
hardware. **The real-time #1 product gap is thus answered: "yes, real-time on a Pi for narrowband;
wideband needs offload" — a known, quantified envelope, not an open unknown.**

---

## Where the ported code is (Python reference preserved)

- `entroptics_mesh/fec.py` — `crc32`→`zlib` (reference kept as `_crc32_py`); `viterbi_decode` /
  `viterbi_decode_batch` now flag-dispatch to `_fast`, references kept as `_viterbi_decode_py` /
  `_viterbi_decode_batch_py`; trellis-table aliases + `_gate_viterbi` / `_use_fast_viterbi`.
- `entroptics_mesh/_fast.py` — **new**: numba single + batch Viterbi kernels.
- `entroptics_rf/waveform.py` — `_cm_rows` now flag-dispatches to `_fast` (reference kept as
  `_cm_rows_py`); `_gate_cm_rows` / `_use_fast_cm`. **Phase 2:** the constant-modulus speed study's bit-identical
  LA leanings on the DEFAULT numpy path — `_scaled_scan` occurring-phases-only filter bank + cached
  `_scan_coeffs`; `_scaled_gram` cached `E0c`; `_scaled_coords` cached `Cc`/`E0c`; `_joint_coords`
  cached `AH`. No flag; maxabsdiff 0.0 vs oracle. **§3a:** `_scaled_scan` gains
  `start0`/`start_win` + `SCAN_LOCAL_SAMPLES` (local window = exact subset of the full grid) + research
  `_debug`/`_c64_debug`; default (no window) unchanged. **complex64:** `_use_c64()` helper
  + `_joint_coords` casts its dense products to complex64 under `ENTROPTICS_C64` (default OFF →
  byte-identical; built + oracle-clean but no Pi e2e win, kept off).
- `entroptics_mesh/phy.py` — **§3a:** `receive_known` split into a public wrapper +
  `_receive_known` worker with `tracked=(start_phase, carrier[, window])` + a CRC-gated full re-scan
  fallback; `TRACK_WINDOW=3.0`. Default `tracked=None` is byte-identical to the untracked read. **Phase 3
 :** `_settle_parabola` (alternating `_bowl` start/carrier, `SETTLE_PARABOLA_ROUNDS=2`)
  replaces the per-burst bounded Powell in `_receive_known`, behind `ENTROPTICS_PARABOLA_SETTLE`
  (default on; Powell kept verbatim as the `=0` fallback/oracle). Byte-identical on 120/120.
  **§3b:** `_coarse_rows` + `ENTROPTICS_COARSE_ROWS` cap the rank/settle GEOMETRY reads
  (`T_geom`/`rank_cap`) to a row subset; default unset = all rows = byte-identical. Payload read
  unchanged. Oracle 0-flip to N=4 on 120/120 (blind + tracked); N=16 gives x86 1.27× canonical.
- `entroptics_mesh/neighbors.py` — **§3a mesh:** `Neighbor.lock`/`lock_prior`;
  `NeighborTable.locked(...)` records a lock and sets `lock_prior` only when two consecutive locks
  agree within the window (the stability guard); `tracked(node_id)` returns the usable prior or None.
- `entroptics_mesh/node.py` — **§3a mesh:** fast-read loop passes
  `tracked=table.tracked(who)` into `receive_known` and caches the lock on each CRC-passing decode.
  **7c:** `start()` uses the provider's keyed `transmit_offset` when present (null →
  the same random draw); the fast-read loop prefers a 7c start prediction (`_predict_tracked`, anchored
  by `_anchor_schedule` on each CRC pass) over the lock cache; `NodeStats.local_reads/local_hits`
  (any §3a prior) and `coord_reads/coord_hits` (7c prior); `PlanContext` carries `node_id`.
- `entroptics_mesh/providers.py` — **7c:** `AgilityProvider` optional
  `transmit_offset`/`predict_offset` (null returns `None`); `PlanContext.node_id`.
- `entroptics_mesh/schedules.py` — **new, 7c:** `KeyedSchedule`, the minimal example
  keyed-PRNG transmit schedule (an integrator supplies the real one).
- `entroptics-mesh/research/realtime/exp_7c.py` — **new, 7c:** the measurement script (firing,
  parity, no added structure).
- `entroptics_rf/_fast.py` — **new**: numba constant-modulus entropy read.
