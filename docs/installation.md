# Installation

octacam is a Python package (Python **3.11+**). The recommended way to install it
is with [uv](https://docs.astral.sh/uv/), which installs octacam and its own
isolated Python without touching your system environment.

Everything octacam needs to record from a **Basler** rig is installed
automatically — including the Basler pylon runtime (bundled by
[pypylon](https://github.com/basler/pypylon)), OpenCV, and ffmpeg (via
imageio-ffmpeg). No SDK downloads or C++ toolchain are required. **FLIR /
Teledyne** cameras need the Spinnaker SDK installed separately (see
[FLIR / Teledyne setup](#flir-teledyne-setup)).

!!! note "Supported cameras"
    octacam drives **Basler** (USB3, via pypylon) and **FLIR / Teledyne** (USB3,
    via Spinnaker/PySpin) cameras through one common interface, plus a `fake`
    in-memory backend used for tests and hardware-free demos. See
    [Camera backends](guide/backends.md).

## Install with uv

```bash
uv tool install git+https://github.com/NeLy-EPFL/octacam.git
```

This puts an `octacam` command on your `PATH`. Verify it:

```bash
octacam --version         # prints 0.2.0.dev0
octacam list-cameras      # list detected Basler cameras (--backend basler|flir|fake)
```

`octacam list-cameras` opens the camera SDK to enumerate connected devices; with
no cameras attached it simply prints nothing.

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

## Try it without hardware

octacam ships with Basler's camera emulator, so you can run the full GUI with no
cameras attached. Set `PYLON_CAMEMU` to the number of emulated cameras and point
`octacam gui` at the bundled example config:

```bash
PYLON_CAMEMU=8 octacam gui configs/emulate_8_cameras
```

Then open <http://127.0.0.1:8765>. See the [Quickstart](quickstart.md) for a
full walk-through.

## Camera backends

octacam picks one camera backend **per rig**, via the top-level `backend` key in
that rig's `octacam_config.toml` (one vendor per config directory). The default
is `basler`.

| `backend` | SDK | Per-camera parameter file | Ships with octacam? |
| --------- | --- | ------------------------- | ------------------- |
| `basler`  | pypylon (bundled)  | `<serial>.pfs`  | ✅ Yes — works out of the box (default) |
| `flir`    | Spinnaker / PySpin | `<serial>.json` | ❌ Install the Spinnaker SDK separately |
| `fake`    | none (in-memory)   | `<serial>.fake` | ✅ Yes — synthetic frames for tests/demos |

Preview, recording, the software trigger, per-camera exposure/gain/ROI controls,
the recording summary, and the web GUI behave identically across backends. See
[Camera backends](guide/backends.md) for details.

### FLIR / Teledyne setup

PySpin is **not on PyPI** — it ships with Teledyne's Spinnaker SDK. Install the
SDK and its PySpin wheel into octacam's environment, then (optionally) record the
intent with the `flir` extra:

```bash
# 1. Install the Spinnaker SDK for your platform (from Teledyne).
# 2. Install the matching PySpin wheel into octacam's environment:
pip install spinnaker_python-*.whl
# 3. (optional) records the dependency; installs nothing on its own:
pip install "octacam[flir]"
```

!!! warning "The `flir` extra installs nothing"
    `octacam[flir]` is an empty, documented install marker — it does **not** pull
    in PySpin (which cannot be published on PyPI). You must install the Spinnaker
    SDK and its PySpin wheel by hand.

If a config pins `backend = "flir"` and PySpin is missing, octacam exits with a
clear message ("the Spinnaker SDK and its PySpin wheel must be installed") rather
than a traceback. Confirm the backend loads with:

```bash
octacam list-cameras --backend flir
```

## Plugins

Optional hardware/integration features ship as opt-in
[plugins](guide/plugins.md). **The default launch loads none.** The two bundled
plugins — `flywheel` (Arduino stepper-motor controller) and `twophoton` (Arduino
2-photon hardware trigger) — talk to an Arduino over serial via
[pyserial](https://pyserial.readthedocs.io/), which is a **core dependency**.
No extra install is needed:

```bash
octacam list-plugins      # bundled plugins and whether each can load
```

!!! note "The `flywheel` / `twophoton` extras also install nothing"
    Like `flir`, the `flywheel` and `twophoton` optional-dependency extras are
    empty documentation markers; pyserial already ships in the core dependencies
    so both plugins work out of the box once enabled.

Enable a plugin per-launch with `--plugin <name>` on `gui`/`record`, or
persistently with a `[[plugins]]` entry in the rig's `octacam_config.toml` — see
[Plugins](guide/plugins.md) and [Configuration](guide/configuration.md).

## Development install

To hack on octacam itself, clone it and let uv manage the environment:

```bash
git clone https://github.com/NeLy-EPFL/octacam.git
cd octacam
uv sync                     # create the venv with runtime + dev dependencies
uv run octacam --help       # run from the checkout
uv run pytest               # run the test suite (against Basler's emulator)
```

`uv sync` reads the `dev` dependency group (pytest, pytest-cov, ruff, pyright,
av, httpx, psutil) from `pyproject.toml`. To pull in later changes, `git pull`
and re-run `uv sync`.

### Build the documentation

This site is built with
[Material for MkDocs](https://squidfunk.github.io/mkdocs-material/). Preview it
locally with live reload using the `docs` dependency group:

```bash
uv run --group docs mkdocs serve     # → http://127.0.0.1:8000
```

## Troubleshooting

!!! failure "\"Insufficient system resources exist to complete the API\" at start of streaming"
    pylon's USB stack needs roughly **150 open file descriptors and ~16 MB of
    usbfs memory per camera**. octacam raises its own soft file-descriptor limit
    at startup, but if the session's hard limit is still too low (`ulimit -Hn`),
    raise it in `/etc/security/limits.conf` or run Basler's `setup-usb.sh`. Also
    make sure `usbcore.usbfs_memory_mb=1000` is set (check
    `/sys/module/usbcore/parameters/usbfs_memory_mb`).

## Next steps

Head to the [Quickstart](quickstart.md) to record your first trial — no cameras
required.
