# Changelog

All notable changes to octacam are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and octacam adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).
Releases are tagged `vX.Y.Z`; install a specific one with
`git+https://github.com/NeLy-EPFL/octacam.git@vX.Y.Z`.

## [0.3.1] - 2026-07-11

### Added

- **Update notice** — `octacam doctor` and a dismissible GUI banner tell you when
  a newer release is available on PyPI and print the correct upgrade command for
  your install (pip / uv tool / pipx / conda). Read-only and fail-silent: octacam
  never updates itself, and the check is disabled by `OCTACAM_NO_UPDATE_CHECK` or
  `DO_NOT_TRACK`. Stays silent until octacam is published to PyPI.
- **MIT license** — octacam now ships a `LICENSE` file (MIT, matching SeptaCam)
  and declares it in the package metadata alongside trove classifiers and project
  URLs.

### Changed

- **Compacter `octacam doctor`** — the camera list now collapses same-model
  cameras onto one line (`acA1920-150um: 40018619, 40018631, …`) in both the
  per-backend list and the cascade selection, and every backend (not just Basler)
  labels its cameras with the model name, read without opening the device (from
  the transport-layer node map for flir/spinnaker, or the device descriptor for
  basler/pycameleon). Dropped two low-signal lines (the "GUI port is free" happy
  path and the NVENC `save_method` how-to hint). The `flir`-unavailable message
  now says plainly that only the PySpin wheel is missing (often pruned by
  `uv sync`) and that the ctypes `spinnaker` tier still serves FLIR cameras.

## [0.3.0] - 2026-07-11

The 0.3.0 line is a large step beyond 0.2: octacam gains a multi-vendor camera
backend cascade, trigger-hardware plugins, a frame-rate benchmark, GPU encoding,
one-command post-processing, and a much richer web GUI.

### Added

- **Backend cascade** — `backend = "auto"` resolves each camera to the highest
  available tier: `basler` (pypylon) → `flir` (Spinnaker/PySpin) → `spinnaker`
  (`libSpinnaker_C` via ctypes) → `pycameleon` (libusb floor, always present).
  Multiple backends run at once; sensor params persist as native GenApi TSV
  (`.txt`), so a rig can switch flir↔spinnaker on shared param files.
- **triggerbox plugin** — drives the EPFL common-trigger-circuit (Arduino Nano
  ESP32): self-describing wire protocol v2, three symmetric light channels, and
  server-side auto strobe duty; auto-recovers a wedged USB CDC link.
- **Arduino firmware auto-flash** — per-plugin source-hash fingerprinting and
  one-command flashing (`octacam flash`) via `arduino-cli`.
- **Benchmark / diagnostics** (`octacam benchmark` + a GUI tab) — measures grab,
  encode, and end-to-end ceilings and names the bottleneck.
- **Post-processing** (`octacam process`) — transcode, tile into grid videos,
  and mirror to storage, driven by a per-recording config snapshot.
- **CLI** — `doctor` (install/rig diagnosis; never opens a camera), `config`
  (interactive rig scaffold), Rich-styled help, and a rig instance-lock.
- **Web GUI** — adaptive per-client preview (adaptive resolution, sharp
  maximize, scroll-zoom, server-side crop of the zoomed region), a full
  Camera-tab GenApi node-map browser, plugin tabs in a responsive overflow menu,
  a configurable colour theme, and keyboard shortcuts with a `?` help overlay.
- **GPU encoding** — opt-in NVIDIA NVENC H.264, with the session cap
  auto-detected and overflow falling back to CPU encoding.
- **Recording** — per-frame timestamps in one `timestamps.npz`; a configurable
  writer queue depth (`record.writer_queue_size`, default 64); and a
  trigger-source model (software / managed / external) mirrored in the live
  preview.

### Changed

- `octacam.__version__` is single-sourced from the installed package metadata.
- FLIR `DeviceLinkThroughputLimit` is raised to max at open, and
  `TriggerOverlap=ReadOut` is set best-effort in every backend to lift the
  software-trigger frame rate.
- Every backend restores the config trigger source on save, so a preview's
  software trigger is never baked into a param file.
- Every grab loop is capped at `round(fps × duration)` frames so all cameras
  stop at the same count.

### Fixed

- Hardened teardown and error paths across the recording pipeline (dozens of
  verified robustness fixes); every camera now captures the same frame count.
- Stopped Spinnaker/GenTL teardown crashes — the `spinnaker` ctypes tier exists
  specifically to avoid the Spinnaker GenTL producer's GIL-holding `DevClose`.

### Removed

- The GenTL/`harvesters` backend and the mvIMPACT path — the always-present
  libusb `pycameleon` floor covers the general GenICam-USB3 camera without a
  vendor producer or EULA.

[0.3.1]: https://github.com/NeLy-EPFL/octacam/releases/tag/v0.3.1
[0.3.0]: https://github.com/NeLy-EPFL/octacam/releases/tag/v0.3.0
