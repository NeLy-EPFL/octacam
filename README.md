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
  throughput with offline transcoding (`octacam process`).
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
octacam record <config_dir>   # headless recording (--fps/--duration/--output overrides)
octacam process <paths...>    # transcode + grid + transfer, all config-driven
                              #   (-r/--no-transcode/--no-grid/--no-transfer/--dry-run)
octacam process --last        # …or the last recording / --session / --all (no paths)
octacam list-cameras          # show detected cameras (--backend basler|flir|fake)
octacam list-plugins          # show bundled plugins and whether each can load
octacam --help
```

`gui` and `record` also accept `--plugin <name>` (repeatable) and
`--no-plugins` (see [Plugins](#plugins)).

Everything after recording — transcoding, composite grid videos, and copying to
a shared destination — is one command, **`octacam process`**. It reads all its settings
(encoder parameters, grid layouts, transfer destination) from a copy of the
rig's `octacam_config.toml` that each recording saves into its own folder, so no
`--config` is needed. Skip any step with `--no-transcode` / `--no-grid` /
`--no-transfer`.

### Recording outputs

Each recording writes its videos plus one `recording_summary.json` in the save
directory. The summary holds what matters for checking a trial: per camera the
recording fps, the start timestamp, and which frame indices were dropped, plus
the session start wall-clock time and recording settings. It also documents what
the `dropped` count does *not* include — only frames the encoder/writer queue
could not accept are counted, not frames the camera or transport never delivered
(e.g. USB bandwidth gaps). For the latter, turn on the per-frame timestamp CSV
(`record.save_timestamps = true`, off by default) and inspect the inter-frame gaps.

By default frames are saved **transformed**: each camera's rotation/flips (as
configured in the GUI's View tab) are baked into the video so the file matches
what you see on screen. Set `record.save_transformed = false` to save the raw,
untransformed sensor image instead. This is toggleable live in the web GUI's
Record tab. A raw recording (`record.save_method = "raw"`) writes only the
`.raw` byte dump per camera; its width/height/pixel-format/fps live in
`recording_summary.json`, so `octacam process` can transcode it later without a
per-camera sidecar.

Each recording also saves a copy of the rig's `octacam_config.toml` into its own
folder. That is what lets `octacam process` transcode, grid, and transfer with
no `--config`: it reads the encoder args (`[transcode].ffmpeg_params`), grid
layouts (`[[visualization]]`), and transfer destination (`[transfer]`) from that
embedded copy.

`octacam process` accepts any mix of recording folders (and `-r` to recurse). A
folder is transcoded to mp4 according to its `recording_summary.json`; encoder
settings come from the config, not the command line. Recordings are reproduced
as saved (any display transform was already baked in at record time). Pass
`--remove-source` to delete each `.mkv`/`.raw` once it transcodes successfully —
the `recording_summary.json` is always kept. Skip any step with
`--no-transcode` / `--no-grid` / `--no-transfer` (e.g. `--no-transcode
--no-transfer` regenerates just the grids after a layout change).

Progress is shown as an octacam-style bar (`[i/N] name`, percent, fps, speed,
elapsed) reformatted live from ffmpeg's output. Pass `--progress-style ffmpeg`
to stream ffmpeg's own native output verbatim instead.

Instead of typing paths, you can let octacam remember where it recorded. Every
finished recording (from the GUI or `octacam record`) is noted in a small cache
under `~/.cache/octacam` (override with `OCTACAM_CACHE_DIR`), so you can process
without retyping a single path:

```bash
octacam process --last      # the most recent recording folder
octacam process --session   # every folder from the last GUI session
octacam process --all       # every folder still in the cache
```

`--session` means the *most recent* session; `--session-id <id>` names an exact
one if a later recording would otherwise steal "most recent" out from under it.
When a GUI session ends, octacam prints ready-to-run `--session` and `--all`
commands. Folders that were deleted between recording and processing are silently
skipped. The cache prunes itself (entries older than 30 days are dropped on each
write), so it never grows without bound.

### Grid video

`octacam process` generates one composite video per recording folder that tiles
all cameras in a configurable grid, right after the individual files are
transcoded. Each grid comes from a `[[visualization]]` entry in the rig's
`octacam_config.toml`; list several to produce several composites. Each cell is a
camera name (as declared in `[[cameras]]`); an empty string `""` places a black
fill. All rows must have the same number of columns.

```toml
# configs/my_rig/octacam_config.toml

