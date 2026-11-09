"""Multi-homing: a drone two hops out sends home.  origin -> relays R1,R2 (9 km, 2 km apart) ->
gateways G1,G2 (18 km, 4 km apart, wired to each other).  Relays forward with sibling share.
  unicast   addressed to G1 only          anycast   addressed to HOME, which both gateways accept
  *_kill    G1 dies at slot 14 (mid-message)
Delivery = the message reaches home (any gateway for anycast; G1 for unicast)."""
import sys, time
import numpy as np
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
from entroptics_mesh import phy
from entroptics_mesh.emu.air import Air, Radio
from entroptics_mesh.emu.subband_run import SubNode, SubbandRun
from entroptics_mesh.providers import TxPlan

arm, seed = sys.argv[1], int(sys.argv[2])
RX, TXR, HOP = 2.4e6, 1.0e6, 9000.0
HOME = b"HOME\x00\x00\x00\x00"
geom = lambda c: phy.Known(64, TXR / RX, c / RX)
air = Air(rate=RX, seed=seed)
layout = [((0, 0), -0.6e6), ((HOP, 1000), +0.6e6), ((HOP, -1000), 0.0),
          ((2 * HOP, 2000), -0.3e6), ((2 * HOP, -2000), +0.3e6)]
nodes = []
for i, ((x, yy), c) in enumerate(layout):
    idx = air.add(Radio(position=np.array([x, yy, 100.0]), tx_power_dbm=20.0, noise_dbm=-100.0))
    nodes.append(SubNode(bytes([65 + i]) * 8, idx, TxPlan(rate=TXR, center=c), key=1234,
                         accepts=frozenset({HOME}) if i >= 3 else frozenset()))
xy = [np.array(p) for p, _ in layout]
for i, nd in enumerate(nodes):
    nd.known = [geom(layout[q][1]) for q in range(len(nodes))
                if q != i and np.linalg.norm(xy[q] - xy[i]) <= 13000]
if arm.endswith("_kill"):
    nodes[3].dies_at = 14
to = HOME if arm.startswith("anycast") else nodes[3].node_id
msg = bytes(np.random.default_rng(0).integers(0, 256, 2400, dtype=np.uint8))
run = SubbandRun(air, nodes, seed=seed, fresh=3.0, passive_ack=True, sibling_ack=True)
run._seq[0] = seed
t = time.perf_counter()
done = run.send(origin=0, to=to, message=msg, hops=4, redundancy=3.0)
homes = [g for g in (3, 4) if g in done and list(nodes[g].got.values())[0][1] == msg]
if to != HOME:
    homes = [g for g in homes if g == 3]
first = min((done[g] for g in homes), default=None)
print(f"{arm:14s} seed {seed}: delivered {bool(homes)} slot {first} via {homes} bursts {run.bursts} "
      f"dup {run.duplicates} cancelled {run.cancelled} [{time.perf_counter()-t:.0f}s]", flush=True)
