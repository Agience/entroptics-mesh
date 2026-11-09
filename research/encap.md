# IP encapsulation over the mesh

Code: `src/entroptics_mesh/tunnel.py` (datagram tunnel), `src/entroptics_mesh/tcpproxy.py` (split
TCP), `src/entroptics_mesh/emu/ipnet.py` (pure-Python lossy message net for tests and
measurements). Tests: `tests/test_tunnel.py`, `tests/test_tcpproxy.py`. Measurements:
`research/encap/measure.py` -> `research/encap/results.txt`.

## What the mesh needs, and what it does not

The mesh delivers fountain-coded messages with no reply, so nothing in it needs a handshake,
an ack or a route. The tunnel keeps that. Best-effort IP (UDP, video, ICMP, anything the end
hosts already protect) travels with **no state and nothing sent back**. Reliability is
opt-in **per flow**, and only those flows ever send anything back: a tiny cumulative +
selective ACK.

| layer | carries | state | anything back? |
|---|---|---|---|
| mesh (`link`, `routing`) | messages, as fountain symbols | seen-cache only | no |
| tunnel best-effort | raw IP packets, packed | none | no |
| tunnel reliable | raw IP packets or proxy segments, seq-numbered | ~40 B per flow per end | ACK/NACK, piggybacked |
| tcpproxy | TCP byte streams, terminated at each edge | ~43 B per connection | through the reliable class |
| BGP | fleet prefix to the outside, between gateways | in the gateways only | ordinary BGP on the wire |

## Tunnel

**Addressing.** An IP prefix -> mesh address map, longest prefix wins (IPv4 and IPv6). For one
prefix: configured beats announced beats gleaned, newest among equals. Entries come from
three places, none of them pre-shared between drones:

- *configured* at the node that owns the prefix: a gateway's upstream (`0/0`), a drone's own
  payload LAN;
