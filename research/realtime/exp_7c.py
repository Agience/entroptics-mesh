"""
exp_7c.py -- measure the 7c coordination prior: a keyed transmit schedule that lets the §3a local
(tracked) scan fire on EVERY burst of a sender, not only a stable-clock one.

The §3a mesh finding (port_results.md) is that in the default listen-before-talk mesh the sender
draws a fresh random start every burst, so the previous burst's lock is not a usable prior and the
lock cache degrades to the full scan.  7c replaces that random start with a KEYED one: the receiver,
running the same agility provider with the same key, predicts the sender's start and feeds it to the
local scan.  This script exercises the real node receive path (``Node.on_capture`` ->
``_predict_tracked`` -> ``phy.receive_known(tracked=...)`` -> ``_anchor_schedule``) over a sequence of
bursts whose start is chosen the way the node chooses it:

  * null provider  -> start drawn at random in the contention window (today's access), and
  * keyed schedule -> start drawn by ``schedules.KeyedSchedule``,

mapped to the capture's arrival delay through the emu air channel (``research/realtime/_bursts``).
Reports, per cell: the fraction of post-first bursts the LOCAL scan locked, and the node-path
per-burst time, keyed vs null.  A separate end-to-end run checks delivery parity, and a short check
confirms the keyed start sequence is PRNG-like (no repeating structure).

Run with ``OPENBLAS_NUM_THREADS=1 PYTHONHASHSEED=0`` and PYTHONPATH = mesh/src;rf/src.
"""
from __future__ import annotations

import os
import sys
import time

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(__file__)))

from entroptics_mesh import phy
from entroptics_mesh.adapt import Radio
from entroptics_mesh.link import read_frame
from entroptics_mesh.node import Node
from entroptics_mesh.providers import NullCrypto, PlanContext, Providers
from entroptics_mesh.schedules import KeyedSchedule

import _bursts

RX = 2.4e6
CENTER = 0.3e6
SENDER = b"A" * 8
KEY = _bursts.KEY
CONT = Node.CONTENTION / RX                       # the contention window (s), = 2048 rx samples
SLOT_S = 0.05
BASE_DELAY = 120.0                                # the constant propagation-like arrival offset


class KeyOnly(NullCrypto):
    name = "key-only"

    def waveform_key(self, context):
        return KEY


def _receiver(keyed: bool) -> Node:
    radio = Radio(min_rate=1.2e6, max_rate=RX, center_hz=915e6, max_power_dbm=20.0, noise_dbm=-100.0)
    agility = KeyedSchedule(KEY, epoch_s=SLOT_S) if keyed else None
    prov = Providers(crypto=KeyOnly(), agility=agility) if agility else Providers(crypto=KeyOnly())
    return Node(b"B" * 8, radio, slot_s=SLOT_S, providers=prov, blind_scaled=False, geometry_age=1e9)


def _sender_offset(keyed: bool, sched: KeyedSchedule, rng, slot: int) -> float:
    """The start offset (s) the sender uses this slot -- keyed schedule or today's random draw."""
    if keyed:
        return sched.transmit_offset(PlanContext(t=slot * SLOT_S, node_id=SENDER), window_s=CONT)
    return float(rng.uniform(0.0, CONT))


def run_cell(label: str, keyed: bool, n_bursts: int = 12, seed: int = 0):
    bkw, rx_rate, snr = _bursts.CELLS[label]
    iq0, plan = _bursts.make_burst(**bkw)
    known = phy.Known(plan.L, plan.rate / RX, CENTER / RX)
    b = _receiver(keyed)
    b.learn(known)                                 # first contact seeded (as after one blind lock)
    sched = KeyedSchedule(KEY, epoch_s=SLOT_S)
    rng = np.random.default_rng(1000 + seed)
    per_burst_s = []
    decoded = 0
    starts = []
    for slot in range(n_bursts):
        s = _sender_offset(keyed, sched, rng, slot)
        delay = BASE_DELAY + s * RX
        starts.append(delay)
        y, _ = _bursts.capture(iq0, plan, rx_rate=RX, snr_db=snr, delay=delay,
                               seed=seed, center_hz=CENTER)
        t0 = time.perf_counter()
        got = b.on_capture(y, slot * SLOT_S)
        per_burst_s.append(time.perf_counter() - t0)
        decoded += len(got)
    st = b.stats
    return {
        "decoded": decoded, "n": n_bursts,
        "local_reads": st.local_reads, "local_hits": st.local_hits,
        "coord_reads": st.coord_reads, "coord_hits": st.coord_hits,
        "first_ms": 1e3 * per_burst_s[0],
        "post_ms": 1e3 * float(np.mean(per_burst_s[1:])),
        "all_ms": 1e3 * float(np.mean(per_burst_s)),
        "starts": starts,
    }


