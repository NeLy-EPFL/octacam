# octacam

Preview, record, and save synchronized video from many scientific cameras
through one fast, simple interface. octacam drives **Basler** (USB3, via pypylon)
and **FLIR / Teledyne** (USB3, via Spinnaker/PySpin) cameras from a live web GUI,
and turns a day's recordings into archived videos with one command. It is the
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

The Basler runtime is bundled, so a Basler rig works out of the box. FLIR /
Teledyne cameras need the Spinnaker SDK installed separately — see the
[installation guide](https://nely-epfl.github.io/octacam/installation/).

## Quickstart

No cameras attached? Try the built-in Basler emulator first:

```bash
PYLON_CAMEMU=8 octacam gui configs/emulate_8_cameras   # 8 fake cameras, live GUI
```

On a real rig, point octacam at a config directory (camera names, layout, and
recording settings — see [configs/](configs/) for examples):

```bash
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
| [Camera backends](https://nely-epfl.github.io/octacam/guide/backends/) | Basler, FLIR/Teledyne, and the fake test backend |
| [Plugins](https://nely-epfl.github.io/octacam/guide/plugins/) | Flywheel turntable and 2-photon trigger |
| [Troubleshooting](https://nely-epfl.github.io/octacam/reference/troubleshooting/) | Common errors and fixes |

## Repository layout

- [src/octacam/](src/octacam/) — the Python package
- [configs/](configs/) — example rig configs (`octacam_config.toml` + per-camera sensor files)
- [docs/](docs/) — documentation site source ([Material for MkDocs](https://squidfunk.github.io/mkdocs-material/))
- [arduino/](arduino/) — firmware for the `flywheel` and `twophoton` plugins
- [benchmarks/](benchmarks/) — performance benchmarks
- [design/](design/) — internal design notes and research
