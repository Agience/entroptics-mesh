"""Sudden node loss.  Diamond: origin -> relay A (sub-band +0.6 MHz) and relay B (-0.6 MHz... see
centres) -> far end, 9 km hops, 1.0 Msps senders into 2.4 Msps receivers.  Arms:
  both      both relays alive
  killA_early   relay A gone from slot 2 (before it can rebuild the message)
  killA_mid     relay A gone from slot 12 (while forwarding)
  single_kill   control: chain with ONE relay, killed at slot 12 (no redundant path)
Steady-state caches; decode-and-forward; passive acks on."""
import sys, time
import numpy as np
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
from entroptics_mesh import phy
from entroptics_mesh.emu.air import Air, Radio
from entroptics_mesh.emu.subband_run import SubNode, SubbandRun
from entroptics_mesh.providers import TxPlan

arm, seed = sys.argv[1], int(sys.argv[2])
mode = sys.argv[3] if len(sys.argv) > 3 else "fixed"   # fixed: fresh 1.5 | shared: fresh 3.0 + sibling_ack
RX, TXR, HOP = 2.4e6, 1.0e6, 9000.0
alive = arm.endswith("_alive")                    # control: same layout, nobody dies
arm = arm.removesuffix("_alive")
geom = lambda c: phy.Known(64, TXR / RX, c / RX)
air = Air(rate=RX, seed=seed)
if arm == "single_kill":
    layout = [((0, 0), -0.6e6), ((HOP, 0), +0.6e6), ((2 * HOP, 0), 0.0)]
elif arm == "tri_kill2":                          # three relays 2 km apart; two die mid-message
    layout = [((0, 0), -0.6e6), ((HOP, 2000), +0.6e6), ((HOP, 0), 0.0), ((HOP, -2000), -0.3e6),
              ((2 * HOP, 0), 0.3e6)]
elif arm == "two_stage":                          # origin -> {A,B} -> {C,D} -> far; one dies per stage
    layout = [((0, 0), -0.6e6), ((HOP, 1000), +0.6e6), ((HOP, -1000), 0.0),
              ((2 * HOP, 1000), -0.3e6), ((2 * HOP, -1000), +0.3e6), ((3 * HOP, 0), -0.6e6)]
else:                                             # diamond: two relays side by side, 2 km apart
    layout = [((0, 0), -0.6e6), ((HOP, 1000), +0.6e6), ((HOP, -1000), 0.0), ((2 * HOP, 0), 0.3e6)]
nodes = []
for i, ((x, yy), c) in enumerate(layout):
    idx = air.add(Radio(position=np.array([x, yy, 100.0]), tx_power_dbm=20.0, noise_dbm=-100.0))
    nodes.append(SubNode(bytes([65 + i]) * 8, idx, TxPlan(rate=TXR, center=c), key=1234))
far = len(nodes) - 1
relays = list(range(1, far))
for r in relays:
    nodes[r].known = [geom(layout[q][1]) for q in [0, *relays, far] if q != r]   # origin, siblings, far end
nodes[0].known = [geom(layout[r][1]) for r in relays]          # origin overhears relays (passive ack)
nodes[far].known = [geom(layout[0][1])] + [geom(layout[r][1]) for r in relays]
if arm in ("tri_kill2", "two_stage"):             # each node holds the geometries it can hear (<= 13 km)
    xy = [np.array(p) for p, _ in layout]
    for i, nd in enumerate(nodes):
        nd.known = [geom(layout[q][1]) for q in range(len(nodes))
                    if q != i and np.linalg.norm(xy[q] - xy[i]) <= 13000]
if alive:
    pass
elif arm == "tri_kill2":
    nodes[1].dies_at = nodes[2].dies_at = 12
elif arm == "two_stage":
    nodes[1].dies_at, nodes[4].dies_at = 12, 20
if arm == "killA_early":
    nodes[1].dies_at = 2
elif arm in ("killA_mid", "single_kill"):
    nodes[1].dies_at = 12
msg = bytes(np.random.default_rng(0).integers(0, 256, 2400, dtype=np.uint8))
run = (SubbandRun(air, nodes, seed=seed, fresh=1.5, passive_ack=True) if mode == "fixed" else
       SubbandRun(air, nodes, seed=seed, fresh=3.0, passive_ack=True, sibling_ack=True))
run._seq[0] = seed                                # a different message (fountain seed) per run
t = time.perf_counter()
done = run.send(origin=0, to=nodes[far].node_id, message=msg, hops=4, redundancy=3.0)
ok = far in done and list(nodes[far].got.values())[0][1] == msg
print(f"{mode:6s} {arm + ('_alive' if alive else ''):16s} seed {seed}: delivered {ok} slot {done.get(far)} bursts {run.bursts} "
      f"decoded {[n.decoded for n in nodes]} cancelled {run.cancelled} [{time.perf_counter()-t:.0f}s]", flush=True)
