"""Node loss on the node runtime with listen before talk: relay budget x3 vs x2 (x1.5 for reference).

The node-loss family of ``node_loss.py`` / ``multihome.py`` (which ran on ``SubbandRun``, before the
access layer) re-run on the current runtime: every node a :class:`~entroptics_mesh.node.Node`
deciding from its own reads -- plans, sub-bands, listen before talk, interleaving, passive ack,
sibling share -- over ``FleetRun`` (real entroptics-rf IQ, ``real``) or the calibrated capture model
``ModelRun`` (``model``).  Only the relay policy's budget changes between arms
(``relay.Policy(fresh=B)``, sibling share and passive ack on).

Layout (20 dBm, 915 MHz, free space, noise -100 dBm over 2.4 MHz; at the default 9 km hops a link is
~9 dB full band, ~12 dB in a 1.2 MHz sub-band; siblings 2 km apart hear each other at ~22 dB):

    diamond     origin -> relays A, B (one hop, 2 km apart) -> far end (two hops)
    tri         origin -> relays A, B, C (one hop, 2 km apart) -> far end
    two_stage   origin -> {A, B} -> {C, D} -> far end (three hops)
    single      origin -> relay -> far end
    anycast     origin -> R1, R2 (one hop) -> gateways G1, G2 (two hops, 4 km apart, one backhaul
                wire); addressed to HOME (both gateways accept it); ``unicast`` addresses G1 only.

Links between nodes more than one stage apart (|dx| > 1.44 hops) are blocked (+40 dB, a ridge):
with today's PHY a 1.2 MHz burst at 18 km in free space is ~6 dB, above DBPSK 1/2's 4 dB threshold,
so without the block the far end reads the origin directly and no relay is needed.  The old runs
had no such direct path either (single_kill 0/10).  State at the start is the state after first
contact among the nodes that hear each other (announcements, the three warm sender geometries),
and every radio has heard its band's noise once (its floor).

Arms (``_alive`` suffix: the same layout, nobody lost):
    diamond_early   relay A lost from slot 1 (before it can rebuild the message)
    diamond_mid0    relay A lost the moment it has rebuilt the message (queued, nothing sent)
    diamond_mid     relay A lost right after its first relayed burst (its sibling has cancelled one)
    tri_kill2       relays A and B each lost the moment they have rebuilt the message
    two_stage       A (stage 1) and D (stage 2) each lost the moment they have rebuilt the message
    single_kill     control: the one relay lost the moment it has rebuilt the message (expect 0)
    anycast_kill    G1 lost when relaying starts (the first relay has rebuilt the message)
    unicast_kill    control: as anycast_kill, addressed to G1 (expect 0)

``msgs`` messages of ``size`` bytes are handed to the origin together in slot 0 (48 B -> k = 1, the
showcase's telemetry; 2400 B -> k = 6); origin redundancy the node default x1.5, hops 4.  ``hop``
sets the hop length (a longer hop: a marginal link).  Latency is from the slot the message was
handed over to the slot it was delivered in (slot granularity, as research/access).

    PYTHONPATH=src python research/scenarios/node_loss_listen.py one ARM BUDGET SIZE SEED model|real [HOP MSGS]
    PYTHONPATH=src python research/scenarios/node_loss_listen.py batch model|real SEEDS PROCS OUT.jsonl \\
        [--arms a,b] [--budgets 3,2,1.5] [--sizes 48,2400] [--seed0 1] [--hop 9000] [--msgs 1]
    PYTHONPATH=src python research/scenarios/node_loss_listen.py table OUT.jsonl [...]
"""
from __future__ import annotations

import json
import math
import sys
import time
from collections import defaultdict
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from entroptics_mesh import phy, relay  # noqa: E402
from entroptics_mesh.adapt import Radio  # noqa: E402
from entroptics_mesh.emu.air import Air, Radio as AirRadio  # noqa: E402
from entroptics_mesh.emu.fleet_run import FleetRun  # noqa: E402
from entroptics_mesh.emu.link_model import ModelRun  # noqa: E402
from entroptics_mesh.neighbors import Position  # noqa: E402
from entroptics_mesh.node import Node, Wire  # noqa: E402
from entroptics_mesh.providers import NullCrypto, Providers  # noqa: E402
from entroptics_mesh.routing import HOME  # noqa: E402

R_EARTH = 6_371_000.0
RX = 2.4e6
TX_DBM, NOISE_DBM = 20.0, -100.0
RADIO = Radio(min_rate=1.2e6, max_rate=RX, center_hz=915e6, max_power_dbm=TX_DBM, noise_dbm=NOISE_DBM)
WARM = [phy.Known(64, 1.0, 0.0), phy.Known(64, 0.5, 0.25), phy.Known(64, 0.5, -0.25)]
HOP, ALT = 9000.0, 100.0
BLOCK_DX, BLOCK_DB = 13000.0, 40.0          # at HOP; scales with the hop
MAX_SLOTS = 400

