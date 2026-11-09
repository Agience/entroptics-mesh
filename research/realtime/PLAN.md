# Real-time receive path — scope, profile, and phased port plan

This is a
**research / measurement / design** pass: it profiles `phy.receive_known` and the mesh per-slot
work, identifies the hot kernels, ports **one** of them (the Viterbi decoder) to numba as a
proof of concept, and lays out a phased plan. **No codec was rewritten.** The scripts and the
frozen PoC live under `research/realtime/`; nothing in `entroptics-rf` or `entroptics-mesh`
source was touched.

All measurements below are on the development box — **Intel i7-1185G7 (Tiger Lake, 4C/8T, x86-64),
numpy 2.4.4 + scipy-openblas, single BLAS thread** — over the mesh emulator air channel (the same
path that produced the 187 ms node figure in `research/mcs/kL.md` and
`research/scenarios/settle_tolerance.txt`). Reproduce with:

```
PYTHONPATH="src;<entroptics-rf>/src" OPENBLAS_NUM_THREADS=1 \
    python research/realtime/profile_receive.py          # profile, all cells
cd research/realtime/poc && python bench_viterbi.py       # PoC: numba Viterbi vs reference
```

Raw output is checked in as `research/realtime/profile.txt`. **Absolute ms in `profile.txt` are
~1.5–2× inflated** — it runs all five cells + cProfile back to back and the local box is contended;
only ratios and the cProfile self-time breakdown
are reliable there. The §1 / §2 ms-per-burst tables are from **isolated single-cell** runs
(`profile_receive.py <cell>` alone, warm cache, one BLAS thread), which reproduce the 187 ms node-path figure.

**Pi caveat:** there is no aarch64 / Raspberry Pi hardware in this
environment. The dev box is x86, and the shared server is x86 (WSL). Every Pi-5 number in this
document is an **extrapolation** from the x86 measurement and published A76/OpenBLAS throughput
ratios. The go/no-go needs a real run on a Pi 5 — Phase 0 of the plan.

---

## 1. Target — what "real-time" means for a node

A node tunes a band and captures a stream at its own sample rate (the bench runs **2.4 Msps**, the
NESDR ceiling). Each transmission is a **burst** — one `compose` call, read on its own by
`phy.receive_known` once the sender's geometry is known. Real-time = **a node decodes each burst
within its airtime**, so the receive pipeline keeps up with the capture stream and does not fall
behind across back-to-back bursts.

**Airtime per burst** (burst samples ÷ sender rate; burst sizes from `research/mcs/kL.md`):

| burst (mode, frame) | tx samples | airtime @1.0 Msps | airtime @2.4 Msps |
|---|---|---|---|
| DBPSK 1/2 (long, ~131 rows) | 8384 | 8.4 ms | 3.5 ms |
| DQPSK 1/2 (~66 rows) | 4224 | 4.2 ms | 1.8 ms |
| 256-QAM 3/4 (~12 rows) | 768 | 0.77 ms | 0.32 ms |
| ~300-row burst (framing headroom) | ~19200 | 19 ms | 8 ms |

**Budget.** To keep up with a continuous 2.4 Msps stream, per-burst decode must fit the burst's
airtime. Taking the common mid-length frame as the design point: **~2–4 ms/burst** for DQPSK-class
bursts, down to **~0.3–0.8 ms** for dense short QAM bursts, up to **~8 ms** for a 300-row burst.
Equivalently the pipeline must sustain **2.4 MS/s of decoded capture**.

**Throughput target (one statement):** sustain **2.4 MS/s** per receive chain, i.e. decode a
typical ~66-row burst in **≤ ~2–4 ms** on a Pi-class CPU (Raspberry Pi 5, Cortex-A76 4-core;
note x86 for servers/relays).

**The number to beat (measured here, x86, single core):**

| cell (node fast path, `accept=read_frame`) | ms/burst | vs airtime |
|---|---|---|
| **DQPSK 1/2, 1.0→2.4 Msps, +0.3 MHz (canonical node cell)** | **180** | ~43–100× too slow |
| DQPSK 1/2, matched 2.4 Msps | 98 | ~55× |
| 256-QAM 3/4, 1.0→2.4 Msps | 138 | ~180× |
| 256-QAM 3/4, matched | 75 | ~230× |
| 16-QAM 1/2, 1.0→2.4 Msps | 180 | — |

