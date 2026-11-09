"""The lab fleet roster: which hosts the fleet scripts reach.

Read from the JSON file named by ``ENTROPTICS_LAB_ROSTER`` (default ``~/.entroptics/lab_roster.json``),
mapping each node's name to its host::

    {"pi-0": "<host or IP>", "pi-1": "<host or IP>", "pi-2": "<host or IP>", "pi-3": "<host or IP>"}

Credentials stay in ``~/.ssh/pi_lab.env``; the roster holds only names and hosts.
"""
import json
import os

PATH = os.environ.get("ENTROPTICS_LAB_ROSTER", os.path.expanduser("~/.entroptics/lab_roster.json"))


def nodes() -> dict:
    """Node name -> host, in the roster's order."""
    try:
        with open(PATH, encoding="utf-8") as f:
            got = json.load(f)
    except FileNotFoundError:
        raise SystemExit(f"no lab roster at {PATH}: write {{\"pi-0\": \"<host>\", ...}} there "
                         f"or point ENTROPTICS_LAB_ROSTER at one")
    if not isinstance(got, dict) or not got:
        raise SystemExit(f"{PATH}: expected a JSON object of node name -> host")
    return {str(k): str(v) for k, v in got.items()}


def host(name: str) -> str:
    """The host of one named node."""
    n = nodes()
    if name not in n:
        raise SystemExit(f"{PATH}: no node named {name!r} (have {sorted(n)})")
    return n[name]
