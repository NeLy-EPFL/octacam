# Installation

octacam is a Python package (Python 3.10+). The recommended way to install it is
with [uv](https://docs.astral.sh/uv/), which installs octacam and its own
isolated Python without touching your system environment.

!!! note "Python version and the FLIR vendor tier"
    Any Python ≥3.10 runs octacam and drives Basler, FLIR, and any GenICam
    USB3-Vision camera through the [backend cascade](guide/backends.md). The one
    exception is the *FLIR vendor SDK* (Spinnaker/PySpin), whose wheel is
    cp310-only — so if you specifically want that tier, use Python 3.10. On newer
    Python octacam still drives FLIR cameras through the Spinnaker C-API backend
    (`spinnaker`) — the SDK's `libSpinnaker_C.so` via ctypes, which has no cp310
    wheel limit — falling back to the always-present pycameleon floor if the SDK
    is absent.

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

## Cameras: what you need per tier

octacam auto-detects the best available driver per camera (the
[backend cascade](guide/backends.md)). The Python-installable tiers all ship in
core, so **a rig works out of the box**; only the two non-pip pieces are manual.

| Tier | Runtime | Ships with octacam? |
| --- | --- | --- |
| **pycameleon** (floor) | libusb (USB3 Vision) | ✅ Yes — always available |
| **Basler** | pypylon | ✅ Yes — works out of the box |
| **harvesters** (GenTL consumer) | harvesters + genicam | ✅ Yes, but needs a **producer** (below) |
| **FLIR C-API** (`spinnaker`) | Spinnaker SDK (`libSpinnaker_C.so`) | ❌ Install the SDK separately; works on any Python — no PySpin/producer |
| **FLIR / Teledyne vendor** (PySpin) | Spinnaker SDK + PySpin | ❌ Install separately, Python 3.10 only — the PySpin vendor tier (below) |

The only system requirement for the always-on floor is `libusb-1.0`. See
[Camera backends](guide/backends.md) for how the cascade picks a backend per
camera. Run `octacam doctor` to see which tiers are available and which one each
camera would use.

### A GenTL producer (opt-in harvesters tier)

harvesters drives any GenICam camera through an installed GenTL *producer* (a
`.cti` transport layer). Install a producer's SDK and point octacam at it via
`GENICAM_GENTL64_PATH` or `OCTACAM_GENTL_CTI`. The recommended producer is the
**Basler pylon `ProducerU3V`** (installed with the pylon SDK) — it opens and
closes cleanly with no watermark, but enumerates **Basler** U3V cameras only
(drive FLIR through the `spinnaker`/`flir` backends). **Allied Vision Vimba X**
only covers Allied Vision cameras and third-party *GigE Vision* — its USB
transport layer will not see FLIR/Basler USB3 cameras. Teledyne's Spinnaker
producer is denied by default (its device close deadlocks while holding the GIL,
wedging the process); **Balluff mvIMPACT** is denylisted (watermarks third-party
frames and crashes the device scan). When several producers are installed,
`OCTACAM_GENTL_PRODUCER` pins/prioritises them (see
[Camera backends](guide/backends.md)).

### FLIR / Teledyne vendor SDK (tier 1)

PySpin is **not on PyPI** — it ships with Teledyne's Spinnaker SDK — and only has
a **cp310** wheel, so this tier requires **Python 3.10**. On newer Python the
cascade drives FLIR cameras through the `spinnaker` C-API backend
(`libSpinnaker_C.so` via ctypes — needs the Spinnaker SDK but no PySpin/cp310
wheel), falling back to the pycameleon floor. harvesters is never auto-selected.

```bash
# 1. Install the Spinnaker SDK for your platform (from Teledyne), on Python 3.10.
# 2. Install the matching PySpin wheel into octacam's environment:
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