- *announced*: a ROUTES record broadcast in an ordinary tunnel message ("I serve these
  prefixes"; `to = me` costs no address bytes). Gateways announce `0.0.0.0/0 -> HOME` and
  `::/0 -> HOME`, so a drone that has heard any gateway sends everything else to HOME, and
  "home" is whichever gateway hears (anycast). Announced routes age out (`max_age`, 60 s), so
  a vanished node takes its routes with it with no withdrawal;
- *gleaned* (gateways only, `glean=True`): a drone's source address from the traffic it sends,
  aged out after 30 s. This makes the way back work before a drone announces anything.

**Wire format** (inside the sealed message; `v` = LEB128 varint):

```
message   u8 version<<4, then records to the end
record    u8 tag   kind(2: PKT FRAG ACK CTRL) | R reliable | S seq ("newest") | O ordered
                   | L length follows (absent on the last record) | A app stream | U urgent
          v  flow  sender-chosen flow id (CTRL: subtype)
          PKT/FRAG [u16 seq, v (seq - low-water) if R | u16 seq if S]
          FRAG     u16 packet id, u8 index, u8 count
          ACK      u16 cum; body = bitmap, bit i = cum+1+i received (up to 64)
          [v len]  body
```

A best-effort packet costs **2 bytes** of record header when last in its message (3 with the
message's version byte); a reliable one **5-7**. The sealing context is
`"tun" + origin + seq + to`, so a real AEAD provider rejects a message replayed under another
mesh header. Crypto is only ever the provider's (`NullCrypto` in tests; the tests also use a
test-only HMAC provider to show the binding).

**Packing.** A 512-byte frame costs the same whether its 475-byte block is full or not, so a
lone 64-byte packet uses 12.5% of what it pays for. The tunnel never holds a packet while the
radio is idle: `pull()` is called whenever the radio can take a message and packs everything
queued for one mesh address since the last one. So aggregation is whatever built up behind the
transmission ahead, and it costs no latency. When the backlog exceeds one message
(`max_blocks`, default 8 = 3.8 kB), the message ends where its last block is fullest. The record
that would have spilled into a mostly empty block leads the next message instead.
`hold > 0` lets a lone small packet wait for company (off by default: latency comes first).

**MTU.** Never IP-fragment. A packet that fits one message goes whole, and the fountain cuts the
message into symbols. A bigger one (up to `mtu`, default 9000) is cut into FRAG records across
messages and rebuilt in any order, with fragments of an incomplete packet dropped after
`frag_timeout`. A packet over `mtu` is refused, so the local stack's PMTUD sees the tunnel's
real limit (the TUN adapter should answer ICMP Packet Too Big / Frag Needed; not built).

**Best-effort class.** No sequence numbers, no state, no ack. A packet still queued past its
deadline (default 1 s, per packet) is dropped before it goes. Nodes have no shared clock, so the
receiver cannot judge age. Instead a flow may be marked `newest` (2-byte seq), and the receiver
drops anything older than what it already delivered. That suits video and telemetry, where late
means useless.

**Reliable class** (opt-in per flow). Built for "anything can happen":

- *low-water mark in every data record* (the oldest seq the sender still holds): a receiver
  that never saw the start, lost its state, or is a *different gateway* after failover
  synchronizes from any one record. No handshake;
- *sender may give up* (explicit deadline, or `lifetime` = 10 s after first send): the
  low-water mark moves past the record and the receiver stops waiting (an ordered flow releases
  what it held behind the hole). The TCP proxy treats that as a broken stream and resets;
- *random ISN*, and a reused flow id starts half the sequence space away, so a rebooted sender
  is a new epoch, not a run of duplicates;
- *ACK = cumulative + 64-seq bitmap*: any one surviving ACK says everything. An out-of-order
  arrival or a duplicate triggers one at once (the NACK). Otherwise one goes after
  `ack_delay` (50 ms) or every 16 records, and it piggybacks on any message already going to
  that node;
- *selective retransmit*: a hole is retransmitted once a record sent after it has been
  acknowledged (RACK-style, with a small reorder allowance), and never again until a later
  record proves it lost again. The *short timer* (RTO from SRTT/RTTVAR, 0.1-2 s, Karn) fires
  only on silence. It then probes with the oldest message's worth, not the whole window, and
  the bitmap in the answer says what else is missing;
- *adaptive copies*: a flow that has been losing (EWMA of records needing retransmission
  >= 10% / 30%) sends each retransmission in 2 / 3 separate messages. That spends airtime to
  cut the tail, since latency matters more;
- *state dropped on idle*: sender flow after 30 s with nothing outstanding, receiver flow after
  30 s silent (it must outlive the sender's `lifetime`, or a late retransmission could deliver
  twice).

**Multi-homing (`Backhaul`).** Every gateway accepts HOME. The first gateway to decode a message
hands it over the wire to the gateway *anchoring* that drone (the first live gateway that heard
it), and a copy decoded by another gateway is dropped. One drone's reliable flows therefore
keep their state in one place. If the anchor dies, the next gateway to hear the drone becomes
the anchor and resynchronizes from the low-water mark. Measured in tests: each packet is
delivered upstream once, and a reliable flow keeps going when its anchor dies mid-flow.

## TCP: split at the edges

End-to-end TCP across the mesh would read every lost message as congestion. Its window would
collapse and its RTO back off to seconds. So TCP never crosses the mesh. The drone's proxy
accepts the application's connection locally and at once. The gateway's proxy opens the real
connection to the destination. The byte stream between them rides the tunnel's reliable,
ordered class, one tunnel flow per direction per connection, so connections never block each
other.

Segments (application records): `OPEN` (address, port and the first data, like TFO, so a
request goes with the open, not a round trip later), `DATA`, `FIN`, `RST`. A connection is keyed
(initiator node, conn id), so either end may open one (a gateway can reach a service on a
drone). There is no open handshake over the mesh. The far proxy connects on OPEN and answers RST
only if it cannot. State per connection is 43 bytes (key, peer, flow, address, two FIN flags,
timestamp). It goes when both FINs have crossed, on RST, when the tunnel gives up a segment, or
after `idle` (120 s). Backpressure: `writable()` turns false while `window` segments are
unacknowledged, the socket owner stops reading, and local TCP flow control does the rest.

On failover, if the OPEN was still unacknowledged it is still in the retransmission, and the
connection opens on the new gateway intact. If it was acknowledged, the new gateway holds no
connection for the stream and answers RST, so the application sees a reset and not a hang. The
proxy is pure state machines (actions out: `Connect`, `Write`, `Close`, `Reset`). A socket/asyncio
adapter is not built.

## BGP

No BGP runs on drones. A drone's whole routing table is what the tunnel learns:
`0/0 -> HOME` from gateways, plus other drones' announced prefixes. BGP is a gateway concern:

1. **To the outside (eBGP from each gateway to its upstream).** Every gateway originates the
   same fleet prefixes (e.g. a public /24 and an IPv6 /48, or private space into an enterprise
   network). At the IP level this is anycast, the counterpart of HOME on the mesh side. Inbound
   traffic enters at whichever gateway the upstream prefers, and that gateway's address map
   (announced or gleaned) gives the drone. Origination is **conditional**: a gateway announces
   the fleet prefix only while its tunnel has heard a drone within T (a tracked object such as
   "any announcement received in 30 s"), and BFD on the upstream link catches a dead box in
   well under a second. A gateway whose radio is dead or out of range withdraws, so upstream
   stops sending it traffic it cannot deliver.
2. **Between gateways (iBGP over the wired backhaul).** Full mesh or a route reflector, plus BFD.
   This carries the fleet prefixes and, optionally, **host routes for drones each gateway hears
   well** (/32, /128, from the tunnel's gleaned/announced entries, LOCAL_PREF or MED from
   announced SNR). Inbound traffic is then steered to the gateway nearest the drone and does not
   rely on flooding from a far one. The same wire carries the tunnel's `Backhaul` (dedupe +
   anchor).
