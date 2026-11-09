# MCS table: differential M-PSK on the keyed modes + K=7 convolutional code

> **Not the planner's table.** This table measures the code and modulation at K = 60 with the
> true geometry. `adapt.MODES` takes its thresholds and goodputs from `kL.md`'s
> second table (`results_kL_fp.json`). That table covers both families at K = L through the mesh's own receive
> path (fast path, CRC-named mode), at matched rate and 1.0 -> 2.4 Msps.

These numbers were measured in simulation through the real codec:
`entroptics_rf.waveform.compose` -> complex AWGN -> `waveform.read_soft`
(with the true geometry: offset 0, phase 0, carrier 0, row0 0) ->
`fec.soft_bits_from_differential(..., weight_by_magnitude=True)` ->
`fec.decode_packet(..., rate=r)`. Scripts: `mcs_table.py` (measurement; raw data in
`results.json` and `results_15_25.json`) and `make_report.py` (the tables below).

## Setup

* Geometry: K = 60 keyed modes, L = 64 samples per row, seed 1234. Each burst
  carries one packet: one keyed reference row plus T payload rows.
* Packet: 500 payload bytes (4000 bits). For coded rates, `fec.encode_packet`
  adds a 2-byte length and CRC-32, encodes with the (171,133) K=7 code
  (zero tail), punctures to rate r, then applies the 16-column interleave.
  "Uncoded" sends the same length+payload+CRC frame raw. A packet counts as
  failed when its CRC fails. Across all coded cells, no packet passed CRC
  with a wrong payload.
* Puncturing follows the 802.11 patterns (G0 row / G1 row):
  2/3 = [1,1]/[1,0], 3/4 = [1,1,0]/[1,0,1]. Deleted bits go back in as zero
  soft values before Viterbi.
* SNR is Es/N0 per complex sample. The composed burst is unit-RMS, and
  complex AWGN with variance N0 = 10^(-SNR/10) is added. The modes are
  orthonormal and the energy is spread over K of L dimensions, so the SNR
  per mode symbol is SNR + 10 log10(64/60) = SNR + 0.28 dB.
* Each coded cell ran 1000 packets. A cell stopped early (marked "1 (40)")
  when its first 40 packets all failed. Uncoded cells use the same 1000
  packets of 4048 bits each (about 4e6 bits). The "raw BER" column is the
  hard-decision channel BER of the uncoded run.
* Grid: 0-30 dB in 2 dB steps, plus 15 and 25 dB. Runtime was 2272 s for
  the main grid and 235 s for the 15/25 dB points on 8 processes, which is
  over the ~30 min target.

## Goodput formula

    G = (1 - PER) * 8*P / ((T + 1) * L)      [payload bits per complex sample = b/s/Hz]
    T = ceil( ceil(Nc / log2 M) / K )        payload rows; +1 = the reference row
    Nc = packet_coded_length(P, r)           = 8108 / 6081 / 5406 bits at r = 1/2, 2/3, 3/4
       = 8 * (P + 6) = 4048                  uncoded

G counts the following as overhead: the reference row, the 2-byte header,
the CRC-32, the 6 tail bits, and the unused modes of the partial last row.
Bursts are 137/103/92/69 rows (M=2, rates 1/2, 2/3, 3/4, uncoded),
69/52/47/35 (M=4), 47/35/32/24 (M=8) and 35/27/24/18 (M=16). The nominal
rate r * log2(M) * 60/64 is 3-8% higher. Preamble, synchronization and
any MAC overhead are not counted, because the receiver is given the true
geometry.

## PER vs SNR per modulation

Raw BER is the uncoded hard-decision bit error rate. Each entry is PER. "0/1000"
means no failures in 1000 packets. "1 (40)" means all of the first 40 failed.

### M = 2

