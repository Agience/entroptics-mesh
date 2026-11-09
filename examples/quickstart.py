"""
entroptics-mesh quickstart -- a runnable "hello world" for the SDK.

Run it:   PYTHONPATH=src:../entroptics-rf/src python examples/quickstart.py
          (or just `python examples/quickstart.py` after `pip install entroptics-mesh`, which pulls
           entroptics  <-  entroptics-rf  <-  entroptics-mesh)

What it shows, end to end, with NO radio hardware -- the emulated air (entroptics_mesh.emu) stands
in for real SDRs, so you can integrate and test before you ever key a transmitter:

  * a three-node chain  A --> B --> C  (15 km hops; C is 30 km from A, beyond A's direct reach).
    A has a message for C but cannot reach it directly; B relays.  This is the mesh's core:
    decode-and-forward over one-way links.
  * the FOUR refusals that make the waveform what it is:
      - nothing pre-shared : B and C were told no rate, centre, alphabet or code rate; each MEASURES
                             every parameter of every burst it hears, blind, confirmed by a frame CRC.
      - nothing required back: there is no handshake, no quality report, no ack that delivery depends
                             on.  C decodes as well as any node; a listen-only receiver works.  (A does
                             overhear B forwarding its message -- a free PASSIVE ack that the hop
                             worked, costing no airtime -- but nothing is ever required back.)
      - no added structure: the relay B never reuses the sub-band it heard A on.
      - pluggable security : crypto / frequency-agility / interference handling are providers you supply.  The core
                             ships null defaults (an open broadcast mesh); here we plug in one key.

  * a CUSTOM provider.  ``SharedKeyCrypto`` below is a minimal CryptoProvider: it supplies the
    physical-layer key the keyed aperture is drawn from (the sender and receivers hold the same key
    out of band, exactly like the waveform_key contract in research/PROVIDERS.md).  Swap in your own
    AEAD in ``seal`` / ``open`` and your own key schedule in ``waveform_key`` to make it real.

It takes a few seconds: the first burst from each sender triggers a full blind acquisition (the price
of pre-sharing nothing); after that a sender is tracked and reads are fast.  See the real-time
envelope in research/realtime/port_results.md.
"""
from __future__ import annotations

import math

import numpy as np

from entroptics_mesh import phy
from entroptics_mesh.adapt import Radio
from entroptics_mesh.emu.air import Air, Radio as AirRadio
from entroptics_mesh.emu.node_run import NodeRun
from entroptics_mesh.neighbors import Position
from entroptics_mesh.node import Node
from entroptics_mesh.providers import Providers

RX_RATE = 2.4e6                                            # the radios capture 2.4 MS/s of band
# The radio each node owns: the width range it may send in, its tuned centre, power and noise floor.
RADIO = Radio(min_rate=1.2e6, max_rate=RX_RATE, center_hz=915e6, max_power_dbm=20.0, noise_dbm=-100.0)
# Two 1.2 MHz sub-bands inside the 2.4 MHz capture -- the candidate geometries a node reads against
# (a Known is width + centre; the node still MEASURES which one, and the exact clock ratio, per burst).
SUB_BANDS = [phy.Known(64, 0.5, 0.25), phy.Known(64, 0.5, -0.25)]


class SharedKeyCrypto:
    """A minimal :class:`~entroptics_mesh.providers.CryptoProvider`.

    ``waveform_key`` returns the physical-layer key: the seed the keyed aperture, reference row,
    per-row rotations and per-row power are drawn from.  Sender and receivers hold the same key out
    of band (your key schedule's job -- the core only asks for the key in force).  ``seal`` / ``open``
    are where you put real authenticated encryption of the payload; here they pass through so the demo
    stays readable.  Return ``None`` from ``waveform_key`` for the open, key-free public aperture.
    """
    name = "shared-key"

    def __init__(self, key: int):
        self._key = key

    def seal(self, plaintext: bytes, *, context: bytes) -> bytes:
        return bytes(plaintext)                             # plug your AEAD in here

    def open(self, sealed: bytes, *, context: bytes) -> bytes | None:
        return bytes(sealed)                               # ...and its inverse here

    def waveform_key(self, context: bytes):
        return self._key                                   # the keyed-aperture seed


