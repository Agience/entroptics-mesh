"""Node runtime measurements (emu.node_run): delivery, latency and per-node CPU per slot.

    chain    origin -> relay -> far end, 9 km hops, 20 dBm, positions announced, 1.2 Msps sub-bands
             known at every node (state after first contact); one 300-B message (k = 1).
    switch   two nodes 9 km apart; message 1 on the sender's 1.2 Msps sub-band, then the sender
             switches to full width (2.4 Msps) and sends message 2: the receiver's fast read misses,
             blind acquisition recovers it and keeps the new geometry.

Usage: PYTHONPATH=src python research/scenarios/node_runtime.py [seeds]  ->  stdout (node_runtime.txt)
"""
from __future__ import annotations

import math
import sys

import numpy as np

from entroptics_mesh import phy
from entroptics_mesh.adapt import Radio
from entroptics_mesh.emu.air import Air, Radio as AirRadio
from entroptics_mesh.emu.node_run import NodeRun
from entroptics_mesh.neighbors import Position
from entroptics_mesh.node import Node
from entroptics_mesh.providers import NullCrypto, Providers, replace

RX = 2.4e6
RADIO = Radio(min_rate=1.2e6, max_rate=RX, center_hz=915e6, max_power_dbm=20.0, noise_dbm=-100.0)
SUB = [phy.Known(64, 0.5, 0.25), phy.Known(64, 0.5, -0.25)]


class KeyOnly(NullCrypto):
    name = "key-only"

    def waveform_key(self, context):
        return 1234


class Switch:
    name = "switch"
    wide = False

    def plan(self, intent, default, table):
        return replace(default, rate=RX, center=0.0) if self.wide else default


def mesh(xs, seed, agility=None):
    air = Air(rate=RX, seed=seed)
    nodes = []
    for i, x in enumerate(xs):
        air.add(AirRadio(position=np.array([x, 0.0, 100.0]), tx_power_dbm=20.0, noise_dbm=-100.0))
        p = Providers(crypto=KeyOnly(), agility=agility) if (agility and i == 0) else Providers(crypto=KeyOnly())
        nodes.append(Node(bytes([65 + i]) * 8, RADIO, slot_s=0.0, providers=p, seed=100 * seed + i,
                          position=Position(0.0, math.degrees(x / 6_371_000.0), 100.0), blind_scaled=False))
    for a in nodes:
        for b in nodes:
            if a is not b:
                a.on_announcement(b.announcement(0.0), b.node_id, 0.0)
        for g in SUB:
            a.learn(g)
    return air, nodes


def cpu(run):
    return " ".join(f"{c['mean'] * 1e3:.0f}/{c['max'] * 1e3:.0f}" for c in run.cpu_per_slot())


def chain(seed):
    air, (a, b, c) = mesh([0, 9000, 18000], seed)
    run = NodeRun(air, [a, b, c])
    msg = bytes(np.random.default_rng(seed).integers(0, 256, 300, dtype=np.uint8))
    a.send(msg, c.node_id, 0.0)
    run.run(80, until=lambda: bool(c.inbox) and not b.queue and not a.queue)
    ok = [m.payload for m in c.inbox] == [msg]
    slot = c.inbox[0].t / run.slot_s if c.inbox else float("nan")
    print(f"chain  seed {seed}: delivered {ok}  slot {slot:.0f} = {slot * run.slot_s * 1e3:.0f} ms  "
          f"bursts {run.bursts} (a {a.stats.bursts}, b {b.stats.bursts})  acked {a.stats.acked}  "
          f"fast {sum(n.stats.fast_hits for n in (a, b, c))}/{sum(n.stats.fast_reads for n in (a, b, c))}  "
          f"cpu ms/slot mean/max per node {cpu(run)}  slot {run.slot_s * 1e3:.2f} ms", flush=True)
    return ok, slot * run.slot_s


def switch(seed):
    sw = Switch()
    air, (a, b) = mesh([0, 9000], seed, agility=sw)
    run = NodeRun(air, [a, b])
    m1, m2 = (bytes(np.random.default_rng(seed + k).integers(0, 256, 150, dtype=np.uint8)) for k in (10, 20))
    a.send(m1, b.node_id, 0.0)
    run.run(40, until=lambda: len(b.inbox) >= 1 and not a.queue)
    sw.wide = True
    t0 = run.now()
    b0 = b.stats.blind_s
    a.send(m2, b.node_id, t0)
    run.run(40, until=lambda: len(b.inbox) >= 2)
    ok1 = len(b.inbox) >= 1 and b.inbox[0].payload == m1
    ok2 = len(b.inbox) >= 2 and b.inbox[1].payload == m2
    lat1 = b.inbox[0].t * 1e3 if ok1 else float("nan")
    lat2 = (b.inbox[1].t - t0) * 1e3 if ok2 else float("nan")
    print(f"switch seed {seed}: msg1 {ok1} {lat1:.0f} ms  msg2 (after switch) {ok2} {lat2:.0f} ms  "
          f"blind {b.stats.blind_hits}/{b.stats.blind_tries} in {b.stats.blind_s - b0:.2f} s  "
          f"rescans {b.stats.rescans}  geoms {[(g.how, round(g.known.scale, 4)) for g in b.geoms]}  "
          f"cpu ms/slot mean/max {cpu(run)}", flush=True)
    return ok1 and ok2, lat2


if __name__ == "__main__":
    seeds = range(1, 1 + (int(sys.argv[1]) if len(sys.argv) > 1 else 6))
    ch = [chain(s) for s in seeds]
    sw = [switch(s) for s in seeds]
    print(f"chain delivered {sum(o for o, _ in ch)}/{len(ch)}, latency median "
          f"{np.nanmedian([l for o, l in ch if o]) * 1e3:.0f} ms")
    print(f"switch delivered {sum(o for o, _ in sw)}/{len(sw)}, msg2 latency median "
          f"{np.nanmedian([l for o, l in sw if o]):.0f} ms")
