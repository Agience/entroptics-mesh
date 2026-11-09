"""
link.py -- messages as fountain symbols: reliability with nothing sent back.

A message is cut into ``k`` blocks and sent as LT fountain symbols (``fec.fountain_encode``); a
receiver rebuilds it from any set of symbols that spans the blocks (``fec.FountainDecoder``), in any
order, from any mix of paths.  So a collision, a fade or a lost relay costs symbols, not the
message, and no receiver ever has to ask for anything.

What the sender cannot know, with no feedback, is when everyone has enough.  So it sends
``ceil(k * redundancy)`` symbols, a choice made up front from what it knows of its audience -- the
place where an agility provider can spend more airtime on an important message or less on a
routine one.  Measured fountain overhead (30% erasures; systematic + dense repair for k <= 64,
LT above): 1.09 / 1.02 / 1.01 symbols per block on average at k = 16 / 64 / 256, worst seen 1.44 /
1.16 / 1.03 (200 / 200 / 40 trials).

Symbol header, carried inside each FEC-protected frame (big-endian):

    8s origin   I seq   8s to   B hops   H budget ms   H k   H block   I length   I symbol id
"""
from __future__ import annotations

import math
import struct
from dataclasses import dataclass, field

import numpy as np

from . import fec
from .routing import Header

_SYM = struct.Struct(">8sI8sBHHHII")
HEADER_BYTES = _SYM.size


@dataclass(frozen=True)
class Symbol:
    header: Header
    k: int
    block: int
    length: int
    symbol_id: int
    data: bytes

    def pack(self) -> bytes:
        h = self.header
        return _SYM.pack(h.origin, h.seq, h.to, h.hops, min(int(h.budget_ms), 0xFFFF), self.k,
                         self.block, self.length, self.symbol_id) + self.data

    @classmethod
    def unpack(cls, raw: bytes) -> "Symbol | None":
        if len(raw) < _SYM.size:
            return None
        origin, seq, to, hops, budget, k, block, length, sid = _SYM.unpack_from(raw, 0)
        data = raw[_SYM.size:]
        if k < 1 or block < 1 or len(data) != block or length > k * block:
            return None
        return cls(Header(origin, seq, to, hops, budget), k, block, length, sid, data)

    def relayed(self, hops: int) -> "Symbol":
        h = self.header
        return Symbol(Header(h.origin, h.seq, h.to, hops, h.budget_ms), self.k, self.block, self.length,
                      self.symbol_id, self.data)


FRAME_BYTES = 512                 # protected size of every frame: one symbol + its header (+ CRC)
BLOCK = FRAME_BYTES - 2 - HEADER_BYTES       # fountain block that fits in a frame


def frame(sym: "Symbol", rate: str = "1/2") -> np.ndarray:
    """The coded bits of one symbol's frame: the symbol under the packet code and CRC
    (``fec.encode_packet``) at the plan's code ``rate``, fixed size, one burst each.  Nothing in
    the frame names the rate: a receiver tries each and the CRC chooses (:func:`read_frame`)."""
    return fec.encode_packet(sym.pack(), frame_bytes=FRAME_BYTES, rate=rate).astype(np.int64)


RATES = tuple(fec.PUNCTURE_PATTERNS)
SEQUENTIAL = 3      # up to this many hypotheses are decoded one by one (a single Viterbi run is
                    # ~45-60 ms; a batch of 12 ~180-210, of 24 ~220-270, measured locally)


