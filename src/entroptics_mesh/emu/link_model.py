"""
link_model.py -- :class:`FleetRun` with the PHY replaced by a measured capture model.

The real emulation composes every burst as entroptics-rf IQ, mixes it on the air and reads it back
with the node's own receiver: faithful, and ~100-300x slower than real time.  The access layer --
when nodes send, on which band, how often, who relays -- needs many seeds and many variants, so
this run keeps every node's own logic (the same :class:`~entroptics_mesh.node.Node`: queue, plans,
relay rules, listen before talk, announcements, tunnel) and replaces only what the air does to a
burst, by a model measured on the real runs:

  * **A burst is read when its in-band SINR clears its mode's threshold.**  Signal: the sender's
    power less the path loss, over the noise in the burst's own band.  Interference: every other
    burst in the same slot, weighted by the fraction of its power that falls inside the band
    (a burst on a disjoint neighbouring sub-band still spills ``SPILL_DB`` into it -- measured
    ~-13 dB for the rectangular row window, ``research/showcase/README.md``), treated as noise.
    The threshold is the mode's 1%-PER point (``adapt.MODES``); around it a logistic of width
    ``WIDTH_DB`` centred ``OFFSET_DB`` from it.  Calibrated on the recorded fleet showcase
    (``research/showcase/fleet_showcase.json``, 671 bursts x 11 receivers, 2026-10-17, on the
    captures the receiver's gate let through): read fraction 0.00 at margins < -6 dB, 0.01 at
    [-6, -3), 0.58 at [-3, 0), 0.91 at [0, 3), 0.97-1.00 from 3 dB.  The fit (centre -1.5 dB,
    width 0.8 dB) gives 0.00 / 0.03 / 0.61 / 0.87 / 0.99.
  * **Bursts overlap when they share the slot** -- the frames are long against the contention
    window; a node that sends does not hear (half duplex).
  * **A listen hears the full-band power** of every burst that has reached the node by its start
    (propagation included; a burst arriving inside the listen counts for the part it covers),
    over the node's noise -- what ``Node.busy`` reads on the IQ.
  * The SNR a node records for a sender is the true in-band SNR (the real read is within ~0.3 dB,
    ``phy.burst_snr``).

What it leaves out: capture effects beyond SINR (the real receiver can read the stronger of two
bursts on half-overlapping bands less well than SINR says -- measured 0/6 at half overlap), the
level gate, blind acquisition, and CPU.  Its numbers rank access variants; the real run confirms
the chosen one (``research/access``).
"""
from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np

from .. import adapt
from ..neighbors import Measured
from ..propagation import C_LIGHT
from .fleet_run import FleetRun, _Host

SPILL_DB = -13.0            # a disjoint neighbouring sub-band's power inside a burst's band
OFFSET_DB = -1.5            # read probability 1/2 at this margin over the mode's threshold
WIDTH_DB = 0.8              # logistic width

_THRESHOLD = {(m.family, int(m.M), m.rate): m.threshold_db for m in adapt.MODES}


def read_probability(margin_db: float) -> float:
    """Probability a burst is read at ``margin_db`` of in-band SINR over its mode's threshold."""
    return 1.0 / (1.0 + math.exp(-(margin_db - OFFSET_DB) / WIDTH_DB))


class _ModelHost(_Host):
    """The node calls of :class:`FleetRun`'s host, with a symbol on the air instead of IQ (the
    record's ``iq`` field carries the Symbol)."""

    def _emit(self, nd, t, heard, listened):
        if listened and heard is not None and nd.busy_db(heard):
            nd.defer(t)
            return None
        return nd.next_symbol(t)

    def _hear(self, nd, frames, t):
        nd.stats.captures += 1
        taken = []
        for sym, snr_db, center_hz, scale in frames:
            nd.stats.frames += 1
            nd._know(sym.header.origin)
            who = nd._transmitter(sym)
            if who is not None and who != nd.node_id:
                nd._record(who, Measured(t, scale, 64, 64, 2, 0.0, float(snr_db), float(snr_db)))
            if nd._take(sym, center_hz, t):
                taken.append(sym)
        return taken


