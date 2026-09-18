# Changelog

All notable changes to octacam are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and octacam adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).
Releases are tagged `vX.Y.Z`; install a specific one with
`git+https://github.com/NeLy-EPFL/octacam.git@vX.Y.Z`.

## [Unreleased]

### Fixed

Fifteen defects found by a review of the 0.3.2 range, all of which shipped in
0.3.2. Four of them fail *silently* on the rig.

- **A rig no longer records with fewer cameras than its config asks for, silently**
  — a camera that failed to open was dropped with one log line and recording went
  ahead. Nothing compared the configured count against the opened one, so the GUI
  just drew a smaller grid and `octacam record` exited 0: a 7-of-8 session was
  indistinguishable from a healthy one until the data was analysed. octacam now
  tracks the shortfall (`CameraSystem.missing`), logs one `INCOMPLETE RIG` warning
  naming each missing camera and why, reports it in `/api/system`, and makes
  `octacam record` confirm before recording (`--force` to skip).
- **A camera's ROI is no longer silently left at the previous session's** — the
  stale-ROI fix only worked for parameter files that list `Width`/`Height` before
  `OffsetX`/`OffsetY`. Given the other order the zeroed origin was immediately
  re-applied and the following size was rejected against a clamped max, and the
  refusal was logged at *debug*, so the whole take was captured at the wrong
  geometry with nothing visible to the operator. Origins are now applied after
  every size whatever the file order, and a rejected geometry write is a warning.
- **A configured flywheel loop now runs on `octacam record`** — `options.command`
  seeded only the GUI tab (the plugin implemented no `default_start_params`), so
  a headless recording never armed the motor and never said so. Relaunching a
  recording from its own config snapshot therefore did not reproduce its motion.
- **The documented flywheel `options.command` example is valid TOML** — it
  extended an inline table with a dotted key, which is a parse error. Worse,
  `parse_config` swallowed the decode error and returned an all-defaults config,
  so copy-pasting the docs made octacam run with every detected camera, save dir
  `./`, no plugins and no destination. A config that does not parse now raises
  `ConfigError` naming the file and line instead of being silently ignored.
- **A detached job that cannot take its lock now fails instead of running
  invisibly** — it kept going "unmarked", but every liveness check keys off that
  flock: `jobs list` reported it failed while ffmpeg burned CPU, `pause`/`resume`/
  `cancel` all refused it, and `octacam cache clear --all` deleted the directory
  it was still writing into.
- **Two `[[visualization]]` grids no longer rebuild each other forever** — under
  `--no-transcode` the input set is every `*.mp4` in the folder, grids included,
  so building one made the other "older than the videos it composites". Both
  re-encoded on every run, on exactly the flag documented for regenerating grids.
- **The GUI no longer strands itself on the loading placeholder** — a browser
  connecting while the cameras were opening could have its ready descriptor
  overwritten by the handshake's own stale one (newest-only per message kind).
  Nothing re-sends `system`, so only a reload recovered.
- **Shutting down asks again** — the path that actually shuts the server down had
  no confirmation at all, behind a bare icon button next to save-config, and the
  "N other browsers will be disconnected" warning survived only on the path the
  server rejects with 409.
- **Keyboard shortcuts no longer fire behind the shutdown dialog** — the new
  modal was missing from the suppression guard, so bare keys acted on the app
  underneath and Ctrl+Enter started a recording, which then made the pending
  shutdown fail. The guard now finds every modal instead of a hard-coded list.
- **The Save-config button no longer looks live while doing nothing** — it is
  wired in the lazy camera build, so before the cameras arrived (and forever, if
  they failed) clicking it or pressing Ctrl+S silently did nothing.
- **The Connect/Reconnect button is reachable again** — it was hidden behind the
  Record tab's Advanced switch, which is disabled exactly when the socket is
  down, so the recovery control was unreachable by default. It is now always
  shown while disconnected.
- **A failed camera open no longer leaks the whole rig** — an error while loading
  a config left every opened camera claimed for the life of the process, then
  destroyed after `PylonTerminate()`.
- **One odd exception no longer aborts Basler enumeration** — only
  `genicam.GenericException` was caught, so a SWIG `RuntimeError` or an `OSError`
  from the XML download killed the whole rig's enumeration and orphaned every
  still-pending device handle.
- **Starting a recording no longer blocks the GUI's status and Stop** — the
  per-camera parameter export (a full node-map walk over USB, no timeout) ran
  under the controller lock that `/api/state`, the telemetry loop and Stop all
  take. It now runs off the lock, beside the NVENC warm-up.
- **`octacam process` can no longer be parked forever by an idle GUI** — the
  capture-active marker was published for the whole `octacam gui` lifetime, even
  before any camera opened and even when it opened none, and the pause has no
  timeout. The marker is now published only once the GUI holds the cameras, and
  `--ignore-capture` opts out. The launch warning claimed foreground runs do not
  auto-pause, which was the opposite of what the code did.
