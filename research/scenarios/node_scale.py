"""Node runtime at SCALE: an N-node chain (9 km hops), end-to-end delivery, per-node CPU/slot and
total bursts. Confirms the runtime (reply-free flooding + duplicate suppression + relay budget +
bounded memory) scales with node count -- delivery holds, bursts stay ~O(N) (no flood storm), and
per-node CPU/slot stays bounded (not growing with N). Reuses node_runtime's mesh() pattern.

Usage: PYTHONPATH=src;<rf>/src python research/scenarios/node_scale.py [N ...]   (default 4 6 8)
"""
from __future__ import annotations

import math
import sys
import time

import numpy as np

from entroptics_mesh import phy
from entroptics_mesh.adapt import Radio
from entroptics_mesh.emu.air import Air, Radio as AirRadio
from entroptics_mesh.emu.node_run import NodeRun
from entroptics_mesh.neighbors import Position
from entroptics_mesh.node import Node
from entroptics_mesh.providers import NullCrypto, Providers

RX = 2.4e6
RADIO = Radio(min_rate=1.2e6, max_rate=RX, center_hz=915e6, max_power_dbm=20.0, noise_dbm=-100.0)
SUB = [phy.Known(64, 0.5, 0.25), phy.Known(64, 0.5, -0.25)]


class KeyOnly(NullCrypto):
    name = "key-only"

    def waveform_key(self, context):
        return 1234


def mesh(xs, seed):
    air = Air(rate=RX, seed=seed)
    nodes = []
    for i, x in enumerate(xs):
        air.add(AirRadio(position=np.array([x, 0.0, 100.0]), tx_power_dbm=20.0, noise_dbm=-100.0))
        nid = b"%08d" % i                                  # unique 8-byte id for any N
        nodes.append(Node(nid, RADIO, slot_s=0.0, providers=Providers(crypto=KeyOnly()),
                          seed=100 * seed + i,
                          position=Position(0.0, math.degrees(x / 6_371_000.0), 100.0),
                          blind_scaled=False))
    for a in nodes:
        for b in nodes:
            if a is not b:
                a.on_announcement(b.announcement(0.0), b.node_id, 0.0)
        for g in SUB:
            a.learn(g)
    return air, nodes


def scale(N, seed=1, hops=None):
    xs = [i * 9000 for i in range(N)]
    air, nodes = mesh(xs, seed)
    run = NodeRun(air, nodes)
    msg = bytes(np.random.default_rng(seed).integers(0, 256, 300, dtype=np.uint8))
    # hops=None uses the node default (8 radio hops); the 9 km spacing is under the ~18 km radio
    # reach so each radio hop skips ~2 nodes -> a chain of N nodes is ~(N-1)/2 radio hops.  Raise
    # hops for a longer single chain (a real mesh is not a single chain; see the capacity model).
    nodes[0].send(msg, nodes[-1].node_id, 0.0, hops=hops)
    t = time.perf_counter()
    run.run(60 * N, until=lambda: bool(nodes[-1].inbox))
    wall = time.perf_counter() - t
    ok = [m.payload for m in nodes[-1].inbox] == [msg]
    slot = nodes[-1].inbox[0].t / run.slot_s if nodes[-1].inbox else float("nan")
    cpus = [c["max"] * 1e3 for c in run.cpu_per_slot()]
    # Per-node memory in the node runtime is bounded by AGE, not by N: the relay symbol-dedup set
    # (node.seen), per-message tracking (_touched) and hold state (held) are all pruned by _forget
    # to memory_age.  Report the peak over all nodes of each -- it should stay ~flat as N grows (a
    # longer chain is more hops, not more per-node state): the no-storm / bounded-memory property.
    # (node.router._seen is the TUNNEL/IP-path flood dedup -- router.accept is not called in the
    # node relay path, so it is 0 here by design; it is pruned by _expire to seen_age separately.)
    relay = max((len(n.seen) for n in nodes), default=0)
    track = max((len(n._touched) for n in nodes), default=0)
    held = max((len(n.held) for n in nodes), default=0)
    print(f"N={N:2d}: delivered {ok}  {slot:5.0f} slots = {slot * run.slot_s * 1e3:6.0f} ms over {N-1} hops  "
          f"bursts {run.bursts:4d} (~{run.bursts / N:.1f}/node)  cpu/slot max {max(cpus) if cpus else 0:.0f} ms  "
          f"mem(peak): relay {relay} track {track} held {held}  wall {wall:.1f}s", flush=True)
    return ok, run.bursts / N, relay, track, held


if __name__ == "__main__":
    Ns = [int(x) for x in (sys.argv[1:] or ["4", "6", "8"])]
    rows = [scale(N) for N in Ns]
    oks = [r[0] for r in rows]
    bpn = [round(r[1], 1) for r in rows]
    relay = [r[2] for r in rows]
    print(f"\nscale (N={Ns}): delivered {sum(oks)}/{len(rows)}")
    print(f"  bursts/node {bpn} (flat => O(N) flooding, no storm)")
    print(f"  relay-dedup cache peak {relay} (flat => per-node memory bounded by AGE not N)")
