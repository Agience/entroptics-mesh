# Energy-per-bit model: entroptics-mesh vs incumbent low-power / mesh radios

First-cut, order-of-magnitude model of energy per bit against battery, and of
the acquisition-time budget per sender. A measurement/modelling pass — **no codec changes**. Reproduce
with `python research/power/energy_model.py` (pure Python, no deps). Every
assumption is a named constant in that script.

**Provisional flag.** Every entroptics **RX-compute** number here (per-burst
decode, blind acquisition, coordinated-mode decode) is **PROVISIONAL**. It is
built on the real-time port's measured x86 decode times and an x86→Pi-5
*extrapolation* (`research/realtime/PLAN.md`, `port_results.md`), times an
assumed Pi-5 per-core power. It refreshes the moment the port lands and a
Raspberry Pi 5 is power-measured (PLAN Phase 0). The **TX-endpoint** and
**incumbent** numbers are datasheet/typical/literature values, labelled below;
this is a spreadsheet model, not a lab measurement.

---

## 1. The thesis, quantified

> entroptics does **not** uniformly lower power. It **relocates** energy from
> the air / control plane to the **receiver's compute**.

The model puts numbers on both sides of that sentence, for the canonical
DQPSK-1/2 burst (4224 TX samples, 512 B protected frame = 475 B fountain block
= 3800 net payload bits; `research/mcs/kL.md`, `research/realtime/PLAN.md`):

| term | value | why |
|---|---|---|
| **SAVES — TX endpoint, per burst** | **0.84 mJ** (0.53 mJ PA + 0.31 mJ baseband) | every TX sample is payload: no CP/pilots/preamble/guard |
| **SAVES — control plane, per burst** | **0 mJ** | reply-free: no ACK / handshake / beacon / sync / retransmit / join |
| **COSTS — RX decode, per burst (Pi-5, blind)** | **270 mJ** *(provisional)* | blind per-burst DSP (`receive_known` fast path) |
| **COSTS — first contact, per sender (blind)** | **15–450 J** *(provisional)* | blind acquisition 5–150 s once per sender |

The asymmetry is the whole story: the **endpoint radiates ~0.84 mJ** and then
sleeps; the **receiver spends ~270 mJ of compute** to decode it — a **~320×**
relocation from the battery transmitter to the (ideally mains) decoder. Per
bit: endpoint TX **0.22 µJ/bit**, receiver decode **71 µJ/bit** (provisional).

The model adds a second entroptics regime to show the relocation is *tunable*:

> **Coordinated mode (keyed-schedule agility provider).** A pluggable agility
> provider derives a **keyed pseudo-random** schedule from (shared key, coarse
> time, optional location) that predicts the sender's
> `TxPlan(rate, center, power, K, L, M)`. Fed to the receiver it (a) collapses
> first-contact acquisition from 5–150 s to one verify (the docs' "width-given"
> 0.2–1.9 s fast path) and (b) collapses the per-burst `_scaled_scan`
> (35–51 % of a burst; PLAN §3a) to a small **local refine** around the
> predicted carrier/start. The rest of the known-geometry read
> (coords/joint/entropy/Viterbi) is unchanged, so per-burst decode drops
> ~1.6× — **not** to a hard-demod floor. This lives entirely in the
> **provider**; the core stays parameter-free.

Coordinated-mode constraints (all enforced in the provider):

- It reintroduces a **loose shared clock** and a **pre-shared keyed schedule** —
  the only pre-sharing in an otherwise parameter-free system.
- The prior must **accelerate, never be required**: the receiver still verifies
  within the narrow window, so a stale / spoofed / absent hint **degrades to
  blind**, it never breaks decode.
- The schedule must be **keyed-pseudorandom** so it adds no structure: a predictable
  schedule is a repeated pattern on the air. Keyed ⇒ cheap lock for authorized nodes *and*
  still noise-like for everyone else.

---

## 2. Two-profile table

### Profile 1 — sparse IoT uplink (endpoint on battery, gateway on mains)

475 B carried per burst; cadence swept 30–300 s; 3000 mAh Li cell
(11.1 Wh × 0.85 usable ≈ 34 kJ). Incumbent per-message energies are the whole
endpoint cost (TX + RX windows + control); entroptics endpoint is TX-only.

**Endpoint energy/day and projected battery life, 1 message / 60 s:**

