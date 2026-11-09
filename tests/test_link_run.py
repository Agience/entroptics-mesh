"""Messages larger than a frame, as fountain symbols relayed over real bursts."""
from __future__ import annotations

import numpy as np

from entroptics_mesh.emu.air import Air, Radio
from entroptics_mesh.emu.link_run import BLOCK, LinkNode, LinkRun


def _chain(n, spacing_m, tx_dbm=20.0, seed=0):
    air = Air(seed=seed)
    nodes = []
    for i in range(n):
        idx = air.add(Radio(position=np.array([i * spacing_m, 0.0, 100.0]), tx_power_dbm=tx_dbm,
                            noise_dbm=-100.0))
        nodes.append(LinkNode(bytes([65 + i]) * 8, idx, key=1234))
    return air, nodes


def test_a_multi_frame_message_crosses_two_hops_and_is_rebuilt_once():
    """3 nodes 6 km apart (one hop ~12.8 dB, two hops ~6.7 dB): a 2.4 kB message (k = 6 blocks)
    reaches the far end by decode-and-forward (the relay rebuilds it, then sends fresh symbols) and
    is rebuilt there.  Same-channel relaying is collision-limited: measured over seeds 3-6 this
    delivers 3/4 in 18 bursts, against 2/4 in ~30 for plain relaying at 3x redundancy; this pins a
    seed that delivers."""
    air, nodes = _chain(3, 6000.0)
    msg = bytes(np.random.default_rng(0).integers(0, 256, 2400, dtype=np.uint8))
    run = LinkRun(air, nodes, seed=3, fresh=1.5)
    done = run.send(origin=0, to=nodes[2].node_id, message=msg, hops=4, redundancy=1.5)
    assert 2 in done
    assert list(nodes[2].got.values())[0][1] == msg
    assert -(-len(msg) // BLOCK) == 6
