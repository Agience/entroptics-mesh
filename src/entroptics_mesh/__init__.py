"""
entroptics-mesh -- a one-way mesh over the entroptics-rf physical layer.

Dependency graph: entroptics <- entroptics-rf <- entroptics-mesh.

What it is built on, and what it refuses:

    nothing is pre-shared     no synced clocks, no schedules, no agreed channel plan; a receiver
                              measures each sender (its scale, width, carrier) from the capture
    nothing is required back  no acks, no handshakes, no echoes, no decode-quality reports; a
                              listen-only node decodes as fully as any other
    reliability is forward    FEC inside a packet, fountain coding across packets, and relaying
                              over more than one path
    the core is pluggable     encryption, frequency agility and interference handling are providers the user
                              plugs in; the core ships interfaces and null providers, and works
                              fully (as an open broadcast mesh) with the null ones

Modules: ``providers`` (the plug points), ``neighbors`` (what each sender was measured to be, and
what it announced), ``announce`` (the optional "I'm here"), ``routing`` (reply-free flooding),
``fec`` (codes inside and across packets), ``tunnel`` (IP packets over the mesh: prefix map,
packing, best-effort and optional reliable classes), ``tcpproxy`` (TCP split at the mesh edges),
``relay`` (the measured relaying rules), ``node`` (one node's runtime: captures in, bursts out).
"""
from __future__ import annotations

__version__ = "0.1.0"
