"""
lan_air.py -- the emulated air stretched across hosts over a LAN.

:class:`~entroptics_mesh.emu.air.Air` is the shared medium inside one process: every node's burst is
summed, scaled by path loss and delayed, into every other node's capture.  ``LanAir`` is that same
medium with its nodes split across machines.  Each host holds the identical static view of every
radio (position, transmit power, noise floor) and declares which radio indices are LOCAL to it.  A
burst a local node transmits is put on the local medium AND serialized to every peer host over UDP;
a burst that arrives from a peer is injected into the local medium as its (remote) sender.  Captures,
path loss and SNR are then exactly :class:`Air`'s -- the real Entroptics waveform is what crosses the
wire, demodulated on the far side; only the physical RF propagation is replaced by a LAN hop.

The two hosts' clocks are not synchronized and are not assumed to be.  A radio hears a burst when it
arrives, not on a shared grid, so an arriving burst is stamped into the capture that reads it (the
emulated propagation delay over metres is far below a slot and is dropped).  Delivery is best-effort,
like the air it stands in for: a datagram that would exceed the UDP limit is dropped and counted (a
faded burst), which the mesh's FEC, fountain redundancy and reply-free flooding already tolerate.

Pure logic (framing, the medium) is cross-platform and unit-tested over loopback UDP; the daemon that
drives it in wall-clock time is :mod:`entroptics_mesh.run_bridge`.
"""
from __future__ import annotations

import socket
import struct
from dataclasses import dataclass

import numpy as np

from .air import Air, Radio

# Wire framing for one burst: a fixed header then the IQ as interleaved 16-bit samples, scaled per
# burst so the quantizer tracks the burst's own peak (near-lossless, ~16-bit depth -- the depth a
# real SDR's ADC/DAC carry anyway).  int16 keeps one burst in one datagram up to ~16k samples; a
# burst larger than one datagram is dropped and counted (a faded burst the mesh's FEC covers).
_MAGIC = b"EMAB"
_VERSION = 2
_HDR = struct.Struct("<4sBHdddId")      # magic, version, sender, t, rate (0=air rate), center, n, scale
_FULL = 32767.0
_MAX_DATAGRAM = 60000                    # one burst = one datagram; above this it is dropped (a fade)


def encode_burst(sender: int, t: float, iq: np.ndarray, rate: float | None, center: float) -> bytes:
    iq = np.asarray(iq, dtype=complex)
    peak = float(max(np.abs(iq.real).max(initial=0.0), np.abs(iq.imag).max(initial=0.0)))
    scale = peak / _FULL if peak > 0.0 else 1.0
    q = np.empty(iq.size * 2, dtype=np.int16)
    q[0::2] = np.clip(np.rint(iq.real / scale), -_FULL, _FULL).astype(np.int16)
    q[1::2] = np.clip(np.rint(iq.imag / scale), -_FULL, _FULL).astype(np.int16)
    return _HDR.pack(_MAGIC, _VERSION, int(sender), float(t), float(rate or 0.0),
                     float(center), iq.size, scale) + q.tobytes()


def decode_burst(data: bytes):
    """``(sender, t, iq, rate, center)`` from a datagram, or None if it is not a well-formed burst
    (wrong magic/version, short header, or a truncated IQ payload)."""
    if len(data) < _HDR.size:
        return None
    magic, ver, sender, t, rate, center, n, scale = _HDR.unpack_from(data)
    if magic != _MAGIC or ver != _VERSION:
        return None
    body = data[_HDR.size:]
    if len(body) != n * 4:                           # two int16 per sample; a short read is corrupt
        return None
    q = np.frombuffer(body, dtype=np.int16).astype(float) * scale
    iq = q[0::2] + 1j * q[1::2]
    return sender, t, iq, (rate or None), center


@dataclass
class Peer:
    host: str
    port: int

    @property
    def addr(self):
        return (self.host, self.port)


