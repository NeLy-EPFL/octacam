# CLAUDE.md

Development notes and architecture for **octacam** — for contributors and coding
agents. This file captures the durable, hard-won knowledge that isn't obvious
from the code alone. Keep it current when you change the things it describes.

octacam previews, records, and archives **synchronized video from many
scientific cameras** (Basler / FLIR / any GenICam USB3-Vision) through a live
FastAPI web GUI and a headless CLI, plus Arduino-driven trigger/strobe hardware
and one-command post-processing. It targets neuroscience rigs and is the
successor to SeptaCam.

> Active development is on the `develop` branch (git-flow: `main` tracks the
> latest stable release, `develop` is the next). The camera layer is a multi-tier
> GenICam cascade (basler / flir / spinnaker / pycameleon; see below) running on
> Python 3.10–3.14; `requires-python >= 3.10`.

## Dev workflow

The project uses **uv** (no pip in the venv).

```bash
uv sync                                   # install (dev + core deps)
uv run pytest -q -o addopts=""            # ~726 tests (the -o skips slow coverage)
uv run ruff check src/                    # lint (see the known baseline below)
uv run pyright src/                       # types (documented baseline; net-new must be 0)
uv run --group docs mkdocs build --strict # docs build + link/nav validation
uv run --group frontend pytest tests/test_frontend.py  # browser-driven GUI tests

PYLON_CAMEMU=8 octacam gui configs/emulate_basler   # run with 8 fake cameras, no hardware
```

- **`fake` backend** is the CI vehicle — a rich SFNC-keyed synthetic camera that
  exercises every node/widget kind without hardware. Prefer adding fake-backed
  regression tests over hardware-only assertions.
- **Lint/type baselines** (pre-existing, not introduced by new work): ruff has a
  couple of intentional `E501`/`F401`-style items already in `[tool.ruff]`
  ignore or documented; pyright reports ~45 errors, mostly `self.raw`/`self._cam`
  Optional-access in the vendor backends and cli. **New work must add zero.**
- **Frontend has a browser test harness** (`tests/test_frontend.py`): it loads
  the real `web/static/js/*.js` ES modules in a headless Chromium against the
  real `index.html` and asserts GUI wiring (fields exist, enable/disable,
  applySettings round-trips). Opt-in via the `frontend` dependency group
  (`playwright`), so the default suite skips it (importorskip) and it self-skips
  if no Chromium is installed. This is the automated form of the manual
  headless-render recipe below — add a case here whenever a backend setting gains
  a GUI control, so "shipped a knob with no widget" gets caught.
- **Full-suite runtime is ~8 min.** Run the relevant `tests/test_*.py` file(s)
  during development; run the whole suite before committing.
- **Don't run the frontend group inside a full-suite run.** playwright's *sync*
  API leaves a running event loop in the process, so any later test calling
  `asyncio.run` dies (`tests/test_web.py` sender cases, `tests/test_pycameleon_backend.py`
  retrieve cases — one browser test anywhere earlier is enough). That is what the
  opt-in group buys: keep them two commands, as above. If a venv has playwright
  installed, `uv run pytest` collects them and shows those failures.
- **The dev rig runs Python 3.14, but `requires-python` is `>= 3.10`.** 3.14
  evaluates annotations lazily (PEP 649), so a bug that a 3.10–3.13 user hits at
  *import* is invisible here. Concretely: a name imported only under
  `if TYPE_CHECKING:` must be **quoted** where it appears in an annotation Python
  evaluates (function signatures; module/class-level variable annotations —
  function-*local* annotations are never evaluated, so `self._task: TaskID | None`
  inside a method is fine). An unquoted one took out the whole CLI on every
  supported interpreter below 3.14. `tests/test_typing_hygiene.py` walks the AST
  for this and so catches it on any version; an import test cannot.
- **Root-caused (was "known crash, don't chase it"):** pypylon bundles its own
  GenICam/GenApi native libraries; if numpy (or anything pulling it in, e.g.
  `octacam.web.app`) loads first in the process, a later pypylon call made from
  a worker thread segfaults *when that thread is torn down* — a glibc
  static-TLS-exhaustion interaction between the two libraries' native `.so`s,
  not an octacam bug, but a real crash (not limited to pytest teardown — it hit
  production `octacam gui`/`octacam doctor` on real Basler hardware once
  camera-open moved onto a worker thread in 900e074). Fixed by importing
  pypylon first: `cli.py` does a best-effort `import pypylon.pylon` as its
  first statement (before anything else gets a chance to import numpy), since
  it's the first octacam module loaded for every subcommand.

## Versioning & releases

octacam follows **SemVer** and is released **from git tags** — there is no PyPI
package; every install pulls `git+https://github.com/NeLy-EPFL/octacam.git`. The
version has **one source of truth**, `pyproject.toml [project].version`;
`octacam.__version__` reads it back from the installed metadata
(`importlib.metadata.version`), so never hard-code a second copy. Between
releases the dev branch carries a `.devN` suffix (e.g. `0.3.1.dev0`).

To cut release `X.Y.Z`:

1. Roll `CHANGELOG.md`'s `[Unreleased]` items under a new `## [X.Y.Z] - <date>` heading.
2. Set `pyproject.toml` version to `X.Y.Z` (drop `.devN`); commit `release: vX.Y.Z`.
3. Tag it: `git tag -a vX.Y.Z -m "octacam X.Y.Z"`.
4. Land it on `main`: merge (or fast-forward) `main` up to the tagged commit and push, so **`main` is always the latest stable release**; publish the tag too (`git push origin vX.Y.Z`). A fresh `git clone` checks out `main`, so this is what makes cloning install stable octacam — the install docs rely on it. Pushing the tag also deploys the numbered doc version `X.Y.Z` and repoints the `stable` docs alias at it (`.github/workflows/docs.yml`).
5. Back on the `develop` branch, bump to `X.Y.(Z+1).dev0`; commit `chore: open X.Y.(Z+1) development`; add a fresh `[Unreleased]` block.

