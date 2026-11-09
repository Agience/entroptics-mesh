"""
Link-budget sweep -- size the fleet from the physics, before buying anything.

Answers "stock Pluto or a PA? what antenna? how many relays for the scenario range?" with numbers.
Uses the SAME model the mesh plans and the emulated air deliver on (entroptics_mesh.placement /
adapt free-space + horizon), so it is consistent with the fly-out results.

Two ranges per config:
  * RAW decodable hop -- the longest hop whose predicted SNR clears the decode target (HOP_SNR_DB,
    ~99% of bursts decoded). This is roughly how far a DIRECT link actually reaches (matches the
    fly-out: 20 dBm delivered direct to ~12-15 km).
  * PLANNED hop -- what the formation budgets: HOP_SNR_DB + MARGIN + FADE (10 dB). More conservative,
    so relays are placed for robustness/position-error even while the raw link still punches through.

Then: relays needed to hold a target range on the PLANNED budget (what the self-arranging formation
would place), at slack 0.8 (the relay_chain headroom).
"""
from __future__ import annotations

import math

from entroptics_mesh import placement
from entroptics_mesh.adapt import HOP_SNR_DB, MARGIN_DB, Radio

ALT = 100.0                        # node altitude (m)
TARGET_RANGE_KM = 25.0             # the flight-test range to size relays for
RATES = (0.25e6, 0.5e6, 1.2e6)     # MS/s: max-range, mid, FPV-ish
POWERS = ((20.0, "stock Pluto 100 mW"), (27.0, "0.5 W PA"), (30.0, "1 W PA"), (33.0, "2 W PA"))
GAINS = (0.0, 3.0, 6.0)            # dBi per end (whip / small Yagi / panel)


def radio(power_dbm: float, gain_dbi: float) -> Radio:
    return Radio(min_rate=0.25e6, max_rate=2.4e6, center_hz=915e6, max_power_dbm=power_dbm,
                 noise_dbm=-100.0, freq_hz=915e6, antenna_dbi=gain_dbi)


def relays_for(range_m: float, planned_hop_m: float, slack: float = 0.8) -> int:
    """Relay slots to hold ``range_m`` on the planned budget (mirrors placement.relay_chain)."""
    hop = slack * planned_hop_m
    if range_m <= hop:
        return 0
    return max(1, math.ceil(range_m / hop) - 1)


def sweep() -> None:
    print(f"915 MHz, alt {ALT:.0f} m, decode target HOP_SNR_DB={HOP_SNR_DB:.0f} dB (+MARGIN {MARGIN_DB:.0f} "
          f"+FADE {placement.FADE_DB:.0f} planned); relays sized to hold {TARGET_RANGE_KM:.0f} km.\n")
    for rate in RATES:
        print(f"=== rate {rate/1e6:.2f} MS/s ===")
        print(f"{'power':>16} {'gain':>5} | {'raw hop':>8} {'planned hop':>11} | {'relays for '+str(int(TARGET_RANGE_KM))+'km':>13}")
        for p, label in POWERS:
            for g in GAINS:
                r = radio(p, g)
                raw = placement.max_hop_m(r, rate, ALT, ALT, target_db=HOP_SNR_DB) / 1000.0
                planned = placement.max_hop_m(r, rate, ALT, ALT) / 1000.0
                n = relays_for(TARGET_RANGE_KM * 1000.0, planned * 1000.0)
                note = "" if n <= 3 else ("  (>3: need more/kit)" if n <= 8 else "  (impractical)")
                print(f"{label:>16} {g:4.0f}  | {raw:7.1f}  {planned:10.1f}  | {n:12d}{note}")
        print()
    print("Reading it: RAW ~ direct-link reach; PLANNED ~ per-hop spacing the formation uses. "
          "'relays for 25km' <= 3 means your 5-node starter kit (2 FPV + 3 relays) covers it.")


if __name__ == "__main__":
    sweep()
