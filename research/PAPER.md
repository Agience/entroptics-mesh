# Entroptics-Mesh

### A reply-free radio mesh over a keyed-aperture PHY: two-way IP to any gateway, nothing pre-shared, nothing required back, every parameter measured by the receiver.

**Ikailo John Sessford**, Ikailo Inc., `john@ikailo.com`

**Companion papers.** This builds on *Entroptics-RF* ([`entroptics-rf research/PAPER.md`](https://github.com/Agience/entroptics-rf/blob/main/research/PAPER.md), cited §RF-n) and through it on *Entroptics* ([`entroptics research/PAPER.md`](https://github.com/Agience/entroptics/blob/main/research/PAPER.md)). Sections here are written §M-n. Nothing here re-derives the PHY. Where the mesh depends on a PHY read, the read is named and its measured cost is quoted.

**Status markers.** **[measured]** means a number from a run whose output is on disk: in a project repository. Some measured numbers have no output file in this repository; §M-13 marks their source as "not in this repository". **[model]** means arithmetic on stated assumptions. Every number has a pointer in §M-13.

**Which waveform (methods).** The current wire format (§M-3): keyed per-row power, every mode lit (K = L), and the geometry read by a keyed constant-modulus read. Every performance figure in this paper is on that current waveform. The hardware bench (§M-3.1, §M-3.5) was transmitted on the cabled Pluto+ → NESDR link; the planner table (§M-4.1) and the delivered-goodput table (§M-4.2) run the current codec; and the mesh-level scenarios (§M-5, §M-7, §M-10) were measured on the current node runtime, decoding real entroptics-rf IQ through `FleetRun`, or the calibrated capture model for volume (`research/scenarios/*_current*`, `node_loss_listen.txt`). A few supporting engineering runs — acquisition and settle timing, the overlapping-sub-band and sibling-band checks, the node-runtime width-switch, and the 10 dB point of the end-to-end goodput harness — were not repeated on the current waveform and are marked *(prev)* where cited.

---

## Abstract

entroptics-mesh is a multi-hop network layer for a fleet whose far nodes move away from a base station that is in direct range only at the start. Every node is a peer, and any node may be a gateway or a relay. The design rests on three refusals, each taken from the deployment rather than from preference. Nothing is pre-shared: no synchronized clocks, no schedules and no channel plan. Nothing is required back: no acks, handshakes, echoes or decode-quality reports, so a listen-only node decodes as fully as any other. The waveform adds no repeated structure: no preamble, header or periodic feature. Reliability is therefore forward only. A K=7 convolutional code with CRC-32 protects each burst, a fountain code spreads each message across bursts, and decode-and-forward relays spread it across paths and sub-bands.

The PHY (entroptics-rf) lets each sender choose its own width and centre and has the receiver measure both. The mesh uses that as its medium-access mechanism. A relay forwards on a sub-band other than the one it heard. A known sender is read on a per-sender fast path at its measured geometry. The burst's modulation family, alphabet and code rate are not signalled at all: the receiver decodes every hypothesis and the frame CRC picks one. Over about 424,000 keyed frames of the planner measurement, no frame was named in the wrong mode and none passed its CRC with the wrong content.

Measured on an emulated air with the real PHY, decoding real IQ bursts on the current node runtime: a relay that listens until it rebuilds a message and then forwards fresh fountain symbols delivered 8/8 over 9 km hops at ×1.5 redundancy in 18 bursts, where plain relaying needed ×3 and 31 bursts for the same 8/8. Two relays per hop sharing the work by overhearing delivered all 60/60 messages across the node-loss arms with up to two of three relays lost mid-message, and all 40/40 with no loss, at both the ×3 and the adopted ×2 relay budget. Anycast to "home" delivered 10/10 with one of two gateways taken out of service, against 0/10 for unicast. A lead node moving away from the base at 60 m/s, with relays placed at 2.5, 5 and 7.5 km along its path, delivered telemetry home on 18–19 of 20 epochs out to 25.5–27 km, failing only once it had moved out of range of its last relay. A two-node link delivered every planner pick at 10, 20 and 30 dB (at least 15 of 16 frames): 1.28 / 2.46 / 2.78 b/sample differential-only, and 1.42 / 4.00 / 5.33 with coherent QAM allowed. The current waveform was transmitted on the cabled Pluto+ → NESDR bench: all eight rate pairs locked blind and decoded (uncoded BER 0 on five, ≤ 8e-4 at 94% occupancy, 0.007–0.014 at matched rates, ~19–20 dB), and on all seven structure detectors of a hardware battery it read as transmitted Gaussian noise of the same power (worst |z| ≤ 2.3); an energy detector still sees its power. Coherent QAM decodes on hardware with no codec change at ≤ 0.5 occupancy (4- and 16-QAM BER 0, 64-QAM 0.7%); at full-occupancy matched rate the analog band-edge roll-off pulls 16- and 64-QAM to read as 4-QAM.

The core carries no encryption, frequency agility or interference handling. Those are provider interfaces with null implementations, and the mesh runs end to end as an open broadcast mesh under the null ones. Everything mesh-level here is emulated, not flown, and the prototype runs in Python, at tens to hundreds of times real time per slot. The mesh also runs live and distributed across a six-node Raspberry-Pi-4B fleet, each node bridging a real operating-system network interface into the mesh over that emulated air: a commodity device obtained a DHCP lease from an off-the-shelf consumer router and reached the public internet through the mesh, TCP and UDP both carried, a three-node chain relayed by decode-and-forward with its endpoints out of direct earshot, and the chain recovered automatically when the relay was dropped and rejoined.

---

## M-1. Requirements

The requirements come from the deployment the mesh is built for. Each one removes a mechanism that conventional meshes rely on.

| requirement | what it rules out | where it is met |
|---|---|---|
| Far nodes move away from the base; base in direct range at the start only | route discovery and maintenance; anything that assumes links persist | reply-free flooding with a hop budget (§M-5), relays as a role (§M-9) |
| Multi-homed; any node a peer | a distinguished coordinator; a single uplink | anycast HOME on every gateway, backhaul dedupe (§M-7) |
| Sudden loss of nodes adapted to in real time | per-path state that breaks when a node dies | no route state; fountain symbols over parallel relays (§M-5) |
| No synced clocks, no pre-shared parameters; only a key, via a provider | TDMA, hopping schedules, agreed channel plans, agreed MCS | receivers measure each sender (§M-3); slots on each node's own clock (§M-10) |
| No feedback required for delivery | ARQ, link-quality reports, RTS/CTS, handshakes | FEC + fountain + multipath (§M-4); optional "I'm here" carries only position, band and heard SNR (§M-6) |
| No added structure over gain | cyclic prefixes, tapers, preambles, fixed headers, mirrored bands, dark modes | P=0 and every mode lit on the wire (§M-3.5), CRC-arbitrated mode with no header (§M-3.3), never mirror a heard band (§M-6) |
| Band is an input | a design tied to 915 MHz ISM | width and centre are per-burst sender choices; nothing in the core assumes a band |
| Encapsulation of UDP, TCP, BGP and others | end-to-end TCP across lossy hops | IP tunnel with best-effort and opt-in reliable classes; TCP split at the edges; BGP on gateways only (§M-8) |
| GPS may be compromised | trusting claimed positions | positions are clues, checked against measured SNR (§M-9) |

915 MHz is the bench band only. The band is an input; nothing in the core assumes a band, a width or a regulatory power cap.

---

## M-2. Architecture

**Dependency graph.** `entroptics` ← `entroptics-rf` (PHY) ← `entroptics-mesh`. The mesh's only contact with the PHY is `phy.py`. The core library (`entroptics`, PyPI ≥ 0.2.10) supplies the reads the PHY is built from (entropy geometry, level edges, occupancy; the constant-modulus read uses its `entropy.shannon_bits` and `entropy.dirichlet_entropy_moments`) and contains no RF-specific logic.

**Modules** (`src/entroptics_mesh/`):

| module | role |
|---|---|
| `providers` | the plug points: crypto, agility, interference; null implementations |
| `phy` | bursts out (`transmit_burst`), bursts in (`receive_known` fast path, `receive_burst` blind); the coherent residual row turn (`_row_turn`) |
| `fec` | CRC-32, K=7 convolutional code with 802.11 puncturing, soft Viterbi (batched), interleaver, Gray remap, QAM max-log LLRs, LT / systematic-dense fountain |
| `link` | messages as fountain symbols in 512-byte FEC frames; `read_frame` lets the CRC pick the mode |
| `routing` | reply-free controlled flooding, duplicate suppression, hop and time budgets |
| `relay` | the measured relaying rules, shared by the emulator and the node runtime |
| `neighbors` | what each sender was measured to be, kept apart from what it announced |
| `announce` | the optional "I'm here" |
| `adapt` | the default transmission plan: width, centre, power, family, alphabet, code rate (`MODES`, measured at K = L); `refit` after a provider changes the plan |
| `placement` | which links hold, who is cut off, where relays go |
| `plausibility` | whether a claimed position fits the measured SNR |
| `propagation` | Friis path loss for planning |
| `tunnel`, `tcpproxy` | IP over the mesh; TCP split at the edges |
| `node` | one node's runtime: captures in, bursts out |
| `emu/*` | the emulated air and run drivers (`air`, `net`, `link_run`, `subband_run`, `node_run`, `ipnet`) |

### M-2.1 Providers: the pluggable core

The core ships interfaces and null providers only. Encryption, frequency agility and interference handling are supplied by the integrator. The core offers only the sockets, a networking SDK (`providers.py`), and the integrator designs what goes in them. Every provider has a null implementation, and under the null ones the mesh works end to end as an open broadcast mesh on the PHY's public aperture.

| interface | calls | null behaviour |
|---|---|---|
| `CryptoProvider` | `seal(plaintext, context)`, `open(sealed, context)`, `waveform_key(context)` (the PHY seed) | no sealing; no key, which selects the public aperture |
| `AgilityProvider` | `plan(intent, default, table) -> TxPlan` | returns the core's default plan |
| `InterferenceProvider` | `observe(BandObservation)`, `review(TxPlan) -> TxPlan or None` | observes nothing, changes nothing |

`TxPlan` is (rate, centre, power, K, L, M, family, code rate); K defaults to L, and the PHY refuses any other value (§M-3). `Intent` is (destination, size, priority). `BandObservation` is (time, occupancy, centre, interference). The sealing context binds the mesh header's origin, sequence number and destination. It excludes the hop and time budgets, which relays change (`node._context`). One rule binds every provider: it never receives anything that must come back from another node. A provider that needed a reply could not work with listen-only peers.

**Key freshness belongs to the crypto provider.** Under one key every burst repeats the same keyed aperture, reference row, per-row mode rotations and per-row power sequence. None of them gives the content away, but a key held for a long time lets an observer who collects many bursts compare like with like (`providers.CryptoProvider.waveform_key` docstring; §RF-8). The node asks for one key, `waveform_key(b"phy")`, and uses whatever key is in force. How often it changes, and how a receiver comes to hold the next one, is the provider's design.

This paper describes no concrete crypto, hopping or interference-handling scheme, and the repository contains none.

---

## M-3. The PHY as the mesh sees it

entroptics-rf composes a block of bits as coordinates on keyed orthonormal modes of a screen L samples wide, raveled to IQ. Every mode is lit (K = L; K + 3 = L under a row beacon), and only a message's final row may use fewer. Each row carries a keyed power r_t² ~ Gamma(L, 1/L), and the first row is a keyed reference row. The receiver is handed a capture and a key and measures everything else: width, row offset, sub-sample position, carrier, alphabet and payload family (§RF-1–3). There is no mode count to measure. The geometry is found by a keyed constant-modulus read (the per-row entropy deficit of the mode energies against the flat Dirichlet null), in place of the earlier dark-mode read (`entroptics-rf/research/rank_leak.md`, The shipped codec). The mesh relies on five properties.

### M-3.1 Sender-chosen width and centre, measured blind

A sender writes at its own sample rate and centre and may change both mid-conversation. The receiver measures the rate ratio ρ and the band centre, told only the key.

**On hardware.** The waveform was transmitted on the cabled bench (Pluto+ → 30 dB → NESDR, ~19–20 dB, −26 dB TX backoff) and decoded blind, the receiver told only the key **[measured]**:

| TX → RX (Msps) | condition | occupancy | ratio error | closure × null | BER |
|---|---|---|---|---|---|
| 0.8 → 2.4 | harmonic 1:3 | 0.33 | +0.78 ppm | 317 | 0.0000 |
| 1.2 → 2.4 | harmonic 1:2 | 0.50 | +1.59 ppm | 159 | 0.0000 |
| 1.234567 → 2.4 | coprime | 0.51 | +0.76 ppm | 83 | 0.0000 |
| 1.234567 → 2.048 | coprime | 0.60 | +0.78 ppm | 19 | 0.0000 |
| 2.0 → 2.4 | 5:6 | 0.83 | +0.76 ppm | 269 | 0.0000 (worst seg 2e-4) |
| 1.92 → 2.048 | coprime, 94% of band | 0.94 | +0.56 ppm | 233 | 0.0008 |
| 2.4 → 2.4 | matched | 1.00 | +1.17 ppm | 74 | 0.0144 |
| 2.048 → 2.048 | matched | 1.00 | +1.25 ppm | 178 | 0.0071 |

All eight locked blind to L = 64, K = 64, M = 4 and were accepted (closure 19–317× the null). These are uncoded DQPSK figures: BER 0 on the five pairs at ≤ 0.83 occupancy, 8e-4 at 94%, and 0.007–0.014 at matched rate (matched rates are the worst case, where the receiver's filter edges mix modes). The coded link closes these (§M-4). A simulated run of the same eight pairs through the same codec gave BER 0 on all eight at 22 dB, so the matched-rate residual is the analog chain, not the codec.

The band centre is measured too, from the centroid of the occupied exponentials: on the current bench each of the eight senders was read at its own centre. A dedicated off-centre test (senders placed +0.2 / −0.15 cycles/sample off centre, read within 0.01) was run on the earlier waveform and not repeated.

No width-free read of L/ρ exists. The aperture is a separate keyed draw per width, so a blind receiver pays per width it tries. On the previous waveform the per-width period read cost 5–23 s for L = 32–64 and 26–71 s for L = 128 blind, against 0.2–1.9 s with the width given (`blind_width_scaled.md`). The constant-modulus read costs 1.4–3.3× the dark-mode map: 19–81 s against 9–41 s at L = 32 and 64, ρ 0.514 and 0.833, 10 and 20 dB, locking 4/4 in every cell (`rank_leak.md`, *Lock and cost*). Below 3 dB it holds the geometry where the dark read did not at ρ ≠ 1 (0 dB, 640 rows: 7/12 against 3/12); −3 dB is out of reach for both.

### M-3.2 The known-geometry fast path

Clock ratios are stable, so the measured (L, ρ, centre) of a sender is that neighbour's state. It is a measurement, not something shared in advance. The mesh therefore acquires blind **once per sender** and reads every later burst from that sender at the known geometry (`phy.Known`, `phy.receive_known`). The frame CRC decides success, and blind acquisition is the fallback (`node.py`).

The read, as it stands on the current waveform (`phy.py` constants and `rank_leak.md`, *The mesh on it*):

- **Scan.** The constant-modulus read over 8 to 16 rows (`SCAN_ROWS`, `SCAN_ROWS_MAX`) on 16 modes proposes starts and carriers. The best 3 starts per carrier probe are ranked again on every mode of up to 32 rows. A dense-QAM row is weak evidence for this read (256-QAM reads about 0.5 of the null at the true start, PSK 0), which is why the scan reads 16 rows where the dark scan read 8: matched 256-QAM 3/4 at 28 dB delivered 27 of 32 frames on 8 rows and 29 on 16.
- **Settle, then a start and carrier re-read.** A coherent burst re-reads on its keyed reference row alone. Adding payload rows let their own QAM levels outweigh the geometry: matched 256-QAM 3/4 at 28 dB fell from 29 to 13 of 32 with two rows. A differential burst re-reads on its first 16 rows, since its carrier branches are only 1/M apart: on the reference row alone D16PSK 2/3 took the wrong branch on 4 of 16 bursts. Carrier and start are picked alternately. The family is taken from whichever closure is better.
- **One coherent gain, and the burst's first row from its run.** The coherent read divides every mode by one gain averaged over the modes on the reference row (`phy._common_gain`), not by a per-mode gain. The first row is where the burst's run of rows begins below the constant-modulus null (`phy._run`, `phy._onset`); the M-th-power closure's row is offered second and the frame CRC decides.
- **Residual row turn** (coherent only). `phy._row_turn` reads each row's residual phase turn from the burst's own amplitude-weighted fourth moment, −Σ|y|⁸(y/|y|)⁴, as a slope through the reference. It makes no symbol decisions. A decision-directed read is not used (§M-4.1).

| path | cost (Python, one thread) | waveform | source |
|---|---|---|---|
| fast path read, 2.4 Msps sender, 20 and 30 dB, per-cell means | 23–93 ms read + 17–56 ms frame decode | current | not in this repository |
| fast path read, 1.0 Msps sender into 2.4 Msps | 67–218 ms read + 16–59 ms decode | current | not in this repository |
| node path (`receive_known(accept=read_frame)`), 60 noisy bursts at 5/6 dB, full band | 187 ms read + decode | previous | `research/scenarios/settle_tolerance.txt` |
| the same, before the receive-speed work | 322 ms | previous | not in this repository |
| keyed 1.0 → 2.4 Msps read, before → after the speed work | ~230 → ~120 ms; open ~340 → ~115 ms | previous | not in this repository |
| blind recovery after a width switch (2.4 Msps) | 2.2–4.3 s CPU, once per sender | previous | `research/scenarios/node_runtime.txt` |
| blind acquisition of a narrower sender (1.2 → 2.4 Msps) | ~40 s | previous | `node.py` docstring |

The receive path offers the frame CRC as the acceptance test first, profiles from the rows already read, memoizes the settle objectives, narrows the carrier reach to 0.2 cycles/row, settles the start on a parabola, caches mode keys and computes soft bits per hypothesis lazily. The blind costs on the last two rows were measured with the dark-mode read. The constant-modulus read costs 1.4–3.3× as much at the codec level (§M-3.1), and the mesh's blind paths have not been re-timed.

**Settle tolerance.** `SETTLE_FTOL = 1e-2`. On 60 noisy bursts at 5 and 6 dB, every tolerance from 1e-9 to 1 decoded 30/30 at both levels, at 362 ms per burst (1e-9), 234 ms (1e-2) and 168 ms (1.0) (`settle_tolerance.txt`, previous waveform). The tolerance was chosen on an earlier sweep in which 1.0 lost one burst.

A width change shows up as the fast read failing. The node then tries each known scale with the band centre measured from the capture, and finally a gated blind read. Measured on the previous waveform: a sender switched from 1.2 to 2.4 Msps mid-conversation, and 6/6 second messages were delivered after one blind recovery each (`node_runtime.txt`).

**The geometry read sets no planner threshold.** Near threshold, 0–17% of lost frames read with raw BER > 0.2 (the most: DBPSK 1/2 17%, DQPSK 1/2 15%, 4-QAM 1/2 13%, at 3–7 dB), and some of those are decode losses (`kL.md`, second table). The thresholds of §M-4.1 are set by the code and the modulation.

### M-3.3 Family, alphabet and rate are arbitrated by the CRC

Nothing in a burst names its modulation family, alphabet or code rate, and nothing about them is agreed. The receiver reads every burst both ways at the measured geometry (§M-3.2). All (family, M, rate) hypotheses then go through one batched Viterbi pass, and the first hypothesis whose CRC-32 and symbol header both pass wins (`link.read_frame`). The burst's measured length cuts the 24 hypotheses to 1–3 in most cases.

The alternative was a self-describing header row. It was rejected on two measured grounds (`link.read_frame` docstring). It would cost 0.7% of goodput at DBPSK 1/2 and 7.1% at 256-QAM 3/4, most where the rate is highest. It would also be a fixed-format field in a fixed place on every burst, which is the repeated structure the no-added-structure rule keeps off the air.

Wrong-mode decodes: 0 in 466,506 keyed and open frames over 24 modes at K = L, with no CRC pass on the wrong content (`research/mcs/kL.md`, second table). Also 0 over every cell of the previous waveform's `results_phy_link_{12,24}.json`, and over about 1150 earlier bursts.

**Capture tail.** `RxBurst.rows` checks its edge against the burst's run of rows, so a capture that ends less than a row after a burst still reads it: 256-QAM 3/4 at 50 dB, with the capture ending 0.5–0.7 of a row after a 1.0 Msps burst, loses 0/80 frames, and 0/80 at matched rate (`kL.md`).

### M-3.4 A time budget for every burst

A known-geometry read plus decode costs about 0.04–0.28 s in Python on the current waveform (above). A blind read of a narrow sender costs tens of seconds. The node bounds what it spends: blind acquisition runs under `blind_scaled` / `blind_every`, and a level gate skips captures with nothing above the measured floor (`node.py`). That is a CPU budget. The per-message *delivery* budget is separate (§M-5.4).

### M-3.5 What is on the wire, and what adding no structure costs

The mesh adds no repeated structure to the PHY's waveform, and the PHY's own key-free leaks have been closed to second order.

- **No cyclic extension (P = 0).** A per-row extension of P=2 at L=128 bought about 2 dB of floor at non-unit ρ. It also showed a lag-128 correlation line at z 8.4 at −5 dB and 15.7 at 0 dB on 83 ms of capture. Tapered row edges were worse: a row-rate line at z 55 at 0 dB (`entroptics-rf/research/extension.md`). The sender writes none, and the receiver searches none. The floor is recovered in the receiver by the joint read, which models the neighbouring rows' converter tails and puts nothing on the air. It takes the noiseless floor from −22 dB to −66…−69 dB at a cost of 9–23× the row read (`entroptics-rf/research/joint_read.md`).
- **No header row**, as above.
- **No mirrored band** (§M-6).

**The standard battery.** The rf battery of ten blind detectors read a worst |z| of 2.7 over 96 traces (§RF-5.2). That was measured on the previous screen (K = 60), and one of its detectors, the eigenvalue MME, read +2.5 to +2.7 in every run. That was a real bias, caused by the rank deficiency below (§RF-5.2).

**Key-free leaks: the second-order ones are closed; a fourth-order one remains.** Three features of the earlier construction were readable without a key, and two wire changes close them:

1. **Equal row energy.** Every row carried the same energy, so the row period L/ρ was scorable (z 38 at 0 dB from 417 ms, L=64). Closed by the keyed per-row power r_t² ~ Gamma(L, 1/L). Every energy detector reads inside the noise-only range. It costs ~0.1 dB on DQPSK and +0.14–0.21 dB of PAPR (`entroptics-rf/research/row_energy_leak.md`). In open mode the radius is drawn at random by the sender.
2. **Rank deficiency** (K < L), and with it **the fold and the spectral ripple.** A spare mode is a noise-only direction of the L-sample window covariance. It is not bounded by SNR: at L=64 a 10 s capture shows the true width at z 19–27 from −15 dB, one spare mode is as visible as eight, and ρ ≠ 1 does not mask it. Closed by cure (e): every mode is lit (K = L), and the receiver finds the geometry by the keyed constant-modulus read instead of a dark mode. On the codec's own transmissions the rank, fold, lag, row-energy and kurtosis detectors read noise at 417 ms, 1.67 s and 10 s (ρ = 1, L = 64 and 128), at 417 ms and 1.67 s at ρ 0.514 and 0.833, and open at 417 ms, from −15 to +10 dB (`entroptics-rf/research/rank_leak.md`, The shipped codec; §RF-5.3). It costs 0.28 dB per symbol at L=64 for +6.7% bits per row. Through the mesh's receive path the coded thresholds of the code-limited modes moved +0.1 to +0.9 dB (§M-4.1).
3. **What remains is fourth-order.** Given the width and the row offset, the fourth-order cumulant of whitened windows shows the constant-modulus structure: at L=64 it is noise at 0 dB and detects at +10 dB in 417 ms; at L=32 it reaches 0 dB (§RF-5.3). The same structure is what ACMA and ILSP invert: given the width, the offset and a long enough capture they recover the mode coefficients, and what they leave undetermined, a per-mode constant phase, is where the keyed per-row rotation sits (§RF-5.4, §RF-5.5). A keyed aperture per absolute row would close the fourth-order read, at the cost of the blind late join (`rank_leak.md` §3). It is not adopted.

So the mesh's detectability claim is about structure and content, not energy. The energy is visible: a radiometer sees the transmission and out-detects the decoder by about 20–28 dB (`entroptics-rf/research/detectability/README.md`). What is claimed is that the mesh adds no repeated structure, that no second-order key-free read of the PHY finds its width (measured on the codec's own path to 10 s of capture), so a blind classifier cannot identify it as a structured signal, and that the content is unreadable without the key. A fourth-order observer that already holds the width and offset is not excluded.

**On hardware**, against a transmitted-Gaussian-noise null of the same power, width and chain, the current waveform read as noise on every one of seven detectors (covariance rank and rank-1, lag correlation, two row-energy contrasts, kurtosis, and the fold) across all eight rate pairs: worst |z| ≤ 2.3, inside the noise-vs-noise leave-one-out spread of up to 5.5, while the K = 60 control still lit rank at z 83 and the fold at z 18–33 **[measured]**. The right null on hardware is a transmitted signal: the front-end filters shape any transmission, so a TX-idle or synthetic-white null makes even transmitted Gaussian noise look structured (that is why the first hardware pass against those nulls lit rank, lag and kurtosis for the waveform *and* the control alike). Front-end structure (I/Q imbalance, DC, AGC) is folded into this comparison, since the waveform and the null pass through the same chain. The fourth-order read that already holds the width and offset was not part of this battery and has not been run on hardware.

**Open mode.** With the null crypto provider, the PHY uses the public aperture. Anyone can derive it, and an open broadcast is readable by anyone. It is whitened: it is the keyed construction with an empty seed field, so its occupancy reads like a keyed one. On the previous waveform, whitening took keyless blind decode in the mesh from 31 s to 7.5 s (keyed: 1.0 s), and the fast path from 0/5 to 64/64 across open/keyed × narrow/full × zero/random payloads. Open mode keeps identity per-row mode keys by design, so a receiver can join anywhere. As a consequence, a constant payload in open mode produces rows that, by construction, differ only by the sender-random row power: periodic structure an observer can see (observed before the keyed row power was added; not re-measured). This is noted, and it is not changed. No open-mode K estimate is needed, since K = L.

Open coherent reception measures each row's level itself (`waveform._row_levels`), where keyed reception divides the known radius out. On the 4-QAM hypothesis (constant |x|², zero spread) the spread is floored at 1e-6 (`entroptics-rf/src/entroptics_rf/waveform.py`), so the level is always defined; without the floor the read dropped 3–10% of open frames at every SNR (`kL.md`). Open 256-QAM 3/4 reads 1.5 dB worse than keyed (`kL.md`, second table).

---

## M-4. The link: FEC inside, fountain across, no ARQ

**Inside a burst** (`fec.py`): payload + CRC-32. A rate-1/2 K=7 convolutional code (171, 133 octal) with zero tail, punctured to 2/3 or 3/4 on the 802.11 patterns. A 16-column interleaver. Soft-input Viterbi. Soft bits come from the PHY's differential read (angle distance, |d|-weighted; with the keyed row power the magnitude is r_t r_{t+1}, so soft bits are SNR-weighted) with Gray labelling done inside the FEC layer, so the wire format is untouched. For coherent QAM, soft bits are max-log LLRs with the noise measured per mode from the burst's own decision residuals. No noise level is passed in.

**Across bursts** (`fec.py`, `link.py`): a message is cut into k blocks and sent as fountain symbols. Each symbol rides one 512-byte frame: a 35-byte symbol header (origin, seq, destination, hops, time budget, k, block, length, symbol id), a 475-byte block, and CRC. Any spanning set of symbols, in any order and from any mix of paths, rebuilds the message. LT with robust soliton was inefficient at small k: a k=6 message at 9 km hops reached the far node with 8–10 distinct symbols and failed (`research/scenarios/subband_relay_9km.txt`: same-channel 1/8, split 2/8). For k ≤ 64 the code is systematic with dense repair, decoded by Gaussian elimination. At 30% erasures, mean overhead is 1.09 / 1.02 / 1.01 symbols per block at k = 16 / 64 / 256, worst seen 1.44 / 1.16 / 1.03 (`link.py` docstring); LT gives 1.17 at k=16, worst 2.19. With the dense code the 9 km chain on the current waveform delivers 8/8 both same-channel and split sub-band (`subband_relay_9km_current.txt`).

**Redundancy is chosen up front.** With no feedback, the sender cannot know when every receiver has enough symbols. It sends ceil(k × redundancy) symbols, where redundancy is a per-message choice an agility provider may steer. The relay policy then trims what is not needed (§M-5).

**Per-burst success vs SNR** (blind PHY, current waveform, r=1/2 DQPSK frames, 100 bursts per point; `research/scenarios/burst_success_current.txt`):

| SNR dB | 4 | 6 | 7 | 8 | 9 | 10–16 |
|---|---|---|---|---|---|---|
| geometry found | 100 | 100 | 100 | 100 | 100 | 100 |
| frame decoded | 5 | 89 | 98 | 100 | **99** | 100 |

The planning target **HOP_SNR_DB = 9 dB** (≥ 99% per burst) comes from this table. On the current waveform the geometry is found on every capture from 4 dB up, and the frame decodes well below 9 dB (89% at 6 dB, 98% at 7 dB), consistent with DQPSK 1/2 reaching 1% PER at 7.2 dB on the fast path (§M-4.1), so the 9 dB target is comfortably conservative.

### M-4.1 The MCS envelope

The planner's table (`adapt.MODES`) is the second table of `research/mcs/kL.md` (`results_kL_fp.json`, `kl_cells_fp.jsonl`, `mcs_kl.py`). Each frame goes through the chain a node uses: a fountain symbol, the 512-byte protected frame, `phy.transmit_burst` at K = L = 64 with the keyed row power and reference row, the emulated air over 6 km, the fast path (`receive_known`, handed a prior 2 ppm off in scale and 5e-4 of the band off in centre) and `read_frame` with nothing about the mode passed in. Two rates: matched 2.4 Msps, and a 1.0 Msps sender into 2.4 Msps; the planning threshold is the worse of the two, rounded up. The run used 1397 keyed and open cells and 466,506 frames, with no undetected error, no wrong-mode decode and no receive-path exception. The 1%-PER point comes from a binomial fit of log PER against SNR (90% interval about ±0.1–0.2 dB).

| mode | matched dB | 1.0 → 2.4 dB | planning dB | goodput b/sample | on envelope |
|---|---|---|---|---|---|
| DBPSK 1/2 | 3.86 | 3.93 | 4.0 | 0.489 | yes |
| DBPSK 2/3 | 5.27 | 5.51 | 5.6 | 0.646 | differential only |
| DBPSK 3/4 | 6.15 | 6.36 | 6.4 | 0.727 | differential only |
| DQPSK 1/2 | 6.90 | 7.11 | 7.2 | 0.970 | differential only |
| DQPSK 2/3 | 8.89 | 8.92 | 9.0 | 1.280 | differential only |
| DQPSK 3/4 | 9.87 | 10.10 | 10.1 | 1.422 | differential only |
| D8PSK 1/2 | 11.09 | 11.22 | 11.3 | 1.422 | |
| D8PSK 2/3 | 13.49 | 13.61 | 13.7 | 1.882 | differential only |
| D8PSK 3/4 | 14.89 | 15.01 | 15.1 | 2.133 | differential only |
| D16PSK 1/2 | 15.67 | 15.82 | 15.9 | 1.882 | |
| D16PSK 2/3 | 18.90 | 18.99 | 19.0 | 2.462 | differential only |
| D16PSK 3/4 | 20.41 | 20.62 | 20.7 | 2.783 | differential only |
| 4-QAM 1/2 | 4.29 | 4.34 | 4.4 | 0.970 | yes |
| 4-QAM 2/3 | 6.32 | 6.47 | 6.5 | 1.280 | yes |
| 4-QAM 3/4 | 7.53 | 7.70 | 7.7 | 1.422 | yes |
| 16-QAM 1/2 | 10.05 | 10.11 | 10.2 | 1.882 | yes |
| 16-QAM 2/3 | 12.57 | 12.77 | 12.8 | 2.462 | yes |
| 16-QAM 3/4 | 14.14 | 14.39 | 14.4 | 2.783 | yes |
| 64-QAM 1/2 | 15.57 | 15.94 | 16.0 | 2.783 | |
| 64-QAM 2/3 | 18.07 | 18.30 | 18.4 | 3.556 | yes |
| 64-QAM 3/4 | 19.75 | 19.95 | 20.0 | 4.000 | yes |
| 256-QAM 1/2 | 19.01 | 19.27 | 19.3 | 3.556 | |
| 256-QAM 2/3 | 23.84 | 24.00 | 24.1 | 4.571 | yes |
| 256-QAM 3/4 | 26.05 | 26.28 | 26.3 | 5.333 | yes |

**The envelope over both families:** DBPSK 1/2 (4.0 dB) → 4-QAM 1/2 (4.4) → 2/3 (6.5) → 3/4 (7.7) → 16-QAM 1/2 (10.2) → 2/3 (12.8) → 3/4 (14.4) → 64-QAM 2/3 (18.4) → 3/4 (20.0) → 256-QAM 2/3 (24.1) → 3/4 (26.3, 5.333 b/sample). The differential-only envelope (DBPSK 1/2–3/4, DQPSK 1/2–3/4, D8PSK 2/3 and 3/4, D16PSK 2/3 and 3/4) ends at D16PSK 3/4, 2.783 b/sample at 20.7 dB. The coherent-only envelope starts at 4-QAM 1/2, 4.4 dB, and has nothing below it. Within the differential family D8PSK 1/2 (by DQPSK 3/4) and D16PSK 1/2 (by D8PSK 3/4) are dominated, and within the coherent family 64-QAM 1/2 and 256-QAM 1/2. A whispered hop (12 dB, §M-6) plans DQPSK 3/4 differential-only and 16-QAM 1/2 when coherent is allowed.

What sets the thresholds (`kL.md`, second table):

- **Coherent QAM sits about 2.8 dB under the differential mode of the same goodput** (4-QAM 1/2 at 4.4 dB against DQPSK 1/2 at 7.2), the textbook coherent/differential gap. One coherent gain for every mode (`phy._common_gain`) is what puts it there: dividing each mode by its own gain off the single reference row spent that row's noise and inter-mode mixing again on every symbol.
- **The fast path reads coherent QAM below the K = 60 true-geometry tables** (`results.md`, `results_gray.json`, `coherent.md`), which used per-mode gains: 4-QAM 1/2 is 4.4 dB where they had 6.7, and 256-QAM 3/4 26.3 where they had 28.2. Those tables measured the code and the modulation, not synchronization; they are kept as reference and do not feed the planner.
- **Matched and 1.0 → 2.4 Msps differ by 0.2–0.4 dB** at every rate, so the planning threshold, the worse of the two, costs little.
- **The geometry read is not the limit** (§M-3.2): its share of losses near threshold is 0–17%.

**The residual row turn on 256-QAM 3/4.** Frames delivered of 32 on the fast path, at 26 / 27 / 28 / 29 / 30 dB (measured with per-mode coherent gains, before `phy._common_gain`; it compares the turn reads, not thresholds):

| | matched | 1.0 → 2.4 Msps |
|---|---|---|
| no residual-turn read | 10 / 20 / 25 / 29 / 30 | 6 / 15 / 23 / 26 / 27 |
| decision-directed (not used) | 15 / 25 / 31 / 32 / 32 | 13 / 23 / 32 / 32 / 32 |
| **`_row_turn`** (shipped) | **18 / 27 / 29 / 32 / 32** | **12 / 23 / 31 / 32 / 32** |

**Reference figures at the true geometry** (K = 60; `coherent.md`). ρ = 0.514 through the joint read lands within ±0.5 dB of matched rate, except 64-QAM 1/2 and 2/3 (+0.6 and +0.8 dB).

**Open mode** (spot check, 300 frames per cell; `kL.md`, second table): open reads as keyed within 0.2 dB for DBPSK 1/2, DQPSK 3/4, 16-QAM 1/2 and 64-QAM 2/3. Open 256-QAM 3/4 is 1.5 dB worse (27.55 / 27.78 dB against 26.05 / 26.28 keyed), because an open receiver levels each row itself (`waveform._row_levels`). No open frame raised.

### M-4.2 Delivered goodput through the mesh PHY

This is a two-node link on the emulated air at 6 km (`research/mcs/goodput_kL.md`, `results_phy_link_kL_{10,12,24}.json`, `phy_mcs_link.py`). The receiver holds the sender's geometry (fast path) and learns family, M and rate from the burst. Goodput is frame bits per complex sample at the sender's rate, delivered frames only, 16 frames per mode and cell. The run sent every mode, on a receive path that predates the read changes behind `kL.md`'s second table, so it checks that each of today's planner picks delivers, not where the thresholds sit. The picks are `adapt._mcs_for` on `adapt.MODES` with no margin.

| in-band SNR | planner pick, differential only | planner pick, coherent allowed | best delivered |
|---|---|---|---|
| 10 dB | 1.280 (DQPSK 2/3, 16/16 at every rate) | 1.422 (4-QAM 3/4, 16/16 at every rate) | 1.882 (16-QAM 1/2, 16/16) |
| 20 dB | 2.462 (D16PSK 2/3, 16/16 at every rate) | 4.000 (64-QAM 3/4, 16/16; 15/16 at 1.0 → 2.4) | 4.000 (64-QAM 3/4) |
| 30 dB | 2.783 (D16PSK 3/4, 16/16 at every rate) | 5.333 (256-QAM 3/4, 16/16 at every rate) | 5.333 (256-QAM 3/4) |

Rates are matched 2.4 Msps, 1.2 → 2.4 and 1.0 → 2.4 Msps. Wrong-mode decodes: 0 of 2729 delivered frames.

---

## M-5. Relaying

All of these rules live in `relay.py`. The emulator (`emu/subband_run.py`) and the node runtime (`node.py`) both call it, so the two cannot drift apart. The adopted policy is `relay.ADOPTED`: fresh ×2 with a rank margin of 12, passive ack on, sibling share on (`relay.Policy`). The ×2 budget is measured against ×3 in §M-5.5. The scenarios in this section were measured on the current node runtime (real entroptics-rf IQ through `FleetRun`, or the calibrated capture model for volume; `research/scenarios/*_current*`).

### M-5.1 Decode-and-forward with fresh symbols

A relay can repeat the symbols it caught (plain relaying) or listen until it has rebuilt the message and then send *fresh* fountain symbols from its own region of symbol ids (decode-and-forward). On a 3-node chain at 6 km, a 2400-byte message (k=6), on one shared 2.0 Msps band (8 seeds, real IQ; `research/scenarios/relay_df_current.py`), decode-and-forward delivered 8/8 at ×1.5 redundancy in 18 bursts. Plain relaying matched that 8/8 only at ×3, and then took 31 bursts (at ×2 it lost one, 7/8, in 21 bursts). Fresh symbols carry more downstream than repeats of caught ones, so decode-and-forward reaches full delivery at about 40% less airtime; it also sidesteps the shared channel, where a plain relay's bursts collide with the origin's next ones because a radio cannot hear while it sends.

A relay's id region is derived from its node id (`relay.region_of`), so there is nothing to agree. Two nodes share a region with probability 1/4095, which costs efficiency and never correctness.

### M-5.2 Each relay on its own sub-band

The collision finding made sender-chosen width and centre the core MAC mechanism, not only an interference-avoidance feature. The setup: a 3-node chain, 1.0 Msps senders into 2.4 Msps receivers, equal power. The 'same' arm puts origin and relay both at −0.6 MHz. The 'split' arm moves the relay to +0.6 MHz. Every receiver reads every geometry it knows from each capture and keeps every frame that passes CRC.

| hop | same sub-band | split sub-band | source |
|---|---|---|---|
| 9 km, 8 seeds | 8/8, 18 bursts | **8/8**, 18 bursts (far end reads two frames from one capture in 1–3 runs) | `subband_relay_9km_current.txt` |

On the current waveform both arms deliver at this load. The split arm's advantage is capture efficiency: the far node reads the relay's and the origin's frames from the same capture (1–3 of the 8 runs), so it fills the fountain faster (far-end symbols 18 against the same arm's 12–16). The advantage widens with denser traffic, where same-channel bursts collide. Two simultaneous 1.2 MHz bursts in a 2.4 MHz capture decode 6/6 each when disjoint, 0/6 each when half-overlapping, and 0/6 each on the same band (`research/scenarios/subband_overlap.txt`; not re-measured on the current waveform). So sub-bands are chosen disjoint, never overlapping.

### M-5.3 Passive ack

An origin that overhears a symbol of its own message that it never sent knows a relay has rebuilt the message. It drops whatever it still has queued for that message. This costs zero airtime and sends nothing back. Measured on the current node runtime, 9 km single-relay chain, 2400-byte message (k=6), 10 seeds, adopted relay policy (`passive_ack_current.txt`, real IQ):

| origin redundancy | delivered, without / with | bursts, without | bursts, with passive ack |
|---|---|---|---|
| ×1.5 | 10/10 / 10/10 | 27 | 24.7 |
| ×3 | 10/10 / 10/10 | 36 | 24.7 |

With passive ack the origin stops at the same ~6.7 bursts whichever redundancy it budgeted, so the airtime lands at ~24.7 bursts either way (the relay carries the rest under its own policy). So a sender can budget generous redundancy against node loss and pay for it only when it is needed.

### M-5.4 Hop and time budgets

A relay forwards only while `hops > 1`, and decrements it. Each symbol header also carries `budget_ms`. Each node subtracts only its own holding time, which is a duration on its own clock, so no shared clock is needed. A message whose budget is spent is dropped (`routing.spend`). On the 2-hop 9 km chain (4.71 ms slots), no budget and a 200 ms budget both delivered at slot 26 (122 ms) in 16 bursts. A budget of 40 or 60 ms abandoned the message after 5 or 6 bursts (`research/scenarios/budget_chain.txt`). The tunnel maps a best-effort packet's deadline into `budget_ms` (§M-8).

### M-5.5 Sibling share, and node loss

A relay budgets to carry its hop alone. For each fresh symbol it hears from a sibling at the same hop count, it cancels one queued symbol. When it hears a later hop, it cancels all of them. There is no handshake: a dead sibling is simply not heard, so the survivors send in full.

**Node loss on the current node runtime** (`research/scenarios/node_loss_listen.txt`, real entroptics-rf IQ, 10 seeds per arm, 9 km hops, 2 km between siblings, adopted ×2 policy). A relay is taken out of service before it rebuilds the message, the moment it rebuilds it, or just after its first relayed burst; or two of three relays are lost; or one relay is lost per stage over two stages (27 km). Across every loss arm the message was still delivered:

| arm | 48-byte telemetry (k=1) | 2400-byte message (k=6) |
|---|---|---|
| diamond, relay A lost (early / on rebuild / after first forward) | 10/10 each | 10/10 each |
| tri, relays A and B lost on rebuild | 10/10 | 10/10 |
| two stages, one relay lost per stage | 10/10 | 10/10 |
| all-alive controls (diamond, tri, two stages, anycast) | 40/40 | 40/40 |
| single relay lost (no redundant path; control) | 0/10 | 0/10 |

Totalled, the loss arms deliver **60/60** and the all-alive arms **40/40**, identically at the ×3 and the adopted ×2 budget. The cancellations from sibling share pay for the budget, so with nothing lost the airtime is no worse than a thinner fixed budget would use; a lost relay costs a few slots of latency. (The one residual is a *lone* surviving relay of a multi-block message under ×2 — single_alive k=6 read 9/10 — which the rank margin fixes, below.)

**Budget ×3 → ×2 + rank margin** (`relay.Policy`; `research/scenarios/node_loss_listen.txt`, `showcase_budget.txt`). With the listen-before-talk access layer in place, ×3 is over-provisioned. A re-measurement on real PHY IQ (FleetRun, 10 seeds per arm, 9 km hops, one message) found ×2 delivering exactly as ×3 in every node-loss arm — 60/60 across the loss arms and 40/40 across the all-alive arms, at both k = 1 (48-byte telemetry) and k = 6 (2400-byte) — at about 20% fewer relay bursts and about 20% lower latency (all-alive k = 6: 20.3 bursts against 26.4). The fleet showcase agreed: ×2 delivered 210/210 at 63 ms median / 185 ms p90 against ×3's 210/210 at 81 / 232 ms, with 924 bursts against 1146 (`showcase_budget.txt`). The one place a thinner budget fell short was a *lone* surviving relay of a multi-block message: ×2 leaves it 2k fountain symbols, which left the small-k dense code short of full rank about 1–2% of the time (single_alive, k = 6: 9/10 at ×2 against 10/10 at ×3). `rank_margin = 12` fixes exactly that — a relay of a k ≥ 2 message sends at least k + 12 symbols regardless of the budget, so a lone relay reaches full fountain rank — which restored single_alive k = 6 to 80/80 in the model, while k = 1 telemetry still sends two symbols. The adopted policy is therefore ×2 with rank_margin 12 **[measured; model for the rank_margin arm]**.

**Sibling bands.** A node avoids every centre it heard the message on, not just the one it arrived on (`node._heard_bands` → `adapt.default_plan(avoid=)`). When no band is clear of them, the choice spreads at random over all bands rather than falling back to one, since with two slots both relays otherwise took the same band in every burst. In the diamond with two 1.2 MHz slots this gave no measurable benefit: avoid delivered 10/10 with median slot 16.5 and 29.1 bursts, plain delivered 10/10 with median 16 and 28.0 (`sibling_bands.txt`, not re-measured on the current waveform). With only two slots, siblings still collide, and sibling share absorbs it. The rule is kept because it is cheap. No gain is claimed. Wider bands with 3+ siblings are untested.

### M-5.6 The flooding baseline and the departure run

The first router is reply-free controlled flooding with duplicate suppression by message id (`routing.py`). Without CRC, collisions corrupted headers into phantom messages and caused relay storms. With FEC+CRC, a 5-drone line at 2.5 km spacing and 20 dBm delivered to all 4 drones in 6/6 runs, and each node transmitted exactly once.

**Fleet departure** (`research/scenarios/fleet_departure_current.py` → `fleet_departure_current_real_{1,2,3}.jsonl`; current node runtime, real IQ, free space, 20 dBm, −100 dBm noise; 3 seeds of 20 message epochs). A lead node moves away from the base at 60 m/s, with relays placed at 2.5, 5 and 7.5 km along its path, sending telemetry home every 25 s. The base received **18–19 of 20 messages, out to 25.5–27 km**, and lost the message only from 27 km on. By then the lead had moved out of range of its last relay (held at 7.5 km): the lead-to-relay link there is about 2.5 dB, below what a hop needs, and the direct lead-to-base link is near 0 dB. This failure is what fixes the placement rule (§M-9) — every chain link needs ≥ 9 dB + margin. Free space is optimistic; a two-ray check (base 10 m, drones 100 m) puts the breakpoint near 38 km, so the reach to about 25 km stands roughly. A replay viewer renders an earlier run of this scenario (`research/viewer/fleet_departure.html`).

Least-cost and position-aware routing are not built. Flooding is correct with no knowledge, and smarter routers will be measured against it.

---

## M-6. Audience-aware planning

The sender's plan is fitted to an *audience*: a class of receivers listening within a band, at a distance that sets the power needed. The rules (`adapt.py`):

- **Nothing is required from the receiver.** The sender learns from what it hears: announcements (position → distance → power; band → width) and other nodes' transmissions read blind. With nothing heard, it **shouts**: full width, full power.
- **Whisper when the audience is known.** At equal power, halving the width raises in-band SNR by about 3 dB. When a destination's position is known, the plan narrows until full-power SNR clears HOP_SNR_DB + MARGIN_DB (9 + 3 dB), then cuts power to that target. A relay with no destination position halves the width, which gives two sub-bands, and takes the one it did not hear.
- **Clues, not constraints.** A neighbour's announced capture range bounds only what is sent *to* that neighbour. Hearing a narrowband neighbour never forces narrowband, and adaptation must never lock a sender into the most limited mode it has heard.
- **Never mirror, and keep off siblings.** A relay never reuses the sub-band it heard the message on (`heard_on`; measured gain in §M-5.2), and avoids every other band it heard the same message on where a clear band exists (§M-5.5). The choice among the rest varies at random. Reusing the band just heard would be a repeated pattern.
- **MCS from the measured envelope.** The plan picks the highest-goodput mode whose 1%-PER threshold the predicted SNR clears, among the families the node may send, from the table measured at K = L through the node's own receive path (§M-4.1). The default is `families=("differential",)` because coherent QAM reads reliably on hardware only at ≤ 0.5 occupancy, not at full-occupancy matched rate (§M-11). At the whisper target of 12 dB the pick is DQPSK 3/4 differential-only, and 16-QAM 1/2 (10.2 dB) when coherent is allowed.
- **The agility provider has the last word.** If a provider widens or quiets the plan, `adapt.refit` steps the mode down to the best one whose threshold clears the old threshold minus the loss (10 log10 w plus the power drop). A narrower or louder plan keeps its mode. If the provider chose its own alphabet and rate, they are left alone. The node applies it on every burst (`node.plan_for`).

**The optional "I'm here"** (`announce.py`) carries position, velocity, the capturable band range, and a heard list of node ids with SNR. SNR is a physical measurement of the channel. Decode-quality figures (BER, closure) are feedback and are excluded. The content is topology, so it travels sealed by the crypto provider (plain under the null provider). Nothing depends on an announcement arriving. Measured and announced knowledge are kept apart in `neighbors.py`, because they are trusted differently.

---

## M-7. Multi-homing: anycast HOME

Every gateway accepts the address HOME. A drone that has heard any gateway sends everything else to HOME, so "home" is whichever gateway hears it. Gateways share a wired backhaul. The first gateway to decode a message hands it to the gateway anchoring that drone's flows, and a duplicate decoded elsewhere is dropped (`node.Backhaul`, `tunnel.Backhaul`).

Measured on the current node runtime (real entroptics-rf IQ, 10 seeds per arm; `research/scenarios/multihome_current.txt`): origin → 2 relays (9 km, 2 km apart, sibling share) → 2 gateways (18 km, 4 km apart, wired backhaul); in the kill arms G1 dies when relaying starts; the message is addressed to HOME (anycast) or to G1 (unicast).

| arm | delivered | bursts (mean, k=6) |
|---|---|---|
| anycast HOME | 10/10 | 24.7 |
| anycast HOME, G1 out of service | **10/10** via G2, same delivery slots | 24.7 |
| unicast to G1, G1 out of service | **0/10** | 42.6 |

With a gateway lost, anycast reaches home through the other gateway with no change in delivery, while unicast to the lost gateway fails outright. Anycast also uses fewer bursts — 24.7 against the unicast arm's 42.6 — because whichever gateway completes first ends the message for both.

---

## M-8. IP encapsulation

Full design and measurements: `research/encap.md`, `research/encap/results.txt`. In summary:

**Tunnel** (`tunnel.py`). A raw IPv4/v6 packet becomes a record inside a mesh message, which is sealed by the crypto provider. The tunnel adds three things:

- *Addressing.* An IP prefix → mesh address map with longest-prefix match. Entries are configured at their owner, announced in ROUTES records with 60 s ageing, or gleaned by gateways from source addresses with 30 s ageing. Gateways announce `0/0 → HOME`.
- *Packing.* Records bound for one address share a message. Aggregation is whatever queued behind the transmission ahead, so packing adds no latency. The tunnel never IP-fragments; it uses its own FRAG records, and a packet over the MTU is refused so the host's PMTUD sees the real limit.
- *Two classes.* Best-effort is stateless, carries no ack, and drops a packet still queued past its deadline. It has an optional `newest` mode for video and telemetry. Reliable is opt-in per flow: 16-bit seq, cumulative + 64-bit selective ACK, piggybacked. The low-water mark rides in every record, so a receiver, or a different gateway after failover, synchronizes from any one record with no handshake.

The reliable class is the only place anything comes back, and delivery never depends on it.

**Overhead** (512-byte frame per block, redundancy 1). The table was measured with a 477-byte block and a 33-byte symbol header, where link framing alone caps efficiency at 93.2%. The block is now 475 bytes (the 35-byte header carries `budget_ms`), which caps it at 92.8%, so the rows are about 0.4% optimistic (`encap.md`).

| IP packet | efficiency, sent alone | efficiency, packed (radio busy) |
|---|---|---|
| 64 B | 12.5% | 83–86% |
| 200 B | 39.1% | 89–91% |
| 512 B | 50.0% | 87% |
| 1400 B | 91.1% | 91% |

The tunnel's own header is 2–4 B best-effort and 5–7 B reliable. Block padding, not the tunnel, is the overhead, and packing recovers it.

**Reliable class against message loss** (`emu/ipnet.py` message-level model: 200 blocks/s per radio, 20 ms + 20 ms jitter, redundancy 1.5/2.0, 5 seeds):

| message loss | bulk: in order, goodput efficiency | paced: delivered, p50 / p99 / max ms |
|---|---|---|
| 0% | 5/5, 47.1% | 100%, 44 / 52 / 52 |
| 5% | 5/5, 43.3% | 100%, 48 / 208 / 254 |
| 20% | 5/5, 30.8% | 100%, 114 / 398 / 458 |
| 40% | 5/5, 20.0% | 100%, 184 / 718 / 778 |

This is a message-level model, not RF.

**TCP** (`tcpproxy.py`) never crosses the mesh. Each edge terminates it locally, and the byte stream rides the reliable ordered class, one flow per direction per connection. OPEN carries the first data, and there is no open handshake over the mesh. State is 43 B per connection. A proxied connection dies with its anchor gateway: the application sees a reset, not a hang. Datagrams fail over without loss.

**BGP** (design only, `encap.md`). No BGP runs on drones. Gateways originate the fleet prefixes by eBGP, conditionally on having heard a drone recently, so the prefix is anycast at the IP level as HOME is on the mesh. Gateways run iBGP and BFD over the wired backhaul, optionally with host routes for drones each gateway hears well. Gateways with no wire peer through the tunnel, either proxied or raw, with loose timers and graceful restart.

An OS network adapter is built and demonstrated live: a TAP-based L2 bridge (`l2bridge.py`, driven by `run_bridge.py`) that carries raw Ethernet — including DHCP — across the mesh between real hosts, used for the live distributed-fleet bring-up (§M-11, item 1). **Still not built:** an L3 TUN/socket adapter, congestion control and airtime fairness, proxy-state replication, and header compression. The node runtime does attach a `Tunnel` (`node.attach`), and the tunnel sets `budget_ms` (`encap.md`).

---

## M-9. Placement and plausibility

**Relays are a role** (`placement.py`). A relay is any node assigned to hold a position; the other nodes keep moving. Every link is held to HOP_SNR_DB + MARGIN_DB. A measured SNR from a heard list beats a free-space prediction from two claimed positions. Predicted links pay a 10 dB fade margin and are flagged, because positions are claims. Measured SNRs pay no margin, since they already contain the fading. The radio horizon caps every hop. Relays come in parallel, two per slot by default, because one relay per slot delivered 0/10 when it was lost and two with sibling share delivered 20/20 (§M-5.5). Slots are placed at 0.8 × the maximum hop.

Planned figures at 915 MHz with the `adapt.Radio` default noise (−100 dBm over the radio's full rate, about NF 10 at 2.4 MHz), 0 dBi unless stated, a 12 dB target plus the 10 dB fade margin on predictions **[model]**:

| radio | max hop at 2.4 / 1.0 / 0.25 Msps | relays to span 1500 km, link-budget arithmetic (2 per slot) |
|---|---|---|
| 1 W omni drone at 120 m | 6.5 / 10.1 / 20.3 km | – |
| 4 W, 6 dBi relay at 1000 m | 52 / 81 / 161 km | 70 / 46 / 22 |

**GPS may lie** (`plausibility.py`). Free space is the best case: a signal can lose more than the Friis loss, never less. A measured SNR therefore caps how far away its sender can be, at the sender's full power and both antenna gains. A claimed position outside any observer's cap, with 3 dB of tolerance, is flagged. The test is one-sided, so a weak signal from a near claim never accuses anyone. Successive claims are also checked against a maximum speed (default 150 m/s). A flag names an inconsistent pair or set, not a culprit, because observers' positions are claims too. Distrusted claims buy no predicted links, no anchoring and no planning toward them. Heard links still count. A claim is re-judged on every new SNR measurement of its sender, in both directions (distrust and vindication), keeping the last speed verdict (`node.Node._record`; one test each way).

---

## M-10. Node runtime and emulator

**`Node`** (`node.py`) is a state machine with no clock and no radio of its own. Every call takes `now` on the node's own clock, never compared with another node's.

- `on_capture(iq, now)`: a level gate (98th percentile of block power, so a slot holds the longest frame any mode makes), then the known-geometry read for every measured sender, then a retry at each known scale with the band centre measured from the capture, then a gated blind read. Every frame that passes CRC goes to the relay rules, and the geometry it was read at is kept, as is every band the message was heard on.
- `next_burst(now)`: the `adapt` default plan (avoiding heard bands), then the agility provider, then `adapt.refit`, then the interference provider's review, then the budget pop. The burst is composed with the crypto provider's waveform key, at K = L.
- `on_announcement`, `send`, `attach(tunnel)`, `tick`: announcements, ageing, plausibility (re-judged on every measurement, §M-9).
- **Bounded memory.** `tick` forgets per-message state idle longer than `memory_age` (default 120 s): seen ids, held symbols, heard-on bands and the reassembler's partial messages. Queued messages are kept.

Access is reply-free and slotted on each node's own clock: after each burst, the node waits 1 to `window` (default 4) of its own slots at random. Nothing aligns one node's slots with another's.

**Transmitter identity** is inferred from symbol-id regions. The PHY's free sender mark is read on the integer screen only, and coherent bursts carry none.

**Emulated air** (`emu/air.py`). Each burst goes out at its sender's own rate and centre. It is reconstructed exactly as a band-limited signal, sampled at the air's rate at real positions (quantized to 1/Q of a sample), scaled by Friis loss, delayed by propagation time, and summed with every overlapping burst at each receiver, so collisions are real. Receiver noise is added once per capture. The receivers on top are the real PHY and codec. What the air does *not* model: Doppler, oscillator drift (rate ratios are exact as declared), ADC quantization, multipath, terrain, and front-end roll-off. Node caches start warm, in the state after first contact, except in the width-switch runs.

**Measured on the current node runtime** (`research/scenarios/node_loss_listen.txt`, real entroptics-rf IQ; 20 dBm, 1.2 Msps sub-bands, 9 km hops):

| scenario | result |
|---|---|
| single-relay 9 km chain, 48-byte telemetry | 10/10 delivered, median ~32 ms (4 slots of ~8 ms), 3–4 bursts |
| two-relay diamond, same | 10/10 delivered, median 16 ms, 3.4 bursts |
| per-burst CPU (read + decode, fast path) | ~40–280 ms per burst, tens to hundreds of times a slot (§M-11) |

A width change mid-conversation is handled by the receiver re-measuring the sender's geometry blind (§M-3.2): on the current waveform all 8 rate pairs, including a 1.2 → 2.4 Msps change, were read blind on the cabled bench (§M-3.1). The earlier per-conversation width-switch run on the node runtime (`node_runtime.txt`, 6/6) was on the previous waveform and is not re-run here.

**Speed.** That last row is the gap. On the last per-slot measurement, with a node-path receive of 322 ms per burst, the Python prototype ran about 45–300× slower than real time per slot. The node path now costs 187 ms (§M-3.2), and on the current waveform a fast-path read plus decode costs about 40–280 ms a burst (§M-3.2). The per-slot ratio has not been re-measured.

---

## M-11. Limitations and open issues

Stated plainly, most severe first.

1. **Emulated, not flown.** Every mesh-level result is on the emulated air with the real PHY. No mesh message has crossed a real radio link. The air omits Doppler, oscillator drift, quantization, multipath and terrain (§M-10). The PHY's hardware evidence is a cabled single link (Pluto+ → NESDR) at 0.8–2.4 Msps (§M-3.1). The mesh does now run live and distributed across the six-node Pi-4B fleet — separate hosts, each bridging a real OS interface, carrying real OS IP (DHCP, TCP, UDP, and the public internet through an off-the-shelf router) and multi-hop decode-and-forward with drop/rejoin self-healing — but over that same emulated air, so this closes the single-process/orchestrator gap, not the over-the-air one.
2. **Out-of-band emission — measured ≥ ~22 dB below the in-band peak on a cabled bench, under the RSS-247 §6.6 20 dB line; not a certified measurement; residual is an SDR image spur.** On the current waveform the 99% occupied bandwidth tracks the intended width (0.79 / 1.19 / 1.84 / 2.25 MHz for 0.8 / 1.23 / 1.92 / 2.4 Msps), so there is no wide splatter, and the broadband adjacent-100-kHz out-of-band emission is ≥ ~22 dB below the in-band peak at every occupancy including full occupancy (2.4 / 2.048 Msps full-occupancy: 21.7 / 22.0 dB) — under the RSS-247 DTS §6.6 ≥ 20 dB line on this bench, with no pulse shaping or guard band by design. The only sub-20 dB readings are discrete image spurs from the SDR analog chain (a DAC/LO image: 8–11 dB above the surrounding out-of-band floor, 36–52% of out-of-band power in a single 100 kHz bin, linear with TX backoff, worst on commensurate TX/RX sample-rate ratios, and absent on coprime ratios where the worst case is 24 dB with a smooth skirt) — i.e. a hardware-calibration item (IQ/LO calibration or an output filter), not a waveform property, and the coprime-ratio operating rule already avoids it. Residual: the full-occupancy **coprime** skirt is not yet measured directly (it needs a receiver wider than the NESDR's 2.4 Msps ceiling — a wide Pluto RX), and this is a 100 kHz-RBW Welch measurement, not a calibrated RBW sweep. See `entroptics-rf/research/emission.md`.
3. **Mesh-level scenarios are measured on the current waveform.** Decode-and-forward, passive ack, sub-band relaying, node loss, the relay budget, anycast, the fleet departure, per-burst success and the node runtime (§M-4, §M-5, §M-7, §M-10) were measured on the current node runtime, decoding real entroptics-rf IQ through `FleetRun` (or the calibrated capture model for volume; `research/scenarios/*_current*`, `node_loss_listen.txt`). Three supporting runs were not re-done on the current waveform and are flagged where cited: the overlapping-sub-band capture test (`subband_overlap.txt`), the sibling-band avoidance run (`sibling_bands.txt`, which claims no gain either way), and the 10 dB point of the end-to-end delivered-goodput harness (§M-4.2 — the per-mode envelope at that SNR is current in §M-4.1). The mesh remains emulated, not flown (item 1).
4. **Python prototype speed.** About 45–300× slower than real time per slot on the last per-slot measurement, taken with a slower receive path (§M-10). Blind acquisition of a narrower sender took about 40 s per burst with the dark-mode read, and the constant-modulus read is 1.4–3.3× slower at the codec level. The fast path (about 40–280 ms per burst, read plus decode, on the current waveform) is the only per-burst path.
5. **The planner table is an emulation measurement.** The thresholds of §M-4.1 come from the emulated air with the real PHY; the residual geometry losses (0–17% of losses near threshold) sit at the lowest modes. Neither has been measured over the air.
6. **Coherent QAM on hardware needs ≤ 0.5 occupancy.** Tested on the current waveform (1.2 → 2.4 Msps, occupancy 0.5, ~22 dB), coherent QAM decoded blind with no codec change: 4-QAM and 16-QAM at BER 0, 64-QAM at 0.7%, each read at the correct M. At full occupancy (matched 2.4 Msps) the analog chain's band-edge roll-off (7–12 dB) scatters into the keyed modes and pulls 16- and 64-QAM to read as 4-QAM (BER 0.49; 4-QAM itself holds at BER ~0.001), the same failure the previous waveform showed at matched rate. This is a flatness limit, not a codec fault — in simulation the same chain decodes 16-QAM at BER 0 and 64-QAM at 0.3–0.4% at full occupancy — and running dense QAM at ≤ 0.5–0.6 occupancy clears it on hardware. The planner still defaults to differential, since a narrow coherent hop costs the width that bought the SNR.
7. **Blind coherent reception of a single cold burst is weak** (measured at K = 60). At matched L=128, blind reading failed on 153/360 captures (89 with the CRC choosing M) against 8 informed. At ρ = 0.514, the cold scale read on 7–34-row bursts landed within 1e-4 on only 26/96 captures, and where it did, 24/26 decoded (`coherent.md`). The mesh avoids the cold path by caching each sender's geometry. A sender's *first* coherent burst may be lost.
8. **A fourth-order detectability leak remains** (§M-3.5); the second-order ones are closed by the row radius and the full screen. On hardware, against a transmitted-noise null through the same front end, the battery (covariance rank, lag, two row-energy contrasts, kurtosis, fold) read noise on all eight rate pairs (worst |z| ≤ 2.3; §M-3.5), so the front end's own structure (I/Q imbalance, DC, AGC) does not by itself expose the waveform. An observer that already holds the width and offset is still not excluded, and that fourth-order read has not been run on hardware. Key freshness is the crypto provider's, and under one key the reference row, mode keys and row radii repeat (§M-2.1).
9. **The coherent gain comes from the single keyed reference row,** averaged over the modes (`phy._common_gain`). A payload-averaged gain read has not been built or measured against it.
10. **Open mode.** A constant payload makes near-identical rows on the air (§M-3.5). Open 256-QAM 3/4 reads 1.5 dB worse than keyed (§M-4.1), because an open receiver levels each row itself.
11. **Small sub-band grids.** With only two sub-bands, sibling relays collide, and avoidance measured no gain (§M-5.5). Grids of 4+ slots with 3+ siblings are untested.
12. **No congestion control or airtime fairness.** Contention among many senders near the base is unmeasured.
13. **Routing is flooding.** Least-cost and position-aware routing are not built. Measured path-loss exponents from the heard map are not used yet.
14. **Sender identity** comes from symbol-id regions. The PHY sender mark works on the integer screen only, and coherent bursts carry none.
15. **Fountain efficiency at small k.** 9 dense repair symbols at k=6 fail about 10% of the time (measured 0.095–0.135). Small messages need redundancy above the overhead table's mean.
16. **Bench hardware.** The Pluto+ 2r2t data path comes up randomly aligned per connection, on RX as on TX. A self-check-and-reconnect fix is designed, not built.

---

## M-12. What is not in this paper

No crypto, hopping or interference-handling scheme is described or implemented. The Wi-Fi comparison in `research/mcs/results.md` is left out because its reference values are from memory and marked "verify". Comparisons with commercial mesh radios are left out because none were measured.

---

## M-13. Provenance: measured numbers → files

Paths are relative to `entroptics-mesh/` unless prefixed `rf:` (`entroptics-rf/`). A source given as "not in this repository" is a measurement whose output file is not shipped. "prev" marks the previous waveform.

| claim | value | source |
|---|---|---|
| codec engine on the 8 bench captures (prev) | 0.5–1.5 ppm; BER 0 at ρ 0.33–0.833, 5e-4 at 0.9375 | rf:`research/blind_width_scaled.md` § Hardware |
| current receiver on spare-mode bench captures | width 64, 0.5–1.6 ppm; payload declined; K=60 read BER 0…0.011 | rf:`research/rank_leak.md` § Bench captures from a spare-mode sender |
| 8 rate pairs blind on hardware, current waveform | 64/64/4, +0.56–1.59 ppm, closure 19–317×; BER 0 on five, 8e-4 @ 94%, 0.007–0.014 matched; ~19–20 dB | not in this repository |
| hardware detectability vs transmitted-noise null | 7 detectors, 8 pairs: worst \|z\| ≤ 2.3 (null leave-one-out spread ≤ 5.5); K=60 control rank 83, fold 18–33 | not in this repository |
| coherent QAM on hardware at ≤ 0.5 occupancy | 4/16-QAM BER 0, 64-QAM 0.7%, read at correct M; full-occupancy 16/64 read as 4 (BER 0.49, 4-QAM ~0.001); sim decodes at full occ | not in this repository |
| OOB emission vs RSS-247 §6.6, current waveform | 99% occ width 0.79–2.25 MHz tracks intent; measured on a cabled bench, not certified — broadband adjacent-100-kHz ≥~22 dB below in-band peak at all occupancies incl. full; sub-20 dB readings are discrete SDR image spurs (linear w/ TX backoff, commensurate ratios only, absent on coprime = 24 dB), a hardware-cal item | `entroptics-rf/research/emission.md` |
| relay budget ×3 → ×2 + rank_margin | loss arms 60/60, all-alive 40/40 at both budgets (k=1, 6); ×2 ~20% fewer bursts / latency; showcase ×2 210/210 63/185 ms vs ×3 81/232; single_alive k=6 9/10→80/80 with margin | `research/scenarios/node_loss_listen.txt`, `showcase_budget.txt`; `src/entroptics_mesh/relay.py` (`Policy`) |
| band centre measured (prev) | within 0.01 cycles/sample | not in this repository |
| blind width cost at non-unit ρ (prev) | 5–23 s (L 32/64), 26–71 s (128); given 0.2–1.9 s | rf:`research/blind_width_scaled.md` |
| constant-modulus read: blind width, lock | 4/4 every cell; 19–81 s vs 9–41 s (1.4–3.3×); 0 dB 640 rows 7/12 vs 3/12 | rf:`research/rank_leak.md` § Lock and cost |
| fast-path read + decode, current waveform | 23–93 + 17–56 ms (2.4 Msps); 67–218 + 16–59 ms (1.0 → 2.4) | not in this repository |
| scan rows | 27 vs 29 of 32 on 8 vs 16 rows; ~100 ms read | rf:`research/rank_leak.md`; `phy.py` (`SCAN_ROWS_MAX`) |
| reference-row-only coherent re-read | 29 → 13 of 32 with 2 rows; D16PSK 4/16 wrong branch on ref row alone | rf:`research/rank_leak.md` § The mesh on it |
| node-path receive (prev) | 187 ms read + decode, 30/30 + 30/30 | `research/scenarios/settle_tolerance.txt` |
| receive-speed work (prev) | 322 → 187 ms; keyed 1.0→2.4 ~230 → ~120; open ~340 → ~115 ms | not in this repository |
| settle tolerance (prev) | 30/30 at every ftol; 362 / 234 / 168 ms at 1e-9 / 1e-2 / 1 | `research/scenarios/settle_tolerance.txt` |
| width-switch blind recovery (prev) | 2.18–4.31 s; 6/6 | `research/scenarios/node_runtime.txt` |
| blind narrow-sender read (prev) | ~40 s / 41 s | `src/entroptics_mesh/node.py` docstring |
| 0 wrong-mode decodes | 466,506 keyed and open frames (second table); 2729 delivered frames in the goodput run | `research/mcs/kL.md`, `results_kL_fp.json`; `research/mcs/goodput_kL.md` |
| 1–3 surviving rate hypotheses | – | not in this repository |
| capture tail | 0/80 lost at 50 dB, capture ending 0.5–0.7 row after the burst (1.0 → 2.4 and matched) | `research/mcs/kL.md` § Capture tail |
| header row cost | 0.7–7.1% of goodput | `src/entroptics_mesh/link.py` (`read_frame`) |
| rf blind battery (K = 60 screen) | worst \|z\| 2.7 / 96 traces; MME bias +2.5…2.7 | rf:`research/PAPER.md` §RF-5.2 |
| cyclic extension detectability | z 8.4 @ −5 dB, 15.7 @ 0 dB; taper z 55 @ 0 dB | rf:`research/extension.md` |
| joint read floor and cost | −22 → −66…−69 dB; 9–23× row read | rf:`research/joint_read.md` |
| whitened public aperture (prev) | keyless 31 → 7.5 s; fast path 0/5 → 64/64 | not in this repository |
| row-energy leak and Gamma(L) fix | z 38; ~0.1 dB; +0.14–0.21 dB PAPR | rf:`research/row_energy_leak.md` |
| rank-deficiency leak (K < L) and cure (e) | z 19–27 @ −15 dB (10 s, L=64, any K < L); codec path at K = L: noise at every cell; 0.28 dB/symbol, +6.7% | rf:`research/rank_leak.md` (§1, The shipped codec); rf:`research/PAPER.md` §RF-5.3 |
| fourth-order structure | L=64: z 1.3 @ 0 dB, 558 @ +10 dB (417 ms); L=32 from 0 dB | rf:`research/rank_leak.md` §3; rf:`research/PAPER.md` §RF-5.3 |
| `_row_levels` 4-QAM spread floor | 3–10% of open frames dropped without it; sd floored at 1e-6 | `research/mcs/kL.md`; rf:`src/entroptics_rf/waveform.py` (`_row_levels`) |
| non-decision-directed row turn | 256-QAM 3/4, 26–30 dB: 18/27/29/32/32 of 32 matched, 12/23/31/32/32 at 1.0→2.4 | not in this repository |
| burst success vs SNR | 99% @ 9 dB; 100% ≥ 10 dB; geometry 100% at every SNR (4 dB up) | `research/scenarios/burst_success_current.txt` |
| fountain overhead, dense small k | 1.09 / 1.02 / 1.01, worst 1.44 / 1.16 / 1.03; LT 1.17, worst 2.19 at k=16 | `src/entroptics_mesh/link.py` docstring; LT figures not in this repository |
| planner MCS table at K = L | §M-4.1 table; 24 modes, 1397 cells, 466,506 frames | `research/mcs/kL.md` (second table), `results_kL_fp.json`, `kl_cells_fp.jsonl`; `src/entroptics_mesh/adapt.py` (`MODES`) |
| true-geometry MCS (K = 60), reference only | 3.2 … 28.2 dB; ρ 0.514 ±0.5 dB | `research/mcs/results.md`, `results_gray.json`, `coherent.md`, `results_coherent.json` |
| open-mode spot check | DBPSK 1/2, DQPSK 3/4, 16-QAM 1/2, 64-QAM 2/3 within 0.2 dB of keyed; 256-QAM 3/4 1.5 dB worse | `research/mcs/kL.md` (second table) § Open mode |
| blind coherent spot-check (K = 60) | 153/360 (89 CRC-M) vs 8; ρ: 26/96 scale, 24/26 decode | `research/mcs/coherent.md` § Blind spot-check |
| delivered goodput, planner picks | §M-4.2 table | `research/mcs/goodput_kL.md`, `results_phy_link_kL_{10,12,24}.json` |
| decode-and-forward vs plain relaying | DF 8/8 at ×1.5 in 18 bursts; plain 8/8 only at ×3, 31 bursts | `research/scenarios/relay_df_current.txt` (real IQ) |
| sub-band vs same-channel relaying | 9 km: same 8/8, split 8/8 (split reads 2 frames/capture, 1–3 of 8) | `research/scenarios/subband_relay_9km_current.txt` |
| overlapping sub-bands (prev, not re-measured) | disjoint 6/6, half-overlap 0/6, same 0/6 | `research/scenarios/subband_overlap.txt` |
| passive ack | origin ×1.5 27→24.7, ×3 36→24.7 bursts; 10/10 | `research/scenarios/passive_ack_current.txt` (real IQ) |
| time budget (prev) | slot 26 = 122 ms, 16 bursts; 40/60 ms → 5/6 bursts | `research/scenarios/budget_chain.txt` |
| node loss, all arms | §M-5.5 table; loss arms 60/60, all-alive 40/40 at ×2 and ×3 | `research/scenarios/node_loss_listen.txt` (real IQ) |
| sibling bands (prev, not re-measured; no gain claimed) | 10/10 both; median slot 16.5 vs 16; 29.1 vs 28.0 bursts | `research/scenarios/sibling_bands.txt` |
| flooding line, 5 drones (prev) | 6/6, one transmission per node | not in this repository |
| fleet departure | 18–19/20 to 25.5–27 km (node runtime, real IQ, 3 seeds) | `research/scenarios/fleet_departure_current_real_{1,2,3}.jsonl` |
| two-ray breakpoint | ~38 km | not in this repository |
| anycast HOME, gateway lost | 10/10 via G2 vs unicast 0/10; 24.7 vs 42.6 bursts | `research/scenarios/multihome_current.txt` (real IQ) |
| tunnel efficiency, reliable class, state | §M-8 tables | `research/encap/results.txt`, `research/encap.md` |
| placement hops and relay counts | 6.5/10.1/20.3 km; 52/81/161 km; 70/46/22 | model, `src/entroptics_mesh/placement.py` (run output not in this repository) |
| plausibility tolerance, max speed | 3 dB; 150 m/s | `src/entroptics_mesh/plausibility.py`, `node.py` |
| node runtime chain, latency/CPU | single-relay 10/10 ~32 ms; fast path 40–280 ms/burst | `research/scenarios/node_loss_listen.txt`; §M-11 |
| coherent on hardware, matched (prev) | BER 0.08–0.11 (4-QAM); 16/64 read as 4 | not in this repository |
