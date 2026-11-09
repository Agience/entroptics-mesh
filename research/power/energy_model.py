#!/usr/bin/env python3
"""First-cut energy-per-bit model for entroptics-mesh vs incumbents.

Energy per bit against battery, and the acquisition-time budget per sender.
This is a spreadsheet-class,
order-of-magnitude model, not a lab measurement. No codec is touched.

Central thesis under test (quantified, not asserted):
    entroptics does NOT uniformly lower power. It RELOCATES energy from the
    air / control plane to the RECEIVER's compute.
      SAVES : no CP/pilots/preamble/guard (every TX sample is payload);
              no ACK/handshake/beacon/sync/retransmit (reply-free; forward
              FEC + fountain); a truly sleepable TX-mostly endpoint.
      COSTS : blind acquisition + per-burst decode are DSP-heavy.
    => the net depends on ROLE and DUTY CYCLE. This script finds the
       duty-cycle crossover where entroptics' RX-compute cost overtakes its
       air/control savings vs each incumbent.

ALL entroptics RX-compute numbers are PROVISIONAL. They refresh when the
real-time port (research/realtime/PLAN.md, port_results.md) lands and a
Raspberry Pi 5 is power-measured (Phase 0). Every assumption is a named
constant below.

Sources for the entroptics side (measured / extrapolated, not invented here):
  * burst sizes, goodput, frame framing : research/mcs/kL.md, research/mcs/results.md
  * decode times, Pi-5 extrapolation    : research/realtime/PLAN.md, port_results.md
  * incumbent datasheet draws (MANET)   : vendor datasheets
Incumbent LPWAN/cellular figures are datasheet/typical/literature values,
labelled inline and in energy_model.md; this is an order-of-magnitude model.

Run:  python research/power/energy_model.py
"""

from __future__ import annotations

from dataclasses import dataclass

# --------------------------------------------------------------------------
# 0. Universal assumptions
# --------------------------------------------------------------------------
V_ENDPOINT = 3.3          # V, endpoint rail (LoRa-class compare)
BATTERY_MAH = 3000.0      # mAh, stated Li cell
BATTERY_V = 3.7           # V, nominal Li
BATTERY_USABLE = 0.85     # fraction of nameplate actually usable
BATTERY_J = BATTERY_MAH / 1000.0 * BATTERY_V * 3600.0 * BATTERY_USABLE  # ~34 kJ
SEC_PER_DAY = 86400.0

# --------------------------------------------------------------------------
# 1. entroptics TX endpoint (the part on a battery)
# --------------------------------------------------------------------------
# Burst: DQPSK 1/2, the canonical mid-length mesh burst.
#   research/mcs/kL.md: 4224 complex TX samples, goodput 0.97 b/s/Hz,
#   frame = 512 B protected (35 B header + 475 B fountain block).
#   research/realtime/PLAN.md: "~66-row DQPSK-1/2 burst, L=64" (66*64=4224).
BURST_SAMPLES = 4224
FRAME_BITS_GROSS = 4096          # 512 B protected frame
PAYLOAD_BITS_NET = 475 * 8       # 3800 b, the fountain block actually carried
ENDPOINT_SAMPLE_RATE = 1.0e6     # Msps; a narrow, low-power endpoint band

# PA: entroptics is band-agnostic; pick a LoRa-class endpoint point so the
# air/control comparison is like-for-like.
P_TX_W = 0.025            # 25 mW == 14 dBm (same as LoRaWAN EU default)
PA_EFF = 0.20            # low-power PA efficiency at ~14 dBm (typical; SX1276
                         # at 14 dBm draws ~145 mW DC for 25 mW out => ~17%)
# Baseband/MCU active while composing + radiating the burst. entroptics TX is
# a plain compose() -> DAC; no sync search, no listen. Small but not zero.
P_TX_BASEBAND_W = 0.050   # 50 mW MCU/DAC active
TX_COMPOSE_S = 0.002      # ~2 ms to compose a burst before it goes on air

# Sleep floor of the endpoint HARDWARE (the BOM term, see caveat).
#  low  = MCU-SoC-class deep sleep; high = SDR/DAC-class residual.
SLEEP_UA_LOW = 10.0
SLEEP_UA_HIGH = 100.0


