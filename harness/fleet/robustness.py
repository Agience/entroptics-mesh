"""Node-loss resilience on the REAL fleet: base + two redundant relays + a lead beyond base's direct
range. Run with both relays, each relay lost, and both lost -> show reply-free delivery re-adapts when
a relay drops (redundancy), and that a relay is genuinely needed. Star topology, emulated air; the
decode + mesh logic run on the physical Pis. Cleans up listeners after each scenario.
"""
import os, time, threading, paramiko
import air_server as A
import roster

d = {}
for l in open(os.path.expanduser("~/.ssh/pi_lab.env")):
    if "=" in l and not l.startswith("#"):
        k, v = l.strip().split("=", 1); d[k] = v
U, P = d["PI_USER"], d["PI_PASS"]

# name -> (ip, role, id, port, km-from-base)
FLEET = {
    "base":    (roster.host("pi-3"), "base", b"BASE0000", 52000, 0.0),    # 8 GB node, accepts HOME
    "relay_a": (roster.host("pi-1"), "relay", b"RLY00000", 52001, 37.0),
    "relay_b": (roster.host("pi-2"), "relay", b"RLY00001", 52002, 37.0),  # redundant with relay_a
    "lead":    (roster.host("pi-0"), "lead",   b"LEAD0000", 52003, 74.0),  # beyond base's direct reach
}
SCENARIOS = [
    ("full (both relays)", ["base", "relay_a", "relay_b", "lead"], True),
    ("relay_b LOST",       ["base", "relay_a", "lead"],            True),
    ("relay_a LOST",       ["base", "relay_b", "lead"],            True),
    ("BOTH relays LOST",   ["base", "lead"],                       False),   # expected: no path
]

def ssh(ip):
    c = paramiko.SSHClient(); c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    c.connect(ip, username=U, password=P, timeout=30, look_for_keys=False, allow_agent=False); return c

def launch(ip, role, nid, port):
    c = ssh(ip)
    try:
        c.exec_command("pkill -f '[p]i_node.py' 2>/dev/null")[1].channel.recv_exit_status(); time.sleep(0.4)
        cmd = (f"cd ~/entroptics-lab && setsid env OPENBLAS_NUM_THREADS=1 ENTROPTICS_FAST=0 "
               f"PYTHONPATH=lab:lab/harness venv/bin/python lab/harness/pi_node.py "
               f"--role {role} --id {nid.decode()} --port {port} > ~/pi_node_{port}.log 2>&1 < /dev/null &")
        c.exec_command(cmd)[1].channel.recv_exit_status()
    finally:
        c.close()

def ready(ip, port, tries=20):
    for _ in range(tries):
        c = ssh(ip)
        try:
            _, o, _ = c.exec_command(f"grep -q 'listening on' ~/pi_node_{port}.log && pgrep -f '[p]i_node.py' >/dev/null && echo READY || echo WAIT", timeout=10)
            if "READY" in o.read().decode(errors="replace"): return True
        finally:
            c.close()
        time.sleep(1.0)
    return False

def cleanup(ip):
    c = ssh(ip)
    try: c.exec_command("pkill -f '[p]i_node.py' 2>/dev/null")[1].channel.recv_exit_status()
    finally: c.close()

print("== NODE-LOSS RESILIENCE on the real fleet (lead -> HOME; base + 2 redundant relays @37km, lead @74km) ==\n")
results = []
for name, present, expect in SCENARIOS:
    nodes = [(n, *FLEET[n]) for n in present]
    ts = [threading.Thread(target=launch, args=(ip, r, i, p)) for n, ip, r, i, p, km in nodes]
    [t.start() for t in ts]; [t.join() for t in ts]
    up = all(ready(ip, p) for n, ip, r, i, p, km in nodes)
    if not up:
        print(f"  {name:20s}: listeners failed to bind -- skipped"); [cleanup(ip) for n, ip, *_ in nodes]; continue
    specs = [{"id": i, "role": r, "pos": A._pos(km * 1000), "physical": (ip, p)} for n, ip, r, i, p, km in nodes]
    t0 = time.time()
    delivered, bursts, senders, order = A.run_fleet(specs, send_from=b"LEAD0000", max_slots=400)
    dt = time.time() - t0
    txers = [order[k].decode() for k in senders]
    relayed = any(0 < k < len(order) - 1 for k in senders)
    ok = (delivered == expect)
    print(f"  {name:20s}: delivered={delivered!s:5s} (expected {expect!s:5s}) {'OK' if ok else 'XX'}  "
          f"via-relay={relayed!s:5s}  tx={txers}  [{bursts} bursts, {dt:.0f}s]", flush=True)
    results.append((name, delivered, expect, ok))
    for n, ip, *_ in nodes: cleanup(ip)
    time.sleep(1.0)

print("\n== summary ==")
allok = all(r[3] for r in results)
for name, dl, ex, ok in results:
    print(f"  {'PASS' if ok else 'FAIL'}  {name}: delivered={dl} expected={ex}")
print(f"\n=> node-loss resilience {'CONFIRMED' if allok and len(results)==len(SCENARIOS) else 'see above'}: "
      "delivery survives losing either relay (redundancy), and genuinely needs a relay (both lost -> no path).")