The canonical cell reproduces the 187 ms (161–198 ms) node-path figure. **The Python receive path
is ~45–300× slower than real time**, matching the per-slot figure in the mesh paper (§M-10).

**Scope note.** This budget is for the **steady-state known-geometry read** (`receive_known`), which
runs once per burst. **First-contact blind acquisition** (`receive_burst` / `_discover_scaled`:
blind 5–150 s, width-given 0.2–1.9 s) runs **once per sender** and is out
of the per-burst real-time budget — it can stay Python (Phase plan §5).

---

## 2. Profile — where the per-burst time goes

Two views in `profile_receive.py`: wall-clock stage attribution (wrapped kernels, overlapping
where one calls another) and cProfile **self-time** (non-overlapping, authoritative). Numbers below
are the canonical cell (**DQPSK 1/2, 1.0→2.4 Msps, ~180 ms wall**), per burst, cProfile self-time
unless noted.

| kernel | self ms | share | what it is numerically | arithmetic shape |
|---|---|---|---|---|
| `wf._scaled_scan` | ~63 | 35% | **dense complex matmul** — per-sub-sample-phase filter banks × capture windows, over 5 carrier probes × 64 modes; builds the cached `_scaled_gram` inverse | per phase: `(rows × n) @ (n × C·m)`, n≈L/ρ≈154, C=5, m=16 → many small `cgemm` (64-dim), complex64 |
| `wf._scaled_coords` | ~30 | 17% | **least-squares row read** — ranking candidates + Powell objective: `(X @ E0ᶜ) @ G0⁻ᵀ @ Cc` per row block | `(r × n)·(n × L)·(L × L)·(L × L)`, L=64, ~37 calls/burst |
| entropy `shannon_bits` (+ reduces, in `_cm_rows`) | ~20 | 11% | **transcendental reduction** — `−Σ p log₂ p` over mode energies per row | `(rows × L)` log₂ + sum, L=64; `_cm_null` Dirichlet moments cached |
| `fec.viterbi_decode` | ~33 | 18% | **branchy sequential ACS** — 64-state add-compare-select, Python per-step loop + traceback | n_steps≈3850–4150 steps × 64 states; float64; **inherently sequential** (step t depends on t−1) |
| `wf._joint_coords` (payload read, `_scaled_rows`) | ~10 | 6% | **least-squares + per-row q×q far-field solve** (band-limited neighbour tails) | window `(r × n)`, skeleton q=14, per-row `solve` |
| Powell (`scipy.optimize.minimize`) control + `_run` + misc | ~20 | 11% | optimizer bookkeeping around the objective (the objective itself is the `_scaled_coords`/`_cm_rows` above) | ~1 settle/burst, xtol 1e-4 / ftol 1e-2 |
| de-key / carrier / SHAKE keystream | <1 | <1% | `_dekeyed` (mean gain + `r₀/rₜ`), `_row_carrier` (4th-power lag read), `mode_keys`/`row_radius` SHAKE-256 | **SHAKE is already ~free** — `_unkeys_block` and `mode_basis` are `lru_cache`d, drawn once per (K,key) |

**Grouped:** dense linear algebra + entropy (scan + coords + joint + entropy) ≈ **123 ms (68%)**;
Viterbi ≈ **33 ms (18%)**; Powell/optimizer control + glue ≈ **24 ms (14%)**.

**Per-cell shift of the balance** (stage attribution, `profile.txt`):
- **Matched / dense-QAM** bursts are shorter → the scan's share falls and **Viterbi rises to
  40–48%** (e.g. 256-QAM matched: Viterbi 37 ms of 78 ms).
- **1.0→2.4 (narrow sender)** bursts spend more in the scan (46–51%) because the carrier span is
  searched and the joint read runs.
- **De-keying / SHAKE is not a bottleneck** in steady state — the keystream is cached across bursts
  from the same sender (`phy._unkeys_block`, `wf._mode_basis_cached`). The constant-modulus speed study's "~7 ms/burst"
  SHAKE cost was the *blind* path drawing per call; the fast path already fixed it.