| SNR dB | raw BER | PER 1/2 | PER 2/3 | PER 3/4 | PER uncoded |
|---|---|---|---|---|---|
| 0 | 1.70e-01 | 1 (40) | 1 (40) | 1 (40) | 1 (40) |
| 2 | 9.07e-02 | 0.71 | 1 (40) | 1 (40) | 1 (40) |
| 4 | 3.45e-02 | 0/1000 | 0.185 | 0.89 | 1 (40) |
| 6 | 7.44e-03 | 0/1000 | 0/1000 | 0.004 | 1 (40) |
| 8 | 5.72e-04 | 0/1000 | 0/1000 | 0/1000 | 0.844 |
| 10 | 9.88e-06 | 0/1000 | 0/1000 | 0/1000 | 0.036 |
| 12 | 0.00e+00 | 0/1000 | 0/1000 | 0/1000 | 0/1000 |
| 14 | 0.00e+00 | 0/1000 | 0/1000 | 0/1000 | 0/1000 |
| 15 | 0.00e+00 | 0/1000 | 0/1000 | 0/1000 | 0/1000 |
| 16 | 0.00e+00 | 0/1000 | 0/1000 | 0/1000 | 0/1000 |
| 18 | 0.00e+00 | 0/1000 | 0/1000 | 0/1000 | 0/1000 |
| 20 | 0.00e+00 | 0/1000 | 0/1000 | 0/1000 | 0/1000 |
| 22 | 0.00e+00 | 0/1000 | 0/1000 | 0/1000 | 0/1000 |
| 24 | 0.00e+00 | 0/1000 | 0/1000 | 0/1000 | 0/1000 |
| 25 | 0.00e+00 | 0/1000 | 0/1000 | 0/1000 | 0/1000 |
| 26 | 0.00e+00 | 0/1000 | 0/1000 | 0/1000 | 0/1000 |
| 28 | 0.00e+00 | 0/1000 | 0/1000 | 0/1000 | 0/1000 |
| 30 | 0.00e+00 | 0/1000 | 0/1000 | 0/1000 | 0/1000 |

### M = 4

| SNR dB | raw BER | PER 1/2 | PER 2/3 | PER 3/4 | PER uncoded |
|---|---|---|---|---|---|
| 0 | 3.32e-01 | 1 (40) | 1 (40) | 1 (40) | 1 (40) |
| 2 | 2.56e-01 | 1 (40) | 1 (40) | 1 (40) | 1 (40) |
| 4 | 1.77e-01 | 1 (40) | 1 (40) | 1 (40) | 1 (40) |
| 6 | 9.53e-02 | 0.971 | 1 (40) | 1 (40) | 1 (40) |
| 8 | 3.98e-02 | 0.014 | 0.631 | 1 (40) | 1 (40) |
| 10 | 9.49e-03 | 0/1000 | 0.002 | 0.073 | 1 (40) |
| 12 | 1.29e-03 | 0/1000 | 0/1000 | 0/1000 | 0.969 |
| 14 | 5.53e-05 | 0/1000 | 0/1000 | 0/1000 | 0.138 |
| 15 | 5.68e-06 | 0/1000 | 0/1000 | 0/1000 | 0.017 |
| 16 | 0.00e+00 | 0/1000 | 0/1000 | 0/1000 | 0/1000 |
| 18 | 0.00e+00 | 0/1000 | 0/1000 | 0/1000 | 0/1000 |
| 20 | 0.00e+00 | 0/1000 | 0/1000 | 0/1000 | 0/1000 |
| 22 | 0.00e+00 | 0/1000 | 0/1000 | 0/1000 | 0/1000 |
| 24 | 0.00e+00 | 0/1000 | 0/1000 | 0/1000 | 0/1000 |
| 25 | 0.00e+00 | 0/1000 | 0/1000 | 0/1000 | 0/1000 |
| 26 | 0.00e+00 | 0/1000 | 0/1000 | 0/1000 | 0/1000 |
| 28 | 0.00e+00 | 0/1000 | 0/1000 | 0/1000 | 0/1000 |
| 30 | 0.00e+00 | 0/1000 | 0/1000 | 0/1000 | 0/1000 |

### M = 8

| SNR dB | raw BER | PER 1/2 | PER 2/3 | PER 3/4 | PER uncoded |
|---|---|---|---|---|---|
| 0 | 4.02e-01 | 1 (40) | 1 (40) | 1 (40) | 1 (40) |
| 2 | 3.61e-01 | 1 (40) | 1 (40) | 1 (40) | 1 (40) |
| 4 | 3.11e-01 | 1 (40) | 1 (40) | 1 (40) | 1 (40) |
| 6 | 2.47e-01 | 1 (40) | 1 (40) | 1 (40) | 1 (40) |
| 8 | 1.85e-01 | 1 (40) | 1 (40) | 1 (40) | 1 (40) |
| 10 | 1.20e-01 | 1 (40) | 1 (40) | 1 (40) | 1 (40) |
| 12 | 6.26e-02 | 0.381 | 1 (40) | 1 (40) | 1 (40) |
| 14 | 2.46e-02 | 0.002 | 0.148 | 0.689 | 1 (40) |
| 15 | 1.30e-02 | 0/1000 | 0.013 | 0.165 | 1 (40) |
| 16 | 5.97e-03 | 0/1000 | 0.001 | 0.011 | 1 (40) |
| 18 | 7.11e-04 | 0/1000 | 0/1000 | 0/1000 | 0.8 |
| 20 | 3.09e-05 | 0/1000 | 0/1000 | 0/1000 | 0.063 |
| 22 | 0.00e+00 | 0/1000 | 0/1000 | 0/1000 | 0/1000 |
| 24 | 0.00e+00 | 0/1000 | 0/1000 | 0/1000 | 0/1000 |
| 25 | 0.00e+00 | 0/1000 | 0/1000 | 0/1000 | 0/1000 |
| 26 | 0.00e+00 | 0/1000 | 0/1000 | 0/1000 | 0/1000 |
| 28 | 0.00e+00 | 0/1000 | 0/1000 | 0/1000 | 0/1000 |
| 30 | 0.00e+00 | 0/1000 | 0/1000 | 0/1000 | 0/1000 |