| endpoint | energy/day | battery life | notes |
|---|---|---|---|
| **entroptics TX work only** | **1.21 J/day** | (TX-unlimited) | 0.84 mJ/burst × 1440 |
| entroptics + 10 µA sleep (MCU-class) | 4.06 J/day | **22.9 yr** | sleep-floor-limited |
| entroptics + 100 µA sleep (SDR-class) | 29.7 J/day | **3.1 yr** | **BOM caveat — the TX hardware floor, not airtime, sets life** |
| LoRaWAN Class A SF7 | 14.4 J/day | 6.5 yr | TTN-measured 10 mJ/msg |
| LoRaWAN Class A SF9 | 64.8 J/day | 1.4 yr | interpolated 45 mJ/msg |
| LoRaWAN Class A SF12 | 285 J/day | 0.3 yr | TTN-measured 198 mJ/msg |

The entroptics endpoint's **radiated/active** work (1.21 J/day) is **12–235×
below** LoRaWAN's across SF7–SF12. Its realized battery life, however, is set
by the **sleep floor of its own transmit hardware** (see §5 caveat), not by
airtime — that is the real bound on the "coin-cell" claim.

**Energy-per-bit, endpoint / air side (who pays what):**

| system | payload | e/msg | **e/bit** |
|---|---|---|---|
| **entroptics endpoint TX** | 475 B | 0.84 mJ | **0.22 µJ/bit** |
| BLE 4.x notification | 20 B | 0.02 mJ | 0.13 µJ/bit |
| Zigbee 802.15.4 | 20 B | 0.15 mJ | 0.94 µJ/bit |
| LoRaWAN SF7 | 19 B | 10 mJ | 66 µJ/bit |
| LoRaWAN SF9 | 19 B | 45 mJ | 296 µJ/bit |
| LoRaWAN SF12 | 19 B | 198 mJ | 1303 µJ/bit |
| LTE-M (good cov.) | 50 B | 1.0 J | 2500 µJ/bit |
| NB-IoT (good cov.) | 50 B | 2.0 J | 5000 µJ/bit |
| **entroptics gateway RX-compute — blind** | — | 270 mJ/burst | **71 µJ/bit** *(prov., on mains)* |
| **entroptics gateway RX-compute — coordinated** | — | 171 mJ/burst | **45 µJ/bit** *(prov., on mains)* |

Reading: on the **battery/air side** the entroptics endpoint (0.22 µJ/bit)
beats every LPWAN/cellular uplink by 300–20000× and is within a small factor of
BLE/Zigbee — while carrying 20–25× the payload per transaction and adding an emission with no classifiable structure +
parameter-free operation they do not offer. The **whole decode cost (45–71
µJ/bit)** sits on the gateway, which is on mains.

### Profile 2 — continuous-listening relay / always-on RX (duty cycle ≈ 1)

A node that must decode continuously. entroptics relay power is Pi-5 compute +
SDR RX front end (provisional):

| comparator (always-on RX) | power | entroptics verdict |
|---|---|---|
| BLE / Zigbee fixed-function RX | ~0.03–0.06 W | **LOSES ~80–100×** (blind DSP vs hard demod) |
| LoRa concentrator (SX130x) | ~0.4–0.8 W | **LOSES ~7–8×** |
| **entroptics relay — coordinated** | **~3.9 W** | *(provisional)* |
| **entroptics relay — blind** | **~5.0 W** | *(provisional)* |
| Rajant BreadCrumb ME4 | 5.5–19 W | **WINS** (compute < RF front end) |
| Doodle Mesh Rider | 10.6–14 W | **WINS** |
| Silvus SC4400/4200 | 8–100 W | **WINS** |

The relay verdict **flips on the comparator class**: entroptics' Pi-class blind
decode (~5 W) is far more expensive than a fixed-function LPWAN demod, but is
**cheaper than any datasheet MANET radio's RF front end** (vendor datasheets:
5–100 W). So a continuous entroptics relay is a loss against cheap IoT silicon
and a *win* against MANET mesh hardware — and coordinated mode (~3.9 W)
widens the MANET win and narrows the LPWAN loss.

---

## 3. The crossover duty cycle

Model a battery node that sends `M` msgs/day and must also **decode** a
fraction `d` of the time: `E_day = M·e_tx + d·P_decode·86400`. Solve for the
`d*` where it equals the incumbent **endpoint** budget. `b/msg` is the
equivalent bursts a node may decode per message it sends at breakeven
(Pi-5, provisional; `e_tx` = 0.84 mJ, `P_decode` = 3 W).

| incumbent (@ 1 msg/60 s) | its endpoint J/day | **blind d\*** | blind b/msg | **coord d\*** | coord b/msg |
|---|---|---|---|---|---|
| LoRaWAN SF7 | 14.4 | 0.0051 % | 0.034 | 0.0080 % | 0.053 |
| LoRaWAN SF9 | 64.8 | 0.0245 % | 0.164 | 0.0387 % | 0.258 |
| LoRaWAN SF12 | 285 | 0.1095 % | 0.730 | 0.1726 % | 1.151 |
| NB-IoT (good cov.) | 2880 | 1.11 % | 7.40 | 1.75 % | 11.7 |

