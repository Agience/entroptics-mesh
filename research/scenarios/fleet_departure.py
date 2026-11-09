"""Fleet departure: a lead drone flies outbound at speed and drops relays; it sends telemetry home
throughout.  Measures delivery to the base over time, with real bursts on the emulated air.

Geometry: base at x=0.  The lead flies +x at V m/s from x=0.  Relay i (i = 1..R) flies with the
lead until x = i * D, then holds there.  Every T_MSG seconds of mission time the lead originates a
telemetry message addressed to the base; it floods with FEC/CRC frames.  The air advances its clock
by the slots actually used, and mission time between messages is jumped (positions are evaluated at
each slot's time).
"""
import json, sys, time
import numpy as np
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
from entroptics_mesh.emu.air import Air, Radio
from entroptics_mesh.emu.net import FloodRun, Node
from entroptics_mesh.routing import FloodRouter

V, D, R, T_MSG, N_MSG = 60.0, 2500.0, 3, 25.0, int(sys.argv[1]) if len(sys.argv) > 1 else 12
TX, NOISE, ALT = 20.0, -100.0, 100.0

def positions(t):
    lead = V * t
    pos = [np.array([0.0, 0.0, 10.0])]                              # base
    for i in range(1, R + 1):
        pos.append(np.array([min(lead, i * D), 0.0, ALT]))          # relays hold at i*D
    pos.append(np.array([lead, 0.0, ALT]))                          # lead
    return pos

air = Air(seed=0)
nodes = []
for i, p in enumerate(positions(0.0)):
    idx = air.add(Radio(position=p, tx_power_dbm=TX, noise_dbm=NOISE))
    nid = bytes([65 + i]) * 8
    nodes.append(Node(nid, idx, FloodRouter(nid), key=1234))
run = FloodRun(air, nodes, seed=1, window=6, move=positions)
lead = len(nodes) - 1
t0 = time.perf_counter()
record = dict(scenario="fleet_departure", V=V, D=D, relays=R, T_MSG=T_MSG, tx_dbm=TX, noise_dbm=NOISE,
              nodes=[dict(id=n.node_id.decode(), role=("base" if i == 0 else "lead" if i == lead else "relay"))
                     for i, n in enumerate(nodes)], messages=[])
for m in range(N_MSG):
    run._clock = m * T_MSG
    got = run.run(origin=lead, to=nodes[0].node_id, payload=f"telemetry {m}".encode(), hops=8, max_slots=40)
    x = V * m * T_MSG
    tm = m * T_MSG
    record["messages"].append(dict(
        m=m, t=tm, positions=[[float(c) for c in p] for p in positions(tm)],
        tx=[dict(slot=int(sl), sender=int(i)) for sl, i, _ in run.log],
        delivered={str(k): int(v) for k, v in got.items()},
        snr=[[round(air.snr_db(a, b), 1) if a != b else None for b in range(len(nodes))] for a in range(len(nodes))]))
    print(f"t={m*T_MSG:5.0f}s lead at {x/1000:5.2f} km: base {'GOT' if 0 in got else '---'} (slot {got.get(0)})"
          f"  snr lead->base {air.snr_db(lead, 0):6.1f} dB  transmissions {sum(1 for e in run.log if e[0] >= 0)}", flush=True)
    run.log.clear()
print(f"[{time.perf_counter()-t0:.0f}s wall]")
out = Path(__file__).with_suffix(".json")
out.write_text(json.dumps(record))
print("recorded ->", out)