### M = 16

| SNR dB | raw BER | PER 1/2 | PER 2/3 | PER 3/4 | PER uncoded |
|---|---|---|---|---|---|
| 0 | 4.29e-01 | 1 (40) | 1 (40) | 1 (40) | 1 (40) |
| 2 | 4.03e-01 | 1 (40) | 1 (40) | 1 (40) | 1 (40) |
| 4 | 3.66e-01 | 1 (40) | 1 (40) | 1 (40) | 1 (40) |
| 6 | 3.27e-01 | 1 (40) | 1 (40) | 1 (40) | 1 (40) |
| 8 | 2.90e-01 | 1 (40) | 1 (40) | 1 (40) | 1 (40) |
| 10 | 2.44e-01 | 1 (40) | 1 (40) | 1 (40) | 1 (40) |
| 12 | 1.98e-01 | 1 (40) | 1 (40) | 1 (40) | 1 (40) |
| 14 | 1.45e-01 | 1 (40) | 1 (40) | 1 (40) | 1 (40) |
| 15 | 1.21e-01 | 1 (40) | 1 (40) | 1 (40) | 1 (40) |
| 16 | 9.31e-02 | 0.76 | 1 (40) | 1 (40) | 1 (40) |
| 18 | 4.95e-02 | 0.033 | 0.987 | 1 (40) | 1 (40) |
| 20 | 2.06e-02 | 0/1000 | 0.114 | 0.436 | 1 (40) |
| 22 | 4.93e-03 | 0/1000 | 0.001 | 0.006 | 1 (40) |
| 24 | 6.05e-04 | 0/1000 | 0/1000 | 0/1000 | 0.73 |
| 25 | 1.44e-04 | 0/1000 | 0/1000 | 0/1000 | 0.265 |
| 26 | 2.47e-05 | 0/1000 | 0/1000 | 0/1000 | 0.045 |
| 28 | 0.00e+00 | 0/1000 | 0/1000 | 0/1000 | 0/1000 |
| 30 | 0.00e+00 | 0/1000 | 0/1000 | 0/1000 | 0/1000 |

### Thresholds and nominal rates

The grid value is the lowest 2 dB grid SNR from which PER stays at or below the target at every higher grid point. The bracketed value interpolates log10(PER) linearly between the two bracketing grid points. A zero-failure cell counts as 0.5/1000 for that interpolation only, so the bracketed values are estimates.

| MCS | b/s/Hz at PER 0 | PER<=10% grid (interp) | PER<=1% grid (interp) |
|---|---|---|---|
| DBPSK 1/2 | 0.456 | 4 (2.5) | 4 (3.2) |
| DBPSK 2/3 | 0.607 | 6 (4.2) | 6 (5.0) |
| DBPSK 3/4 | 0.679 | 6 (4.8) | 6 (5.7) |
| DBPSK uncoded | 0.906 | 10 (9.4) | 12 (10.6) |
| DQPSK 1/2 | 0.906 | 8 (7.1) | 10 (8.2) |
| DQPSK 2/3 | 1.202 | 10 (8.6) | 10 (9.4) |
| DQPSK 3/4 | 1.330 | 10 (9.8) | 12 (10.8) |
| DQPSK uncoded | 1.786 | 16 (14.1) | 16 (14.9) |
| D8PSK 1/2 | 1.330 | 14 (12.5) | 14 (13.4) |
| D8PSK 2/3 | 1.786 | 16 (14.2) | 16 (15.1) |
| D8PSK 3/4 | 1.953 | 16 (14.9) | 18 (16.1) |
| D8PSK uncoded | 2.604 | 20 (19.6) | 22 (20.8) |
| D16PSK 1/2 | 1.786 | 18 (17.3) | 20 (18.6) |
| D16PSK 2/3 | 2.315 | 22 (20.1) | 22 (21.0) |
| D16PSK 3/4 | 2.604 | 22 (20.7) | 22 (21.8) |
| D16PSK uncoded | 3.472 | 26 (25.4) | 28 (26.7) |

