#!/usr/bin/env bash
# Pi-4B (or any aarch64) real-time decode benchmark. No RF hardware needed — this times the DECODE.
# Runs flag-OFF first (dense-LA, the ~70% cost, needs no numba — always works), then flag-ON +
# bench_viterbi if numba installs. See PI_BENCHMARK.md. Pins OPENBLAS to one thread throughout.
set -u

HERE="$(cd "$(dirname "$0")" && pwd)"            # .../entroptics-mesh/research/realtime
BUNDLE="$(cd "$HERE/../../.." && pwd)"           # dir holding entroptics-rf + entroptics-mesh
RF="$BUNDLE/entroptics-rf"; MESH="$BUNDLE/entroptics-mesh"
# defensive: drop any stale (e.g. x86) numba/pyc caches so numba recompiles for THIS arch
find "$BUNDLE" -name __pycache__ -type d -prune -exec rm -rf {} + 2>/dev/null
VENV="$BUNDLE/venv"; OUT="$BUNDLE/results"; mkdir -p "$OUT"
PP="$MESH/src:$RF/src"
export OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1 PYTHONHASHSEED=0
LOG="$OUT/bench_$(date +%Y%m%d_%H%M%S).log"
say(){ echo "$@" | tee -a "$LOG"; }

say "=== entroptics Pi decode benchmark $(date -u +%FT%TZ) ==="
say "arch: $(uname -m)   kernel: $(uname -r)"
[ "$(uname -m)" = "aarch64" ] || say "WARNING: not aarch64 — numba (flag-ON) half may be unavailable; flag-OFF still runs."
say "cpu: $(grep -m1 'model name' /proc/cpuinfo 2>/dev/null | cut -d: -f2- | xargs || lscpu 2>/dev/null | grep -m1 'Model name' | cut -d: -f2- | xargs)"
say "cores: $(nproc)   mem: $(free -h 2>/dev/null | awk '/Mem:/{print $2}')"
TEMP0=$( (vcgencmd measure_temp 2>/dev/null || awk '{printf "%.1f C\n",$1/1000}' /sys/class/thermal/thermal_zone0/temp 2>/dev/null) )
say "temp (idle): ${TEMP0:-n/a}"

# --- venv + deps -------------------------------------------------------------------------------
if [ ! -x "$VENV/bin/python" ]; then
  say "creating venv $VENV"; python3 -m venv "$VENV" || { say "venv creation FAILED"; exit 1; }
fi
PY="$VENV/bin/python"
say "python: $("$PY" --version 2>&1)"
say "--- installing numpy scipy entroptics (flag-OFF deps; aarch64 wheels) ---"
"$PY" -m pip install -q --upgrade pip 2>&1 | tail -1 | tee -a "$LOG"
"$PY" -m pip install -q numpy scipy "entroptics>=0.2.8" 2>&1 | tail -3 | tee -a "$LOG" \
  || { say "core dep install FAILED — cannot benchmark"; exit 1; }
say "--- trying numba (flag-ON; aarch64 wheel expected, see PI_BENCHMARK.md if it source-builds) ---"
if "$PY" -m pip install -q numba 2>&1 | tail -3 | tee -a "$LOG"; then
  HAVE_NUMBA=$("$PY" -c "import importlib.util as u; print(1 if u.find_spec('numba') else 0)" 2>/dev/null)
else HAVE_NUMBA=0; fi
say "numba available: ${HAVE_NUMBA:-0}"
say "versions: $("$PY" -c "import numpy,scipy;print('numpy',numpy.__version__,'scipy',scipy.__version__)" 2>&1)$("$PY" -c "import numba;print(' numba',numba.__version__)" 2>/dev/null)"

run(){ # label  fast_flag  -- cmd...
  local label="$1" fast="$2"; shift 2
  say ""; say "### $label (ENTROPTICS_FAST=$fast) ###"
  ( cd "$MESH" && PYTHONPATH="$PP" ENTROPTICS_FAST="$fast" "$PY" "$@" ) 2>&1 | tee -a "$LOG"
}

# --- flag-OFF: the critical aarch64 number (dense LA, no numba) --------------------------------
run "profile_receive  flag-OFF (pure-Python/BLAS: the dominant dense-LA cost)" 0 \
    "$MESH/research/realtime/profile_receive.py"

# --- flag-ON: compiled kernels, if numba installed --------------------------------------------
if [ "${HAVE_NUMBA:-0}" = "1" ]; then
  run "profile_receive  flag-ON (compiled Viterbi + entropy)" 1 \
      "$MESH/research/realtime/profile_receive.py"
  say ""; say "### bench_viterbi (numba kernel vs Python reference) ###"
  ( cd "$MESH/research/realtime/poc" && "$PY" bench_viterbi.py ) 2>&1 | tee -a "$LOG"
else
  say ""; say "SKIPPED flag-ON + bench_viterbi (numba unavailable). The flag-OFF number above is the"
  say "critical one (dense LA is ~70% of the burst and needs no numba)."
fi

TEMP1=$( (vcgencmd measure_temp 2>/dev/null || awk '{printf "%.1f C\n",$1/1000}' /sys/class/thermal/thermal_zone0/temp 2>/dev/null) )
say ""; say "temp (after): ${TEMP1:-n/a}   (compare to idle ${TEMP0:-n/a} — watch for thermal throttling)"
say "=== done.  full log: $LOG ==="
say "Compare ms/burst to the x86 table in port_results.md; budget ~2-4 ms/burst (<=8 for 300-row)."
