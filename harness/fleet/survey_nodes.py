"""Readiness survey of the fleet: Python/pip/venv tools, disk, git, and what's already deployed.
Creds from ~/.ssh/pi_lab.env."""
import os, paramiko
import roster

d = {}
for l in open(os.path.expanduser("~/.ssh/pi_lab.env")):
    if "=" in l and not l.startswith("#"):
        k, v = l.strip().split("=", 1); d[k] = v
NODES = roster.nodes()

CMD = r"""
echo "py=$(python3 --version 2>&1)"
echo "pip=$(python3 -m pip --version 2>&1 | awk '{print $2}')"
echo "venvmod=$(python3 -c 'import venv;print(\"ok\")' 2>&1)"
echo "numpy=$(python3 -c 'import numpy;print(numpy.__version__)' 2>&1 | tail -1)"
echo "git=$(git --version 2>&1 | awk '{print $3}')"
echo "diskfree=$(df -h / | awk 'NR==2{print $4}')"
echo "net_pypi=$(timeout 8 python3 -c 'import urllib.request as u;u.urlopen(\"https://pypi.org/simple/numpy/\",timeout=6);print(\"reachable\")' 2>&1 | tail -1)"
echo "bench=$(ls -d ~/entroptics-bench 2>/dev/null && (ls ~/entroptics-bench | tr '\n' ' '))"
echo "benchvenv=$(ls ~/entroptics-bench/*/bin/python* 2>/dev/null | head -1 || ls ~/entroptics-bench/.venv/bin/python 2>/dev/null || echo none)"
"""

for name, ip in NODES.items():
    c = paramiko.SSHClient(); c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        c.connect(ip, username=d["PI_USER"], password=d["PI_PASS"], timeout=20, look_for_keys=False, allow_agent=False)
        _, o, _ = c.exec_command(CMD, timeout=40)
        txt = {k: v for k, v in (ln.split("=", 1) for ln in o.read().decode(errors="replace").splitlines() if "=" in ln)}
        print(f"\n== {name} ({ip}) ==")
        for k in ("py", "pip", "venvmod", "numpy", "git", "diskfree", "net_pypi", "bench", "benchvenv"):
            print(f"   {k:10s}: {txt.get(k,'')}")
    except Exception as e:
        print(f"\n== {name} ({ip}) ==  ERR {type(e).__name__}: {e}")
    finally:
        c.close()
