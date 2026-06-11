# octacam

octacam is the successor to SeptaCam, a tool for previewing, recording, and saving video streams from multiple Basler cameras. It is designed to be fast, easy to use, and maintainable.

<p align="center">
  <img src="https://github.com/user-attachments/assets/a7b6ac6e-5ae3-45fa-ae5a-2e3f5281e5c3" width="400"/>
</p>

## Features
- Support >7 cameras (8 is not the limit despite the name)
- See live updates of all cameras while recording.
- Save frames directly to videos

## Repository layout

octacam is being migrated from C++ to a pip/uv-installable Python package:

- **Repo root** — the Python package (work in progress). Uses [pypylon](https://github.com/basler/pypylon) (which bundles the Basler pylon runtime — no manual SDK install), so installation is a single `uv`/`pip` command once released.
- **[cpp/](cpp/)** — the original C++/Qt6 implementation, fully functional. See [cpp/README.md](cpp/README.md) for build instructions. This remains the reference implementation until the Python package reaches feature parity.
- **[configs/](configs/)** — camera `.pfs` files and `octacam_config.yaml` per rig, shared by both implementations.
- **[benchmarks/](benchmarks/)** — Phase 0 performance benchmarks that gate the Python architecture (pure Python vs. native hot-path module). See [benchmarks/README.md](benchmarks/README.md).
- **[arduino_script/](arduino_script/)** — Arduino sketch for stepper motor control.

## Python development setup

Requires [uv](https://docs.astral.sh/uv/) and Python ≥ 3.11.

```bash
git clone https://github.com/NeLy-EPFL/octacam.git
cd octacam
uv sync
```

Run benchmarks against emulated cameras (no hardware needed):

```bash
uv run benchmarks/bench_pipeline.py --help
```