**How to read it.** Below `d*` the entroptics node beats the incumbent endpoint;
above it the blind-decode compute dominates and entroptics loses.

- **vs sleepy LPWAN endpoints `d*` is tiny** (0.005–0.1 %): entroptics wins
  only as a **near-pure TX endpoint** (`d≈0`) — which is exactly what reply-free
  operation makes natural. The `b/msg` column makes it concrete: against an SF7
  endpoint, a node may decode only **~0.03 bursts per message it sends** before
  it falls behind; i.e. one decode costs more than ~20 whole SF7 cycles. Against
  SF12 it is ~0.7 bursts/msg, against NB-IoT ~7 bursts/msg.
- **Coordinated mode moves the crossover in entroptics' favour by ~1.6×** on the
  per-burst term (cheaper decode) **and removes the per-sender acquisition
  duty** entirely — e.g. the SF12 tolerance rises from 0.73 to 1.15 bursts/msg.
- **vs an always-on MANET radio (5–100 W) there is no crossover**: entroptics
  wins at **all** duty cycles in both regimes, because Pi-class decode
  (2.6–5 W) is below their RF front-end draw.

---

## 4. Acquisition-time budget per sender

Blind first contact is a **once-per-sender** cost (out of the per-burst budget;
PLAN scope note), amortized over the messages that sender then sends. Taking a
budget rule of "first contact may add < 10 % to per-message decode energy"
(Pi-5, provisional):

| regime | scan time | energy | breakeven messages/sender |
|---|---|---|---|
| blind, fast | 5 s | 15 J | **556** |
| blind, slow | 150 s | 450 J | **16 700** |
| **coordinated, fast** | 0.2 s | 0.6 J | **22** |
| **coordinated, slow** | 1.9 s | 5.7 J | **211** |

So: a **fully-blind** first-contact scan only pays for itself if the sender then
sends **hundreds to tens of thousands** of messages — fine for a fixed relay
neighbour, wasteful for a one-shot contact. **Coordinated mode** (keyed-schedule
provider) drops first contact to a single narrow-window verify, amortizing in
**~20–210 messages** — **80–750× cheaper** than worst-case blind. This is the
line that most changes the receiver-energy budget, and it is a pure
*provider-side* gain: the core still measures everything blind if the hint is
stale, spoofed or absent.

---

## 5. The BOM caveat

The model's TX-endpoint number (0.84 mJ/burst) assumes a **programmable
transmitter** — an MCU + DAC or a small SDR that can `compose()` the keyed
aperture waveform. That is **not** a $0.50 fixed-function LoRa/BLE SoC. Two
consequences:

1. **Battery life is set by the TX hardware's sleep/active floor, not by
   airtime.** With MCU-class 10 µA sleep the 60 s-cadence endpoint lasts ~23 yr;
   with SDR-class 100 µA residual it is ~3 yr (Profile 1). The airtime energy
   (1.2 J/day) is almost noise next to the sleep floor.
2. **Today's fit is capable endpoints, not coin-cell sensors.** Until an
   entroptics **TX ASIC** exists, the endpoint is a programmable radio
   (handset, vehicle, drone, powered sensor hub), where sub-mJ bursts and
   reply-free sleep are a genuine win. The per-bit air advantage is real now;
   the coin-cell form factor is a future-silicon claim, not a today claim.

Note too that on raw endpoint **energy-per-bit**, BLE (0.13) and Zigbee (0.94
µJ/bit) are at or below entroptics' 0.22 µJ/bit — because they move tiny
payloads on hyper-optimized fixed silicon. entroptics' endpoint advantage over
them is **not** lower energy-per-bit; it is 20–25× more payload per transaction,
no control-plane traffic, an emission with no classifiable structure, and parameter-free multi-rate operation. Its
decisive energy win is over **LoRaWAN / NB-IoT / LTE-M** (66–5000 µJ/bit).

---

## 6. Conclusions

- **The relocation thesis holds and is quantified:** ~0.84 mJ radiated at the
  endpoint vs ~270 mJ (blind) / ~171 mJ (coordinated) of decode compute at the
  receiver — a **~200–320× transfer** from battery TX to (mains) RX. Net
  verdict is entirely **role- and duty-cycle-dependent**, not a blanket
  power saving.
- **Sparse TX-mostly endpoint → capable gateway: entroptics WINS big.** Endpoint
  radiates sub-mJ and sleeps reply-free; the gateway on mains absorbs the
  decode. 12–235× below LoRaWAN on daily endpoint energy.
