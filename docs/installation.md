# Installation

octacam is a Python package (Python 3.11+). The recommended way to install it is
with [uv](https://docs.astral.sh/uv/), which installs octacam and its own
isolated Python without touching your system environment.

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

```bash
uv tool upgrade octacam
```

Or reinstall from the latest `main`:

```bash
uv tool install --force git+https://github.com/NeLy-EPFL/octacam.git
```

## Cameras: what you need per vendor

| Backend | Runtime | Ships with octacam? |
| --- | --- | --- |
| **Basler** (default) | pypylon | ✅ Yes — works out of the box |
| **FLIR / Teledyne** | Spinnaker SDK + PySpin | ❌ Install separately (see below) |

The Basler runtime is bundled, so a Basler rig needs nothing extra. See
[Camera backends](guide/backends.md) for how octacam picks a backend.

### FLIR / Teledyne setup

PySpin is **not on PyPI** — it ships with Teledyne's Spinnaker SDK. Install the
SDK and its PySpin wheel into octacam's environment:

```bash
# 1. Install the Spinnaker SDK for your platform (from Teledyne).
# 2. Install the matching PySpin wheel into octacam's environment:
pip install spinnaker_python-*.whl
# 3. (optional) record the intent — installs nothing on its own:
pip install "octacam[flir]"
```

If PySpin is missing when a FLIR config is served, octacam exits with a clear
message rather than a traceback. Run `octacam doctor --backend flir` to confirm
the backend loads.

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
