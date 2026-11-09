"""The sealing crypto provider (``ext.aead.AeadCrypto``): the provider contract, the round trip, the
refusals (wrong key, tampering, replay), and end-to-end sealing over the emulated mesh -- the
destination reads the plaintext while the relay forwards bytes it cannot open."""
from __future__ import annotations

import math

import numpy as np
import pytest

pytest.importorskip("cryptography")

from entroptics_mesh import phy
from entroptics_mesh.adapt import Radio
from entroptics_mesh.emu.air import Air, Radio as AirRadio
from entroptics_mesh.emu.node_run import NodeRun
from entroptics_mesh.ext.aead import OVERHEAD, AeadCrypto
from entroptics_mesh.link import Reassembler
from entroptics_mesh.neighbors import Position
from entroptics_mesh.node import Node, _context
from entroptics_mesh.providers import CryptoProvider, NullCrypto, Providers

KEY = bytes(range(32))
OTHER = bytes(range(1, 33))
CTX = b"msg" + b"ORIGIN00" + (7).to_bytes(4, "big") + b"DEST0000"


# ── the provider ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("cipher", ["chacha20poly1305", "aes-256-gcm"])
def test_aead_passes_the_provider_contract(cipher):
    c = AeadCrypto(KEY, cipher=cipher, waveform_key=1234)
    assert isinstance(c, CryptoProvider)
    assert isinstance(c.name, str) and cipher in c.name
    assert c.waveform_key(b"phy") == 1234                      # the PHY key is a separate input
    assert AeadCrypto(KEY).waveform_key(b"phy") is None         # default: the public aperture
    assert c.address(b"NODE0000", context=b"e") == b"NODE0000"  # raw addressing, as the null provider
    assert c.resolve(b"NODE0000") == b"NODE0000"
    sealed = c.seal(b"hello", context=CTX)
    assert isinstance(sealed, bytes) and len(sealed) == len(b"hello") + OVERHEAD
    assert AeadCrypto(KEY, cipher=cipher).open(sealed, context=CTX) == b"hello"


def test_bad_keys_and_options_are_refused():
    with pytest.raises(ValueError, match="32 bytes"):
        AeadCrypto(b"short")
    with pytest.raises(ValueError, match="cipher"):
        AeadCrypto(KEY, cipher="rot13")
    with pytest.raises(ValueError, match="window"):
        AeadCrypto(KEY, window=0)


def test_round_trip_hides_the_plaintext_and_draws_a_fresh_nonce_per_seal():
    tx, rx = AeadCrypto(KEY), AeadCrypto(KEY)
    msg = b"the payload, in the clear only at the ends" * 3
    a, b = tx.seal(msg, context=CTX), tx.seal(msg, context=CTX)
    assert msg not in a and a != b                              # encrypted; unique nonce per seal
    assert a[:12] != b[:12]
    assert rx.open(a, context=CTX) == msg
    assert rx.open(b, context=CTX) == msg
    assert rx.open(tx.seal(b"", context=CTX), context=CTX) == b""


def test_wrong_key_is_refused():
    sealed = AeadCrypto(KEY).seal(b"secret", context=CTX)
    assert AeadCrypto(OTHER).open(sealed, context=CTX) is None
    assert AeadCrypto(KEY, cipher="aes-256-gcm").open(sealed, context=CTX) is None   # other cipher


def test_tampering_is_refused_anywhere_in_the_sealed_bytes_or_the_context():
    sealed = AeadCrypto(KEY).seal(b"secret payload", context=CTX)
    for i in range(len(sealed)):                                # nonce, ciphertext and tag
        bad = bytearray(sealed)
        bad[i] ^= 0x01
        assert AeadCrypto(KEY).open(bytes(bad), context=CTX) is None
    rx = AeadCrypto(KEY)
    assert rx.open(sealed, context=CTX[:-1] + b"1") is None     # context is bound: another destination
    assert rx.open(sealed[:-1], context=CTX) is None            # truncated
    assert rx.open(sealed[:OVERHEAD - 1], context=CTX) is None
    assert rx.open(sealed, context=CTX) == b"secret payload"    # a refused forgery moved no window


