# MCS table: coherent square QAM on the keyed modes + K=7 convolutional code

> **Not the planner's table.** This table measures coherent QAM at K = 60/124 with the true
> geometry. `adapt.MODES` takes its thresholds and goodputs from `kL.md`'s
> second table (`results_kL_fp.json`). That table covers both families at K = L through the mesh's
> own receive path, where one coherent gain for every mode puts coherent QAM below the figures
> here (4-QAM 1/2 at 4.4 dB against 6.7) and on the planner's envelope from 4-QAM 1/2 up.

Measured in simulation through the real codec:
`fec.encode_packet` -> `waveform.compose_qam` -> channel -> the codec's coherent read
(`_coherent_project`, the joint read at a scale, then `_dekeyed`, which takes the gains from the
single keyed reference row) -> `fec.soft_bits_from_qam` -> `fec.decode_packet`. The frame is the
one the differential sweep (`results.md`) measures, so the two tables compare directly.

Scripts: `mcs_coherent.py` (measurement, phases `coarse`, `fine`, `fine2`, `redo`, `blind`) and
`make_coherent_report.py` (the tables below and `results_coherent.json`). The raw cells are in
`results_coherent_{coarse,fine,fine2,redo,blind,blind_m128crc}.json`, and the merged cells,
thresholds, ladder, envelope and blind rows are in `results_coherent.json`.

## Setup

* **Packet.** 500 payload bytes in `fec.encode_packet`: a 2-byte length, the payload and CRC-32,
  coded with the (171,133) K=7 code (zero tail), punctured to 1/2, 2/3 or 3/4 and interleaved
  over 16 columns. A packet fails if its CRC fails. A CRC pass with the wrong payload also counts
  as a failure and is tallied as undetected; there were none in any cell.
* **Burst.** `compose_qam` writes one keyed reference row, then the coded bits as Gray square
  M-QAM, M in {4, 16, 64, 256}, row-major over the K modes. A short last row uses fewer modes.
* **Conditions.**
  * `m64`: matched rate, L=64, K=60. This is the differential table's geometry.
  * `m128`: matched rate, L=128, K=124.
  * `r128`: sender/receiver rate ratio ρ = 0.514403 through `tests/_ratechannel.at_rate`, the exact
    band-limited channel, at L=128, K=124. The read is the joint read (`research/joint_read.md`
    in entroptics-rf).

  The burst sits between two silent rows on each side.
* **Known geometry.** As in the differential sweep, the table's read is handed the true
  offset/phase/scale and carrier 0. The table therefore measures the modulation, the code and the
  codec's own gain read, not synchronization. The blind spot-check below measures what discovery
  adds.
* **SNR** is the in-band Es/N0 per transmitted complex sample, against the unit-RMS burst. At
  matched rate, complex AWGN of variance N0 = 10^(-SNR/10) is added per sample. At ρ the receiver's
  band is 1/ρ wider, so white noise of variance N0/ρ per received sample puts N0 per transmitted
  sample in the signal's band. This is the SNR the planner predicts (noise over the occupied width).
  `at_rate`'s own `snr_db` would be SNR − 10·log10(1/ρ) = SNR − 2.9 dB.
* **Goodput** follows `mcs_table.py`: G = (1 − PER)·8P / ((T + 1)·L), with
  T = ceil(ceil(Nc / log2 M) / K). Nc is 8108, 6081 or 5406 coded bits at r = 1/2, 2/3, 3/4. G counts the
  reference row, header, CRC, tail, code rate and the partial last row as overhead. The planning
  goodput is the L=64 K=60 figure, the geometry `TxPlan` defaults to and the differential table
  uses. At L=128 a 500-byte packet fills fewer, longer rows, so the reference row and the partial
  last row cost more: 0-13% less goodput at M ≥ 16 (1-2% more at 4-QAM). Bursts are 69/52/47 rows (4-QAM, r = 1/2, 2/3, 3/4),
  35/27/24 (16), 24/18/17 (64) and 18/14/13 (256) at L=64, and 34/26/23, 18/14/12, 12/10/9 and 10/8/7
  at L=128.
* **Packets.** The coarse phase sent 60 packets per cell on a 1 dB grid, stopping after 20 if all
  20 failed. The fine phases sent up to 500 per cell on the waterfall and two points past its
  foot. A fine cell stopped early only once both decodes had 50 failures. Wall time was 21 min
  (coarse, 5 local processes), 13 min (fine, 16 processes on a 22-core server), 9 + 17 min (fine2/redo, 5
  local processes) and 49 min (blind, 16 processes on a 22-core server, plus 2 min locally for the m128 CRC-arbitrated rerun).

## Soft bits: `fec.soft_bits_from_qam`

These are max-log LLRs for Gray square QAM in the rf codec's labelling. A symbol index is
`(gray(i) << h) | gray(q)` with h = log2 √M, so the first h bits (MSB first) are the in-phase axis's
Gray label and the last h are the quadrature's. Each axis is read on its own:
`LLR_b = (min_{s: b=1} (x − s)² − min_{s: b=0} (x − s)²) / N0`. The sign is positive for bit 0, the
module's convention.