- Also: `python -m octacam`'s module no longer runs the CLI on plain import
  (missing `if __name__ == "__main__"` guard).


## [0.3.2] - 2026-09-18

### Added

- **Detachable processing** — `octacam process --detach` runs the
  transcode → grid → transfer pipeline as a background job that survives an SSH
  disconnect (no `tmux` needed), printing a job id. Manage jobs with the new
  `octacam jobs` command: `list`, `attach` (a tmux-like live view of the log and
  progress — Ctrl-C detaches without cancelling, reattach anytime), `pause`,
  `resume`, and `cancel`. Job state lives under `~/.cache/octacam/jobs/`.
- **Shut down & process** — the GUI shut-down button now offers to start a
  detached processing job for the session's recordings on the way out; reattach
  from a terminal with `octacam jobs attach`.
- **Recordings are relaunchable configs** — each recording folder's
  `octacam_config.toml` snapshot now holds the settings the recording actually
  ran with, not just the rig file's values. Settings changed live in the GUI are
  written in: the Record tab (fps, duration, trigger source, save method and
  encoder args, …) and each plugin's tab (the triggerbox camera lines and
  lights). Each camera's sensor parameters are read just before it starts
  recording and saved beside the snapshot as `<serial>.pfs`/`<serial>.txt`, so
  unsaved Camera-tab edits (exposure, gain, ROI) are kept too. The folder is
  then a complete config directory for rerunning the same setup; the path
  templates stay unexpanded, so a relaunch still records into a fresh dated
  folder. Previously 19 of 24 snapshots on the test rig disagreed with their own
  summary (e.g. `duration = 300` for a 3600 s take), and exposure/gain were
  recorded nowhere. A snapshot with no live changes is still an exact copy of
  the rig file. Plugins can contribute through a new `snapshot_options` hook.
- **A flywheel rig's loop program is configurable** — the turntable's motor
  pattern (`n_steps`, `step_interval_us`, `rest_duration_ms`, `n_repeats`,
  `init_wait_duration_s`) lived only in the GUI tab, so it could not be set per
  rig and a recording had no record of the motion it ran. `[[plugins]]` now takes
  an `options.command` table that seeds the tab, and whatever was armed is
  written into the recording's config snapshot.
- **The transfer step carries the config** — `octacam process` now copies the
  config snapshot and the camera parameter files to the destination with the
  videos, summary and timestamps, so the archived copy can relaunch the
  recording's setup. These small metadata files are compared by content when
  deciding whether a copy is already there: an edited config is often exactly
  the same size.
- **Cache management** — a new `octacam cache` command inspects and clears
  octacam's on-disk cache under `~/.cache/octacam` (the recording list, detached-
  job logs, and activity markers): `cache info` shows the location, size, and a
  breakdown; `cache path` prints the directory; `cache clear [--all] [--yes]`
  removes the recording list and stale markers (add `--all` to also drop finished
  job logs). Clearing never touches a live capture, transcode, or job.

### Changed

- **Composite grid videos are now opt-in** — `octacam process` used to build a
  `grid.mp4` for every recording, falling back to a layout derived from the rig's
  cameras (or a built-in 7-camera one) when the config named none. It now
  composites only what the config asks for: a rig with no `[[visualization]]`
  entry gets no grid, and pays none of the extra ffmpeg time per folder. Add an
  entry to keep the old behavior — `octacam config` offers to write a near-square
  one for your cameras — and `--no-grid` still skips the configured grids.
- **Preview encodes the cameras in parallel** — one tick used to encode every
  camera's JPEG in sequence on a single worker, so preview cost grew linearly
  with the rig. `cv2.imencode` releases the GIL, so each camera now encodes on
  its own executor task and a tick costs the slowest single camera instead of the
  sum: measured on the test box, eight 2048² cameras on a focused (1:1) tile drop
  from **85 ms to 14 ms**, and a maximized tile while recording from 26 ms to
  4.4 ms — the difference between overrunning the 33 ms refresh interval and
  fitting inside it. Per-camera frame counters are now stamped on the event loop
  rather than in the worker, so nothing shared is touched from the encode threads.

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

- **A leftover video from an earlier take is no longer transferred as this
  recording's** — recording into a folder again (confirming the overwrite)
  replaces only the files the new take writes, so the previous take's
  `*.mp4`/`grid.mp4` stayed behind. They looked like finished outputs, so
  `octacam process` skipped transcoding, left them in place and copied them to
  the destination, where they sat next to the new take's summary describing a
  different recording (a real case on the test rig: 1000-frame mp4s beside a
  100-frame take). An output older than the file it was made from is now redone,
  with a warning naming it; the grid is rebuilt when any video it composites is
  newer, and `--dry-run` lists both as work to do.

