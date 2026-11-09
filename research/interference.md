# Interference: the interfered-band report

A node's interference is **local** — an interferer near it floods a band on *its* receiver, but a peer
elsewhere sees a clean band and would never know to avoid it. So the node that measures the
interference tells its peers, in its optional sealed state announcement: *"my receiver sees
interference on this band, at this level, from this bearing."*

This is a **measurement of the local RF environment**, not a decode-quality report — it is read from
ambient energy on the node's own receiver, needs no one to have transmitted to it, and so stays within
the reply-free contract (the same category as the announcement's `heard`/SNR map and `tx_power`). It is
a **clue, never a constraint**: a peer steers off the band when it can, and is never cornered into
silence.

## Wire form (relative, never absolute)

Each report (`neighbors.InterferenceBand`, carried in `announce`, flag bit4 `interference`) is:

| field | meaning |
|---|---|
| `offset_hz` | centre of the interfered band, **relative to this node's own announced band centre** (not a standalone absolute frequency) |
| `width_hz` | width of the interfered band |
| `bearing_deg` | direction the interference arrives from, in the node's array frame, if the array measured it (else absent) |
| `level_db` | interference level, if measured (else absent) |

Nothing in the protocol is an absolute frequency: a peer resolves `offset_hz` against the announcer's
announced `BandRange.center_hz`.

## Use

1. **Steer off it (avoidance).** A node planning a transmission *to* a peer that reports interference
   resolves that peer's interfered bands into its own centre-offset space and drops any sub-band whose
   occupied width overlaps one (`adapt.default_plan(interference=…)`, the same soft sub-band mechanism as
   the never-the-heard-band and sibling-avoid rules — if every sub-band is interfered it transmits
   anyway). A stronger response (hop, spread) is the `AgilityProvider`/`InterferenceProvider`'s; the core
   only respects the clue.

## Core vs provider

- **Core (transport + clue):** `announce` carries the field; `neighbors.Announced.interference` stores it;
  `default_plan` steers off a destination's reported bands as a soft clue. Carrying a field and nudging
  a sub-band is not an interference-handling algorithm.
- **Provider (measurement + policy):** the `InterferenceProvider` decides *which* bands count as
  interfered (from its `observe(BandObservation)` interference reads) and measures the bearing, returns
  them from `interference_bands()` to announce, and owns any stronger response in `review`. **Null
  provider → no interfered bands → the announcement is byte-identical to an announcement without the
  field.** A worked example is `interference_provider.py` (`ThresholdInterference`: threshold the
  per-band interference, cluster the hot captures, report them) — an example, not shipped in the core.

## The detector (`strehl`) — validated in sim

The measurement behind `interference_bands()` is `waveform.interference(iq, L).strehl` — power in the
dominant coherent mode. The entroptics waveform has a flat spectrum (no dominant mode), so a
concentrated interferer lands as exactly the mode our own signal lacks. Validated on synthetic data: noise and the
entroptics waveform sit at the floor (strehl ≈ 0.022, **no false alarm on our own traffic**); a CW
interferer climbs monotonically (strehl ≈ 0.11 → 0.99 over −10 → +20 dB INR) and a LoRa chirp climbs
too, both localized by the coarse-centre read. **Validated on real hardware:** a RAK2287 LoRa radio
keyed CW at 915.5 MHz, cabled (RAK → 20 cm → 30 dB pad → NESDR), read on the NESDR at 915.0 MHz —
**strehl 0.020 (clean) → 0.62 (with interference), ~31×, localized to +0.5 MHz** (the exact offset),
and back to 0.020 with the interferer off. Notably the capture's total power (`rx-std`) did **not**
move: the narrowband CW barely lifts the band energy, yet `strehl` jumps — so the read catches an
interferer a plain energy/power detector would miss.

A power sweep (RAK `--pwid` 0→22) gave a monotone curve, strehl **0.59 → 0.997**, localized to +0.5 MHz
throughout; over that range the capture energy (`rx-std`) rose only 0.012 → 0.139, so at the low end the
interferer is **~15–20 dB below energy-detectability** while strehl already reads > 0.5. A
**LoRa-packet interferer** (SF7/BW250) was also caught — strehl **0.15** (~7× the floor), localized —
lower than CW because the chirp sweeps across modes, matching the sim. (The RAK's minimum power through
the 30 dB pad still gives strehl 0.59, so the low-strehl threshold end would need more attenuation to
map.)

**Blind spot:** a steady CW at an exactly *row-commensurate* frequency (an integer number of cycles
per row at the operating width L) is absorbed by the carrier-invariant aperture and NOT flagged
(strehl stays at the floor). A LoRa interferer (swept chirps) is always caught; a steady CW is caught at
any non-commensurate frequency — which any real channel is. A provider wanting to close even this gap
can sweep L / the row alignment; the core only reports the read.

## Limits

- `bearing_deg` is in the node's **array frame**; a peer needs the announcer's orientation to use it
  in its own frame.
- Which band counts as interfered and how the bearing is read are **provider measurements** — the core
  ships no threshold. The reference `ThresholdInterference` in `interference_provider.py` (used by the
  demo) is illustrative, not a shipped policy; it reports the band but leaves bearing unmeasured
  (bearing needs the array's angle of arrival fed into `observe`).
- The interfered region is a time-stamped clue (it ages with the announcement); local interference is
  transient. A peer with nothing else usable still transmits — degraded beats silent.
