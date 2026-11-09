"""
run_bridge.py -- the real-time daemon that bridges a host's Ethernet onto the mesh.

It ties together the four pieces built elsewhere: a :class:`~entroptics_mesh.l2bridge.Tap` (the host's
``emtap0``), a :class:`~entroptics_mesh.node.Node` (one mesh node), a
:class:`~entroptics_mesh.emu.lan_air.LanAir` (the emulated air carrying the waveform to the peer
hosts over the LAN), and an :class:`~entroptics_mesh.l2bridge.L2Bridge` (frames <-> mesh messages).
One wall-clock loop services three things: a frame readable on the TAP is sent onto the mesh; a
datagram readable on the LAN socket is taken into the air; and on each slot boundary the node
transmits whatever is due, reads what it heard, and ticks.

Linux only (the TAP needs ``/dev/net/tun`` and CAP_NET_ADMIN).  The TAP, the bridge (``br0`` =
``eth0`` + ``emtap0``) and any netns/dnsmasq are created by the host setup step, not here; this
daemon opens an already-created TAP and runs.  :class:`RealtimeRunner` keeps the per-slot node drive
(:meth:`RealtimeRunner.step`) free of the wall clock so it is driven deterministically in tests.
"""
from __future__ import annotations

import argparse
import json
import os

from .adapt import Radio as AdaptRadio
from .emu.air import Radio as AirRadio
from .emu.lan_air import LanAir, Peer
from .l2bridge import L2Bridge
from .neighbors import Position
from .providers import NullCrypto, Providers
from .schedules import KeyedSchedule

# The default PHY sub-bands and waveform key: every node on the mesh must share them to read the
# waveform (this is the PHY waveform key, not payload sealing -- a separate concern).
_SUB_BANDS = ((64, 0.5, 0.25), (64, 0.5, -0.25))
_DEFAULT_KEY = 0x00E7


class FixedWaveformKey(NullCrypto):
    """A crypto provider with one shared PHY waveform key for the whole demo mesh.  Payload sealing
    stays NullCrypto; only the waveform key is pinned so both hosts' PHY agree."""
    name = "fixed-waveform-key"

    def __init__(self, key: int = _DEFAULT_KEY):
        self._key = int(key)

    def waveform_key(self, ctx):
        return self._key


