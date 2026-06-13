# octacam

octacam is the successor to SeptaCam, a tool for previewing, recording, and saving video streams from multiple scientific cameras. It drives Basler (USB3, via pypylon) and FLIR/Teledyne (USB3, via Spinnaker/PySpin) cameras through one common interface, and is designed to be fast, easy to use, and maintainable. See [Camera backends](#camera-backends).

<p align="center">
  <img src="https://github.com/user-attachments/assets/a7b6ac6e-5ae3-45fa-ae5a-2e3f5281e5c3" width="400"/>
</p>

## Features
- Support >7 cameras (8 is not the limit despite the name)
- See live updates of all cameras while recording.
- Save frames directly to videos: H.264 (ffmpeg/x264, true monochrome
  4:0:0, crash-safe MKV) by default, or raw Mono8 dumps for maximum
  throughput with offline transcoding (`octacam transcode`).
- Web GUI (`octacam serve`): control the rig from any browser; works
  remotely through a plain SSH tunnel (`ssh -L 8000:127.0.0.1:8000 <rig-hostname>`).

## Installation

octacam is a Python package. All dependencies — including the Basler pylon
runtime (bundled by [pypylon](https://github.com/basler/pypylon)), OpenCV, and
ffmpeg — install automatically; no SDK downloads or C++ toolchain needed.

```bash
uv tool install git+https://github.com/NeLy-EPFL/octacam.git
```

or, from a clone: `uv tool install .` (or `pip install .`).

The Basler runtime is bundled, so a Basler rig works out of the box. **FLIR
cameras need the Spinnaker SDK installed separately** (PySpin is not on PyPI) —
see [FLIR / Teledyne setup](#flir--teledyne-setup).

Optional plugins ship as extras — install the ones you need, e.g. the Arduino
stepper controller: `pip install "octacam[arduino]"` (see [Plugins](#plugins)).

## Usage

```bash
octacam serve <config_dir>    # web GUI on http://127.0.0.1:8000 (--host/--port)
octacam record <config_dir>   # headless recording (--fps/--duration/--output/
                              #   --codec x264|raw|mjpg|h264/--crf/--preset/--trigger)
octacam transcode <dir>       # convert .raw recordings to x264 MKV offline
octacam list-cameras          # show detected cameras (--backend basler|flir|fake)
octacam --help
```

`serve` and `record` also accept `--plugin <name>` (repeatable) and
`--no-plugins` (see [Plugins](#plugins)).

For remote operation, tunnel the web GUI over SSH from your machine:

```bash
ssh -L 8000:127.0.0.1:8000 <rig-hostname> octacam serve <config_dir>
# then open http://localhost:8000
```

`<config_dir>` contains an optional `octacam_config.toml` (camera names, display
layout, GUI defaults, and the camera [`backend`](#camera-backends)) plus the
per-camera sensor-parameter files — `<serial>.pfs` for Basler, `<serial>.json`
for FLIR — see [configs/](configs/) for examples.

To run without hardware using Basler's camera emulation:

```bash
PYLON_CAMEMU=8 octacam serve configs/emulate_8_cameras
```

## Camera backends

octacam drives more than one camera vendor through a common interface. Pick the
backend per rig with a top-level `backend` key in `octacam_config.toml` (one
vendor per config directory):

```toml
backend = "basler"   # default — omit the key and Basler is assumed
# backend = "flir"   # FLIR / Teledyne (Spinnaker / PySpin)
```

| `backend` | SDK                     | per-camera parameter file | notes                       |
| --------- | ----------------------- | ------------------------- | --------------------------- |
| `basler`  | pypylon (bundled)       | `<serial>.pfs`            | default                     |
| `flir`    | Spinnaker / PySpin      | `<serial>.json`           | runs the sensor in Mono8    |
| `fake`    | none (in-memory)        | `<serial>.json`           | synthetic frames for tests  |

Everything else — preview, recording (monochrome H.264 or raw), the software
trigger, the per-camera exposure/gain/ROI controls, the timestamp CSVs, and the
web GUI — behaves identically across backends. `list-cameras` takes a matching
`--backend`:

```bash
octacam list-cameras --backend flir
```

### FLIR / Teledyne setup

PySpin is not on PyPI; it ships with Teledyne's Spinnaker SDK. Install the SDK
and its PySpin wheel into octacam's environment, then (optionally) mark the
intent with the `flir` extra:

```bash
# 1. Install the Spinnaker SDK for your platform (from Teledyne).
# 2. Install the matching PySpin wheel into octacam's environment:
pip install spinnaker_python-*.whl
# 3. (optional) records the dependency; installs nothing on its own:
pip install "octacam[flir]"
```

If PySpin is missing when a FLIR config is served, octacam exits with a clear
message rather than a traceback.

## Plugins

Optional hardware/integration features ship as opt-in plugins under
`octacam.plugins.*`. **The default launch loads none** — you choose what each
rig needs. Enable a plugin persistently in the rig's `octacam_config.toml`:

```toml
# bare names work too: plugins = ["arduino"]
[[plugins]]
name = "arduino"
options = { device = "/dev/ttyACM0", baud = 115200 }
```

or per-launch with `--plugin arduino` (repeatable; adds to the config), and
disable everything for one run with `--no-plugins`. Each plugin declares its
own extra dependencies — install them with the matching extra, e.g.
`pip install "octacam[arduino]"`.

Bundled plugins:

- **arduino** — drives an Arduino stepper-motor controller over serial. Adds
  the web GUI's Arduino tab (loop program + hold-to-jog), and fires an armed
  loop command at the first captured frame so motion is synced to capture. See
  [arduino_script/](arduino_script/) for the matching firmware. Extra:
  `octacam[arduino]` (pyserial).

## Troubleshooting

**"Insufficient system resources exist to complete the API" at start of
streaming** — pylon's USB stack needs ~150 open file descriptors and ~16 MB of
usbfs memory per camera. `octacam` raises its own soft file-descriptor limit at
startup, but if the hard limit of the session is still too low (`ulimit -Hn`),
raise it in `/etc/security/limits.conf` or run Basler's `setup-usb.sh`. Also
make sure `usbcore.usbfs_memory_mb=1000` is set (see
`/sys/module/usbcore/parameters/usbfs_memory_mb`).

## Repository layout

- **[src/octacam/](src/octacam/)** — the Python package.
- **[configs/](configs/)** — camera `.pfs` files and `octacam_config.toml` per
  rig.
- **[benchmarks/](benchmarks/)** — Phase 0 performance benchmarks that gated the
  pure-Python architecture. See [benchmarks/README.md](benchmarks/README.md).
- **[arduino_script/](arduino_script/)** — Arduino sketch for stepper motor control.

## Development

Requires [uv](https://docs.astral.sh/uv/) and Python ≥ 3.11.

```bash
git clone https://github.com/NeLy-EPFL/octacam.git
cd octacam
uv sync
uv run pytest                  # runs against Basler's camera emulator
uv run benchmarks/bench_pipeline.py --help
```