3. **Between gateways with no wire (over the mesh).** A gateway on a forward vehicle or relay with
   its own uplink peers with the others through the tunnel. There are two ways:
   - *proxied* (tcpproxy, port 179): best behaved, since BGP sees a clean local TCP and the
     mesh's tails are absorbed by the reliable class. But TCP-MD5 and TCP-AO are end-to-end TCP
     options and **do not survive a split proxy**. Authenticate with the crypto provider (the
     tunnel seals every message) and accept that, or use the next option;
   - *raw* (the BGP TCP packets as tunnel packets in the reliable class, unordered): end-to-end
     TCP, so MD5/AO work, and the tunnel repairs losses faster than TCP's RTO would.
   Either way, use loose timers (hold 90-180 s, keepalive 30-60 s), BFD only on wired links, and
   graceful restart / long-lived graceful restart so that a mesh outage of seconds to minutes
   does not flap the fleet prefix.

**What the tunnel must provide for this, and does:** the `0/0 -> HOME` route announcement
(CTRL ROUTES), gleaning and announced prefixes for the gateway's way back (the input to
conditional origination and host routes), the reliable class and the TCP proxy for mesh-borne
sessions, and the backhaul dedupe/anchor.

**One rule for proxied TCP:** the gateway proxy should connect out from **its own address**,
not transparently from the drone's. The return traffic of a proxied connection must reach the
gateway that holds the socket, and with the fleet prefix anycast from every gateway, only a
per-gateway source address guarantees that. Raw-IP flows (UDP, ICMP, raw reliable) use the
drone's own address and may return through any gateway.

## Measurements (`research/encap/measure.py`)

Model: a 475-byte fountain block costs one 512-byte frame on the air (35-byte symbol header,
which since 2026-10-16 carries the 2-byte time budget, + 2-byte CRC; link cost alone caps
efficiency at 92.8%). The tables below were measured with the earlier 477-byte block (33-byte
header, cap 93.2%), so they read about 0.4% high. Efficiency = IP bytes / frame bytes at
redundancy 1. Fountain redundancy multiplies every row alike.

### Overhead and efficiency per packet

"Single" = one packet per message (radio idle). "Packed" = a standing backlog (radio busy),
8-block cap, best-fill cut. "AEAD" adds 28 B per message (12 nonce + 16 tag) for a sealing
provider.

| IP pkt | class | tunnel hdr B/pkt single / packed | frames/pkt single | eff. single | frames/pkt packed | eff. packed | eff. packed, AEAD |
|---|---|---|---|---|---|---|---|
| 64 | best-effort | 3.0 / 3.0 | 1 | 12.5% | 0.145 | 86.2% | 86.2% |
| 64 | reliable | 6.0 / 6.4 | 1 | 12.5% | 0.150 | 83.3% | 83.3% |
| 200 | best-effort | 3.0 / 3.9 | 1 | 39.1% | 0.430 | 90.8% | 88.8% |
| 200 | reliable | 6.0 / 7.3 | 1 | 39.1% | 0.440 | 88.8% | 87.8% |
| 512 | best-effort | 3.0 / 3.9 | 2 | 50.0% | 1.145 | 87.3% | 87.3% |
| 512 | reliable | 6.0 / 7.2 | 2 | 50.0% | 1.145 | 87.3% | 87.3% |
| 1400 | best-effort | 3.0 / 3.0 | 3 | 91.1% | 3.000 | 91.1% | 91.1% |
| 1400 | reliable | 6.0 / 6.4 | 3 | 91.1% | 3.000 | 91.1% | 91.1% |

The tunnel's own bytes (3-7 per packet) are never the problem. Block padding is: small and
mid-size packets sent alone waste 50-88% of their frames, and packing recovers that to 83-91%,
within a few points of the link ceiling. The IP/UDP headers inside the packets (28 B v4,
48 B v6) are now the largest overhead left on small packets (see open issues: header
compression).

### Reliable class vs message loss

Model (`emu/ipnet.py`): two nodes, each radio 200 blocks/s, one-way delay 20 ms + up to 20 ms
jitter (reorders), each **message** lost independently with probability p, where p is the loss
*after* fountain decoding. Redundancy 1.5 on first sends and 2.0 on retransmissions; ACK
blocks are counted in airtime. 5 seeds.