class RealtimeRunner:
    """Drives one node's slot loop and pumps the TAP and the LAN air.  ``step`` is one slot and takes
    the time explicitly; ``run`` is the wall-clock loop that calls it on the slot grid while servicing
    the TAP and the socket in between."""

    def __init__(self, node, air: LanAir, bridge: L2Bridge, local_index: int, *, slot_s: float, n: int,
                 max_backlog: int = 3, tx_period: float | None = None):
        self.node = node
        self.air = air
        self.bridge = bridge
        self.local_index = int(local_index)
        self.slot_s = float(slot_s)
        self.n = int(n)
        self.slots = 0
        self.max_backlog = int(max_backlog)   # bursts to decode per wake; excess is dropped (a fade)
        # Minimum wall-clock spacing between our own bursts.  Decode is CPU-bound and far slower than
        # a PHY slot, so transmitting every slot would swamp the peer's decoder; pacing tx keeps the
        # two sides balanced (throughput is traded for reliable delivery -- fine for ARP/DHCP/ping).
        self.tx_period = float(tx_period) if tx_period is not None else max(float(slot_s), 0.15)
        self.bk_drop = 0
        self._next_tx = 0.0

    def step(self, now: float) -> bool:
        """One slot at time ``now``: transmit a due burst (if any), otherwise read what was heard,
        then tick.  Half duplex -- a slot the node transmits in, it does not also capture.  Returns
        whether a burst was sent."""
        got = self.node.next_burst(now)
        sent = got is not None
        if sent:
            iq, plan = got
            amp = 10.0 ** (min(plan.power_db, 0.0) / 20.0)
            off = self.node.scheduled_tx_offset(now)   # keyed start within the contention window; 0.0
            self.air.send(self.local_index, now + off, iq * amp, rate=plan.rate, center=plan.center)
        else:
            y = self.air.capture(self.local_index, now, self.n)
            self.node.on_capture(y, now)
        self.node.tick(now)
        self.air.clear_before(now)
        self.slots += 1
        return sent

    def run(self, clock, stop=None) -> None:
        """The wall-clock loop.  ``clock`` returns seconds (e.g. ``time.monotonic``); ``stop`` ends
        it.  Event-driven: ``select`` wakes on a TAP frame, a LAN datagram, or the next transmit
        opportunity.  Each arrived burst is decoded in its OWN capture (so bursts that arrive close
        together are not piled into one window), and the node transmits a due burst at most once per
        slot period (it self-paces its own redundancy via its backoff)."""
        import os
        import select                                     # stdlib, but keep the import at the edge
        import sys
        tap_fd = self.bridge.tap.fileno()
        sock_fd = self.air.fileno()
        stats_every = float(os.environ.get("RUN_BRIDGE_STATS", "0") or 0)
        self._next_tx = clock()
        next_stats = clock() + stats_every if stats_every else float("inf")
        while stop is None or not stop():
            timeout = min(self._next_tx, next_stats) - clock()
            timeout = min(max(timeout, 0.005), 0.05)       # floor avoids idle busy-spin; cap keeps tx prompt
            try:
                readable, _, _ = select.select([tap_fd, sock_fd], [], [], timeout)
            except (OSError, ValueError):
                break
            if tap_fd in readable:                         # a frame from the local side -> queue to send
                self.bridge.forward()
            if sock_fd in readable:                        # drain every waiting datagram into the inbox
                self.air.pump()
            inbox = self.air.take_inbox()
            if len(inbox) > self.max_backlog:              # can't decode in real time -> drop excess (a
                self.bk_drop += len(inbox) - self.max_backlog   # fade); the newest copies are kept
                inbox = inbox[-self.max_backlog:]
            for burst in inbox:                            # decode each arrival on its own window
                now = clock()
                self.node.on_capture(self.air.capture_one(burst, self.local_index, now, self.n), now)
                self._maybe_tx(clock())                    # interleave tx so a reply is not starved
            now = clock()
            self._maybe_tx(now)
            if now >= next_stats:
                s = self.node.stats
                sys.stderr.write(
                    f"[stats] slots={self.slots} tap_in={self.bridge.tx} tap_out={self.bridge.rx} "
                    f"air_tx={self.air.tx} air_rx={self.air.rx} bk_drop={self.bk_drop} "
                    f"bursts={s.bursts} captures={s.captures} gated={s.gated} "
                    f"fast_hits={s.fast_hits} macs={len(self.bridge.table)}\n")
                sys.stderr.flush()
                next_stats = now + stats_every

    def _maybe_tx(self, now: float) -> None:
        """Transmit one due burst if the slot period has elapsed; the node self-paces its redundancy."""
        if now < self._next_tx:
            return
        got = self.node.next_burst(now)
        if got is not None:
            iq, plan = got
            amp = 10.0 ** (min(plan.power_db, 0.0) / 20.0)
            off = self.node.scheduled_tx_offset(now)   # keyed start within the contention window; 0.0
            self.air.send(self.local_index, now + off, iq * amp, rate=plan.rate, center=plan.center)
        self.node.tick(now)
        self.slots += 1
        self._next_tx = now + self.tx_period


# ---- wiring from a shared mesh config -------------------------------------------------------------

def build(config: dict, index: int, tap, peers: list[Peer], *, bind=("0.0.0.0", 0), now_fn=None):
    """Build ``(node, air, bridge, runner)`` for the local node ``index`` from a shared mesh config
    (identical on every host).  ``tap`` is an already-opened TAP (or a test double with read/write/
    fileno).  Does not touch the network beyond binding the LAN air's UDP socket."""
    from .emu.node_run import slot_samples

    freq_hz = float(config.get("freq_hz", 915e6))
    rate = float(config.get("rate", 2.4e6))
    key = int(config.get("waveform_key", _DEFAULT_KEY))
    rc = config.get("radio", {})
    adapt_radio = AdaptRadio(min_rate=float(rc.get("min_rate", 1.2e6)),
                             max_rate=float(rc.get("max_rate", rate)),
                             center_hz=float(rc.get("center_hz", freq_hz)),
                             max_power_dbm=float(rc.get("max_power_dbm", 20.0)),
                             noise_dbm=float(rc.get("noise_dbm", -100.0)))
    bands = config.get("sub_bands", _SUB_BANDS)

    import numpy as np
    from . import phy
    air_radios = []
    for r in config["radios"]:
        air_radios.append(AirRadio(position=np.array([float(r["x"]), float(r["y"]), float(r.get("z", 100.0))]),
                                   tx_power_dbm=float(r.get("tx_dbm", 20.0)),
                                   noise_dbm=float(r.get("noise_dbm", -100.0))))
    me = config["radios"][index]
    node_id = _node_id(me.get("node_id", f"node{index}"))
    position = _position(me)

    air = LanAir(air_radios, local={index}, peers=peers, bind=bind, freq_hz=freq_hz, rate=rate,
                 seed=index, now_fn=now_fn)
    # Optional roadmap-7c keyed transmit schedule (DEFAULT OFF).  When on, a KeyedSchedule keyed on
    # the SAME shared waveform key is attached as the agility provider: the sender draws its start
    # offset from the keyed PRNG (random-looking on air) and a peer running the same provider with
    # the same key predicts it, feeding the receive path's §3a local scan (see node.py / schedules.py
    # / research/realtime/port_results.md §7c).  It derives from the shared key alone -- nothing is
    # pre-shared beyond the key, nothing comes back from a peer.  Off (the default) attaches NullAgility,
    # draws no keyed offset, and is byte-identical to a mesh without the hook.
    schedule = KeyedSchedule(key) if _keyed_schedule_on(config) else None
    node = _make_node(node_id, adapt_radio, position, key, seed=10 + index,
                      announce_every=config.get("announce_every"),
                      redundancy=config.get("redundancy"), schedule=schedule)
    for b in bands:
        node.learn(phy.Known(int(b[0]), float(b[1]), float(b[2])))
    bridge = L2Bridge(node, tap, now_fn=now_fn or (lambda: 0.0))

    n = slot_samples(rate, adapt_radio.min_rate, families={f for f in node.families})
    slot_s = n / rate
    node.slot_s = slot_s
    if schedule is not None:
        # Quantize the schedule on the slot cadence so each burst draws a fresh offset and sender and
        # receiver agree on which slot's offset it is (the node reads epoch_s at call time, so no rebind).
        schedule.epoch_s = slot_s
    runner = RealtimeRunner(node, air, bridge, index, slot_s=slot_s, n=n,
                            tx_period=config.get("tx_period"))
    return node, air, bridge, runner


