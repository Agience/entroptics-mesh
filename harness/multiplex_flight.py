"""
Sub-band multiplexing in the fleet: two FPV drones stream to base at the same time, each on its own
disjoint sub-band, both read from base's single shared capture.

This is the aggregate-throughput lever that SURVIVED the MIMO probe (co-channel key-stacking was a
no-go; frequency-division is real and already in the stack -- emu/subband_run measured disjoint
sub-bands cost nothing). Here it is demonstrated with the real PHY in the flight harness: 2 concurrent
FPV sources -> 1 base, both delivered. The split is steered by a simple agility provider pinning each
sender to a 1.2 MHz sub-band (a legitimate provider use: it chooses width/centre). Entroptics-pure.
"""
from __future__ import annotations

import math

import numpy as np

from entroptics_mesh import phy
from entroptics_mesh.adapt import Radio
from entroptics_mesh.emu.air import Air, Radio as AirRadio
from entroptics_mesh.emu.node_run import NodeRun
from entroptics_mesh.neighbors import Position
from entroptics_mesh.node import Node
from entroptics_mesh.providers import NullAgility, NullCrypto, Providers, replace
from entroptics_mesh.routing import HOME

GAIN = 3.0
RADIO = Radio(min_rate=0.25e6, max_rate=2.4e6, center_hz=915e6, max_power_dbm=20.0,
              noise_dbm=-100.0, freq_hz=915e6, antenna_dbi=GAIN)
AIR_TX = 20.0 + 2 * GAIN
BASE = Position(45.0, -75.0, 100.0)
SUB_PLUS = phy.Known(64, 0.5, 0.25)        # +0.6 MHz sub-band
SUB_MINUS = phy.Known(64, 0.5, -0.25)      # -0.6 MHz sub-band
SUBS = [SUB_PLUS, SUB_MINUS]


class KeyOnly(NullCrypto):
    name = "key-only"

    def waveform_key(self, context):
        return 0xA17C0DE


class Band(NullAgility):
    """Pin this sender to a sub-band: a fixed width (rate) and centre offset (frequency-division)."""
    name = "band"

    def __init__(self, center_hz: float, rate: float = 1.2e6):
        self.center = float(center_hz)
        self.rate = float(rate)

    def plan(self, intent, default, table, ctx=None):
        return replace(default, rate=self.rate, center=self.center)   # narrow -> refit keeps the mode


def _east_m(p: Position) -> float:
    return (p.lon - BASE.lon) * math.pi / 180 * 6_371_000.0 * math.cos(math.radians(BASE.lat))


def _pos(east_m: float) -> Position:
    return Position(BASE.lat, BASE.lon + math.degrees(east_m / (6_371_000.0 * math.cos(math.radians(BASE.lat)))), 100.0)


def run(km: float = 8.0, seed: int = 1):
    # base (gateway) + two FPV sources a hop out, each pinned to its own sub-band.
    specs = [(b"BASE0000", BASE, None),
             (b"LEAD0000", _pos(km * 1000.0), Band(+0.6e6)),
             (b"FOLW0000", _pos(km * 1000.0 - 800.0), Band(-0.6e6))]
    air = Air(freq_hz=915e6, rate=2.4e6, seed=seed)
    nodes = []
    for i, (nid, p, agility) in enumerate(specs):
        air.add(AirRadio(position=np.array([_east_m(p), 0.0, p.alt]), tx_power_dbm=AIR_TX, noise_dbm=-100.0))
        prov = Providers(crypto=KeyOnly(), agility=agility) if agility else Providers(crypto=KeyOnly())
        nodes.append(Node(nid, RADIO, slot_s=0.0, providers=prov, seed=100 + i, position=p,
                          blind_scaled=False, families=("differential",),
                          accepts=({HOME} if nid == b"BASE0000" else frozenset())))
    for a in nodes:
        for b in nodes:
            if a is not b:
                a.on_announcement(b.announcement(0.0), b.node_id, 0.0)
        for g in SUBS:
            a.learn(g)
    base, lead, follower = nodes
    m_lead = bytes(np.random.default_rng(seed).integers(0, 256, 300, dtype=np.uint8))
    m_follower = bytes(np.random.default_rng(seed + 99).integers(0, 256, 300, dtype=np.uint8))
    lead.send(m_lead, HOME, 0.0)
    follower.send(m_follower, HOME, 0.0)
    runner = NodeRun(air, nodes, listen=True)
    runner.run(80, until=lambda: {bytes(x.payload) for x in base.inbox} >= {m_lead, m_follower})
    got = {bytes(x.payload) for x in base.inbox}
    # the sub-band each source actually transmitted on (from the run log)
    bands = {i: sorted({round(p.center / 1e6, 2) for _, j, p in runner.log if j == i}) for i in (1, 2)}
    return {"lead_ok": m_lead in got, "follower_ok": m_follower in got, "both": got >= {m_lead, m_follower},
            "bursts": runner.bursts, "lead_band_MHz": bands[1], "follower_band_MHz": bands[2]}


if __name__ == "__main__":
    r = run()
    print("sub-band multiplexing flight: 2 FPV drones -> base, concurrent, disjoint sub-bands")
    print(f"  lead delivered : {r['lead_ok']}  (sub-band {r['lead_band_MHz']} MHz)")
    print(f"  follower delivered : {r['follower_ok']}  (sub-band {r['follower_band_MHz']} MHz)")
    print(f"  BOTH concurrent: {r['both']}   total bursts {r['bursts']}")
    print("\n=> frequency-division multiplexing delivers N streams from one capture (the surviving "
          "aggregate-throughput lever); scales to more drones on more disjoint sub-bands.")
    raise SystemExit(0 if r["both"] else 1)
