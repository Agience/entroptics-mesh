"""Fleet showcase: a drone fleet flies out from a two-gateway base, with two relays trailing above it,
and sends telemetry and a tunnelled UDP flow home over several hops -- every burst real entroptics-rf
IQ on the emulated air, every node the mesh's own runtime (``node.Node``) deciding from its own reads.
Recorded slot by slot for the player (``build_player.py``).

Cast (12 nodes, 1 W, 915 MHz, free space, noise -100 dBm over 2.4 MHz):
    G1, G2   gateways at the base, 3 km apart, 30 m up: both accept HOME (anycast) and share a
             backhaul wire (the tunnel's ``Backhaul``: first copy wins, duplicates die on the wire).
    R1, R2   relays 2.4 km apart, 400 m up, trailing the fleet at ~half its distance from base.
    D1-D8    drones in a 2 x 4 formation (600 m x 400 m), 120 m up, flying +x at V m/s.

Every epoch (``EPOCH`` s of mission time; time jumps between epochs, positions hold within one):
    * every live node announces "I'm here" (position, velocity, band, whom it heard at what SNR),
      phased one per two slots;
    * every drone sends one 48-byte telemetry record to HOME;
    * D1 sends ``UDP_PER_EPOCH`` UDP datagrams (IPv4, 160-byte payload) to HOME through its tunnel;
    * the slots run until nothing is queued anywhere (or ``MAX_SLOTS``).
Events:
    * from epoch ``SPOOF_EPOCH`` D6's GPS is spoofed: its claimed position walks sideways
      ``SPOOF_STEP`` m per epoch (the claims alone pass the speed check); the air does not move.
    * in epoch ``LOSS_EPOCH`` relay R1 is lost mid-epoch, after its first burst of that epoch.

State at the start is the state after first contact, as the tests start (``tests/test_node.py``):
every node has heard every other's announcement once and holds the three sender geometries the
plans use (full width, and the two 1.2 Msps sub-bands).  Blind acquisition is tried at most once per
node (``blind_every``): with every geometry warm it only costs time.

Usage:
    PYTHONPATH=src python research/showcase/fleet_showcase.py [epochs] [workers]
      -> research/showcase/fleet_showcase.json   (then build_player.py -> fleet_showcase.html)
"""
from __future__ import annotations

import json
import math
import struct
import sys
import time
from pathlib import Path

import numpy as np

from entroptics_mesh import phy
from entroptics_mesh.adapt import Radio
from entroptics_mesh.emu.air import Air, Radio as AirRadio
from entroptics_mesh.emu.fleet_run import FleetRun
from entroptics_mesh.emu.link_model import ModelRun
from entroptics_mesh.neighbors import Position
from entroptics_mesh.node import Node
from entroptics_mesh.providers import NullCrypto, Providers
from entroptics_mesh.routing import HOME
from entroptics_mesh.tunnel import Backhaul, Tunnel, TunnelConfig

HERE = Path(__file__).resolve().parent
R_EARTH = 6_371_000.0
RX = 2.4e6
TX_DBM, NOISE_DBM = 20.0, -100.0
RADIO = Radio(min_rate=1.2e6, max_rate=RX, center_hz=915e6, max_power_dbm=TX_DBM, noise_dbm=NOISE_DBM)
WARM = [phy.Known(64, 1.0, 0.0), phy.Known(64, 0.5, 0.25), phy.Known(64, 0.5, -0.25)]

EPOCH = 40.0                      # s of mission time per epoch
V = 60.0                          # m/s, fleet ground speed (+x)
X0 = 3000.0                       # fleet lead's x at t = 0
RELAY_FRAC, RELAY_ALT = 0.5, 400.0
DRONE_ALT, GW_ALT = 120.0, 30.0
SPOOF_EPOCH, SPOOF_NODE, SPOOF_STEP = 2, "D6", 4000.0
LOSS_EPOCH, LOSS_NODE = 4, "R1"
UDP_PER_EPOCH = 2
TLM_BYTES, UDP_BYTES = 48, 160
MAX_SLOTS, MIN_SLOTS = 90, 30

NAMES = ["G1", "G2", "R1", "R2"] + [f"D{k}" for k in range(1, 9)]
ROLES = ["gateway"] * 2 + ["relay"] * 2 + ["drone"] * 8
FORMATION = [(-dx, dy) for dx in (0.0, 400.0, 800.0, 1200.0) for dy in (-300.0, 300.0)]