def airtime_s(samples: int = BURST_SAMPLES, rate: float = ENDPOINT_SAMPLE_RATE) -> float:
    return samples / rate


def tx_endpoint_energy_j(samples: int = BURST_SAMPLES,
                         rate: float = ENDPOINT_SAMPLE_RATE) -> dict:
    """Energy a battery endpoint spends to deliver ONE burst (reply-free)."""
    t_air = airtime_s(samples, rate)
    e_pa = (P_TX_W / PA_EFF) * t_air
    e_bb = P_TX_BASEBAND_W * (t_air + TX_COMPOSE_S)
    e_control = 0.0  # reply-free: no RX window, no ACK listen, no join/beacon
    total = e_pa + e_bb + e_control
    return dict(airtime_ms=t_air * 1e3, e_pa_mj=e_pa * 1e3,
                e_bb_mj=e_bb * 1e3, e_control_mj=0.0, total_mj=total * 1e3)


# --------------------------------------------------------------------------
# 2. entroptics RX compute (the part the model RELOCATES to the receiver)
#    *** PROVISIONAL — refreshes on real Pi-5 power measurement (Phase 0). ***
# --------------------------------------------------------------------------
# Decode time per burst (receive_known fast path, one burst):
#   Python reference, measured    : research/realtime/port_results.md
#       canonical DQPSK 135 ms (flag ON), 172 ms (OFF)
#   compiled x86 full-port target : PLAN.md 20-25 ms (projection)
#   Pi-5 full-port projection     : PLAN.md ~60-120 ms/core (extrapolation)
DECODE_MS = {
    "python_ref":        135.0,  # measured, port ON, canonical cell
    "x86_compiled_proj":  22.0,  # projection
    "pi5_fullport_proj":  90.0,  # extrapolation, 1 A76 core (60-120 band mid)
}

# --- Coordinated mode (keyed-schedule agility PROVIDER, not core) ----------
# A pluggable agility provider derives a keyed pseudo-random schedule from
# (shared key, coarse time, optional location) that predicts the sender's
# TxPlan(rate, center, power, K, L, M). Fed to the receiver it:
#   (a) collapses first-contact acquisition 5-150 s -> one verify (0.2-1.9 s,
#       the docs' "width-given" fast path), and
#   (b) collapses the per-burst _scaled_scan (35-51% of a burst; PLAN.md 3a)
#       to a small LOCAL refine around the predicted carrier/start.
# The rest of the known-geometry read (coords/joint/entropy/Viterbi) is
# UNCHANGED, so decode drops ~1.5-1.7x per burst, NOT to a hard-demod floor.
# PROVISIONAL, same caveat as the blind numbers. Lives in the provider; core
# stays parameter-free. The prior ACCELERATES, never REQUIRED:
# a stale/spoofed/absent hint degrades to blind (still verified in the narrow
# window), never breaks. The schedule must be KEYED-pseudorandom so it adds no repeating structure.
SCAN_FRACTION = 0.43        # mid of the 35-51% _scaled_scan share
SCAN_REFINE_KEEP = 0.15     # local refine retains ~15% of the scan cost
_COORD_FACTOR = 1.0 - SCAN_FRACTION * (1.0 - SCAN_REFINE_KEEP)  # ~0.63
for _k in ("python_ref", "x86_compiled_proj", "pi5_fullport_proj"):
    DECODE_MS[_k + "_coord"] = round(DECODE_MS[_k] * _COORD_FACTOR, 1)
# CPU power per active core.
#   Pi 5: ~2.7 W idle, +2-3 W for one busy A76 core, ~8-11 W at 4-core load.
P_CPU_PI5_CORE_W = 3.0     # provisional per-core compute increment
P_CPU_X86_CORE_W = 10.0    # a server/relay x86 core under load (mains side)

# Blind first-contact acquisition — ONCE PER SENDER, out of the per-burst
# budget (PLAN.md §scope). cm_speed / PLAN: width-given 0.2-1.9 s, fully
# blind 5-150 s.
ACQ_S = {"width_given_fast": 0.2, "width_given_slow": 1.9,
         "blind_fast": 5.0, "blind_slow": 150.0}


