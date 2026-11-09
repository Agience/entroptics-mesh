"""Deploy the lab bundle to each node in parallel: SFTP tarball, extract, venv, pip install
entroptics+scipy, verify imports.  Creds from ~/.ssh/pi_lab.env.  Idempotent (re-run = update)."""
import os, threading, paramiko
import roster

SB = os.path.dirname(os.path.abspath(__file__))
BUNDLE = os.path.join(SB, "lab_bundle.tgz")
d = {}
for l in open(os.path.expanduser("~/.ssh/pi_lab.env")):
    if "=" in l and not l.startswith("#"):
        k, v = l.strip().split("=", 1); d[k] = v
NODES = roster.nodes()

SETUP = ("cd ~ && mkdir -p entroptics-lab && tar xzf ~/lab_bundle.tgz -C entroptics-lab && "
         "{ [ -x entroptics-lab/venv/bin/python ] || python3 -m venv entroptics-lab/venv; } && "
         "entroptics-lab/venv/bin/python -m pip install -q --upgrade pip && "
         "entroptics-lab/venv/bin/python -m pip install -q entroptics scipy && echo SETUP_OK")
VERIFY = ("cd ~/entroptics-lab && PYTHONPATH=lab:lab/harness venv/bin/python -c "
          "\"import entroptics,entroptics_rf,entroptics_mesh,numpy,scipy;"
          "print('entroptics',entroptics.__version__,'| np',numpy.__version__,'| sp',scipy.__version__)\"")

results = {}
def deploy(name, ip):
    c = paramiko.SSHClient(); c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        c.connect(ip, username=d["PI_USER"], password=d["PI_PASS"], timeout=30, look_for_keys=False, allow_agent=False)
        sftp = c.open_sftp(); sftp.put(BUNDLE, "lab_bundle.tgz"); sftp.close()
        _, o, e = c.exec_command(SETUP, timeout=420)
        rc = o.channel.recv_exit_status(); setup_out = (o.read() + e.read()).decode(errors="replace").strip().splitlines()
        _, o2, e2 = c.exec_command(VERIFY, timeout=60)
        rc2 = o2.channel.recv_exit_status(); ver = (o2.read() + e2.read()).decode(errors="replace").strip()
        results[name] = (rc == 0 and "SETUP_OK" in setup_out[-1:], rc2 == 0, ver, setup_out[-1] if setup_out else "")
    except Exception as ex:
        results[name] = (False, False, f"{type(ex).__name__}: {ex}", "")
    finally:
        c.close()

ths = [threading.Thread(target=deploy, args=(n, ip)) for n, ip in NODES.items()]
for t in ths: t.start()
for t in ths: t.join()

print("\n================ DEPLOY RESULTS ================")
for n in NODES:
    ok, vok, ver, last = results.get(n, (False, False, "no result", ""))
    print(f"{n:6s} setup={'OK' if ok else 'FAIL'}  verify={'OK' if vok else 'FAIL'}  {ver}")
    if not ok:
        print(f"       last: {last}")
