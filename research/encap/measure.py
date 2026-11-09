"""Tunnel overhead, packing, reliable-class goodput under message loss, and per-flow state.
Pure Python on the IpNet message model (no RF).  Writes results.txt next to this file.

Conventions: a fountain block is 477 bytes and costs one 512-byte protected frame on the air
(``tunnel.FRAME``/``BLOCK``, from emu.link_run); efficiency = IP bytes / frame bytes at
redundancy 1 (fountain redundancy multiplies every row alike).  Under the null crypto provider a
message is sealed at its own size; an AEAD provider adds its nonce + tag per message (28 bytes
modelled: 12 + 16), which packing also amortizes."""
import math
import random
import statistics as st
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
from entroptics_mesh.emu.ipnet import IpNet
from entroptics_mesh.tcpproxy import Conn, state_size
from entroptics_mesh.tunnel import BLOCK, FRAME, RecvFlow, SendFlow, Tunnel, TunnelConfig

A, B = b"drone-A.", b"drone-B."
lines = []


def out(s=""):
    print(s)
    lines.append(s)


def pkt(n, i=0):
    return bytes([0x45, i & 255]) + bytes(n - 2)


def efficiency(size, aggregate, reliable, seal):
    """IP bytes per frame byte: one packet per message (no aggregation), or a standing backlog
    packed into 4-block messages."""
    t = Tunnel(A, config=TunnelConfig(seal_overhead=seal), rng=random.Random(0))
    n = 200 if aggregate else 1
    for i in range(n):
        t.send(pkt(size, i), 0.0, to=B, reliable=reliable, flow=1 if reliable else None)
    blocks = plain = 0
    while (o := t.pull(0.0)) is not None:
        blocks += math.ceil((len(o.sealed) + seal) / BLOCK)
        plain += len(o.sealed)
    return n * size / (blocks * FRAME), (plain - n * size) / n, blocks / n


out("## 1. Overhead and efficiency per packet (IP bytes / on-air frame bytes, redundancy 1)")
out()
out("| IP pkt | class | tunnel hdr B/pkt single / packed | frames/pkt single | eff. single | frames/pkt packed | eff. packed | eff. packed, AEAD +28 B/msg |")
out("|---|---|---|---|---|---|---|---|")
for size in (64, 200, 512, 1400):
    for rel in (False, True):
        e1, h1, f1 = efficiency(size, False, rel, 0)
        e2, h2, f2 = efficiency(size, True, rel, 0)
        e3, _, _ = efficiency(size, True, rel, 28)
        out(f"| {size} | {'reliable' if rel else 'best-effort'} | {h1:.1f} / {h2:.1f} | {f1:.0f} | "
            f"{e1:.1%} | {f2:.3f} | {e2:.1%} | {e3:.1%} |")
out()
out(f"Link cost alone (mesh symbol header 33 B + CRC 2 B per 512 B frame) caps any row at {BLOCK / FRAME:.1%}.")


def reliable_run(loss, seed, n=400, size=1000):
    net = IpNet(loss=loss, jitter=0.02, seed=seed, rate=200.0)
    a = net.add(Tunnel(A, rng=random.Random(seed)))
    b = net.add(Tunnel(B, rng=random.Random(seed + 1000)))
    for i in range(n):
        a.send(pkt(size, i), 0.0, to=B, reliable=True, ordered=True, flow=1)
    net.run(60, until=lambda: not a.sending[1].unacked)
    got = [d.payload for d in net.inbox[B]]
    return dict(ok=got == [pkt(size, i) for i in range(n)], t=net.now, retx=a.stats.retransmits / n,
                eff=n * size / (net.blocks_on_air * FRAME), ackb=b.stats.blocks, datab=a.stats.blocks)