Bulk: 400 x 1000-byte packets queued at t=0, one ordered flow.

| loss p | all delivered, in order | completion s | retransmits / pkt | ACK blocks / data blocks | goodput eff. (all air, both ways) | goodput kB/s |
|---|---|---|---|---|---|---|
| 0% | 5/5 | 7.58 | 0.00 | 0.103 | 47.1% | 53 |
| 5% | 5/5 | 8.23 | 0.07 | 0.106 | 43.3% | 49 |
| 20% | 5/5 | 11.83 | 0.44 | 0.106 | 30.8% | 34 |
| 40% | 5/5 | 19.81 | 1.14 | 0.084 | 20.0% | 20 |

At 0% loss the 47% is packing (90%) / redundancy (1.5) / ACK airtime (+10%). The floor for
retransmissions is p/(1-p) = 0.05 / 0.25 / 0.67 per packet. The adaptive copies spend roughly
1.7x that at 20-40% loss to shorten the tail.

Paced: 50 x 200-byte packets/s for 8 s, one ordered flow. Latency is local send to in-order
delivery (including head-of-line wait behind a repaired hole). Each latency column is the worst
of 5 seeds; the retransmits column is the mean.

| loss p | delivered | retransmits / pkt | p50 ms | p95 ms | p99 ms | max ms |
|---|---|---|---|---|---|---|
| 0% | 100% | 0.00 | 44 | 52 | 52 | 52 |
| 5% | 100% | 0.09 | 48 | 146 | 208 | 254 |
| 20% | 100% | 0.46 | 114 | 224 | 398 | 458 |
| 40% | 100% | 1.16 | 184 | 528 | 718 | 778 |

The one-way floor here is about 44 ms. Repair costs about one RTT plus a short wait. Even at 40%
message loss nothing is lost and the worst packet is under 0.8 s.

### Per-flow state

| state | essential bytes | CPython object, idle |
|---|---|---|
| best-effort flow | 0 (`newest` flows: 6 at the receiver) | 0 |
| reliable sender, idle | 38 (flow 2, to 8, next seq 2, srtt/rttvar/rto 12, last-acked-send 4, loss 2, touched 4, ISN memo 4) | 975 B |
| reliable sender, per record in flight | 16 + the packet (seq 2, deadline 4, first/last send 8, sends 1, flags 1) | -- |
| reliable receiver | 19 (cum 2, 64-seq bitmap 8, ack due 4, pending 1, touched 4) + held records | 793 B |
| TCP proxy connection | 43 (key 10, peer 8, flow 2, address 16 + port 2, flags 1, touched 4) + one sender + one receiver flow | 1053 B |

All of it goes after 30 s of silence (120 s for a proxied connection). A dead peer costs a
sender at most `lifetime` (10 s) and a handful of probes (test: 10 records to a dead node, at
most 40 transmissions in total, then nothing).

## Open issues

- **Wired to the node runtime; no TUN adapter yet.** `Node.attach(tunnel)` turns `Tunnel.pull()`
  into mesh symbols and hands decoded messages to `Tunnel.receive` (test_node: a UDP datagram end
  to end over the emulated air). A TUN/asyncio socket adapter does not exist yet, and the
  measurements below are on the message-level model, not RF.
- **Relays drop stale traffic -- resolved 2026-10-16.** The mesh header carries a time budget
  (`Header.budget_ms`); each node subtracts only its own holding time, so no clock is shared,
  and a message of best-effort records carries its latest deadline as that budget. Reliable,
  ACK and control records travel without one.
- **No congestion control / airtime fairness.** The reliable window (256 records) is a
  flow-control cap. Many flows or nodes sharing one channel need an airtime-fair scheduler
  across flows (the model gives every node its own radio, with no contention).
- **Proxied TCP dies with its anchor gateway.** Datagrams fail over without loss, but proxied
  connections whose OPEN was acknowledged are reset. Replicating proxy state over the backhaul
  would fix this and is not done.
- **Header compression.** IP/UDP headers are 28-48 B of every small packet. A ROHC-like
  per-flow context would conflict with "state dropped on idle" and lossy delivery. A
  stateless scheme (elide fields the mesh header already implies) is worth measuring.
- **Adaptive copies are a heuristic** (thresholds at 10% / 30%). At 20% loss they retransmit
  about 1.8x the minimum. Tuning against delivery-latency targets per class is open.
- **Gleaning trusts the origin.** Under `NullCrypto` any node can claim any source address.
  An authenticating crypto provider closes this, since the origin is bound in the sealing
  context.
- **The 64-seq ACK bitmap** limits selective repair to holes within 64 records of `cum`. Beyond
  that, the RTO probe recovers it in more round trips. A larger bitmap is a one-line change if
  windows grow.
