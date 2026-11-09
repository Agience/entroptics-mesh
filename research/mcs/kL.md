# MCS table at K = L: every planner mode through the mesh's own receive path

This table gives the 1%-PER threshold and goodput of every mode in `adapt.MODES`, measured on the
current waveform. On that waveform every mode is lit (K = L = 64), each row carries a keyed power,
geometry is read by the keyed constant-modulus read, and the coherent row turn is read by
`phy._row_turn`. Each frame goes through the same chain a node uses. The planner (`adapt.MODES`)
takes its thresholds and goodputs from the second table below.

The earlier tables measured something narrower. They used K = 60 and the codec's own read with the
true geometry handed in (`results.md`, `results_gray.json`, `coherent.md`). They measured the code
and the modulation, but not synchronization.

Two tables follow, both measured by `mcs_kl.py` through the same chain. The **second** is the
one the planner uses: it re-measures every mode after the fast path's geometry and gain reads were
fixed (section "Second table"). The **first** is kept below as the provenance of that work: it is
where the fast path's own losses were found.

* Second table: raw cells `kl_cells_fp.jsonl`, merged cells, fits and summary `results_kL_fp.json`.
* First table: raw cells `kl_cells.jsonl`, summary `results_kL.json`.

## Second table: the fast path read again

### What changed in the read (phy.receive_known, phy.RxBurst)

Each fix answers a loss class the first table and a per-frame diagnostic found. The diagnostic
re-read lost frames against the true geometry (start, carrier, reference row).

1. **One coherent gain for every mode** (`phy._common_gain`). The coherent read divided each mode by
   its own gain, read off the single reference row. Every keyed mode spans the whole band, so the
   channel reaches each mode's own coordinate with nearly the same gain. What one row reads per
   mode beyond that is the row's noise plus its share of inter-mode mixing, and dividing it out of
   every payload row spends both again on each symbol. At the true geometry, with 40 frames a
   cell, the mean gain decoded 40/40 in every cell, where per-mode gains decoded:
   * 4-QAM 1/2 at 8.6 dB: 32 (matched) and 34 (1.0→2.4) of 40;
   * 16-QAM 1/2 at 12 dB: 30/40;
   * 64-QAM 2/3 at 19.6 dB: 29/40;
   * 256-QAM 2/3 at 27.5 dB: raw BER 0.019 against 0.004.

   Noiseless, with a 3-sample echo of 0.1 / 0.3 added at the receiver, the mean gain still reads
   the payload 2.8 / 3.0 dB cleaner. So the per-mode spread is mixing, not gain, and the change
   should hold off the emulator too (not yet measured on hardware). This, not the geometry, was
   most of coherent 4-QAM's "slow tail" and most of the coherent family's distance from theory.
2. **The burst's first row from its run of rows** (`phy._run`, `phy._onset`). The start row came
   from the de-keyed differential's M-th-power closure. For 16- and 8-PSK near their thresholds
   that power sits at its own noise (|mean u^16| 0.04-0.06 at the true row of a D16PSK 1/2 burst at
   16.6 dB, 0.014-0.045 at the wrong ones). The first row is now where the burst's run begins: the
   rows reading most firmly below the constant-modulus null, a scan statistic with no threshold.
   The closure's row is still offered second, and the frame CRC decides between them.
3. **The row grid starts where the capture holds a whole row.** When a burst began about one row
   into a capture, the scan's grid could start a fraction of a sample before the first readable
   sample. `waveform._scaled_rows` drops a row it does not hold whole, which shifted every row
   index by one, so the reference was read on the noise row ahead of the burst. The grid now moves
   a row on.
4. **Each differential alphabet reads its own row turn** (`phy._diff_turn`, in
   `RxBurst.soft_bits`). The residual turn was read once, with the M the start closure favoured.
   Near threshold that M was often wrong (2, 4 or 8 on a 16-PSK burst), which turned the burst
   randomly by up to a quarter cycle. The differential is no longer refined after the settle,
   which places it within p90 0.003-0.007 cycles/row.
