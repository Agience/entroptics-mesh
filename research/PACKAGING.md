# Packaging & install — the three-repo entroptics stack

How to install, test, and release the stack:

```
entroptics  <-  entroptics-rf  <-  entroptics-mesh
  (core)          (PHY)              (mesh)
```

All three are published to PyPI; `pip install entroptics-mesh` pulls `entroptics` and `entroptics-rf`
from PyPI. For development, install the sibling checkouts editable, each **after** the repo it
depends on, so pip uses the checkout instead of fetching the published release.

## Dependency graph & requirements

| Repo | Name | Version | Python | Core runtime deps | Optional extras |
|---|---|---|---|---|---|
| entroptics | `entroptics` | 0.2.10 | >=3.10 | `numpy>=2.0` | `torch`, `scipy`, `test`, `figures`, `dev` |
| entroptics-rf | `entroptics-rf` | 0.1.0 | >=3.10 | `numpy>=2.0`, `entroptics>=0.2.10`, `scipy>=1.10` | `test`, `sdr`, `rtlsdr` |
| entroptics-mesh | `entroptics-mesh` | 0.1.0 | >=3.10 | `numpy>=2.0`, `entroptics>=0.2.10`, `entroptics-rf>=0.1` | `test`, `crypto` |

`numpy>=2.0` is the real floor for the whole stack: `entroptics` needs the numpy 2.0
`numpy.linalg.svdvals` API, so rf and mesh inherit it (their metadata states 2.0 rather than an
older number that pip would silently override).

## Install from PyPI

```sh
pip install entroptics-mesh             # pulls entroptics and entroptics-rf
pip install "entroptics-mesh[crypto]"   # with the AEAD crypto provider (cryptography)
```

## Development install (sibling checkouts, CPU only)

In a fresh virtualenv (not a project `.venv`):

```sh
python -m venv .venv-stack
# Windows: .venv-stack\Scripts\activate    POSIX: source .venv-stack/bin/activate
pip install --upgrade pip

pip install -e ./entroptics            # core (or take it from PyPI: pip install entroptics)
pip install -e "./entroptics-rf[test]" # PHY; entroptics>=0.2.10 is satisfied by the editable core
pip install -e "./entroptics-mesh[test]"  # mesh; entroptics-rf is satisfied by the editable PHY
```

Order matters: installing rf or mesh before its dependency makes pip fetch the published release
of that dependency from PyPI instead of using your checkout. With the dependency already installed
editable, pip marks it satisfied and never touches the index for it.

In a throwaway venv on Python 3.12 / Windows all three install clean; imports (`entroptics`,
`entroptics_rf`, `entroptics_mesh`, `entroptics_mesh.emu`) resolve; `pytest --collect-only` reports
0 collection errors in each repo.

rf also installs a console entry point: `entroptics-rf <command>` (equivalent to
`python -m entroptics_rf`), commands `selftest focus ber embed tx rx`.

## With-SDR install (optional hardware)

The SDR extras drive real radios and need **system libraries** that pip cannot install:

| Extra | Python pkg | System prerequisite |
|---|---|---|
| `entroptics-rf[sdr]` | `pyadi-iio>=0.0.18` | **libiio** (+ libad9361) — ADALM-Pluto / PlutoSDR. Linux: `libiio-dev`; Windows: the Analog Devices libiio installer; macOS: `brew install libiio`. |
| `entroptics-rf[rtlsdr]` | `pyrtlsdr>=0.2.9` | **librtlsdr** — RTL-SDR / NESDR. Linux: `rtl-sdr`/`librtlsdr-dev`; Windows: the `rtl-sdr`/Zadig USB driver; macOS: `brew install librtlsdr`. |

```sh
pip install "entroptics-rf[sdr,rtlsdr]"             # from PyPI, only after the system libs are present
pip install -e "./entroptics-rf[test,sdr,rtlsdr]"   # or from the checkout
```

Note on the `rtlsdr` pin: it is `>=0.2.9`, not `>=0.3`. The current bench build is `pyrtlsdr
0.2.93`, which under PEP 440 ordering is `0.2.9 < 0.2.93 < 0.3.0` — a `>=0.3` pin excludes the
version that actually runs on the bench. `>=0.2.9` admits 0.2.93 and the 0.3.x/0.5.x line.

Do not install the SDR extras in CI or on build machines without the radios/system libs — the core
codec, mesh, and full non-hardware test suites need neither.

## Version floor

rf and mesh both require `entroptics>=0.2.10`, the current release; rf re-checks the floor at
import (`waveform.REQUIRED_ENTROPTICS`) so a too-old core fails with a reason rather than mid-read.
The floor is a minimum only: the dependents run against the current library, not a pin. An upper
cap such as `<0.3` belongs in rf once a 0.3 release is known to change a read rf consumes.

## Running the test suites

```sh
# core (needs scipy + threadpoolctl for the full green run; both in the [test] extra)
pip install -e "./entroptics[test]" && (cd entroptics && pytest)

# PHY
pip install -e "./entroptics-rf[test]" && (cd entroptics-rf && pytest)

# mesh
pip install -e "./entroptics-mesh[test]" && (cd entroptics-mesh && pytest)
```

Each repo configures pytest in its own `pyproject.toml` (`[tool.pytest.ini_options]`): core
`testpaths=src/tests`, rf `testpaths=tests`, mesh `testpaths=tests` + `pythonpath=src`. The
`entroptics[test]` extra pulls `scipy` and `threadpoolctl` (the BLAS-pin test needs the latter);
without `threadpoolctl` collection still succeeds but that test errors at run time.

## Release

Each repo publishes to PyPI from its `.github/workflows/publish.yml` (trusted publishing) when a
GitHub release is published.

- **entroptics** `v0.2.10` — the PyPI release rf and mesh depend on.
- **entroptics-rf** `v0.1.0` — sdist + wheel (`python -m build`); README present;
  console script wired. AGPL-3.0-only or commercial licence declared.
- **entroptics-mesh** `v0.1.0` — sdist + wheel (`python -m build`). **Licence: AGPL-3.0-only or commercial**
  (declared in `pyproject.toml` + `LICENSE`).