def test_replay_is_refused_and_out_of_order_inside_the_window_is_accepted():
    tx, rx = AeadCrypto(KEY), AeadCrypto(KEY, window=8)
    s = [tx.seal(bytes([i]), context=CTX) for i in range(12)]
    assert rx.open(s[3], context=CTX) == b"\x03"
    assert rx.open(s[3], context=CTX) is None                   # replay
    assert rx.open(s[1], context=CTX) == b"\x01"                # late, inside the window
    assert rx.open(s[1], context=CTX) is None
    assert rx.open(s[11], context=CTX) == b"\x0b"               # jumps ahead
    assert rx.open(s[3], context=CTX) is None                   # 8 behind the top: outside the window
    assert rx.open(s[4], context=CTX) == b"\x04"                # 7 behind: inside, never seen
    assert rx.open(s[11], context=CTX) is None
    # each sender has its own window: another instance's counter 3 is not a replay
    assert rx.open(AeadCrypto(KEY).seal(b"x", context=CTX), context=CTX) == b"x"


# ── end to end over the emulated mesh ────────────────────────────────────────────────────────

RX = 2.4e6
RADIO = Radio(min_rate=1.2e6, max_rate=RX, center_hz=915e6, max_power_dbm=20.0, noise_dbm=-100.0)
SUB = [phy.Known(64, 0.5, 0.25), phy.Known(64, 0.5, -0.25)]
PHY_KEY = 1234                                  # one aperture key for every node; sealing is separate


class KeyOnly(NullCrypto):
    """The aperture key and no sealing key: a relay that holds nothing to open payloads with."""
    name = "key-only"

    def waveform_key(self, context):
        return PHY_KEY


class Recording(Reassembler):
    """Keeps every message this relay rebuilt -- the bytes it forwards."""

    def __init__(self):
        super().__init__()
        self.rebuilt = {}

    def add(self, s):
        m = super().add(s)
        if m is not None:
            self.rebuilt[s.header.msg_id] = (s.header, m)
        return m


def _pos(x):
    return Position(0.0, math.degrees(x / 6_371_000.0), 100.0)


def _chain(cryptos, seed=2):
    air = Air(rate=RX, seed=seed)
    nodes = []
    for i, c in enumerate(cryptos):
        air.add(AirRadio(position=np.array([9000.0 * i, 0.0, 100.0]), tx_power_dbm=20.0, noise_dbm=-100.0))
        nodes.append(Node(bytes([65 + i]) * 8, RADIO, slot_s=0.0, position=_pos(9000.0 * i),
                          providers=Providers(crypto=c), seed=100 * seed + i, blind_scaled=False))
    for a in nodes:
        for b in nodes:
            if a is not b:
                a.on_announcement(b.announcement(0.0), b.node_id, 0.0)
        for g in SUB:
            a.learn(g)
    return air, nodes


@pytest.mark.slow
@pytest.mark.parametrize("relay_crypto", [lambda: AeadCrypto(OTHER, waveform_key=PHY_KEY), KeyOnly],
                         ids=["relay-other-key", "relay-no-key"])
def test_multi_hop_delivers_plaintext_only_to_the_destination(relay_crypto):
    # a -> b -> c over 9 km hops; b rebuilds the message and forwards fresh fountain symbols of it.
    # a and c share the mesh key; b holds another key or none, and the same aperture key.
    air, (a, b, c) = _chain([AeadCrypto(KEY, waveform_key=PHY_KEY), relay_crypto(),
                             AeadCrypto(KEY, waveform_key=PHY_KEY)])
    b.rx = Recording()
    run = NodeRun(air, [a, b, c])
    msg = bytes(np.random.default_rng(1).integers(0, 256, 300, dtype=np.uint8))
    mid = a.send(msg, c.node_id, 0.0)
    run.run(60, until=lambda: bool(c.inbox) and not b.queue)

    assert [m.payload for m in c.inbox] == [msg]                # the destination reads the plaintext
    assert any(i == 1 for _, i, _ in run.log)                   # the relay transmitted ...
    header, carried = b.rx.rebuilt[mid]                         # ... the message it rebuilt
    assert msg not in carried                                   # the plaintext never reached the relay
    # and it cannot open it: a wrong key refuses, no key hands back the ciphertext unchanged
    assert b.crypto.open(carried, context=_context(header)) in (None, carried)
    assert not b.inbox
    # the bytes the relay forwarded are what the destination opened
    assert AeadCrypto(KEY).open(carried, context=_context(header)) == msg
