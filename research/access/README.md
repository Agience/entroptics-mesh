# Access layer: when a node sends

The fleet showcase (`research/showcase/`) lost 39 of 70 messages. None of their origin bursts was decoded by any node. This folder holds the diagnosis, the access variants that were tried, and the measurements behind the variant that was adopted.

The constraints held throughout:

- No synchronized clocks and nothing pre-shared, so no shared slot grid or schedule.
- No feedback is required for delivery.
- No fixed periodic pattern on the air.
- Every decision is local to the node.

## Files

- `diagnose.py <recording>`: the loss diagnosis from any showcase recording. Output for the old and new recordings is in `diagnosis.txt`.
- `metrics.py`: per-run metrics.
  - Delivery fraction.
  - Latency median and p90 (delivered items only), and the fraction of all items delivered within 50, 100 and 200 ms.
  - Bursts per delivered item.
  - Deferrals.
  - Fairness: the worst origin's delivery fraction and Jain's index over origins.
- `contention.py`: the dense-contention scenario. N nodes in a 500 m disc, 6 km from one gateway, all reacting to one event. Every node is handed a 48-byte record for HOME in the same slot. All nodes hear each other, and every node relays the others (any node is a peer). Each radio has heard its band's noise once before the event.
- `variants.py`: the variants as listen on/off plus `Node` options.
- `screen.py`: many-seed screening on `emu.link_model.ModelRun`. Results are in `screen_model.txt`.
- `real.py`: real-IQ runs on `FleetRun` (run on a 22-core server). Results are in `out/real_*.json`. The `oldgate` option restores the old gate floor to reproduce the recorded run, and it reproduces it exactly: 31/70, 671 bursts, 288 slots.

## Diagnosis (recorded showcase, seed 1)

Source: `diagnosis.txt`, first block.

- **Load.** There were 671 bursts. Only 125 were origin bursts of HOME messages and 81 were announcements. 465 were relay bursts, and 382 of those came from drones relaying each other's HOME traffic. Every drone in the formation hears every other one at 25-36 dB, so each one decodes a neighbour's message, becomes a relay, and queues 3 fresh symbols. The result was 14.5 relay bursts per relayed message, and bursts per slot ranged from 0 to 7.
- **Width.** A message to HOME has no known distance, so the planner shouts: every origin burst goes out at the full 2.4 MHz, while relays send at 1.2 MHz. An origin burst therefore overlaps every relay burst in its slot.
- **Collisions.** An origin burst alone in its slot was read 10 times out of 10. With other bursts in the slot it was read 24 times out of 115. The 38 lost HOME messages never had a frame decoded:
  - 26 had an SINR below threshold at every receiver;
  - 12 reached a margin ≥ 0 somewhere, but only a few dB, and at drones under half-band overlap.
- **Level-gate bug.** The gate's noise floor was the median of the last 16 captures' quiet-block reads. In a busy band most captures hold a 1.2 Msps burst that fills the slot and leaves about 1.5% of its blocks quiet, so the floor rose to the loudest neighbour's level. The gate then refused 17-75% of the captures that held a burst decodable at 3-27 dB of margin. It did not cause the 38 losses directly, but it made relays deaf to one another, so passive acks and sibling share failed and more relay airtime followed. With only this fix and the old access, seed 1 goes from 31/70 to 39/70 and from 21.6 to 18.7 bursts per delivered item.
- **Capture model.** On captures the gate let through, the read fraction against in-band SINR margin is:

  | Margin (dB) | < -6 | [-6, -3) | [-3, 0) | [0, 3) | [3, 6) | ≥ 6 |
  |---|---|---|---|---|---|---|
  | Read fraction | 0.00 | 0.01 | 0.58 | 0.91 | 0.97 | 0.98-1.00 |

  The margin is SINR over the mode's threshold, with interference weighted by in-band overlap and 13 dB of spill from a disjoint neighbouring band. Overlapping bursts combine as signals and each burst is either read or not. That is how `emu/air.py` models them, and the capture model (`emu/link_model.py`) is calibrated on this table.

## What was changed (all local, nothing agreed)

1. **Listen before talk** (`Node.start` / `busy` / `defer` / `commit`; `FleetRun` and `NodeRun` listen by default).
   - A node with a burst due draws its start uniformly at random inside a contention window of 2048 samples (0.85 ms at 2.4 Msps), on its own clock.
   - The radio listens for 64 samples just before that start.
   - If the mean power heard stands above the node's own floor by its gate (`gate_db`, 2.5 dB, the same test that decides whether a capture is read), the node defers to a random later slot, 1..`window`.
   - The start times form no grid and the deferrals are random, so there is no periodic structure.
   - The emulator's slot grows by the window: 7.15 ms becomes 8.0 ms.