def rx_decode_energy_j(platform: str = "pi5_fullport_proj",
                       core_w: float = P_CPU_PI5_CORE_W) -> float:
    return core_w * DECODE_MS[platform] / 1e3


def acq_energy_j(mode: str = "width_given_slow",
                 core_w: float = P_CPU_PI5_CORE_W) -> float:
    return core_w * ACQ_S[mode]


def acq_breakeven_messages(mode: str, frac_of_decode: float = 0.10,
                           core_w: float = P_CPU_PI5_CORE_W) -> float:
    """How many messages from a sender are needed before the one-time
    first-contact scan adds less than `frac_of_decode` to per-message decode
    energy. This is the 'acquisition-time budget per sender'."""
    e_acq = acq_energy_j(mode, core_w)
    e_dec = rx_decode_energy_j("pi5_fullport_proj", core_w)
    return e_acq / (frac_of_decode * e_dec)


# --------------------------------------------------------------------------
# 3. Incumbents (datasheet / typical / literature — labelled, NOT measured here)
# --------------------------------------------------------------------------
@dataclass
class Incumbent:
    name: str
    payload_bytes: float
    e_msg_j: float          # endpoint energy to deliver one message (TX+RX+ctrl share)
    note: str
    idle_rx_w: float = 0.0  # always-on RX/idle draw (for the relay profile)

    def e_per_bit_uj(self) -> float:
        return self.e_msg_j / (self.payload_bytes * 8) * 1e6


# LoRaWAN Class A: the key IoT-uplink comparison.
#   TTN measured, SX1276, 19 B, 125 kHz, confirmed uplink + ACK:
#     SF7 2.78 uWh = 10.0 mJ ; SF12 55.1 uWh = 198 mJ.  SF9 interpolated ~45 mJ.
#   (includes the two Class A RX windows and the gateway ACK listen).
LORA_SF7 = Incumbent("LoRaWAN Class A SF7", 19, 10.0e-3,
                     "TTN meas. SX1276 19B conf.+ACK; RX windows incl.")
LORA_SF9 = Incumbent("LoRaWAN Class A SF9", 19, 45.0e-3,
                     "interpolated between TTN SF7/SF12")
LORA_SF12 = Incumbent("LoRaWAN Class A SF12", 19, 198.0e-3,
                      "TTN meas. SX1276 19B conf.+ACK; RX windows incl.")

# BLE connection/notification, small payload (nRF52-class, datasheet-derived):
#   TX ~5 mA @3V, 1 Mbps, short packet; one connection event w/ ACK.
BLE = Incumbent("BLE 4.x notification", 20, 20.0e-6,
                "nRF52-class datasheet; ~20 uJ per small conn. event")

# Zigbee / 802.15.4 packet (CC2538-class / vendor harvesting figure ~100 uJ):
ZIGBEE = Incumbent("Zigbee 802.15.4 packet", 20, 150.0e-6,
                   "CC2538-class; vendor 100 uJ harvest fig; ~100-200 uJ/pkt")

# NB-IoT / LTE-M: full RRC procedure, TX 23 dBm ~200-490 mA, seconds active.
#   Literature (arXiv 2106.13286, 2005.13648): ~1-10 J/msg good coverage.
NBIOT = Incumbent("NB-IoT (good cov.)", 50, 2.0,
                  "lit. arXiv 2106.13286/2005.13648; 1-5 J typ good coverage")
LTEM = Incumbent("LTE-M (good cov.)", 50, 1.0,
                 "lit.; ~0.5-2 J/msg good coverage")

# MANET / mesh radios — always-on draw from vendor datasheets.
#   These are the relay/always-on comparators (idle/RX draw in W).
MANET = [
    ("Silvus SC4400/4200", 8.0, 100.0, "vendor datasheet: 8-100 W draw"),
    ("Doodle Mesh Rider", 10.6, 14.0, "vendor datasheet: <=10.6 W (Pro)/<=14 W (Xtreme)"),
    ("Rajant BreadCrumb ME4", 5.5, 19.0, "vendor datasheet: 5.5 W idle / 19 W peak"),
]

LPWAN = [LORA_SF7, LORA_SF9, LORA_SF12, BLE, ZIGBEE, NBIOT, LTEM]


# --------------------------------------------------------------------------
# 4. Report
# --------------------------------------------------------------------------
def hr(c="-", n=74):
    return c * n