**Mesh per-slot work** beyond `receive_known`: `link.read_frame` runs `fec.decode_packets`, which
batches every (family, alphabet, rate) hypothesis into **one** `viterbi_decode_batch` pass **only
when the first offer fails its CRC**. On the fast path most bursts pass on the first offer, so the
node runs a **single** `viterbi_decode` (not the batch). CRC-32 (`fec.crc32`, pure-Python per-byte
loop) is ~0.1 ms/burst — negligible now but a cheap C/`zlib.crc32` swap later. Fountain
(`FountainDecoder`, big-int GF(2)) is per-**message**, not per-burst, and tiny.

---

## 3. Where the time really is — per-kernel remedy

Five hot kernels. For each: what it is, and the realistic speedup from (a) better numpy/BLAS,
(b) C/Cython/numba, (c) SIMD/NEON, (d) GPU, (e) FPGA, (f) algorithmic change. **Entroptics-pure
constraint holds throughout:** every substitution must compute the *same reads* (constant-modulus
geometry, joint/band least-squares, 4th-power carrier, max-log LLRs, Viterbi) — no FFT/pilots/
equalizers/resampling in the signal path. FFT-structured BLAS is forbidden; a plain `cgemm` is a
faithful implementation of the projection the read already is.

### 3a. `_scaled_scan` — the #1 cost (35–51%)
Numerically: projection of capture windows onto the first 16 aperture modes at every quarter-sample
start and 5 carrier probes, then the per-row entropy deficit. It is **BLAS-able dense complex
matmul** on **small (64-dim) matrices**, so it is dispatch/overhead-bound, not FLOP-bound (the
microbench hits 74 GFLOP/s single-thread on one `cgemm`, but each scan block is tiny).
- (a) numpy/BLAS: the constant-modulus speed study already measured **1.5–1.7×** bit-identical from leaner axis paths +
  complex64 + cached grams + one product per round. **Threaded BLAS does NOT help** — on 64-dim
  matrices default OpenBLAS threads made the whole path *2× slower* (351 vs 184 ms), so pin
  `OPENBLAS_NUM_THREADS=1` and parallelize across bursts/cores instead.
- (b) C/Cython: fuse the per-phase product + entropy reduction into one pass, keep the cached
  gram — removes Python dispatch over ~37 small ops. Expect **5–10×**.
- (c) SIMD/NEON: the inner product is complex MAC; A76 NEON (128-bit, 2×f64 or 4×f32) and
  OpenBLAS's A76 `cgemm` kernel vectorize it. Folds into (b)'s compiled path.
- (d) GPU: small matrices, per-burst latency-bound → PCIe/launch overhead dominates; a win only if
  **many** bursts are batched (a gateway, not a drone). Not for a Pi.
- (e) FPGA: the projection is a fixed filter bank → maps well to a systolic array / DSP slices on
  the SDR's FPGA fabric; highest performance, highest effort, last resort.
- (f) **Algorithmic:** on the *known-geometry* path the carrier is already known to ±0.2 cyc/row,
  so the 5-probe span and quarter-sample grid can shrink to a local search once a sender is tracked
  (today it re-scans every burst). Biggest single lever after the compiled port.

### 3b. `_scaled_coords` / `_joint_coords` — least-squares row reads (17% + 6%)
**BLAS-able dense linear algebra**: matmuls against the cached `_scaled_gram`/`_joint_gram`
factorizations plus a small per-row `q×q` solve (q=14) in the joint read. (a) leaner numpy 1.5×;
(b)/(c) a compiled, NEON-vectorized fused kernel **5–8×**; the grams are already closed-form
(`_dirichlet_gram`) and cached, so no factorization in the loop. GPU/FPGA as 3a. No algorithmic
change needed — the read is already the minimal least-squares.

### 3c. entropy `shannon_bits` in `_cm_rows` (11%)
**Transcendental reduction** (`−Σ p log₂ p`). The constant-modulus speed study measured a lean in-place path at **1.6×**
bit-identical (425→265 ms per 14M elements; `log₂` alone is half the cost). (b)/(c) a C/NEON kernel
with a vectorized `log2` (or a polynomial approximation validated against the library moments)
**3–5×**. This is a **generic entroptics-core enhancement** (`entropy.shannon_bits(axis=)`), not
RF-specific — fits the "core may be updated surgically" rule and benefits every repo.