2. **Floor = minimum of the last 32 captures' reads.** This fixes the gate bug. On noise alone it reads about 0.2 dB below the noise.
3. **Interleave** (`Node.interleave`, default on). The node sends the queued symbol of the message it has sent the fewest symbols of, so a new message's first symbol goes before another message's redundancy.
4. **Read from the burst's energy onset** (`Node._onset`). This is needed by item 1. The fast known-geometry read seeks a burst's first row within about 8-16 rows of the capture start, so a full-width burst at 13 dB is read at offsets of 100 and 500 samples and lost at 1000 or more. Reads now start 256 samples before the run of 64-sample blocks that stands above the floor. That run is found with `phy._run`, the threshold-free scan statistic the PHY already uses for a burst's rows. Measured: 20/20 read at offsets of 100-2100 samples, full width at 6 and 13 dB and half width at 5 and 8 dB.
5. `emu/link_model.py` is the capture model for screening. It keeps every node's own logic and replaces only what the air does to a burst. Ten seeds of the showcase take about 4 s.

## What was tried and not kept

All rows below are from the capture model, 10 seeds each, from `screen_model.txt`. Latency is for delivered items. "≤100/200 ms" is the fraction of all items delivered within that time.

| Showcase (700 items) | delivered | lat med / p90 ms | ≤100 / ≤200 ms | bursts / delivered |
|---|---|---|---|---|
| baseline (as recorded) | 0.727 | 81 / 168 | 0.42 / 0.69 | 19.2 |
| interleave only | 0.723 | 54 / 175 | 0.48 / 0.68 | 17.9 |
| LBT + doubling backoff (×8) | 0.996 | 104 / 351 | 0.50 / 0.69 | 5.48 |
| LBT, no growth | 1.000 | 89 / 236 | 0.55 / 0.83 | 5.69 |
| **LBT + interleave (adopted)** | **1.000** | **83 / 238** | **0.56 / 0.82** | **5.44** |
| adopted + doubling backoff | 1.000 | 101 / 363 | 0.51 / 0.68 | 5.30 |
| adopted, relay budget ×2 | 1.000 | 59 / 176 | 0.69 / 0.93 | 4.39 |

| Contention N=16 (800 items) | delivered | lat med / p90 ms | ≤100 / ≤200 ms | bursts / delivered |
|---|---|---|---|---|
| baseline | 0.331 | 101 / 203 | 0.16 / 0.29 | 30.9 |
| LBT + doubling backoff | 1.000 | 165 / 558 | 0.39 / 0.55 | 4.67 |
| **adopted** | **1.000** | **81 / 271** | **0.57 / 0.82** | **4.10** |
| adopted, relay budget ×2 | 1.000 | 85 / 276 | 0.56 / 0.79 | 3.09 |

At N = 4, 8 and 32 the adopted variant delivered 1.00, 1.00 and 0.93 against a baseline of 0.63, 0.55 and 0.15. N=32 is capacity-bound: about 130 bursts of work against a 120-slot (about 1 s) round. The relay budget ×2 reached 1.00 there.

Each variant that was dropped, and why:

- **Backoff that grows with busy-ness** (doubling per deferral in a row, `backoff_max` 2-8). It was worse for latency in every scenario, including 32 and 48 simultaneous origins, with no gain in delivery. The random start inside the contention window already resolves who goes first. A deferral costs a whole frame-length slot, and growing the wait only adds idle slots. The option remains as `backoff_max`, default 1.
- **Weak-first relaying** (a relay holds back one slot per X dB it heard the sender above the hop target, so far relays go first). No gain: the sibling share already caps a hop group's total at about 3 symbols.
- **Start priority for first symbols** (first symbols drawn in the first half of the window, redundancy in the second). Within noise.
- **Band-aware listening** (sense only the planned sub-band, letting two half-band bursts share a slot). Within noise. Inside the formation a neighbour at 30 dB spills about 17 dB into the other half, so the band reads busy anyway. Origins at half width ("narrow when distance unknown") were also within noise once listening was on, although they gave +15% delivery without listening.
- **Contention window** of 1024 or 4096 samples and **post-burst window** 2 or 6. All within noise of the adopted variant.
- **Adaptive origin redundancy.** The showcase's traffic goes through the tunnel at a fixed ×1.5, so k=1 gives 2 symbols. Passive acks already stop the second symbol once a relay is heard forwarding: 76 origin bursts for 70 messages (seed 1).

