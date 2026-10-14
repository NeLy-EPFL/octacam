# Changelog

All notable changes to octacam are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and octacam adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).
Releases are tagged `vX.Y.Z`; install a specific one with
`git+https://github.com/NeLy-EPFL/octacam.git@vX.Y.Z`.

## [Unreleased]

### Added

- **2-photon: stop when ThorImage stops (new default)** — the `twophoton`
  plugin no longer needs a duration: the Arduino keeps watching its ThorSync
  input after the start edge and ends the camera trigger once that input has
  been quiet for `stop_idle_ms` (default 2 s), and octacam then ends the
  recording. While the take will be armed, the Record tab shows a "Synced with
  ThorImage" checkbox (default from `[plugins.options] match_thorimage`,
  default `true`) in place of the Duration field, and the status line shows
  elapsed time; untick it to enter a manual duration. `max_duration_s` (default 3600) bounds the take if the
  stop never comes. Summaries of such takes carry the real length as
  `duration_s` plus `ended_by`/`duration_limit_s` and the board's end-of-run
  report. **Requires reflashing the 2-photon Arduino** (protocol v2; older
  boards are refused and offered a reflash). New plugin hook
  `recording_end_override` lets any plugin end a take itself.

- **Synced behavior + 2-photon video** — `octacam process` now renders
  `Renderings/synced_2p.mp4` for every take with a matched ThorImage +
  ThorSync folder: the behavior grid next to a green/red 2P merge, aligned
  frame-by-frame on ThorSync's DAQ clock. FastZ planes are averaged per
  timepoint (or max-projected). Configure with `[transfer.twophoton]`
  `render_synced_video`, `synced_green_channel`, `synced_red_channel`,
  `synced_fastz_projection`; backfill or re-render with the new
  `octacam render PATHS [--force]`.
- **Z-stack MIP images** — `octacam process` writes
  `zstack_mip_green.png` / `zstack_mip_red.png` / `zstack_mip_dual.png` into
  `Renderings/` for every anatomical Z-stack of the flies it processed
  (`[transfer.twophoton] render_zstack_mip`, default on); `octacam render`
  backfills any folder.
- **Detachable processing** — `octacam process --detach` runs the
  transcode → grid → transfer pipeline as a background job that survives an SSH
  disconnect (no `tmux` needed), printing a job id. Manage jobs with the new
  `octacam jobs` command: `list`, `attach` (a tmux-like live view of the log and
  progress — Ctrl-C detaches without cancelling, reattach anytime), `pause`,
  `resume`, and `cancel`. Job state lives under `~/.cache/octacam/jobs/`.
- **Shut down & process** — the GUI shut-down button now offers to start a
  detached processing job for the session's recordings on the way out; reattach
  from a terminal with `octacam jobs attach`.
- **Cache management** — a new `octacam cache` command inspects and clears
  octacam's on-disk cache under `~/.cache/octacam` (the recording list, detached-
  job logs, and activity markers): `cache info` shows the location, size, and a
  breakdown; `cache path` prints the directory; `cache clear [--all] [--yes]`
  removes the recording list and stale markers (add `--all` to also drop finished
  job logs). Clearing never touches a live capture, transcode, or job.

### Changed

- **Instant GUI startup** — `octacam gui` now binds the web server and serves the
  page *before* opening the cameras. The browser shows the full UI immediately
  (with a "connecting to cameras" placeholder in the preview area); the cameras
  and serial plugins are opened in parallel on a background thread and the grid
  fills in over the WebSocket once they are ready — so time-to-first-paint no
  longer waits on vendor-SDK camera enumeration or a trigger-board handshake. A
  camera-open failure is now surfaced in the GUI instead of aborting the process.
- **Processing auto-pauses during capture** — a running `octacam process`
  (detached or foreground) now pauses between files/folders while an
  `octacam gui`/`octacam record` on the same machine owns the cameras, and
  resumes automatically when they are free — so a background transcode no longer
  competes with a live recording for CPU/GPU/disk. The pause releases the
  encoder between units (it holds no NVENC session while paused), and is
  crash-safe (a crashed gui/record auto-clears the pause).

