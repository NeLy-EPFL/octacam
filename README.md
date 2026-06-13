# octacam

octacam is the successor to SeptaCam, a tool for previewing, recording, and saving video streams from multiple Basler cameras. It is designed to be fast, easy to use, and maintainable.

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
runtime (bundled by [pypylon](https://github.com/basler/pypylon)), Qt, OpenCV,
and ffmpeg — install automatically; no SDK downloads or C++ toolchain needed.

```bash
uv tool install git+https://github.com/NeLy-EPFL/octacam.git
```

or, from a clone: `uv tool install .` (or `pip install .`).

## Usage

```bash
octacam serve <config_dir>    # web GUI on http://127.0.0.1:8000 (--host/--port)
octacam gui <config_dir>      # Qt GUI (also: bare `octacam` for ./)
octacam record <config_dir>   # headless recording (--fps/--duration/--output/
                              #   --codec x264|raw|mjpg|h264/--crf/--preset/--trigger)
octacam transcode <dir>       # convert .raw recordings to x264 MKV offline
octacam list-cameras          # show detected cameras
octacam --help
```

For remote operation, tunnel the web GUI over SSH from your machine:

```bash
ssh -L 8000:127.0.0.1:8000 <rig-hostname> octacam serve <config_dir>
# then open http://localhost:8000
```

`<config_dir>` contains the per-camera `.pfs` Basler configuration files and an
optional `octacam_config.yaml` (camera names, display layout, GUI defaults) —
see [configs/](configs/) for examples.

To run without hardware using Basler's camera emulation:

```bash
PYLON_CAMEMU=8 octacam gui configs/emulate_8_cameras
```

## Troubleshooting

**"Insufficient system resources exist to complete the API" at start of
streaming** — pylon's USB stack needs ~150 open file descriptors and ~16 MB of
usbfs memory per camera. `octacam` raises its own soft file-descriptor limit at
startup, but if the hard limit of the session is still too low (`ulimit -Hn`),
raise it in `/etc/security/limits.conf` or run Basler's `setup-usb.sh`. Also
make sure `usbcore.usbfs_memory_mb=1000` is set (see
`/sys/module/usbcore/parameters/usbfs_memory_mb`).

**GUI exits with "Could not load the Qt platform plugin 'xcb'"** — install the
cursor library required by Qt ≥ 6.5: `sudo apt install libxcb-cursor0`.

## Repository layout

- **[src/octacam/](src/octacam/)** — the Python package.
- **[cpp/](cpp/)** — the original C++/Qt6 implementation, kept as the reference
  until the Python port is validated on the acquisition rig. See
  [cpp/README.md](cpp/README.md) for build instructions.
- **[configs/](configs/)** — camera `.pfs` files and `octacam_config.yaml` per
  rig, shared by both implementations.
- **[benchmarks/](benchmarks/)** — Phase 0 performance benchmarks that gated the
  pure-Python architecture. See [benchmarks/README.md](benchmarks/README.md).
- **[arduino_script/](arduino_script/)** — Arduino sketch for stepper motor control.

## Development

Requires [uv](https://docs.astral.sh/uv/) and Python ≥ 3.11.

```bash
git clone https://github.com/NeLy-EPFL/octacam.git
cd octacam
uv sync
uv run pytest                  # includes an offscreen GUI test on emulated cameras
uv run benchmarks/bench_pipeline.py --help
```
