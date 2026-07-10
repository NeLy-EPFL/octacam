# octacam

Preview, record, and save synchronized video from multiple scientific cameras
through one fast, simple interface. octacam drives **Basler** (USB3, via pypylon)
and **FLIR / Teledyne** (USB3, via Spinnaker / PySpin) cameras from a live web
GUI or a headless CLI, and turns a day's recordings into archived videos with one
command. It is the successor to SeptaCam.

<p align="center">
  <img src="https://github.com/user-attachments/assets/a7b6ac6e-5ae3-45fa-ae5a-2e3f5281e5c3" width="560"/>
</p>

## What it does

- **Drives many cameras at once** — 8 is not the limit despite the name. All
  cameras open, start, and stream in parallel.
- **Live web GUI** — preview every camera while recording, from the same machine
  or over a plain SSH tunnel. See [Web GUI](guide/gui.md).
- **Records straight to video** — monochrome H.264 by default (ffmpeg / libx264,
  true 4:0:0 `gray`, crash-safe MKV), or a raw Mono8 byte dump for maximum
  throughput with offline transcoding. See [Recording](guide/recording.md).
- **Synchronized capture** — pace a software trigger from octacam at your target
  fps, or slave the rig to an external hardware trigger. See
  [Camera backends](guide/backends.md).
- **A per-recording summary** — every recording writes a `recording_summary.json`
  (per-camera fps, start timestamps, and dropped frame indices) alongside the
  videos, plus an optional per-frame timestamp CSV.
- **One-command post-processing** — `octacam transcode` re-encodes recordings,
  optionally tiles the cameras into a composite grid video, and mirrors results
  to a NAS. See [Processing recordings](guide/processing.md).
- **Opt-in plugins** for rig hardware — a flywheel turntable and a 2-photon
  camera trigger. See [Plugins](guide/plugins.md).

## Get started

<div class="grid cards" markdown>

- :material-download: **[Install](installation.md)** — one line with uv; a Basler
  rig works out of the box.
- :material-rocket-launch: **[Quickstart](quickstart.md)** — record your first
  trial, with or without hardware.
- :material-tune: **[Configuration](guide/configuration.md)** — the
  `octacam_config.toml` reference.

</div>

## The workflow at a glance

```bash
# 1. Preview + record from the web GUI
octacam gui <config_dir>                        # → http://127.0.0.1:8765

# 2. …or record headlessly (no browser) for scripted / remote runs
octacam record <config_dir>

# 3. Transcode, build grid videos, and copy to storage — one command
octacam transcode --all --config <config_dir>
```

A rig's `<config_dir>` holds an `octacam_config.toml` (camera names, display
layout, GUI defaults, the camera [`backend`](guide/backends.md), and optional
plugins) plus one per-camera sensor-parameter file — `<serial>.pfs` for Basler,
`<serial>.json` for FLIR.

!!! tip "Run without any hardware"

    The Basler runtime ships with a camera emulator, so you can explore the GUI
    and the full pipeline on any machine:

    ```bash
    PYLON_CAMEMU=8 octacam gui configs/emulate_8_cameras
    ```

!!! note "FLIR / Teledyne cameras"

    Basler works with no extra setup (the pylon runtime is bundled). FLIR
    cameras need Teledyne's Spinnaker SDK and its PySpin wheel installed
    separately (PySpin is not on PyPI). See [Installation](installation.md).

## The commands

octacam is a single `octacam` executable with seven subcommands. Every command
accepts `-h`/`--help`, and `--version` prints the version (`0.2.0.dev0`).

| Command | What it does |
| --- | --- |
| `octacam gui <config_dir>` | Launch the web GUI for a rig |
| `octacam record <config_dir>` | Record headlessly (no browser) |
| `octacam transcode [paths…]` | Re-encode recordings; optionally build grids and copy to a NAS |
| `octacam grid <paths…>` | Build a composite grid video from transcoded folders |
| `octacam nas <paths…>` | Mirror recordings to a NAS, preserving the directory tree |
| `octacam list-cameras` | List detected cameras (`--backend basler\|flir\|fake`) |
| `octacam list-plugins` | List the bundled plugins and whether each can load |

See the [CLI reference](reference/cli.md) for every option.

## Where to next

| Guide | |
| --- | --- |
| [Web GUI](guide/gui.md) | Preview, record, and remote operation over SSH |
| [Recording](guide/recording.md) | Outputs, the recording summary, display vs sensor form |
| [Processing recordings](guide/processing.md) | Transcode, grid videos, and NAS export |
| [Configuration](guide/configuration.md) | The `octacam_config.toml` reference |
| [Camera backends](guide/backends.md) | Choosing Basler, FLIR, or the fake test backend per rig |
| [Plugins](guide/plugins.md) | The flywheel turntable and 2-photon trigger |
| [CLI reference](reference/cli.md) | Every command and option |
| [Troubleshooting](reference/troubleshooting.md) | Common errors and fixes |
