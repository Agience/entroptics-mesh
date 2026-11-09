"""
air.py -- the shared medium.

Each node has a position, a transmit power and a receiver noise floor.  A burst sent at time t from
node s arrives at node r scaled by the free-space path loss between them at the carrier frequency
(Friis), delayed by the propagation time, and is summed with every other burst overlapping it at r;
r's thermal noise is added once over the whole capture.

Each burst goes out at its sender's own rate and centre.  The converter's output is the band-limited
signal the burst's samples describe (zero outside the burst); the receiver samples it at the air's
rate, at real positions, shifted to the sender's centre.  The reconstruction is exact for the
zero-padded burst, quantizing each position to 1/Q of a transmitted sample -- a model of the
physical channel, not of any receiver.

Units: power in dBm, distance in metres, time in seconds; the IQ a burst is composed at is unit RMS,
so its received power is the transmit power minus path loss.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np

from ..propagation import C_LIGHT, fspl_db  # noqa: F401  (re-exported for the emulator's users)


@dataclass
class Radio:
    """One node's radio as the air sees it."""
    position: np.ndarray                  # metres, (x, y, z)
    tx_power_dbm: float = 0.0
    noise_dbm: float = -100.0             # receiver noise power over its band


Q = 256


@dataclass
class Burst:
    sender: int
    t: float
    iq: np.ndarray
    rate: float | None = None             # the sender's sample rate; None = the air's
    center: float = 0.0                   # Hz from the air's centre


def _at_rx(iq: np.ndarray, ratio: float, n: int, start: float) -> np.ndarray:
    """``n`` samples of the band-limited signal of ``iq`` (zero outside it) at transmitted positions
    ``start + j * ratio``."""
    pad = 64
    x = np.concatenate([np.zeros(pad, complex), iq, np.zeros(pad + 8, complex)])
    N = x.size
    X = np.fft.fft(x)
    k = np.fft.fftfreq(N) * N
    tau = start + pad + np.arange(int(n)) * float(ratio)
    q = np.rint(tau * Q).astype(np.int64)
    whole, frac = np.divmod(q, Q)
    out = np.zeros(int(n), dtype=complex)
    inside = (whole >= 0) & (whole < N)
    for f in np.unique(frac[inside]):
        sel = inside & (frac == f)
        out[sel] = np.fft.ifft(X * np.exp(2j * np.pi * k * (f / Q) / N))[whole[sel]]
    return out


@dataclass
class Air:
    """The medium at one carrier frequency and one sample rate."""
    freq_hz: float = 915e6
    rate: float = 2.0e6
    radios: list[Radio] = field(default_factory=list)
    seed: int = 0

    def __post_init__(self):
        self._bursts: list[Burst] = []
        self._rng = np.random.default_rng(self.seed)

    def add(self, radio: Radio) -> int:
        self.radios.append(radio)
        return len(self.radios) - 1

    def send(self, sender: int, t: float, iq, *, rate: float | None = None, center: float = 0.0) -> None:
        self._bursts.append(Burst(int(sender), float(t), np.asarray(iq, dtype=complex),
                                  None if rate is None else float(rate), float(center)))

    def gain_db(self, a: int, b: int) -> float:
        """Received power at b of a's transmission, dBm, from a's transmit power and the path loss."""
        d = float(np.linalg.norm(self.radios[a].position - self.radios[b].position))
        return self.radios[a].tx_power_dbm - fspl_db(d, self.freq_hz)

    def snr_db(self, a: int, b: int) -> float:
        return self.gain_db(a, b) - self.radios[b].noise_dbm

    def capture(self, receiver: int, t0: float, n: int) -> np.ndarray:
        """What ``receiver`` hears over ``n`` samples from ``t0``: every burst overlapping that window,
        scaled and delayed, plus the receiver's noise.  Its own transmissions are not heard (a
        half-duplex radio does not receive while sending)."""
        r = self.radios[receiver]
        y = np.zeros(int(n), dtype=complex)
        for b in self._bursts:
            if b.sender == receiver:
                continue
            d = float(np.linalg.norm(self.radios[b.sender].position - r.position))
            amp = 10.0 ** (self.gain_db(b.sender, receiver) / 20.0)
            arrive = b.t + d / C_LIGHT - t0                       # seconds into the capture
            if b.rate is None or b.rate == self.rate:
                start = int(round(arrive * self.rate))
                lo, hi = max(0, start), min(int(n), start + b.iq.size)
                if lo >= hi:
                    continue
                seg = np.zeros(int(n), dtype=complex)
                seg[lo:hi] = b.iq[lo - start:hi - start]
            else:
                ratio = b.rate / self.rate                       # transmitted samples per received one
                seg = _at_rx(b.iq, ratio, int(n), -arrive * b.rate)
            if b.center:
                seg = seg * np.exp(2j * np.pi * b.center * np.arange(int(n)) / self.rate)
            y += amp * seg
        s = 10.0 ** (r.noise_dbm / 20.0) / math.sqrt(2.0)
        y += s * (self._rng.standard_normal(y.size) + 1j * self._rng.standard_normal(y.size))
        return y

    def clear_before(self, t: float) -> None:
        self._bursts = [b for b in self._bursts if b.t + b.iq.size / (b.rate or self.rate) >= t]


__all__ = ["fspl_db", "Radio", "Burst", "Air"]