### 3d. `fec.viterbi_decode` — branchy sequential ACS (18–48%)
**Not BLAS**: a 64-state add-compare-select with a hard sequential dependency (step t needs t−1),
so numpy can only vectorize *across states within a step* (it does) — the per-step Python loop is
irreducible in pure Python. This is the textbook case for a compiled language.
- (b) **numba/C: measured 52–64× here (PoC §4), bit-identical.**
- (c) SIMD/NEON: the 64-state ACS is a natural 128-bit vector op; folds into the compiled kernel.
  (The existing `viterbi_decode_batch` already exploits the trellis symmetry — a C port inherits
  it for the multi-hypothesis case.)
- (d)/(e) GPU/FPGA: standard Viterbi IP exists but is overkill; the numba/C port already removes
  the kernel from the budget.
- (f) none needed — it is already ML-optimal; K=7 is fixed by the wire format.

### 3e. Powell settle + optimizer control (~14%)
The objective *is* 3a/3b (already counted); the ~14% here is scipy's Powell bookkeeping and the
`_run` scan statistic. (f) **Algorithmic:** `settle_tolerance.txt` already adopted ftol 1e-2
(362→187 ms). The constant-modulus speed study recommends settling only the shortlist winner and a fixed 2-parameter
parabola in place of bounded Powell (the `_bowl` helper already does this for the coherent re-read).
Expect the settle control to drop to ~5 ms with a hand-rolled parabola — no scipy. **Determinism
risk** (§5): Powell's float path must be reproduced exactly or the Python oracle no longer matches.

---

## 4. Proof of concept — Viterbi ported to numba, bit-identical

**Kernel chosen:** `fec.viterbi_decode`. Rationale: it is the cleanest bit-exact win (pure integer
output, trivial to verify identical), 18–48% of the path depending on mode, fully self-contained,
deterministic (no Powell/float-ordering hazard), and it proves the compile toolchain end to end.
(The scan is the larger #1 cost but is BLAS-bound and harder to make bit-identical through the
Powell objective — it is Phase 2, not the PoC.)

**Files:** `poc/viterbi_ref.py` (frozen copy of the fec kernel = oracle), `poc/viterbi_numba.py`
(`@njit` port, same trellis tables, same strict-greater tie rule, same −1e300 floor and
per-step renormalization), `poc/bench_viterbi.py` (identity check + timing).

**Result (x86, single thread, `bench_viterbi.py`):**

```
identical on 200 trials: YES          (random scales 0.3–6.0, erasure rates 0–40%, 5 frame sizes)

frame   64 B  (  566 steps):  ref  8.20 ms   numba 0.13 ms   speedup 64.3x
frame  475 B  ( 3854 steps):  ref 55.82 ms   numba 0.92 ms   speedup 60.7x
frame  512 B  ( 4150 steps):  ref 63.22 ms   numba 1.21 ms   speedup 52.4x
```

**Bit-identical output on every one of 200 adversarial inputs** (including all-erasure and near-tie
soft values), and **52–64× faster**. The 512-byte frame Viterbi drops from **63 ms → 1.2 ms**.

**Extrapolation to the full path:**
- Replacing Viterbi alone on the canonical cell: 180 ms − ~32 ms + ~1 ms ≈ **149 ms** (x86).
- On matched 256-QAM (Viterbi 48%): 78 − 37 + 0.7 ≈ **42 ms**.
- Project the same class of compiled win onto the rest: scan+coords+joint+entropy ≈ 123 ms at a
  conservative **8×** (compiled fused NEON kernel, cached grams, single-thread BLAS) → ~15 ms;
  Powell control ~20 ms → ~5 ms with a hand-rolled parabola; Viterbi ~1 ms. **Fully-ported x86
  per-burst ≈ 20–25 ms** → ~**7–9× over the 180 ms baseline**.

