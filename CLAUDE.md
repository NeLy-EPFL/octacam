# CLAUDE.md

Development notes and architecture for **octacam** — for contributors and coding
agents. This file captures the durable, hard-won knowledge that isn't obvious
from the code alone. Keep it current when you change the things it describes.

octacam previews, records, and archives **synchronized video from many
scientific cameras** (Basler / FLIR / any GenICam USB3-Vision) through a live
FastAPI web GUI and a headless CLI, plus Arduino-driven trigger/strobe hardware
and one-command post-processing. It targets neuroscience rigs and is the
successor to SeptaCam.

> Active development is on branch `feat/harvesters-backend` (migrating the camera
> layer off pypylon/PySpin toward a multi-tier GenICam cascade that runs on
> Python 3.10–3.14). `requires-python >= 3.10`.

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
- **Known crash:** on some setups `pytest` can SIGSEGV *at process teardown* when
  pypylon + genicam are both loaded in one process (a multi-lib native-teardown
  interaction, not octacam code — the tests themselves pass). Don't chase it.

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
3. Tag it: `git tag -a vX.Y.Z -m "octacam X.Y.Z"` (publish with `git push --tags`).
4. Bump to `X.Y.(Z+1).dev0`; commit `chore: open X.Y.(Z+1) development`; add a fresh `[Unreleased]` block.

An **update notice** (`updates.py`, surfaced in `octacam doctor` and a dismissible
GUI banner via `/api/system`'s `update` field) compares the installed version
against the latest stable on **PyPI** and advises the correct per-install upgrade
command (pip / uv tool / pipx / conda). It is read-only and fail-silent —
**octacam never updates itself** (a library must not mutate its own install, and
octacam is also sometimes a `uv add` dependency or lives in a conda env) — honors
`OCTACAM_NO_UPDATE_CHECK` / `DO_NOT_TRACK`, and stays dormant until octacam is on
PyPI (the Simple API 404s → no signal). Branch model: `main` is stable (docs
published as `stable`); `dev-*` are the development line (docs `dev`), published
by mike on `gh-pages`.

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

`config_writer._toml_value` must serialize every scalar the loader can produce —
including inline tables (triggerbox's nested `cameras`/`lights` arrays) and
date/datetime scalars (a bare serial number or date-like save dir parses as one).
A `None`-valued field is **omitted** (TOML has no null); the loader restores its
default on read, so an "auto" field like `record.max_nvenc_sessions = None` must
never be written as a literal — the `octacam config` scaffold `model_dump()`s the
whole `RecordConfig`, so any None-defaulting field would otherwise crash the dump.

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

**triggerbox** generalizes the EPFL `common-trigger-circuit` (Arduino Nano
ESP32). Self-describing wire protocol v2: `0xA5 | ver=2 | len u16 | payload |
xor`; payload = fps, duration_ms, N camera lines `{pin, pulse_us, delay_us}`, and
3 symmetric light channels (`off`/`strobe`/`continuous`/`pulse_train`).
`PIN_LABELS` is the single source of truth, mirrored in `triggerbox.ino` and
asserted equal by a unit test. Server-side **auto strobe duty** sizes the LED
on-time to the longest live camera exposure. On a wedged USB CDC link the plugin
auto-recovers via a host `USBDEVFS_RESET` bus reset and surfaces the error loudly.

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
share one WebSocket. The **adaptive preview** protocol sends a per-client
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