### Goodput envelope (best measured MCS per SNR)

| SNR dB | best MCS | its PER | goodput b/s/Hz | best with PER<=1% | goodput | Shannon log2(1+SNR) |
|---|---|---|---|---|---|---|
| 0 | DBPSK 1/2 | 1 (40) | 0.000 | none | 0 | 1.00 |
| 2 | DBPSK 1/2 | 0.71 | 0.132 | none | 0 | 1.37 |
| 4 | DBPSK 2/3 | 0.185 | 0.495 | DBPSK 1/2 | 0.456 | 1.81 |
| 6 | DBPSK 3/4 | 0.004 | 0.677 | DBPSK 3/4 | 0.677 | 2.32 |
| 8 | DQPSK 1/2 | 0.014 | 0.893 | DBPSK 3/4 | 0.679 | 2.87 |
| 10 | DQPSK 3/4 | 0.073 | 1.233 | DQPSK 2/3 | 1.200 | 3.46 |
| 12 | DQPSK 3/4 | 0/1000 | 1.330 | DQPSK 3/4 | 1.330 | 4.07 |
| 14 | DQPSK uncoded | 0.138 | 1.539 | DQPSK 3/4 | 1.330 | 4.71 |
| 15 | D8PSK 2/3 | 0.013 | 1.762 | DQPSK 3/4 | 1.330 | 5.03 |
| 16 | D8PSK 3/4 | 0.011 | 1.932 | DQPSK uncoded | 1.786 | 5.35 |
| 18 | D8PSK 3/4 | 0/1000 | 1.953 | D8PSK 3/4 | 1.953 | 6.00 |
| 20 | D8PSK uncoded | 0.063 | 2.440 | D8PSK 3/4 | 1.953 | 6.66 |
| 22 | D8PSK uncoded | 0/1000 | 2.604 | D8PSK uncoded | 2.604 | 7.32 |
| 24 | D16PSK 3/4 | 0/1000 | 2.604 | D16PSK 3/4 | 2.604 | 7.98 |
| 25 | D16PSK 3/4 | 0/1000 | 2.604 | D16PSK 3/4 | 2.604 | 8.31 |
| 26 | D16PSK uncoded | 0.045 | 3.316 | D16PSK 3/4 | 2.604 | 8.64 |
| 28 | D16PSK uncoded | 0/1000 | 3.472 | D16PSK uncoded | 3.472 | 9.30 |
| 30 | D16PSK uncoded | 0/1000 | 3.472 | D16PSK uncoded | 3.472 | 9.97 |

### weight_by_magnitude False vs True, M = 4

| SNR dB | 1/2 True | 1/2 False | 2/3 True | 2/3 False | 3/4 True | 3/4 False |
|---|---|---|---|---|---|---|
| 0 | 1 (40) | 1 (40) | 1 (40) | 1 (40) | 1 (40) | 1 (40) |
| 2 | 1 (40) | 1 (40) | 1 (40) | 1 (40) | 1 (40) | 1 (40) |
| 4 | 1 (40) | 1 (40) | 1 (40) | 1 (40) | 1 (40) | 1 (40) |
| 6 | 0.971 | 1 (40) | 1 (40) | 1 (40) | 1 (40) | 1 (40) |
| 8 | 0.014 | 0.011 | 0.631 | 0.764 | 1 (40) | 1 (40) |
| 10 | 0/1000 | 0/1000 | 0.002 | 0.007 | 0.073 | 0.08 |
| 12 | 0/1000 | 0/1000 | 0/1000 | 0/1000 | 0/1000 | 0.002 |
| 14 | 0/1000 | 0/1000 | 0/1000 | 0/1000 | 0/1000 | 0/1000 |
| 16 | 0/1000 | 0/1000 | 0/1000 | 0/1000 | 0/1000 | 0/1000 |
| 18 | 0/1000 | 0/1000 | 0/1000 | 0/1000 | 0/1000 | 0/1000 |
| 20 | 0/1000 | 0/1000 | 0/1000 | 0/1000 | 0/1000 | 0/1000 |
| 22 | 0/1000 | 0/1000 | 0/1000 | 0/1000 | 0/1000 | 0/1000 |
| 24 | 0/1000 | 0/1000 | 0/1000 | 0/1000 | 0/1000 | 0/1000 |
| 26 | 0/1000 | 0/1000 | 0/1000 | 0/1000 | 0/1000 | 0/1000 |
| 28 | 0/1000 | 0/1000 | 0/1000 | 0/1000 | 0/1000 | 0/1000 |
| 30 | 0/1000 | 0/1000 | 0/1000 | 0/1000 | 0/1000 | 0/1000 |


