# Fleet ops — HIL lab management toolkit

Scripts to bring up, deploy to, and run scenarios across the physical Pi fleet (stationary
Raspberry Pi 4B nodes). The nodes they reach are the ones named in the fleet roster.

## Dependencies
- **Roster:** `roster.py` reads a JSON file mapping each node's name to its host,
  `{"pi-0": "<host>", "pi-1": "<host>", ...}`, from `~/.entroptics/lab_roster.json` or the path in
  `ENTROPTICS_LAB_ROSTER`. Every script takes its node list from it (`roster.nodes()`, or
  `roster.host(name)` for one node); it holds only names and hosts. `lab_run.py` and `robustness.py`
  place `pi-0`…`pi-3` in fixed roles (lead, relays, base).
- **Creds:** `~/.ssh/pi_lab.env` with `PI_USER=...` / `PI_PASS=...` (same login on every node). Every
  script reads it; none echoes the password. Keep it out of every repository.
- **paramiko** (`pip install paramiko`) — no `sshpass`/`plink` on Windows; SSH is driven via paramiko.

## Workflow
```
classify_nodes.py [host ...] # model/SoC/RAM/OS/SPI of each node (read-only; default: every roster node)
rename_hosts.py              # unique hostname per node from its roster name (hostnamectl + /etc/hosts)
survey_nodes.py              # readiness: python/pip/venv/disk/net/existing deploy
# --- build the deploy bundle (see below), then ---
deploy_lab.py                # SFTP bundle -> ~/entroptics-lab, venv, pip install entroptics scipy, verify
bench_decode.py  (deployed)  # per-node decode micro-bench (run via deploy; see lab_run pattern)
lab_run.py                   # DISTRIBUTED mesh across the real Pis (listeners + air_server over the bridge)
robustness.py                # node-loss scenarios: base + 2 redundant relays + lead
```

### Building the deploy bundle (deploy_lab.py expects `lab_bundle.tgz`)
Not a script — a one-liner. **Must strip `__pycache__`** (stale aarch64/x86 numba caches break the Pi):
```bash
cd <workspace>          # the directory holding the sibling checkouts
STAGE=/tmp/lab_bundle; rm -rf "$STAGE"; mkdir -p "$STAGE/lab/harness"
cp -r entroptics-rf/src/entroptics_rf "$STAGE/lab/"
cp -r entroptics-mesh/src/entroptics_mesh "$STAGE/lab/"
cp entroptics-mesh/harness/*.py "$STAGE/lab/harness/"
cp entroptics-mesh/examples/quickstart.py "$STAGE/lab/quickstart_mesh.py"
find "$STAGE" -type d -name __pycache__ -exec rm -rf {} +          # CRITICAL
tar czf entroptics-mesh/harness/fleet/lab_bundle.tgz -C "$STAGE" lab
```
`deploy_lab.py` reads the bundle from this directory (`BUNDLE`). On the Pi:
`PYTHONPATH=lab:lab/harness venv/bin/python ...`. Run `lab_run.py` from the orchestrator with
`PYTHONPATH=<harness>;<rf>/src;<mesh>/src` and the rf `.venv` python (with entroptics, numpy and scipy installed).

## Gotchas
- **`pi_node.py`/`bridge.serve_listening` accepts exactly ONE connection.** Do NOT TCP-probe a listener
  to check readiness — a connect+close consumes its accept and kills it. Check the **Pi-side log**
  (`grep 'listening on' ~/pi_node_<port>.log`) instead; `lab_run.py` does this.
- **Launch listeners detached:** paramiko backgrounding/`nohup &` empties the channel. Use
  `setsid env ... venv/bin/python ... > log 2>&1 < /dev/null &` and let the exec return.
- **2r2t dual-RX:** pyadi `rx_enabled_channels=[0,1]` comes up with a DEAD data path; use raw-libiio with
  all four `cf-ad9361-lpc` channels enabled (`PlutoRadio.receive_both` does this; lane1 = RX2).
- **Killing a stuck Windows python:** MSYS mangles `taskkill /PID` (reads `/PID` as a path). Use
  PowerShell `Stop-Process -Id <pid> -Force`.
- **`pytest -q` summary line** ("N passed") often doesn't survive a `> file` redirect; trust the exit code.
- The Pis may sit on different subnets: the orchestrator (star topology) reaches each Pi, and the Pis
  never need to reach each other.
