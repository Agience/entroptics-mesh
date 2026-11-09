"""
propagation.py -- path loss, for planning.  Free space (Friis) is what the core plans with; it is
optimistic near the ground, and the plan's margin is where that is paid for.
"""
from __future__ import annotations

import math

C_LIGHT = 299_792_458.0


def fspl_db(distance_m: float, freq_hz: float) -> float:
    """Free-space path loss (Friis), dB.  Below one wavelength the far-field law does not apply;
    the loss is held at its one-wavelength value."""
    lam = C_LIGHT / float(freq_hz)
    d = max(float(distance_m), lam)
    return 20.0 * math.log10(4.0 * math.pi * d / lam)


__all__ = ["C_LIGHT", "fspl_db"]
