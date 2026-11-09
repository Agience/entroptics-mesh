"""
interference.py -- the detection side of interference handling: an :class:`~entroptics_mesh.providers.InterferenceProvider`
that reads which cells of the node's band hold energy above the measured noise floor, and reports
them as :class:`~entroptics_mesh.neighbors.InterferenceBand` for the node's announcement.  The avoid side is
the core's: a sender drops the destination's reported bands from its plan (``adapt.default_plan``).

The band is cut into cells of the narrowest width the node plans a burst at (``obs.cell``, its
radio's minimum rate), on the planner's own grid of centres, so a reported cell is exactly a
sub-band a sender can step off.  Per capture, each cell is mixed to baseband, low-passed to its
width (a Blackman-windowed sinc spanning one 64-sample block at the cell's rate), decimated to the
cell's rate, and cut into 64-sample blocks.  The cell's floor is the node's own floor read
(``Node._level``): the ``Node.FLOOR_QUANTILE`` quantile of its block powers over what that
quantile reads on noise alone, the least over the last ``Node.FLOOR_CAPTURES`` captures.  A burst
leaves quiet blocks at the head and tail of a slot and moves between sub-bands, so a cell's floor
is noise unless something occupies it all the time.

The reference is the quietest cell's floor.  A cell holds interference when its floor stands above the
reference by more than noise alone ever reads between the floors of that many cells: the largest
max/min ratio of the same floor read over the same block count and cell count, on 4096 draws of
Gamma(64, 1/64) block powers (a fixed draw, the same every call, as ``phy.noise_quantile``).  The
bound is computed for one capture; the floor held over several captures is a minimum and spreads
less, so the bound only grows more conservative with history.

Limits.  An interferer that covers every cell leaves no quiet reference and is not reported (nor could
a sender step off it).  The test assumes a flat receiver response across the capture: a front end
whose band edges roll off reads its edge cells quieter than the centre.  An interferer that switches on
under a held quiet floor is reported once the quiet captures leave the history.
"""
from __future__ import annotations

import math
from collections import deque
from functools import lru_cache

import numpy as np

from .. import phy
from ..neighbors import InterferenceBand
from ..node import Node
from ..providers import BandObservation, TxPlan

BLOCK = 64                     # samples per block power, at the cell's rate (the node's level read)
DRAWS = 4096                   # noise-alone draws the bound is read from


@lru_cache(maxsize=64)
def noise_spread(blocks: int, cells: int, q: float) -> float:
    """The largest ratio noise alone reads between the highest and lowest of ``cells`` floor reads,
    each the ``q`` quantile of ``blocks`` Gamma(64, 1/64) block powers."""
    g = np.random.default_rng(0).gamma(BLOCK, 1.0 / BLOCK, size=(DRAWS, int(cells), int(blocks)))
    f = np.percentile(g, 100 * float(q), axis=2)
    return float(np.max(f.max(axis=1) / f.min(axis=1)))


@lru_cache(maxsize=16)
def _lowpass(decim: int) -> np.ndarray:
    """Unit-DC-gain low-pass to 1/``decim`` of the band: a Blackman-windowed sinc one block long at
    the decimated rate."""
    n = BLOCK * int(decim) + 1
    k = np.arange(n) - (n - 1) / 2
    h = np.sinc(k / decim) * np.blackman(n)
    return h / h.sum()


def cell_floors(iq, rate: float, cell: float, q: float = Node.FLOOR_QUANTILE):
    """``(centres_hz, floors, blocks)``: each cell's centre offset, its floor read (power per
    sample at the cell's rate, over what the quantile reads on noise alone), and the block count
    the floors were read on.  None when the band holds a single cell or a capture too short for
    two blocks."""
    decim = int(round(float(rate) / float(cell)))
    if decim < 2:
        return None
    iq = np.asarray(iq, dtype=complex)
    h = _lowpass(decim)
    if iq.size < h.size + 2 * BLOCK * decim:
        return None
    centres = (np.arange(decim) - (decim - 1) / 2) * float(cell)
    t = np.arange(iq.size) / float(rate)
    floors, blocks = [], 0
    for c in centres:
        y = np.convolve(iq * np.exp(-2j * np.pi * c * t), h, mode="valid")[::decim]
        blocks = y.size // BLOCK
        p = np.mean(np.abs(y[:blocks * BLOCK].reshape(blocks, BLOCK)) ** 2, axis=1)
        floors.append(float(np.percentile(p, 100 * q)) / phy.noise_quantile(BLOCK, blocks, q))
    return centres, np.asarray(floors), blocks


class EnergyInterference:
    """Reports the cells of this node's band whose floor stands above the quietest cell's by more
    than noise alone reads.  ``review`` passes every plan through: interference at this node's receiver
    is avoided by the peers that send to it."""
    name = "energy"

    def __init__(self, *, history: int = Node.FLOOR_CAPTURES, quantile: float = Node.FLOOR_QUANTILE):
        self.history, self.quantile = int(history), float(quantile)
        self._grid = None                          # (rate, cell) the held floors were read on
        self._floors: list[deque] = []
        self._bands: list[InterferenceBand] = []
        self.bound = math.nan                      # the last ratio a cell had to exceed

    def observe(self, obs: BandObservation) -> None:
        if obs.iq is None or obs.rate is None or obs.cell is None:
            return
        got = cell_floors(obs.iq, obs.rate, obs.cell, self.quantile)
        if got is None:
            self._bands = []
            return
        centres, floors, blocks = got
        if self._grid != (obs.rate, obs.cell):
            self._grid = (obs.rate, obs.cell)
            self._floors = [deque(maxlen=self.history) for _ in centres]
        for d, f in zip(self._floors, floors):
            d.append(f)
        held = np.array([min(d) for d in self._floors])
        ref = float(held.min())
        self.bound = noise_spread(blocks, len(centres), self.quantile)
        if not ref > 0.0:
            self._bands = []
            return
        self._bands = [InterferenceBand(offset_hz=float(c), width_hz=float(obs.cell),
                                level_db=10.0 * math.log10(f / ref))
                        for c, f in zip(centres, held) if f / ref > self.bound]

    def review(self, plan: TxPlan) -> TxPlan | None:
        return plan

    def interference_bands(self) -> list:
        return list(self._bands)


__all__ = ["EnergyInterference", "cell_floors", "noise_spread"]