## Findings

* **Waterfalls.** With soft Viterbi, PER <= 1% is reached at these
  interpolated SNRs (the 2 dB grid point is in brackets):
  DBPSK 1/2 3.2 (4), 2/3 5.0 (6), 3/4 5.7 (6).
  DQPSK 1/2 8.2 (10), 2/3 9.4 (10), 3/4 10.8 (12).
  D8PSK 1/2 13.4 (14), 2/3 15.1 (16), 3/4 16.1 (18).
  D16PSK 1/2 18.6 (20), 2/3 21.0 (22), 3/4 21.8 (22).
  Uncoded: DBPSK 10.6, DQPSK 14.9, D8PSK 20.8, D16PSK 26.7.
* **Coding gain** at PER 1%, comparing 3/4 with uncoded at the same M, is
  about 5 dB (DBPSK 4.9, DQPSK 4.1, D8PSK 4.7, D16PSK 4.9). The cost is
  25-33% of the rate.
* **Dominated MCSs.** Several entries never appear on the envelope:
  * D8PSK 1/2 delivers 1.330 b/s/Hz, the same as DQPSK 3/4, but needs 13.4 dB against 10.8.
  * D16PSK 1/2 delivers 1.786 at 18.6 dB. D8PSK 2/3 and uncoded DQPSK give the same rate at 15.1 and 14.9 dB.
  * D16PSK 2/3 delivers 2.315 at 21.0 dB. Uncoded D8PSK gives 2.604 at 20.8 dB.
  * D16PSK 3/4 delivers 2.604 at 21.8 dB. Uncoded D8PSK gives the same rate at 20.8 dB.

  Above about 14 dB, then, the code at high M loses to dropping one order
  of M and sending uncoded. Two causes are likely, and neither was tested
  separately here. First, the PHY labels symbols in natural binary, not
  Gray: an error to an adjacent symbol often flips 2+ bits, which hurts
  most at M=8/16. Second, the soft metric is purely geometric and is not
  scaled per bit position. A Gray remap done above the PHY, inside the FEC
  layer, would be the next experiment.
* **Usable MCS ladder** at PER <= 1%, with the SNR needed:
  DBPSK 1/2 0.456 b/s/Hz @ 3.2 dB; DBPSK 3/4 0.679 @ 5.7; DQPSK 1/2 0.906 @ 8.2;
  DQPSK 2/3 1.202 @ 9.4; DQPSK 3/4 1.330 @ 10.8; D8PSK 2/3 1.786 @ 15.1;
  D8PSK 3/4 1.953 @ 16.1; D8PSK uncoded 2.604 @ 20.8; D16PSK uncoded 3.472 @ 26.7.
  The ceiling is 3.47 b/s/Hz: log2(16) * 60/64 minus framing.
* **weight_by_magnitude** (M=4): True vs False differ by at most about 0.4 dB.
  At PER 1%: 1/2 8.2 vs 8.1, 2/3 9.4 vs 9.8, 3/4 10.8 vs 11.1. With 1000
  packets this is barely resolved. Under AWGN, |d| carries little extra
  information. Magnitude weighting would matter more under fading, which
  was not simulated.
* **Shannon gap.** At 20 dB the envelope delivers 2.44 b/s/Hz (PER 6%), or
  1.95 at PER 0, against log2(1+SNR) = 6.66. At 30 dB it delivers 3.47
  against 9.97. Differential PSK saturates at log2 M, so the gap widens
  with SNR. The coherent QAM mode in entroptics-rf (`compose_qam`) is the
  route to rate above about 20 dB; its coded table is in `coherent.md`.

## Comparison with 802.11ac / 802.11ax (reference values, not measured here)

**These Wi-Fi numbers are reference values, not measurements.** They are
standard 1-spatial-stream PHY data rates for 20 MHz with an 800 ns guard
interval, divided by 20 MHz. They are quoted from the standard MCS tables as
I know them (from memory), not from a fresh lookup, so check them before any
external use.

