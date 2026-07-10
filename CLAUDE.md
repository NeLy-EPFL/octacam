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

PYLON_CAMEMU=8 octacam gui configs/emulate_8_cameras   # run with 8 fake cameras, no hardware
```

- **`fake` backend** is the CI vehicle — a rich SFNC-keyed synthetic camera that
  exercises every node/widget kind without hardware. Prefer adding fake-backed
  regression tests over hardware-only assertions.
- **Lint/type baselines** (pre-existing, not introduced by new work): ruff has a
  couple of intentional `E501`/`F401`-style items already in `[tool.ruff]`
  ignore or documented; pyright reports ~45 errors, mostly `self.raw`/`self._cam`
  Optional-access in the vendor backends and cli. **New work must add zero.**
- **Full-suite runtime is ~8 min.** Run the relevant `tests/test_*.py` file(s)
  during development; run the whole suite before committing.
- **Known crash:** on some setups `pytest` can SIGSEGV *at process teardown* when
  pypylon + genicam are both loaded in one process (a multi-lib native-teardown
  interaction, not octacam code — the tests themselves pass). Don't chase it.

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
| 1 | `flir` | Spinnaker SDK + **PySpin** | FLIR vendor SDK; **PySpin wheel is cp310-only** → this tier drops out on Python ≥3.11 |
| 2 | `spinnaker` | `libSpinnaker_C.so` via **ctypes** | Same FLIR cameras as `flir`, no cp310 limit → claims FLIRs on modern Python; ctypes releases the GIL on the blocking grab |
| 3 | `pycameleon` | libusb (Rust `cameleon`) | Always-present floor; no vendor SDK/producer/EULA |
| — | `harvesters` | GenTL producer | **Opt-in only, never auto** (see below) |
| — | `fake` | synthetic | CI vehicle; only used when named |

**Why `harvesters` is not in the cascade:** the only freely-installable U3V GenTL
producer we validated is Balluff's **mvIMPACT**, whose free eval expires after
~8 s of streaming and stamps a "BALLUFF … evaluation period ended" watermark onto
frames (its EULA also restricts third-party-hardware use to paid licensing).
Auto-routing a FLIR through it would silently watermark recordings, so
`harvesters` is used only when a rig explicitly sets `backend = "harvesters"`.

### GenTL producer facts (empirical, on the test rig)
- **mvIMPACT** (`/opt/ImpactAcquire`): the only producer that sees all cameras
  (incl. Basler over U3V) and closes cleanly (~0.3 s). Correct choice when
  harvesters is used. Selection via `OCTACAM_GENTL_PRODUCER` + a default denylist.
- **Spinnaker GenTL** (`Spinnaker_GenTL.cti`): **`DevClose` deadlocks AND holds
  the GIL** → wedges the whole process; only an external SIGKILL ends it.
  Denylisted; disabled from the GenTL path.
- **Vimba X USB TL**: AVT-vendor-only → sees 0 third-party cameras. Useless here.

### Backend contract (`cameras/base.py :: CameraBackend`)
All backends implement: enumerate/open/close; `load_params`/`save_params`;
frame-trigger setup; a **software-trigger hand-off** (below); `begin_freerun` /
`retrieve_freerun` (used by the benchmark and free-run preview); and a full
GenApi node-map walk (`list_features`/`read_feature`/`write_feature`/
`execute_command`) for the Camera-tab node browser. `basler`/`harvesters` walk
via genicam; `flir`/`spinnaker` walk the C/PySpin node map; `pycameleon` (no
introspection) and the base fallback use a curated node set.

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
- **Every other GenICam backend** (flir, spinnaker, harvesters, pycameleon,
  fake) → the native **GenApi persistence TSV** (`.txt`) via
  `cameras/_genicam_config.py` (`apply_config`/`dump_config`/`parse_config`). The
  unified `.txt` format lets a rig switch flir↔spinnaker (PySpin 3.10 ↔ ctypes
  3.14) sharing the same param files.

**Trigger normalization on save:** a GUI "Save" taken while previewing with a
software trigger must not bake `TriggerSource=Software` into the file (it would
make a later external-trigger recording silently never start). Every backend's
`save_params`/`dump_config` restores the camera's original (config) trigger
source via `normalize_trigger_source` — keep this parity when adding a backend.

`config_writer._toml_value` must serialize every scalar the loader can produce —
including inline tables (triggerbox's nested `cameras`/`lights` arrays) and
date/datetime scalars (a bare serial number or date-like save dir parses as one).

## Plugin system

Serial-hardware plugins under `plugins/<name>/`, registered in
`_BUILTINS = ("flywheel", "twophoton", "triggerbox")` with legacy
`_ALIASES = {"arduino": "flywheel", "omniview": "triggerbox"}` (old configs keep
working). The default launch loads none; enable via `[[plugins]]` or `--plugin`.

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
  ESP32. SDKs at `/opt/spinnaker` (Spinnaker) and `/opt/ImpactAcquire` (mvIMPACT).

## Where the deep detail lives

Per-feature engineering history and unverified-on-hardware caveats are captured
in the project's agent memory (`~/.claude/projects/-home-tlam-octacam/memory/`) —
backend migration, benchmark design, FLIR frame-rate, preview/trigger-source,
adaptive preview, camera-tab node map, firmware auto-flash, and the triggerbox
plugin. This file is the durable summary; those are the source of record for
"why".
