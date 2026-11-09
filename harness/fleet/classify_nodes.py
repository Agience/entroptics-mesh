"""Classify the lab fleet: SSH to each node, pull model/SoC/RAM/OS/SPI-LoRa, print a roster.
Reads creds from ~/.ssh/pi_lab.env (PI_USER/PI_PASS); never prints the password."""
import os, sys, paramiko
import roster

CREDS = os.path.expanduser("~/.ssh/pi_lab.env")
HOSTS = sys.argv[1:] or list(roster.nodes().values())
d = {}
for line in open(CREDS):
    line = line.strip()
    if "=" in line and not line.startswith("#"):
        k, v = line.split("=", 1); d[k.strip()] = v.strip()
USER, PASS = d["PI_USER"], d["PI_PASS"]

# CPU part -> core.  Pi4/CM4 = A72 (0xd08); Pi3 = A53 (0xd03); Pi5 = A76 (0xd0b).
CORE = {"0xd08": "Cortex-A72 (Pi4/CM4 class)", "0xd03": "Cortex-A53 (Pi3 class)",
        "0xd0b": "Cortex-A76 (Pi5 class)", "0xd07": "Cortex-A57"}

PROBE = r"""
echo "HOST=$(hostname)"
echo "MODEL=$(tr -d '\0' < /sys/firmware/devicetree/base/model 2>/dev/null)"
echo "ARCH=$(uname -m)"
echo "PART=$(grep -m1 'CPU part' /proc/cpuinfo | awk '{print $NF}')"
echo "NCPU=$(nproc)"
echo "MHZ=$(( $(cat /sys/devices/system/cpu/cpu0/cpufreq/cpuinfo_max_freq 2>/dev/null || echo 0) / 1000 ))"
echo "RAM=$(awk '/MemTotal/{printf \"%.1f GiB\", $2/1048576}' /proc/meminfo)"
echo "OS=$(. /etc/os-release 2>/dev/null; echo $PRETTY_NAME)"
echo "PY=$(python3 --version 2>&1)"
echo "SPIDEV=$(ls /dev/spidev* 2>/dev/null | tr '\n' ' ')"
echo "EMMC=$(lsblk -dno NAME,SIZE 2>/dev/null | grep -i mmcblk | tr '\n' '; ')"
echo "LORAHINT=$(grep -rhiE 'sx130|lora|concentrator' /boot/firmware/config.txt /boot/config.txt 2>/dev/null | grep -v '^#' | tr '\n' ',' )$(systemctl list-units --type=service 2>/dev/null | grep -iE 'lora|concentrat|pktfwd|chirpstack' | awk '{print $1}' | tr '\n' ',')"
"""

rows = []
for h in HOSTS:
    info = {"IP": h}
    c = paramiko.SSHClient(); c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        c.connect(h, username=USER, password=PASS, timeout=20, look_for_keys=False, allow_agent=False)
        _, out, err = c.exec_command(PROBE, timeout=25)
        for line in out.read().decode(errors="replace").splitlines():
            if "=" in line:
                k, v = line.split("=", 1); info[k] = v.strip()
        info["core"] = CORE.get(info.get("PART", ""), f"? ({info.get('PART','')})")
        model = info.get("MODEL", "")
        info["unit"] = ("bare Raspberry Pi 4B" if "Pi 4 Model B" in model
                        else "CM4-based (SenseCAP M2 / RAK miner)" if "Compute Module 4" in model
                        else model or "unknown")
        info["ok"] = True
    except Exception as e:
        info["ok"] = False; info["error"] = f"{type(e).__name__}: {e}"
    finally:
        c.close()
    rows.append(info)

print("\n================ LAB FLEET ROSTER ================\n")
for r in rows:
    if not r.get("ok"):
        print(f"{r['IP']:16s}  UNREACHABLE/AUTH FAIL -> {r.get('error')}"); continue
    print(f"{r['IP']:16s}  host={r.get('HOST')}")
    print(f"{'':16s}  unit : {r.get('unit')}")
    print(f"{'':16s}  SoC  : {r.get('core')}  x{r.get('NCPU')} @ {r.get('MHZ')} MHz  | {r.get('ARCH')} | RAM {r.get('RAM')}")
    print(f"{'':16s}  OS   : {r.get('OS')}  | {r.get('PY')}")
    print(f"{'':16s}  store: {r.get('EMMC') or '(SD only)'}")
    print(f"{'':16s}  LoRa : SPI={r.get('SPIDEV') or 'none'}  hints={r.get('LORAHINT') or 'none'}")
    print()