**Relay budget ×2 instead of ×3 (adopted as `relay.ADOPTED`).**

- It is the largest remaining latency and airtime lever.
  - Showcase: 59/176 ms against 83/238 ms.
  - Showcase: bursts per delivered item 4.39 against 5.44.
  - N=32: 1.00 delivered against 0.93.
- The ×3 budget was measured and decided for node loss (`node_loss_multi.txt`: 40/40 against 28/40). That decision was made under heavy collisions, and with listening most of that loss is gone.
- Re-measured with listening on, ×2 delivers as ×3 in every node-loss arm (`research/scenarios/node_loss_listen.txt`), with `rank_margin = 12` for a lone relay of a multi-block message.

## Real IQ (every burst entroptics-rf IQ, every read the node's own receiver)

| Showcase, 7 epochs | before (old gate, old access) | gate fix only | adopted |
|---|---|---|---|
| seed 1 | 31/70, 36 / 114 ms, 21.6 b/d | 39/70, 43 / 122 ms, 18.7 b/d | **70/70**, 88 / 232 ms, 5.57 b/d |
| seed 2 | 35/70, 57 / 136 ms, 21.9 b/d | – | **70/70**, 76 / 232 ms, 5.50 b/d |
| seed 3 | 35/70, 64 / 123 ms, 21.1 b/d | – | **70/70**, 80 / 216 ms, 5.30 b/d |
| total | 101/210 (48%) | | **210/210** |
| within 100 / 200 ms | 0.37-0.40 / 0.44-0.50 | 0.44 / 0.56 | 0.53-0.61 / 0.80-0.87 |
| worst drone | 0/7, 2/7, 1/7 | 2/7 | 7/7 every drone, every seed |

"b/d" is bursts per delivered item. Latency is median / p90 over delivered items.

- Adopted, seed 1: 390 bursts in 397 slots and 956 deferrals.
- The real runs match the capture model: model 1.000 delivered, 83/238 ms, 5.44 b/d; real 1.000 delivered, 76-88 / 216-232 ms, 5.3-5.6 b/d.
- With fewer collisions the SNR read improves too: p90 |error| 0.30 dB against 1.6 dB before.
- The gateways now flag D6's spoofed position from epoch 3. Before they never did, because more of D6's announcements now reach them.

Contention N=16, real IQ, 3 rounds per seed, 48 items per seed:

| | before (old gate, old access) | adopted |
|---|---|---|
| seed 1 | 13/48, 72 / 156 ms, 36.2 b/d | **48/48**, 76 / 203 ms, 4.04 b/d |
| seed 2 | 8/48, 89 / 131 ms, 48.0 b/d | **48/48**, 80 / 275 ms, 4.13 b/d |
| within 100 / 200 ms | 0.08-0.17 / 0.17-0.27 | 0.56-0.60 / 0.77-0.88 |
| worst origin | 0/3 | 3/3 every origin |

These match the capture model: before 0.33, adopted 1.00 delivered at 81 / 271 ms and 4.10 b/d.

## Known limits

- **Latency of delivered items rose** (median 36-64 ms → 76-88 ms) while far more items arrive and more arrive early.
  - Within 200 ms: 0.44-0.50 → 0.80-0.87.
  - The before-median counts only the lucky survivors.
  - Every node in the formation hears every other, so the cluster carries about one burst per slot, and each epoch's burst of orders (22 messages in about 23 slots, at about 5.4 bursts each) queues behind itself.
  - The latency levers left are fewer bursts per message (the relay budget above) and spatial reuse beyond earshot.
- **Hidden senders.** A burst from a sender out of earshot of the first can start later in the same capture than the read reaches, because only the first energy onset is read.
- **Cold start.** A node that has never heard its band has no floor and sends without effectively listening. In the emulator, a radio hears only slots where someone transmits. The contention scenario therefore gives each radio one noise capture first. Without it, round 0 delivered 13/16 and rounds 1 and 2 delivered 16/16 (`out/cold/`).
- **Latency granularity.** Latency is measured at slot granularity, from the slot an item is handed over to the start of the slot it is delivered in. An item read in its own slot reads 0 ms, though the burst itself takes 3.5-7 ms.
- **Older recordings mislabel bursts.** Recordings made before `Node.last_sent` existed (`out/real_showcase_adopted_{2,3}.json`) name each burst by the queue head before interleaving. Deliveries and timing are right, but a burst's origin/seq/sid and the `own` count can be wrong. The seed-1 recording in `research/showcase/` was rerun with the fix.
