# octacam

Preview, record, and save synchronized video from many scientific cameras
through one fast, simple interface. octacam drives **Basler** (USB3, via
[pypylon](https://github.com/basler/pypylon)) and **FLIR / Teledyne** (USB3, via
Spinnaker / PySpin) cameras from a live web GUI, and turns a day's recordings
into archived videos with one command. It is the successor to SeptaCam.

<p align="center">
  <img src="https://github.com/user-attachments/assets/a7b6ac6e-5ae3-45fa-ae5a-2e3f5281e5c3" width="560"/>
</p>

## What it does

- **Drives many cameras at once** — 8 is not the limit despite the name.
- **Live web GUI** — preview every camera while recording, from the same machine
  or over SSH.
- **Records straight to video** — monochrome H.264 (or a raw byte dump), with a
  per-recording summary that records dropped frames and timing.
- **One-command post-processing** — `octacam process` transcodes, tiles cameras
  into composite grid videos, and copies everything to shared storage, all from
  a config snapshot each recording carries with it.
- **Opt-in plugins** for rig hardware (turntable, 2-photon trigger).

## Get started

<div class="grid cards" markdown>

- :material-download: **[Install](installation.md)** — one line with uv; Basler
  works out of the box.
- :material-rocket-launch: **[Quickstart](quickstart.md)** — record your first
  trial, with or without hardware.
- :material-tune: **[Configuration](guide/configuration.md)** — the
  `octacam_config.toml` reference.

</div>

## The workflow at a glance

```bash
# 1. Check the install and validate your rig
octacam doctor <config_dir>

# 2. Preview + record from the web GUI
octacam gui <config_dir>          # → http://127.0.0.1:8765

# 3. Transcode, build grid videos, and copy to storage — one command
octacam process --all
```

`octacam record <config_dir>` does step 2 headlessly (no browser) for scripted
or remote runs.

## Where to next

| Guide | |
| --- | --- |
| [Web GUI](guide/gui.md) | Preview, record, and remote operation over SSH |
| [Recording](guide/recording.md) | Outputs, the recording summary, transformed vs raw |
| [Processing](guide/processing.md) | Transcode, grid videos, and transfer to storage |
| [Configuration](guide/configuration.md) | The `octacam_config.toml` reference |
| [Camera backends](guide/backends.md) | Basler, FLIR/Teledyne, and the fake test backend |
| [Plugins](guide/plugins.md) | Flywheel turntable and 2-photon trigger |
| [CLI reference](reference/cli.md) | Every command and option |
| [Troubleshooting](reference/troubleshooting.md) | Common errors and fixes |