def _keyed_schedule_on(config: dict) -> bool:
    """Whether the roadmap-7c keyed transmit schedule is enabled -- DEFAULT OFF.  The shared mesh
    config's ``keyed_schedule`` turns it on; the ``ENTROPTICS_KEYED_SCHEDULE`` env var overrides it
    (``1``/``on``/``true``/``yes`` on, anything else off) for an operator who wants to flip it per host
    without editing the shared config."""
    env = os.environ.get("ENTROPTICS_KEYED_SCHEDULE")
    if env is not None and env.strip() != "":
        return env.strip().lower() in ("1", "on", "true", "yes")
    return bool(config.get("keyed_schedule", False))


def _make_node(node_id, radio, position, key, *, seed, announce_every=None, redundancy=None,
               schedule=None):
    from .node import Node
    kw = {} if redundancy is None else {"redundancy": float(redundancy)}
    providers = (Providers(crypto=FixedWaveformKey(key), agility=schedule) if schedule is not None
                 else Providers(crypto=FixedWaveformKey(key)))
    return Node(node_id, radio, slot_s=0.0, position=position,
                providers=providers, seed=seed, blind_scaled=False,
                announce_every=announce_every, **kw)


def _node_id(s: str) -> bytes:
    b = s.encode()
    return (b + b"\x00" * 8)[:8]


def _position(r: dict) -> Position:
    """A node's geographic position.  If the config gives lat/lon use them; else map the planar
    x/y (metres) to small-angle lat/lon about the origin, as the emulator's tests do."""
    import math
    if "lat" in r and "lon" in r:
        return Position(float(r["lat"]), float(r["lon"]), float(r.get("alt", r.get("z", 100.0))))
    x, y, z = float(r["x"]), float(r["y"]), float(r.get("z", 100.0))
    return Position(math.degrees(y / 6_371_000.0), math.degrees(x / 6_371_000.0), z)


def _parse_hostport(s: str) -> Peer:
    host, _, port = s.rpartition(":")
    return Peer(host or "0.0.0.0", int(port))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Bridge this host's Ethernet onto the Entroptics mesh.")
    ap.add_argument("--config", required=True, help="shared mesh config JSON (identical on every host)")
    ap.add_argument("--index", type=int, required=True, help="this host's radio index in the config")
    ap.add_argument("--tap", default="emtap0", help="TAP interface name (already created and up)")
    ap.add_argument("--bind", default="0.0.0.0:0", help="host:port to receive peer bursts on")
    ap.add_argument("--peer", action="append", default=[], help="host:port of a peer host (repeatable)")
    args = ap.parse_args(argv)

    import time

    from .l2bridge import Tap
    with open(args.config, "r", encoding="utf-8") as fh:
        config = json.load(fh)
    bind = _parse_hostport(args.bind)
    peers = [_parse_hostport(p) for p in args.peer]
    tap = Tap(name=args.tap)
    _, air, _, runner = build(config, args.index, tap, peers,
                              bind=(bind.host, bind.port), now_fn=time.monotonic)
    try:
        runner.run(clock=time.monotonic)
    finally:
        air.close()
        tap.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