class KeyOnly(NullCrypto):
    """No sealing, a waveform key: the keyed aperture the fast read is measured on."""
    name = "key-only"

    def waveform_key(self, context):
        return 1234


def pos(x, y, z) -> Position:
    return Position(math.degrees(y / R_EARTH), math.degrees(x / R_EARTH), z)


def xyz(p) -> list:
    lat, lon, alt = p
    return [round(math.radians(lon) * R_EARTH, 1), round(math.radians(lat) * R_EARTH, 1), round(alt, 1)]


def truth(i: int, t: float) -> np.ndarray:
    lead = X0 + V * t
    name = NAMES[i]
    if name == "G1":
        return np.array([0.0, -1500.0, GW_ALT])
    if name == "G2":
        return np.array([0.0, 1500.0, GW_ALT])
    if name in ("R1", "R2"):
        return np.array([RELAY_FRAC * lead - 600.0, 1200.0 if name == "R1" else -1200.0, RELAY_ALT])
    dx, dy = FORMATION[i - 4]
    return np.array([lead + dx, dy, DRONE_ALT])


def claim(i: int, e: int, t: float) -> np.ndarray:
    p = truth(i, t)
    if NAMES[i] == SPOOF_NODE and e >= SPOOF_EPOCH:
        p = p + np.array([0.0, SPOOF_STEP * (e - SPOOF_EPOCH + 1), 0.0])
    return p


def velocity(i: int) -> tuple:
    r = ROLES[i]
    return (0.0, 0.0, 0.0) if r == "gateway" else ((V * RELAY_FRAC, 0.0, 0.0) if r == "relay" else (V, 0.0, 0.0))


def tlm(drone: int, epoch: int) -> bytes:
    return (b"T" + struct.pack(">BH", drone, epoch)).ljust(TLM_BYTES, b".")


def udp(epoch: int, k: int) -> bytes:
    payload = (b"U" + struct.pack(">HB", epoch, k)).ljust(UDP_BYTES, b"~")
    body = struct.pack(">HHHH", 5000, 6000, 8 + len(payload), 0) + payload
    hdr = struct.pack(">BBHHHBBH4s4s", 0x45, 0, 20 + len(body), 0, 0, 64, 17, 0,
                      bytes([10, 0, 0, 5]), bytes([192, 168, 1, 1]))
    return hdr + body


def build(seed: int, node_kw: dict | None = None):
    air = Air(rate=RX, seed=seed)
    bh = Backhaul()
    cfg = TunnelConfig(deadline=5.0, hops=4)
    nodes = []
    for i, name in enumerate(NAMES):
        p = truth(i, 0.0)
        air.add(AirRadio(position=p.copy(), tx_power_dbm=TX_DBM, noise_dbm=NOISE_DBM))
        gw = ROLES[i] == "gateway"
        nd = Node(name.encode().ljust(8, b"_"), RADIO, slot_s=0.0, providers=Providers(crypto=KeyOnly()),
                  accepts={HOME} if gw else frozenset(), position=pos(*p), velocity=velocity(i),
                  seed=1000 * seed + i, blind_scaled=False, blind_every=1e9, announce_every=1e9,
                  hops=4, **(node_kw or {}))
        nd.attach(Tunnel(nd.node_id, crypto=KeyOnly(), config=cfg, accepts={HOME} if gw else frozenset(),
                         backhaul=bh if gw else None))
        nodes.append(nd)
    for a in nodes:                                   # state after first contact
        for b in nodes:
            if a is not b:
                a.on_announcement(b.announcement(0.0), b.node_id, 0.0)
        for g in WARM:
            a.learn(g)
        a._next_announce = math.inf                   # the driver phases announcements per epoch
    return air, nodes


def _summarise(rec: dict, sent: dict) -> None:
    """Latency: the first delivery of each item sent so far, at either gateway."""
    first = {}
    for d in rec["deliveries"]:
        d.pop("latency_ms", None)
        d.pop("sent_slot", None)
        key = (("tlm", d["origin"], d["epoch"]) if d["kind"] == "tlm" else ("udp", "D1", d["epoch"], d["k"]))
        if key in sent and sent[key][0] is not None and key not in first:
            first[key] = d
            d["latency_ms"] = round((d["t"] - sent[key][0]) * 1e3, 2)
            d["sent_slot"] = sent[key][1]
    rec["sent"] = [dict(kind=k[0], origin=k[1], epoch=k[2], k=(k[3] if len(k) > 3 else None),
                        t=v[0], slot=v[1], delivered=k in first,
                        latency_ms=first[k]["latency_ms"] if k in first else None,
                        at=first[k]["at"] if k in first else None) for k, v in sent.items() if v[0] is not None]