5. **The scan shortlists, all 64 modes rank.** The scan scores starts on 16 modes. On 16 modes a
   12-14-row dense QAM burst inside a 16-row scan can lose to a chance start (true 0.82 of the
   null, chance 0.77). The scan now keeps its 3 best starts per carrier probe (rf
   `_scaled_scan(keep=)`). Each distinct start is ranked on all modes by the run statistic over up
   to 32 rows, and the settle minimizes the same statistic (rf `_scaled_cm(reduce=)`), so rows of
   noise around a short burst do not dilute it.
6. **`RxBurst.rows` checks its edge against the run.** With no row of silence after the burst, the
   rank edge of the row levels fell between two of the burst's own rows. The count is now:
   * the edge, where the run ends at or before it;
   * the run's end, where the run ends one row past the edge;
   * otherwise None.

   A burst the capture ends with is still refined (the run reaches the capture's last rows).

### Thresholds

Same method as the first table, with one extra phase. The coarse grid is centred on the old
(K = 60) thresholds, and every coherent mode's waterfall now lay below it, so a `low` phase added
cells 1-6 dB under each mode's lowest cell before the fine phase. The run used 1397 keyed and open
cells and 466,506 frames, with no undetected error, no wrong-mode decode and no receive-path
exception. In the table, "geometry" is the share of lost frames whose returned read had raw BER >
0.2, over every diagnosed cell.

| mode | first table: planning (matched / 1.0→2.4) | second: planning (matched / 1.0→2.4) | change | geometry share of losses, first → second | G |
|---|---|---|---|---|---|
| DBPSK 1/2 | 4.1 (4.04 / 4.05) | **4.0** (3.86 / 3.93) | −0.1 | 30% → 17% | 0.489 |
| DBPSK 2/3 | 5.5 (5.26 / 5.43) | **5.6** (5.27 / 5.51) | +0.1 | 0% → 6% | 0.646 |
| DBPSK 3/4 | 6.5 (6.19 / 6.42) | **6.4** (6.15 / 6.36) | −0.1 | 0% → 2% | 0.727 |
| DQPSK 1/2 | 7.2 (6.84 / 7.19) | **7.2** (6.90 / 7.11) | 0 | 1% → 15% | 0.97 |
| DQPSK 2/3 | 9.0 (8.86 / 8.91) | **9.0** (8.89 / 8.92) | 0 | 1% → 1% | 1.28 |
| DQPSK 3/4 | 10.2 (9.85 / 10.13) | **10.1** (9.87 / 10.10) | −0.1 | 1% → 2% | 1.422 |
| D8PSK 1/2 | 11.5 (11.24 / 11.40) | **11.3** (11.09 / 11.22) | −0.2 | 63% → 1% | 1.422 |
| D8PSK 2/3 | 13.6 (13.41 / 13.53) | **13.7** (13.49 / 13.61) | +0.1 | 0% → 0% | 1.882 |
| D8PSK 3/4 | 15.1 (14.94 / 15.08) | **15.1** (14.89 / 15.01) | 0 | 1% → 0% | 2.133 |
| D16PSK 1/2 | 17.7 (17.34 / 17.65) | **15.9** (15.67 / 15.82) | **−1.8** | 96% → 0% | 1.882 |
| D16PSK 2/3 | 18.9 (18.73 / 18.88) | **19.0** (18.90 / 18.99) | +0.1 | 33% → 0% | 2.462 |
| D16PSK 3/4 | 20.6 (20.40 / 20.59) | **20.7** (20.41 / 20.62) | +0.1 | 0% → 0% | 2.783 |
| 4-QAM 1/2 | 10.8 (10.56 / 10.71) | **4.4** (4.29 / 4.34) | **−6.4** | 59% → 13% | 0.97 |
| 4-QAM 2/3 | 12.0 (11.59 / 11.91) | **6.5** (6.32 / 6.47) | **−5.5** | 24% → 2% | 1.28 |
| 4-QAM 3/4 | 12.7 (12.21 / 12.62) | **7.7** (7.53 / 7.70) | **−5.0** | 8% → 1% | 1.422 |
| 16-QAM 1/2 | 13.8 (13.53 / 13.73) | **10.2** (10.05 / 10.11) | **−3.6** | 8% → 1% | 1.882 |
| 16-QAM 2/3 | 15.5 (15.27 / 15.46) | **12.8** (12.57 / 12.77) | **−2.7** | 1% → 0% | 2.462 |
| 16-QAM 3/4 | 16.9 (16.58 / 16.80) | **14.4** (14.14 / 14.39) | **−2.5** | 0% → 0% | 2.783 |
| 64-QAM 1/2 | 18.9 (18.67 / 18.86) | **16.0** (15.57 / 15.94) | **−2.9** | 1% → 0% | 2.783 |
| 64-QAM 2/3 | 21.1 (20.81 / 21.02) | **18.4** (18.07 / 18.30) | **−2.7** | 2% → 0% | 3.556 |
| 64-QAM 3/4 | 22.8 (22.37 / 22.78) | **20.0** (19.75 / 19.95) | **−2.8** | 1% → 0% | 4.0 |
| 256-QAM 1/2 | 22.5 (22.22 / 22.43) | **19.3** (19.01 / 19.27) | **−3.2** | 1% → 0% | 3.556 |
| 256-QAM 2/3 | 28.9 (27.35 / 28.86) | **24.1** (23.84 / 24.00) | **−4.8** | 31% → 0% | 4.571 |
| 256-QAM 3/4 | 30.3 (29.06 / 30.26) | **26.3** (26.05 / 26.28) | **−4.0** | 27% → 0% | 5.333 |

**Findings**

* **The coherent family moved 2.5-6.4 dB, now below even the K = 60 true-geometry tables.** For
  example, 4-QAM 1/2 is 4.4 dB where the K = 60 table had 6.7, and 256-QAM 3/4 is 26.3 where it
  had 28.2. Those tables used per-mode gains too, so the gain read was the larger loss
  everywhere. Coherent 4-QAM now sits 2.8 dB under DQPSK at the same goodput, which is the
  textbook coherent/differential gap.
* **The 1.0→2.4 dense-QAM floor is gone.**
  * 256-QAM 2/3 lost 1.37% of frames in cells at least 0.5 dB above its first-table threshold.
    It now loses 0.05% of frames at least 0.5 dB above its new one.
  * 256-QAM 3/4: 0.88% → 0.07%.
  * Matched and 1.0→2.4 now differ by 0.2-0.4 dB at every rate, against 1.2-1.5 dB before.
* **D16PSK 1/2 recovered 1.8 dB** and is again below its 2/3 rate. It sits 1.5 dB above the K = 60
  true-geometry figure (14.4), and its remaining losses are decodes, not geometry.
* **The code-limited differential modes moved ±0.1-0.2 dB**, within the fits' intervals.
* **The remaining "geometry" losses (DBPSK 1/2 17%, DQPSK 1/2 15%, 4-QAM 1/2 13%)** sit at 3-7 dB.
  The class is read from the burst the read returns, which is built on its first candidate row,
  so some of these are decode losses. Seen frame by frame at DBPSK 3.2 dB: the run placed the
  burst a row early, and the closure's row, also tried, decoded at raw BER 0.067.

### Capture tail (problem 3 of the first table)

256-QAM 3/4 at 50 dB, with the capture ending 72-112 received samples after a 1.0 Msps burst (0.5-0.7
of a row): first table 19/80 lost (9-11/80 in a re-run), now 0/80. At 30 dB the same tail loses
0/80. At matched rate, with the capture ending under a row past the burst: 12/80 before, 0/80
now. The table keeps two rows of tail so that it compares with the first.

### Open mode (spot check, 300 frames a cell)

| mode | keyed (matched / 1.0→2.4) | open (matched / 1.0→2.4) |
|---|---|---|
| DBPSK 1/2 | 3.86 / 3.93 | 3.92 / 3.96 |
| DQPSK 3/4 | 9.87 / 10.10 | 9.81 / 10.05 |
| 16-QAM 1/2 | 10.05 / 10.11 | 10.03 / 10.05 |
| 64-QAM 2/3 | 18.07 / 18.30 | — (no fit: under 1% throughout) / 18.46 |
| 256-QAM 3/4 | 26.05 / 26.28 | 27.55 / 27.78 |

Open reads as keyed within 0.2 dB, except 256-QAM 3/4. That mode is 1.5 dB worse open, because
an open receiver levels each row itself (`waveform._row_levels`). No frame raised (the first
table's open runs, measured before the `_row_levels` spread floor, crashed 3-10% of frames).

### CPU per burst

On a 22-core server, one process, the first read's code against this one alternately, twice; mean ms per
burst, receive plus frame decode.

| burst | first | second |
|---|---|---|
| node path (research/scenarios/settle_tolerance.py: DQPSK 1/2, 5-6 dB, 1.0→2.4 Msps, +0.3 MHz) | 161-175 | 180-198 |
| DBPSK 1/2, 1.0→2.4, 5 dB | 207-208 | 230-235 |
| D8PSK 2/3, 1.0→2.4, 14 dB | 130-146 | 161-171 |
| D16PSK 3/4, matched, 21 dB | 87-92 | 84-99 |
| 4-QAM 1/2, 1.0→2.4, 11 dB | 156-180 | 164-190 |
| 64-QAM 2/3, matched, 22 dB | 57-62 | 86-101 |
| 256-QAM 3/4, 1.0→2.4, 31 dB | 158-160 | 130-132 |

The read costs ~10-20% more on long or matched bursts. The extra cost is:

* the shortlist's ranking reads;
* the run statistic in every settle step;
* a second candidate row when the first fails.

Dense QAM at 1.0→2.4 costs less, because its first offer now passes without the coherent re-read.

### Envelope (both families)

DBPSK 1/2 (4.0) → 4-QAM 1/2 (4.4) → 2/3 (6.5) → 3/4 (7.7) → 16-QAM 1/2 (10.2) → 2/3 (12.8) → 3/4 (14.4)
→ 64-QAM 2/3 (18.4) → 3/4 (20.0) → 256-QAM 2/3 (24.1) → 3/4 (26.3, 5.333 b/sample).

* **Differential only:** DBPSK 1/2-3/4, DQPSK 1/2-3/4, D8PSK 2/3 and 3/4, D16PSK 2/3 and 3/4, as
  before. D8PSK 1/2 is still dominated by DQPSK 3/4, and D16PSK 1/2 (15.9) by D8PSK 3/4 (15.1),
  since both carry the same goodput.
* **Coherent only:** 4-QAM 1/2-3/4, 16-QAM 1/2-3/4, 64-QAM 2/3 and 3/4, 256-QAM 2/3 and 3/4.
  64-QAM 1/2 and 256-QAM 1/2 are dominated.

A whispered hop (12 dB) now plans 16-QAM 1/2 when coherent is allowed.

### Reproduce

    python research/mcs/mcs_kl.py --phase coarse --workers 16 --out research/mcs/kl_cells_fp.jsonl
    python research/mcs/mcs_kl.py --phase low    --workers 16 --out research/mcs/kl_cells_fp.jsonl
    python research/mcs/mcs_kl.py --phase fine   --workers 16 --out research/mcs/kl_cells_fp.jsonl
    python research/mcs/mcs_kl.py --phase open   --workers 16 --out research/mcs/kl_cells_fp.jsonl
    python research/mcs/mcs_kl.py --report research/mcs/kl_cells_fp.jsonl --json research/mcs/results_kL_fp.json

This run did coarse, fine, low, then fine again. The second fine places its grid from every
cell, so the order does not change the table.

## First table (superseded; the planner does not use it): the fast path before the fixes above

### Method

* **Chain.** Each frame goes through these steps, as `node.Node` sends and reads it:
  1. `link.symbols` makes one fountain symbol (a 35-byte header and a 475-byte block).
  2. `link.frame(sym, rate)` builds the 512-byte protected frame: 2-byte length, CRC-32, the K=7
     (171,133) code punctured to the rate, and the interleave.
  3. `phy.transmit_burst` composes it with `TxPlan(rate, M, family, code_rate)` at K = L = 64,
     key 1234, keyed per-row power and the keyed reference row. Differential bursts are
     Gray-labelled.
  4. The burst goes through `emu.air.Air`: free space over 6 km (48 received samples of delay).
     The receiver's noise is set so that the in-band SNR has the cell's value. In-band means the
     noise over the sender's width, which is the SNR the planner predicts.
  5. `phy.receive_known(y, key, Known(64, rho(1+2e-6), 5e-4), accept=read_frame)` reads the
     burst. This is the fast path, given a prior 2 ppm off in scale and 5e-4 of the band off in
     centre.
  6. `link.read_frame` decodes it. Every hypothesis is tried, the CRC names family, alphabet and
     code rate, and nothing about the mode is passed to the receiver.

  A frame counts as delivered when the symbol comes back byte-exact. No cell had an undetected
  error (a CRC pass with the wrong symbol), and no frame was named in the wrong mode.
* **Rates.**
  * `matched`: a 2.4 Msps sender into a 2.4 Msps receiver.
  * `r1024`: a 1.0 Msps sender into a 2.4 Msps receiver, over the band-limited emulator channel.

  The burst starts 80-120 received samples into the capture (uniform), plus the delay. Its
  sub-sample position and its row phase in the capture therefore vary. The capture ends 2 sender
  rows plus 72-112 samples after the burst; see the capture-tail finding below for why.
* **Keyed** (key 1234) for the table. An **open** spot check (key None) covers 5 modes.
* **Grid.**
  1. **Coarse:** the old threshold −1 … +3 dB, 100 frames per cell, stopping at 25 failures.
  2. **Fine:** 0.2 dB steps from the highest SNR with PER ≥ 20% to 0.6 dB past the lowest SNR
     from which every coarse cell lost ≤ 2%, plus three tail points at +1, +1.5 and +2 dB. Each
     cell ran up to 600 frames and stopped at 60 failures.

  In total, 1114 keyed cells (423,578 frames) and 90 open cells (27,000 frames) ran, in 16
  processes on a 22-core server.
* **Threshold.** A binomial maximum-likelihood fit of log10 PER = a + b·SNR to the waterfall
  cells: those with observed PER < 0.3, up to 1 dB past the last failure. The fit is solved for
  PER = 1%. The 90% interval comes from a parametric bootstrap with 200 draws.
* **Planning threshold.** The worse of the two rates, rounded up to 0.1 dB, as before.
* **Goodput.** 4096 frame bits divided by the burst's complex samples at the sender's rate (the
  reference row included), at PER 0. The old tables counted a 500-byte payload in a 500-byte
  packet at K = 60. The mesh frame is 512 bytes, and K = L adds 64/60, so the same burst now
  carries 6-14% more. `results_kL.json` also gives `goodput_block`, which counts the 475-byte
  fountain block alone (×0.928).
* **Why frames were lost (diagnostic only).** For every lost frame, the script re-reads the true
  hypothesis's own soft bits:
  * raw BER > 0.2 means the geometry was lost (start or carrier), not the code;
  * a pinned decode that passes means `read_frame` skipped the true hypothesis.

  The column "geometry" in the table is the share of lost frames in the first class. The second
  class never occurred.

### Thresholds and goodput: old (K=60, true geometry) vs new (K=L, mesh fast path)

| mode | old thr | matched (90%) | 1.0→2.4 (90%) | **planning** | Δ | old G | new G | burst samples | geometry | frames |
|---|---|---|---|---|---|---|---|---|---|---|
| DBPSK 1/2 | 3.2 | 4.04 (3.96-4.12) | 4.05 (3.97-4.13) | **4.1** | +0.9 | 0.456 | 0.489 | 8384 | 27% | 21112 |
| DBPSK 2/3 | 5.0 | 5.26 (5.18-5.34) | 5.43 (5.35-5.51) | **5.5** | +0.5 | 0.607 | 0.646 | 6336 | 0% | 15727 |
| DBPSK 3/4 | 5.7 | 6.19 (6.11-6.28) | 6.42 (6.32-6.52) | **6.5** | +0.8 | 0.679 | 0.727 | 5632 | 0% | 12883 |
| DQPSK 1/2 | 8.2 | 6.84 (6.74-6.93) | 7.19 (7.10-7.28) | **7.2** | -1.0 | 0.906 | 0.97 | 4224 | 1% | 15767 |
| DQPSK 2/3 | 9.4 | 8.86 (8.77-8.97) | 8.91 (8.82-8.99) | **9.0** | -0.4 | 1.202 | 1.28 | 3200 | 1% | 15891 |
| DQPSK 3/4 | 10.8 | 9.85 (9.76-9.95) | 10.13 (10.04-10.23) | **10.2** | -0.6 | 1.33 | 1.422 | 2880 | 1% | 20554 |
| D8PSK 1/2 | 10.9 | 11.24 (11.14-11.30) | 11.40 (11.33-11.48) | **11.5** | +0.6 | 1.33 | 1.422 | 2880 | 58% | 15076 |
| D8PSK 2/3 | 13.1 | 13.41 (13.31-13.51) | 13.53 (13.42-13.64) | **13.6** | +0.5 | 1.786 | 1.882 | 2176 | 0% | 16057 |
| D8PSK 3/4 | 14.7 | 14.94 (14.85-15.03) | 15.08 (14.95-15.20) | **15.1** | +0.4 | 1.953 | 2.133 | 1920 | 1% | 16342 |
| D16PSK 1/2 | 14.4 | 17.34 (17.26-17.41) | 17.65 (17.58-17.73) | **17.7** | +3.3 | 1.786 | 1.882 | 2176 | 87% | 16225 |
| D16PSK 2/3 | 18.8 | 18.73 (18.61-18.85) | 18.88 (18.76-18.99) | **18.9** | +0.1 | 2.315 | 2.462 | 1664 | 32% | 17748 |
| D16PSK 3/4 | 19.8 | 20.40 (20.27-20.51) | 20.59 (20.48-20.70) | **20.6** | +0.8 | 2.604 | 2.783 | 1472 | 0% | 18055 |
| 4-QAM 1/2 | 6.7 | 10.56 (10.40-10.70) | 10.71 (10.57-10.85) | **10.8** | +4.1 | 0.906 | 0.97 | 4224 | 56% | 23737 |
| 4-QAM 2/3 | 8.8 | 11.59 (11.45-11.75) | 11.91 (11.78-12.07) | **12.0** | +3.2 | 1.202 | 1.28 | 3200 | 23% | 25320 |
| 4-QAM 3/4 | 9.7 | 12.21 (12.02-12.39) | 12.62 (12.47-12.76) | **12.7** | +3.0 | 1.33 | 1.422 | 2880 | 8% | 18152 |
| 16-QAM 1/2 | 12.3 | 13.53 (13.40-13.65) | 13.73 (13.63-13.84) | **13.8** | +1.5 | 1.786 | 1.882 | 2176 | 7% | 23833 |
| 16-QAM 2/3 | 14.9 | 15.27 (15.16-15.40) | 15.46 (15.36-15.57) | **15.5** | +0.6 | 2.315 | 2.462 | 1664 | 1% | 15905 |
| 16-QAM 3/4 | 16.5 | 16.58 (16.50-16.66) | 16.80 (16.72-16.92) | **16.9** | +0.4 | 2.604 | 2.783 | 1472 | 0% | 16277 |
| 64-QAM 1/2 | 18.5 | 18.67 (18.58-18.78) | 18.86 (18.75-18.99) | **18.9** | +0.4 | 2.604 | 2.783 | 1472 | 1% | 18514 |
| 64-QAM 2/3 | 21.0 | 20.81 (20.70-20.94) | 21.02 (20.91-21.12) | **21.1** | +0.1 | 3.472 | 3.556 | 1152 | 1% | 24099 |
| 64-QAM 3/4 | 21.9 | 22.37 (22.27-22.49) | 22.78 (22.67-22.93) | **22.8** | +0.9 | 3.676 | 4.0 | 1024 | 1% | 16832 |
| 256-QAM 1/2 | 21.9 | 22.22 (22.12-22.36) | 22.43 (22.34-22.54) | **22.5** | +0.6 | 3.472 | 3.556 | 1152 | 1% | 18251 |
| 256-QAM 2/3 | 27.0 | 27.35 (27.17-27.55) | 28.86 (28.61-29.18) | **28.9** | +1.9 | 4.464 | 4.571 | 896 | 30% | 25621 |
| 256-QAM 3/4 | 30.2 | 29.06 (28.82-29.25) | 30.26 (29.95-30.77) | **30.3** | +0.1 | 4.808 | 5.333 | 768 | 26% | 22600 |

The 1.0→2.4 link is the planning (worse) rate for every mode, by 0.0-0.4 dB, except the dense
256-QAM rates (1.2-1.5 dB).

### Findings

* **Code-limited modes moved +0.1 to +0.9 dB.** This covers most of the table: DBPSK 2/3 and 3/4,
  D8PSK 2/3 and 3/4, D16PSK 3/4, 16-QAM 2/3 and 3/4, every 64-QAM rate and 256-QAM 1/2. Lighting
  every mode costs 0.28 dB per symbol (64/60). The rest comes from the fast path's read: the
  measured start and carrier, and N0 off the burst's own symbols. The old tables were handed the
  true geometry.
* **DQPSK got better by 0.4-1.0 dB.** The old M=4 differential rows were natural-labelled
  (`results.md`). The mesh sends Gray, and Gray coding is worth more than K = L costs at M = 4.
* **Where the fast path's geometry read sets the threshold** (most lost frames read with raw BER
  near 0.5, a start error of 0.1-0.5 sample or a wrong carrier branch):
  * D16PSK 1/2: +3.3 dB, 87% of losses geometry. The 1/2 burst (34 rows) loses its geometry
    where the 2/3 burst's (26 rows) code would already decode, so 1/2 now needs more SNR than 2/3.
  * coherent 4-QAM: +3.0 to +4.1 dB. Geometry is 56% of losses at 1/2. The reference row alone
    (`REF_ROWS = 1`) carries the start and carrier re-read, and at 8-11 dB that is too little.
    4-QAM also keeps a slow tail without geometry loss: 0.2-1% lost at 11-12.7 dB.
  * D8PSK 1/2 (58%), DBPSK 1/2 (27%), D16PSK 2/3 (32%).

  These are weaknesses of `phy.receive_known`, not of the code. A better low-SNR geometry read
  would bring these modes back toward the old figures.
* **Dense QAM at 1.0→2.4 Msps has a loss floor near 1%.** The losses are geometry misses:
  * 256-QAM 2/3 loses 0.7-1.9% from 28.4 to 30.5 dB.
  * 256-QAM 3/4 loses 0.4-1.1% from 29.8 to 31.7 dB.
  * 64-QAM 2/3 and 3/4 and 256-QAM 1/2 lose 0.1-0.4% to +2 dB.

  The fitted 1% points (28.9 and 30.3) sit where the waterfall meets this floor. Their bootstrap
  intervals (±0.3 dB) are therefore wider than the rest, and PER just above them is about 1%, not
  well below it. At matched rate both reach their 1% points 1.2-1.5 dB lower (27.4 and 29.1).
* **256-QAM 3/4 (was 30.2, raised from 28.2 for fast-path loss): now 30.3 from this
  measurement.** Matched rate needs 29.1. 1.0→2.4 needs 30.3 and sits on the floor above. The
  2/3 rate now needs 28.9 (it was 27.0) because of the same floor, so both stay on the envelope.
* **The capture tail matters (a fast-path defect).** The first coarse run captured 240 samples
  past the burst's start offset, which ends the capture only 0.5-0.7 of a 1.0 Msps row after the
  burst. There, `RxBurst.rows` misread a dense-QAM burst's length (1 row, or none), and
  `read_frame` skipped or lost the true hypothesis. At 50 dB, 256-QAM 3/4 at 1.0→2.4 lost 19/80
  frames this way, against 0/80 with 2 rows of tail. The table uses ≥ 2 rows. A node whose
  capture ends within about one row of a burst will lose dense-QAM frames this way, and
  `rows()` should return None rather than a short count when no silent row follows. That data is
  not in `kl_cells.jsonl`; it was set aside as `kl_cells_tail240.jsonl`, not in this repository.

### Envelope (both families)

DBPSK 1/2 (4.1) → 2/3 (5.5) → 3/4 (6.5) → DQPSK 1/2 (7.2) → 2/3 (9.0) → 3/4 (10.2) → D8PSK 2/3 (13.6)
→ 3/4 (15.1) → 16-QAM 2/3 (15.5) → 3/4 (16.9) → 64-QAM 2/3 (21.1) → 3/4 (22.8) → 256-QAM 2/3 (28.9)
→ 3/4 (30.3, 5.333 b/sample).

**Changes from the old envelope:**
* Coherent 4-QAM has left it at every rate. DQPSK carries the same goodput 2.5-3.6 dB lower.
* 16-QAM 1/2 has left it. D8PSK 2/3 carries the same 1.882 at 13.6 dB, against 13.8.
* D8PSK 2/3 has joined it.

One consequence: a whispered hop (`HOP_SNR_DB + MARGIN_DB` = 12 dB) now plans DQPSK 3/4 even
where coherent is allowed. `tests/test_phy_mcs.py::test_a_node_pair_delivers_over_a_coherent_plan`
now restricts its nodes to the coherent family.

**Per family** (`_mcs_for(snr, families)`):
* **Differential only:** DBPSK 1/2-3/4, DQPSK 1/2-3/4, D8PSK 2/3 and 3/4, D16PSK 2/3 and 3/4
  (top 2.783 at 20.6 dB). D8PSK 1/2 and D16PSK 1/2 are dominated.
* **Coherent only:** 4-QAM 1/2-3/4, 16-QAM 1/2-3/4, 64-QAM 2/3 and 3/4, 256-QAM 2/3 and 3/4. It
  starts at 10.8 dB and has nothing that clears below that. 64-QAM 1/2 and 256-QAM 1/2 are
  dominated.

Both per-family envelopes still rise monotonically.

### Open mode (spot check, 300 frames per cell, 0.25 dB steps around the keyed threshold)

* **Coherent 16-QAM 1/2 and 64-QAM 2/3** read as keyed does, within the cells' resolution:
  * 16-QAM 1/2 lost ≤ 1% from 13.0 (matched) and 13.5 dB (1.0→2.4), with stray 0.3-0.7% to
    15.2 dB.
  * 64-QAM 2/3 lost ≤ 1% from 21.1 (matched) and 21.0-21.3 dB (1.0→2.4), with 0.7% strays to
    22.0.
* **Coherent 256-QAM 3/4 is worse open.** The floor is 1.7-5% at matched rate up to 30.6 dB and
  0.7-2.7% at 1.0→2.4 up to 31.8 dB. Open coherent levels each row itself (`waveform._row_levels`),
  where keyed divides the known radius out.
* **Open differential (DBPSK 1/2, DQPSK 3/4) was dominated, in this run, by a receive-path crash.** In 3-10% of
  frames at every SNR, `read_frame` raised `IndexError` in entroptics-rf `waveform._row_levels`
  (369 frames in all). The crash happens when it tried the coherent 4-QAM hypothesis on an open
  burst. 4-QAM has constant |x|², so `sd = 0`, `pe = 1/sd² = inf` and the level becomes NaN.
  `_slice_qam` then indexes with int64(NaN). `wf._row_levels(Y, 4, n=..., L=64)` reproduces it on
  any input. Leaving those frames out:
  * open DBPSK 1/2 lost ≤ 1% from about 4.0-4.3 dB;
  * open DQPSK 3/4 lost ≤ 1% from about 10.1-10.4 dB.

  Both are equal to keyed within resolution. The table is keyed. The current rf receive path
  floors the spread in `waveform._row_levels` at 1e-6, so the 4-QAM
  `sd = 0` case no longer produces NaN (open-mode 4-QAM frames decode without raising). These
  open-differential figures were measured without the floor and exclude the crashed frames; they were not re-run.

### Reproduce

    # a 22-core server, 16 processes (about 8 min coarse, 85 min fine, 15 min open)
    python research/mcs/mcs_kl.py --phase coarse --workers 16 --out research/mcs/kl_cells.jsonl
    python research/mcs/mcs_kl.py --phase fine   --workers 16 --out research/mcs/kl_cells.jsonl
    python research/mcs/mcs_kl.py --phase open   --workers 16 --out research/mcs/kl_cells.jsonl
    python research/mcs/mcs_kl.py --report research/mcs/kl_cells.jsonl --json research/mcs/results_kL.json

Seeds are fixed per (mode, rate, SNR, key, frame index), so a rerun repeats the same frames. The
`--phase` runs skip cells already in `--out`. The coarse cells were run before the loss
diagnostic was added, so they carry no geometry/skip split.
