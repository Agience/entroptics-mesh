"""Messages as fountain symbols: any spanning subset rebuilds the message, nothing is sent back."""
from __future__ import annotations

import numpy as np

from entroptics_mesh.link import Reassembler, Symbol, symbols
from entroptics_mesh.routing import BROADCAST, Header

H = Header(b"origin..", 9, BROADCAST, 4)
MSG = bytes(np.random.default_rng(0).integers(0, 256, 1000, dtype=np.uint8))


def test_a_symbol_packs_and_unpacks():
    s = symbols(H, MSG, 32, 1.0)[0]
    assert Symbol.unpack(s.pack()) == s


def test_any_spanning_subset_rebuilds_the_message_in_any_order():
    rng = np.random.default_rng(1)
    sent = symbols(H, MSG, 32, 2.0)                 # k = 32, 64 symbols on the air
    kept = [s for s in sent if rng.random() > 0.3]   # 30% lost
    rng.shuffle(kept)
    r = Reassembler()
    got = [m for m in (r.add(Symbol.unpack(s.pack())) for s in kept) if m is not None]
    assert got == [MSG]


def test_a_completed_message_is_delivered_once():
    r = Reassembler()
    out = [r.add(s) for s in symbols(H, MSG, 32, 2.0) * 2]
    assert sum(m is not None for m in out) == 1


def test_a_relayed_symbol_keeps_its_identity():
    s = symbols(H, MSG, 32, 1.0)[3]
    t = s.relayed(2)
    assert t.header.msg_id == s.header.msg_id and t.header.hops == 2 and t.data == s.data
