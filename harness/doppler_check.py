"""
Flight-Doppler robustness: is Doppler even significant for entroptics at drone speeds?

Two effects of a node's radial velocity v (carrier 915 MHz):
  * carrier Doppler  f_d = v/c * f_c  Hz   -- a frequency offset the receiver already measures blind.
  * clock/rate scale v/c (ppm)             -- a tiny sample-rate change the scaled read already absorbs
                                               (we routinely handle the ~0.75 ppm Pluto/NESDR clock gap).

This applies a constant carrier Doppler to a composed capture and decodes it BLIND (told only the key),
across realistic drone speeds, and reports BER + lock closure + the measured width. If it holds with
margin at these speeds, the harness needs no dedicated Doppler model -- air.py's existing per-burst
`center` (a burst is ~ms, so Doppler is ~constant over it) already covers it. Entroptics-pure: only a
carrier offset is applied to the test IQ; the receiver measures the carrier as it always does.
"""
from __future__ import annotations

import numpy as np

from entroptics_rf import ber, compose, decode, random_bits

C = 299_792_458.0
FC = 915e6
FS = 2.4e6                      # receiver/air sample rate
L = 64
KEY = 0xA17C0DE
WIDTHS = range(60, 69)


def run() -> None:
    bits = random_bits(12000, seed=1)
    iq = compose(bits, K=L, L=L, seed=KEY)                      # keyed QPSK, ~94 rows (blind-L needs >~80)
    b0, d0 = decode(iq, KEY, WIDTHS)
    print(f"baseline (no Doppler): BER {ber(bits, b0):.4f}  closure {d0.closure:.3f}  L={d0.L}\n")
    print(f"context: receiver carrier search spans +/-{1.0:.0f} cycle/row; clock gap we handle ~0.75 ppm\n")
    print(f"{'v(m/s)':>6} {'f_d(Hz)':>8} {'ppm':>6} {'cyc/row':>8} | {'BER':>7} {'closure':>7} {'L':>3}")
    n = np.arange(iq.size)
    for v in (10, 25, 50, 100, 150, 300):
        fd = v / C * FC
        ppm = v / C * 1e6
        cyc_row = fd * L / FS
        iq_d = iq * np.exp(2j * np.pi * fd * n / FS)            # carrier Doppler, constant over the capture
        b, d = decode(iq_d, KEY, WIDTHS)
        print(f"{v:6d} {fd:8.1f} {ppm:6.3f} {cyc_row:8.4f} | {ber(bits, b):7.4f} {d.closure:7.3f} {d.L:3d}")
    print("\nverdict: if BER/closure track the baseline across the fleet's speed range, flight Doppler is\n"
          "negligible for the blind read -- no dedicated Doppler channel needed (per-burst center suffices).")


if __name__ == "__main__":
    run()
