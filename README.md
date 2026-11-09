# entroptics-mesh

A one-way, parameter-free mesh over the [entroptics-rf](https://github.com/Agience/entroptics-rf) physical layer.

Dependency graph: [entroptics](https://github.com/Agience/entroptics) &larr; entroptics-rf &larr; entroptics-mesh.

New to the stack? Start with [ARCHITECTURE.md](docs/ARCHITECTURE.md) — the system overview, the
end-to-end data path, the provider model, a map of every document, and the current maturity.

Operating it end to end (software, SDR bench, Pi fleet) is [RUNBOOK.md](docs/RUNBOOK.md) — a
deterministic, console-driven procedure with a PASS gate at every step. The protocol contract — the
mesh↔RF interface, the on-wire formats, and the invariants — is [PROTOCOL-SPEC.md](docs/PROTOCOL-SPEC.md).

Want to see it run? [examples/quickstart.py](examples/quickstart.py) sends a message across a
three-node relay chain over the emulated air (no radio hardware), plugging in a custom key provider:

```sh
python examples/quickstart.py
```

## What it is built on, and what it refuses

- **Nothing is pre-shared.** No synced clocks, no schedules, no agreed channel plan; a receiver
  measures each sender (its scale, width, carrier) from the capture.
- **Nothing is required back.** No acks, no handshakes, no echoes, no decode-quality reports; a
  listen-only node decodes as fully as any other.
- **Reliability is forward.** FEC inside a packet, fountain coding across packets, and relaying over
  more than one path.
- **The core is pluggable.** Encryption, frequency agility and interference handling are providers the user
  plugs in; the core ships interfaces and null providers, and works fully (as an open broadcast
  mesh) with the null ones.

## Modules

`providers` (the plug points), `neighbors` (what each sender was measured to be, and what it
announced), `announce` (the optional node state & capability announcement), `routing` (reply-free flooding, HOME anycast),
`fec` (codes inside and across packets), `link` (fountain, frames), `phy` (fast known-geometry
read, CRC-arbitrated family/M/rate), `adapt` (shout/whisper MCS envelope), `relay` (measured
relaying rules, passive ack, sibling share), `node` (one node's runtime: captures in, bursts out),
`placement`, `plausibility`, `tunnel` + `tcpproxy` (IP over the mesh), `emu` (air and scenario
runners). Outside the core, `ext` holds working providers selected explicitly: `ext.aead.AeadCrypto`
(AEAD sealing under a pre-shared key, replay refusal; `pip install -e ".[crypto]"`) and
`ext.interference.EnergyInterference` (detection of sub-bands with interference, for the announcement).

## Install

Python 3.10 or later; entroptics 0.2.10 or later.

```sh
pip install entroptics-mesh             # pulls entroptics and entroptics-rf from PyPI
pip install "entroptics-mesh[crypto]"   # with the AEAD crypto provider (ext.aead)
```

For development, install the three sibling checkouts editable, in dependency order:

```sh
pip install -e ../entroptics        # core
pip install -e ../entroptics-rf     # physical layer
pip install -e .                    # this package
```

See [research/PACKAGING.md](research/PACKAGING.md) for the full CPU-only and with-SDR install
sequences, system prerequisites for the SDR extras, the version-pinning decision, and how to run
the test suites.

## Test

```sh
pip install -e ".[test]"
pytest
```

Security issues: email connect@agience.ai rather than opening a public issue.

## Licence

AGPL-3.0-only, or a commercial licence — see [`LICENSE`](LICENSE) and
[`COMMERCIAL_LICENSE.md`](COMMERCIAL_LICENSE.md). Patent pending — see [`PATENTS.md`](PATENTS.md).
Licensing inquiries: **connect@agience.ai**.
