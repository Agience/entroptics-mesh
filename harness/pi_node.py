"""
Deployable node server for a physical Pi-4B.

scp this (plus the entroptics stack) to the Pi and run ONE of these per Pi:

    python pi_node.py --role lead  --id LEAD0000 --port 52001
    python pi_node.py --role relay --id RLY00000 --port 52002
    python pi_node.py --role base  --id BASE0000 --port 52000

It constructs the node and listens for the air server (on the coordinator host), which drives it over the bridge:
the per-slot IQ (next_burst / on_capture / tick) and setup (position, announcements, learn) all arrive
as RPCs, so the Pi just runs the real entroptics stack and answers. Coordinator side:

    ep = bridge.connect("<pi-host>", 52001)      # one RemoteEndpoint per Pi
    run = bridge.DistributedAirRun(air, [ ...endpoints... ], min_rate=..., families=..., listen=True)

Role sets only construction-time flags (``accepts={HOME}`` for base); position, movement and
announcements are pushed by the harness at run time. The radio, key and families are the shared
scenario constants (match the harness).
"""
from __future__ import annotations

import argparse

from entroptics_mesh.adapt import Radio
from entroptics_mesh.neighbors import Position
from entroptics_mesh.node import Node
from entroptics_mesh.providers import NullCrypto, Providers
from entroptics_mesh.routing import HOME

import bridge

# Shared scenario constants -- keep in sync with the harness (fleet Pluto @915, ~3 dBi folded in air).
RADIO = Radio(min_rate=0.25e6, max_rate=2.4e6, center_hz=915e6, max_power_dbm=20.0,
              noise_dbm=-100.0, freq_hz=915e6, antenna_dbi=3.0)
KEY = 0xA17C0DE


class KeyOnly(NullCrypto):
    name = "key-only"

    def waveform_key(self, context):
        return KEY


def make_node(role: str, node_id: bytes) -> Node:
    accepts = {HOME} if role == "base" else frozenset()
    return Node(node_id, RADIO, slot_s=0.0, providers=Providers(crypto=KeyOnly()), seed=0,
                position=Position(0.0, 0.0, 100.0), blind_scaled=False,
                families=("differential",), accepts=accepts)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--role", required=True, choices=("base", "lead", "relay", "follower"))
    ap.add_argument("--id", required=True, help="8-char node id, e.g. LEAD0000")
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=52000)
    a = ap.parse_args()
    nid = a.id.encode()
    if len(nid) != 8:
        ap.error("--id must be exactly 8 characters")
    node = make_node(a.role, nid)
    print(f"pi_node: role={a.role} id={a.id} -- serving the real entroptics stack to the air server")
    bridge.serve_listening(node, a.host, a.port)