ARMS = ["diamond_alive", "diamond_early", "diamond_mid0", "diamond_mid","tri_alive", "tri_kill2",
        "two_stage_alive", "two_stage", "single_alive", "single_kill",
        "anycast_alive", "anycast_kill", "unicast_kill"]


class KeyOnly(NullCrypto):
    name = "key-only"

    def waveform_key(self, context):
        return 1234


class RidgeAir(Air):
    """Free space, except links more than one stage apart, which a ridge blocks (+BLOCK_DB)."""
    block_dx: float = BLOCK_DX

    def gain_db(self, a: int, b: int) -> float:
        g = super().gain_db(a, b)
        dx = abs(float(self.radios[a].position[0] - self.radios[b].position[0]))
        return g - (BLOCK_DB if dx > self.block_dx else 0.0)


def pos(x, y, z) -> Position:
    return Position(math.degrees(y / R_EARTH), math.degrees(x / R_EARTH), z)


def layout(arm: str, hop: float = HOP):
    """(positions, names, kills, destination kind).  kills: {index: 'early' | 'first_fwd' | 'relaying'}."""
    base = arm.removesuffix("_alive")
    alive = arm.endswith("_alive")
    if base.startswith("diamond"):
        xy = [(0, 0), (hop, 1000), (hop, -1000), (2 * hop, 0)]
        names = ["O", "A", "B", "F"]
        kills = {1: {"diamond_early": "early", "diamond_mid0": "rebuilt"}.get(base, "first_fwd")}
    elif base.startswith("tri"):
        xy = [(0, 0), (hop, 2000), (hop, 0), (hop, -2000), (2 * hop, 0)]
        names = ["O", "A", "B", "C", "F"]
        kills = {1: "rebuilt", 2: "rebuilt"}
    elif base.startswith("two_stage"):
        xy = [(0, 0), (hop, 1000), (hop, -1000), (2 * hop, 1000), (2 * hop, -1000), (3 * hop, 0)]
        names = ["O", "A", "B", "C", "D", "F"]
        kills = {1: "rebuilt", 4: "rebuilt"}
    elif base.startswith("single"):
        xy = [(0, 0), (hop, 0), (2 * hop, 0)]
        names = ["O", "A", "F"]
        kills = {1: "rebuilt"}
    elif base.startswith(("anycast", "unicast")):
        xy = [(0, 0), (hop, 1000), (hop, -1000), (2 * hop, 2000), (2 * hop, -2000)]
        names = ["O", "R1", "R2", "G1", "G2"]
        kills = {3: "relaying"}
    else:
        raise ValueError(arm)
    dest = "home" if base.startswith("anycast") else ("g1" if base.startswith("unicast") else "far")
    return xy, names, ({} if alive else kills), dest


def build(arm: str, seed: int, budget: float, hop: float = HOP):
    xy, names, kills, dest = layout(arm, hop)
    air = RidgeAir(rate=RX, seed=seed)
    air.block_dx = BLOCK_DX * hop / HOP
    wire = Wire()
    gws = {3, 4} if dest in ("home", "g1") else set()
    nodes = []
    for i, (x, y) in enumerate(xy):
        p = np.array([float(x), float(y), ALT])
        air.add(AirRadio(position=p, tx_power_dbm=TX_DBM, noise_dbm=NOISE_DBM))
        gw = i in gws
        nodes.append(Node(names[i].encode().ljust(8, b"_"), RADIO, slot_s=0.0,
                          providers=Providers(crypto=KeyOnly()), policy=relay.Policy(fresh=float(budget)),
                          accepts={HOME} if gw else frozenset(), backhaul=wire if gw else None,
                          position=pos(*p), seed=1000 * seed + i, blind_scaled=False, blind_every=1e9,
                          announce_every=None, hops=4))
    for i, a in enumerate(nodes):           # state after first contact, among those that hear each other
        for j, b in enumerate(nodes):
            if i != j and air.snr_db(j, i) > 0.0:
                a.on_announcement(b.announcement(0.0), b.node_id, 0.0)
        for g in WARM:
            a.learn(g)
    for i, a in enumerate(nodes):           # each radio has heard its band's noise (its floor)
        a.on_capture(air.capture(i, -1.0, 4096), -1.0)
    nodes[0].router._seq = 1000 * seed       # different messages (fountain seeds) per run
    return air, nodes, names, kills, dest