@dataclass
class ModelRun(FleetRun):
    """A :class:`FleetRun` over the capture model.  Same calls, same record; ``air`` supplies the
    positions, powers and noise only (its IQ is never used).  Runs in-process."""
    seed: int = 0

    def __post_init__(self):
        self.workers = False
        super().__post_init__()
        self._local = [_ModelHost({i: self.nodes[i] for i in grp}) for grp in self.groups]
        self.rng = np.random.default_rng(self.seed)
        for nd in self.nodes:
            nd.floor_db = 0.0                    # the model's levels are dB over each node's noise

    def _snr(self, a: int, b: int) -> float:
        return self.air.snr_db(a, b)

    def _delay(self, a: int, b: int) -> float:
        return float(np.linalg.norm(self.air.radios[a].position - self.air.radios[b].position)) / C_LIGHT

    def step(self, t: float, orders=(), claims=None) -> dict:
        claims = claims or {}
        G = range(len(self.groups))
        alive = [[i for i in self.groups[g] if self.alive(i)] for g in G]
        calls = [(t, alive[g], {i: c for i, c in claims.items() if self.owner[i] == g},
                  [o for o in orders if self.owner[o[0]] == g]) for g in G]
        on_air = []                               # (node, start s, Symbol, plan)
        deferred = []
        if not self.listen:
            for grp in self._call_all([("bursts",) + c for c in calls]):
                for b in grp:
                    on_air.append((b["i"], 0.0, b))
        else:
            starts = sorted((s, i) for grp in self._call_all([("starts",) + c for c in calls])
                            for i, s in grp.items())
            for s, i in starts:
                w = self.nodes[i].listen_s
                p = 0.0
                for j, sj, b in on_air:
                    arrive = sj + self._delay(j, i)
                    cover = min(max((s - arrive) / w, 0.0), 1.0) if w > 0 else float(arrive < s)
                    p += cover * 10.0 ** ((self._snr(j, i) + b["plan"].power_db) / 10.0)
                b = self._call(self.owner[i], "commit", t, i, 10.0 * math.log10(1.0 + p))
                if b is None:
                    deferred.append(i)
                else:
                    on_air.append((i, s, b))
        txs = []
        for i, s, b in on_air:
            p = b["plan"]
            self.bursts += 1
            txs.append(dict(node=i, rate=float(p.rate), center=float(p.center), power_db=float(p.power_db),
                            family=p.family, M=int(p.M), code=p.code_rate, origin=b["origin"], seq=b["seq"],
                            to=b["to"], hops=b["hops"], sid=b["sid"], k=b["k"], own=b["own"],
                            cpu=round(b["cpu"], 4), start=round(s, 7)))
        senders = {i for i, _, _ in on_air}
        frames = [{} for _ in G]
        full = self.air.rate
        for r in range(len(self.nodes)):
            if r in senders or not self.alive(r):
                continue
            got = []
            for i, s, b in on_air:
                p = b["plan"]
                lo, hi = p.center - p.rate / 2, p.center + p.rate / 2
                n_in = p.rate / full                      # noise in the burst's band / full-band noise
                sig = 10.0 ** ((self._snr(i, r) + p.power_db) / 10.0)
                inter = 0.0
                for j, _, o in on_air:
                    if j == i:
                        continue
                    q = o["plan"]
                    ov = max(0.0, min(hi, q.center + q.rate / 2) - max(lo, q.center - q.rate / 2)) / q.rate
                    pj = 10.0 ** ((self._snr(j, r) + q.power_db) / 10.0)
                    inter += pj * (ov if ov > 0 else 10.0 ** (SPILL_DB / 10.0))
                sinr = 10.0 * math.log10(sig / (n_in + inter))
                thr = _THRESHOLD.get((p.family, int(p.M), p.code_rate), 4.0)
                if self.rng.random() < read_probability(sinr - thr):
                    got.append((b["iq"], 10.0 * math.log10(sig / n_in), p.center, p.rate / full))
            if got:
                frames[self.owner[r]][r] = got
        reps = self._call_all([("receive", t, alive[g], frames[g]) for g in G])
        nodes = {}
        for rep in reps:
            nodes.update(rep)
        out = dict(t=t, tx=txs, nodes=nodes)
        if deferred:
            out["deferred"] = deferred
        return out


__all__ = ["ModelRun", "read_probability", "SPILL_DB", "OFFSET_DB", "WIDTH_DB"]