[[visualization]]
name = "grid.mp4"            # output filename inside each recording folder
layout = [
    ["camera_LF", "",          "camera_RF"],
    ["camera_LM", "camera_F",  "camera_RM"],
    ["camera_LH", "",          "camera_RH"],
]
```

The grid is generated whenever `process` runs (skip it with `--no-grid`). If a
config lists `[[cameras]]` but no `[[visualization]]`, a near-square layout is
derived from that rig's own cameras. A layout cell naming a camera that isn't in
`[[cameras]]` is reported (and renders black) instead of failing silently. For an
8-camera rig add `camera_H` in the bottom-centre cell instead of the empty
string — see [2p_1](configs/2p_1/octacam_config.toml) (7 cameras) and
[emulate_8_cameras](configs/emulate_8_cameras/octacam_config.toml) (8 cameras).

To regenerate the grid for already-transcoded folders without re-running
the full transcode:

```bash
# single folder — grid only (skip transcode + transfer)
octacam process /data/octacam/260620-wt/Fly1/001-bhv --no-transcode --no-transfer

# whole experiment tree at once — grids only
octacam process /data/octacam/260620-wt -r --no-transcode --no-transfer
```

### Transfer

`octacam process` copies all transcoded mp4s, grid videos, and
`recording_summary.json` to the transfer destination (a network share or any
writable path), mirroring the fly/trial directory tree. The destination is
`transfer.directory` joined with the recording's
`relative_directory` (the sub-path resolved at record time and stored in the
summary), so a recording made under `.../260620-wt/Fly1/001-bhv` lands at
`<transfer.directory>/260620-wt/Fly1/001-bhv` and distinct trials that share a
name never collide. Configure it once in the rig's `octacam_config.toml`:

```toml
[transfer]
directory = "/mnt/store/matthias"                # strftime %-codes expand here too
checksum = true                                  # content-verify each copy (default)
```

**Integrity and resume.**  Each file is streamed to a temporary name and only
swapped onto its final name once it is whole and content-verified (a blake2b
checksum of the source is compared against the written copy), so an interrupted
copy never leaves a truncated file masquerading as complete.  Re-running skips
files already present (by size), so a copy that was killed part-way simply
resumes — at most the one in-progress file is redone. Set `checksum = false` for
a faster size-only verify on trusted/fast links.

### One-command end-of-day workflow

Because each recording embeds its own config, the entire post-recording
pipeline — transcode → grid → transfer — is a single command with no `--config`:

```bash
octacam process --all
```

Each step shows a live progress bar (frame/fps/speed for transcode and grid;
MB/s per file for transfer, then a verify pass).  `--dry-run` logs the intended
grid ffmpeg call and transfer plan without writing anything — useful for
validating paths on a new workstation.

Because transcoding is CPU-heavy, `octacam gui` and `octacam record` warn at
startup when an `octacam process` is already transcoding on the same machine (it
competes with live capture/encoding and can cause dropped frames), so you can
choose to wait for it to finish.

`octacam gui` opens the default browser automatically. It skips this over an SSH
session or on a headless host (where the browser would launch on the rig instead
of your machine); pass `--no-browser` to skip it explicitly.

For remote operation, tunnel the web GUI over SSH from your machine:

```bash
ssh -L 8765:127.0.0.1:8765 <rig-hostname> octacam gui <config_dir>
# then open http://localhost:8765
```

`<config_dir>` contains an optional `octacam_config.toml` (camera names, display
layout, the `[record]`/`[transcode]`/`[[visualization]]`/`[transfer]` settings,
and the camera [`backend`](#camera-backends)) plus the
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
- **twophoton** — arms an Arduino hardware camera trigger for a 2-photon rig.
  The Arduino waits for a ThorSync rising edge, then emits a square-wave trigger
  at the recording's fps for its duration. Adds the web GUI's 2-Photon tab (live
  Arduino state + "arm with recording") and arms at recording start so capture
  is synced to the ThorSync edge. See
  [arduino/2photon_trigger/](arduino/2photon_trigger/) for the firmware and
  setup. Uses pyserial, which ships with octacam by default.

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
