"""Set a unique hostname on each lab node (entroptics-<roster name>, e.g. entroptics-pi-0). Idempotent; updates /etc/hostname
(via hostnamectl) and the 127.0.1.1 line in /etc/hosts; verifies. Creds from ~/.ssh/pi_lab.env."""
import os, paramiko
import roster

d = {}
for l in open(os.path.expanduser("~/.ssh/pi_lab.env")):
    if "=" in l and not l.startswith("#"):
        k, v = l.strip().split("=", 1); d[k] = v
USER, PASS = d["PI_USER"], d["PI_PASS"]

# host -> new hostname: roster name pi-N becomes entroptics-pi-N
MAP = {h: f"entroptics-{name}" for name, h in roster.nodes().items()}

for ip, new in MAP.items():
    c = paramiko.SSHClient(); c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        c.connect(ip, username=USER, password=PASS, timeout=20, look_for_keys=False, allow_agent=False)
        # one sudo shell: set static hostname + fix the 127.0.1.1 line (replace if present, else add)
        script = (f"hostnamectl set-hostname {new} && "
                  f"(grep -q '^127.0.1.1' /etc/hosts "
                  f"&& sed -i 's/^127.0.1.1.*/127.0.1.1\\t{new}/' /etc/hosts "
                  f"|| echo '127.0.1.1\\t{new}' >> /etc/hosts)")
        cmd = f"echo {PASS!r} | sudo -S -p '' bash -c {script!r}"
        _, out, err = c.exec_command(cmd, timeout=25)
        rc = out.channel.recv_exit_status()
        errtxt = err.read().decode(errors="replace").strip()
        errtxt = "\n".join(x for x in errtxt.splitlines() if "password for" not in x.lower())
        # verify
        _, o2, _ = c.exec_command("hostnamectl --static; echo ---; getent hosts 127.0.1.1", timeout=15)
        ver = o2.read().decode(errors="replace").strip().replace("\n", " | ")
        print(f"{ip:16s} -> {new}   rc={rc}  verify: {ver}" + (f"   err:{errtxt}" if errtxt else ""))
    except Exception as e:
        print(f"{ip:16s} ERR {type(e).__name__}: {e}")
    finally:
        c.close()
