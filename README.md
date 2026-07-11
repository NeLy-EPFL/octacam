# octacam

Preview, record, and save synchronized video from many scientific cameras
through one fast, simple interface. octacam drives **Basler**, **FLIR /
Teledyne**, and **any GenICam USB3-Vision** camera from a live web GUI, and turns
a day's recordings into archived videos with one command. It auto-detects the
best available driver per camera (a [backend cascade](https://nely-epfl.github.io/octacam/guide/backends/))
with a pip-installable floor, so it just works on modern Python. It is the
successor to SeptaCam.

<p align="center">
  <img src="https://github.com/user-attachments/assets/a7b6ac6e-5ae3-45fa-ae5a-2e3f5281e5c3" width="480"/>
</p>

- 📹 **Many cameras at once** — 8 is not the limit despite the name
- 🖥️ **Live web GUI** — preview every camera while recording; run it locally or over SSH
- 💾 **Record straight to video** — monochrome H.264 (or raw) with per-frame drop tracking
- ⚙️ **One-command post-processing** — transcode, tile into grid videos, and copy to storage
- 📦 **One-line install** with [uv](https://docs.astral.sh/uv/)

📖 **Full documentation: <https://nely-epfl.github.io/octacam/>**

## Install

```bash
uv tool install git+https://github.com/NeLy-EPFL/octacam.git
```

The Python-installable backends (pypylon and the always-on pycameleon floor)
all ship in core, so **a rig works out of the box** on Python 3.10+. One tier is
an optional manual install: the FLIR vendor SDK (Spinnaker/PySpin, Python ≤3.10).
See the [installation guide](https://nely-epfl.github.io/octacam/installation/) and
[camera backends](https://nely-epfl.github.io/octacam/guide/backends/).

## Quickstart

No cameras attached? Try the built-in Basler emulator first:

```bash
PYLON_CAMEMU=8 octacam gui configs/emulate_basler   # 8 fake cameras, live GUI
```

On a real rig, point octacam at a config directory (camera names, layout, and
recording settings — see [configs/](configs/) for examples):

```bash
octacam config <config_dir>    # scaffold a new rig config interactively
octacam doctor <config_dir>    # check the install + validate the rig
octacam gui <config_dir>       # live web GUI on http://127.0.0.1:8765
octacam record <config_dir>    # headless recording (no browser)
octacam process --all          # transcode + grid + copy everything you recorded
```

Everything after recording — transcoding, composite grid videos, and copying to
a shared destination — is the single command **`octacam process`**, driven by a
config snapshot each recording saves alongside its videos.

## Commands

| Command | What it does |
| --- | --- |
| `octacam config [config_dir]` | Interactively scaffold a new rig config (`--backend`/`--force`/`--no-snapshot-params`) |
| `octacam doctor [config_dir]` | Diagnose the install and list cameras/plugins; validate a rig |
| `octacam gui <config_dir>` | Launch the live web GUI (`--host`/`--port`/`--no-browser`) |
| `octacam record <config_dir>` | Record headlessly (`--fps`/`--duration`/`--output`) |
| `octacam process <paths…>` | Transcode, build grids, and transfer (config-driven) |

Run `octacam --help` (or `<command> --help`) for the full option list.

## Documentation

| Guide | |
| --- | --- |
| [Installation](https://nely-epfl.github.io/octacam/installation/) | Install, update, FLIR setup, development install |
| [Quickstart](https://nely-epfl.github.io/octacam/quickstart/) | Your first recording, with or without hardware |
| [Web GUI](https://nely-epfl.github.io/octacam/guide/gui/) | Preview, record, and remote operation over SSH |
| [Recording](https://nely-epfl.github.io/octacam/guide/recording/) | Outputs, the recording summary, transformed vs raw |
| [Processing](https://nely-epfl.github.io/octacam/guide/processing/) | Transcode, grid videos, and transfer to storage |
| [Configuration](https://nely-epfl.github.io/octacam/guide/configuration/) | The `octacam_config.toml` reference |
| [Camera backends](https://nely-epfl.github.io/octacam/guide/backends/) | The auto-detect cascade (Basler → FLIR → Spinnaker → pycameleon) |
| [Plugins](https://nely-epfl.github.io/octacam/guide/plugins/) | Flywheel turntable, 2-photon trigger, and the configurable triggerbox (camera trigger + lights) |
| [Troubleshooting](https://nely-epfl.github.io/octacam/reference/troubleshooting/) | Common errors and fixes |

## License

octacam is released under the [MIT License](LICENSE).
© 2026 Neuroengineering Laboratory @EPFL — Ramdya Lab.