def main(epochs: int = 7, workers: int = 6, seed: int = 1, out: Path | None = HERE / "fleet_showcase.json",
         *, listen: bool = True, model: bool = False, node_kw: dict | None = None, verbose: bool = True):
    """Run the showcase; returns the record.  ``listen``: listen before talk; ``model``: the capture
    model (``emu.link_model``) instead of real IQ; ``node_kw``: Node options (the access variant)."""
    air, nodes = build(seed, node_kw)
    N = len(nodes)
    ids = [nd.node_id.hex() for nd in nodes]
    name_of = {h: NAMES[i] for i, h in enumerate(ids)}
    name_of[HOME.hex()] = "HOME"
    drones = [i for i in range(N) if ROLES[i] == "drone"]
    # gateways share one wire, so they share one worker; the others spread over the rest
    rest = [i for i in range(N) if ROLES[i] != "gateway"]
    k = max(1, workers - 1)
    groups = [[0, 1]] + [rest[j::k] for j in range(k) if rest[j::k]]
    run = (ModelRun(air, nodes, [list(range(N))], listen=listen, seed=seed) if model
           else FleetRun(air, nodes, groups, workers=workers > 0, listen=listen))
    rng = np.random.default_rng(seed)
    rec = dict(scenario="fleet_showcase", seed=seed, slot_s=run.slot_s, listen=listen, model=model,
               node_kw={k: v for k, v in (node_kw or {}).items()}, epoch_s=EPOCH, V=V,
               tx_dbm=TX_DBM, noise_dbm=NOISE_DBM, rx_rate=RX, freq_hz=air.freq_hz,
               hop_snr_db=9.0, home=HOME.hex(), spoof=dict(node=SPOOF_NODE, epoch=SPOOF_EPOCH, step_m=SPOOF_STEP),
               loss=dict(node=LOSS_NODE, epoch=LOSS_EPOCH),
               nodes=[dict(id=ids[i], name=NAMES[i], role=ROLES[i]) for i in range(N)],
               epochs=[], sent=[], deliveries=[])
    sent = {}                                          # key -> (t sent, origin name, kind)
    t_wall = time.perf_counter()
    loss_i = NAMES.index(LOSS_NODE)
    for e in range(epochs):
        t_e = e * EPOCH
        for i in range(N):
            air.radios[i].position = truth(i, t_e)
        claims = {i: (pos(*claim(i, e, t_e)), velocity(i)) for i in range(N)}
        live = [i for i in range(N) if run.alive(i)]
        # orders: slot -> [(node, kind, payload)]
        orders: dict[int, list] = {}
        for j, i in enumerate(rng.permutation(live)):
            orders.setdefault(2 * j, []).append((int(i), "announce", None))
        for j, i in enumerate(d for d in drones if run.alive(d)):
            s = 1 + 3 * j
            orders.setdefault(s, []).append((i, "send", tlm(i, e)))
            sent[("tlm", NAMES[i], e)] = None, s
        d1 = NAMES.index("D1")
        for k_ in range(UDP_PER_EPOCH):
            s = 4 + 8 * k_
            orders.setdefault(s, []).append((d1, "ip", udp(e, k_)))
            sent[("udp", "D1", e, k_)] = None, s
        ep = dict(e=e, t=t_e,
                  truth=[[round(float(c), 1) for c in truth(i, t_e)] for i in range(N)],
                  claims=[[round(float(c), 1) for c in claim(i, e, t_e)] for i in range(N)],
                  alive=[run.alive(i) for i in range(N)],
                  snr=[[None if a == b else round(air.snr_db(a, b), 1) for b in range(N)] for a in range(N)],
                  slots=[])
        queues = {}
        s = 0
        lost_now = False
        while s < MAX_SLOTS:
            t = t_e + s * run.slot_s
            od = orders.get(s, [])
            od = [o for o in od if run.alive(o[0])]
            for o in od:
                if o[1] in ("send", "ip"):
                    key = (("tlm", NAMES[o[0]], e) if o[1] == "send"
                           else ("udp", "D1", e, struct.unpack(">HB", o[2][29:32])[1]))
                    sent[key] = (t, s)
            r = run.step(t, od, claims if s == 0 else None)
            slot = dict(s=s, t=round(t, 6), tx=r["tx"], orders=[(NAMES[o[0]], o[1]) for o in od], nodes={})
            if r.get("deferred"):
                slot["deferred"] = [NAMES[i] for i in r["deferred"]]
            for i, nr in r["nodes"].items():
                queues[i] = nr.get("queue", 0)
                keep = {k2: v for k2, v in nr.items() if k2 not in ("inbox", "ip", "queue")}
                if nr.get("queue"):
                    keep["queue"] = nr["queue"]
                for m in nr.get("inbox", []):
                    p = m["payload"]
                    if p[:1] == b"T":
                        drone, ep_ = struct.unpack(">BH", p[1:4])
                        key = ("tlm", NAMES[drone], ep_)
                        rec["deliveries"].append(dict(kind="tlm", origin=NAMES[drone], epoch=ep_, at=NAMES[i],
                                                      e=e, s=s, t=m["t"]))
                for x in nr.get("ip", []):
                    p = x["payload"][28:]
                    if p[:1] == b"U":
                        ep_, k_ = struct.unpack(">HB", p[1:4])
                        rec["deliveries"].append(dict(kind="udp", origin=name_of.get(x["origin"], "?"),
                                                      epoch=ep_, k=k_, at=NAMES[i], e=e, s=s, t=t))
                if len(keep) > 1 or "cpu" not in keep:
                    slot["nodes"][NAMES[i]] = keep
                else:
                    slot["nodes"][NAMES[i]] = {"cpu": keep["cpu"]}
            ep["slots"].append(slot)
            if e == LOSS_EPOCH and not lost_now and any(tx["node"] == loss_i for tx in r["tx"]):
                run.dead.add(loss_i)
                lost_now = True
                slot["event"] = f"{LOSS_NODE} lost"
            s += 1
            if s >= MIN_SLOTS and s > max(orders) and not any(queues.get(i, 0) for i in range(N) if run.alive(i)):
                break
        if e == LOSS_EPOCH and not lost_now:            # never sent this epoch: lose it at the end
            run.dead.add(loss_i)
            ep["slots"][-1]["event"] = f"{LOSS_NODE} lost"
        views = {}
        for g in ("G1", "G2", "D1"):
            gi = NAMES.index(g)
            if run.alive(gi):
                v = run.view(gi, relay_alt=RELAY_ALT)
                views[g] = dict(positions={name_of.get(h, h): xyz(p) for h, p in v["positions"].items()},
                                cut_off=[name_of.get(h, h) for h in v["cut_off"]],
                                plan={name_of.get(h, h): [[xyz(p) for p in sl] for sl in slots]
                                      for h, slots in v["plan"].items()},
                                distrusted=[name_of.get(h, h) for h in v["distrusted"]],
                                verdicts={name_of.get(h, h): vv for h, vv in v["verdicts"].items()},
                                heard=[(name_of.get(a, a), name_of.get(b, b), round(sn, 1))
                                       for a, b, sn in v["heard"]])
        ep["views"] = views
        rec["epochs"].append(ep)
        got_e = [d for d in rec["deliveries"] if d["e"] == e]
        if verbose:
            print(f"epoch {e} t={t_e:5.0f}s lead x={X0 + V * t_e:7.0f} m: slots {len(ep['slots'])} "
                  f"bursts {sum(len(sl['tx']) for sl in ep['slots'])} delivered {len(got_e)} "
                  f"G1 cut-off {views.get('G1', {}).get('cut_off')} distrusted@G1 {views.get('G1', {}).get('distrusted')} "
                  f"[{time.perf_counter() - t_wall:.0f}s wall]", flush=True)
        _summarise(rec, sent)                           # written every epoch: a partial run replays too
        rec["wall_s"] = round(time.perf_counter() - t_wall, 1)
        if out is not None:
            out.write_text(json.dumps(rec, separators=(",", ":")))
    run.close()
    ok = [x for x in rec["sent"] if x["delivered"]]
    lat = [x["latency_ms"] for x in ok]
    if verbose:
        print(f"delivered {len(ok)}/{len(rec['sent'])}  latency median {np.median(lat) if lat else float('nan'):.0f} ms "
              f"max {max(lat) if lat else float('nan'):.0f} ms  wall {rec['wall_s']:.0f} s  -> {out}")
    return rec


if __name__ == "__main__":
    main(int(sys.argv[1]) if len(sys.argv) > 1 else 7, int(sys.argv[2]) if len(sys.argv) > 2 else 6)