def run_one(arm: str, budget: float, size: int, seed: int, model: bool, hop: float = HOP,
            msgs: int = 1) -> dict:
    """One run; ``msgs`` messages are handed to the origin together in slot 0."""
    air, nodes, names, kills, dest = build(arm, seed, budget, hop)
    N = len(nodes)
    run = (ModelRun(air, nodes, [list(range(N))], seed=seed) if model
           else FleetRun(air, nodes, [list(range(N))], workers=False))
    rng = np.random.default_rng(seed)
    payloads = [bytes(rng.integers(0, 256, size, dtype=np.uint8)) for _ in range(msgs)]
    to = HOME if dest == "home" else (nodes[3].node_id if dest == "g1" else nodes[-1].node_id)
    takers = {"far": [N - 1], "home": [3, 4], "g1": [3]}[dest]
    t_wall = time.perf_counter()
    k = None
    got = {}                                  # payload index -> (slot, via)
    killed = {}
    relaying = False
    own = rel = 0
    slot = 0
    while slot < MAX_SLOTS:
        t = slot * run.slot_s
        if slot == 1:
            for i, how in kills.items():
                if how == "early":
                    run.dead.add(i)
                    killed[names[i]] = 1
        if slot == 0:                         # the runs are in-process: the origin's own call
            for m in payloads:
                nodes[0].send(m, to, t)
        r = run.step(t)
        for tx in r["tx"]:
            k = tx["k"] if k is None else k
            own += bool(tx["own"])
            rel += not tx["own"]
            i = tx["node"]
            if not tx["own"] and kills.get(i) == "first_fwd" and i not in run.dead:
                run.dead.add(i)
                killed[names[i]] = slot + 1
        for i, nr in r["nodes"].items():
            if nr.get("forward") and kills.get(i) == "rebuilt" and i not in run.dead:
                run.dead.add(i)
                killed[names[i]] = slot + 1
        if any(nr.get("forward") for nr in r["nodes"].values()):
            if not relaying:
                for i, how in kills.items():
                    if how == "relaying" and i not in run.dead:
                        run.dead.add(i)
                        killed[names[i]] = slot + 1
            relaying = True
        for g in takers:
            for m in nodes[g].inbox:
                if m.payload in payloads and payloads.index(m.payload) not in got:
                    got[payloads.index(m.payload)] = (slot, names[g])
        slot += 1
        busy = any(run.alive(i) and (nd.queue or nd.due is not None) for i, nd in enumerate(nodes))
        if not busy:
            break
    run.close()
    sl = [got[j][0] for j in sorted(got)]
    return dict(arm=arm, budget=budget, size=size, k=k, seed=seed, model=model, hop=hop, msgs=msgs,
                delivered=len(got), slot=sl, via=[got[j][1] for j in sorted(got)],
                latency_ms=[round(x * run.slot_s * 1e3, 2) for x in sl],
                slot_ms=round(run.slot_s * 1e3, 3), slots=slot, bursts=run.bursts, own=own, relay=rel,
                cancelled=sum(nd.stats.cancelled for nd in nodes), acked=sum(nd.stats.acked for nd in nodes),
                deferred=sum(nd.stats.deferred for nd in nodes), killed=killed,
                per_node={names[i]: nd.stats.bursts for i, nd in enumerate(nodes)},
                wall_s=round(time.perf_counter() - t_wall, 1))


def _one(args):
    try:
        return run_one(*args)
    except Exception as e:                    # report and keep the batch going
        import traceback
        return dict(arm=args[0], budget=args[1], size=args[2], seed=args[3], model=args[4], hop=args[5],
                    msgs=args[6], error=f"{e!r}", tb=traceback.format_exc())


def batch(model: bool, seeds: int, procs: int, out: Path, arms, budgets, sizes, seed0: int = 1,
          hop: float = HOP, msgs: int = 1):
    from multiprocessing import Pool
    jobs = [(a, b, s, sd, model, hop, msgs) for s in sizes for a in arms for b in budgets
            for sd in range(seed0, seed0 + seeds)]
    done = set()
    if out.exists():                          # resume: skip what is already recorded
        for line in out.read_text().splitlines():
            r = json.loads(line)
            if "error" not in r:
                done.add((r["arm"], float(r["budget"]), r["size"], r["seed"], float(r["hop"]), r["msgs"]))
    jobs = [j for j in jobs if (j[0], float(j[1]), j[2], j[3], float(j[5]), j[6]) not in done]
    # longest first (big messages, multi-relay layouts) so the pool drains evenly
    jobs.sort(key=lambda j: (-j[2], -len(layout(j[0])[0]), -j[1]))
    t0 = time.time()
    with Pool(procs) as pool, out.open("a") as f:
        for n, r in enumerate(pool.imap_unordered(_one, jobs), 1):
            f.write(json.dumps(r) + "\n")
            f.flush()
            if n % 20 == 0 or "error" in r:
                print(f"[{n}/{len(jobs)} {time.time() - t0:.0f}s] {r.get('arm')} x{r.get('budget')} "
                      f"{r.get('size')}B seed {r.get('seed')}: {r.get('error') or r.get('delivered')}", flush=True)


