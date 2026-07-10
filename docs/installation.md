# Installation

octacam is a Python package (Python 3.10+). The recommended way to install it is
with [uv](https://docs.astral.sh/uv/), which installs octacam and its own
isolated Python without touching your system environment.

!!! note "Python version and the FLIR vendor tier"
    Any Python ≥3.10 runs octacam and drives Basler, FLIR, and any GenICam
    USB3-Vision camera through the [backend cascade](guide/backends.md) — every
    tier works on any supported Python. The *FLIR vendor SDK* (Spinnaker/PySpin)
    ships its PySpin wheel for multiple Python versions (cp310–cp314 as of
    Spinnaker 4.4); install the one matching your interpreter. If PySpin is not
    installed, octacam still drives FLIR cameras through the Spinnaker C-API
    backend (`spinnaker`) — the SDK's `libSpinnaker_C.so` via ctypes, which needs
    no PySpin wheel — falling back to the always-present pycameleon floor if the
    SDK is absent too.

## Install with uv

```bash
uv tool install git+https://github.com/NeLy-EPFL/octacam.git
```

This puts an `octacam` command on your `PATH`. Check it:

```bash
octacam --version
octacam doctor          # diagnose the install and list detected cameras
```

!!! tip "Don't have uv?"
    Install it with `curl -LsSf https://astral.sh/uv/install.sh | sh` (see the
    [uv install docs](https://docs.astral.sh/uv/getting-started/installation/)).

### Alternatives

From a clone, or with pip:

```bash
uv tool install .      # from a checkout
pip install .          # into an existing environment
```

## Update

octacam is released from git (there is no PyPI package), so **how you update
depends on how you installed it**. Check what you are running first:

```bash
octacam --version
```

**uv tool install** (the recommended install — tracks the latest `main`):

```bash
uv tool upgrade --reinstall octacam
```

`--reinstall` forces uv to re-fetch the git source and rebuild; plain
`uv tool upgrade octacam` may reuse a cached checkout of the branch.

**pip install:**

```bash
pip install --upgrade --force-reinstall git+https://github.com/NeLy-EPFL/octacam.git
```

`--force-reinstall` is required: pip deduplicates by version, and a rolling
`main` install keeps the same version between releases.

**Development checkout** (`git clone` + `uv sync`):

```bash
git pull && uv sync
```

### Pin to a release

Releases are tagged `vX.Y.Z` (see the
[releases page](https://github.com/NeLy-EPFL/octacam/releases) and the
`CHANGELOG.md` in the repository root). To install or hold a rig at a specific
release instead of the moving `main`:

```bash
uv tool install --force git+https://github.com/NeLy-EPFL/octacam.git@v0.3.0
```

## Cameras: what you need per tier

octacam auto-detects the best available driver per camera (the
[backend cascade](guide/backends.md)). The Python-installable tiers all ship in
core, so **a rig works out of the box**; only the two non-pip pieces are manual.

| Tier | Runtime | Ships with octacam? |
| --- | --- | --- |
| **pycameleon** (floor) | libusb (USB3 Vision) | ✅ Yes — always available |
| **Basler** | pypylon | ✅ Yes — works out of the box |
| **FLIR C-API** (`spinnaker`) | Spinnaker SDK (`libSpinnaker_C.so`) | ❌ Install the SDK separately; works on any Python — no PySpin wheel needed |
| **FLIR / Teledyne vendor** (PySpin) | Spinnaker SDK + PySpin | ❌ Install separately — the PySpin vendor tier (below); wheels for cp310–cp314 |

The only system requirement for the always-on floor is `libusb-1.0`. See
[Camera backends](guide/backends.md) for how the cascade picks a backend per
camera. Run `octacam doctor` to see which tiers are available and which one each
camera would use.

### FLIR / Teledyne vendor SDK (tier 1)

PySpin is **not on PyPI** — it ships with Teledyne's Spinnaker SDK — but Spinnaker
4.4 provides wheels for **cp310–cp314**, so install the one matching your
interpreter. If PySpin is not installed, the cascade drives FLIR cameras through
the `spinnaker` C-API backend (`libSpinnaker_C.so` via ctypes — needs the
Spinnaker SDK but no PySpin wheel), falling back to the pycameleon floor.

```bash
# 1. Install the Spinnaker SDK for your platform (from Teledyne).
# 2. Install the PySpin wheel matching your interpreter into octacam's environment:
pip install spinnaker_python-*.whl
# 3. (optional) record the intent — installs nothing on its own:
pip install "octacam[flir]"
```

If a config pins `backend = "flir"` and PySpin is missing, octacam exits with a
clear message rather than a traceback. Run `octacam doctor --backend flir` to
confirm the backend loads.

## Development install

To hack on octacam itself, clone it and let uv manage the environment:

```bash
git clone https://github.com/NeLy-EPFL/octacam.git
cd octacam
uv sync                     # create the venv with runtime + dev dependencies
uv run octacam --help       # run from the checkout
uv run pytest               # run the test suite
```

`uv sync` reads the `dev` dependency group (pytest, ruff, pyright, …) from
`pyproject.toml`. To pull in the latest changes later, `git pull` and re-run
`uv sync`.

### Build the documentation

This site is built with [Material for MkDocs](https://squidfunk.github.io/mkdocs-material/).
Preview it locally with live reload:

```bash
uv run --group docs mkdocs serve     # → http://127.0.0.1:8000
```

## Next steps

Head to the [Quickstart](quickstart.md) to record your first trial — no cameras
required.
