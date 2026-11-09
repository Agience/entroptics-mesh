"""
aead.py -- a sealing :class:`~entroptics_mesh.providers.CryptoProvider`: AEAD under a pre-shared
mesh key, with replay refusal.

Sealed form (bytes)::

    sender  8   random per provider instance, drawn at construction (and again if the counter runs out)
    counter 4   big-endian, one per seal, never reused under this sender id
    body    n   the AEAD ciphertext of the plaintext, with ``context`` as associated data
    tag     16

``sender || counter`` is the 96-bit AEAD nonce, so every seal under one key has a fresh nonce as
long as no two instances draw the same 64-bit sender id.  ``open`` authenticates first and only
then consults the replay window, so a forgery cannot move it.  The window is per sender id: the
highest counter accepted plus a bitmap of the ``window`` counters below it; a counter already
accepted, or older than the window, is refused.  A mesh delivers out of order (fountain symbols,
several paths), so the window admits a late message as long as fewer than ``window`` newer ones
from the same sender were accepted before it.

The mesh opens each message once per node (relays de-duplicate by message id and only the
destination opens), so one provider instance belongs to one node: two nodes sharing an instance
would share a replay window and refuse each other's first open.

The key is shared by every node that may read the payload.  A relay that holds a different key, or
the null provider, forwards the sealed bytes unopened: relaying runs on the header, which is bound
(``context``) but not hidden.  The physical-layer key (``waveform_key``) is a separate input, so a
mesh can share one aperture key across every relay while only the end points hold the sealing key.

No key exchange: keys are pre-shared out of band, as the reply-free mesh requires.  The replay
window lives in memory, so a restarted receiver accepts counters it had seen before the restart.
"""
from __future__ import annotations

import os

try:
    from cryptography.exceptions import InvalidTag
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM, ChaCha20Poly1305
except ImportError as e:                       # the optional extra is not installed
    raise ImportError("entroptics_mesh.ext.aead needs the 'crypto' extra: "
                      "pip install entroptics-mesh[crypto]") from e

SENDER = 8                                     # bytes of the per-instance sender id
COUNTER = 4                                    # bytes of the per-seal counter
NONCE = SENDER + COUNTER                       # the AEAD's 96-bit nonce
TAG = 16
OVERHEAD = NONCE + TAG                         # bytes a seal adds (``tunnel.TunnelConfig.seal_overhead``)
WINDOW = 1024                                  # counters per sender accepted out of order

_CIPHERS = {"chacha20poly1305": ChaCha20Poly1305, "aes-256-gcm": AESGCM}


class _Window:
    """One sender's replay window: the highest counter accepted and a bitmap of the ``size``
    counters at and below it (bit i set = counter ``top - i`` accepted)."""
    __slots__ = ("top", "bits", "size")

    def __init__(self, size: int):
        self.top, self.bits, self.size = -1, 0, int(size)

    def fresh(self, n: int) -> bool:
        if n > self.top:
            return True
        d = self.top - n
        return d < self.size and not (self.bits >> d) & 1

    def accept(self, n: int) -> None:
        if n > self.top:
            shift = n - self.top
            self.bits = ((self.bits << shift) | 1) & ((1 << self.size) - 1) if shift < self.size else 1
            self.top = n
        else:
            self.bits |= 1 << (self.top - n)


class AeadCrypto:
    """Seals with ChaCha20-Poly1305 (default) or AES-256-GCM under a 32-byte pre-shared ``key``.

    ``waveform_key`` is the physical layer's key, returned as given (None: the public zero-state
    aperture).  ``window`` is the replay window per sender.  ``address``/``resolve`` are raw, as
    under the null provider."""

    OVERHEAD = OVERHEAD

    def __init__(self, key: bytes, *, waveform_key=None, cipher: str = "chacha20poly1305",
                 window: int = WINDOW):
        key = bytes(key)
        if len(key) != 32:
            raise ValueError("the mesh key is 32 bytes")
        if cipher not in _CIPHERS:
            raise ValueError(f"cipher is one of {sorted(_CIPHERS)}")
        if int(window) < 1:
            raise ValueError("the replay window holds at least one counter")
        self.name = f"aead-{cipher}"
        self._aead = _CIPHERS[cipher](key)
        self._phy = waveform_key
        self._size = int(window)
        self._sender = os.urandom(SENDER)
        self._counter = 0
        self._seen: dict[bytes, _Window] = {}

    def seal(self, plaintext: bytes, *, context: bytes) -> bytes:
        if self._counter >= 1 << (8 * COUNTER):            # counter space spent: a fresh sender id
            self._sender, self._counter = os.urandom(SENDER), 0
        nonce = self._sender + self._counter.to_bytes(COUNTER, "big")
        self._counter += 1
        return nonce + self._aead.encrypt(nonce, bytes(plaintext), bytes(context))

    def open(self, sealed: bytes, *, context: bytes) -> bytes | None:
        sealed = bytes(sealed)
        if len(sealed) < OVERHEAD:
            return None
        nonce = sealed[:NONCE]
        sender, n = nonce[:SENDER], int.from_bytes(nonce[SENDER:], "big")
        w = self._seen.get(sender)
        if w is not None and not w.fresh(n):
            return None
        try:
            plain = self._aead.decrypt(nonce, sealed[NONCE:], bytes(context))
        except InvalidTag:
            return None
        if w is None:
            w = self._seen[sender] = _Window(self._size)
        w.accept(n)
        return plain

    def waveform_key(self, context: bytes):
        return self._phy

    def address(self, identity: bytes, *, context: bytes = b"") -> bytes:
        return bytes(identity)

    def resolve(self, token: bytes, *, context: bytes = b"") -> bytes | None:
        return bytes(token)


__all__ = ["AeadCrypto", "OVERHEAD", "WINDOW"]