def main():
    out = []
    p = out.append

    p(hr("="))
    p("entroptics-mesh energy-per-bit model  (order-of-magnitude, first cut)")
    p("RX-compute terms are PROVISIONAL (refresh on Pi-5 power measurement).")
    p(hr("="))

    # ---- TX endpoint ----
    tx = tx_endpoint_energy_j()
    p("\n[1] entroptics TX ENDPOINT per burst (the part on a battery)")
    p(f"    burst = DQPSK 1/2, {BURST_SAMPLES} samples @ {ENDPOINT_SAMPLE_RATE/1e6:.1f} Msps")
    p(f"    airtime           : {tx['airtime_ms']:.2f} ms")
    p(f"    PA energy         : {tx['e_pa_mj']:.3f} mJ   (P_tx={P_TX_W*1e3:.0f} mW, eff={PA_EFF})")
    p(f"    baseband/MCU      : {tx['e_bb_mj']:.3f} mJ")
    p(f"    control plane     : {tx['e_control_mj']:.3f} mJ   (reply-free: no ACK/join/beacon)")
    p(f"    TOTAL endpoint TX : {tx['total_mj']:.3f} mJ / burst  (sub-mJ to ~1 mJ)")
    etx_bit = tx['total_mj'] / PAYLOAD_BITS_NET * 1e3  # uJ/bit
    p(f"    => TX energy/bit  : {etx_bit:.3f} uJ/bit  (net {PAYLOAD_BITS_NET} payload bits)")

    # ---- RX compute ----
    p("\n[2] entroptics RX DECODE compute per burst  *** PROVISIONAL ***")
    p("    regime       platform            decode     energy       per-bit")
    for plat, w in (("python_ref", P_CPU_X86_CORE_W),
                    ("x86_compiled_proj", P_CPU_X86_CORE_W),
                    ("pi5_fullport_proj", P_CPU_PI5_CORE_W)):
        for tag, suffix in (("blind ", ""), ("coord ", "_coord")):
            key = plat + suffix
            e = rx_decode_energy_j(key, w)
            ebit = e / PAYLOAD_BITS_NET * 1e6
            p(f"    {tag}{plat:20s}: {DECODE_MS[key]:6.1f} ms x {w:4.1f} W = "
              f"{e*1e3:8.1f} mJ = {ebit:7.1f} uJ/bit")
    e_rx_pi5 = rx_decode_energy_j("pi5_fullport_proj")
    e_rx_pi5_coord = rx_decode_energy_j("pi5_fullport_proj_coord")
    p(f"    design points (Pi-5 full-port): blind {e_rx_pi5*1e3:.0f} mJ/burst, "
      f"coordinated {e_rx_pi5_coord*1e3:.0f} mJ/burst "
      f"({e_rx_pi5/e_rx_pi5_coord:.2f}x lower; scan collapsed, rest of read kept)")

    # ---- acquisition budget ----
    p("\n[3] First-contact ACQUISITION  (once per sender)  *** PROVISIONAL ***")
    p("    BLIND (default, no provider): 5-150 s scan")
    for mode in ("blind_fast", "blind_slow"):
        e = acq_energy_j(mode)
        k = acq_breakeven_messages(mode)
        p(f"      {mode:15s}: {ACQ_S[mode]:6.1f} s -> {e:7.1f} J  "
          f"(breakeven {k:,.0f} msgs/sender for <10% decode overhead)")
    p("    COORDINATED (keyed-schedule provider): one verify in the narrow window")
    for mode in ("width_given_fast", "width_given_slow"):
        e = acq_energy_j(mode)
        k = acq_breakeven_messages(mode)
        p(f"      {mode:15s}: {ACQ_S[mode]:6.1f} s -> {e:7.1f} J  "
          f"(breakeven {k:,.0f} msgs/sender) -- "
          f"{acq_energy_j('blind_slow')/e:,.0f}x cheaper than worst-case blind")

    # ---- Profile 1: sparse IoT uplink ----
    p("\n" + hr("="))
    p("[PROFILE 1] Sparse IoT uplink - endpoint on battery, gateway on mains")
    p(hr("="))
    e_tx_j = tx['total_mj'] / 1e3
    for period_s in (30, 60, 300):
        msgs_day = SEC_PER_DAY / period_s
        tx_day = msgs_day * e_tx_j
        sleep_lo = SLEEP_UA_LOW * 1e-6 * V_ENDPOINT * SEC_PER_DAY
        sleep_hi = SLEEP_UA_HIGH * 1e-6 * V_ENDPOINT * SEC_PER_DAY
        day_lo = tx_day + sleep_lo
        day_hi = tx_day + sleep_hi
        life_lo = BATTERY_J / day_lo / 365.0
        life_hi = BATTERY_J / day_hi / 365.0
        p(f"\n  every {period_s:4d} s  ({msgs_day:6.0f} msgs/day):")
        p(f"    entroptics endpoint TX work : {tx_day:8.2f} J/day")
        p(f"    + sleep {SLEEP_UA_LOW:.0f} uA (MCU-class)  : {day_lo:8.2f} J/day"
          f"  -> {life_lo:5.1f} yr on {BATTERY_MAH:.0f} mAh Li")
        p(f"    + sleep {SLEEP_UA_HIGH:.0f} uA (SDR-class) : {day_hi:8.2f} J/day"
          f"  -> {life_hi:5.1f} yr  (BOM caveat: TX HW floor, not airtime)")
        # incumbent endpoint energy/day for same cadence
        for inc in (LORA_SF7, LORA_SF9, LORA_SF12):
            inc_day = msgs_day * inc.e_msg_j
            inc_life = BATTERY_J / inc_day / 365.0
            p(f"    {inc.name:22s}      : {inc_day:8.2f} J/day"
              f"  -> {inc_life:5.1f} yr   (air+RX+ctrl on the SAME battery)")

    # ---- energy-per-bit table ----
    p("\n[Profile 1] ENERGY-PER-BIT, endpoint/air side (who pays what)")
    p(f"    {'system':26s} {'payload':>8s} {'e/msg':>10s} {'e/bit':>12s}")
    p(f"    {'entroptics endpoint TX':26s} {PAYLOAD_BITS_NET//8:6d} B "
      f"{tx['total_mj']:8.3f} mJ {etx_bit:8.3f} uJ/b")
    for inc in LPWAN:
        p(f"    {inc.name:26s} {inc.payload_bytes:6.0f} B "
          f"{inc.e_msg_j*1e3:8.3f} mJ {inc.e_per_bit_uj():8.3f} uJ/b")
    p("    entroptics gateway RX-compute (Pi-5, PROVISIONAL, on MAINS):")
    p(f"        blind       : {e_rx_pi5*1e3:.0f} mJ/burst = "
      f"{e_rx_pi5/PAYLOAD_BITS_NET*1e6:.1f} uJ/bit")
    p(f"        coordinated : {e_rx_pi5_coord*1e3:.0f} mJ/burst = "
      f"{e_rx_pi5_coord/PAYLOAD_BITS_NET*1e6:.1f} uJ/bit "
      f"(endpoint TX unchanged at {etx_bit:.3f} uJ/bit; the WIN widens on the")
    p("                      system side -- cheaper decode + near-free first contact)")

    # ---- Profile 2: continuous relay ----
    p("\n" + hr("="))
    p("[PROFILE 2] Continuous-listening relay / always-on RX (duty cycle ~1)")
    p(hr("="))
    # entroptics continuous decode: a node decoding back-to-back bursts.
    # Power = per-core compute (it is running the blind DSP whenever there is
    # signal, and scanning when idle). Add an SDR RX front-end draw.
    SDR_RX_W = 2.0
    # A continuous-stream relay's average compute scales with the core-seconds
    # per burst; coordinated mode cuts the per-burst decode by _COORD_FACTOR,
    # so for the same offered traffic the effective compute load drops too.
    ent_relay_w_pi5 = P_CPU_PI5_CORE_W + SDR_RX_W
    ent_relay_w_pi5_coord = P_CPU_PI5_CORE_W * _COORD_FACTOR + SDR_RX_W
    p(f"  entroptics relay (Pi-5, 1 core decoding + SDR RX {SDR_RX_W} W):"
      f"  *** PROVISIONAL ***")
    p(f"      blind       : ~{ent_relay_w_pi5:.1f} W continuous")
    p(f"      coordinated : ~{ent_relay_w_pi5_coord:.1f} W continuous "
      f"(scan collapsed to a local refine; no re-acquisition)")
    p("  vs cheap fixed-function demod (always-on RX):")
    p(f"    BLE/Zigbee fixed-function RX : ~0.03-0.06 W  "
      f"(entroptics LOSES ~{ent_relay_w_pi5/0.05:.0f}x blind, ~{ent_relay_w_pi5_coord/0.05:.0f}x coord"
      f" -- DSP vs hard demod)")
    p(f"    LoRa concentrator (SX130x)   : ~0.4-0.8 W   "
      f"(LOSES ~{ent_relay_w_pi5/0.6:.0f}x blind / ~{ent_relay_w_pi5_coord/0.6:.0f}x coord)")
    p("  vs MANET radio always-on draw (vendor datasheets):")
    for name, lo, hi, note in MANET:
        verdict = "WINS" if ent_relay_w_pi5 < lo else "comparable"
        p(f"    {name:22s}: {lo:.1f}-{hi:.1f} W  -> entroptics {verdict} "
          f"(compute {ent_relay_w_pi5_coord:.0f}-{ent_relay_w_pi5:.0f} W < RF front end)")

    # ---- Crossover sweep ----
    p("\n" + hr("="))
    p("[CROSSOVER] duty cycle where entroptics RX-compute overtakes its savings")
    p(hr("="))
    p("  Model a battery node that SENDS M msgs/day and must also DECODE a")
    p("  fraction d of the time (blind DSP). entroptics node energy/day =")
    p("    M * e_tx  +  d * P_decode * 86400    (+ amortized acq)")
    p("  Compare to the incumbent ENDPOINT budget for the same M msgs/day.")
    p(f"  entroptics: e_tx={tx['total_mj']:.2f} mJ, P_decode(Pi-5)={P_CPU_PI5_CORE_W:.0f} W.\n")

    p(f"  {'incumbent':22s} {'J/day':>8s}  {'blind d*':>10s} {'bl b/msg':>9s}"
      f"  {'coord d*':>10s} {'co b/msg':>9s}")
    e_dec_blind = rx_decode_energy_j("pi5_fullport_proj")
    e_dec_coord = rx_decode_energy_j("pi5_fullport_proj_coord")
    for inc in (LORA_SF7, LORA_SF9, LORA_SF12, NBIOT):
        period_s = 60
        M = SEC_PER_DAY / period_s
        ent_tx_day = M * e_tx_j
        inc_day = M * inc.e_msg_j
        budget = inc_day - ent_tx_day
        if budget <= 0:
            p(f"  {inc.name:22s} {inc_day:8.1f}  entroptics TX alone already exceeds it")
            continue
        d_blind = budget / (P_CPU_PI5_CORE_W * SEC_PER_DAY)
        d_coord = budget / (P_CPU_PI5_CORE_W * _COORD_FACTOR * SEC_PER_DAY)
        bpm_blind = budget / e_dec_blind / M
        bpm_coord = budget / e_dec_coord / M
        p(f"  {inc.name:22s} {inc_day:8.1f}  {d_blind*100:9.4f}% {bpm_blind:9.3f}"
          f"  {d_coord*100:9.4f}% {bpm_coord:9.3f}")

    p("\n  d* = continuous-decode duty cycle at which entroptics node energy")
    p("  equals the incumbent ENDPOINT budget; b/msg = bursts a node may decode")
    p("  per message it sends at that breakeven. Reading:")
    p("   - Below d* entroptics wins; above it, blind-decode compute dominates.")
    p("   - vs sleepy LPWAN endpoints d* is tiny: entroptics wins only as a")
    p("     near-pure TX endpoint (reply-free, d~0). Coordinated mode raises the")
    p(f"     tolerance by {_COORD_FACTOR**-1:.2f}x (cheaper decode) and removes the")
    p("     per-sender acquisition duty -- the crossover moves in entroptics' favour.")
    p("   - vs an always-on MANET radio (5-100 W) entroptics WINS AT ALL duty")
    p("     cycles in BOTH regimes: Pi-class decode (2.6-5 W) < their RF front end.")

    print("\n".join(out))


if __name__ == "__main__":
    main()
