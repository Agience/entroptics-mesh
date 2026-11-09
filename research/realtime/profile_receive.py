"""Profile phy.receive_known on representative bursts.

Three views, all on the fast path the mesh node uses (``accept=read_frame``):
  1. correctness + wall-clock ms/burst per cell (warm cache);
  2. cProfile, top functions by cumulative and self time;
  3. stage attribution by wrapping the top-level kernels (scan / rank+settle / payload read /
     coherent read / soft bits / Viterbi) with a wall-clock accumulator.

Run: PYTHONPATH="src;<rf>/src" python research/realtime/profile_receive.py [cell ...]
"""
from __future__ import annotations

import sys, time, cProfile, pstats, io
from collections import defaultdict
import numpy as np

sys.path.insert(0, __file__.rsplit("research", 1)[0] + "research/realtime")
import _bursts as B                                   # noqa: E402

from entroptics_mesh import phy                        # noqa: E402
from entroptics_mesh.link import read_frame            # noqa: E402
from entroptics_rf import waveform as wf               # noqa: E402

CELLS = list(B.CELLS)


def _accept():
    return lambda r: read_frame(r) is not None


def reps_for(cell, n=24, seed0=0):
    """n independent captures of a cell (varying noise + sub-sample start)."""
    out = []
    bkw, rx_rate, snr = B.CELLS[cell]
    iq, plan = B.make_burst(**bkw)
    for s in range(n):
        y, known = B.capture(iq, plan, rx_rate=rx_rate, snr_db=snr, seed=seed0 + s,
                             center_hz=0.3e6, delay=80 + 7 * s)
        out.append((y, known))
    return out, plan


def correctness_and_timing(cells, n=24):
    print("=== correctness + ms/burst (node fast path, accept=read_frame) ===")
    for cell in cells:
        reps, plan = reps_for(cell, n)
        # warmup (fills the lru_cache of gram/aperture/mode_basis)
        read_frame(phy.receive_known(reps[0][0], B.KEY, reps[0][1], accept=_accept()))
        ok = 0
        t = time.perf_counter()
        for y, known in reps:
            rx = phy.receive_known(y, B.KEY, known, accept=_accept())
            got = read_frame(rx)
            ok += got is not None
        ms = 1e3 * (time.perf_counter() - t) / len(reps)
        print(f"  {cell:18s} decoded {ok}/{len(reps)}   {ms:6.1f} ms/burst")


def cprofile(cell, n=24):
    print(f"\n=== cProfile: {cell} (x{n}) ===")
    reps, plan = reps_for(cell, n)
    read_frame(phy.receive_known(reps[0][0], B.KEY, reps[0][1], accept=_accept()))  # warm
    pr = cProfile.Profile()
    pr.enable()
    for y, known in reps:
        read_frame(phy.receive_known(y, B.KEY, known, accept=_accept()))
    pr.disable()
    s = io.StringIO()
    ps = pstats.Stats(pr, stream=s).sort_stats("tottime")
    ps.print_stats(22)
    for line in s.getvalue().splitlines():
        print(line)


# ---- stage attribution -----------------------------------------------------
# Wrap the top-level kernels.  Times are wall-clock totals over the run; nested kernels are
# reported separately so the reader can see how the big buckets decompose.
_T = defaultdict(float)
_N = defaultdict(int)


def _wrap(mod, name, label):
    fn = getattr(mod, name)

    def g(*a, **k):
        t = time.perf_counter()
        try:
            return fn(*a, **k)
        finally:
            _T[label] += time.perf_counter() - t
            _N[label] += 1
    g.__wrapped__ = fn
    setattr(mod, name, g)


def stage_attribution(cell, n=24):
    print(f"\n=== stage attribution: {cell} (x{n}) ===")
    _T.clear(); _N.clear()
    import scipy.optimize as so
    targets = [
        (wf, "_scaled_scan", "scan (_scaled_scan)"),
        (wf, "_scaled_coords", "  row read (_scaled_coords)"),
        (wf, "_joint_coords", "  joint read (_joint_coords)"),
        (wf, "_band_coords", "  band read (_band_coords)"),
        (wf, "_cm_rows", "  _cm_rows (entropy)"),
        (wf, "_dekeyed", "  coherent de-key (_dekeyed)"),
        (wf, "mode_keys", "  mode_keys (SHAKE)"),
        (wf, "row_radius", "  row_radius (SHAKE)"),
        (wf, "_keyed_uniform", "  _keyed_uniform (SHAKE total)"),
        (wf, "_reference_at", "  _reference_at"),
        (wf, "_row_carrier", "  _row_carrier"),
        (wf, "_scaled_rows", "  _scaled_rows"),
        (phy, "_soft_from_rows", "  soft diff (_soft_from_rows)"),
        (phy, "_start_scores", "  _start_scores"),
    ]
    import entroptics_mesh.fec as fec
    targets += [(fec, "viterbi_decode_batch", "viterbi_decode_batch"),
                (fec, "viterbi_decode", "viterbi_decode"),
                (so, "minimize", "Powell settle (minimize)")]
    # phy imports scipy.optimize.minimize inside receive_known via `from scipy.optimize import
    # minimize`, so wrapping the module attribute is enough (it is looked up at call time).
    for mod, name, label in targets:
        _wrap(mod, name, label)
    reps, plan = reps_for(cell, n)
    read_frame(phy.receive_known(reps[0][0], B.KEY, reps[0][1], accept=_accept()))  # warm (not timed below)
    _T.clear(); _N.clear()
    t0 = time.perf_counter()
    for y, known in reps:
        read_frame(phy.receive_known(y, B.KEY, known, accept=_accept()))
    total = time.perf_counter() - t0
    for mod, name, label in targets:
        setattr(mod, name, getattr(mod, name).__wrapped__)
    print(f"  total {1e3*total/n:.1f} ms/burst over {n} bursts")
    for label in [t[2] for t in targets]:
        if _N[label]:
            print(f"    {label:34s} {1e3*_T[label]/n:7.2f} ms/burst  "
                  f"{100*_T[label]/total:5.1f}%  ({_N[label]/n:.0f} calls/burst)")


if __name__ == "__main__":
    cells = [a for a in sys.argv[1:] if a in B.CELLS] or CELLS
    correctness_and_timing(cells)
    for cell in cells:
        stage_attribution(cell)
    for cell in cells:
        cprofile(cell)