**Does a full port hit the budget on a Pi 5?** Derate x86→Pi-5: Cortex-A76 @ 2.4 GHz single-core is
~⅓–½ of this Tiger Lake core for compiled scalar+NEON, and OpenBLAS `cgemm` on A76 is ~5–10
GFLOP/s/core vs ~74 here (~8–15× on the BLAS parts). So a fully-ported **~22 ms x86 burst → ~60–120
ms on one A76 core** — that does **not** meet a 2–4 ms single-core airtime budget. **But** the Pi 5
has **4 cores** and bursts are independent: pinning one burst per core and keeping BLAS
single-threaded gives ~4× aggregate, and the §3a algorithmic scan reduction (local carrier search
on a tracked sender) can take another 2–4× off the dominant kernel. That brings aggregate
throughput into the **~2.4 MS/s for the lighter modes** range, tight for the long DBPSK bursts.

**Go/no-go:** see §5. The PoC proves the toolchain and the Viterbi; a real Pi-5 measurement
(Phase 0) is required before committing to strict per-burst airtime on a single core.

**Update (Phase 1 + entropy now productionized and measured — see §4a and `port_results.md`):** the
Viterbi is landed in `fec.viterbi_decode`/`viterbi_decode_batch` and the entropy reduction in a
compiled `_cm_rows`, both behind `ENTROPTICS_FAST` and bit-identity-gated. The extrapolation's
"Viterbi ~1 ms" and "entropy compiled" terms are now **measured** (51× and 1.4–3.9× isolated); the
scan+coords LA term (the dominant 8× lever) is **still a projection** — that port is Phase 2's bulk
and was deliberately not attempted as a numba matmul (not bit-identical to BLAS; see §4a).

---

## 4a. Phase 1 / Phase 2(entropy) — measured

Full detail, oracle evidence and the per-cell table are in `research/realtime/port_results.md`.
Headlines (dev x86 box, one BLAS thread, numba 0.68):

- **Ported, oracle-gated, behind `ENTROPTICS_FAST`** (Python reference kept and default whenever a
  gate is short of exact): `fec.crc32`→`zlib` (exact); `fec.viterbi_decode` + `viterbi_decode_batch`
  numba (**bit-identical**, 200+60 adversarial trials); `waveform._cm_rows` numba entropy
  (**≤1e-9**). **Decoded frame byte-identical ON vs OFF on all 120 corpus bursts.**
- **Isolated:** Viterbi **51×** (25.7→0.50 ms, 512 B frame); `_cm_rows` **1.4–3.9×**.
- **Per-burst (paired OFF→ON):** canonical DQPSK **171.6→135.3 ms (1.27×)**; dqpsk matched
  86.2→52.8 (1.63×); 256-QAM matched 73.2→33.2 (**2.20×**). Gain tracks the Viterbi's per-cell
  share, as profiled.
- **Remaining:** flag ON, the dense complex LA (`_scaled_scan`+`_scaled_coords`+`_joint_coords`) is
  now **~73 % of self-time** — the two non-BLAS hot kernels (Viterbi, entropy) are out of the
  budget; the BLAS "68 % core" is the whole remaining cost and the real Phase-2 effort.