- **A config rewrite no longer drops a bare-name plugin list** — the loader
  accepts `plugins = ["flywheel"]`, but writing the config back (a GUI layout
  save, or a patched recording snapshot) emitted only `[[plugins]]` tables and
  silently lost the list. Bare names are now written as tables.

- **`octacam process --dry-run` no longer transcodes** — the flag only simulated
  the grid and transfer steps. The transcode step still ran ffmpeg on every
  pending file, so previewing `--all` could spend hours encoding, and a dry run
  started while the GUI owned the cameras paused until the capture ended. A dry
  run now does no work at all: it lists each file it would transcode (and, with
  `-d`, each source it would delete), each grid it would build, and each file it
  would transfer, and only counts what is already done. That makes
  `octacam process --all --dry-run` the way to see what is left to process. The
  grid and transfer plans include the outputs the transcode step would write,
  and a dry run never waits on a live capture.

- **Two `octacam process` runs over one folder no longer destroy each other's
  work** — an in-progress transcode wrote to a temp whose name was derived only
  from the output (`.<stem>.octacam-part<ext>`), and `_atomic_output` deleted
  that name on entry to clear orphans. So a second run (trivially: `--last` in
  two terminals) unlinked the first run's live temp and then renamed a file it
  had not written onto the output; the loser failed with a bare
  `FileNotFoundError` from `os.replace`. Temps are now unique per process and
  call (pid + uuid), matching what `octacam.transfer` already did for copies.
  Orphan reclamation is unchanged in practice and more precise: `_atomic_output`
  holds an advisory `flock` on its temp, so a re-run after a hard kill still
  frees the previous attempt's (possibly multi-GB) disk immediately, while a
  concurrent run's live temp is never touched — no timing heuristic. Temps left
  by an older octacam are still recognised and reclaimed, and the temp keeps the
  output's real extension last so ffmpeg still infers the muxer.

- **A missing `ffprobe` no longer aborts `octacam process`** — the grid
  compositor probed each cell with a bare `ffprobe` off `$PATH`, but `ffprobe` is
  a *separate* binary from `ffmpeg` and imageio-ffmpeg bundles ffmpeg only. On a
  host with no system ffmpeg the probe raised `FileNotFoundError`, which is not
  one of the errors the per-cell guard catches, so it escaped
  `build_grid_video` and killed the whole run — *after* the transcodes had
  finished but *before* the transfer, leaving the recordings un-mirrored. octacam
  now resolves ffprobe next to the ffmpeg it actually uses (so a rig pinning
  `OCTACAM_FFMPEG` probes with that same build rather than an unrelated older
  ffprobe first on `$PATH`; `OCTACAM_FFPROBE` overrides), reports a missing one
  as a skipped grid with the real reason, and treats a per-file probe error as a
  black cell as documented. The probe also now runs with `stdin=DEVNULL` — the
  rule every other ffmpeg-family launch here already followed, so a kill
  mid-probe cannot leave the terminal in no-echo mode — and is bounded by a
  30 s timeout instead of hanging the run on a corrupt or network-backed file.

- **One sick camera can no longer stall startup for minutes** — a USB3 camera
  whose link trains at full SuperSpeed but whose control transfers time out
  enumerates normally, and pylon then retries its first register read for as long
  as it likes: on the test rig a single such camera held `octacam gui` for
  **271 s** inside one `CreateDevice` call (a healthy camera returns in ~0.16 s),
  with no output at all while it did. Enumeration now runs those calls
  concurrently under a shared deadline (15 s by default, override with
  `OCTACAM_BASLER_CREATE_TIMEOUT`); a camera that misses it is reported through
  the existing "present but unusable" path — the rest of the rig comes up
  normally — and its handle is released if pylon eventually hands one over, so it
  is not left for the garbage collector to destroy after `PylonTerminate()`. The
  wait is also no longer silent: octacam names the cameras it is still waiting on,
  and the skip message explains that the link trained but the camera is not
  answering (check `dmesg` for a matching `can't set config` line, then reseat the
  cable), instead of passing the raw SDK text through.
- **A rig no longer pays to enumerate cameras it never asked for** — the auto
  cascade offered every tier the whole USB bus rather than the rig's configured
  serials, so a 2-camera FLIR rig still ran `CreateDevice` on every attached
  Basler (and inherited the stall above when one of them was sick), then dropped
  the surplus handles without destroying them. Each tier is now given the
  requested serial list, so those devices are never touched and the leak is gone.
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

[0.3.2]: https://github.com/NeLy-EPFL/octacam/releases/tag/v0.3.2
[0.3.1]: https://github.com/NeLy-EPFL/octacam/releases/tag/v0.3.1
[0.3.0]: https://github.com/NeLy-EPFL/octacam/releases/tag/v0.3.0