* 802.11ac (VHT): 52 data subcarriers, 4.0 us symbol. MCS0-8 =
  6.5 / 13 / 19.5 / 26 / 39 / 52 / 58.5 / 65 / 78 Mb/s, which is
  0.325 / 0.65 / 0.975 / 1.30 / 1.95 / 2.60 / 2.925 / 3.25 / 3.90 b/s/Hz.
  MCS9 is not valid at 20 MHz with 1 SS.
* 802.11ax (HE): 234 data subcarriers, 13.6 us symbol. MCS0-11 =
  8.6 / 17.2 / 25.8 / 34.4 / 51.6 / 68.8 / 77.4 / 86.0 / 103.2 / 114.7 / 129.0 / 143.4 Mb/s,
  which is 0.43 / 0.86 / 1.29 / 1.72 / 2.58 / 3.44 / 3.87 / 4.30 / 5.16 / 5.74 / 6.45 / 7.17 b/s/Hz.
* Required SNR per MCS. Wi-Fi defines no AWGN SNR thresholds, so these are
  derived from the 20 MHz minimum input sensitivities: -82, -79, -77, -74,
  -70, -66, -65, -64, -59, -57 dBm for MCS0-9, plus -54 and -52 dBm for
  ax MCS10-11. The derivation uses the assumptions the standard states for
  those sensitivities: thermal noise of -101 dBm in 20 MHz, a 10 dB noise
  figure and a 5 dB implementation margin. That gives SNR = sensitivity +
  86 dB, so MCS0-11 need 4, 7, 9, 12, 16, 20, 21, 22, 27, 29, 32, 34 dB.
  These thresholds are for PER <= 10%. They are pessimistic: real
  receivers usually beat minimum sensitivity by several dB.
* Accounting differences. The Wi-Fi rates include the guard interval,
  pilots and guard tones, but not preamble or MAC overhead. The figures
  measured here include the reference row and the length/CRC/tail
  framing, but not synchronization, because the true geometry was given.
  The channel here is ideal AWGN with perfect timing and no CFO. The two
  SNR definitions are close but not identical. Ours is per complex sample
  over the full sample bandwidth. Wi-Fi's is over 20 MHz, with 52/64 or
  242/256 of the band occupied.

Best rate at PER <= 10% for each SNR. The measured column takes the
highest-goodput MCS with PER <= 10% at that SNR. The reference columns take
the highest MCS whose spec-derived threshold is at or below that SNR.

| SNR dB | measured here (b/s/Hz, MCS, PER) | 802.11ac 20 MHz 1SS 0.8 us (reference) | 802.11ax 20 MHz 1SS 0.8 us (reference) |
|---|---|---|---|
| 10 | 1.233 (DQPSK 3/4, PER 7.3%); 1.200 at PER 0.2% | 0.975 (MCS2) | 1.29 (MCS2) |
| 15 | 1.762 (D8PSK 2/3, PER 1.3%) | 1.30 (MCS3) | 1.72 (MCS3) |
| 20 | 2.440 (D8PSK uncoded, PER 6.3%); 1.953 at PER 0 | 2.60 (MCS5) | 3.44 (MCS5) |
| 25 | 2.604 (D16PSK 3/4 / D8PSK uncoded, PER 0) | 3.25 (MCS7) | 4.30 (MCS7) |
| 30 | 3.472 (D16PSK uncoded, PER 0) | 3.90 (MCS8) | 5.74 (MCS9) |

What the comparison shows, with all the caveats above:

* At 10-15 dB, this link matches or exceeds the spec-floor Wi-Fi rates. It
  is about 25-35% above 802.11ac and about level with 802.11ax.
* From 20 dB up it falls behind. At 25-30 dB it delivers 2.6-3.5 b/s/Hz
  against 3.3-3.9 (ac) and 4.3-5.7 (ax). There are two reasons. Wi-Fi
  switches to 64/256/1024-QAM, while this link stops at D16PSK. And Wi-Fi
  runs rate-5/6 codes and coherent detection, while this link is
  differential, which costs about 3 dB.
* Because the Wi-Fi thresholds carry a 5 dB implementation margin, a
  like-for-like AWGN comparison would move the Wi-Fi columns about 5 dB to
  the left. On that basis this link is behind 802.11ac/ax at every SNR
  above about 5 dB, not just at the top. The advantage at 10-15 dB should
  not be quoted without that qualification.
