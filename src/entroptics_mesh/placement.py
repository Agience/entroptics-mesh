"""
placement.py -- the relay role: which links hold, who is cut off, and where relays should go.

A relay is any node told to hold a position rather than fly a mission; nothing else about it differs.
The rule every link is held to is the one the emulation measured: an SNR of ``HOP_SNR_DB`` (>= 99%
of bursts decoded) plus ``MARGIN_DB``.

  * **Measured beats predicted.**  A link's SNR is what a node said it heard (the heard list in an
    announcement) when anyone said so, else the free-space prediction from the two positions.  A
    predicted link is flagged: positions are claims, and GPS may be wrong or spoofed.  Planning uses
    predictions only where nothing was heard.
  * **Predictions pay a fade margin** (``FADE_DB``); measurements do not -- they already hold it.
  * **The horizon caps every hop.**  No link budget carries a signal past the radio horizon.
  * **Relays come in parallel.**  Each relay slot gets ``parallel`` relays side by side, so a slot
    survives the loss of one (measured: two relays per slot with sibling share delivered 20/20 with
    one lost; one relay per slot delivered 0/10).

Nothing here transmits or needs a reply; it is arithmetic over what the node table already holds.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

from .adapt import HOP_SNR_DB, MARGIN_DB, Radio, _snr_at
from .neighbors import Position
from .propagation import C_LIGHT

EARTH_M = 6_371_000.0
FADE_DB = 10.0      # what a free-space prediction is debited for ground reflection, fading and
                    # position error.  A measured SNR already contains all of that, so it is not.


def horizon_m(alt_a: float, alt_b: float) -> float:
    """Radio horizon between two antennas (4/3 earth): 4.12 (sqrt h1 + sqrt h2) km, heights in m."""
    return 4120.0 * (math.sqrt(max(alt_a, 0.0)) + math.sqrt(max(alt_b, 0.0)))


def predicted_snr_db(radio: Radio, distance_m: float, rate: float, power_dbm: float | None = None,
                     peer_dbi: float | None = None) -> float:
    """Free-space SNR at ``distance_m`` for a sender at ``rate``: both antenna gains in, noise
    scaled to the width (the same prediction :mod:`adapt` plans power with)."""
    p = radio.max_power_dbm if power_dbm is None else power_dbm
    return _snr_at(radio, p, distance_m, rate, peer_dbi)


def max_hop_m(radio: Radio, rate: float, alt_a: float, alt_b: float,
              target_db: float = HOP_SNR_DB + MARGIN_DB + FADE_DB) -> float:
    """Longest hop whose free-space prediction clears ``target_db`` (fade margin included) at full
    power, capped by the horizon."""
    noise = radio.noise_dbm + 10 * math.log10(rate / radio.max_rate)
    loss = radio.max_power_dbm + 2 * radio.antenna_dbi - noise - target_db
    lam = C_LIGHT / radio.freq_hz
    return min(lam / (4 * math.pi) * 10 ** (loss / 20), horizon_m(alt_a, alt_b))


@dataclass(frozen=True)
class Link:
    a: bytes
    b: bytes
    snr_db: float
    measured: bool                  # False: predicted from claimed positions only


def links(positions: dict[bytes, Position], heard: dict[tuple[bytes, bytes], float], radio: Radio,
          rate: float, target_db: float = HOP_SNR_DB + MARGIN_DB,
          fade_db: float = FADE_DB, distrusted=frozenset()) -> list[Link]:
    """Every usable link.  ``heard[(a, b)]`` is the SNR b reported hearing a at; a link reported in
    both directions takes the weaker.  Unreported pairs fall back to prediction from positions,
    which the horizon also bounds."""
    ids = sorted(set(positions) | {x for k in heard for x in k})
    out = []
    for i, a in enumerate(ids):
        for b in ids[i + 1:]:
            said = [heard[k] for k in ((a, b), (b, a)) if k in heard]
            if said:
                snr, measured = min(said), True
            elif a in positions and b in positions and a not in distrusted and b not in distrusted:
                pa, pb = positions[a], positions[b]
                d = pa.distance_m(pb)
                if d > horizon_m(pa.alt, pb.alt):
                    continue
                snr, measured = predicted_snr_db(radio, d, rate), False
            else:
                continue
            if snr >= target_db + (0.0 if measured else fade_db):
                out.append(Link(a, b, float(snr), measured))
    return out


def reachable(usable: list[Link], homes) -> set[bytes]:
    """Nodes connected to any home (gateway) through usable links."""
    adj: dict[bytes, set[bytes]] = {}
    for l in usable:
        adj.setdefault(l.a, set()).add(l.b)
        adj.setdefault(l.b, set()).add(l.a)
    seen, stack = set(homes), list(homes)
    while stack:
        for n in adj.get(stack.pop(), ()):
            if n not in seen:
                seen.add(n)
                stack.append(n)
    return seen


def _toward(a: Position, b: Position, f: float, side_m: float = 0.0) -> Position:
    """The point a fraction ``f`` of the way from a to b (local flat-earth step, fine at the hop
    scale it is used for), pushed ``side_m`` to the left of the a->b direction."""
    north = (b.lat - a.lat) * math.pi / 180 * EARTH_M
    east = (b.lon - a.lon) * math.pi / 180 * EARTH_M * math.cos(math.radians((a.lat + b.lat) / 2))
    norm = math.hypot(north, east) or 1.0
    n = north * f - east / norm * side_m
    e = east * f + north / norm * side_m
    lat = a.lat + n / EARTH_M * 180 / math.pi
    lon = a.lon + e / (EARTH_M * math.cos(math.radians((a.lat + b.lat) / 2))) * 180 / math.pi
    return Position(lat, lon, a.alt + (b.alt - a.alt) * f)


def relay_chain(start: Position, end: Position, radio: Radio, rate: float, *, relay_alt: float,
                parallel: int = 2, spread_m: float = 2000.0, slack: float = 0.8) -> list[list[Position]]:
    """Relay slots from ``start`` to ``end``: each slot is ``parallel`` positions ``spread_m`` apart
    across the line, at ``relay_alt``.  Slots sit at ``slack`` x the longest hop, so the plan keeps
    headroom for position error and fading beyond the margin.  Empty when ``end`` is in reach."""
    s = Position(start.lat, start.lon, relay_alt)
    e = Position(end.lat, end.lon, relay_alt)
    hop = slack * max_hop_m(radio, rate, relay_alt, relay_alt)
    d = start.distance_m(end)
    if d <= slack * max_hop_m(radio, rate, start.alt, end.alt):
        return []
    n = max(1, math.ceil(d / hop) - 1)
    off = [(k - (parallel - 1) / 2) * spread_m for k in range(parallel)]
    return [[_toward(s, e, (i + 1) / (n + 1), o) for o in off] for i in range(n)]


def plan(positions: dict[bytes, Position], heard: dict[tuple[bytes, bytes], float], homes,
         radio: Radio, rate: float, *, relay_alt: float, parallel: int = 2,
         distrusted=frozenset()) -> dict[bytes, list]:
    """For every node cut off from home: the relay slots that would reconnect it, from the nearest
    connected node with a known position.  Connected nodes get nothing.  A node in ``distrusted``
    (a position claim :mod:`plausibility` flagged) is neither predicted from, anchored on, nor
    planned toward -- only what was heard of it counts."""
    usable = links(positions, heard, radio, rate, distrusted=distrusted)
    ok = reachable(usable, homes)
    anchors = [n for n in ok if n in positions and n not in distrusted]
    out = {}
    for n, p in positions.items():
        if n in ok or n in distrusted or not anchors:
            continue
        a = min(anchors, key=lambda m: positions[m].distance_m(p))
        out[n] = relay_chain(positions[a], p, radio, rate, relay_alt=relay_alt, parallel=parallel)
    return out


__all__ = ["FADE_DB", "Link", "horizon_m", "links", "max_hop_m", "plan", "predicted_snr_db", "reachable",
           "relay_chain"]