class LanAir:
    """The shared medium across hosts.  Construct the identical ``radios`` list on every host (same
    order = same index); ``local`` is the set of indices whose nodes run on THIS host.  ``now_fn`` is
    the host's wall clock, used only to time arrivals.  Delegates all physics to an inner :class:`Air`
    so captures, path loss and SNR match the single-process emulator exactly."""

    def __init__(self, radios: list[Radio], local, peers: list[Peer], *, bind=("0.0.0.0", 0),
                 freq_hz: float = 915e6, rate: float = 2.0e6, seed: int = 0, now_fn=None):
        self._air = Air(freq_hz=freq_hz, rate=rate, seed=seed)
        for r in radios:
            self._air.add(r)
        self.local = set(int(i) for i in local)
        self.peers = list(peers)
        self.now_fn = now_fn
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        # A large receive buffer so bursts arriving while the node is busy decoding are queued by
        # the kernel instead of dropped (the real-time loop drains them after each decode).
        try:
            self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 8 * 1024 * 1024)
        except OSError:
            pass
        self.sock.bind(bind)
        self.sock.setblocking(False)
        self._inbox: list = []                       # raw bursts received, injected at next capture
        self.tx = 0                                  # bursts sent to peers
        self.rx = 0                                  # bursts received from peers
        self.dropped_tx = 0                          # local bursts too big for one datagram
        self.dropped_rx = 0                          # malformed datagrams

    # ---- medium (delegates to the inner Air) ------------------------------------------------------

    @property
    def radios(self):
        return self._air.radios

    @property
    def rate(self):
        return self._air.rate

    @property
    def freq_hz(self):
        return self._air.freq_hz

    def gain_db(self, a: int, b: int) -> float:
        return self._air.gain_db(a, b)

    def snr_db(self, a: int, b: int) -> float:
        return self._air.snr_db(a, b)

    def clear_before(self, t: float) -> None:
        self._air.clear_before(t)

    def send(self, sender: int, t: float, iq, *, rate: float | None = None, center: float = 0.0) -> None:
        """A burst from a local node: onto the local medium and out to every peer host."""
        self._air.send(sender, t, iq, rate=rate, center=center)
        if int(sender) in self.local:
            dg = encode_burst(sender, t, iq, rate, center)
            if len(dg) > _MAX_DATAGRAM:
                self.dropped_tx += 1
                return
            for p in self.peers:
                self.sock.sendto(dg, p.addr)
            self.tx += 1

    # ---- UDP ingress ------------------------------------------------------------------------------

    def fileno(self) -> int:
        """The receive socket's fd, for ``select``."""
        return self.sock.fileno()

    def pump(self) -> int:
        """Drain every datagram waiting on the socket into the inbox; returns how many bursts were
        taken.  Non-blocking -- call it whenever the socket is readable."""
        got = 0
        while True:
            try:
                data, _ = self.sock.recvfrom(65535)
            except BlockingIOError:
                break
            except OSError:
                break
            b = decode_burst(data)
            if b is None:
                self.dropped_rx += 1
                continue
            self._inbox.append(b)
            self.rx += 1
            got += 1
        return got

    def capture(self, receiver: int, t0: float, n: int) -> np.ndarray:
        """What ``receiver`` hears over ``n`` samples from ``t0``.  Every burst that arrived from a
        peer since the last capture is injected into the medium at ``t0`` first (it is heard in the
        slot that reads it, decoupled from the far host's clock), then the inner Air captures it.
        Used by the batch/slot callers (tests); the real-time daemon decodes one burst at a time via
        :meth:`capture_one` so arrivals never overlap in a single window."""
        if self._inbox:
            guard = 8.0 / self._air.rate                  # a few samples in, so it lands fully inside
            for sender, _t, iq, rate, center in self._inbox:
                self._air.send(sender, t0 + guard, iq, rate=rate, center=center)
            self._inbox.clear()
        return self._air.capture(receiver, t0, n)

    def take_inbox(self) -> list:
        """Remove and return every burst received since the last call (each to be decoded on its own)."""
        got, self._inbox = self._inbox, []
        return got

    def capture_one(self, burst, receiver: int, t0: float, n: int) -> np.ndarray:
        """A capture window holding exactly ONE arrived burst (plus the receiver's noise), so bursts
        that arrived close together are decoded separately instead of piled into one window."""
        sender, _t, iq, rate, center = burst
        guard = 8.0 / self._air.rate
        self._air.send(sender, t0 + guard, iq, rate=rate, center=center)
        y = self._air.capture(receiver, t0, n)
        self._air.clear_before(t0 + n / self._air.rate + 1.0)   # drop it; it has been read
        return y

    def close(self) -> None:
        try:
            self.sock.close()
        except OSError:
            pass


__all__ = ["LanAir", "Peer", "encode_burst", "decode_burst"]
