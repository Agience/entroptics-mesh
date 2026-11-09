import sys, runpy
from entroptics_mesh.emu import subband_run as sr
orig_take = sr.SubbandRun._take
log = []
def take(self, node, sym, slot, due, done):
    log.append((slot, node.index, sym.header.hops, sym.symbol_id))
    return orig_take(self, node, sym, slot, due, done)
sr.SubbandRun._take = take
orig_send = sr.Air.send if hasattr(sr, "Air") else None
sys.argv = ["x", sys.argv[1], sys.argv[2]]
runpy.run_path("research/scenarios/node_loss.py", run_name="__main__")
for e in log: print(e)