An **update notice** (`updates.py`, surfaced in `octacam doctor` and a dismissible
GUI banner via `/api/system`'s `update` field) compares the installed version
against the latest stable on **PyPI** and advises the correct per-install upgrade
command (pip / uv tool / pipx / conda). It is read-only and fail-silent —
**octacam never updates itself** (a library must not mutate its own install, and
octacam is also sometimes a `uv add` dependency or lives in a conda env) — honors
`OCTACAM_NO_UPDATE_CHECK` / `DO_NOT_TRACK`, and stays dormant until octacam is on
PyPI (the Simple API 404s → no signal). Branch model (git-flow): `main` is stable;
`develop` is the single permanent development line (feature work branches off it;
a maintenance line for an older release, if ever needed, is named by series like
`0.3.x` — never a versioned `dev-*`). mike publishes versioned docs to `gh-pages`:
each release tag becomes a numbered version, the `stable` alias (and the site root)
follows the newest release, and `develop` pushes refresh the rolling `dev` version.

## Repo layout

```
src/octacam/
  cli.py            typer CLI: gui, doctor, config, record, flash, benchmark, process
  controller.py     RecordingController — the framework-free record state machine
  config.py         octacam_config.toml parsing (pydantic, tolerant/warn-and-default)
  config_writer.py  writes config snapshots (inline-table TOML for triggerbox)
  writer.py         AsyncFrameWriter → ffmpeg subprocess (H.264) or raw byte dump
  transform.py      DisplayTransform (rotate/flip) + recording-summary constants
  diagnostics.py    the frame-rate benchmark engine
  firmware.py       Arduino sketch fingerprinting + arduino-cli flashing
  serial_ports.py   serial-port detection, USB bus-reset recovery
  transfer.py       octacam process → mirror recordings to storage
  twophoton_transfer.py  discover/settle/match ThorSync/ThorImage folders (see below)
  grid.py           octacam process → composite grid videos (ffmpeg xstack)
  session_cache.py  remembers recording folders for `process --last/--all`
  camera.py         re-exports CameraSystem/Camera/PARAM_NODES
  cameras/          the backend layer (see below)
  plugins/          serial-hardware plugins (triggerbox, twophoton, flywheel)
  web/              FastAPI app + vanilla-JS static frontend
arduino/            triggerbox, 2photon_trigger, stepper_motor sketches
configs/<rig>/      octacam_config.toml + per-camera sensor files (.pfs / .txt)
tests/              pytest suite (fake backend + faked SDK facades)
docs/               Material for MkDocs site (published to GitHub Pages)
```

## The recording pipeline

`CameraSystem` (a set of `Camera` objects, each wrapping a per-camera
`CameraBackend`) is driven by `RecordingController`, which both the web app and
the headless `octacam record` share. The controller is a framework-free state
machine:

```
preview/idle → waiting → recording → finishing → preview/idle
```

A **monitor thread** replaces the old Qt timers: it polls for the first frame on
every camera (firing plugin `on_first_frame` hooks at that t0), enforces the
recording deadline, then runs teardown in a fixed order (stop trigger → grab
loops exit → writers drain → `recording_summary.json` + `timestamps.npz` +
session-cache note). Two subtleties that are easy to break:

- **Plugin hooks dispatch OFF the controller lock** (a plugin's serial write can
  block on an ack), guarded by a `_start_hooks_done` event so a stop/abort can
  never overtake a not-yet-sent hardware arm.
- **Camera control is locked** while `recording_active` *or* `diagnosing` — any
  device-touching op (start, param write, node-map snapshot) is refused.

Each `Camera` runs its own grab thread; `retrieve()` must **never raise** (it
returns `None` on a device/stop-race error) or a dead grab thread would orphan
its ffmpeg writer.

Each grab thread is **capped at `round(fps × duration)` frames**
(`controller.capture_frame_count` → `Camera.start_record(max_frames=…)`) for the
`software`/`managed` trigger sources octacam clocks, so the independent grab
loops all stop at the same count instead of ending a frame apart when the
teardown race lets one retrieve a trailing pulse (e.g. 801 vs 800). `external`
(a source octacam doesn't drive) has an unknown pulse count and stays uncapped —
bounded only by the deadline. A camera that can't keep up never reaches the cap
and is bounded by the deadline too (the existing short-capture path). The writer
queue depth is `record.writer_queue_size` (default 64), tunable per rig to absorb
transient encoder stalls (a full queue is the only source of a "dropped" frame).

## Camera backends & the auto cascade

`cameras/registry.py` is the seam. `backend = "auto"` (the default) resolves to a
per-camera **cascade**: each camera is claimed by the highest tier that
enumerates its serial (deduped by serial in `CameraSystem._enumerate`).

```
CASCADE = ("basler", "flir", "spinnaker", "pycameleon")
```

| Tier | Backend | Driver | Notes |
| --- | --- | --- | --- |
| 1 | `basler` | pypylon | Basler vendor SDK; core dep |
| 1 | `flir` | Spinnaker SDK + **PySpin** | FLIR vendor SDK; the default FLIR path. PySpin ships wheels for cp310–cp314 (Spinnaker 4.4), so this tier is available whenever `import PySpin` succeeds |
| 2 | `spinnaker` | `libSpinnaker_C.so` via **ctypes** | Same FLIR cameras as `flir`; the FLIR path when PySpin is *not* installed (needs only the SDK's `libSpinnaker_C.so`, no PySpin wheel); ctypes releases the GIL on the blocking grab |
| 3 | `pycameleon` | libusb (Rust `cameleon`) | Always-present floor; general GenICam-USB3 path; no vendor SDK/producer/EULA |
| — | `fake` | synthetic | CI vehicle; only used when named |

**No GenTL tier (the removed `harvesters` backend).** octacam once carried an
opt-in `harvesters` GenTL-consumer backend; it was **removed entirely** (module,
the `harvesters`/`genicam` deps, and docs). Every GenTL producer is a
user-installed, vendor-EULA'd `.cti` with its own quirks (watermarks, close
deadlocks, vendor-only enumeration), and the always-present `pycameleon` floor —
libusb-only, no producer needed — covers the general GenICam-USB3 camera better.
Do **not** reintroduce a GenTL/`.cti` path. One producer fact still matters for a
live backend: Teledyne's **Spinnaker GenTL producer** (`Spinnaker_GenTL.cti`) has
a `DevClose` that **deadlocks while holding the GIL** (wedges the whole process) —
which is exactly why the `spinnaker` tier drives the Spinnaker **SDK C API** over
`ctypes` instead of that producer.

### Backend contract (`cameras/base.py :: CameraBackend`)
All backends implement: enumerate/open/close; `load_params`/`save_params`;
frame-trigger setup; a **software-trigger hand-off** (below); `begin_freerun` /
`retrieve_freerun` (used by the benchmark and free-run preview); and a full
GenApi node-map walk (`list_features`/`read_feature`/`write_feature`/
`execute_command`) for the Camera-tab node browser. `basler` walks via genicam;
`flir`/`spinnaker` walk the C/PySpin node map; `pycameleon` (no introspection)
and the base fallback use a curated node set.

## Trigger model

Recording `trigger_source` (config `[record]`):
- **`software`** — octacam paces a software trigger at `fps`.
- **`managed`** — octacam drives a trigger-*generating* plugin (triggerbox): the
  Arduino emits the pulses + strobes. (A legacy `external` config with a driving
  plugin auto-promotes to `managed`.)
- **`external`** — a truly external master octacam does not drive (e.g. a
  2-photon rig slaved to ThorSync).

`preview_trigger_source` (`auto`|`software`|`free_running`) makes live preview
approximate the recording — `auto` mirrors `trigger_source`.

**Software-trigger hand-off** (`cameras/_trigger_handoff.py :: SoftwareTriggerHandoff`):
a shared trigger-timer thread calling each backend's device trigger serially let
a slow FLIR starve a fast Basler. So `trigger_once()` only bumps a `_pending`
counter; each camera's own `retrieve()` fires the device trigger + fetches one
frame. This is why `trigger_once` is *not* a device call.

## Frame-rate ceilings (test-rig hardware)

FLIR **GS3-U3-41C6NIR** (CMV4000 CMOS, 2048², Mono8):
- Software-triggered frame time = USB transfer (~11 ms) + exposure, **serial** —
  this camera does not overlap them for software FrameStart triggers. So delivered
  fps ≈ 1/(11 ms + exposure): 4 ms→~65, 1 ms→~81, 100 µs→~90. **This is a hard
  camera limit, not a bug.**
- **`TriggerOverlap=ReadOut` is mandatory** — with the default `Off`, a trigger
  fired during the previous frame's readout is silently ignored (~half rate + a
  200 ms stall). Set best-effort in every backend's frame-trigger enable.
- To exceed 80 fps: external/hardware trigger (overlaps exposure+transfer → ~90),
  or shorten exposure ≲1 ms. `DeviceLinkThroughputLimit` is maxed at open.
- **One USB3 bus is ~384 MB/s shared** → at 2048² Mono8 (4.19 MB/frame) ~90
  frames/s total per bus. Distribute cameras across host controllers; the
  benchmark flags this as the TRANSFER/HOST bottleneck.

## Config system

Per-rig **`octacam_config.toml`** (parsed tolerantly in `config.py` —
warn-and-default, never raise) plus **one per-camera sensor file**:
- **Basler** → native `.pfs`.
- **Every other GenICam backend** (flir, spinnaker, pycameleon, fake) → the
  native **GenApi persistence TSV** (`.txt`) via
  `cameras/_genicam_config.py` (`apply_config`/`dump_config`/`parse_config`). The
  unified `.txt` format lets a rig switch flir↔spinnaker (PySpin ↔ ctypes)
  sharing the same param files.

**Trigger normalization on save:** a GUI "Save" taken while previewing with a
software trigger must not bake `TriggerSource=Software` into the file (it would
make a later external-trigger recording silently never start). Every backend's
`save_params`/`dump_config` restores the camera's original (config) trigger
source via `normalize_trigger_source` — keep this parity when adding a backend.

**ROI apply order:** a camera **keeps its ROI until power-cycled**, and SFNC makes
a size node's max `sensor - origin`, so the *previous* session's cropped/offset
ROI clamps the *next* config's `Width`/`Height` (hexaview's `OffsetY=278` made
triggerbox's `Height=2048` out of range at max 1770 — it failed the whole rig
init). `apply_config` therefore zeroes the origin whose size node the file sets
(`_clear_roi_offsets`) **before** the file-order loop; the file's own
`OffsetX`/`OffsetY` lines follow and restore it. Two invariants this rests on:
every backend's typed setters (`_set_enum`/`_set_bool`/`_set_number`) must raise
`BackendError` and **never leak a raw SDK exception** — that is what makes
`apply_config`'s skip-and-continue best-effort real, and a leak turns one refused
value into a dead rig — and the `fake` backend models the ROI coupling in both
directions so wrong-order programming is caught without hardware.

