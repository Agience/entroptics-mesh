"""The mesh time budget on a 2-hop chain (origin -> relay -> far end, 9 km hops, separate 1.0 Msps
sub-bands; the far end cannot hear the origin).  Budget 0 (none) vs budgets shorter than the
delivery time: a spent budget should stop the message instead of carrying it on.

Usage: PYTHONPATH=src;tests python research/scenarios/budget_chain.py  ->  budget_chain.txt"""
import test_budget as t

for b in (0, 40, 60, 200):
    run, done = t._chain(b)
    n = run._slot_samples()
    print(f"budget {b:4d} ms: delivered {2 in done} (slot {done.get(2)}), bursts {run.bursts}, "
          f"expired {run.expired}, slot {1e3 * n / run.air.rate:.2f} ms", flush=True)
