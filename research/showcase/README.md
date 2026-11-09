# Fleet showcase (queue item 11)

A drone fleet flies out from a two-gateway base with two relays trailing above it. It sends telemetry and a tunnelled UDP flow home over several hops. Every burst is real entroptics-rf IQ on the emulated air, and every node is the mesh's own runtime (`node.Node`) deciding from its own reads.

## Files
- `src/entroptics_mesh/emu/fleet_run.py`: `FleetRun` runs the nodes in worker processes; the gateways share one worker so they share one backhaul. Positions are set per epoch: the air holds the true positions, and `node.position` holds the GPS claim, which can be spoofed. Every slot is recorded: bursts with plan and symbol, each frame taken with its transmitter and the SNR the receiver measured that burst at, forwards, acks, cancels, expiries, deliveries, and changes to each node's distrusted set. `view(i)` returns what node i believes.
- `fleet_showcase.py` sets up the scenario and writes `fleet_showcase.json`, rewritten after every epoch. The cast is G1 and G2 (HOME anycast, tunnel `Backhaul`), R1 and R2 trailing at 400 m, and D1-D8 at 60 m/s. Each epoch, every node sends "I'm here", every drone sends telemetry, and D1 sends 2 UDP datagrams. D6's GPS is spoofed from epoch 2, walking 4 km sideways per epoch. R1 is lost in epoch 4, after its first burst.
- `build_player.py` writes `fleet_showcase.html` next to the JSON and prints the run summary. The HTML is a self-contained player with no network access; it follows the system's light or dark setting. It contains:
  - a map of the whole cast and a close-up of the formation, showing true positions, announced positions, and a red cross with a count on a node whose claim is distrusted;
  - links coloured by the measured SNR;
  - each burst flashing on its sub-band with its mode, plus a band strip;
  - a delivery and latency panel;
  - an event log of deliveries, distrust and trust changes, the node loss, the spoof start, and gateway relay plans;
  - a table of every item sent.

## How to run
From `entroptics-mesh`, using the entroptics-rf venv:

    PYTHONPATH=src python research/showcase/fleet_showcase.py [epochs=7] [workers=6]
    python research/showcase/build_player.py

`FleetRun` listens before talking by default (`listen=False` gives the old access). On a 22-core server with 8 workers, all 7 epochs take about 12 minutes of wall time; with listening the nodes read more captures.

## Result (7 epochs, seed 1, 2026-10-17, with listen before talk)
`fleet_showcase.json` / `.html` is this run; `fleet_showcase_before.json` is the run before the access work, and the player shows its figures beside this one's. The access work is described in `research/access/README.md`.
- **Delivery: 70/70 items home** (telemetry 56/56, UDP 14/14). Before, 31/70. Seeds 2 and 3 also delivered 70/70, against 35/70 before.
- **Latency of delivered items:** median 88 ms, p90 232 ms, max 312 ms. Before: median 36 ms, p90 114 ms, but only 31 items arrived. Of all items sent, 53% arrived within 100 ms and 80% within 200 ms, against 37% and 44% before. Latency is measured to the start of the slot the item is delivered in.
- **Airtime:** 390 bursts in 397 slots, 5.6 bursts per item delivered, against 671 bursts and 21.6 per item before. 956 starts were deferred because the listen heard the channel busy. Each slot carries one burst, or two from senders out of earshot (16 slots).
- **Why items were lost before.** The cause was collisions, not the receiver read. Drones relayed each other's HOME traffic, which was 382 of 671 bursts. Full-width origin bursts overlapped every relay burst in the slot: 24 of 115 were read with another burst present, against 10 of 10 alone. The level gate's floor also rose to the loudest neighbour in the busy band and refused many decodable captures. Now every node listens before it talks, the floor is the least of 32 reads, and a node sends a new message's first symbol before older messages' redundancy.
- **Measured SNR.** Over 3929 frames taken, measured minus true in-band SNR has median -0.12 dB and p90 of |error| 0.30 dB. Before it was median -0.15 dB and p90 1.6 dB; fewer collisions mean cleaner reads.
- **Plausibility.** Only the spoofed D6 distrusts truthful nodes. D6's claim is flagged by every drone and R2 from epoch 2. The gateways now flag it too, G1 from epoch 3 and G2 from epoch 5, because more of D6's announcements reach them. Before, the gateways never flagged it.
- **Node loss.** R1 is gone from epoch 4. Delivery stays at 10/10 per epoch, with R2 and the drones carrying the relaying.

## Known limits
- Gateways cannot catch a spoof whose offset is smaller than about 40% of the true distance (3 dB of SNR tolerance). Close neighbours do catch it.
- An announcement's `heard` SNRs carry no measurement time. Read one epoch later, against moving nodes, they are bounded by `2 * max_speed * table.max_age` and constrain nothing in a fast fleet. Carrying the age would need a change to the announce format.
- A burst that fills the slot leaves one quiet row of its own. At 35-39 dB the edge of the burst still reaches that row, and the SNR reads up to ~3 dB low (see `phy.burst_snr`).
