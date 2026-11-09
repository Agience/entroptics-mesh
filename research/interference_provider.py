"""A worked-example InterferenceProvider -- NOT part of the shipped core (the core ships only the
`InterferenceProvider` interface + `NullInterference`). It shows the measure -> report path: threshold the
interference the node measures per captured band, and report the hot bands (relative to the node's
own band centre) so peers sending TO this node steer off them.

A real provider owns the threshold, the clustering/tracking, the bearing read (array AOA), and any
mitigation in `review()`; this is the minimal illustration an integrator adapts.

Run via research/interference_locate.py.
"""
from __future__ import annotations

from collections import deque

from entroptics_mesh.neighbors import InterferenceBand
from entroptics_mesh.providers import BandObservation, TxPlan


class ThresholdInterference:
    """Flags a band as interference when its measured interference exceeds ``threshold`` and reports it as
    a :class:`~entroptics_mesh.neighbors.InterferenceBand` (offset = the capture centre relative to the node's
    band centre, a configured width, the measured level). Bearing is left ``None`` here: it needs the
    array AOA of the interferer fed into ``observe`` (the orientation-adjacent refinement), not the
    scalar interference read ``BandObservation`` carries today."""
    name = "threshold-example"

    def __init__(self, threshold: float, width_hz: float, memory_s: float = 5.0):
        self.threshold = float(threshold)
        self.width_hz = float(width_hz)
        self.memory_s = float(memory_s)
        self._obs: deque[BandObservation] = deque()

    def observe(self, obs: BandObservation) -> None:
        self._obs.append(obs)
        cutoff = obs.t - self.memory_s                     # age out stale observations
        while self._obs and self._obs[0].t < cutoff:
            self._obs.popleft()

    def review(self, plan: TxPlan) -> TxPlan | None:
        return plan                                        # no mitigation in the example

    def interference_bands(self) -> list[InterferenceBand]:
        hot = [o for o in self._obs if o.interference >= self.threshold]
        out: list[InterferenceBand] = []
        used: list[float] = []
        for o in sorted(hot, key=lambda o: -o.interference):   # strongest first, cluster by centre
            if any(abs(o.center - c) < self.width_hz for c in used):
                continue
            used.append(o.center)
            out.append(InterferenceBand(offset_hz=float(o.center), width_hz=self.width_hz,
                               bearing_deg=None, level_db=float(o.interference)))
        return out
