"""Self-contained per-node decode micro-bench (uses the deployed entroptics_rf).
Times: blind scaled decode (realistic per-burst cost) + matched known-geometry read (floor).
Identical on every node -> a uniform cross-node comparison. Prints ms + lock + temp."""
import time, numpy as np
from entroptics_rf import waveform as wf

SEED, K, L, M, ROWS = 1234, 64, 64, 4, 120
rng = np.random.default_rng(1)
bits = rng.integers(0, 2, ROWS * 2 * K)                       # M=4 -> 2 bits/mode
iq = wf.compose(bits, K, L, SEED, M=M)
sig = float(np.mean(np.abs(iq) ** 2)); snr = 10 ** (20 / 10)
noise = (rng.standard_normal(iq.size) + 1j * rng.standard_normal(iq.size)) * np.sqrt(sig / snr / 2)
cfo = np.exp(2j * np.pi * 0.01 * np.arange(iq.size))
x = (iq * cfo + noise).astype(complex)

def med(fn, n):
    ts = []
    for _ in range(n):
        t = time.perf_counter(); fn(); ts.append((time.perf_counter() - t) * 1000)
    ts.sort(); return ts[len(ts) // 2], ts[0], ts[-1]

# blind scaled decode (first-contact acquisition cost) -- one warm-up + 2 timed (spread is tiny)
try:
    out = wf.decode_scaled(x, SEED, (63, 64, 65), carrier_span=0.3)       # warm-up + lock
    d = out[1] if isinstance(out, tuple) else out
    locked = f"L={getattr(d,'L','?')} K={getattr(d,'K','?')} M={getattr(d,'M','?')}"
    dm, dlo, dhi = med(lambda: wf.decode_scaled(x, SEED, (63, 64, 65), carrier_span=0.3), 2)
except Exception as e:
    dm = dlo = dhi = float("nan"); locked = f"ERR {e}"; print("decode_scaled ERR:", e)
try:
    wf.read(x, L, K, SEED, M=M)                                            # warm-up
    rm, rlo, rhi = med(lambda: wf.read(x, L, K, SEED, M=M), 20)
except Exception as e:
    rm = rlo = rhi = float("nan"); print("read ERR:", e)

import subprocess
try:
    temp = subprocess.run(["vcgencmd", "measure_temp"], capture_output=True, text=True, timeout=5).stdout.strip()
except Exception:
    temp = "na"
print(f"RESULT lock[{locked}]  decode_scaled_ms={dm:.0f}[{dlo:.0f}-{dhi:.0f}]  read_matched_ms={rm:.1f}[{rlo:.1f}-{rhi:.1f}]  {temp}")