def _agg(rs):
    """(delivered, sent, latencies ms of delivered, slots of delivered, bursts)."""
    lat = np.array([x for r in rs for x in r["latency_ms"]], float)
    sl = np.array([x for r in rs for x in r["slot"]], float)
    return sum(r["delivered"] for r in rs), sum(r["msgs"] for r in rs), lat, sl, sum(r["bursts"] for r in rs)


def _q(a, q):
    return float(np.percentile(a, q)) if a.size else float("nan")


def _line(label, b, rs, extra=True):
    d, n, lat, sl, bursts = _agg(rs)
    s = (f"{label:16s} {'x%g' % b:>6s} {d:5d}/{n:<5d} "
         f"{_q(lat, 50):6.0f}/{_q(lat, 90):<6.0f}   {_q(sl, 50):5.0f}/{_q(sl, 90):<5.0f}  "
         f"{bursts / max(1, d):10.2f}  {bursts / len(rs):9.1f}")
    if extra:
        s += (f" {np.mean([r['relay'] for r in rs]):6.1f} {np.mean([r['cancelled'] for r in rs]):6.1f} "
              f"{np.mean([r['deferred'] for r in rs]):6.1f}")
    return s


def table(paths) -> str:
    rows = [json.loads(l) for p in paths for l in Path(p).read_text().splitlines() if l.strip()]
    errs = [r for r in rows if "error" in r]
    rows = [r for r in rows if "error" not in r]
    g = defaultdict(list)
    for r in rows:
        g[(r["model"], float(r["hop"]), r["msgs"], r["size"], r["arm"], float(r["budget"]))].append(r)
    out = []
    for scen in sorted({k[:4] for k in g}, key=lambda c: (not c[0], c[1], c[2], c[3])):
        model, hop, msgs, size = scen
        ks = [r["k"] for k_, rs in g.items() if k_[:4] == scen for r in rs if r["k"]]
        out.append(f"\n## {'capture model (ModelRun)' if model else 'real IQ (FleetRun)'}, hops {hop / 1e3:g} km, "
                   f"{msgs} x {size}-byte message{'s' if msgs > 1 else ''} (k = {max(ks) if ks else '?'})\n")
        out.append(f"{'arm':16s} {'budget':>6s} {'delivered':>11s} {'lat med/p90 ms':>15s} "
                   f"{'slots med/p90':>13s} {'bursts/deliv':>12s} {'bursts/run':>10s} {'relay':>6s} "
                   f"{'cancel':>6s} {'defer':>6s}")
        for arm in ARMS:
            for b in (3.0, 2.5, 2.0, 1.5):
                rs = g.get(scen + (arm, b))
                if rs:
                    out.append(_line(arm, b, rs))
        # roll-up: every arm with a loss (not the no-redundant-path controls), every all-alive control
        for label, pick in (("ALL LOSS ARMS", lambda a: not a.endswith("_alive") and a not in ("single_kill", "unicast_kill")),
                            ("ALL ALIVE", lambda a: a.endswith("_alive") and a != "single_alive")):
            for b in (3.0, 2.5, 2.0, 1.5):
                rs = [r for k_, v in g.items() if k_[:4] == scen and k_[5] == b and pick(k_[4]) for r in v]
                if rs:
                    out.append(_line(label, b, rs, extra=False))
    if errs:
        out.append(f"\n{len(errs)} runs failed: " + "; ".join(
            f"{e['arm']} x{e['budget']} {e['size']}B s{e['seed']}: {e['error']}" for e in errs[:10]))
    return "\n".join(out)


if __name__ == "__main__":
    a = sys.argv[1:]
    if a[0] == "one":
        print(json.dumps(run_one(a[1], float(a[2]), int(a[3]), int(a[4]), a[5] == "model",
                                 float(a[6]) if len(a) > 6 else HOP, int(a[7]) if len(a) > 7 else 1)))
    elif a[0] == "batch":
        opts = dict(zip(a[5::2], a[6::2]))
        batch(a[1] == "model", int(a[2]), int(a[3]), Path(a[4]),
              opts.get("--arms", ",".join(ARMS)).split(","),
              [float(x) for x in opts.get("--budgets", "3,2,1.5").split(",")],
              [int(x) for x in opts.get("--sizes", "48,2400").split(",")],
              int(opts.get("--seed0", "1")), float(opts.get("--hop", HOP)), int(opts.get("--msgs", "1")))
    elif a[0] == "table":
        print(table(a[1:]))