def paced_run(loss, seed, rate=50, secs=8, size=200):
    net = IpNet(loss=loss, jitter=0.02, seed=seed, rate=200.0)
    a = net.add(Tunnel(A, rng=random.Random(seed)))
    net.add(Tunnel(B, rng=random.Random(seed + 1000)))
    sent_at, got_at = {}, {}
    i = 0

    def drain():
        for d in net.inbox[B]:
            got_at[int.from_bytes(d.payload[1:5], "big")] = net.now
        net.inbox[B].clear()

    while net.now < secs:
        while i < rate * net.now:
            a.send(b"\x45" + i.to_bytes(4, "big") + bytes(size - 5), net.now, to=B, reliable=True,
                   ordered=True, flow=1)
            sent_at[i] = net.now
            i += 1
        net.step(0.002)
        drain()
    while a.sending[1].unacked and net.now < secs + 10:
        net.step(0.002)
        drain()
    lat = sorted(got_at[k] - sent_at[k] for k in got_at)
    return dict(delivered=len(got_at) / len(sent_at), p50=lat[len(lat) // 2],
                p95=lat[int(len(lat) * 0.95)], p99=lat[int(len(lat) * 0.99)], mx=lat[-1],
                retx=a.stats.retransmits / len(sent_at))


out()
out("## 2. Reliable class vs message loss")
out()
out("Bulk: 400 x 1000-byte packets queued at t=0, one ordered reliable flow.  5 seeds per row.")
out()
out("| loss p | all delivered, in order | completion s | retransmits / pkt | ACK blocks / data blocks | goodput eff. | goodput kB/s |")
out("|---|---|---|---|---|---|---|")
for p in (0.0, 0.05, 0.2, 0.4):
    rs = [reliable_run(p, s) for s in range(5)]
    t = st.mean(r["t"] for r in rs)
    out(f"| {p:.0%} | {sum(r['ok'] for r in rs)}/5 | {t:.2f} | {st.mean(r['retx'] for r in rs):.2f} | "
        f"{st.mean(r['ackb'] / r['datab'] for r in rs):.3f} | {st.mean(r['eff'] for r in rs):.1%} | "
        f"{400 * 1000 / t / 1000:.0f} |")
out()
out("Paced: 50 x 200-byte packets/s for 8 s, one ordered reliable flow; latency = local send to")
out("in-order delivery.  Worst of 5 seeds per column.")
out()
out("| loss p | delivered | retransmits / pkt | p50 ms | p95 ms | p99 ms | max ms |")
out("|---|---|---|---|---|---|---|")
for p in (0.0, 0.05, 0.2, 0.4):
    rs = [paced_run(p, s) for s in range(5)]
    out(f"| {p:.0%} | {min(r['delivered'] for r in rs):.1%} | {st.mean(r['retx'] for r in rs):.2f} | "
        f"{max(r['p50'] for r in rs) * 1e3:.0f} | {max(r['p95'] for r in rs) * 1e3:.0f} | "
        f"{max(r['p99'] for r in rs) * 1e3:.0f} | {max(r['mx'] for r in rs) * 1e3:.0f} |")


def deep(o, seen=None):
    seen = set() if seen is None else seen
    if id(o) in seen:
        return 0
    seen.add(id(o))
    n = sys.getsizeof(o)
    if isinstance(o, dict):
        n += sum(deep(k, seen) + deep(v, seen) for k, v in o.items())
    elif isinstance(o, (list, tuple, set, frozenset)):
        n += sum(deep(x, seen) for x in o)
    elif hasattr(o, "__dict__"):
        n += deep(vars(o), seen)
    return n


out()
out("## 3. Per-flow state")
out()
sf, rf = SendFlow(1, B, 0), RecvFlow(0)
c = Conn((A, 1), B, 1, True, None, 80)
out("| state | essential bytes | CPython object, idle |")
out("|---|---|---|")
out("| best-effort flow | 0 ('newest' flows: 6 at the receiver) | 0 |")
out(f"| reliable sender, idle | 38 | {deep(sf)} B |")
out("| reliable sender, per record in flight | 16 + the packet | -- |")
out(f"| reliable receiver | 19 (+ held records, ordered flows) | {deep(rf)} B |")
out(f"| TCP proxy connection | {state_size(c)} (+ one sender + one receiver flow) | {deep(c)} B |")

Path(__file__).with_name("results.txt").write_text("\n".join(lines) + "\n")