def _position(x_m: float) -> Position:
    """A position x metres east of a reference point (lat/lon degrees, 100 m altitude)."""
    return Position(0.0, math.degrees(x_m / 6_371_000.0), 100.0)


def run_quickstart(verbose: bool = True, hop_m: float = 15_000.0) -> tuple[bool, dict]:
    """Build the A->B->C chain, send one message A->C, and return (delivered, facts).

    ``hop_m`` is the A-B and B-C spacing; C sits at 2*hop_m, beyond A's direct reach, so B must
    relay (that is the point of the demo)."""
    key = 0xA17C0DE
    crypto = SharedKeyCrypto(key)

    # Three nodes, each a hop apart.  Each gets its own emulated radio on the shared air and its own
    # Node runtime.  blind_scaled=False keeps the demo quick (the senders here hold a stable clock).
    air = Air(rate=RX_RATE, seed=1)
    nodes = []
    for i, x in enumerate((0.0, hop_m, 2 * hop_m)):
        air.add(AirRadio(position=np.array([x, 0.0, 100.0]), tx_power_dbm=20.0, noise_dbm=-100.0))
        nodes.append(Node(bytes([65 + i]) * 8, RADIO, slot_s=0.0,          # ids b"AAAAAAAA", b"BBB...", b"CCC..."
                          providers=Providers(crypto=crypto), seed=100 + i,
                          position=_position(x), blind_scaled=False))
    a, b, c = nodes

    # One round of "I'm here": each node hears the others' position+band announcement (optional, and
    # the only thing ever broadcast about a node besides its traffic -- never decode quality).
    for src in nodes:
        for other in nodes:
            if other is not src:
                src.on_announcement(other.announcement(0.0), other.node_id, 0.0)
    # Register the candidate sub-band geometries each node reads against.
    for n in nodes:
        for g in SUB_BANDS:
            n.learn(g)

    # A has a 300-byte message for C.  No route is computed and none is asked for: A sends, B relays
    # what it rebuilds (fresh fountain symbols on a sub-band it did NOT hear A on), C reassembles.
    msg = bytes(np.random.default_rng(0).integers(0, 256, 300, dtype=np.uint8))
    a.send(msg, c.node_id, 0.0)

    run = NodeRun(air, nodes)                              # the driver: it is only the radios + the air
    run.run(max_slots=60, until=lambda: bool(c.inbox))

    delivered = [m.payload for m in c.inbox] == [msg]
    facts = {
        "delivered": delivered,
        "bursts_on_air": run.bursts,
        "relay_sent": sum(1 for _, i, _ in run.log if i == 1),
        "dest_captures": c.stats.captures,
        "source_overheard": a.stats.captures,             # A overhears B forwarding = passive ack (free)
        "relay_waveform_key": b.key,                      # B holds the same keyed aperture as A and C
    }
    if verbose:
        print(f"delivered A->C (via B)       : {delivered}")
        print(f"bursts on the air            : {facts['bursts_on_air']}")
        print(f"fresh symbols B relayed       : {facts['relay_sent']}  (B rebuilt A's message and re-sent)")
        print(f"bursts C captured             : {facts['dest_captures']}  (reads B's relay; A's is too far to decode)")
        print(f"bursts A overheard            : {facts['source_overheard']}  (hearing B forward = passive ack, never required)")
        print(f"keyed aperture seed at B      : {hex(facts['relay_waveform_key'])}  (== A's and C's key)")
        print("\nnothing was pre-shared but the key; no handshake or ack was required; B measured every parameter blind.")
    return delivered, facts


if __name__ == "__main__":
    ok, _ = run_quickstart()
    raise SystemExit(0 if ok else 1)
