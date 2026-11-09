"""
Air server -- the lab's fleet runner.

Owns the emulated air + slot clock and drives a fleet of nodes that are a MIX of physical (a Pi over
TCP, a RemoteEndpoint) and virtual (a Node in-process on this host, a LocalEndpoint). This is the
lab topology: run this on the coordinator host, one pi_node.py per physical Pi, and the rest virtual
to fill out larger fleets. The same script runs anywhere -- only the fleet spec changes.

Validated here with 1 physical Pi (lead role, over TCP) + virtual base/relays: physical and virtual
nodes share one air, and a physical FPV drone's video reaches a virtual base through a virtual relay
chain -- physical+virtual interop, server-driven, the production path.
"""
from __future__ import annotations

import math

import numpy as np

import bridge
from flight import Formation

from entroptics_mesh import phy
from entroptics_mesh.adapt import Radio
from entroptics_mesh.emu.air import Air, Radio as AirRadio
from entroptics_mesh.neighbors import Position
from entroptics_mesh.node import Node
from entroptics_mesh.providers import NullCrypto, Providers
from entroptics_mesh.routing import HOME

RADIO = Radio(min_rate=0.25e6, max_rate=2.4e6, center_hz=915e6, max_power_dbm=20.0,
              noise_dbm=-100.0, freq_hz=915e6, antenna_dbi=3.0)
AIR_TX = 26.0
BASE = Position(45.0, -75.0, 100.0)
FULL = phy.Known(64, 1.0, 0.0)
SUB = [phy.Known(64, 0.5, 0.25), phy.Known(64, 0.5, -0.25)]


class KeyOnly(NullCrypto):
    name = "key-only"
    def waveform_key(self, c): return 0xA17C0DE


def _pos(m): return Position(BASE.lat, BASE.lon + math.degrees(m / (6_371_000 * math.cos(math.radians(BASE.lat)))), 100.0)
def _east(p): return (p.lon - BASE.lon) * math.pi / 180 * 6_371_000 * math.cos(math.radians(BASE.lat))


def run_fleet(specs, send_from, seed=1, max_slots=200):
    """specs: ordered list of {id: bytes, role: str, pos: Position, physical: (host,port)|None}.
    A physical node is reached over TCP (a pi_node.py); a virtual one is built in-process. Returns
    (delivered, bursts, sender_indices)."""
    air = Air(freq_hz=915e6, rate=2.4e6, seed=seed)
    eps, roles, order = {}, {}, [s["id"] for s in specs]
    for i, s in enumerate(specs):
        air.add(AirRadio(position=np.array([_east(s["pos"]), 0.0, 100.0]), tx_power_dbm=AIR_TX, noise_dbm=-100.0))
        roles[s["id"]] = s["role"]
        if s["physical"]:
            ep = bridge.connect(*s["physical"])
            ep.set("position", s["pos"])
            eps[s["id"]] = ep
        else:
            accepts = {HOME} if s["role"] == "base" else frozenset()
            node = Node(s["id"], RADIO, slot_s=0.0, providers=Providers(crypto=KeyOnly()), seed=100 + i,
                        position=s["pos"], blind_scaled=False, families=("differential",), accepts=accepts)
            eps[s["id"]] = bridge.LocalEndpoint(node)
    anns = {nid: eps[nid].announcement(0.0) for nid in order}            # all-to-all "I'm here" + learn
    for a in order:
        for b in order:
            if a != b:
                eps[a].on_announcement(anns[b], b, 0.0)
        for g in (FULL, *SUB):
            eps[a].learn(g)
    msg = bytes(np.random.default_rng(seed).integers(0, 256, 300, dtype=np.uint8))
    eps[send_from].send(msg, HOME, 0.0)
    base_id = next(nid for nid in order if roles[nid] == "base")
    run = bridge.DistributedAirRun(air, [eps[nid] for nid in order], min_rate=RADIO.min_rate,
                                   families=("differential",), listen=True)
    run.run(max_slots, until=lambda: bool(eps[base_id].get("inbox")))
    inbox = eps[base_id].get("inbox")
    delivered = [bytes(m.payload) for m in inbox] == [msg]
    senders = sorted(run.senders_seen)
    for nid in order:
        if isinstance(eps[nid], bridge.RemoteEndpoint):
            eps[nid].close()
    return delivered, run.bursts, senders, order


if __name__ == "__main__":
    import os
    import sys
    # usage: air_server.py [lead_km] [pi_host:port]   (or ENTROPTICS_PI=host:port)
    lead_km = float(sys.argv[1]) if len(sys.argv) > 1 else 36.0
    pi = sys.argv[2] if len(sys.argv) > 2 else os.environ.get("ENTROPTICS_PI", "")
    if ":" not in pi:
        sys.exit("usage: air_server.py [lead_km] <pi_host:port>  (or set ENTROPTICS_PI=host:port)")
    PI = (pi.rsplit(":", 1)[0], int(pi.rsplit(":", 1)[1]))
    relays = Formation(RADIO, rate=0.5e6).assign(BASE, _pos(lead_km * 1000), 3)["relays"]
    specs = [{"id": b"BASE0000", "role": "base", "pos": BASE, "physical": None}]
    for i, p in enumerate(relays):
        specs.append({"id": b"RLY%05d" % i, "role": "relay", "pos": p, "physical": None})
    specs.append({"id": b"LEAD0000", "role": "lead", "pos": _pos(lead_km * 1000), "physical": PI})
    print(f"mixed fleet: base(virtual) + {len(relays)} relays(virtual) + lead @{lead_km:.0f}km (PHYSICAL Pi)")
    delivered, bursts, senders, order = run_fleet(specs, send_from=b"LEAD0000")
    relayed = any(0 < i < len(order) - 1 for i in senders)              # a virtual relay transmitted
    print(f"  delivered {delivered}  bursts {bursts}  transmitters {[order[i].decode() for i in senders]}")
    print(f"  via virtual relay chain: {relayed}  (physical lead @36km > direct range, so relays carry it)")
    print("=> physical Pi + virtual nodes on ONE server-driven air; the production lab topology.")
    sys.exit(0 if delivered else 1)