### Fixed

- **`RecordingN` renumbering no longer collides when a fly's sequence shifts
  up** (e.g. an earlier Z-stack arriving late): renames are two-phase, and a
  name held by a session that isn't moving is refused instead of merged into.

- **2P matching: FastZ captures now verify, and never cross experiments** —
  ThorSync's FrameOut counts every scanned frame (timepoints × (planes +
  flyback)), not timepoints, so FastZ captures never verified; and the
  verified ThorImage tier had no time limit, so another day's folder with the
  same frame count could be paired with a take. Both fixed.
- **Z-stack ThorSync folders are filed under their fly** — a Z-stack's
  SyncData (one capture segment per slice, frame-averaged) was never
  recognized as belonging to its Z-stack and landed in the generic
  `2p_only/` bucket.
- **2P sweep no longer skips folders whose name already exists elsewhere on
  the NAS** — ThorImage reuses names like `Fly1_Zstack` across experiments;
  dedup now compares the acquisition itself (Experiment.xml start time /
  ThorSync episode size), not the bare name.

- **`octacam` now starts on Python 3.10–3.13 again** — the CLI annotated two
  helpers with `JobReporter`, a name imported only under `if TYPE_CHECKING:`, and
  without quotes. Python 3.14 evaluates annotations lazily (PEP 649) so the dev
  machine never saw it, but every older supported interpreter evaluates them when
  the `def` runs: importing `octacam.cli` raised `NameError: name 'JobReporter' is
  not defined`, so **no** command worked (`requires-python` is `>= 3.10`). Both
  annotations are quoted now, matching how the rest of the module already refers
  to type-checking-only names, and a new AST test (`tests/test_typing_hygiene.py`)
  fails on any `TYPE_CHECKING`-only name used in an annotation Python evaluates —
  a check that works on every version, since an import test cannot catch this on
  3.14.
- **Auto strobe duty now shows up in the triggerbox timing plot** — with instant
  (serve-first) GUI startup the triggerbox tab read the camera exposures before
  the cameras were open, and the WebSocket push that fills the rest of the UI in
  never made it re-read them, so a light channel set to "Auto (cover exposure)"
  stayed drawn at its manual duty percent — with the note "no camera exposures
  yet" — for the rest of the session. Only the plot was wrong: an actual arm
  recomputes the on-time server-side, so the board still strobed over the real
  exposure. The tab now re-reads the exposures when that push arrives.
- **Switching rigs no longer fails to open the cameras** — a camera keeps its ROI
  until it is power-cycled, and a GenICam camera's `Width`/`Height` maximum is
  `sensor - offset`, so launching a rig whose config wants the full sensor right
  after one whose config cropped and offset the ROI made the parameter load write
  an out-of-range size (`Height = 2048 must be equal or smaller than Max = 1770`)
  and abort camera initialization for the whole rig. The applier now clears the
  ROI origin before programming the size, then restores the origin the config
  asks for. Affects the flir / spinnaker / pycameleon backends (Basler `.pfs`
  files are applied by pylon, which already ordered this correctly).
- **One unsettable camera parameter no longer takes down the rig** — the FLIR
  (PySpin) backend's typed setters let a raw `SpinnakerException` escape, which
  defeated the parameter applier's deliberate best-effort, skip-and-continue
  guard: any single value the device refused (out of range, bad increment) failed
  the entire startup instead of being logged and skipped.
- **`octacam doctor` no longer breaks the terminal** — the GPU/NVENC probes ran
  ffmpeg encodes with the controlling terminal on stdin, so ffmpeg switched the
  tty to no-echo mode (to watch for keypresses) and left it that way (the
  concurrent session-count probe reliably raced echo off; a timeout-killed probe
  never restored it), making typed input invisible after doctor exited. Every
  ffmpeg probe/transcode/remux launch now runs with `-nostdin` and a redirected
  stdin, so ffmpeg never touches the terminal. The same hardening covers a
  Ctrl-C'd `octacam process` transcode.

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
