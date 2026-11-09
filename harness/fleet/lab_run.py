"""Bring the HIL lab live: start pi_node.py listeners on the 4 physical Pis (detached), then drive a
distributed mesh across them from here (base on pi-3, relays on pi-1/pi-2, lead on pi-0 beyond direct
range). Validates real-hardware delivery over the bridge. Cleans up the listeners after."""
import os, socket, time, threading, paramiko
import air_server as A
import roster
from entroptics_mesh.routing import HOME

d = {}
for l in open(os.path.expanduser("~/.ssh/pi_lab.env")):
    if "=" in l and not l.startswith("#"):
        k, v = l.strip().split("=", 1); d[k] = v
U, P = d["PI_USER"], d["PI_PASS"]

# (name, ip, role, id, port, km-from-base) -- wide spacing so the lead is beyond base's direct reach
FLEET = [("pi-3", roster.host("pi-3"), "base", b"BASE0000", 52000, 0.0),
         ("pi-1", roster.host("pi-1"), "relay", b"RLY00000", 52001, 25.0),
         ("pi-2", roster.host("pi-2"), "relay", b"RLY00001", 52002, 50.0),
         ("pi-0", roster.host("pi-0"), "lead", b"LEAD0000", 52003, 75.0)]

def ssh(ip):
    c = paramiko.SSHClient(); c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    c.connect(ip, username=U, password=P, timeout=30, look_for_keys=False, allow_agent=False)
    return c

def launch(name, ip, role, nid, port):
    c = ssh(ip)
    try:
        c.exec_command("pkill -f '[p]i_node.py' 2>/dev/null")[1].channel.recv_exit_status()
        time.sleep(0.5)
        cmd = (f"cd ~/entroptics-lab && setsid env OPENBLAS_NUM_THREADS=1 ENTROPTICS_FAST=0 "
               f"PYTHONPATH=lab:lab/harness venv/bin/python lab/harness/pi_node.py "
               f"--role {role} --id {nid.decode()} --port {port} > ~/pi_node_{port}.log 2>&1 < /dev/null &")
        c.exec_command(cmd)[1].channel.recv_exit_status()
    finally:
        c.close()

def listener_ready(ip, port, tries=20):
    # serve_listening accepts ONE connection, so we must NOT probe by connecting -- that consumes it.
    # Check readiness via the Pi-side log + a live process instead.
    for _ in range(tries):
        c = ssh(ip)
        try:
            _, o, _ = c.exec_command(
                f"grep -q 'listening on' ~/pi_node_{port}.log && pgrep -f '[p]i_node.py' >/dev/null "
                f"&& echo READY || echo WAIT", timeout=10)
            if "READY" in o.read().decode(errors="replace"):
                return True
        finally:
            c.close()
        time.sleep(1.0)
    return False

def cleanup(name, ip, port):
    c = ssh(ip)
    try:
        _, o, _ = c.exec_command(f"tail -2 ~/pi_node_{port}.log 2>/dev/null", timeout=10)
        log = o.read().decode(errors="replace").strip().replace("\n", " | ")
        c.exec_command("pkill -f '[p]i_node.py' 2>/dev/null")[1].channel.recv_exit_status()
        return log
    finally:
        c.close()

print("== starting listeners on 4 Pis ==")
ts = [threading.Thread(target=launch, args=(n, ip, r, i, p)) for n, ip, r, i, p, _ in FLEET]
[t.start() for t in ts]; [t.join() for t in ts]

print("== waiting for listeners to bind (log-based; no socket probe) ==")
up = {n: listener_ready(ip, p) for n, ip, r, i, p, _ in FLEET}
for n, ip, r, i, p, _ in FLEET:
    print(f"   {n:5s} {ip}:{p}  {'LISTENING' if up[n] else 'NOT UP'}")
if not all(up.values()):
    print("!! not all listeners up; aborting run (cleaning up)")
    for n, ip, r, i, p, _ in FLEET: cleanup(n, ip, p)
    raise SystemExit(1)

print("== driving distributed mesh (lead -> HOME across real relays) ==")
specs = [{"id": i, "role": r, "pos": A._pos(km * 1000), "physical": (ip, p)}
         for n, ip, r, i, p, km in FLEET]
t0 = time.time()
delivered, bursts, senders, order = A.run_fleet(specs, send_from=b"LEAD0000", max_slots=400)
dt = time.time() - t0
txers = [order[k].decode() for k in senders]
relayed = any(0 < k < len(order) - 1 for k in senders)      # a middle node (relay) transmitted

print(f"\n================ LAB RUN RESULT ================")
print(f"  delivered     : {delivered}   ({dt:.1f}s, {bursts} bursts over the real fleet)")
print(f"  transmitters  : {txers}")
print(f"  relayed via mid-chain Pi : {relayed}")
print(f"  topology      : BASE(pi-3) <- RLY(pi-1 @9km) <- RLY(pi-2 @18km) <- LEAD(pi-0 @27km)")

print("\n== cleanup ==")
for n, ip, r, i, p, _ in FLEET:
    print(f"   {n}: {cleanup(n, ip, p)}")
raise SystemExit(0 if delivered else 2)
