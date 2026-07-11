# Installation

octacam is installed from git with [uv](https://docs.astral.sh/uv/) — there is no
PyPI package. Clone the repository and install it as a uv tool. Cloning is the
recommended path for everyone, not just developers: the repo ships the `configs/`
example rigs and the emulator config the [Quickstart](quickstart.md) uses to run
with no hardware.

!!! tip "Don't have uv?"
    Install it with `curl -LsSf https://astral.sh/uv/install.sh | sh` (see the
    [uv install docs](https://docs.astral.sh/uv/getting-started/installation/)).

## Install

```bash
git clone https://github.com/NeLy-EPFL/octacam.git
cd octacam
uv tool install .            # puts an `octacam` command on your PATH
octacam doctor               # diagnose the install and list detected cameras
```

A fresh clone is on `main`, which is **always the latest stable release**, so this
installs stable octacam. `uv tool install` manages an isolated Python and every
dependency for you; the `octacam` command then works from any directory.

## Update

Run this from inside your octacam clone (the folder you `git clone`d into):

```bash
git pull && uv tool install --force .
```

`git pull` advances your clone to the latest stable `main`; `--force` rebuilds the
tool so the `octacam` command matches what you just pulled. Check what you are
running with `octacam --version`. octacam never updates itself — you choose when to
pull. Using a FLIR camera? Read [Keep PySpin across updates](#cameras) first — a
plain reinstall drops the vendor wheel.

## Pin to a release, or follow development

`main` follows the latest stable release. From inside your clone, check out a
different ref and reinstall to hold a rig on a specific release:

```bash
git checkout v0.3.0 && uv tool install --force .   # pin to a tagged release
git checkout main   && uv tool install --force .   # back to the latest stable
```

Releases are tagged `vX.Y.Z` — see the
[releases page](https://github.com/NeLy-EPFL/octacam/releases) and the repository
root's `CHANGELOG.md`. To follow active development instead, check out the current
`dev-*` branch before installing.

## Cameras

Basler (pypylon) and the always-on pycameleon USB3-Vision floor ship in core, so
**a rig works out of the box**; the only system requirement is `libusb-1.0`.
octacam auto-detects the best driver per camera — run `octacam doctor` to see which
backend each camera would use.

FLIR / Teledyne cameras additionally need Teledyne's Spinnaker SDK, plus the PySpin
wheel for the default FLIR tier (cp310–cp314; the SDK's C-API backend works without
it). See [Camera backends](guide/backends.md) for the setup.

!!! warning "Keep PySpin across updates"
    PySpin is a manually installed vendor wheel, **not** a declared dependency, so
    rebuilding or syncing the environment on update drops it unless you re-include
    it. Point `spinnaker_python-*.whl` at the wheel you installed the Spinnaker SDK
    from:

    - **Tool install (the default):** pass the wheel every time you install or
      update — `uv tool install --force . --with spinnaker_python-*.whl`.
    - **Development checkout:** install it once with
      `uv pip install spinnaker_python-*.whl`, then update with `uv sync --inexact`.
      A plain `uv sync` prunes packages that aren't declared dependencies;
      `--inexact` keeps them, and `uv run octacam` never prunes.

## Develop octacam

To hack on octacam, work from the clone with an editable environment instead of the
tool install:

```bash
uv sync                              # venv with runtime + dev dependencies
uv run octacam --help                # run from the checkout
uv run pytest                        # test suite
uv run --group docs mkdocs serve     # docs with live reload → http://127.0.0.1:8000
```

## Next steps

Head to the [Quickstart](quickstart.md) to record your first trial — no cameras
required.