**N0 is measured, never given.** `fec.qam_noise_var` is the mean squared decision residual of the
burst's own symbols. With `K=` it is measured per mode, where symbol i is on mode i % K, the order
`compose_qam` writes. A max-log Viterbi is invariant to one common scale, so a single global N0
calibrates the LLRs but cannot change a decision. **Per-mode N0 does change decisions, and it is
the table's decoder.** Every mode's gain is divided by one noisy reference symbol, so each mode
carries its own noise level, and weighting by it recovers part of what that costs:

| | 4-QAM | 16-QAM | 64-QAM | 256-QAM |
|---|---|---|---|---|
| 1%-PER threshold, global N0 → per-mode N0 | −1.0 to −2.3 dB | −0.2 to −0.8 dB | −0.4 to +0.3 dB | −0.3 to +0.4 dB |

The ranges cover the nine (rate, condition) cells per order. At 64/256-QAM the differences are
within the threshold read's own scatter.

Tests: `tests/test_fec_qam.py` (17 cases). They check that the signs are the rf slicer's own decisions
(`waveform._qam_bits`) at M = 4..256 and that noiseless points give their labels with finite LLRs.
They check that 4-QAM's max-log LLR is the exact Gaussian LLR, that N0 is measured within 8% globally
and 35% per mode, and that a packet decodes through `compose_qam` and the codec's coherent read
with nothing about the noise passed. They also check that the per-mode read is mode-by-mode with a
short last row, and that non-square orders are refused.

## Thresholds

PER ≤ 1% thresholds, interpolated linearly in log10(PER) between the bracketing 1 dB grid points.
Each entry is per-mode N0 / global N0. The **planning threshold** is the worst per-mode figure over
the three conditions, because the plan does not know the receiver's rate.

| mode | matched, L=64 K=60 | matched, L=128 K=124 | rho 0.514403, L=128 K=124 | genie gains, m64 / m128 | goodput L=64 | goodput L=128 | planning threshold |
|---|---|---|---|---|---|---|---|
| 4-QAM 1/2 | 6.54 / 8.58 | 6.66 / 9.0 | 6.57 / 8.89 | 3 / 4 | 0.906 | 0.919 | 6.7 |
| 4-QAM 2/3 | 8.79 / 10.34 | 8.55 / 9.76 | 8.51 / 9.8 | 6 / 6 | 1.202 | 1.202 | 8.8 |
| 4-QAM 3/4 | 9.17 / 10.2 | 9.68 / 10.65 | 9.67 / 10.71 | 7.3 / 7.77 | 1.33 | 1.359 | 9.7 |
| 16-QAM 1/2 | 12.28 / 12.7 | 12.35 / 12.77 | 12.26 / 12.68 | 9.93 / 9.9 | 1.786 | 1.736 | 12.3 |
| 16-QAM 2/3 | 14.42 / 15.23 | 14.77 / 15.28 | 14.91 / 15.17 | 11.94 / 12.65 | 2.315 | 2.232 | 14.9 |
| 16-QAM 3/4 | 16.0 / 16.33 | 16.39 / 16.63 | 16.53 / 16.9 | 13.9 / 13.9 | 2.604 | 2.604 | 16.5 |
| 64-QAM 1/2 | 17.69 / 18.13 | 17.92 / 18.35 | 18.49 / 18.34 | 15.31 / 16.2 | 2.604 | 2.604 | 18.5 |
| 64-QAM 2/3 | 20.23 / 20.29 | 20.23 / 20.3 | 21.0 / 20.67 | 16.93 / 17 | 3.472 | 3.125 | 21.0 |
| 64-QAM 3/4 | 21.87 / 22.2 | 21.79 / 22.13 | 21.9 / 21.91 | 18.93 / 18.94 | 3.676 | 3.472 | 21.9 |
| 256-QAM 1/2 | 21.88 / 21.59 | 21.86 / 21.69 | 21.75 / 21.83 | 19.0 / 19.0 | 3.472 | 3.125 | 21.9 |
| 256-QAM 2/3 | 26.49 / 26.78 | 27.0 / 26.58 | 26.51 / 26.55 | 23.9 / 23 | 4.464 | 3.906 | 27.0 |
| 256-QAM 3/4 | 28.13 / 28.17 | 27.9 / 27.9 | 28.23 / 28.07 | 24.42 / 24.93 | 4.808 | 4.464 | 28.2 |

* **Matched and ρ = 0.514 agree.** At equal in-band SNR, the joint read at ρ = 0.514 lands within
  ±0.5 dB of matched rate at the same L=128 for every mode except 64-QAM 1/2 and 2/3 (+0.6 and +0.8
  dB). Its noise gain (1.01-1.05×) and remaining floor cost nothing measurable at these SNRs. L=64 and
  L=128 agree within 0.5 dB.