- **Not ported:** the LA matmuls stay numpy — a numba matmul is not bit-identical to
  OpenBLAS `cgemm` (the scan's argmax can flip on a sub-ulp change) and would not beat BLAS at
  64-dim. The win there is a **fused** compiled kernel removing Python dispatch (plan §3a (b)),
  the 1–2-month Phase-2 body, not a drop-in.

---

## 5. Phased port plan

Ordering = most ms saved per unit effort, cheapest risk first. **The Python reference stays the
oracle**: each ported kernel keeps the exact signature of the function it replaces and is gated in
CI by `np.array_equal` (integer kernels) or a tight tolerance (float kernels) against the live
`entroptics-rf`/`entroptics-mesh` function on a fixed corpus of captures. A ported kernel ships
behind a flag so the Python path remains runnable.

**Phase 0 — Pi-5 baseline + toolchain.** Run `profile_receive.py` and `bench_viterbi.py`
**on a Raspberry Pi 5** (needs the hardware — flagged). Pin `OPENBLAS_NUM_THREADS=1`,
confirm the A76 OpenBLAS kernel is present, measure the real x86→Pi derating. This replaces every
extrapolation in §4 with a number. Decide single-core vs 4-core-pipelined target from it.

**Phase 1 — Viterbi (DONE, productionized — §4a, `port_results.md`).** numba decode landed behind
`fec.viterbi_decode` / `viterbi_decode_batch`, oracle-gated (bit-identical on 200+60 adversarial
trials), flag `ENTROPTICS_FAST`. `fec.crc32` swapped for `zlib.crc32` (exact). **Saves 18–48%** of
the path (measured: 256-QAM matched 2.2×, canonical 1.27×). A small C extension + ctypes remains the
alternative if numba on aarch64 is unwanted (decide in Phase 0).

**Phase 2 — scan + coords + entropy (the 68% core; ~1–2 months).** Port `_scaled_scan`,
`_scaled_coords`/`_joint_coords` and the entropy reduction to a fused C/Cython + NEON kernel,
keeping the cached `_scaled_gram`/`_joint_gram`/`_mode_basis_cached` interfaces. Start with the
constant-modulus speed study's bit-identical numpy leanings (1.5–1.7×, already specified) as the safe first step, then
the compiled kernel. The entropy path is a **generic entroptics-core** change
(`entropy.shannon_bits(axis=)`), reported per the surgical-core rule. **Biggest single win.**

**Status:** the entropy reduction is **done** as an rf-local compiled `_cm_rows` (numba, gated
≤1e-9, end-to-end identical on the corpus; core left untouched, acceleration of
`shannon_bits(axis=)` reported as a recommendation). The scan/coords LA and the constant-modulus speed study's numpy
leanings are **not yet done** — the bulk of this phase.

**Phase 3 — settle / optimizer (algorithmic).** Replace bounded Powell with the
hand-rolled parabola settle (`_bowl`-style) already used for the coherent re-read, and the §3a
local carrier search for tracked senders. Pure-Python-replaceable but **determinism-sensitive**:
the reference's Powell path must be frozen as the oracle, or the two diverge on near-flat
objectives.

**Phase 4 — integration + scheduling.** Pipeline: one burst per core, BLAS pinned to one
thread, lock-free handoff from the capture ring. This is where aggregate 2.4 MS/s is actually met
(or not) on 4 A76 cores. GPU/FPGA only if a gateway/relay must batch many simultaneous streams
(§3a-d/e) — not on a drone.

**Stays Python (not on the per-burst budget):**
- **First-contact blind acquisition** (`receive_burst`, `_discover_scaled`, `_period_read`): 5–150 s
  blind, run **once per sender**; the constant-modulus speed study has its own speedups but it is not real-time-critical.
- **Fountain / routing / plausibility / adapt**: per-message or per-tick, already sub-ms.

**Must be C (per-burst steady state):** everything in `receive_known` on the fast path — the scan,
the row/joint reads, the entropy, the Viterbi, the settle.

### Risks
- **Determinism:** Powell's float path (Phase 3) and any `fastmath` in compiled kernels can break
  bit-identity with the oracle. Mitigation: `fastmath=False` (the PoC uses it), freeze the settle
  as oracle, gate every kernel against the live Python on a capture corpus.
- **SHAKE keystream:** *not* a per-burst bottleneck in steady state (cached), but a streaming
  receiver seeking deep into a keystream pays per-block SHAKE-256 — keep the `_KEY_BLOCK` seek and
  the `lru_cache`; budget it in Phase 4 for long-running links.
- **Pi memory / thermal:** the cached grams/apertures are small (≤ a few MB per (L,ρ)); the risk is
  thermal throttling under sustained 4-core decode on a passively-cooled Pi — Phase 0 must measure
  with a realistic duty cycle, not a cold burst.
- **aarch64 toolchain:** numba/llvmlite and a NEON C path both exist on aarch64, but verify on the
  exact Pi OS in Phase 0 before committing to numba vs a hand-written C extension.

---

## Files

- `research/realtime/_bursts.py` — representative bursts + emu-air captures (matched & 1.0→2.4,
  DQPSK/coherent), the inputs the node reads.
- `research/realtime/profile_receive.py` — correctness + ms/burst, cProfile self-time, stage
  attribution.
- `research/realtime/profile.txt` — captured output of the above.
- `research/realtime/poc/viterbi_ref.py` — frozen oracle copy of the fec Viterbi.
- `research/realtime/poc/viterbi_numba.py` — the numba port (bit-identical).
- `research/realtime/poc/bench_viterbi.py` — identity check + timing.
