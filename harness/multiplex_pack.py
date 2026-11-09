"""
Sub-band multiplexing CAPACITY: how many concurrent FPV streams pack into the 2.4 MHz band?

Sweeps N concurrent sources, each pinned to one of N disjoint sub-bands tiling the band (width =
2.4/N MHz), all -> base, and counts how many deliver from base's single shared capture. This is the
direct throughput-capacity answer (the surviving aggregate lever). The air can carry every disjoint
sub-band at once (subband_run: disjoint cost nothing, half-overlap collides) -- so the ceiling is the
total band and the base decode compute, not the mesh. Comfortable direct range (no relays) to
isolate the multiplexing question.
"""
from __future__ import annotations

import numpy as np

from multiplex_flight import AIR_TX, BASE, RADIO, Band, KeyOnly, _east_m, _pos

from entroptics_mesh import phy
from entroptics_mesh.emu.air import Air, Radio as AirRadio
from entroptics_mesh.emu.node_run import NodeRun
from entroptics_mesh.node import Node
from entroptics_mesh.providers import Providers
from entroptics_mesh.routing import HOME

AIR_RATE = 2.4e6


def pack(n: int, km: float = 8.0, seed: int = 1) -> dict:
    width = AIR_RATE / n
    centers = [(k - (n - 1) / 2) * width for k in range(n)]
    geoms = [phy.Known(64, width / AIR_RATE, c / AIR_RATE) for c in centers]
    air = Air(freq_hz=915e6, rate=AIR_RATE, seed=seed)
    nodes, msgs = [], {}
    # base (gateway) first
    air.add(AirRadio(position=np.array([_east_m(BASE), 0.0, BASE.alt]), tx_power_dbm=AIR_TX, noise_dbm=-100.0))
    nodes.append(Node(b"BASE0000", RADIO, slot_s=0.0, providers=Providers(crypto=KeyOnly()), seed=100,
                      position=BASE, blind_scaled=False, families=("differential",), accepts={HOME}))
    for k in range(n):
        p = _pos(km * 1000.0 - 200.0 * k)
        air.add(AirRadio(position=np.array([_east_m(p), 0.0, p.alt]), tx_power_dbm=AIR_TX, noise_dbm=-100.0))
        nodes.append(Node(b"FPV%05d" % k, RADIO, slot_s=0.0,
                          providers=Providers(crypto=KeyOnly(), agility=Band(centers[k], rate=width)),
                          seed=200 + k, position=p, blind_scaled=False, families=("differential",)))
    for a in nodes:
        for b in nodes:
            if a is not b:
                a.on_announcement(b.announcement(0.0), b.node_id, 0.0)
        for g in geoms:
            a.learn(g)
    base = nodes[0]
    for k in range(n):
        m = bytes(np.random.default_rng(seed + k).integers(0, 256, 240, dtype=np.uint8))
        msgs[m] = k
        nodes[1 + k].send(m, HOME, 0.0)
    run = NodeRun(air, nodes, listen=True)
    run.run(120, until=lambda: len({bytes(x.payload) for x in base.inbox} & set(msgs)) >= n)
    got = {bytes(x.payload) for x in base.inbox} & set(msgs)
    return {"n": n, "width_MHz": width / 1e6, "delivered": len(got), "bursts": run.bursts}


if __name__ == "__main__":
    print("sub-band multiplexing capacity: N concurrent FPV streams in 2.4 MHz, each on a disjoint sub-band")
    print(f"{'streams':>7} {'sub-band':>9} | {'delivered':>9} {'bursts':>7}")
    for n in (2, 3, 4):
        r = pack(n)
        print(f"{r['n']:7d} {r['width_MHz']:7.2f}MHz | {r['delivered']:2d}/{r['n']:<6d} {r['bursts']:7d}", flush=True)
    print("\n=> every disjoint sub-band delivers from one capture; capacity ceiling = total band / "
          "per-stream width, then base decode compute (the FPV cluster math).")
