"""Same-channel vs two-sub-band decode-and-forward on a 3-node chain (6 km hops), 1.0 Msps senders
into 2.4 Msps receivers, equal power.  Arm 'same': origin and relay both at -0.6 MHz.  Arm 'split':
origin at -0.6 MHz, relay at +0.6 MHz.  Caches in the steady state after first contact."""
import sys, time
import numpy as np
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
from entroptics_mesh import phy
from entroptics_mesh.emu.air import Air, Radio
from entroptics_mesh.emu.subband_run import SubNode, SubbandRun
from entroptics_mesh.providers import TxPlan

arm, seed = sys.argv[1], int(sys.argv[2])
PACK = len(sys.argv) > 4 and sys.argv[4] == 'ack'
RX, TXR = 2.4e6, 1.0e6
centers = {"same": (-0.6e6, -0.6e6), "split": (-0.6e6, +0.6e6)}[arm]
air = Air(rate=RX, seed=seed)
nodes = []
HOP = float(sys.argv[3]) if len(sys.argv) > 3 else 6000.0
for i, (x, c) in enumerate(zip((0.0, HOP, 2 * HOP), (centers[0], centers[1], 0.0))):
    idx = air.add(Radio(position=np.array([x, 0.0, 100.0]), tx_power_dbm=20.0, noise_dbm=-100.0))
    nodes.append(SubNode(bytes([65 + i]) * 8, idx, TxPlan(rate=TXR, center=c), key=1234))
geom = lambda c: phy.Known(64, TXR / RX, c / RX)
nodes[1].known = [geom(centers[0])]                       # the relay hears the origin
nodes[0].known = [geom(centers[1])] if PACK else []        # the origin overhears the relay
nodes[2].known = sorted({geom(centers[0]), geom(centers[1])}, key=lambda g: g.center)   # far end: both
msg = bytes(np.random.default_rng(0).integers(0, 256, 2400, dtype=np.uint8))
run = SubbandRun(air, nodes, seed=seed, fresh=1.5, passive_ack=PACK)
t = time.perf_counter()
RED = float(sys.argv[5]) if len(sys.argv) > 5 else 1.5
done = run.send(origin=0, to=nodes[2].node_id, message=msg, hops=4, redundancy=RED)
ok = 2 in done and list(nodes[2].got.values())[0][1] == msg
print(f"{arm}{'+ack' if PACK else ''} seed {seed}: acked {run.acked} delivered {ok} slot {done.get(2)} bursts {run.bursts} decoded {[n.decoded for n in nodes]} "
      f"multi-frame captures {run.multi} [{time.perf_counter()-t:.0f}s]", flush=True)