`config_writer._toml_value` must serialize every scalar the loader can produce —
including inline tables (triggerbox's nested `cameras`/`lights` arrays) and
date/datetime scalars (a bare serial number or date-like save dir parses as one).
A `None`-valued field is **omitted** (TOML has no null); the loader restores its
default on read, so an "auto" field like `record.max_nvenc_sessions = None` must
never be written as a literal — the `octacam config` scaffold `model_dump()`s the
whole `RecordConfig`, so any None-defaulting field would otherwise crash the dump.

### Per-user transfer profiles (`[transfer.users.<initials>]`)

A NAS shared by a whole lab (organized by member initials as top-level
folders, e.g. `MD`, `MA`) means several people can share one rig's hardware
config while each wanting their own save destination — there was previously
**zero** per-user concept anywhere in this codebase. Deliberately narrow
scope: only the save-destination fields are overridable (`directory`, and
the 2P `source` path) — everything else in `[transfer]`/`[transfer.twophoton]`
(`checksum`, `delete_after_transfer`, `match_window_s`, `settle_s`, ...) stays
shared, rig-wide policy, since those are hardware/timing-tuned and have no
reason to differ per person. Nobody, including a rig's usual owner, is an
implicit default — every person is a symmetric named entry:

```toml
[transfer]
directory = "/mnt/store/default"
[transfer.users.MD]
directory = "/mnt/store/MD/BallPushing_Imaging"
[transfer.users.MD.twophoton]
source = "/mnt/windows_share/MD"
```

`resolve_transfer_for_user` (config.py) applies the override — deliberately
**raises** `ValueError` on an unknown user (unlike the rest of config.py's
tolerant warn-and-default parsing) because a `--user` typo is a CLI
input-validation error, not a malformed config value, and must never
silently transfer to the wrong destination; `cli.py`'s `_load_config_for_user`
turns that into a clear `sys.exit`. Overriding just `twophoton.source` is a
field-level overlay, not a full re-specification — it doesn't reset the
rig's own tuned `match_window_s`/`settle_s` back to class defaults.

`--user`/`-u` is threaded through every command that touches `[transfer]`:
`gui`/`record` (a whole-session/one-time resolution — `record` bakes the
resolved directory into that recording's own config snapshot at record
time, so `process` needs no `--user` for it later), and `process`'s
fallback-to-`--config` path plus its three standalone modes
(`--twophoton-sweep`/`--migrate-layout`/`--reassemble-tiffs`, which resolve
`[transfer].directory` fresh from `--config` on every run). `doctor --user`
reports a bad `--user` as a report line (never `sys.exit`s) — unlike every
other command, doctor's whole point is to keep running every other check
and summarize, not abort on the first problem.

**A real bug found while building the GUI dropdown (below)**: `record
--user`'s resolved `twophoton.source` override never actually reached the
recording's config snapshot — `RecordingSettings` had no twophoton field at
all, and `_snapshot_config`'s `with_process_params` call only ever patched
`transcode_ffmpeg_params`/`transfer_directory`/`transfer_checksum`. Only the
directory override worked end-to-end; the 2P source silently fell back to
the rig's shared default. Fixed by adding
`RecordingSettings.transfer_twophoton_source`, populating it in
`_settings_from_record`, and giving `with_process_params` a matching
diff-based patch for `[transfer.twophoton].source` (confirmed via a real
`octacam record --user <profile-with-a-2P-override>` run: the resulting
snapshot has the profile's `source`, with `match_window_s`/`settle_s`
untouched).

**Bootstrapping from the NAS** (`octacam config CONFIG_DIR --bootstrap-users
<nas-root>`): scans `<nas-root>`'s immediate subdirectories for
initials-shaped folders (`^[A-Z]{2,4}$`) and adds a
`[transfer.users.<initials>]` entry (default `<nas-root>/<initials>/octacam_2P`)
for each one not already present — so a new lab member can start recording
without configuring anything, unless they want something other than the
generic default (like Matthias's own project-specific `BallPushing_Imaging`).
This is the **one config write in this codebase that edits an existing,
possibly hand-authored file in place** rather than creating a brand-new one
— every other write here (`config_writer.py`'s `_dumps`, the record-time
snapshot embed) is a from-scratch re-serialize that would silently drop an
existing file's comments/formatting. That's specifically why this needed a
new dependency, **`tomlkit`** (round-trip-safe parse/dump), rather than
reusing `config_writer.py` or stdlib `tomllib`. Never touches an
already-present initials key (so a person's already-customized entry is
never clobbered back to the generic default), and is safe to re-run.

**Phase 2 — the GUI dropdown**: `/api/system` gains `transfer_users` (the
same `{initials: {directory, twophoton_source}}` shape, `{}` when nothing's
configured) and `active_user` (the `--user` the GUI was launched with, if
any); `create_app`/`_AppState` now take `user` to report it.
`web/static/js/record.js` renders a `<select>` right above the existing
"Transfer directory" field from `transfer_users` (the whole row stays
`hidden` when it's empty — zero UI change for a single-user rig),
pre-selected to `active_user`. Selecting a profile is just one `PUT
/api/settings` with that profile's `transfer_directory`/
`transfer_twophoton_source` — **no new settings-patch endpoint**, it reuses
the existing one (`update_settings` validates generically against
`dataclasses.fields(RecordingSettings)`, so a new field there is
automatically a legal patch key). Self-service creation ("+ Add
yourself…", the dropdown's last option) prompts for initials, `POST
/api/transfer/users`; if the rig has `[transfer].users_root` configured
(set once, automatically, the first time `--bootstrap-users` runs — never
overwritten after) the destination auto-suggests
`<users_root>/<initials>/octacam_2P`, otherwise the endpoint 422s and the
frontend prompts for a directory too. The single-entry tomlkit write
(`config_writer.add_transfer_user`) shares its core mutation
(`add_transfer_user_entry`) with the CLI's many-at-once
`--bootstrap-users` loop, so both stay byte-for-byte the same
comment-preserving in-place edit.

## Plugin system

Serial-hardware plugins under `plugins/<name>/`, registered in
`_BUILTINS = ("flywheel", "twophoton", "triggerbox")` with legacy
`_ALIASES = {"arduino": "flywheel"}` (old configs keep working). The default
launch loads none; enable via `[[plugins]]` or `--plugin`.

Lifecycle hooks (`plugins/base.py :: Plugin`): `on_recording_start/stop`,
`on_first_frame`, `default_start_params` (so **headless `octacam record` arms the
board** — a plugin that omits this never arms on the CLI), `drives_preview_trigger`
/`on_preview_start/stop`; `set_controller`/`set_broadcast` are duck-typed
injections. Each plugin adds a WS topic + `/api/<name>/*` REST + a GUI tab.
`recording_metadata()` (schema_version 4+) is queried synchronously right
*before* `recording_summary.json` is written — earlier than `on_recording_stop`,
which fires after the summary is already on disk — so it must read back state
the plugin already captured (e.g. at `on_recording_start`), not compute
anything fresh; results merge into the summary's `"plugins"` key
(`PluginManager.collect_recording_metadata`, mirroring `default_start_params`).
`twophoton` uses it to report `{"armed": bool}` for the twophoton-transfer
matcher (see the 2P transfer section below).

**triggerbox** generalizes the EPFL `common-trigger-circuit` (Arduino Nano
ESP32). Self-describing wire protocol v2: `0xA5 | ver=2 | len u16 | payload |
xor`; payload = fps, duration_ms, N camera lines `{pin, pulse_us, delay_us}`, and
3 symmetric light channels (`off`/`strobe`/`continuous`/`pulse_train`).
`PIN_LABELS` is the single source of truth, mirrored in `triggerbox.ino` and
asserted equal by a unit test. Server-side **auto strobe duty** sizes the LED
on-time to the longest live camera exposure. On a wedged USB CDC link the plugin
auto-recovers via a host `USBDEVFS_RESET` bus reset and surfaces the error loudly.

## 2-photon transfer (`twophoton_transfer.py`)

Pairs a behavior take with its ThorSync/ThorImage folder(s) on `octacam
process`, replacing a legacy shell script (`move_files.sh`). Investigated
against a real 2P rig; the durable findings:

- **No shared relative path.** The legacy script assumed a sibling
  `2p`/`behData` folder pair at an identical relative path — real ThorSync/
  ThorImage write flat, auto-incrementing folders (`SyncData102`, `Fly1_004`,
  …) directly under an experiment folder, completely decoupled from octacam's
  own `<date>_/<Fly>/<take>` naming. Wall-clock time (`match_take_to_twophoton`)
  is the *fallback* pairing signal — but real testing found a plain overlap
  check alone isn't reliable enough to trust: 13 of 15 real matches came back
  `ambiguous` (more than one plausible candidate), and ThorImage's own folder
  naming doesn't even track creation order (a folder named `Fly1`, normally
  the *first* acquisition, was created *after* `Fly1_004`–`Fly1_007` in one
  real session). See **Verified matching** below for what actually fixed
  ambiguity when a `SyncData` folder exists.
- **Timestamp-only matching requires matching start, end, AND duration —
  not just overlap.** A proper behavior/2P pair runs for essentially the
  same length of time; a short, unrelated ThorImage snapshot (a focus check,
  an ROI tune) nested entirely inside a much longer take satisfies a plain
  "windows overlap" check, and can even satisfy "both endpoints individually
  close" (a much *longer* candidate can loosely straddle a short take with
  both ends "close enough" while its own duration is nothing alike) while
  being a spurious match. `_gap_seconds` is the *worst* of three deviations —
  `|candidate.start - take_start|`, `|candidate.last_mtime - take_end|`, and
  `|candidate_duration - take_duration|` (0 only for a genuinely matched
  pair); `match_take_to_twophoton` requires it ≤ `match_window_s`, not the
  old "any overlap, however loose" check. Confirmed on a real rig with no
  `SyncData` folders at all (so nothing to verify against): its ThorImage
  folders split cleanly into two populations — several 20–33s snapshots
  (correctly rejected) and several 147–159s sessions (correctly matched,
  each within the expected margin of its take's 129s) — not a fluke, a real
  structural difference the duration check picks out. `match_window_s`'s
  default was tightened from 120s to **60s** after finding the real spurious
  matches deviated 90s+ while genuine ones clustered at 20–33s (documented
  ~20–40s ThorSync startup lag + a newly confirmed ~25–30s ThorImage disk
  write-out lag — see next bullet). This check only governs the *timestamp*
  tier — the verified tier's confidence never depends on timing shape at all.
- **ThorImage writes files in a burst after acquisition, not in real time.**
  Confirmed by comparing a verified pairing's actual `FrameOut` edge timing
  (from `Episode001.h5`, i.e. ground truth) against its own files' raw
  mtimes: the real acquisition had already ended (per the DAQ) before the
  first `.tif` appeared on disk, with the full write-out taking a further
  ~25–30s. Irrelevant to a `SyncData`-verified match (edge count doesn't care
  about file timestamps) but means a purely timestamp-only `image`-kind match
  (no `SyncData` to verify against at all) is the least certain path in this
  feature — there's no independent signal to check it against, only a
  generous-enough `match_window_s` as mitigation, never a guarantee.
- **Verified matching (`twophoton_signals.py`)**: whenever a `SyncData*`
  folder exists, its `Episode001.h5` records the Arduino's camera-trigger
  pulse on ThorSync's own DAQ clock (`DI/Cameras`) — confirmed on real data
  with an **exact** edge-count match to the paired take's own recorded frame
  count (19399 == 19399), and `DI/FrameOut`'s edge count exactly matching the
  paired ThorImage folder's own `Experiment.xml <Timelapse
  timepoints="...">` (1500 == 1500). `DI/CaptureOn` gates each take's window
  as N ≥ 0 rising→falling segments (a `SyncData` folder can span more than
  one take). `match_takes_to_twophoton_batch` (the entry point `cli.py`'s
  Phase 3 actually drives, superseding a bare `match_take_to_twophoton` call)
  runs the whole batch of takes sharing one `[transfer.twophoton].source`
  together, chronologically: tier 1 ranks every not-yet-claimed candidate's
  edge-count diff and picks the best (never just the first found in iteration
  order — real data has more than one `SyncData` folder land within
  tolerance of a take's frame count when nearby takes ran similar durations,
  and the *exact* one is the one actually confirmed correct), claiming
  `(folder, segment)` rather than the whole folder so one `SyncData` can
  verify-match several takes; tier 2 (`match_take_to_twophoton`) is the
  timestamp fallback, now run only against the still-unclaimed pool — which
  also fixes the double-booking that produced most of the real `ambiguous`
  flags, using nothing but timestamps. `h5py` is lazy-imported (the
  `twophoton` extra) and every read is best-effort — no h5py, no episode
  file, or no signal match just falls through to tier 2, never raises.
  Validated end-to-end against a full real experiment (5 takes): every take
  verified, zero ambiguity, matching the pairing manual timestamp inspection
  had already suggested.
- **ThorSync vs. ThorImage start-time proxies differ.** A `SyncData*`
  folder's own directory mtime is a decent start proxy (only its two files —
  `Episode001.h5`, `ThorRealTimeDataSettings.xml` — are ever created in it, so
  nothing bumps the directory's mtime again). A ThorImage folder's directory
  mtime is **not** usable this way (thousands of per-frame `.tif` files keep
  bumping it for the whole capture) — its `Experiment.xml`'s own `<Date
  uTime="...">` attribute is used instead.
- **ThorSync/ThorImage are not always 1:1 with each other or with one take.**
  The operator starts each independently; a `SyncData*` folder can span, or
  miss, a take's `Fly*` folder. `match_take_to_twophoton` returns 0–2 matches
  (at most one per kind) and never forces a pairing that isn't there;
  `twophoton_match.json` (written alongside `recording_summary.json`, not a
  mutation of it — the summary is already finalized and written earlier in
  teardown, see the plugin lifecycle note above) records what was matched and
  flags ambiguity for audit.
- **No definitive "acquisition complete" marker exists** in either folder
  type — `is_settled` is a straight port of the old script's mtime-quiescence
  idea (`find_last_file_access_time` + delay), because there's nothing better.
- **NAS layout is not flat**: `<relative_directory>/2P/<original-folder-name>/`
  sits alongside `Behavior/` (per-camera archival `.mp4`) and `Renderings/`
  (grid + future composited output) — see `docs/guide/processing.md`. The raw
  capture-time `.mkv` is deliberately **not** part of this layout: it is
  itself already lossy (libx264 `-crf 18`, "near-visually-lossless" per
  `writer.py`), the transcoded `.mp4` is a second-generation re-encode of it,
  and the product has always treated the `.mp4` as the archival tier
  (`--delete-source` already discards the `.mkv` once transcoded) — so "raw
  per-camera data" here means each camera's own `.mp4`, not the `.mkv`. When
  `--no-transcode` supplies `outputs` via a naive `folder.glob("*.mp4")`
  (rather than real per-camera transcode results), it can't tell a camera file
  from `grid.mp4` sitting in the same folder — confirmed as a real bug via a
  dry-run against already-transcoded production data, now filtered out by
  excluding `_visualizations_for` names from the `Behavior/` list.
- **Recordings transferred before this layout existed sit flat** at the
  destination (`camera_LF.mp4` directly, no subfolders) — indistinguishable
  from "not yet transferred" to the layout-aware skip check, confirmed via a
  real dry-run against yesterday's already-transferred recordings (it wanted
  to recopy everything). `octacam process --migrate-layout --config <rig>`
  reorganizes those in place with a same-filesystem rename (true for CIFS/SMB
  too — its rename is a server-side directory-entry change, not a byte copy),
  so it cannot put data integrity at risk; self-contained (classifies each
  file against `recording_summary.json`'s own camera list, no local recording
  needed) and idempotent.
- **Recordings made before this feature existed have no `plugins` key at all**
  (`schema_version < 4`) — confirmed against real production recordings (all
  of yesterday's, `trigger_source: "external"` but no way to have recorded
  `armed`). The Phase 3 gate falls back to attempting the time-window match
  for any `schema_version < 4` take (still gated on `[transfer.twophoton]`
  being configured) rather than silently losing every pre-upgrade recording's
  pairing; a genuine `schema_version >= 4` take with `armed: false` is not
  this case and is skipped as intended.
- **2P-only recordings** (no behavior take at all) use a separate sweep
  (`_sweep_unclaimed_twophoton`, `octacam process --twophoton-sweep` as its
  own standalone entry point), not the per-take path — there's no `armed`
  take to gate on. It cross-checks already-written `twophoton_match.json`
  sidecars (via `session_cache.all_folders()`) so a folder already paired
  with a take isn't re-copied as "2P-only". A normal `process` run does this
  **automatically**, sequentially, right after Phase 3's per-take matching,
  for every distinct `(source_root, dest_root)` its processed folders'
  configs referenced (`--no-twophoton-sweep` opts out) — this is what
  actually separates a real pair from a standalone check/tuning recording:
  anything the duration-aware matcher correctly declines to pair lands here
  instead. One subtlety: `--dry-run` never writes `twophoton_match.json` to
  disk, so the sweep's on-disk "already claimed" check alone can't see a
  match Phase 3 just decided moments earlier in the *same* run — a
  `matched_this_run` set collected during Phase 3 (passed as
  `_sweep_unclaimed_twophoton`'s `also_exclude`) closes that gap so the
  dry-run preview matches what a real run actually does (which doesn't need
  it — the sidecar is on disk before the sweep starts).
- **`--delete-after-transfer`** is a distinct, stronger safety contract from
  `--delete-source`: transfer-gated (only after every file — behavior *and*
  any matched 2P folder — is checksum-verified present on the NAS, never
  weaker than `[transfer].checksum`) rather than transcode-gated, and it never
  deletes a folder that had a transcode failure this run even if every other
  camera's output transferred cleanly.
- **Run-ordering gap, fixed**: a behavior take can be finalized by one
  `process` run before its true 2P counterpart has even appeared/settled on
  the share yet (confirmed on real data — see
  `TWOPHOTON_MATCHING_BUG_REPORT.md`); nothing used to ever revisit an
  already-finalized take, so the pair was permanently missed even though the
  timestamps genuinely overlapped. Fixed with a `twophoton_pending.json`
  sidecar (written instead of `twophoton_match.json` whenever Phase 3
  finalizes an armed take with zero settled matches) that
  `_pending_twophoton_takes` finds via the same cache-wide
  `session_cache.all_folders()` reach `_already_matched_twophoton_paths`
  already uses. `_sweep_unclaimed_twophoton` now retroactively batch-matches
  every pending take against newly-settled, still-unclaimed folders *before*
  writing any of them off as standalone 2P-only data — in both the automatic
  post-`process` sweep and the standalone `--twophoton-sweep` mode, since
  either can be the first run to see a late-arriving 2P folder. Shares the
  same accepted limitation as `_already_matched_twophoton_paths`: a take
  whose local folder `--delete-after-transfer` already removed (or that has
  aged out of `session_cache`'s `RETENTION_DAYS`) can no longer be found this
  way — the pending marker lives only in the take's own local folder, not on
  the NAS.
- **Still deferred**: a synced behavior+2P *preview video* (different fps,
  same wall-clock window) is out of scope even now that `twophoton_signals.py`
  reads `Episode001.h5`'s edges for matching *verification* — that's a
  different consumer of the same data (per-frame alignment for a rendered
  video, not a yes/no pairing check) and still needs its own design pass.
- **TIFF stack assembly (`twophoton_tiff.py`)**: ThorImage's streaming-mode
  capture writes one `.tif` file **per frame per channel** — a 1500-timepoint,
  2-channel recording is 3000 loose files. Neither `<Streaming enable>` nor
  `<CaptureMode mode>` in `Experiment.xml` reliably says "already one file" vs
  "needs assembly" — every real recording inspected (streaming, Z-stack, and
  `rawData="1"` aborted test captures) writes per-frame files or none at all,
  never a native single stack — so detection is disk-truth-driven: count each
  channel's actual files (from `<Wavelength name>`), find which of the 4
  underscore-separated numeric groups in the filename actually varies (never a
  hardcoded position — streaming varies position 3, a Z-stack varies position
  2, both confirmed on real data), then cross-check the count against
  `Timelapse/@timepoints` (T-axis) or `ZStage/@steps` (Z-axis) — a real
  truncated/incomplete recording (confirmed on production data: a folder
  declaring 1000 timepoints with only 89 real files on disk) fails this check
  and falls back to a plain per-file copy rather than assembling an
  incomplete stack. One OME-TIFF per channel, not one combined file — a
  structural/reference channel (motion-correction only) and a functional
  channel (e.g. GCaMP, ROI/ΔF-F extraction) are consumed by different
  pipeline stages that never need both loaded together, and combining them
  would assume a per-timepoint correspondence between channels that can't be
  verified from `Experiment.xml` alone. **`tifffile.imread()` is unsafe** for
  reading a single source frame here — confirmed on real data that ThorImage
  already embeds real OME-XML in a Z-stack/streaming set's first file,
  cross-referencing every sibling file by name (`<TiffData FirstZ FirstC>
  <UUID FileName=.../>`), and `imread()` follows that linkage, silently
  returning a multi-file-stitched array instead of just one file's own frame
  — always read via `TiffFile(path).pages[0].asarray()` instead. Available at
  transfer time (`[transfer.twophoton].assemble_tiff_stacks`, default on,
  degrades automatically without the `twophoton` extra's `tifffile` the same
  way `verify_with_signals` degrades without `h5py`) and as a standing,
  manually-invoked `octacam process --reassemble-tiffs` mode for
  already-transferred NAS data or data someone saved as multiple TIFFs by
  hand — built as a real CLI mode rather than a throwaway script since the
  assembly library already exists for the transfer-time hook. The ad-hoc mode
  holds a higher safety bar than the transfer-time hook (it deletes the
  now-redundant per-frame originals): every assembled page is read back and
  compared pixel-for-pixel against its source frame before anything is
  removed, one file at a time, only after the assembled stack is safely
  written.

## Arduino firmware & auto-flash

Each sketch's identify banner ends with a **source-hash fingerprint**
(`"<NAME> <ver> <build>"`). The committed `fw_build_info.h` ships an `UNBAKED`
placeholder; octacam bakes the real hash into a *throwaway copy* of the sketch at
flash time (the repo tree is never dirtied). `firmware.py` +
`FirmwareProvisioner` classify a board (CURRENT/OUTDATED/WRONG_BOARD/…) and offer
`arduino-cli` flashing under a re-entrant `port_lock`. `octacam flash` and
`octacam record --yes` drive it. flywheel uses a backward-compatible identify
*sentinel* (its wire protocol is frameless, so no forced reflash).

## Web GUI

FastAPI + a vanilla-JS static frontend (`web/static/`). Preview/telemetry/control
share one WebSocket.

**Deferred startup (serve-first).** `octacam gui` binds uvicorn and serves the
page *before* touching hardware, so time-to-first-paint never waits on vendor-SDK
camera enumeration or a trigger-board handshake. The controller is constructed
`ready=False` against a hardware-free `CameraSystem.pending()` placeholder (0
cameras — every consumer, snapshot/preview-loop/`/api/system`, reports an
"initializing" system safely). A daemon init thread then opens the cameras and
arms the serial plugins **in parallel** (independent USB vs serial hardware),
loads params, calls `controller.attach_system(real)` (atomic reference swap
under the GIL — the preview/telemetry loops re-read `camera_system` each tick, so
a reader sees the empty placeholder or the real system, never a torn state),
starts preview, then pushes a fresh `system` WS message. The
frontend renders the whole shell up front with a grid placeholder and builds the
grid/View+Camera tabs/Save dialog lazily in `buildCameras()` when the camera list
arrives (from the initial `/api/system`, the WS-connect handshake — which sends
`system` too, closing the connect-vs-init race — or the init broadcast). Plugin
readiness fills in via each tab's `applyStatus(info)` — which is also a plugin
tab's **only** signal that the cameras are now open, so a tab that reads camera
state at construction (e.g. triggerbox's timing plot pulling
`/api/triggerbox/exposures` for its auto strobe duty) must re-read it there or it
shows placeholder-era data for the whole session. Camera-open failure calls
`controller.fail_init(msg)` (surfaced in the GUI + logged) instead of aborting
the now-running server. On shutdown the finally sets a `stopping` event and
`join()`s the init thread before `controller.close()`, so arming can't race
teardown. The **adaptive preview** protocol sends a per-client
per-camera "view spec"; the server encodes each distinct on-screen resolution
once and shares it (cost tracks resolutions, not clients), with server-side crop
of a zoomed region (frame header v2). The **Camera tab** is a full GenApi
node-map browser (typed widgets, per-field reset, ROI auto-center; nodes writable
only while not grabbing cycle the preview grab). Plugin tabs live in a responsive
overflow menu; theme is a rig config option overridable per-browser.

**Keyboard shortcuts** live in one place: `web/static/js/shortcuts.js`
(`initShortcuts({grid})`, wired once in `app.js main()`). It installs a single
document-level `keydown` listener driven by one binding table, from which the `?`
help overlay and the button `title=` hints are also generated (so they can't
drift). Invariants worth preserving: every binding routes through the same
`suppressed()` guard (no bare key fires while a text/`select`/contenteditable is
focused or a modal is open), no binding uses bare `Enter`/`Escape`/`Tab` (owned by
field/modal handlers), and actions **click the real control / call the real grid
method** so gating (`disabled`), state-aware labels, and confirms are reused, not
duplicated. Recording start/stop is `Ctrl/Cmd+Enter` on purpose (a stray key must
never abort a live trial). Add a `tests/test_frontend.py` case for any new
binding.

To **see** GUI changes without the rig: render the real frontend in the cached
Playwright Chromium and read the screenshot (serve `web/static` over
`http.server`, `import()` the real module, call pure methods on the prototype).
Gotchas: a `ResizeObserver` fires once on `observe()`; SVG text in a `viewBox`
scales with the box (draw at real pixel size); force a theme by setting
`data-theme` (not `--force-dark-mode`).

## CLI notes

Seven commands: `gui`, `doctor`, `config` (scaffold a rig interactively),
`record`, `flash`, `benchmark`, `process`. `doctor` never opens a camera (safe
during a live session). A rig **instance-lock** prevents two octacams owning one
rig.

**typer gotcha:** typer 0.26 vendors a *forked* click that drops `flag_value`, so
an optional-value option (bare `--flag` vs `--flag X`, e.g. `process --last`) is
implemented with a custom `TyperCommand.parse_args` (`_ProcessCommand` /
`_inject_default_last` in `cli.py`), not click internals.

## Benchmark / diagnostics

`diagnostics.py` measures a **grab ceiling**, an **encode ceiling**, and an
**end-to-end trial** to name the bottleneck (acquisition / encode / host /
TRANSFER). It **deliberately re-implements the grab loop** (rather than reusing
`Camera._record_loop`) to keep the hot record path zero-overhead and to isolate
ceilings — do NOT "simplify" this away. Two intentional no-op seams support it:
the read-only `Camera.backend` property and `AsyncFrameWriter(profile=False)`.
Free-run / transfer numbers are not yet calibrated on real hardware.

## Hardware quirks & test rig

- Nano ESP32 **D13 = GPIO48 = 3.3 V LVTTL** (not a classic AVR's 5 V). The
  common-trigger-circuit board does **not** buffer the camera trigger: D13 routes
  raw to screw terminal J7-4 (GND on J5-1). Only the 3 CCS light channels have
  transistors.
- Camera trigger inputs are opto-isolated, each needs its own ground return:
  **Basler acA1920-150um** (Hirose HR10A-7R-6PB) Pin2=Line1 in / **Pin5=opto-gnd**;
  **FLIR GS3-U3-41C6NIR** (Hirose HR25-7TR-8SA) Pin1=Line0 in / **Pin6=opto-gnd**.
- DFU flashing the Nano ESP32 needs a udev rule for VID 2341/303a (else
  `LIBUSB_ERROR_ACCESS`).
- **Test rig:** 2× FLIR GS3-U3-41C6NIR (SN 17475185/17475187, 2048², CMV4000
  CMOS) + up to 4× Basler acA1920-150um (SN 40018619/40018631/40018632/40022761,
  and 40023151; 1920×1200); external trigger via the common-trigger-circuit Nano
  ESP32. SDKs at `/opt/spinnaker` (Spinnaker) and `/opt/pylon` (Basler pylon).

## Where the deep detail lives

Per-feature engineering history and unverified-on-hardware caveats are captured
in the project's agent memory (`~/.claude/projects/-home-tlam-octacam/memory/`) —
backend migration, benchmark design, FLIR frame-rate, preview/trigger-source,
adaptive preview, camera-tab node map, firmware auto-flash, and the triggerbox
plugin. This file is the durable summary; those are the source of record for
"why".