- **Continuous-listening relay: it depends on the comparator.** A loss (~7–100×)
  against cheap fixed-function LPWAN demods; a **win** against every datasheet
  MANET radio (its Pi-class decode undercuts their 5–100 W RF front end).
- **Crossover:** against LPWAN **endpoints**, entroptics stays ahead only below a
  **~0.005–0.1 % continuous-decode duty cycle** (≈ 0.03–0.7 decoded bursts per
  message sent) — i.e. only as a near-pure TX endpoint. Coordinated mode lifts
  that by ~1.6× and removes the acquisition duty. Against **MANET radios** there
  is no crossover — entroptics wins at duty cycle 1.
- **IoT-endpoint power-win thesis: YES under the model**, with one bound — the
  win is in **radiated/air energy and control-plane traffic**, not in the TX
  hardware's sleep floor, which (until a TX ASIC) caps realized battery life and
  keeps the fit at capable endpoints, not coin cells.

### Biggest sensitivities (what would change the conclusion)

1. **Pi-5 per-burst decode energy** (270 mJ blind / 171 mJ coord, PROVISIONAL).
   This is the entire relay verdict and the crossover `d*`. It is an
   extrapolation from x86; the decode time (60–120 ms/core) and the assumed
   3 W/core each move it linearly. **A real Pi-5 power measurement (PLAN Phase
   0) is the single number that most needs refreshing.** If decode lands at the
   optimistic end (~60 ms, ~2 W = ~120 mJ), the LPWAN crossover roughly doubles
   and the relay case strengthens; at the pessimistic end (~120 ms, ~4 W = ~480
   mJ) the relay loss against LPWAN widens and the MANET win narrows.
2. **The agility-provider prior actually working** (coordinated mode). The
   ~1.6× decode cut and the 80–750× acquisition cut depend on the keyed schedule
   predicting the TxPlan tightly enough to collapse the scan to a local refine.
   If the window cannot be made narrow (clock too loose, geometry drift), the
   receiver falls back to blind and the coordinated column collapses to the
   blind column.
3. **Endpoint TX sleep floor** (10 vs 100 µA). Swings projected battery life
   7×+ and is the real limit on the coin-cell claim — a hardware/BOM choice, not
   a waveform property.
4. **Incumbent energies are datasheet/typical, labelled.** LoRaWAN per-message
   (TTN-measured, 19 B, confirmed+ACK) and NB-IoT/LTE-M (literature, good
   coverage) scale strongly with SF/coverage/payload; deep-coverage NB-IoT can
   be 10×+ the figure used, which would only widen entroptics' endpoint win.

---

## Sources

**entroptics (measured / extrapolated in-repo):** `research/mcs/kL.md` and
`results.md` (burst sizes, goodput, frame framing); `research/realtime/PLAN.md`
and `port_results.md` (decode times, scan share, x86→Pi-5 extrapolation,
acquisition 0.2–150 s).

**Incumbents (datasheet / typical / literature, order-of-magnitude):**
- MANET mesh radio always-on draw (vendor datasheets): Silvus 8–100 W,
  Doodle ≤10.6/14 W, Rajant ME4 5.5/19 W.
- LoRaWAN Class A per-cycle energy (SX1276, 19 B, confirmed uplink + ACK):
  [The Things Network — How Spreading Factor affects battery life](https://www.thethingsnetwork.org/article/how-spreading-factor-affects-lorawan-device-battery-life)
  (SF7 2.78 µWh ≈ 10 mJ; SF12 55.1 µWh ≈ 198 mJ).
- BLE vs Zigbee/802.15.4 energy (bytes/J, per-packet):
  [Siekkinen et al., "How low energy is Bluetooth low energy?" (IEEE 2012)](https://cris.vtt.fi/en/publications/how-low-energy-is-bluetooth-low-energy-comparative-measurements-w/);
  Zigbee ~100 µJ/packet harvesting figure
  ([embedded.com / Jennic JN5148](https://www.embedded.com/?p=4027554)).
- NB-IoT / LTE-M per-message energy (good coverage, modelled from measured TX/RX
  states): [Modelling & Experimental Validation for Battery Lifetime Estimation
  in NB-IoT and LTE-M (arXiv 2106.13286)](https://arxiv.org/pdf/2106.13286);
  [In-depth Real-World Evaluation of NB-IoT Module Energy Consumption (arXiv
  2005.13648)](https://arxiv.org/pdf/2005.13648).

All incumbent figures are datasheet/typical and vary with configuration,
coverage and payload; treat as order-of-magnitude, not benchmarks.
