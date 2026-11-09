"""Access-layer variants measured in research/access: whether the radio listens before talking, and
Node options.  ``adopted`` is the Node default (and FleetRun/NodeRun listen by default)."""
from entroptics_mesh import relay

OLD = dict(interleave=False)                     # the access before: no interleaving (backoff unused)

VARIANTS = {
    "baseline": dict(listen=False, node_kw=OLD),                                   # as recorded
    "interleave": dict(listen=False, node_kw={}),
    "lbt_beb": dict(listen=True, node_kw=dict(interleave=False, backoff_max=8)),   # classic doubling
    "lbt_beb2": dict(listen=True, node_kw=dict(interleave=False, backoff_max=2)),
    "lbt": dict(listen=True, node_kw=dict(interleave=False)),                      # no growth
    "adopted": dict(listen=True, node_kw={}),                                      # lbt + interleave
    "adopted_beb": dict(listen=True, node_kw=dict(backoff_max=8)),
    "adopted_w2": dict(listen=True, node_kw=dict(window=2)),
    "adopted_w6": dict(listen=True, node_kw=dict(window=6)),
    "adopted_cw1k": dict(listen=True, node_kw=dict(contention_s=1024 / 2.4e6)),
    "adopted_cw4k": dict(listen=True, node_kw=dict(contention_s=4096 / 2.4e6)),
    "adopted_fresh2": dict(listen=True, node_kw=dict(policy=relay.Policy(fresh=2.0))),
}