def firing_and_speed():
    print("\n## 1. §3a firing + node-path speedup (default listen-before-talk mesh)\n")
    hdr = (f"{'cell':18s} {'prov':6s} {'dec':>4s} {'localHit/post':>13s} "
           f"{'coordHit':>9s} {'first ms':>9s} {'post ms':>8s}")
    print(hdr)
    rows = {}
    for label in _bursts.CELLS:
        for keyed in (False, True):
            # warm once (numba / scan caches), then measure
            run_cell(label, keyed, n_bursts=3)
            r = run_cell(label, keyed)
            rows[(label, keyed)] = r
            post = r["n"] - 1
            print(f"{label:18s} {'keyed' if keyed else 'null':6s} {r['decoded']:4d} "
                  f"{r['local_hits']:>6d}/{post:<6d} {r['coord_hits']:>9d} "
                  f"{r['first_ms']:9.1f} {r['post_ms']:8.1f}")
    print("\n   speedup (null post-first ms / keyed post-first ms):")
    for label in _bursts.CELLS:
        n = rows[(label, False)]["post_ms"]
        k = rows[(label, True)]["post_ms"]
        nh = rows[(label, False)]["local_hits"]
        kh = rows[(label, True)]["local_hits"]
        post = rows[(label, True)]["n"] - 1
        print(f"   {label:18s} null {n:7.1f} ms -> keyed {k:7.1f} ms   {n / k:5.2f}x   "
              f"local hits null {nh}/{post}, keyed {kh}/{post}")
    return rows


def delivery_parity():
    """End-to-end over NodeRun (the full stack, incl. node.start/commit with the keyed schedule):
    the same messages delivered WITH vs WITHOUT the provider."""
    import math

    from entroptics_mesh.emu.air import Air, Radio as AirRadio
    from entroptics_mesh.emu.node_run import NodeRun
    from entroptics_mesh.neighbors import Position

    def pos(x):
        return Position(0.0, math.degrees(x / 6_371_000.0), 100.0)

    def mesh(keyed: bool):
        air = Air(rate=RX, seed=7)
        radio = Radio(min_rate=1.2e6, max_rate=RX, center_hz=915e6, max_power_dbm=20.0, noise_dbm=-100.0)
        nodes = []
        for i, x in enumerate((0.0, 9000.0)):
            air.add(AirRadio(position=np.array([x, 0.0, 100.0]), tx_power_dbm=20.0, noise_dbm=-100.0))
            ag = KeyedSchedule(KEY) if keyed else None
            prov = Providers(crypto=KeyOnly(), agility=ag) if ag else Providers(crypto=KeyOnly())
            nodes.append(Node(bytes([65 + i]) * 8, radio, slot_s=0.0, position=pos(x),
                              providers=prov, seed=10 + i, blind_scaled=False))
        a, b = nodes
        b.learn(phy.Known(64, 1.0, 0.0))                 # full-width shout, first contact seeded
        run = NodeRun(air, nodes)
        if keyed:                                        # epoch == slot: fresh offset per burst
            for nd in nodes:
                nd.providers.agility.epoch_s = run.slot_s
                nd._sched_tx = nd.providers.agility.transmit_offset
                nd._sched_pred = nd.providers.agility.predict_offset
        return air, a, b, run

    print("\n## 2. Delivery parity (end-to-end NodeRun, with vs without the keyed provider)\n")
    out = {}
    msgs = [bytes(np.random.default_rng(s).integers(0, 256, 200, dtype=np.uint8)) for s in range(3)]
    for keyed in (False, True):
        air, a, b, run = mesh(keyed)
        for i, m in enumerate(msgs):
            a.send(m, b.node_id, run.now())
            run.run(60, until=lambda: len(b.inbox) >= i + 1)
        got = [m.payload for m in b.inbox]
        out[keyed] = got
        print(f"   {'keyed' if keyed else 'null ':6s}: delivered {len(got)}/{len(msgs)}  "
              f"coord_hits={b.stats.coord_hits} fast_hits={b.stats.fast_hits} "
              f"blind_tries={b.stats.blind_tries}")
    same = out[False] == out[True] == msgs
    print(f"\n   identical deliveries with and without the provider, and == sent: {same}")
    return same


def detectability():
    print("\n## 3. Detectability sanity (the keyed start sequence must look random, no repeating period)\n")
    s1 = KeyedSchedule(b"key-one", epoch_s=1.0)
    s2 = KeyedSchedule(b"key-two", epoch_s=1.0)
    seq1 = np.array([s1.unit(SENDER, e) for e in range(2000)])
    seq1b = np.array([s1.unit(SENDER, e) for e in range(2000)])    # reproducible for the keyed RX
    seq2 = np.array([s2.unit(SENDER, e) for e in range(2000)])
    # uniformity: mean ~0.5, and a chi-square over 10 bins is not extreme
    counts, _ = np.histogram(seq1, bins=10, range=(0, 1))
    chi2 = float(((counts - 200.0) ** 2 / 200.0).sum())           # 9 dof; ~[2, 22] is unremarkable
    # autocorrelation at lags 1..40 (a period would spike one); report the max |r|
    x = seq1 - seq1.mean()
    ac = np.array([float(np.dot(x[:-k], x[k:]) / np.dot(x, x)) for k in range(1, 41)])
    reproducible = bool(np.array_equal(seq1, seq1b))
    differs = float(np.mean(np.abs(seq1 - seq2)))
    print(f"   reproducible for the keyed receiver (same key -> same sequence): {reproducible}")
    print(f"   differs per key (mean |seq(key1) - seq(key2)|): {differs:.3f}  (~0.33 for independent U[0,1))")
    print(f"   uniform over the window: mean {seq1.mean():.3f}, chi2(9 dof, 10 bins) {chi2:.1f}")
    print(f"   no repeating period: max |autocorr| over lags 1..40 = {np.max(np.abs(ac)):.3f}  "
          f"(= {1.96 / np.sqrt(len(seq1)):.3f} is the 95% white-noise band)")
    return reproducible, chi2, float(np.max(np.abs(ac)))


if __name__ == "__main__":
    firing_and_speed()
    delivery_parity()
    detectability()
