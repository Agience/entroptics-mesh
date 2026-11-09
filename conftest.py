"""Auto-mark the long-running tests ``slow`` so ``pytest -m "not slow"`` is a fast lane.

The substrings name the long-running tests (the node/budget/link/emu scenarios dominate the
mesh suite). Plain ``pytest`` with no ``-m`` still runs everything; the marker only takes effect when
you filter, so this changes nothing about a default run.
"""
import pytest

_SLOW = ("node", "budget", "link_run", "emu")


def pytest_collection_modifyitems(config, items):
    for item in items:
        if any(p in item.nodeid for p in _SLOW):
            item.add_marker(pytest.mark.slow)
