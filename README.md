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
- Web GUI (`octacam gui`): control the rig from any browser; opens automatically
  on the local machine, and works remotely through a plain SSH tunnel
  (`ssh -L 8765:127.0.0.1:8765 <rig-hostname>`).

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

Hardware/integration plugins (flywheel, two-photon trigger) are opt-in but
their dependencies ship by default, so they work out of the box — just enable
the ones a rig needs (see [Plugins](#plugins)).

## Usage

```bash
octacam gui <config_dir>      # web GUI on http://127.0.0.1:8765 (--host/--port/--no-browser)
octacam record <config_dir>   # headless recording (--fps/--duration/--output/--codec
                              #   x264|raw/--crf/--preset/--x264-params/--trigger/
                              #   --record-form sensor|display/--save-frame-timestamps)
octacam transcode <paths...>  # transcode folders/videos offline (-r/--as-displayed)
octacam transcode --last      # …or the last recording / --session / --today (no paths)
octacam list-cameras          # show detected cameras (--backend basler|flir|fake)
octacam list-plugins          # show bundled plugins and whether each can load
octacam --help
```

`gui` and `record` also accept `--plugin <name>` (repeatable) and
`--no-plugins` (see [Plugins](#plugins)).

### Recording outputs

Each recording writes its videos plus one `recording_summary.json` in the save
directory. The summary holds what matters for checking a trial: per camera the
recording fps, the start timestamp, and which frame indices were dropped, plus
the session start wall-clock time and recording settings. It also documents what
the `dropped` count does *not* include — only frames the encoder/writer queue
could not accept are counted, not frames the camera or transport never delivered
(e.g. USB bandwidth gaps). For the latter, turn on the per-frame timestamp CSV
(`--save-frame-timestamps`, off by default) and inspect the inter-frame gaps.

By default frames are saved in **display** form: each camera's rotation/flips
(as configured in the GUI's View tab) are baked into the video so the file
matches what you see on screen. Pass `--record-form sensor` (or set
`gui.record_form_default = "sensor"`) to save the raw, untransformed sensor
image instead. Both are toggleable live in the web GUI's Record tab.

`octacam transcode` accepts any mix of folders and video files (and `-r` to
recurse). A folder is transcoded according to its `recording_summary.json` when
present; otherwise its `.mkv`/`.raw` files are transcoded with default
parameters and no transform (with a warning). `--as-displayed` applies each
video's recorded transform (skipped automatically when it was already baked in
at record time); the default reproduces the file as saved. Output container and
encoder settings default to mp4, `preset=veryslow`, `crf=20`, `pix_fmt=gray` and
are set per run with `--format/--crf/--preset/--pix-fmt/--x264-params`. Pass
`--remove-source` to delete each `.mkv`/`.raw` (and a `.raw`'s `.json` sidecar)
once it transcodes successfully — the `recording_summary.json` is always kept.

Progress is shown as an octacam-style bar (`[i/N] name`, percent, fps, speed,
elapsed) reformatted live from ffmpeg's output. Pass `--progress-style ffmpeg`
to stream ffmpeg's own native output verbatim instead.

Instead of typing paths, you can let octacam remember where it recorded. Every
finished recording (from the GUI or `octacam record`) is noted in a small cache
under `~/.cache/octacam` (override with `OCTACAM_CACHE_DIR`), so you can transcode
without retyping a single path:

```bash
octacam transcode --last      # the most recent recording folder
octacam transcode --session   # every folder from the last GUI session
octacam transcode --all       # every folder still in the cache
```

These combine with the encoding flags above (e.g. `octacam transcode --session
--format mkv`). `--session` means the *most recent* session; `--session-id <id>`
names an exact one if a later recording would otherwise steal "most recent" out
from under it. When a GUI session ends, octacam prints ready-to-run `--session`
and `--all` commands. Folders that were deleted between recording and transcoding
are silently skipped. The cache prunes itself (entries older than 30 days are
dropped on each write), so it never grows without bound.

Because transcoding is CPU-heavy, `octacam gui` and `octacam record` warn at
startup when a transcode is already running on the same machine (it competes with
live capture/encoding and can cause dropped frames), so you can choose to wait for
it to finish.

`octacam gui` opens the default browser automatically. It skips this over an SSH
session or on a headless host (where the browser would launch on the rig instead
of your machine); pass `--no-browser` to skip it explicitly.

For remote operation, tunnel the web GUI over SSH from your machine:

```bash
ssh -L 8765:127.0.0.1:8765 <rig-hostname> octacam gui <config_dir>
# then open http://localhost:8765
```

`<config_dir>` contains an optional `octacam_config.toml` (camera names, display
layout, GUI defaults, and the camera [`backend`](#camera-backends)) plus the
per-camera sensor-parameter files — `<serial>.pfs` for Basler, `<serial>.json`
for FLIR — see [configs/](configs/) for examples.

To run without hardware using Basler's camera emulation:

```bash
PYLON_CAMEMU=8 octacam gui configs/emulate_8_cameras
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
trigger, the per-camera exposure/gain/ROI controls, the recording summary (and
opt-in timestamp CSVs), and the web GUI — behaves identically across backends. `list-cameras` takes a matching
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
# bare names work too: plugins = ["flywheel"]
[[plugins]]
name = "flywheel"
options = { device = "/dev/ttyACM0", baud = 115200 }
```

or per-launch with `--plugin flywheel` (repeatable; adds to the config), and
disable everything for one run with `--no-plugins`. The bundled plugins'
dependencies ship by default, so they need no extra install. Run
`octacam list-plugins` to see the bundled plugins and whether each one can load.

Bundled plugins:

- **flywheel** — drives an Arduino stepper-motor controller over serial. Adds
  the web GUI's Flywheel tab (loop program + hold-to-jog), and fires an armed
  loop command at the first captured frame so motion is synced to capture. See
  [arduino/stepper_motor/](arduino/stepper_motor/) for the matching firmware.
  Uses pyserial, which ships with octacam by default.

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
- **[arduino/](arduino/)** — Arduino sketches: `stepper_motor/` (turntable stepper via the `flywheel` plugin) and `2photon_trigger/` (2-photon rig hardware trigger via the `twophoton` plugin).

## Development

Requires [uv](https://docs.astral.sh/uv/) and Python ≥ 3.11.

```bash
git clone https://github.com/NeLy-EPFL/octacam.git
cd octacam
uv sync
uv run pytest                  # runs against Basler's camera emulator
uv run benchmarks/bench_pipeline.py --help
```
