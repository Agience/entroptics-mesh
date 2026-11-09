"""
schedules.py -- a minimal keyed transmit schedule: the example 7c :class:`AgilityProvider`.

This ships as a demonstration of the schedule plug point, not as a production scheduler -- an
integrator writes the real one (the same split as the crypto provider: the core offers the socket,
the user supplies the algorithm).  It does one thing: it keys the sender's transmit start offset,
within the node's contention window, on a keyed PRNG over ``(key, sender id, coarse epoch)``.

Why that is the whole of 7c's support.  A listening node draws its start at random inside its
contention window every burst (``Node.start``), so the burst's arrival start phase jitters across
the whole row and the previous burst's lock is useless as a prior (research/realtime/port_results.md,
"CRITICAL FINDING").  Replace that random draw with a keyed one and the start still looks random to
anyone without the key -- it is a keyed-PRNG sequence, no period, different per key -- but a receiver
holding the same key reproduces it: it predicts ``who``'s offset for the slot it is reading
(``predict_offset``), and pairs that with its last measured lock of ``who`` to predict the arrival
start phase to within a few samples (the node does the pairing; this provider only supplies the
offset).  The receive path then fires its local scan on every burst, not only on a stable-clock one.

The key is this provider's own, like a crypto key; nothing is sent to a peer.  ``epoch_s`` is the
coarse shared clock the schedule is quantized on -- set it to the slot cadence so each burst draws a
fresh offset and sender and receiver agree on which slot's offset it is (they see the same slot
time).  The schedule needs the key plus that loose shared clock and nothing back from any peer, so
it works for a listen-only receiver -- exactly the one-way contract every provider keeps.

The example leaves ``plan`` as the core default: it coordinates WHEN a burst goes out, not how wide
or where.  A real provider can do both at once (keyed width/centre hop AND keyed timing).
"""
from __future__ import annotations

import hashlib
import struct

from .providers import PlanContext, TxPlan


def _as_bytes(key) -> bytes:
    if isinstance(key, (bytes, bytearray)):
        return bytes(key)
    if isinstance(key, int):
        return key.to_bytes((key.bit_length() + 8) // 8 or 1, "big", signed=key < 0)
    return str(key).encode()


class KeyedSchedule:
    """An :class:`~entroptics_mesh.providers.AgilityProvider` that supplies a keyed transmit start.

    ``key``     the schedule's own secret (bytes/int/str); the receiver holds the same one.
    ``epoch_s`` the coarse clock the schedule is quantized on -- one offset per ``epoch_s`` of the
                shared clock.  Set it to the node slot cadence so each slot (each burst) draws a
                fresh offset; sender and receiver read the same slot time, so they derive one epoch.

    The start offset for ``(node_id, t)`` is ``prng(key, node_id, round(t / epoch_s))`` scaled into
    ``[0, window_s)``.  Deterministic across processes (``blake2b``, not Python's salted ``hash``),
    so a sender and a keyed receiver on separate nodes agree without exchanging anything.
    """
    name = "keyed-schedule"

    def __init__(self, key, *, epoch_s: float = 1.0):
        self.key = _as_bytes(key)
        self.epoch_s = float(epoch_s)

    def plan(self, intent, default: TxPlan, table, ctx: PlanContext | None = None) -> TxPlan:
        return default                                  # timing only; width/centre left to the core

    def transmit_offset(self, ctx: PlanContext, *, window_s: float) -> float | None:
        if ctx is None or ctx.node_id is None:
            return None
        return self._offset(ctx.node_id, ctx.t, window_s)

    def predict_offset(self, who: bytes, t: float, *, window_s: float) -> float | None:
        if who is None:
            return None
        return self._offset(who, t, window_s)

    def _offset(self, node_id: bytes, t: float, window_s: float) -> float:
        epoch = int(round(float(t) / self.epoch_s)) if self.epoch_s > 0 else 0
        return self.unit(node_id, epoch) * float(window_s)

    def unit(self, node_id: bytes, epoch: int) -> float:
        """The schedule's raw draw in ``[0, 1)`` for a sender and epoch -- exposed so a detectability
        check can read the sequence directly."""
        h = hashlib.blake2b(digest_size=8)
        h.update(self.key)
        h.update(bytes(node_id))
        h.update(struct.pack(">q", int(epoch)))
        return int.from_bytes(h.digest(), "big") / 2.0 ** 64


__all__ = ["KeyedSchedule"]