def read_frame(rx, *, rates=RATES, hypotheses=None):
    """The symbol in one burst and the mode it rode, ``(Symbol, (family, M, rate))``, or None when
    no hypothesis passes the CRC.

    The burst does not say how it was sent, and nothing was agreed about it.  Every hypothesis --
    each (family, alphabet) the burst was read as (``phy.RxBurst.hypotheses``: the family its
    closures favour first) at each code rate -- is turned into soft bits and all of them are
    decoded in one batched Viterbi pass (``fec.decode_packets``); the first, in that order, whose
    CRC and symbol header pass wins.  The CRC is an exact test, so the order only matters if two
    hypotheses both pass (each wrong one does with probability ~2^-32).  The burst's length is
    measured too (``phy.RxBurst.rows``): a hypothesis whose frame would fill a burst more than one
    row longer or shorter is not decoded, which leaves 2-6 of the 24 on a burst with silence after
    it; with no length to read (the capture ends in the burst), all of them are.

    Why the CRC and not a self-describing header (measured, research/mcs/phy_mcs_link.py):

      * a header would cost airtime on every burst -- one row of a 13-140-row burst is 0.7%
        (DBPSK 1/2) to 7.1% (256-QAM 3/4) of the goodput, most where the rate is highest -- and
        it must be read before the alphabet is known, so it needs its own fixed alphabet inside
        the burst: a wire change in entroptics-rf (``compose``/``compose_qam`` write one
        alphabet), and a fixed-format field in a fixed place on every burst, which is the kind of
        repeated structure the no-added-structure rule keeps off the air;
      * the CRC costs CPU only: with the burst's length read, 1-3 hypotheses remain and are
        decoded one by one, stopping at the first pass, so a frame costs about one Viterbi run
        (~45-120 ms here, as the single pinned decode did); 24 hypotheses only when no length
        is readable (~220-270 ms, one batched pass).  Each wrong hypothesis passes the CRC-32
        with probability 2^-32 and must then also unpack as a symbol header.

    ``rx`` is a ``phy.RxBurst``, or a plain differential soft array (read as every differential
    alphabet).  ``hypotheses`` restricts the (family, M) pairs tried.  The result is cached on the
    burst, so a frame checked once (the fast read's ``accept``) is not decoded again."""
    from . import phy
    if not isinstance(rx, phy.RxBurst):
        soft = np.asarray(rx)
        rx = phy.RxBurst(None, soft.reshape(1, -1) if soft.ndim < 2 else soft)
    hyp = tuple(rx.hypotheses() if hypotheses is None else hypotheses)
    tag = ("frame", hyp, tuple(rates))
    if tag in rx.cache:
        return rx.cache[tag]
    # Two stages: the hypotheses whose family's length was read and fits it, then -- only if none
    # of those passes -- those of a family whose read gave no length.
    stages = ([], [])
    held = {}
    for family, M in hyp:
        if family not in held:
            a = rx.soft if family == "differential" else rx.qam
            held[family] = (rx.rows(family), int(a.shape[1]) if a.ndim == 2 and a.shape[1] else 1)
        rows, K = held[family]
        size = (rx.soft if family == "differential" else rx.qam).size
        for rate in rates:
            n = fec.packet_coded_length(FRAME_BYTES, rate=rate)
            n_sym = -(-n // int(round(np.log2(M))))
            if rows is not None and abs(-(-n_sym // K) - rows) > 1:
                continue        # the burst's measured length rules this one out
            if size >= n_sym:   # the read holds the hypothesis's symbols (``soft_bits``' own test)
                stages[rows is None].append(((family, int(M), n, rate), (family, int(M), rate)))
    got = None
    for stage in stages:
        # A few hypotheses (the usual case once the length is read): one at a time in order,
        # stopping at the first that passes -- a single decode is the leaner loop, and each
        # hypothesis's soft bits are made only when it is tried (a coherent read's, open, levels
        # every row: ~5 ms a hypothesis, spent for nothing when the differential passes first).
        # Many: one batched pass.
        if len(stage) <= SEQUENTIAL:
            raws = (fec.decode_packet(rx.soft_bits(f, M, n), rate=r) for f, M, n, r in
                    (c for c, _ in stage))
        else:
            raws = iter(fec.decode_packets([(rx.soft_bits(f, M, n), r) for (f, M, n, r), _ in stage]))
        for raw, (_, mode) in zip(raws, stage):
            sym = None if raw is None else Symbol.unpack(raw)
            if sym is not None:
                got = (sym, mode)
                break
        if got is not None:
            break
    rx.cache[tag] = got
    return got


def unframe(rx, M: int | None = None, rate: str | None = None, family: str | None = None) -> "Symbol | None":
    """The symbol in one burst (a ``phy.RxBurst`` or differential soft values), or None when the
    CRC refuses every hypothesis.  ``M``, ``rate`` and ``family`` pin a hypothesis instead of
    letting the CRC choose among all of them."""
    from . import phy
    hyp = None
    if M is not None or family is not None:
        alph = {"differential": phy.DIFFERENTIAL_M, "coherent": phy.COHERENT_M}
        hyp = [(f, m) for f in ((family,) if family is not None else phy.FAMILIES)
               for m in alph[f] if M is None or m == int(M)]
    got = read_frame(rx, rates=RATES if rate is None else (rate,), hypotheses=hyp)
    return None if got is None else got[0]


def _seed(origin: bytes, seq: int) -> int:
    """The fountain's seed for one message, derived from its id: nothing to agree."""
    return int.from_bytes(fec.crc32(origin + seq.to_bytes(4, "big")).to_bytes(4, "big"), "big")


def symbols(header: Header, message: bytes, block: int, redundancy: float):
    """The symbols a sender puts on the air for one message."""
    message = bytes(message)
    k = max(1, math.ceil(len(message) / block))
    n = max(k, math.ceil(k * float(redundancy)))
    src = fec.fountain_encode(message, block, _seed(header.origin, header.seq))
    out = []
    for _ in range(n):
        sid, data = next(src)
        out.append(Symbol(header, k, block, len(message), sid, data))
    return out


@dataclass
class Reassembler:
    """Collects symbols per message and hands back each message once, when it completes."""
    done: set = field(default_factory=set)
    _open: dict = field(default_factory=dict)

    def add(self, s: Symbol) -> bytes | None:
        mid = s.header.msg_id
        if mid in self.done:
            return None
        dec = self._open.get(mid)
        if dec is None:
            dec = self._open[mid] = fec.FountainDecoder(s.k, s.block, _seed(*mid))
        if dec.add(s.symbol_id, s.data):
            self.done.add(mid)
            del self._open[mid]
            return dec.message(s.length)
        return None

    def forget(self, msg_ids) -> None:
        """Drop everything held for these messages, finished or not (memory bound by age)."""
        for m in msg_ids:
            self.done.discard(m)
            self._open.pop(m, None)


__all__ = ["BLOCK", "FRAME_BYTES", "HEADER_BYTES", "RATES", "Symbol", "frame", "read_frame", "symbols", "unframe",
           "Reassembler"]
