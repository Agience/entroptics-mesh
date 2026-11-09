"""
l2bridge.py -- an Ethernet-over-mesh learning bridge: the OS-integration layer that lets a device
behind one node's eth join a subnet served behind another node, over the mesh.

The mesh carries whole Ethernet FRAMES (L2), not IP -- so a real DHCP router behind one node serves a
subnet, and a device behind another node gets a lease on it and is reachable on it, the subnet
isolated from the management LAN.  A frame read off the local TAP is sent as a mesh message; a frame
delivered from the mesh is written to the TAP.  Addressing is a learning switch, distributed over the
mesh: each node maps a MAC to the mesh node that last sourced it, floods what it has not learned (and
all broadcast/multicast, so DHCP and ARP work), and unicasts what it has.  Nothing is pre-shared and
nothing comes back -- it rides the mesh's own reply-free, best-effort delivery (the tunnel's reliable
class is a later add for TCP throughput).

This is NOT the IP tunnel (:mod:`tunnel`, which is L3 with prefix addressing); L2 bridging needs MAC
forwarding, not IP routing.  The pure logic (``MacTable``, frame addressing) is cross-platform and
unit-tested; the TAP device and the run loop are Linux-only (``/dev/net/tun``, CAP_NET_ADMIN) and
import their platform bits lazily so this module still loads elsewhere.
"""
from __future__ import annotations

import os
import struct

from .routing import BROADCAST

ETH_ALEN = 6
BROADCAST_MAC = b"\xff" * ETH_ALEN


def is_group_mac(mac: bytes) -> bool:
    """Broadcast or multicast: the low bit of the first octet is the group bit (ff:ff:.. is the
    broadcast case of it).  Group frames flood the mesh so DHCP and ARP reach every segment."""
    return bool(mac[0] & 0x01)


def dst_mac(frame: bytes) -> bytes:
    return bytes(frame[:ETH_ALEN])


def src_mac(frame: bytes) -> bytes:
    return bytes(frame[ETH_ALEN:2 * ETH_ALEN])


class MacTable:
    """A learning forwarding database: MAC -> mesh node id, aged by last-seen.  One per bridge node;
    together they are a switch distributed over the mesh."""

    def __init__(self, age_s: float = 300.0):
        self.age_s = float(age_s)
        self._t: dict[bytes, tuple[bytes, float]] = {}

    def learn(self, mac: bytes, node: bytes, now: float) -> None:
        if not is_group_mac(mac):                       # never learn a group address as a source
            self._t[bytes(mac)] = (bytes(node), float(now))

    def lookup(self, mac: bytes, now: float) -> bytes | None:
        e = self._t.get(bytes(mac))
        if e is None:
            return None
        node, t = e
        if now - t > self.age_s:
            del self._t[bytes(mac)]
            return None
        return node

    def __len__(self) -> int:
        return len(self._t)


def frame_dest(frame: bytes, table: MacTable, now: float) -> bytes:
    """The mesh destination for an outbound frame: the learned node for a known unicast MAC, else
    BROADCAST -- a learning switch floods group frames and anything it has not learned yet."""
    d = dst_mac(frame)
    if is_group_mac(d):
        return BROADCAST
    return table.lookup(d, now) or BROADCAST


# ---- Linux TAP device (guarded: imports its platform bits only when instantiated) ----------------

_TUNSETIFF = 0x400454CA
_IFF_TAP = 0x0002
_IFF_NO_PI = 0x1000


class Tap:
    """A Linux TAP device: reads and writes raw Ethernet frames.  Linux only (``/dev/net/tun``,
    needs CAP_NET_ADMIN).  Bring the interface up and add it to a bridge from the daemon/setup."""

    def __init__(self, name: str = "emtap0", read_size: int = 2048):
        import fcntl                                    # Unix-only; imported here so the module loads elsewhere
        self.fd = os.open("/dev/net/tun", os.O_RDWR)
        ifr = struct.pack("16sH", name.encode(), _IFF_TAP | _IFF_NO_PI)
        fcntl.ioctl(self.fd, _TUNSETIFF, ifr)
        self.name = name
        self.read_size = int(read_size)

    def read(self) -> bytes:
        return os.read(self.fd, self.read_size)

    def write(self, frame: bytes) -> None:
        os.write(self.fd, frame)

    def fileno(self) -> int:
        return self.fd

    def close(self) -> None:
        try:
            os.close(self.fd)
        except OSError:
            pass


class L2Bridge:
    """Wires a :class:`Tap` to a mesh node: frames off the TAP go out as mesh messages (flood or the
    learned node), and frames delivered from the mesh are learned and written to the TAP.  The node's
    ``on_message`` is taken over; the daemon pumps the TAP (:meth:`forward` on a readable fd) and
    drives the node's own loop."""

    def __init__(self, node, tap, now_fn):
        self.node = node
        self.tap = tap
        self.now_fn = now_fn                            # () -> float, the node's clock
        self.table = MacTable()
        self.tx = 0
        self.rx = 0
        node.on_message = self._on_mesh

    def _on_mesh(self, delivered) -> None:
        """A mesh message carrying a frame: learn its source MAC on the origin node, write it out."""
        frame = delivered.payload
        if len(frame) >= 2 * ETH_ALEN:
            self.table.learn(src_mac(frame), delivered.origin, self.now_fn())
            self.tap.write(frame)
            self.rx += 1

    def forward(self) -> None:
        """One readable frame off the TAP -> a mesh message toward its destination (flood or learned)."""
        frame = self.tap.read()
        if len(frame) < 2 * ETH_ALEN:
            return
        now = self.now_fn()
        self.node.send(frame, frame_dest(frame, self.table, now), now)
        self.tx += 1


__all__ = ["ETH_ALEN", "BROADCAST_MAC", "is_group_mac", "dst_mac", "src_mac",
           "MacTable", "frame_dest", "Tap", "L2Bridge"]
