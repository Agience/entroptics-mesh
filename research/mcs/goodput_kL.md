# Delivered goodput on the current waveform (K = L)

`phy_mcs_link.py` (unchanged) on a fixed source snapshot (snapshot_utc 2026-10-17T11:49:52Z; later edits to mesh `phy.py`/`adapt.py` are not in these numbers): 2 nodes, 6 km emulated air, receiver at 2.4 Msps, fast path (`phy.receive_known`), every mode, 16 frames per cell (old: 8). Goodput = delivered frame bits / burst samples (b/sample). Files: `results_phy_link_kL_{24,10,12}.json` (sender 2.4 / 1.0 / 1.2 Msps).

The thresholds, and the planner picks marked "new", are those of `kL.md`'s first table, which the planner no longer uses; the delivered counts per mode stand. Today's picks (`adapt.MODES`, `kL.md`'s second table) against these delivered counts are in the mesh paper, §M-4.2.

Old = `results_phy_link_{24,12}.json` (previous waveform: K = 60, dark-mode reads, lattice turn). Planner pick = `adapt._mcs_for(SNR)` on each run's own planner table (old thresholds vs the K = L table of `kL.md`), no margin. Cell: goodput (mode, delivered/frames).

| SNR | sender | old: diff-only pick | new: diff-only pick | old: both families | new: both families | old: best delivered (all frames) | new: best delivered (all frames) |
|---|---|---|---|---|---|---|---|
| 10 dB | 2.4 (matched) | 1.21 (DQPSK 2/3, 8/8) | 1.28 (DQPSK 2/3, 16/16) | 1.33 (4-QAM 3/4, 8/8) | 1.28 (DQPSK 2/3, 16/16) | 1.33 (DQPSK 3/4, 8/8) | 1.88 (16-QAM 1/2, 16/16) |
| 10 dB | 1.2 -> 2.4 | 1.21 (DQPSK 2/3, 8/8) | 1.28 (DQPSK 2/3, 16/16) | 1.33 (4-QAM 3/4, 8/8) | 1.28 (DQPSK 2/3, 16/16) | 1.33 (DQPSK 3/4, 8/8) | 1.88 (16-QAM 1/2, 16/16) |
| 10 dB | 1.0 -> 2.4 | - | 1.28 (DQPSK 2/3, 16/16) | - | 1.28 (DQPSK 2/3, 16/16) | - | 1.88 (16-QAM 1/2, 16/16) |
| 20 dB | 2.4 (matched) | 2.56 (D16PSK 3/4, 8/8) | 2.46 (D16PSK 2/3, 16/16) | 2.56 (16-QAM 3/4, 8/8) | 2.78 (16-QAM 3/4, 16/16) | 3.37 (64-QAM 2/3, 8/8) | 4.00 (64-QAM 3/4, 16/16) |
| 20 dB | 1.2 -> 2.4 | 2.56 (D16PSK 3/4, 8/8) | 2.46 (D16PSK 2/3, 16/16) | 2.56 (16-QAM 3/4, 8/8) | 2.78 (16-QAM 3/4, 16/16) | 3.37 (64-QAM 2/3, 8/8) | 4.00 (64-QAM 3/4, 16/16) |
| 20 dB | 1.0 -> 2.4 | - | 2.46 (D16PSK 2/3, 16/16) | - | 2.78 (16-QAM 3/4, 16/16) | - | 3.56 (256-QAM 1/2, 16/16) |
| 30 dB | 2.4 (matched) | 2.56 (D16PSK 3/4, 8/8) | 2.78 (D16PSK 3/4, 16/16) | 4.31 (256-QAM 3/4, 7/8) | 4.57 (256-QAM 2/3, 16/16) | 4.57 (256-QAM 2/3, 8/8) | 5.33 (256-QAM 3/4, 16/16) |
| 30 dB | 1.2 -> 2.4 | 2.56 (D16PSK 3/4, 8/8) | 2.78 (D16PSK 3/4, 16/16) | 3.69 (256-QAM 3/4, 6/8) | 4.57 (256-QAM 2/3, 16/16) | 4.57 (256-QAM 2/3, 8/8) | 5.33 (256-QAM 3/4, 16/16) |
| 30 dB | 1.0 -> 2.4 | - | 2.78 (D16PSK 3/4, 16/16) | - | 4.57 (256-QAM 2/3, 16/16) | - | 5.33 (256-QAM 3/4, 16/16) |

Before the MCS wiring (DQPSK 1/2 pinned, `results_phy_link_old.json`): 10 dB 2.4 Msps 0.68 (6/8); 20 dB 2.4 Msps 0.90 (8/8); 30 dB 2.4 Msps 0.90 (8/8); 10 dB 1.2 Msps 0.90 (8/8); 20 dB 1.2 Msps 0.90 (8/8); 30 dB 1.2 Msps 0.90 (8/8).

## Every mode, delivered/frames (new)

| mode | threshold (dB) | table goodput | 10 dB 2.4 | 20 dB 2.4 | 30 dB 2.4 | 10 dB 1.2 | 20 dB 1.2 | 30 dB 1.2 | 10 dB 1.0 | 20 dB 1.0 | 30 dB 1.0 |
|---|---|---|---|---|---|---|---|---|---|---|---|
| DBPSK 1/2 | 4.1 | 0.489 | 16/16 | 16/16 | 16/16 | 16/16 | 16/16 | 16/16 | 16/16 | 16/16 | 16/16 |
| DBPSK 2/3 | 5.5 | 0.646 | 16/16 | 16/16 | 16/16 | 16/16 | 16/16 | 16/16 | 16/16 | 16/16 | 16/16 |
| DBPSK 3/4 | 6.5 | 0.727 | 16/16 | 16/16 | 16/16 | 16/16 | 16/16 | 16/16 | 16/16 | 16/16 | 16/16 |
| DQPSK 1/2 | 7.2 | 0.97 | 16/16 | 16/16 | 16/16 | 16/16 | 16/16 | 16/16 | 16/16 | 16/16 | 16/16 |
| DQPSK 2/3 | 9.0 | 1.28 | 16/16 | 16/16 | 16/16 | 16/16 | 16/16 | 16/16 | 16/16 | 16/16 | 16/16 |
| DQPSK 3/4 | 10.2 | 1.422 | 16/16 | 16/16 | 16/16 | 15/16 | 16/16 | 16/16 | 16/16 | 16/16 | 16/16 |
| D8PSK 1/2 | 11.5 | 1.422 | 12/16 | 16/16 | 16/16 | 15/16 | 16/16 | 16/16 | 14/16 | 16/16 | 16/16 |
| D8PSK 2/3 | 13.6 | 1.882 | 0/16 | 16/16 | 16/16 | 0/16 | 16/16 | 16/16 | 0/16 | 16/16 | 16/16 |
| D8PSK 3/4 | 15.1 | 2.133 | 0/16 | 16/16 | 16/16 | 0/16 | 16/16 | 16/16 | 0/16 | 16/16 | 16/16 |
| D16PSK 1/2 | 17.7 | 1.882 | 0/16 | 16/16 | 16/16 | 0/16 | 16/16 | 16/16 | 0/16 | 16/16 | 16/16 |
| D16PSK 2/3 | 18.9 | 2.462 | 0/16 | 16/16 | 16/16 | 0/16 | 16/16 | 16/16 | 0/16 | 16/16 | 16/16 |
| D16PSK 3/4 | 20.6 | 2.783 | 0/16 | 16/16 | 16/16 | 0/16 | 16/16 | 16/16 | 0/16 | 14/16 | 16/16 |
| 4-QAM 1/2 | 10.8 | 0.97 | 16/16 | 16/16 | 16/16 | 16/16 | 16/16 | 16/16 | 16/16 | 16/16 | 16/16 |
| 4-QAM 2/3 | 12.0 | 1.28 | 16/16 | 16/16 | 16/16 | 16/16 | 16/16 | 16/16 | 16/16 | 16/16 | 16/16 |
| 4-QAM 3/4 | 12.7 | 1.422 | 16/16 | 16/16 | 16/16 | 16/16 | 16/16 | 16/16 | 16/16 | 16/16 | 16/16 |
| 16-QAM 1/2 | 13.8 | 1.882 | 16/16 | 16/16 | 16/16 | 16/16 | 16/16 | 16/16 | 16/16 | 16/16 | 16/16 |
| 16-QAM 2/3 | 15.5 | 2.462 | 1/16 | 16/16 | 16/16 | 0/16 | 16/16 | 16/16 | 0/16 | 16/16 | 16/16 |
| 16-QAM 3/4 | 16.9 | 2.783 | 0/16 | 16/16 | 16/16 | 0/16 | 16/16 | 16/16 | 0/16 | 16/16 | 16/16 |
| 64-QAM 1/2 | 18.9 | 2.783 | 0/16 | 16/16 | 16/16 | 0/16 | 16/16 | 16/16 | 0/16 | 16/16 | 16/16 |
| 64-QAM 2/3 | 21.1 | 3.556 | 0/16 | 16/16 | 16/16 | 0/16 | 16/16 | 16/16 | 0/16 | 15/16 | 16/16 |
| 64-QAM 3/4 | 22.8 | 4.0 | 0/16 | 16/16 | 16/16 | 0/16 | 16/16 | 16/16 | 0/16 | 15/16 | 16/16 |
| 256-QAM 1/2 | 22.5 | 3.556 | 0/16 | 16/16 | 16/16 | 0/16 | 16/16 | 16/16 | 0/16 | 16/16 | 16/16 |
| 256-QAM 2/3 | 28.9 | 4.571 | 0/16 | 2/16 | 16/16 | 0/16 | 1/16 | 16/16 | 0/16 | 1/16 | 16/16 |
| 256-QAM 3/4 | 30.3 | 5.333 | 0/16 | 0/16 | 16/16 | 0/16 | 0/16 | 16/16 | 0/16 | 0/16 | 16/16 |

CPU per burst (process, one thread, a 22-core server): read median of cell medians 203 ms, decode 82 ms; old 141 / 78 ms (old measured on a different host; not like for like). Wrong-mode decodes: 0 of 2729 delivered.