* **Coherent 4-QAM vs DQPSK.** With per-mode N0, coherent 4-QAM needs 1.5, 0.6 and 1.1 dB less than
  DQPSK at r = 1/2, 2/3 and 3/4 (6.7/8.8/9.7 against 8.2/9.4/10.8). With one global N0 it is 0.4-0.9 dB
  worse at 1/2 and 2/3 and level at 3/4. The reason is the reference row; see the genie column and
  the next section.
* **Coherent 16-QAM vs D16PSK (Gray).** It needs 2.1-3.9 dB less at the same goodput (12.3/14.9/16.5
  against 14.4/18.8/19.8), and it also beats D8PSK at the same goodput (12.3 vs 13.1 dB for 1.786
  b/s/Hz).

## The single reference row costs about 2-4 dB

Every symbol is divided by its mode's gain as read off one keyed reference symbol. That symbol
carries the same noise as a payload symbol, so the payload error is s·e_ref + n, with about twice
the channel's noise variance. At the highest SNRs measured, where the decision-directed read is
unbiased, the measured N0 reaches 1.75-2.2× the channel's N0 at every order and condition. That is the same doubling a differential read pays. The **genie**
column reads the gains off the noiseless capture (m64 / m128 only, same packets and noise). It
shows what a better gain read would buy against the per-mode figures: 1.9-3.5 dB at 4-QAM,
2.1-2.5 dB at 16-QAM, 1.7-3.3 dB at 64-QAM and 2.6-4.0 dB at 256-QAM. The genie's waterfall lies
mostly in the coarse 60-packet cells, so take it to ±0.5 dB. At 4-QAM 1/2 the genie cells near
3 dB hold only 20 packets. At 256-QAM 1/2 the lowest point measured (19 dB) already clears, so
19 is an upper bound.

A gain read that averages over the payload rows would remove most of this without adding anything
to the air. One example is a fourth-power (quarter-turn-symmetric) per-mode gain read over all T
rows, anchored in phase by the reference row, the same device `_row_carrier` already uses. It
would be a receiver change in entroptics-rf, and it was not attempted here.

## Envelope: best goodput at each SNR (PER ≤ 1%)

The differential ladder is the existing sweep: natural labels for M = 2/4 (`results.json`) and Gray
for 8/16 (`results_gray.json`). Its thresholds are recomputed here with `make_report.threshold`.
D8PSK 2/3 reads 13.1 dB by that rule; the old `adapt.MCS` carried 12.7. All goodputs are at L=64 K=60.

| SNR dB | differential only | b/s/Hz | with coherent | b/s/Hz | gain |
|---|---|---|---|---|---|
| 6 | DBPSK 3/4 | 0.679 | DBPSK 3/4 | 0.679 | x1.00 |
| 8 | DBPSK 3/4 | 0.679 | 4-QAM 1/2 | 0.906 | x1.33 |
| 10 | DQPSK 2/3 | 1.202 | 4-QAM 3/4 | 1.330 | x1.11 |
| 12 | DQPSK 3/4 | 1.330 | 4-QAM 3/4 | 1.330 | x1.00 |
| 14 | D8PSK 2/3 | 1.786 | 16-QAM 1/2 | 1.786 | x1.00 |
| 16 | D8PSK 3/4 | 1.953 | 16-QAM 2/3 | 2.315 | x1.19 |
| 18 | D8PSK 3/4 | 1.953 | 16-QAM 3/4 | 2.604 | x1.33 |
| 20 | D16PSK 3/4 | 2.604 | 16-QAM 3/4 | 2.604 | x1.00 |
| 22 | D16PSK 3/4 | 2.604 | 64-QAM 3/4 | 3.676 | x1.41 |
| 25 | D16PSK 3/4 | 2.604 | 64-QAM 3/4 | 3.676 | x1.41 |
| 28 | D16PSK 3/4 | 2.604 | 256-QAM 2/3 | 4.464 | x1.71 |
| 30 | D16PSK 3/4 | 2.604 | 256-QAM 3/4 | 4.808 | x1.85 |

Coherent QAM takes the envelope from 6.7 dB up, apart from D8PSK 3/4 at 14.7-14.9 dB. It lifts the ceiling from 2.60 b/s/Hz (D16PSK 3/4 at
19.8 dB) to 3.68 at 21.9 dB (64-QAM 3/4) and 4.81 at 28.2 dB (256-QAM 3/4). Between 6.7 and 20 dB
the gain is in threshold rather than ceiling: the same goodput is reached 0.6-3.9 dB earlier.
Below 6.7 dB DBPSK remains the only mode. These are known-geometry thresholds; the blind
spot-check below shows how much of them a receiver reading a single burst cold actually gets.

The ladder now in `adapt.MCS` (Pareto-optimal: each entry beats every cheaper one on goodput):

