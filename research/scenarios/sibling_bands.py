"""Sibling relays and sub-bands, on the node runtime: does keeping off a sibling's band help?

    origin (0, 0) -> relays A (9 km, +1 km) and B (9 km, -1 km) -> far end (18 km, 0); 20 dBm,
    positions announced, 1.2 Msps sub-bands known at every node (state after first contact).
    One 2400-B message (k = 6).  Arms:
      avoid   the node avoids every band it heard the message on (node._heard_bands -> adapt avoid=)
      plain   only the band the message came in on is avoided (the rule before 2026-10-16)

Usage: PYTHONPATH=src python research/scenarios/sibling_bands.py <arm> <seed>
"""
from __future__ import annotations

import math
import sys

import numpy as np

from entroptics_mesh import adapt, phy
from entroptics_mesh.emu.air import Air, Radio as AirRadio
from entroptics_mesh.emu.node_run import NodeRun
from entroptics_mesh.neighbors import Position
from entroptics_mesh.node import Node
from entroptics_mesh.providers import NullCrypto, Providers

RX = 2.4e6
RADIO = adapt.Radio(min_rate=1.2e6, max_rate=RX, center_hz=915e6, max_power_dbm=20.0, noise_dbm=-100.0)
SUB = [phy.Known(64, 0.5, 0.25), phy.Known(64, 0.5, -0.25)]
M = 111_000.0                                     # metres per degree, close enough here


class KeyOnly(NullCrypto):
    name = "key-only"

    def waveform_key(self, context):
        return 1234


def main(arm: str, seed: int) -> None:
    if arm == "plain":                            # the old rule: drop every band but the heard one
        real = adapt.default_plan
        adapt.default_plan = lambda *a, avoid=(), **k: real(*a, **k)
    xy = [(0, 0), (9000, 1000), (9000, -1000), (18000, 0)]
    air = Air(rate=RX, seed=seed)
    nodes = []
    for i, (x, y) in enumerate(xy):
        air.add(AirRadio(position=np.array([x, y, 100.0]), tx_power_dbm=20.0, noise_dbm=-100.0))
        nodes.append(Node(bytes([65 + i]) * 8, RADIO, slot_s=0.0, providers=Providers(crypto=KeyOnly()),
                          seed=100 * seed + i, position=Position(y / M, x / M, 100.0), blind_scaled=False))
    for a in nodes:
        for b in nodes:
            if a is not b:
                a.on_announcement(b.announcement(0.0), b.node_id, 0.0)
        for g in SUB:
            a.learn(g)
    o, ra, rb, far = nodes
    run = NodeRun(air, nodes)
    msg = bytes(np.random.default_rng(seed).integers(0, 256, 2400, dtype=np.uint8))
    o.send(msg, far.node_id, 0.0)
    run.run(200, until=lambda: bool(far.inbox) and not any(n.queue for n in nodes))
    ok = [m.payload for m in far.inbox] == [msg]
    slot = far.inbox[0].t / run.slot_s if far.inbox else float("nan")
    print(f"{arm:5s} seed {seed}: delivered {ok} slot {slot:.0f} bursts {run.bursts} "
          f"(o {o.stats.bursts} a {ra.stats.bursts} b {rb.stats.bursts}) "
          f"cancelled {ra.stats.cancelled + rb.stats.cancelled}", flush=True)


if __name__ == "__main__":
    main(sys.argv[1], int(sys.argv[2]))
