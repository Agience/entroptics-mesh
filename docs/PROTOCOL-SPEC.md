# entroptics multi-mesh — protocol specification (outline)

The protocols of the entroptics multi-hop mesh network and, in particular, the communication **between
the mesh network layer (`entroptics_mesh`) and the RF/physical layer (`entroptics_rf`)** — the boundary
where mesh frames become a keyed waveform and captured samples become decoded frames.

> **Audience:** engineers integrating, reviewing, or re-implementing the stack. This is a specification
> *outline*: it fixes the contracts, wire formats, and invariants, and points to the implementation
> (`src/entroptics_mesh/*.py`) and the deeper PHY treatment ([`entroptics-rf research/PAPER.md`](https://github.com/Agience/entroptics-rf/blob/main/research/PAPER.md)) for
> derivations. It is the normative companion to `ARCHITECTURE.md` (narrative) and `RUNBOOK.md`
> (operations).

**Normative language.** MUST / MUST NOT / SHOULD / MAY per RFC 2119.
**Status.** Draft. Byte formats
are current as implemented; items marked *(roadmap)* are specified but not yet built.

---

## 1. Reference model

### 1.1 Layering and the one-way dependency

```
  application / encapsulation   IP tunnel, TCP-at-edge, BGP-between-gateways   (tunnel.py, tcpproxy.py)
  ────────────────────────────────────────────────────────────────────────────────────────────────
  mesh network                  addressing, flooding/relay, fountain reliability (routing.py, link.py,
                                 announcement, neighbour table, planner            node.py, neighbors.py,
                                                                                    announce.py, adapt.py)
  ── mesh ↔ RF interface ──────  TxPlan / burst / RxBurst                          (phy.py)   ◄── §2
  physical (entroptics-rf)      keyed entropy-matched screen waveform              (entroptics_rf.waveform)
  core (entroptics)             aperture / entropy primitives                      (entroptics)
```

Dependency is strictly one-way: `entroptics` ← `entroptics-rf` ← `entroptics-mesh`. The mesh MUST NOT
reach around `phy.py` into the codec; `phy.py` is the mesh's only contact with `entroptics_rf`.

### 1.2 The four refusals (global invariants)

These hold at every layer and are the conformance backbone:

1. **Nothing pre-shared.** No synced clocks, schedules, channel plans, pilots, preambles, guard bands,
   or cyclic prefix. A receiver MUST measure every parameter of every burst (rate ratio, start, carrier,
   centre, width, alphabet, family, code rate) blind, confirmed by a frame CRC. The only shared secret
   permitted is a key, supplied by a provider (§6).
2. **Nothing required back.** No ack, handshake, echo, or decode-quality report may be required for
   delivery. A listen-only node MUST decode as well as any other. Reliability is forward-only (§4.3).
3. **No added structure.** No repeated or periodic on-wire structure that a blind classifier could key on
   (e.g. a relay MUST NOT reuse the sub-band it just heard).
4. **Pluggable security, null-provider core.** Encryption, frequency agility, and interference handling are
   provider interfaces (§6) with null defaults under which the mesh is a fully functional open broadcast
   network. The core ships no cryptographic function of its own.

### 1.3 Entroptics-pure constraint (PHY)

The codec path MUST NOT contain pilots, preambles, guard bands, equalizers, channel-rate resampling, or
FFT-based demodulation. Every mode is lit: **K = L** (a spare/dark mode is a noise-only direction a
key-free reader could read the width from, and is refused). Reads are band-limited projections onto
the keyed aperture.

---

## 2. The mesh ↔ RF interface (`phy.py`) — normative

This is the single boundary the rest of the spec rests on. One **burst** = one `compose` call: it
carries its own keyed reference row and is read on its own. Nothing in a burst names its family,
alphabet, or code rate, and nothing about them is agreed in advance.

### 2.1 Transmit: `TxPlan` → burst IQ

`transmit_burst(bits, plan, key, sender=None, *, rng=None) -> complex64[]`

- `bits` are already channel-coded (§4.2) at the plan's code rate.
- Output is unit-RMS `complex64` at the **sender's own sample rate**, reference row first.
- `plan` is a `TxPlan` (§6.1). `family` selects the generator:
  - `differential` → `waveform.compose` (M-PSK, M ∈ {2,4,8,16}, Gray-labelled via `fec.gray_to_phy`).
    MAY carry a sender mark (`sender=`, a per-mode rotation the differential read cancels).
  - `coherent` → `waveform.compose_qam` (square M-QAM, M ∈ {4,16,64,256}); one keyed reference row
    measures every mode's gain; carries no sender mark.
- `key` is the PHY key from the crypto provider (`waveform_key`, §6.2); `None` is the public zero state.
- **Rate and centre are the radio's business**, not the frame's: the burst is written at the sender's
  rate; the receiver measures the ratio of that rate to its own.

### 2.2 Receive: capture → `RxBurst` (both reads)

A receiver reads every burst **both ways** at the geometry it measured and hands both up in an
`RxBurst`; the frame layer (§4.4) decodes all hypotheses and the CRC chooses. The closures only *order*
hypotheses; none decides.

- `receive_burst(iq, key, *, candidates=None) -> RxBurst` — fully blind (`waveform.discover` integer
  read, or `_discover_scaled` for a narrower sender). MAY lock poorly on a single short coherent burst;
  a sender heard once SHOULD be read at its known geometry instead.
- `receive_known(iq, key, known, *, carrier_span=0.2, accept=None, tracked=None) -> RxBurst` — the fast
  path for a sender whose geometry was measured before. `accept` is the frame check (CRC) that stops the
  read at the first passing hypothesis. `tracked=(start_phase, carrier[, window])` restricts the scan to
  a local search around a prior lock; a correct prior is a pure speed-up
  (byte-identical subset of the full scan), a stale one costs only a CRC-gated re-scan — never a wrong
  lock.

`RxBurst` carries: `discovery` (the geometry/`Discovery`, §3.2), `soft` (de-keyed differential symbols),
`qam` + `coherent` (the coherent read and its `Discovery`), `keyed`, and per-row `power` (for SNR). Key
methods: `hypotheses()` → ordered `(family, M)` pairs; `rows(family)` → measured burst length;
`soft_bits(family, M, n_bits)` → soft LLRs for a hypothesis; `burst_snr(...)` → measured in-band SNR.

### 2.3 Measured state: `Known`

`Known(L, scale, center)` is geometry a receiver measured before (width, clock-rate ratio, band centre
in cycles/received-sample). Clock ratios and chosen centres persist, so a sender heard once is read next
time at its known scale. This is **measured state, never pre-shared** — consistent with invariant 1.

### 2.4 Why no self-describing header

The burst does not signal family/alphabet/rate. The receiver tries the hypotheses `RxBurst.hypotheses()`
yields and the **frame CRC-32 decides** (§4.4). Rationale (measured): a header costs airtime on every
burst and is itself a repeated fixed structure; the CRC costs only CPU (1–3 hypotheses once the burst length
is read), and a wrong hypothesis passes CRC-32 with probability ≈ 2⁻³².

---

## 3. Physical layer (`entroptics_rf`) — interface summary

Full treatment in [`entroptics-rf research/PAPER.md`](https://github.com/Agience/entroptics-rf/blob/main/research/PAPER.md); here, only what the mesh depends on.

### 3.1 Waveform

A **keyed entropy-matched screen**: L keyed modes (K = L, every mode lit), per-row keyed radius
(`row_radius`), keyed aperture and per-row mode rotations, **no cyclic extension**. The emission reads
as transmitted Gaussian noise to the blind structure detectors (the keyed per-row amplitude law). Its
energy is visible to a radiometer; its structure is not classifiable and its content is unreadable
without the key. This is what makes cover traffic (§6.3) free. Public API the mesh uses:
`compose`, `compose_qam`, `discover`, `decode`, `decode_scaled`, `read_soft`, `read_qam`,
`closure_significance`, `Discovery`.

### 3.2 `Discovery` — what the receiver measured

Coordinates: `L` (scale = samples/row), `offset`+`phase` (sample-axis translation, the arrival-time
observable), `carrier` (mode-axis translation, cycles/row), `scale` (Tx-samples per Rx-sample = the
blind clock-rate ratio), `row0` (absolute keystream row), plus `K`, `M`, `score`, `margin`, `closure`,
`reference`/`lattice` (coherent). All are **measured blind**; none is supplied.

### 3.3 Blind acquisition notes (normative for integrators)

- A cyclic capture starts at a random row; a blind read's row search (`join_rows`) MUST cover the TX
  buffer's row count (bench buffer ≈ 9000 → use `join_rows ≥ 9001`). The default (4096) silently
  declines a lock whose `row0` is larger.
- Across mismatched clocks use `decode_scaled`/`_discover_scaled` (continuous scale), not the matched
  `decode`. For scale > 1 (receiver slower than sender) the discovery clamps its bracket at unity; this
  does not limit reciprocal ranging (§7), where one node always sees scale ≤ 1.

---

## 4. Framing and reliability (`link.py`, `fec.py`, `routing.py`)

### 4.1 Symbol frame header

Carried inside every FEC-protected frame, big-endian, `struct ">8sI8sBHHHII"`
(`link._SYM`, `HEADER_BYTES` = its size):

| field | type | meaning |
|---|---|---|
| `origin` | 8s | originating node id |
| `seq` | u32 | origin's message sequence number |
| `to` | 8s | destination node id, or `BROADCAST` (0xFF×8), or `HOME` anycast |
| `hops` | u8 | remaining hop budget (TTL) |
| `budget_ms` | u16 | remaining time budget, ms (0 = none) |
| `k` | u16 | fountain block count of the message |
| `block` | u16 | fountain block size (bytes) |
| `length` | u32 | message length (bytes) |
| `symbol_id` | u32 | this fountain symbol's id |

`FRAME_BYTES` = 512 (protected size of one symbol + header + CRC); `BLOCK` = 512 − 2 − `HEADER_BYTES`.
`msg_id` ≡ `(origin, seq)`.

### 4.2 Packet code (within a frame)

Payload + **CRC-32** (IEEE, reflected, init/final 0xFFFFFFFF; `fec.crc32` via zlib, bit-identical to the
reference table loop), then a **rate-1/2, constraint-length-7 convolutional code**, generators **171/133
octal**, zero-tail terminated. Puncturable to **2/3** and **3/4** (`fec.PUNCTURE_PATTERNS`). Decoded by
Viterbi (`viterbi_decode`, batched `viterbi_decode_batch`); a frame failing its CRC after decode is
dropped (`None`). The frame does **not** name its rate — the receiver tries each and the CRC chooses.

### 4.3 Fountain code (across frames)

A message is cut into `k` blocks and sent as **LT fountain symbols** (`fec.fountain_encode`; systematic +
dense repair for k ≤ 64, LT above). A receiver rebuilds from **any** set of symbols that spans the
blocks, in any order, from any mix of paths (`FountainDecoder`, `link.Reassembler`). So a collision, a
fade, or a lost relay costs symbols, not the message, and no receiver asks for anything. The sender emits
`ceil(k × redundancy)` symbols — chosen up front from what it knows of its audience; an agility provider
MAY spend more airtime on an important message. Measured overhead ≈ 1.01–1.09 symbols/block (k ≥ 64).

### 4.4 Hypothesis decode (`link.read_frame`)

Given an `RxBurst`, try each `(family, M, rate)` hypothesis in the order the reads favour; the first
whose **CRC and symbol header pass** wins. The measured burst length (`RxBurst.rows`) prunes hypotheses
whose frame would over- or under-fill the burst. Result cached on the burst (decoded once).

---

## 5. Addressing and routing (`routing.py`)

### 5.1 Identifiers

- **Node id:** 8 bytes.
- **`BROADCAST`** = `0xFF`×8.
- **`HOME`** = `b"HOME\x00\x00\x00\x00"` — **anycast**: every gateway accepts it, so "home" is whichever
  gateway hears the message (multi-homing without a chosen route).
- **`msg_id`** = `(origin, seq)`.

### 5.2 Flood routing with budgets (`FloodRouter`)

`heard(header, now) -> (deliver, relay)`:

- **Deliver** locally if `to` is this node, `BROADCAST`, or an accepted anycast (`HOME` at a gateway).
- **Relay** once if this node is not the sole destination and `hops > 1` (decrement `hops`), subject to
  the relay budget and dedup.
- **Dedup:** a `msg_id` already in the seen-cache is neither delivered again nor relayed. The seen-cache
  is bounded by **age** (`seen_age`, default 60 s), not by count — O(message-rate × window), independent
  of fleet size. `seen_age` MUST exceed the longest cross-mesh delivery time, or a late copy is relayed
  again.
- **Time budget:** `budget_ms` needs **no shared clock** — each node subtracts only the time it *held*
  the message, measured on its own clock (`spend`), and drops what can no longer arrive in time.

### 5.3 Passive acknowledgement

An originator that **overhears** a downstream relay forwarding fresh symbols of its message learns the
hop worked — a free passive ack costing no airtime. This is an optimization only; nothing is ever
required back (invariant 2).

### 5.4 Not yet specified *(roadmap)*

Which relays forward (position-/energy-aware least-cost relay selection), and BGP policy between
gateways, are deliberately left open; today relaying is plain budgeted flooding with dedup.

---

## 6. Providers — the plug points (`providers.py`)

A provider MUST NOT require anything back from another node (invariant 2). Each has a null default under
which the mesh is a working open broadcast network (invariant 4).

### 6.1 Plan objects

- **`TxPlan`**(`rate, center=0, power_db=-20, K=None, L=64, M=4, family="differential", code_rate="1/2"`)
  — how one transmission goes out; every field the sender's choice, every one receiver-measured. `K`
  MUST equal `L` (defaults to L; any other value is refused).
- **`Intent`**(`to, n_bytes, priority=0`) — what the node wants to send, before a plan is chosen;
  providers see this, never the payload.
- **`PlanContext`**(`me, velocity, t, node_id, acceleration, trajectory, history`) — the sender's own
  state for seeding a keyed schedule; the receiver's last-known state is read from `table[intent.to]`.
  All flight-state fields optional.

### 6.2 `CryptoProvider`

`seal(plaintext, *, context) -> bytes`; `open(sealed, *, context) -> bytes | None`;
`waveform_key(context)` → the PHY key (the seed the aperture is drawn from), or `None` for the public
zero state (key freshness is the provider's to manage); `address(identity, *, context)` /
`resolve(token, *, context)` → **optional pseudonymous addressing** (keyed-pseudorandom header tokens so
a relay or eavesdropper cannot link who talks to whom; default returns the identity unchanged → byte-identical
to raw addressing). Null = `NullCrypto` (no sealing, no key, raw addressing).

### 6.3 `AgilityProvider`

`plan(intent, default, table, ctx) -> TxPlan` — chooses width/centre/power/geometry (clues from the
neighbour `table`, never constraints). Optional *(roadmap)*: `transmit_offset(ctx, *, window_s)` /
`predict_offset(who, t, *, window_s)` — a **keyed transmit schedule** so a receiver running the same
provider predicts when a burst goes out (feeds the receive-path local scan, §2.2 `tracked=`);
`cover_traffic(ctx, *, sending) -> [TxPlan]` — **decoy bursts** of keyed-random bytes (a receiver's CRC
drops them) for metadata privacy (traffic-analysis resistance over timing and volume); cheap because a
decoy and a payload burst are not separable by structure on the air. Null = `NullAgility` (core default plan, no schedule, no cover).

### 6.4 `InterferenceProvider`

`observe(BandObservation)` — fed every band observation (`t, occupancy, center, interference`);
`review(plan) -> TxPlan | None` — a plan to transmit instead, or `None` to hold;
`interference_bands() -> list` — OPTIONAL; the bands this node's RECEIVER is measuring interference on
(`neighbors.InterferenceBand`: offset+width relative to band centre, with optional interferer bearing/level),
put in its state announcement (§8) as a physical measurement, not feedback. Null = `NullInterference`
(observes nothing, changes nothing, reports no interference bands).

### 6.5 Composition

`Providers(crypto, agility, interference)` — defaults all null. A node built with all-null providers is the
open broadcast mesh.

---

## 7. Relative navigation and ranging *(candidate / measured in sim + bench)*

Built on the same blind reads; a provider/application concern, outside the reply-free core.

- **Bearing (AOA):** the keyed multi-antenna combine already searches the steering angle; its argmax is
  the neighbour's bearing (< 1° in sim).
- **Range-rate (Doppler):** the carrier the receiver already recovers is the radial velocity
  (hardware-measured ~1–3 m/s per burst).
- **Unsynchronized two-way ranging:** the clock *offset* cancels in the reciprocal exchange; the residual
  clock-*rate* error is removed because the receiver recovers that rate **blind** (the `scale`), so range
  is limited by arrival-time precision, not clock discipline (sim: rate to 0.2–0.8 ppm → cm–dm ranging;
  bench ToA floor ≈ 0.09–0.10 m one-way @2.4 MHz).

See entroptics-rf [`research/nav/`](https://github.com/Agience/entroptics-rf/tree/main/research/nav): `relative_range.md`, `doppler_hw.md`, `unsync_ranging.md`, `toa_floor.md`.

---

## 8. Node state & capability announcement (`announce.py`)

An **optional** sealed message a node MAY emit; nothing depends on it arriving (a node that hears none
uses defaults; one that hears some treats them as **clues, never constraints**). Travels to address
`ANNOUNCE`, **one hop, never relayed**, sealed by the crypto provider (topology is sensitive). It carries
only physical measurements / capability figures — **never a decode-quality figure** (that would be
feedback).

Wire format (big-endian, before sealing):

```
u8  version (=1)
u8  flags        bit0 position, bit1 velocity, bit2 band, bit3 tx-power, bit4 interference
8s  node id
[position]  f64 lat, f64 lon, f32 alt         (if bit0)
[velocity]  f32 east, f32 north, f32 up        (if bit1)
[band]      f64 min_rate, f64 max_rate, f64 center_hz   (if bit2)
[tx power]  f32 dBm                            (if bit3)
[interference] u8 n, then n × (f64 offset_hz, f64 width_hz, f32 bearing_deg, f32 level_db)  (if bit4)
            offset is RELATIVE to this node's band centre; bearing/level NaN when not measured
u8  n_heard, then n × (8s node id, i8 SNR dB clipped ±127)
```

`tx_power` lets a receiver turn measured RSS into a coarse range; `heard`+SNR builds a physical link map
(who reaches whom at what path loss) for power choice and later least-cost routing. Precise two-way
ranging (a *reply* to an announcement) is a provider/application concern (§7) — the core stays reply-free.

---

## 9. Encapsulation (`tunnel.py`, `tcpproxy.py`) *(partially roadmap)*

- **IP tunnel over the mesh:** datagrams carried inside fountain symbols; flow/packet ids are
  unpredictable by design (`random.Random(node_id ^ getrandbits(32))` when no rng is supplied — no predictable identifiers).
- **TCP terminated at the edges:** a proxy per gateway (`tcpproxy.py`) so TCP's acks/retransmits never
  ride the one-way mesh; datagrams + fountain carry the payload inside.
- **BGP between gateways** *(roadmap)*: inter-gateway routing policy; not yet specified.

A Tunnel is never constructed by the node runtime's delivery path; a repeatable Tunnel-level simulation
MUST pass an explicit `rng=`.

---

## 10. Multiplexing (`emu/subband_run.py`, `harness/multiplex_pack.py`)

Parallel streams ride **disjoint sub-bands** of the capture band; a single wideband capture is decoded
into N streams (measured: 2/3/4 concurrent FPV streams all deliver from one capture; linear in the
number of sub-bands). Ceiling = total band ÷ per-stream width, then the coordinator's decode compute.
Co-channel key-stacking ("MIMO") was a measured dead end; spatial MIMO is deferred.

---

## 11. Conformance checklist

An implementation conforms if:

1. A receiver measures **every** burst parameter blind (no pre-shared rate/centre/alphabet/rate/clock);
   only a provider-supplied key may be shared. *(inv. 1)*
2. Delivery never depends on anything sent back; a listen-only node decodes as well as any. *(inv. 2)*
3. No repeated/periodic on-wire structure; a relay does not reuse the heard sub-band. *(inv. 3)*
4. All crypto/agility/interference behaviour is behind the provider interfaces; all-null yields a working
   open mesh. *(inv. 4)*
5. PHY codec contains no pilot/preamble/guard/CP/equalizer/resampling/FFT; **K = L**. *(§1.3)*
6. The frame carries no family/alphabet/rate signalling; the **CRC decides** the hypothesis. *(§2.4, §4.4)*
7. Dedup is bounded by **age**, not fleet size; the time budget uses only locally-measured held time
   (no shared clock). *(§5.2)*
8. The mesh↔RF boundary is `phy.py` only; the mesh does not call the codec directly. *(§1.1)*

---

## 12. Reference map

| concern | module |
|---|---|
| mesh ↔ RF boundary | `phy.py` |
| PHY codec | `entroptics_rf.waveform` |
| framing / fountain | `link.py` |
| packet code (conv/CRC/puncture/fountain) | `fec.py` |
| addressing / flood routing | `routing.py` |
| node runtime (slots, send/receive) | `node.py`, `emu/node_run.py` |
| neighbour table / positions | `neighbors.py` |
| announcement wire format | `announce.py` |
| planner (audience-fit default plan) | `adapt.py` |
| relay role | `relay.py` |
| providers | `providers.py` (+ `schedules.py`, `predict.py` examples) |
| encapsulation | `tunnel.py`, `tcpproxy.py` |
| multiplexing | `emu/subband_run.py`, `harness/multiplex_pack.py` |
| emulated air (test/sim) | `emu/air.py`, `emu/net.py`, `emu/link_model.py` |
| provider SDK guide | `research/PROVIDERS.md` |
| architecture narrative | `ARCHITECTURE.md` |
| operations | `RUNBOOK.md` |
