"""
predict.py -- an example predictive :class:`AgilityProvider` that USES the flight-state fields of
:class:`~entroptics_mesh.providers.PlanContext` (``velocity`` / ``acceleration`` / ``trajectory`` /
``history``).  It is the "here's how you'd plug it in" companion to :class:`KeyedSchedule`: it shows
the sockets for link-margin levers (directional beam + spread width + a coordination prior), driven
by where a node is going -- not a production algorithm.

What is REAL here (the core honours it, end to end):
  * ``plan`` steers the transmit WIDTH from motion + the heard link: a faster node, or a weaker
    measured link to the intended receiver, gets a wider (more spread, more margin) plan; a slow
    node on a strong short link narrows.  The node transmits on the returned ``TxPlan`` (the refit
    keeps a widened plan, see test_provider_contract).  Width = depth x spread-factor is the
    bandwidth-for-margin / process-gain lever (spreading buys 10log10(SF) of decode gain).
  * it inherits the keyed transmit SCHEDULE from :class:`KeyedSchedule` (the coordination prior that
    lets a keyed receiver predict the start and despread/lock without a blind search -- the hint that
    makes below-noise operation possible).

What is ILLUSTRATIVE (computed from the flight state, but the simulated air has no antenna pattern to
apply it to -- a beamforming/array provider acts on it):
  * :meth:`predict_position` dead-reckons where this node will be ``horizon_s`` ahead, from ``me`` +
    ``velocity`` (+ ``acceleration``, and snapped to ``trajectory`` if the plan is known).
  * :meth:`beam_bearing` returns the azimuth/elevation from one position to another -- the pointing a
    directional array would steer toward a peer's predicted position (antenna gain toward the
    intended receiver, and less energy spread toward everyone else sharing the band).

Everything needs only this node's own state + what it has heard (the neighbour table) + the schedule
key; nothing comes back from a peer, so the one-way contract holds.
"""
from __future__ import annotations

import math

from .neighbors import Position
from .providers import PlanContext, TxPlan, replace
from .schedules import KeyedSchedule

_M_PER_DEG = 111_320.0                                   # metres per degree of latitude (flat-earth, short horizons)


class PredictiveAgility(KeyedSchedule):
    """Example predictive provider: motion/link-aware spread width + keyed schedule, plus the
    directional-pointing computation a beamformer would use.  ``spread_max`` caps how far it widens
    (a multiple of the core's default rate); ``horizon_s`` is the prediction lead time."""
    name = "predictive"

    def __init__(self, key, *, epoch_s: float = 1.0, spread_max: float = 4.0, horizon_s: float = 1.0):
        super().__init__(key, epoch_s=epoch_s)
        self.spread_max = float(spread_max)
        self.horizon_s = float(horizon_s)

    # ---- REAL: steer the width from motion + the heard link ----------------------------------
    def plan(self, intent, default: TxPlan, table, ctx: PlanContext | None = None) -> TxPlan:
        factor = 1.0
        speed = _speed(ctx.velocity) if (ctx and ctx.velocity) else 0.0
        factor *= 1.0 + min(speed / 30.0, 1.0)           # up to 2x wider when moving fast (fades change fast)
        if table is not None and intent is not None and getattr(intent, "to", None):
            nb = table.get(intent.to) if hasattr(table, "get") else None
            snr = getattr(getattr(nb, "measured", None), "snr_db", None) if nb else None
            if snr is not None and snr < 10.0:
                factor *= 1.5                            # weak link -> spread more for margin
        factor = max(1.0, min(factor, self.spread_max))
        if factor <= 1.0:
            return default
        return replace(default, rate=float(default.rate) * factor)   # wider = more spread/margin

    # ---- ILLUSTRATIVE: where this node will be, and where to point --------------------------------
    def predict_position(self, ctx: PlanContext, horizon_s: float | None = None) -> Position | None:
        """Dead-reckon this node's position ``horizon_s`` ahead from its own flight state."""
        if ctx is None or ctx.me is None:
            return None
        h = self.horizon_s if horizon_s is None else float(horizon_s)
        if ctx.trajectory:                               # if the plan is known, trust it: nearest planned point >= t+h
            target_t = ctx.t + h
            ahead = [p for (pt, p) in ctx.trajectory if pt >= target_t]
            if ahead:
                return ahead[0]
        v = ctx.velocity or (0.0, 0.0, 0.0)
        a = ctx.acceleration or (0.0, 0.0, 0.0)
        de = v[0] * h + 0.5 * a[0] * h * h               # ENU metres (east, north, up)
        dn = v[1] * h + 0.5 * a[1] * h * h
        du = v[2] * h + 0.5 * a[2] * h * h
        lat = ctx.me.lat + dn / _M_PER_DEG
        lon = ctx.me.lon + de / (_M_PER_DEG * math.cos(math.radians(ctx.me.lat)))
        return Position(lat, lon, ctx.me.alt + du)

    @staticmethod
    def beam_bearing(frm: Position, to: Position) -> tuple[float, float]:
        """Azimuth (deg, 0=N, CW) and elevation (deg) from ``frm`` to ``to`` -- the pointing a
        directional array steers.  Local-tangent (ENU) approximation."""
        clat = math.cos(math.radians(frm.lat))
        de = (to.lon - frm.lon) * math.radians(1.0) * 6_371_000.0 * clat
        dn = (to.lat - frm.lat) * math.radians(1.0) * 6_371_000.0
        du = to.alt - frm.alt
        az = (math.degrees(math.atan2(de, dn))) % 360.0
        el = math.degrees(math.atan2(du, math.hypot(de, dn)))
        return az, el


def _speed(v) -> float:
    return math.sqrt(sum(float(c) ** 2 for c in v))


__all__ = ["PredictiveAgility"]