| threshold dB | family | M | rate | goodput b/s/Hz |
|---|---|---|---|---|
| 3.2 | differential | 2 | 1/2 | 0.456 |
| 5.0 | differential | 2 | 2/3 | 0.607 |
| 5.7 | differential | 2 | 3/4 | 0.679 |
| 6.7 | coherent | 4 | 1/2 | 0.906 |
| 8.8 | coherent | 4 | 2/3 | 1.202 |
| 9.7 | coherent | 4 | 3/4 | 1.33 |
| 12.3 | coherent | 16 | 1/2 | 1.786 |
| 14.7 | differential | 8 | 3/4 | 1.953 |
| 14.9 | coherent | 16 | 2/3 | 2.315 |
| 16.5 | coherent | 16 | 3/4 | 2.604 |
| 21.0 | coherent | 64 | 2/3 | 3.472 |
| 21.9 | coherent | 64 | 3/4 | 3.676 |
| 27.0 | coherent | 256 | 2/3 | 4.464 |
| 28.2 | coherent | 256 | 3/4 | 4.808 |

## Blind spot-check

The same capture is read twice. The capture has a carrier (3e-4 cycles/sample matched, 2e-4 at ρ),
two silent rows plus 37 samples before the burst, and two silent rows after it.

* **Informed**: true geometry and carrier.
* **Blind**: `discover(y, key, range(L−4, L+5))` at matched rate (the mesh receiver's width window),
  or `_discover_scaled(y, key, (L,), carrier_span=0.3)` at ρ. Then `_coherent_read` with
  `decode`'s beacon give-back, `decode`'s payload test (fourth-power closure against the
  differential closure) and `_qam_order`.

Both decode with per-mode N0. The SNRs are the per-mode 1%-PER grid point, plus one point below
and two points above it at matched rate, and the grid point plus two above at ρ. There are 10
packets per point at matched rate (m128) and 4 at ρ (r128), since discovery at ρ costs about 7
minutes per capture. A third column, **CRC picks M**, decodes the blind coherent read at every
square order and keeps the one whose CRC passes, ignoring the closures. That is an exact test with
no level, as `phy.receive_known` already leaves to the frame. It ran on the m128 captures only and
gave 0 undetected errors.

| | packets | informed fails | blind fails (`decode`'s rules) | blind fails, CRC picks M |
|---|---|---|---|---|
| m128 4-QAM | 90 | 2 | 49 (34 width/lock, 13 "not coherent") | 49 |
| m128 16-QAM | 90 | 2 | 10 (6 width/lock) | 9 |
| m128 64-QAM | 90 | 3 | 28 (23 M misread) | 14 |
| m128 256-QAM | 90 | 1 | 66 (66 M misread: 47 as 4, 19 as 1024) | 17 |
| r128 4-QAM | 24 | 0 | 23 | |
| r128 16-QAM | 24 | 0 | 10 | |
| r128 64-QAM | 24 | 0 | 17 | |
| r128 256-QAM | 24 | 0 | 22 | |

**Blind does not cost little on a single short burst. Most of the loss is in discovery and the
order read, not in the coherent read.**

* **Matched, 16-QAM.** Blind comes closest to informed here: 0/30 failures across all three points
  for r = 3/4, and 0/10 at +2 dB for every rate. The other losses (10 against 2 overall) are
  width/lock misses at the lower points.
* **The order read fails at coded operating points.** Coded thresholds sit where the raw BER is
  2-6%. That is where `_qam_order`'s lattice closure falls under the 4-QAM rung's structural ~0.14
  (`research/coherent_blind.md`), on a burst of only 676-1352 symbols. 256-QAM is misread in
  66/90 captures, even 2 dB above threshold. Letting the CRC pick M recovers most of it: 256-QAM
  66 → 17 and 64-QAM 28 → 14 failures. In the residual (256-QAM 3/4 on 7-row bursts accounts for
  13/30), no order's CRC passes at all, so there the blind geometry or carrier read itself costs
  dB.
* **4-QAM at matched rate.** Discovery does not lock the width on 23-34-row coherent bursts at 6-12
  dB (34/90 width/lock failures). The payload test also names 13 of them differential.
* **At ρ = 0.514** the cold-start `_discover_scaled` on one burst of 7-34 rows rarely measures the
  scale. The median |scale error| is 1e-2, and only 26/96 captures came within 1e-4. **Where the
  scale was measured (|error| < 1e-4), blind decoded 24/26.** The coherent read, joint read and
  order read cost little once the geometry is right; the cold start is what fails. The mesh's
  per-burst path at a non-unit rate is `phy.receive_known`, a scale measured before; it reads
  both families (the coherent read from the keyed reference row, the order by CRC) and decodes
  every planned mode at 1.0 -> 2.4 Msps a few dB above its threshold (`tests/test_phy_mcs.py`).

Discovery cost: about 1-4 s per capture at matched rate (L window 124-132) and a median of 441 s
at ρ (one width, 16 processes in parallel on a 22-core server).

## Planner (`adapt.py`)

* `MCS` is a list of `Mode(threshold_db, family, M, rate, goodput)` in rising goodput. Its
  thresholds and goodputs now come from `kL.md` (K = L, mesh fast path), not from this table.
  `family` is "differential" (M-PSK, `compose`) or "coherent" (square M-QAM, `compose_qam`).
* `_mcs_for(snr, families)` returns the highest-goodput mode of an allowed family whose threshold
  the SNR clears. When none clears, it returns the most robust allowed mode.
* `default_plan(..., families=FAMILIES)` puts the family on the plan (`TxPlan.family`, a new
  field defaulting to "differential") and still returns `(plan, code_rate)`. Pass
  `families=("differential",)` where coherent is not yet trusted on the air. On the bench,
  coherent did not survive equal sender/receiver rates (`research/coherent_blind.md` in
  entroptics-rf).
* Tests: `tests/test_adapt.py` (+6). They check that the table is an envelope, that the planner
  takes the best cleared mode for every family set across −5..40 dB, that coherent carries the top,
  and that the plan carries the chosen family and M.

## Limits and open issues

* **The mesh PHY sends and reads both families.** `phy.transmit_burst` composes the plan's
  family, alphabet and code rate; `receive_known` / `receive_burst` read every burst both ways and
  `link.read_frame` lets the frame CRC pick the (family, M, rate) hypothesis, with nothing
  signalled. Emulated link results per mode: `phy_mcs_link.py` (`results_phy_link_*.json`).
* **Hardware.** Coherent QAM failed on the bench at matched rates (filter-edge mode mixing; 16/64
  read as 4). The thresholds here are AWGN through an ideal channel model. Until the bench
  confirms them, `families=("differential",)` is the safe plan for matched-rate links.
* **Blind reception is the binding constraint, not the table.** On a single cold burst:
  * The lattice order read fails at coded operating points: 256-QAM is misread in 66/90 captures.
    Letting the frame's CRC choose M (17/90) is a mesh-side fix and needs no rf change.
  * Matched-rate discovery does not lock 4-QAM bursts at 6-12 dB (34/90).
  * At ρ ≠ 1, the cold-start scale read on 7-34 rows fails 70/96 times.

  `phy.receive_known` is that coherent warm path: a known scale, the start and carrier measured
  again on the burst's rows, the reference row by `_reference_at`, and the order by CRC.
* **The reference-row gain read** costs about 2-4 dB against a genie (above). That is the largest
  single gain available to the coherent family.
* **Differential at ρ ≠ 1** has not been measured as a coded MCS table. The coherent thresholds
  take the worst condition and the differential ones the matched L=64 figure, so the planner's
  comparison at ρ ≠ 1 is not like for like.
* **AWGN only**: no fading, no carrier or timing error in the table's read. Those enter through the
  blind spot-check only.

## PER tables

`fails/packets` where a cell had no failures or all failures, otherwise the PER. Coarse cells
(60 packets, or stopped at 20/20) and fine cells (up to 500 packets) are merged by SNR.

### PER, matched, L=64 K=60

4-QAM

| SNR dB | raw BER (1/2) | PER 1/2 | PER 2/3 | PER 3/4 |
|---|---|---|---|---|
| 2 | 1.94e-01 | 20/20 | 20/20 | 20/20 |
| 3 | 1.56e-01 | 20/20 | 20/20 | 20/20 |
| 4 | 1.17e-01 | 20/20 | 20/20 | 20/20 |
| 5 | 9.15e-02 | 0.533 (0.933) | 20/20 | 20/20 |
| 6 | 6.40e-02 | 0.064 (0.606) | 0.9 (20/20) | 20/20 |
| 7 | 4.21e-02 | 0.002 (0.164) | 0.382 (0.794) | 0.933 (0.967) |
| 8 | 2.56e-02 | 0/500 (0.036) | 0.072 (0.346) | 0.296 (0.615) |
| 9 | 1.42e-02 | 0/500 (0.004) | 0.006 (0.096) | 0.012 (0.144) |
| 10 | 6.63e-03 | 0/500 | 0/500 (0.016) | 0.004 (0.018) |
| 11 | 2.80e-03 | 0/500 | 0/500 (0.004) | 0/500 |
| 12 | 9.13e-04 | 0/60 | 0/500 | 0/500 |
| 13 | 2.10e-04 | 0/60 | 0/500 | 0/500 |

16-QAM

| SNR dB | raw BER (1/2) | PER 1/2 | PER 2/3 | PER 3/4 |
|---|---|---|---|---|
| 7 | 1.76e-01 | 20/20 | 20/20 | 20/20 |
| 8 | 1.49e-01 | 20/20 | 20/20 | 20/20 |
| 9 | 1.27e-01 | 20/20 | 20/20 | 20/20 |
| 10 | 1.03e-01 | 0.817 (0.833) | 20/20 | 20/20 |
| 11 | 8.32e-02 | 0.223 (0.438) | 20/20 | 20/20 |
| 12 | 6.39e-02 | 0.024 (0.084) | 0.95 | 20/20 |
| 13 | 4.67e-02 | 0/500 (0.004) | 0.45 (0.622) | 0.9 (20/20) |
| 14 | 3.19e-02 | 0/500 | 0.054 (0.154) | 0.617 (0.8) |
| 15 | 2.06e-02 | 0/500 | 0/500 (0.016) | 0.098 (0.206) |
| 16 | 1.21e-02 | 0/500 | 0.002 | 0.01 (0.022) |
| 17 | 6.30e-03 | 0/60 | 0/500 | 0/500 (0.002) |
| 18 | 3.05e-03 | 0/60 | 0/500 | 0/500 |
| 19 | 1.43e-03 | 0/60 | 0/60 | 0/500 |
| 20 | 3.04e-04 | 0/60 | 0/60 | 0/500 |

64-QAM

| SNR dB | raw BER (1/2) | PER 1/2 | PER 2/3 | PER 3/4 |
|---|---|---|---|---|
| 13 | 1.38e-01 | 20/20 | 20/20 | 20/20 |
| 14 | 1.18e-01 | 20/20 | 20/20 | 20/20 |
| 15 | 1.02e-01 | 0.9 (0.817) | 20/20 | 20/20 |
| 16 | 8.42e-02 | 0.321 (0.353) | 20/20 | 20/20 |
| 17 | 6.86e-02 | 0.08 (0.136) | 0.917 (0.933) | 20/20 |
| 18 | 5.35e-02 | 0.004 (0.014) | 0.5 (0.51) | 20/20 |
| 19 | 4.01e-02 | 0/500 | 0.129 (0.162) | 0.85 |
| 20 | 2.81e-02 | 0/500 | 0.02 (0.026) | 0.272 (0.38) |
| 21 | 1.89e-02 | 0/500 | 0/500 | 0.044 (0.062) |
| 22 | 1.16e-02 | 0/60 | 0/500 | 0.008 (0.018) |
| 23 | 6.63e-03 | 0/60 | 0/500 | 0/500 |
| 24 | 3.16e-03 | 0/60 | 0/60 | 0/500 |
| 25 | 1.56e-03 | 0/60 | 0/60 | 0/500 |
| 26 | 6.08e-04 | 0/60 | 0/60 | 0/60 |
| 27 | 1.85e-04 | 0/60 | 0/60 | 0/60 |

256-QAM

| SNR dB | raw BER (1/2) | PER 1/2 | PER 2/3 | PER 3/4 |
|---|---|---|---|---|
| 19 | 1.15e-01 | 0.5 (0.517) | 20/20 | 20/20 |
| 20 | 9.94e-02 | 0.142 (0.145) | 20/20 | 20/20 |
| 21 | 8.49e-02 | 0.05 (0.038) | 20/20 | 20/20 |
| 22 | 7.10e-02 | 0.008 (0.004) | 20/20 | 20/20 |
| 23 | 5.80e-02 | 0/500 | 0.95 (20/20) | 20/20 |
| 24 | 4.60e-02 | 0/500 | 0.717 (0.7) | 0.917 (0.95) |
| 25 | 3.49e-02 | 0/500 | 0.249 (0.23) | 0.6 (0.68) |
| 26 | 2.49e-02 | 0/60 | 0.046 (0.062) | 0.176 (0.229) |
| 27 | 1.66e-02 | 0/60 | 0.002 (0.006) | 0.032 (0.062) |
| 28 | 1.09e-02 | 0/60 | 0/500 | 0.014 (0.016) |
| 29 | 6.27e-03 | 0/60 | 0/500 | 0/500 |
| 30 | 3.16e-03 | 0/60 | 0/500 | 0/500 |
| 31 | 1.47e-03 | 0/60 | 0/60 | 0/500 |
| 32 | 5.80e-04 | 0/60 | 0/60 | 0/60 |
| 33 | 2.06e-04 | 0/60 | 0/60 | 0/60 |

### PER, matched, L=128 K=124

4-QAM

| SNR dB | raw BER (1/2) | PER 1/2 | PER 2/3 | PER 3/4 |
|---|---|---|---|---|
| 2 | 1.96e-01 | 20/20 | 20/20 | 20/20 |
| 3 | 1.53e-01 | 20/20 | 20/20 | 20/20 |
| 4 | 1.22e-01 | 20/20 | 20/20 | 20/20 |
| 5 | 9.35e-02 | 0.8 (20/20) | 20/20 | 20/20 |
| 6 | 6.76e-02 | 0.058 (0.784) | 20/20 | 20/20 |
| 7 | 4.57e-02 | 0.004 (0.29) | 0.357 (0.886) | 20/20 |
| 8 | 2.80e-02 | 0/500 (0.06) | 0.03 (0.392) | 0.5 (0.83) |
| 9 | 1.53e-02 | 0/500 (0.01) | 0.004 (0.05) | 0.03 (0.288) |
| 10 | 7.53e-03 | 0/500 | 0/500 (0.006) | 0.006 (0.056) |
| 11 | 3.16e-03 | 0/500 | 0/500 | 0/500 (0.004) |
| 12 | 1.02e-03 | 0/500 | 0/500 | 0/500 |
| 13 | 2.28e-04 | 0/60 | 0/60 | 0/500 |

16-QAM

| SNR dB | raw BER (1/2) | PER 1/2 | PER 2/3 | PER 3/4 |
|---|---|---|---|---|
| 7 | 1.76e-01 | 20/20 | 20/20 | 20/20 |
| 8 | 1.51e-01 | 20/20 | 20/20 | 20/20 |
| 9 | 1.25e-01 | 20/20 | 20/20 | 20/20 |
| 10 | 1.04e-01 | 0.85 (0.933) | 20/20 | 20/20 |
| 11 | 8.29e-02 | 0.25 (0.415) | 20/20 | 20/20 |
| 12 | 6.36e-02 | 0.024 (0.054) | 20/20 | 20/20 |
| 13 | 4.66e-02 | 0.002 (0.006) | 0.424 (0.619) | 20/20 |
| 14 | 3.21e-02 | 0/500 | 0.058 (0.142) | 0.767 (0.817) |
| 15 | 2.06e-02 | 0/500 | 0.006 (0.024) | 0.171 (0.342) |
| 16 | 1.20e-02 | 0/500 | 0/500 | 0.028 (0.048) |
| 17 | 6.34e-03 | 0/60 | 0/500 | 0.002 (0.004) |
| 18 | 3.14e-03 | 0/60 | 0/500 | 0/500 |
| 19 | 1.26e-03 | 0/60 | 0/60 | 0/500 |
| 20 | 3.91e-04 | 0/60 | 0/60 | 0/500 |

64-QAM

| SNR dB | raw BER (1/2) | PER 1/2 | PER 2/3 | PER 3/4 |
|---|---|---|---|---|
| 13 | 1.40e-01 | 20/20 | 20/20 | 20/20 |
| 14 | 1.24e-01 | 20/20 | 20/20 | 20/20 |
| 15 | 1.06e-01 | 20/20 | 20/20 | 20/20 |
| 16 | 8.76e-02 | 0.533 (0.617) | 20/20 | 20/20 |
| 17 | 7.16e-02 | 0.129 (0.15) | 0.95 (20/20) | 20/20 |
| 18 | 5.62e-02 | 0.008 (0.024) | 0.567 (0.481) | 20/20 |
| 19 | 4.26e-02 | 0.002 | 0.128 (0.138) | 0.833 |
| 20 | 3.05e-02 | 0/500 | 0.016 (0.02) | 0.357 (0.371) |
| 21 | 2.04e-02 | 0/500 | 0.002 | 0.068 (0.104) |
| 22 | 1.28e-02 | 0/500 | 0/500 | 0.006 (0.014) |
| 23 | 7.35e-03 | 0/60 | 0/500 | 0/500 |
| 24 | 3.77e-03 | 0/60 | 0/500 | 0/500 |
| 25 | 1.63e-03 | 0/60 | 0/60 | 0/500 |
| 26 | 6.31e-04 | 0/60 | 0/60 | 0/60 |
| 27 | 2.08e-04 | 0/60 | 0/60 | 0/60 |

256-QAM

| SNR dB | raw BER (1/2) | PER 1/2 | PER 2/3 | PER 3/4 |
|---|---|---|---|---|
| 19 | 1.11e-01 | 0.601 (0.327) | 20/20 | 20/20 |
| 20 | 9.65e-02 | 0.178 (0.126) | 20/20 | 20/20 |
| 21 | 8.18e-02 | 0.04 (0.032) | 20/20 | 20/20 |
| 22 | 6.84e-02 | 0.008 (0.006) | 20/20 | 20/20 |
| 23 | 5.54e-02 | 0/500 | 0.95 (20/20) | 20/20 |
| 24 | 4.34e-02 | 0/500 | 0.633 (0.667) | 0.983 (0.967) |
| 25 | 3.26e-02 | 0/500 | 0.226 (0.171) | 0.8 (0.783) |
| 26 | 2.29e-02 | 0/60 | 0.022 (0.02) | 0.255 (0.276) |
| 27 | 1.58e-02 | 0/60 | 0.01 (0.006) | 0.078 (0.07) |
| 28 | 9.48e-03 | 0/60 | 0/500 (0.002) | 0.008 |
| 29 | 5.45e-03 | 0/60 | 0/500 | 0.002 (0/500) |
| 30 | 2.73e-03 | 0/60 | 0/500 | 0/500 |
| 31 | 1.16e-03 | 0/60 | 0/60 | 0/500 |
| 32 | 5.22e-04 | 0/60 | 0/60 | 0/500 |
| 33 | 1.56e-04 | 0/60 | 0/60 | 0/60 |

### PER, rho 0.514403, L=128 K=124

4-QAM

| SNR dB | raw BER (1/2) | PER 1/2 | PER 2/3 | PER 3/4 |
|---|---|---|---|---|
| 2 | 1.97e-01 | 20/20 | 20/20 | 20/20 |
| 3 | 1.62e-01 | 20/20 | 20/20 | 20/20 |
| 4 | 1.27e-01 | 20/20 | 20/20 | 20/20 |
| 5 | 9.93e-02 | 0.75 (20/20) | 20/20 | 20/20 |
| 6 | 7.06e-02 | 0.082 (0.802) | 20/20 | 20/20 |
| 7 | 4.80e-02 | 0.002 (0.338) | 0.442 (0.938) | 20/20 |
| 8 | 3.00e-02 | 0/500 (0.062) | 0.052 (0.376) | 0.517 (0.9) |
| 9 | 1.66e-02 | 0/500 (0.008) | 0.002 (0.078) | 0.064 (0.338) |
| 10 | 8.24e-03 | 0/500 (0.004) | 0/500 (0.006) | 0.004 (0.036) |
| 11 | 3.37e-03 | 0/500 | 0/500 (0.002) | 0/500 (0.006) |
| 12 | 1.22e-03 | 0/60 | 0/500 | 0/500 |
| 13 | 3.15e-04 | 0/60 | 0/60 | 0/500 |

16-QAM

| SNR dB | raw BER (1/2) | PER 1/2 | PER 2/3 | PER 3/4 |
|---|---|---|---|---|
| 7 | 1.77e-01 | 20/20 | 20/20 | 20/20 |
| 8 | 1.52e-01 | 20/20 | 20/20 | 20/20 |
| 9 | 1.30e-01 | 20/20 | 20/20 | 20/20 |
| 10 | 1.08e-01 | 0.85 (0.9) | 20/20 | 20/20 |
| 11 | 8.54e-02 | 0.233 (0.414) | 20/20 | 20/20 |
| 12 | 6.58e-02 | 0.022 (0.068) | 0.9 (20/20) | 20/20 |
| 13 | 4.87e-02 | 0/500 (0.004) | 0.5 (0.65) | 20/20 |
| 14 | 3.35e-02 | 0/500 | 0.088 (0.156) | 0.867 (0.883) |
| 15 | 2.17e-02 | 0/500 | 0.008 (0.012) | 0.263 (0.337) |
| 16 | 1.30e-02 | 0/60 | 0/500 (0.004) | 0.06 (0.072) |
| 17 | 6.85e-03 | 0/60 | 0/500 | 0.002 (0.008) |
| 18 | 3.59e-03 | 0/60 | 0/500 | 0/500 |
| 19 | 1.40e-03 | 0/60 | 0/500 | 0/500 |
| 20 | 4.21e-04 | 0/60 | 0/60 | 0/500 |

64-QAM

| SNR dB | raw BER (1/2) | PER 1/2 | PER 2/3 | PER 3/4 |
|---|---|---|---|---|
| 13 | 1.43e-01 | 20/20 | 20/20 | 20/20 |
| 14 | 1.25e-01 | 20/20 | 20/20 | 20/20 |
| 15 | 1.07e-01 | 20/20 | 20/20 | 20/20 |
| 16 | 8.97e-02 | 0.633 (0.583) | 20/20 | 20/20 |
| 17 | 7.33e-02 | 0.195 (0.181) | 20/20 | 20/20 |
| 18 | 5.80e-02 | 0.024 (0.032) | 0.517 (0.6) | 20/20 |
| 19 | 4.39e-02 | 0.004 (0/500) | 0.12 (0.141) | 0.883 (0.933) |
| 20 | 3.16e-02 | 0/500 | 0.014 (0.028) | 0.424 (0.568) |
| 21 | 2.14e-02 | 0/500 | 0.01 (0.006) | 0.068 (0.098) |
| 22 | 1.35e-02 | 0/500 | 0/500 | 0.008 |
| 23 | 7.61e-03 | 0/60 | 0/500 | 0.002 (0/500) |
| 24 | 4.10e-03 | 0/60 | 0/500 | 0/500 |
| 25 | 1.85e-03 | 0/60 | 0/60 | 0/500 |
| 26 | 7.73e-04 | 0/60 | 0/60 | 0/500 |
| 27 | 2.65e-04 | 0/60 | 0/60 | 0/60 |

256-QAM

| SNR dB | raw BER (1/2) | PER 1/2 | PER 2/3 | PER 3/4 |
|---|---|---|---|---|
| 19 | 1.14e-01 | 0.65 (0.517) | 20/20 | 20/20 |
| 20 | 9.83e-02 | 0.208 (0.114) | 20/20 | 20/20 |
| 21 | 8.34e-02 | 0.046 (0.03) | 20/20 | 20/20 |
| 22 | 6.98e-02 | 0.006 (0.008) | 20/20 | 20/20 |
| 23 | 5.67e-02 | 0.002 (0/500) | 0.967 (0.95) | 20/20 |
| 24 | 4.48e-02 | 0/500 | 0.633 (0.617) | 0.983 (0.95) |
| 25 | 3.37e-02 | 0/500 | 0.264 (0.24) | 0.767 (0.733) |
| 26 | 2.41e-02 | 0/500 | 0.054 (0.03) | 0.321 (0.298) |
| 27 | 1.64e-02 | 0/60 | 0.002 (0.004) | 0.088 (0.108) |
| 28 | 1.01e-02 | 0/60 | 0/500 (0.002) | 0.02 (0.012) |
| 29 | 5.72e-03 | 0/60 | 0/500 | 0/500 |
| 30 | 2.91e-03 | 0/60 | 0/500 | 0/500 |
| 31 | 1.40e-03 | 0/60 | 0/60 | 0/500 |
| 32 | 4.91e-04 | 0/60 | 0/60 | 0/60 |
| 33 | 2.10e-04 | 0/60 | 0/60 | 0/60 |
