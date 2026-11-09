"""
announce.py -- the optional node state & capability announcement (informally, "here I am, and what I
can do").  Not an "I'm here" ping: it carries the node's state and capability, not just presence.

A node may announce itself: its position and velocity, the band range it can capture, its transmit
power, and whom it heard at what SNR.  All of it is a physical measurement or a capability figure,
never a decode-quality figure: SNR is a measurement of the channel and, with positions, maps who
reaches whom and at what path loss; transmit power lets a receiver turn a measured RSS into a coarse
range (https://github.com/Agience/entroptics-rf/blob/main/research/nav/relative_range.md); a decode-quality report would be feedback, and the mesh runs
without feedback.

Nothing depends on an announcement arriving.  A sender that hears none uses its defaults; one that
hears some treats them as clues.  The content is topology, which is sensitive, so it travels sealed
by the crypto provider (plain under the null provider).  (Precise two-way ranging -- a reply to an
announcement -- is a provider/application concern, never the core's: the core stays reply-free.)

Wire format (big-endian), before sealing:

    u8  version (1)
    u8  flags         bit0 position, bit1 velocity, bit2 band, bit3 tx power, bit4 interference,
                      bit5 gateway (no payload: the node accepts HOME)
    8s  node id
    [position]  f64 lat, f64 lon, f32 alt
    [velocity]  f32 east, f32 north, f32 up
    [band]      f64 min_rate, f64 max_rate, f64 center_hz
    [tx power]  f32 dBm
    [interference]    u8 n, then n x (f64 offset_hz, f64 width_hz, f32 bearing_deg, f32 level_db) --
                offset is RELATIVE to this node's band centre (never an absolute frequency);
                bearing/level are NaN when not measured
    u8  n heard, then n x (8s node id, i8 SNR dB rounded and clipped to +-127)
"""
from __future__ import annotations

import math
import struct

from .neighbors import Announced, BandRange, InterferenceBand, Position

VERSION = 1
ADDRESS = b"ANNOUNCE"           # the mesh address an announcement travels to (one hop, never relayed)
_POS, _VEL, _BAND, _POWER, _INTERFERENCE, _GATEWAY = 1, 2, 4, 8, 16, 32


def pack(node_id: bytes, *, position: Position | None = None,
         velocity: tuple[float, float, float] | None = None, band: BandRange | None = None,
         tx_power_dbm: float | None = None, interference=None,
         heard: dict[bytes, float] | None = None, gateway: bool = False) -> bytes:
    if len(node_id) != 8:
        raise ValueError("node ids are 8 bytes")
    heard = dict(heard or {})
    if len(heard) > 255:
        raise ValueError("at most 255 heard entries")
    interference = list(interference or ())
    if len(interference) > 255:
        raise ValueError("at most 255 interference entries")
    flags = ((_POS if position else 0) | (_VEL if velocity else 0) | (_BAND if band else 0)
             | (_POWER if tx_power_dbm is not None else 0) | (_INTERFERENCE if interference else 0)
             | (_GATEWAY if gateway else 0))
    out = [struct.pack(">BB8s", VERSION, flags, node_id)]
    if position:
        out.append(struct.pack(">ddf", position.lat, position.lon, position.alt))
    if velocity:
        out.append(struct.pack(">fff", *velocity))
    if band:
        out.append(struct.pack(">ddd", band.min_rate, band.max_rate, band.center_hz))
    if tx_power_dbm is not None:
        out.append(struct.pack(">f", float(tx_power_dbm)))
    if interference:
        out.append(struct.pack(">B", len(interference)))
        for j in interference:
            out.append(struct.pack(">ddff", float(j.offset_hz), float(j.width_hz),
                                   float("nan") if j.bearing_deg is None else float(j.bearing_deg),
                                   float("nan") if j.level_db is None else float(j.level_db)))
    out.append(struct.pack(">B", len(heard)))
    for nid, snr in heard.items():
        if len(nid) != 8:
            raise ValueError("node ids are 8 bytes")
        out.append(struct.pack(">8sb", nid, int(max(-127, min(127, round(snr))))))
    return b"".join(out)


def unpack(data: bytes, t: float) -> tuple[bytes, Announced]:
    """``(node_id, Announced)`` from a packed announcement.  Raises ValueError if malformed."""
    try:
        ver, flags, nid = struct.unpack_from(">BB8s", data, 0)
        if ver != VERSION:
            raise ValueError(f"announcement version {ver}")
        at = 10
        pos = vel = band = txp = None
        if flags & _POS:
            lat, lon, alt = struct.unpack_from(">ddf", data, at); at += 20
            pos = Position(lat, lon, alt)
        if flags & _VEL:
            vel = struct.unpack_from(">fff", data, at); at += 12
        if flags & _BAND:
            band = BandRange(*struct.unpack_from(">ddd", data, at)); at += 24
        if flags & _POWER:
            (txp,) = struct.unpack_from(">f", data, at); at += 4
        interference = []
        if flags & _INTERFERENCE:
            (nj,) = struct.unpack_from(">B", data, at); at += 1
            for _ in range(nj):
                off, wid, brg, lvl = struct.unpack_from(">ddff", data, at); at += 24
                interference.append(InterferenceBand(off, wid,
                                      None if math.isnan(brg) else float(brg),
                                      None if math.isnan(lvl) else float(lvl)))
        (n,) = struct.unpack_from(">B", data, at); at += 1
        heard = {}
        for _ in range(n):
            h, snr = struct.unpack_from(">8sb", data, at); at += 9
            heard[h] = float(snr)
        if at != len(data):
            raise ValueError("trailing bytes")
    except struct.error as e:
        raise ValueError(f"malformed announcement: {e}") from None
    return nid, Announced(t, pos, tuple(vel) if vel else None, band, heard,
                          float(txp) if txp is not None else None, tuple(interference),
                          bool(flags & _GATEWAY))


def seal(crypto, node_id: bytes, **fields) -> bytes:
    """Pack and seal with the node's crypto provider; the context binds it to the sender."""
    return crypto.seal(pack(node_id, **fields), context=b"announce" + node_id)


def open_sealed(crypto, sender: bytes, sealed: bytes, t: float):
    """``(node_id, Announced)`` or None if it does not open or claims a different sender."""
    plain = crypto.open(sealed, context=b"announce" + sender)
    if plain is None:
        return None
    try:
        nid, a = unpack(plain, t)
    except ValueError:
        return None
    return (nid, a) if nid == sender else None


__all__ = ["ADDRESS", "pack", "unpack", "seal", "open_sealed", "VERSION"]
