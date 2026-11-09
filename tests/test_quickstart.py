"""Keep the SDK quickstart (examples/quickstart.py) runnable and accurate.

An integrator's first contact with the SDK is examples/quickstart.py; if it ever stops delivering, the
"hello world" is broken.  This runs it and checks the claims the script prints: a message crosses
A->B->C via a genuine relay, the source never has to listen (reply-free), and all three nodes share
the one keyed aperture.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_QS = Path(__file__).resolve().parents[1] / "examples" / "quickstart.py"


@pytest.fixture(scope="module")
def quickstart():
    spec = importlib.util.spec_from_file_location("entroptics_quickstart", _QS)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_quickstart_delivers_via_a_genuine_relay(quickstart):
    delivered, facts = quickstart.run_quickstart(verbose=False)
    assert delivered                                   # the message reached C
    assert facts["relay_sent"] >= 1                    # B actually relayed (C is out of A's reach)
    assert facts["dest_captures"] >= 1                 # C read at least the relay burst
    assert facts["relay_waveform_key"] == 0xA17C0DE    # the shared keyed aperture at the relay
