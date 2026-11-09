"""
Validate the physical/virtual bridge: a socket-remote node must behave bit-identically to the same
node run in-process.  Two configs, each vs an all-local NodeRun(listen=False) baseline:
  * source REMOTE  -> the TX-IQ (next_burst) crosses the socket.
  * base   REMOTE  -> the RX-IQ (on_capture captures) crosses the socket.
A 2-node direct link (source shouts to HOME, base is the gateway) so delivery is deterministic.
"""
from __future__ import annotations

import math
import socket
import threading

import numpy as np

import bridge

from entroptics_mesh import phy
from entroptics_mesh.adapt import Radio
from entroptics_mesh.emu.air import Air, Radio as AirRadio
from entroptics_mesh.emu.node_run import NodeRun
from entroptics_mesh.neighbors import Position
from entroptics_mesh.node import Node
from entroptics_mesh.providers import NullCrypto, Providers
from entroptics_mesh.routing import HOME

RADIO = Radio(min_rate=1.2e6, max_rate=2.4e6, center_hz=915e6, max_power_dbm=20.0,
              noise_dbm=-100.0, freq_hz=915e6)
BASE = Position(45.0, -75.0, 100.0)
FULL = phy.Known(64, 1.0, 0.0)
SUB = [phy.Known(64, 0.5, 0.25), phy.Known(64, 0.5, -0.25)]
FAMILIES = ("differential",)
MSG = bytes(np.random.default_rng(7).integers(0, 256, 300, dtype=np.uint8))


class KeyOnly(NullCrypto):
    name = "key-only"

    def waveform_key(self, context):
        return 0xA17C0DE


def _east(m):
    return Position(BASE.lat, BASE.lon + math.degrees(m / (6_371_000.0 * math.cos(math.radians(BASE.lat)))), 100.0)


def build():
    """Air + [base(gateway), source] at a 2 km direct link; identical every call (fixed seeds)."""
    air = Air(freq_hz=915e6, rate=2.4e6, seed=1)
    air.add(AirRadio(position=np.array([0.0, 0.0, 100.0]), tx_power_dbm=20.0, noise_dbm=-100.0))
    air.add(AirRadio(position=np.array([2000.0, 0.0, 100.0]), tx_power_dbm=20.0, noise_dbm=-100.0))
    base = Node(b"BASE0000", RADIO, slot_s=0.0, providers=Providers(crypto=KeyOnly()), seed=10,
                position=BASE, blind_scaled=False, families=FAMILIES, accepts={HOME})
    src = Node(b"SRC00000", RADIO, slot_s=0.0, providers=Providers(crypto=KeyOnly()), seed=11,
               position=_east(2000.0), blind_scaled=False, families=FAMILIES)
    return air, base, src


def local_baseline(listen: bool):
    air, base, src = build()
    for a, b in ((base, src), (src, base)):
        a.on_announcement(b.announcement(0.0), b.node_id, 0.0)
    for n in (base, src):
        for g in (FULL, *SUB):
            n.learn(g)
    src.send(MSG, HOME, 0.0)
    run = NodeRun(air, [base, src], listen=listen)
    run.run(40, until=lambda: bool(base.inbox))
    return [bytes(m.payload) for m in base.inbox], run.bursts


def _remote_endpoint(node, transport: str):
    """A RemoteEndpoint to ``node`` served in a worker thread, over a socketpair or real TCP."""
    if transport == "tcp":
        lsock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        lsock.bind(("127.0.0.1", 0))
        lsock.listen(1)
        port = lsock.getsockname()[1]

        def _accept_serve():
            conn, _ = lsock.accept()
            lsock.close()
            bridge.serve(node, conn)

        threading.Thread(target=_accept_serve, daemon=True).start()
        return bridge.connect("127.0.0.1", port)
    sock_a, sock_b = socket.socketpair()
    threading.Thread(target=bridge.serve, args=(node, sock_b), daemon=True).start()
    return bridge.RemoteEndpoint(sock_a)


def distributed(remote: str, listen: bool, transport: str = "socketpair"):
    """remote in {'src','base'}: that node runs behind a socket in a worker thread."""
    air, base, src = build()
    nodes = {"base": base, "src": src}
    remote_ep = _remote_endpoint(nodes[remote], transport)
    eps = {name: (remote_ep if name == remote else bridge.LocalEndpoint(node))
           for name, node in nodes.items()}
    base_ep, src_ep = eps["base"], eps["src"]
    ann = {name: ep.announcement(0.0) for name, ep in eps.items()}        # mirrors _introduce + learn
    base_ep.on_announcement(ann["src"], b"SRC00000", 0.0)
    src_ep.on_announcement(ann["base"], b"BASE0000", 0.0)
    for ep in eps.values():
        for g in (FULL, *SUB):
            ep.learn(g)
    src_ep.send(MSG, HOME, 0.0)
    run = bridge.DistributedAirRun(air, [base_ep, src_ep], min_rate=RADIO.min_rate,
                                   families=FAMILIES, listen=listen)
    run.run(40, until=lambda: bool(base_ep.get("inbox")))
    inbox = [bytes(m.payload) for m in base_ep.get("inbox")]
    eps[remote].close()
    return inbox, run.bursts


if __name__ == "__main__":
    all_ok = True
    for listen in (False, True):
        base_in, base_bursts = local_baseline(listen)
        print(f"\n--- listen={listen} ---")
        print(f"all-local baseline : delivered {base_in == [MSG]}  bursts {base_bursts}")
        for transport in ("socketpair", "tcp"):
            for remote in ("src", "base"):
                inbox, bursts = distributed(remote, listen, transport)
                parity = (inbox == base_in) and (bursts == base_bursts)
                delivered = inbox == [MSG]
                all_ok &= parity and delivered
                print(f"distributed [{remote:>4} remote, {transport:>10}]: delivered {delivered}  "
                      f"bursts {bursts}  parity-vs-local {parity}")
    print(f"\nBRIDGE VALIDATION: {'PASS' if all_ok else 'FAIL'} "
          f"(remote node == in-process over socketpair AND real TCP, both access modes, delivered)")
    raise SystemExit(0 if all_ok else 1)
