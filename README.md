<h1 align="center">octacam</h1>

<p align="center">
  <a href="https://github.com/NeLy-EPFL/octacam/actions/workflows/ci.yml"><img src="https://github.com/NeLy-EPFL/octacam/actions/workflows/ci.yml/badge.svg" alt="CI"></a>
  <a href="https://nely-epfl.github.io/octacam/"><img src="https://github.com/NeLy-EPFL/octacam/actions/workflows/docs.yml/badge.svg" alt="Docs"></a>
  <img src="https://img.shields.io/badge/python-3.14-blue" alt="Python 3.14">
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-MIT-blue" alt="License: MIT"></a>
</p>

Synchronized video from many scientific cameras: octacam previews and records Basler, FLIR, and any GenICam USB3-Vision camera from a live web GUI, and turns a day's recordings into archived videos with one command.

## Installation

```bash
git clone https://github.com/NeLy-EPFL/octacam && cd octacam
uv tool install .
```

A clone is on `main`, the latest stable release, and brings the example rigs in `configs/`. Basler and other USB3-Vision cameras work out of the box; FLIR cameras also need Teledyne's Spinnaker SDK and its PySpin wheel ([installation guide](https://nely-epfl.github.io/octacam/stable/installation/)). `octacam doctor` reports what the installation can do.

## Quickstart

No cameras attached? Eight emulated Basler cameras stand in:

```bash
PYLON_CAMEMU=8 octacam gui configs/emulate_basler
```

On a rig, point octacam at its config directory:

```bash
octacam config <config_dir>   # scaffold the rig's config
octacam gui <config_dir>      # preview and record in the browser
octacam process --all         # transcode, build grid videos, copy to storage
```

Each recording is a folder with one video per camera and an `octacam_recording/` subfolder holding its summary, its timestamps, and the config it ran with, which `octacam process` reads.

## Documentation

[nely-epfl.github.io/octacam](https://nely-epfl.github.io/octacam/)

- [Quickstart](https://nely-epfl.github.io/octacam/stable/quickstart/): a first recording, with or without hardware.
- [Recording](https://nely-epfl.github.io/octacam/stable/guide/recording/) and [Processing](https://nely-epfl.github.io/octacam/stable/guide/processing/): what a recording holds, and how it becomes archived videos.
- [Configuration](https://nely-epfl.github.io/octacam/stable/guide/configuration/): every key of `octacam_config.toml`.
- [Camera backends](https://nely-epfl.github.io/octacam/stable/guide/backends/) and [Plugins](https://nely-epfl.github.io/octacam/stable/guide/plugins/): the camera drivers, and the Arduino-driven rig hardware.
- [CLI reference](https://nely-epfl.github.io/octacam/stable/cli/) and [Troubleshooting](https://nely-epfl.github.io/octacam/stable/reference/troubleshooting/).

Contributing: see [CONTRIBUTING.md](CONTRIBUTING.md).

<!-- --8<-- [start:citing] -->
## Citation

There is no paper on octacam: please cite the software, as below or with GitHub's "Cite this repository" button, which reads [`CITATION.cff`](https://github.com/NeLy-EPFL/octacam/blob/main/CITATION.cff).

```bibtex
@software{lam_octacam_2026,
  author = {Lam, Thomas Ka Chung and Durrieu, Matthias},
  title  = {octacam: synchronized video from many scientific cameras},
  year   = {2026},
  url    = {https://github.com/NeLy-EPFL/octacam},
}
```
<!-- --8<-- [end:citing] -->

## Acknowledgments

octacam is developed in the [Ramdya lab](https://www.epfl.ch/labs/ramdya-lab/) at EPFL and succeeds [SeptaCam](https://github.com/NeLy-EPFL/SeptaCam). Matthias Durrieu wrote its grid videos, its transfer to storage, and the two-photon trigger plugin. It drives cameras through pypylon, Teledyne's Spinnaker SDK, and pycameleon, among others: [Citing and credits](https://nely-epfl.github.io/octacam/stable/citing/) lists them.

<!-- --8<-- [start:ai] -->
## Use of AI

octacam was developed with extensive help from AI coding assistants, mainly [Claude Code](https://claude.com/claude-code) (Anthropic). Directed by the authors, they wrote much of the code, tests, and documentation, including this README. The authors set the goals, made the design decisions, reviewed the changes, and checked the results against data, and are responsible for the software. As with any tool, validate its output on your own rig before relying on it.
<!-- --8<-- [end:ai] -->

## License

[MIT](LICENSE), © 2026 Neuroengineering Laboratory (Ramdya lab), EPFL; the camera SDKs and FFmpeg keep their own licenses ([Citing and credits](https://nely-epfl.github.io/octacam/stable/citing/)).
