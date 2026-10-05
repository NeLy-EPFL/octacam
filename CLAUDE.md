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
uv run pytest -q -o addopts=""            # ~1,650 tests, ~6 min (-o skips slow coverage)
uv run ruff check src/                    # lint (clean)
uv run pyright src/                       # types (documented baseline; net-new must be 0)
uv run --group docs mkdocs build --strict # docs build + link/nav validation
uv run --group frontend pytest tests/test_frontend.py -o addopts=""  # browser GUI tests, on their own

PYLON_CAMEMU=8 octacam gui configs/emulate_basler   # run with 8 fake cameras, no hardware
```

- Run the touched `tests/test_*.py` files while developing, the whole suite
  before committing.
- **`fake` backend** is the CI vehicle — a rich SFNC-keyed synthetic camera that
  exercises every node/widget kind without hardware. Prefer adding fake-backed
  regression tests over hardware-only assertions.
- **Lint/type baselines:** ruff is clean; pyright reports 4 errors (the fake
  backend's node-table literal and two float() calls on its `object` feature
  values; recording_format's `savez_compressed(**arrays)`); every backend's SDK
  handle is typed `Any`. **New work must add zero.**
- **`tests/conftest.py`** sets the env (`PYLON_CAMEMU`, `OCTACAM_FAKE_CAMERAS`,
  `OCTACAM_NO_UPDATE_CHECK`), gives each test its own `OCTACAM_CACHE_DIR`,
  restores the `octacam` logger after each test (so use `caplog` —
  `caplog.set_level(..., logger="octacam")` for INFO/DEBUG — never attach a
  handler), and stubs arduino-cli flashing and USB resets (`no_flash`,
  `no_usb_reset`; a module that tests those functions overrides them by name).
  Poll with `tests/helpers.py::wait_until`; build a camera's take record with its
  `camera_stats(name, frames=..., **fields)` (a full-field `CameraStats`); patch
  the take's timing constants on `octacam.take`. Tests open the emulated rig with
  `backend="basler"` and the doctor tests use test_cli's `emulated_rig`, so the
  suite never touches the real cameras on the dev rig.
- **Frontend has a browser test harness** (`tests/test_frontend.py`): it loads
  the real `web/static/js/*.js` ES modules in a headless Chromium against the
  real `index.html` and asserts GUI wiring (fields exist, enable/disable,
  applySettings round-trips). It needs the `frontend` group (it skips without
  playwright and self-skips without Chromium), and the conftest collects it only
  when the command line names it. Add a case whenever a backend setting gains a
  GUI control, so "shipped a knob with no widget" gets caught.
- **Run the frontend tests on their own.** playwright's *sync* API leaves a
  running event loop in the process, so any later test calling `asyncio.run`
  dies (`tests/test_web.py` sender cases, `tests/test_pycameleon_backend.py`
  retrieve cases); naming the file in a list with others still collects it.
- **The dev rig runs Python 3.14, but `requires-python` is `>= 3.10`.** 3.14
  evaluates annotations lazily (PEP 649), so a bug a 3.10–3.13 user hits at
  *import* is invisible here: a name imported only under `if TYPE_CHECKING:` must
  be **quoted** in an annotation Python evaluates (function signatures;
  module/class-level variable annotations — function-*local* ones, like
  `self._task: TaskID | None` in a method, are never evaluated). An unquoted one
  took out the whole CLI below 3.14.
  `tests/test_typing_hygiene.py` walks the AST for this on any version; an import
  test cannot.
- **Pylon's GenTL producers segfault at exit**: pylon's GenTL transport layer
  loads the *system* pylon's producers from `GENICAM_GENTL64_PATH`
  (`/etc/profile.d/basler-gentl-path.sh`), whose `libuxapi` unload segfaulted
  every `octacam record` and pytest process at exit (kernel log:
  `segfault at …88bb9 … error 14`). `basler.tl_factory()` loads the transport
  layers with the path hidden; every pylon entry point must go through it (pylon
  reads the path only at the first `TlFactory` use, so a process that enumerated
  before octacam still loads them).

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

An **update notice** (`updates.py`, in `octacam doctor` and a dismissible GUI
banner via `/api/system`'s `update` field) compares the installed version with
the latest stable on **PyPI** and advises the per-install upgrade command (pip /
uv tool / pipx / conda). It is read-only and fail-silent — **octacam never
updates itself** (a library must not mutate its own install; it is also a
`uv add` dependency or lives in a conda env) — honors `OCTACAM_NO_UPDATE_CHECK` /
`DO_NOT_TRACK`, and stays dormant until octacam is on PyPI (the Simple API 404s).
Branch model (git-flow): `main` is stable; `develop` is the one permanent
development line (a maintenance line for an older release is named by series,
like `0.3.x` — never a versioned `dev-*`). mike publishes versioned docs to
`gh-pages`: each release tag a numbered version, the `stable` alias (and the site
root) the newest release, and `develop` pushes the rolling `dev` version.

## Repo layout

```
src/octacam/
  cli/              typer CLI, one module per command group (gui, doctor, wizard=config,
                    record, flash, benchmark, process+check, admin=jobs+cache; _common)
  controller.py     RecordingController: state machine, admission gates, preview arming
  take.py           one recording (Take): start, priming, monitor, teardown, check_sync, files
  pulses.py         trigger-pulse accounting (PulseTracker, PulseClock)
  config.py         octacam_config.toml parsing (pydantic, tolerant per field; ConfigError
                    if unparseable), the live RecordingSettings, save-path/safe-name rules
  config_writer.py  writes config snapshots (inline-table TOML for triggerbox)
  recording_format.py  the recording on disk: layout, discovery, summary/timestamps schema
  files.py          atomic text writes, partial temps, flock liveness
  locks.py          the rig instance lock
  ffmpeg.py         ffmpeg toolchain: discovery, encoder/NVENC probes, 4:2:0 policy, quiet_argv
  writer.py         capture writers (AsyncFrameWriter → ffmpeg pipe or raw) + overload policy
  transform.py      DisplayTransform (rotate/flip)
  process.py        octacam process: ProcessOptions, run, transcode/grid/transfer phases
  process_jobs.py   detached process jobs; the run's worker, reporter and pause gate
  transcode.py      octacam process → one transcode_file into an atomic partial output
  grid.py           octacam process → grid videos (xstack; opt-in via [[visualization]])
  transfer.py       octacam process → mirror recordings to storage
  check.py          octacam check: screen recordings for missed pulses / desync
  session_cache.py  remembers recording folders for `process --last/--all`
  diagnostics.py    the frame-rate benchmark engine
  firmware.py       Arduino sketch fingerprinting + arduino-cli flashing
  serial_ports.py   serial-port detection, USB bus-reset recovery
  updates.py        the read-only update notice
  trigger.py        precise periodic callback timer (the software trigger's pace)
  cameras/          backends (below): base.py (CameraBackend, Camera), take.py (CameraTake,
                    CameraStats), system.py, registry.py, genicam.py, one module per backend
  plugins/          serial hardware (triggerbox, twophoton, flywheel) on serial.py's SerialPlugin
  web/              FastAPI GUI: app.py, hub.py, preview.py, state.py, per-area routers, static/
arduino/            triggerbox, 2photon_trigger, stepper_motor sketches
configs/<rig>/      octacam_config.toml + per-camera sensor files (.pfs / .txt)
tests/              pytest suite (fake backend + faked SDK facades)
docs/               Material for MkDocs site (published to GitHub Pages)
```

## The recording pipeline

`CameraSystem` (a set of `Camera` objects, each wrapping a per-camera
`CameraBackend`) is driven by `RecordingController`, which the web app and the
headless `octacam record` share: a framework-free state machine
(`preview/idle → waiting → recording → finishing → preview/idle`) that admits a
recording and drives one `take.py :: Take` through it. A per-recording **monitor
thread** polls for the first frame on every camera (firing plugin
`on_first_frame` hooks at that t0), enforces the recording deadline, then runs
the teardown in a fixed order (stop trigger → grab loops exit → writers drain →
sync check → `octacam_recording/recording_summary.json` + `timestamps.npz` +
session-cache note). Subtleties that are easy to break:

- **Plugin hooks dispatch OFF the controller lock** (a plugin's serial write can
  block on an ack), guarded by the `Take.hooks_done` event so a stop/abort can
  never overtake a not-yet-sent hardware arm.
- **Camera control is locked** (`_camera_locked`) while `recording_active`,
  `diagnosing` or `_starting` — any device-touching op (start, param write,
  node-map snapshot) is refused. The camera-parameter export (`take.export_camera_params`) and the
  delivery profiles (`take.read_delivery_profiles`) are read **off** that lock,
  before admission (next to the NVENC warm-up): the export walks every camera's
  node map over USB with no timeout, and `snapshot()`/`stop_recording()`/
  `notify_state()` take the same lock, so one stalled camera wedged the GUI's
  status and Stop button. A finished take's off-lock tail (plugin disarm +
  preview re-arm) holds `_tearing_down`, which refuses a new recording *and a
  benchmark*; `RecordingController._busy_reason` is the one table of refusals.
- **A managed preview's trigger arm is canceled *before* the record grab starts.**
  `start_recording` is two-phase: admission + stop the preview grab under the
  lock (fast: pulses still flow) → `on_preview_stop` off the lock under the
  `_starting` gate → `start_record` → `on_recording_start`. A recording arm that
  superseded the preview arm while the cameras grabbed re-phased the board's
  frame clock, and a camera in overlapped readout (`TriggerOverlap=ReadOut`) then
  delays each exposure to the end of the previous readout while the strobe stays
  on the trigger edge: a dark ramp over the first ~7 frames (period − readout ≈
  0.3 ms of catch-up per frame on the GS3 at 125 fps; 7.7 ms intervals). The
  cancel waits for the board's `C` and the cameras are then primed (see below), so
  frame 0 is the train's first pulse, as for headless `octacam record`.
- **A rig that opens fewer cameras than its config asks for is not silent.**
  `CameraSystem` records the shortfall (`.missing`, `.incomplete`), logs one
  `INCOMPLETE RIG` warning naming each camera, exposes it as `missing_cameras` in
  `/api/system` (the GUI shows a persistent warning), and `octacam record`
  confirms before recording (or warns under `--force`/non-interactive). Carrying
  on without a failed camera is deliberate, but synchronized N-camera capture is
  the point, so a 7-of-8 session must never look like a healthy one.

`CameraSystem.for_config` is the one rig-open path (gui, record, benchmark): it
names the cameras, loads their parameter files and display settings, and closes
every camera on any failure (one left open to interpreter teardown can crash it).
Each `Camera` runs its own grab thread (the preview loop, or its `CameraTake`'s
record loop); `retrieve()` must **never raise** (it returns `None` on a
device/stop-race error) or a dead grab thread would orphan its ffmpeg writer.

**Every frame is assigned to the trigger pulse that exposed it** (`pulses.py ::
PulseTracker`, fed by the record loop of `cameras/take.py :: CameraTake`, which
leaves a `CameraStats` for the summary, the sync check and the timestamps file).
A hardware-triggered camera that misses a pulse delivers *nothing* for it, so
frame counts cannot tell it from a healthy one — a camera that missed pulses
used to drift a frame behind per miss and still match the others' count by
catching trailing pulses. Rules that are easy to break:

- **Per-interval rounding against the *measured* period**, not a phase-locked
  grid: an interval of k periods is k−1 missed pulses. An exposure is never
  before its pulse and its delay (trigger latency, a falling-edge trigger
  +500 µs, readout catch-up, a late board pulse) stays far below half a period,
  so rounding is unambiguous; a phase-locked model lost count through a re-armed
  board's phase jump (half the archive). The measured period absorbs the camera
  clock's ppm drift (the top GS3 reads the 8 ms board period as 7.998945 ms), so a
  long outage is bridged exactly; `clock_mismatch` flags a camera that does not
  follow the period (free-running). A jump by a whole **128 s** is folded (a GS3
  timestamp-extension race); any other jump the host contradicts is re-anchored
  by host time. That host check is **one-sided** (`PulseTracker._host_allows`):
  delivery only ever lags, so a camera interval up to the host interval plus the
  previous frame's backlog plus 1.5 s is real. A grab stall the 128 FLIR stream
  buffers absorb (dt = one period, dh = seconds, then a burst with dh ≈ 0) must
  never read as a clock jump: re-anchoring it filled pulses the camera never
  missed, shifted its video and ended its take early.
- Under the **software trigger** the index comes from the hand-off's trigger
  sequence number (`CameraBackend.last_trigger_index`): a fetched image
  answers the **oldest outstanding** trigger (see the hand-off below), so an
  image that arrives after its fetch timed out keeps its own pulse; a backend
  with no hardware timestamp (pycameleon) is `unclocked` and cannot detect misses.
  A stray zero timestamp on a clocked camera is placed as the next pulse on that
  camera's own clock.
- **Fill, don't skip** (`software`/`managed`, `PulseClock.fill`): a missed pulse
  *and* a frame the writer queue refused are written as a repeat of the previous
  frame (`AsyncFrameWriter.write(frame, fill_before=n)` — the fill rides on the
  next queued item so it can never be dropped on its own), so video frame k is
  pulse k in every camera. `external` (a source octacam doesn't drive, possibly
  irregular) is report-only: nothing filled or discarded, `pulse_index` maps it.
  One exception, **writer overload**: a fill costs as much to encode as a real
  frame, so filling a *sustained* encoder/disk shortfall snowballs (real frames
  decay toward zero and `close()` blocks for minutes). Once a camera has refused
  more than `record.writer_queue_size` frames before its writer caught up (backlog
  ≤ half a queue), refused frames are **skipped** for the rest of the take
  (`writer_skipped`, no video frame and no row, one loud error) and `sync.ok` is
  False: `pulse_index` is then the frame-to-pulse map. The writer owns the policy
  (`AsyncFrameWriter.write` returns `WriteResult.WRITTEN`, `REFUSED` — owed as a
  fill — or `SKIPPED`); the take only accounts and logs. Camera-side misses are
  always filled. Fill rows are stamped from the tracker clock, else from the last
  delivered frame plus whole periods — never 0.
- **Priming**: a GS3 ignores its first hardware triggers after an acquisition
  start (two; more after a power-up; see the hardware quirks), so the record grab
  starts on `hold` and `Take.start_sequence` sends rounds of `PRIME_PULSES`
  sacrificial pulses (triggerbox `prime_trigger`: camera lines only, lights dark;
  or software triggers), each followed by a settle of max(`PRIME_SETTLE_S`,
  `PRIME_SETTLE_PERIODS` = 4 periods), **until every recording camera has
  answered one** (`primed_frames > 0`; a camera whose record grab failed to start
  is not waited on), then calls each `CameraTake.arm_counting()` and only then
  starts the train. A fixed count is not enough: two freshly powered GS3s
  answered none of four, and the whole take ran a pulse late against the board,
  silently (they were aligned with each other). No round starts after
  `PRIME_BUDGET_S`; a camera still silent is warned about. The monitor's
  `hooks_done` waits use `start_sequence_timeout_s(period, primed)` —
  `START_HOOKS_TIMEOUT_S` plus the priming's upper bound — because at 1 fps one
  round alone is 8 s. Frames before `arm_counting()`, and stragglers within
  max(`PRIME_STRAGGLER_NS`, 2.5 periods) of the last primed frame (which must stay
  below the settle), are discarded, as is a software-trigger image answering a
  pre-`arm_counting` trigger (`PRIMING_TRIGGER`). The timing constants live in
  take.py (`PRIME_STRAGGLER_*` in cameras/take.py). The whole sequence runs
  before `hooks_done`, which the monitor waits on *before* stopping anything, so
  a stop can never overtake the arm or the software timer's start.
- **Stop on the pulse count**: the countdown ends when every camera has the
  train's last pulse or once the train is over (`Take.train_end`: count × period +
  `TRAIN_END_MARGIN_S` from when the arm — `start_software_trigger` +
  `on_recording_start` — *returned*; anchored before it, a slow USB-reset re-arm
  ended the take early and filled its tail with repeats), since a camera that
  missed the last pulses cannot know it before then; `stop(fill_to=count)` pads
  it. A stopped/aborted take ends where it was. The managed train's exact period
  and count come from the plugin (`trigger_train`), not the settings.
- `take.check_sync` (a pure function of the cameras' `CameraStats`, the clock and
  the delivery profiles) compares when each camera's first frames reached the
  host: same-model cameras deliver a pulse within ~0.5 ms of each other, so one
  that started a pulse late shows up as a whole period (`start_offset_pulses`).
  Arrival includes exposure, readout and USB transfer (a 2048² GS3 lands ~4.5 ms
  after an acA1920 — most of a period at 125 fps), so offsets are compared only
  **between cameras with the same delivery profile** (backend, DeviceModelName,
  W×H, pixel format, exposure in whole µs; read at recording start, off the lock,
  never at teardown); across profiles an informational `sync.notes` entry is
  written. `sync.ok` is also False when a recording camera captured no frame or
  skipped writer frames.

The writer queue depth is `record.writer_queue_size` (default 64), tunable per
rig to absorb transient encoder stalls. Summary schema 4 reports, per camera,
`missed_pulse_indices`, `writer_dropped`, `late_pulse_indices`, `extra_frames`,
the SDK's `stream` counters (a missed pulse with none of them moving is a trigger
the camera never exposed, not a transport loss), `writer_skipped` /
`writer_skipped_pulse_indices`, a cross-camera `sync` (with `notes`) and whether
the take `completed` (ran to its end rather than being stopped); `dropped` counts
every fill. `octacam check` screens recordings: schema-4 ones from their own
accounting (the summary is authoritative without `timestamps.npz`, its capped
index lists are never counted, `sync.ok` and `completed` are honored),
pre-schema-4 ones re-derived from `timestamps.npz` (host-clock series skipped);
an unreadable folder is a problem, never an exception.

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

**No GenTL tier** (the `harvesters` backend and its deps were removed). Every
GenTL producer is a user-installed, vendor-EULA'd `.cti` with its own quirks
(watermarks, close deadlocks, vendor-only enumeration), and the libusb-only
`pycameleon` floor covers the general GenICam-USB3 camera better. Do **not**
reintroduce a GenTL/`.cti` path. Teledyne's **Spinnaker GenTL producer**
(`Spinnaker_GenTL.cti`) has a `DevClose` that **deadlocks while holding the
GIL** (wedging the process), which
is why the `spinnaker` tier drives the Spinnaker **SDK C API** over `ctypes`.

**Enumeration is not free, so the cascade is scoped and deadlined.** Two rules
that are easy to undo:

- Every tier is enumerated with the rig's **requested serial list**, never `None`
  — `spec.enumerate(requested_serial_numbers)`. The Basler tier's `CreateDevice`
  downloads each camera's XML over USB, so sweeping the whole bus made a rig pay
  for cameras it never opens (a 2-camera FLIR rig paid for six attached Baslers)
  and leaked the surplus handles, dropped without `DestroyDevice`. An enumeration
  never reports an absent serial (each tier sees serials another owns);
  `CameraSystem._enumerate` warns once for a serial no tier claimed. Lower tiers
  still enumerate the full requested set, so a camera a vendor tier missed falls
  through.
- `enumerate_basler` runs its `CreateDevice` calls **concurrently under a shared
  deadline** (`_create_device_timeout`, 15 s, `OCTACAM_BASLER_CREATE_TIMEOUT`).
  A camera whose link trains at 5000 Mb/s but whose control transfers time out
  held one rig for **271 s** inside a single call. A missed deadline maps onto the
  existing `(serial, None)` "present but unusable" sentinel. Three traps: the
  pool must **not** be a `with` block (`__exit__` is `shutdown(wait=True)` and
  re-blocks for the full retry, silently undoing the deadline); a straggler's
  handle must be released via `tl_factory.DestroyDevice` in a done-callback (the
  `InstantCamera.DestroyDevice` used in `close()` is a different method); and the
  workers are deliberately **non-daemon**, because letting the interpreter
  finalize under a live pylon call is the teardown segfault `BaslerBackend.close`
  documents.

### Backend contract (`cameras/base.py :: CameraBackend`)
An abstract base every backend subclasses. It owns the serial, `is_grabbing`
(the hand-off's flag), `trigger_once`, and the one software-trigger `retrieve`
(claim → `_fire_trigger()` → `_fetch(timeout_ms, wants_array,
answers_trigger)`); `retrieve_freerun` is the un-gated `_fetch`. Defaults:
`grab_locked_features` (Basler overrides), `stream_statistics` (`{}`),
`retrieve_external` (= `retrieve_freerun`; the fake overrides it), the
trigger-period/sequence hooks and `last_trigger_index`. pycameleon overrides
`retrieve` only to hold its device lock across fire and receive. A backend
implements open/close, `load_params`/`save_params`, the frame-trigger setup,
`begin_freerun`, the grab start/stop, `_fire_trigger`/`_fetch`, and
`list_features`/`read_feature`/`write_feature`/`execute_command` for the
Camera-tab browser (`Camera.read_param` derives from `read_feature`). Each
backend module declares `SPEC = BackendSpec(...)`; every `enumerate_*` takes
`(requested_serials=None)` and returns `registry.select_serials` order;
`registry.is_auto()` is the one auto/all/empty test.

`cameras/genicam.py` holds what the GenICam backends share: `GenApi` (one SDK's
node adapter under one walker, plus `get`/`put`/`read_feature`/`write_feature`/
`run_command`), `GenICamBackend` (the typed seam `get_node`/`set_node` carrying
the shared trigger chain, the TSV persistence and width/height) and
`NodeMapBackend` (that seam and the browser over a `GenApi`). `basler` is a
`NodeMapBackend` over `_PylonGenApi` persisting to `.pfs`; `flir` and `spinnaker`
are one `FlirBackend` over a `FlirBinding` (`PySpinBinding` in flir.py, the ctypes
`_Spinnaker` in spinnaker_c.py), which owns the System session and the camera
handles it handed out (releasing the System with one outstanding aborts the
process: a libusb `usbi_mutex_destroy` assertion, exit 134); `pycameleon` is a
`GenICamBackend` with six curated nodes (no walk), `fake` one over its node
table. Category labels are display names on every walking backend.

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

**Software-trigger hand-off** (`cameras/_trigger_handoff.py :: SoftwareTrigger`,
composed on every backend as `backend.trigger`): a shared timer thread calling
each device trigger serially let a slow FLIR starve a fast Basler, so
`trigger_once()` only offers a trigger (`trigger.offer`, counted in
`trigger.pending`) and each camera's own `retrieve()` fires the device trigger +
fetches one frame — `trigger_once` is *not* a device call. A fired trigger stays
**outstanding** until an image answers it (`trigger.claim` / `unfired` /
`answered`): while one is outstanding and younger than `ANSWER_TIMEOUT_S` (1 s),
`retrieve` fires nothing and only fetches, so a late image is paired with its own
trigger (firing on regardless labeled every later frame one pulse late). Past
the deadline the trigger is given up (a missed pulse).
`restart_trigger_sequence` bumps an epoch, so a pre-restart answer reads
`PRIMING_TRIGGER`; an image no outstanding trigger accounts for reads
`UNMATCHED_TRIGGER` and is discarded as extra. A GS3 **silently ignores its first
software triggers** after acquisition start too: at the long deadline each cost
1 s, so priming answered nothing and the stale priming trigger blocked the
train's first 37 pulses (rig, 50 fps). So a trigger fired before the grab's
first answer, while no recording counts, gets only `PRIMING_ANSWER_TIMEOUT_S`
(0.1 s, one fetch) and is not reported; after the first answer, or once
counting, a silent trigger may be a late image: the long deadline.
`prime_software_trigger` fires one priming trigger at a time (a burst at the fps
overflowed `PENDING_MAX` while a camera waited out an ignored one). A software
recording tells the hand-off its period (`configure_trigger_period`, set by
`CameraTake.start`, cleared for preview): both deadlines become at least two
periods (at 5 fps a 150 ms exposure outlasted the 0.1 s window, slipped the
pairing by one and made the last priming image frame 0), and, while counting, a
pending trigger older than max(`STALE_TRIGGER_S` = 20 ms, half a period) is
**dropped, not fired** (fired late it would show a moment nearer another pulse).
Deadlines alone cannot tell a late image from the next trigger's, so two more
guards keep a given-up trigger's late image from answering a later one. (1)
**Camera-clock check**: an image's timestamp minus its trigger's host fire time
(taken at the claim, so a host stall only ever raises it) is a near-constant
offset; a stale image's is lower by at least the gap between the two fires (≥
0.1 s), so one more than `STALE_IMAGE_TOLERANCE_NS` (50 ms) below the median of
the last `OFFSET_WINDOW` answers answers nothing (`stale_images`) and its trigger
keeps waiting. It judges only with ≥ `REFERENCE_MIN_SAMPLES` (8) answers (one
early stall or 128 s glitch in a median of one or two locked the camera out for
the take), and a trigger given up on after an image was rejected for it clears
the reference (that image was probably its own). Backends pass a timestamp only
when it counts ns (FLIR, Spinnaker C, Basler USB — not GigE ticks; pycameleon has
none). (2) **Drain**: the check cannot see a steady one-period shift, which
starts when a late image waits in the buffer while nothing is outstanding (the
loop does not fetch when it has nothing to fire). So for max(`DRAIN_WINDOW_S`,
two periods) after a give-up, an idle loop fetches anyway (a `DRAIN_POLL_MS` poll
via `trigger.fetch_timeout_ms`: a real fetch blocks for its whole timeout and
would make the next trigger stale), and what it gets answers nothing. Counting
start ends the drain and, if priming gave a trigger up, resets the offset
reference. The fake's `fetch_blocks` models a real SDK fetch (not woken by a
trigger offer); without it the fake hid that the drain made the train's first
trigger stale. Every backend's failed fetch answers its trigger (pycameleon: a
payload cameleon rejects raises from `receive_async`; a timeout is
`asyncio.TimeoutError`, distinct from `TimeoutError` before Python 3.11). Under
the software trigger `check_sync` skips the arrival check and records offset 0:
the sequence number makes frame 0 trigger 0 in every camera, and a camera that
merely delivers later must not read as late. `octacam check` never re-derives a
schema-4 recording's start offsets from timing events (`pulse_index` is the word).

## Frame-rate ceilings (test-rig hardware)

FLIR **GS3-U3-41C6NIR** (CMV4000 CMOS, 2048², Mono8):
- Software-triggered frame time = USB transfer (~11 ms) + exposure, **serial** —
  this camera does not overlap them for software FrameStart triggers. So delivered
  fps ≈ 1/(11 ms + exposure): 4 ms→~65, 1 ms→~81, 100 µs→~90. **This is a hard
  camera limit, not a bug.**
- **`TriggerOverlap=ReadOut` is mandatory** — with the default `Off`, a trigger
  fired during the previous frame's readout is silently ignored (~half rate + a
  200 ms stall). Set best-effort by the shared GenICam trigger chain
  (`GenICamBackend._arm_frame_trigger`) on every backend, Basler included.
- To exceed 80 fps: external/hardware trigger (overlaps exposure+transfer → ~90),
  or shorten exposure ≲1 ms. `DeviceLinkThroughputLimit` is maxed at open.
- **One USB3 bus is ~384 MB/s shared** → at 2048² Mono8 (4.19 MB/frame) ~90
  frames/s total per bus. Distribute cameras across host controllers; the
  benchmark flags this as the TRANSFER/HOST bottleneck.

## Config system

Per-rig **`octacam_config.toml`** (parsed tolerantly in `config.py` —
warn-and-default per *field*) plus **one per-camera sensor file**: Basler's native
`.pfs`; for flir, spinnaker and pycameleon the **GenApi persistence TSV** (`.txt`,
`cameras/genicam.py`: `apply_config`/`dump_config`/`parse_config`), so a rig can
switch flir↔spinnaker (PySpin ↔ ctypes) on the same files; `fake` writes the TSV
as `.fake`. `recording_format.PARAM_FILE_EXTENSIONS` lists every suffix (so the
transfer step needs no SDK import); `tests/test_backends.py` keeps it in step.

That per-field tolerance stops at the file level: a config that exists but does
not parse **at all** raises `ConfigError` (rendered by `cli.main` as a one-line
"Config error:" naming the file and offending line, exit 2). Falling back to a
bare `OctacamConfig()` ran the rig on stock defaults (every detected camera, save
dir `./`, no plugins, no `[transfer]`): "octacam ignored my config", not "line 48
is malformed".

**Record settings have one vocabulary.** config.py declares the shared field
types (`ScalarStr`, `FfmpegArgs`, `TriggerSource`, `PreviewTriggerSource`,
`SaveMethod`, `RecordForm`). `RecordConfig` uses them tolerantly (warn and default
per field; the queue and session counts are floored); the live
`RecordingSettings` dataclass uses them strictly through one `TypeAdapter`:
`updated()` validates only the changed fields, so a config value the GUI would
refuse never blocks another edit, and `PUT /api/settings` passes its body
straight to it (422 naming the field). Save-path rules are config.py functions
(`resolve_save_path`, `compose_save_dir`, `normalize_dir`,
`increment_trailing_number`) and `RecordingSettings.with_save_dir` (an explicit
dir, as with `--output`, clears the base/relative split), `next_take` and
`relative_save_dir`; every single-path-segment name check is `config.safe_segment`.

**Recording folder layout.** A recording folder holds only the videos
(`<camera>.mkv`/`.raw`/`.mp4`, `grid.mp4`); the summary, `timestamps.npz`, the
config snapshot and every camera/auxiliary parameter file go into its
`octacam_recording/` subfolder (`recording_format.RECORDING_INFO_DIRNAME`; the
summary's per-camera `file` names stay relative to the recording folder). Older
archives are flat and must keep working forever, so **writers always write the
subfolder and readers accept both**: every read goes through
`recording_format.recording_info_dir` (the subfolder's summary wins over a stale
flat one from an earlier take in the same folder, which is never deleted);
recordings are found with `find_recordings` (`is_recording_dir` tests one
folder; `recording_folder` maps a summary file or the subfolder to its
recording); `config.resolve_config_dir` lets `octacam gui <recording>` relaunch
from the subfolder. `octacam check` searches beneath a path only when it is not
itself a recording. Trap: every recursive walk goes through `walk_folders`, which
never enters `octacam_recording/` — never treat one as a recording or as a folder
of loose videos. recording_format also owns the summary/timestamps schema
(`SCHEMA_VERSION`, build, read, write).

**The recording's config snapshot** (`Take._write_config_snapshot`) makes each
recording's `octacam_recording/` subfolder a relaunchable config dir (camera
parameter files must stay beside the TOML), and `octacam process` transfers it
with the videos. Its rules:
- The rig TOML is re-emitted with the **live** values patched in: the Record tab
  (`config_writer.with_record_settings` ← `RecordingSettings.record_config_values`,
  the inverse of `RecordingSettings.from_config`), plugin tabs
  (`with_plugin_options` ← the `snapshot_options` hook), each camera's View-tab
  `display_transform` (`with_camera_transforms`, only for cameras the config
  already lists — adding one would change which cameras the rig opens), and the
  Process section (`with_process_params`). Each patch writes a key only when its
  value differs from what the config already *loads as*, so an untouched
  recording stays a byte-verbatim copy.
- `directory`/`relative_directory` are **never** patched. The live values are
  resolved (and auto-incremented) paths, and a relaunch must resolve a fresh
  dated folder. The path actually used is in the summary.
- Camera params (`take.export_camera_params`) are exported in `start_recording`
  **before** `start_record`, while the cameras still preview; a node-map read
  would contend with the record grab loops once they run. Measured: ~60 ms per
  Basler, a few ms per FLIR, in parallel. Unsaved Camera-tab edits are included.
- 19 of 24 real pre-fix snapshots had a `[record]` that disagreed with their
  own summary. GUI Save never writes `[record]`/`[[plugins]]`, so a raw copy of
  the rig file is not a record of what ran.

**Never write 4:0:0 H.264.** `-pix_fmt gray` makes libx264/libx265 write
monochrome 4:0:0, which software decoders read fine but NVIDIA's hardware decoder
(NVDEC/VDPAU — VLC's default on the rig) renders as a uniform 128-gray frame.
Videos are full-range `yuv420p` (neutral chroma: software decoders return the
same pixels), and `ffmpeg._playable_pix_fmt` rewrites a configured `gray` to it
at the shared `ffmpeg.output_args` seam (every encoder spelling), which fixes old
configs and every snapshot `octacam process` transcodes from. Odd-sized frames
stay 4:0:0 (4:2:0 can't encode them; libx264 refuses), and an unknown size is
left alone. To test a decode the way VLC does here, use `ffmpeg -hwaccel cuda`.

**Trigger normalization on save:** a GUI "Save" taken while previewing with a
software trigger must not bake `TriggerSource=Software` into the file (a later
external-trigger recording would silently never start). Every backend's
`save_params` restores the camera's original (config) trigger source
(`genicam.normalize_trigger_source`; Basler's `.pfs` through
`_normalize_pfs_triggers`) — keep this parity when adding a backend.

**ROI apply order:** a camera **keeps its ROI until power-cycled**, and SFNC makes
a size node's max `sensor - origin`, so the *previous* session's cropped/offset
ROI clamps the *next* config's `Width`/`Height` (hexaview's `OffsetY=278` made
triggerbox's `Height=2048` out of range at max 1770 and failed the whole rig
init). `apply_config` therefore zeroes the origin whose size node the file sets
(`_clear_roi_offsets`) **before** the loop, and applies the file's own
`OffsetX`/`OffsetY` lines **last** (`_roi_offsets_last`). Both halves are needed:
zeroing alone only works for files listing sizes before origins (`dump_config`'s
CONFIG_NODES order, not vendor-exported/hand-edited TSVs). A refused
`Width`/`Height`/`OffsetX`/`OffsetY` write is logged at **warning** — a camera
silently keeping the previous ROI changes what gets recorded, and the default
CLI level is info. Two invariants this rests on: the typed seam
`GenICamBackend.set_node(name, kind, value)` must raise `BackendError` and
**never leak a raw SDK exception** (and `get_node` returns None for a node it
cannot read) — that is what makes `apply_config`'s skip-and-continue real; a leak
turns one refused value into a dead rig — and the `fake` backend models the ROI
coupling in both directions so wrong-order programming is caught without hardware.

`config_writer._toml_value` must serialize every scalar the loader can produce —
including inline tables (triggerbox's nested `cameras`/`lights` arrays) and
date/datetime scalars (a bare serial number or date-like save dir parses as one).
A `None`-valued field is **omitted** (TOML has no null); the loader restores its
default on read, so an "auto" field like `record.max_nvenc_sessions = None` must
never be written as a literal — the `octacam config` scaffold `model_dump()`s the
whole `RecordConfig`, so any None-defaulting field would otherwise crash the dump.
Configs, snapshots and parameter files are written by `files.atomic_write_text`
(fsynced sibling temp + rename) with the umask's permissions: a `mkstemp` temp
made them 0600, and the transfer step copied that mode onto the share.

## Plugin system

Serial-hardware plugins under `plugins/<name>/`, listed in `plugins._PLUGINS`
(name → "module:Class", imported on first use by `plugin_class(name)`; each class
builds itself with `from_options(options)`), with the legacy
`_ALIASES = {"arduino": "flywheel"}` (old configs keep working). The default
launch loads none; enable via `[[plugins]]` or `--plugin`. No entry-point plugins.

The contract is declared once, on `plugins/base.py :: Plugin` (class fact
`web_dir`, the GUI assets), and nothing probes for hooks. Hooks (all with
defaults): `setup/teardown`, `is_ready/status`, `on_recording_start/stop`,
`on_first_frame`, `default_start_params` (so **headless `octacam record` arms the
board** — a plugin that omits it never arms on the CLI), `on_preview_start/stop`,
`snapshot_options` (the live settings a recording's config snapshot must carry; a
plugin whose tab edits settings the config also holds must implement it, or a
relaunch from the recording behaves differently), `api_router`,
`on_ws_message/on_ws_disconnect`; for the trigger-*generating* plugin
(`generates_trigger = True`, found by `PluginManager.trigger_plugin()`),
`trigger_train` (the exact `{period_ns, count}` the recording arm will emit — the
recording counts frames against it) and `prime_trigger` (sacrificial pulses
before the train). Every hook taking start params gets its own slice
(`params[plugin.name]`, None when absent). `PluginManager.attach()` (from the
controller's constructor and `create_app`) sets each plugin's `controller` and
`broadcast`; `PluginManager._call` logs and isolates a raising hook. Each plugin
adds a WS topic + `/api/<name>/*` REST + a GUI tab.

Every board plugin subclasses `plugins/serial.py :: SerialPlugin` (the CLI picks
them with `isinstance`) over one `SerialLink` (a subclass implements
`_feed(chunk)`). It owns open/identify, USB recovery, firmware provisioning and
flashing, status, and the shared reconnect/firmware/flash routes, and logs under
the plugin's name. Class facts: `firmware` (always a `FirmwareSpec`; `sketch_dir`
is None without a source checkout) and `default_device`, which `octacam flash`
and `doctor` read from the class, plus `reconnect_path`, `state_topic`,
`recover_silent`.

**triggerbox** generalizes the EPFL `common-trigger-circuit` (Arduino Nano
ESP32), in `plugins/triggerbox/`: `protocol.py` (wire format, `PIN_LABELS`, and
`TriggerboxLink`, whose `arm()`/`cancel()`/`wait_done()` own the acks and
serialize arms), `train.py` (`plan_train`, `TrainPlan.warnings()`) and
`plugin.py`. Self-describing wire protocol v2: `0xA5 | ver=2 | len u16 | payload |
xor`; payload = fps, duration_ms, N camera lines `{pin, pulse_us, delay_us}`, and
3 symmetric light channels (`off`/`strobe`/`continuous`/`pulse_train`).
`PIN_LABELS` is the single source of truth, mirrored in `triggerbox.ino` and
asserted equal by a unit test. Server-side **auto strobe duty** sizes the LED
on-time to the longest live camera exposure (`Camera.trigger_window_us()`: delay
+ exposure). A same-fps re-arm while the board runs **keeps the frame clock's
phase and lands on the next frame edge** (staged in `g_pend_*`, committed in
`loop()`; continuing pins carry their level across the edge, dropped pins are
parked LOW), so no output gets an early, late or extra edge — a spec applied
mid-frame could insert a rising edge whenever it lengthens an on-time, and any
stray camera edge is the dark ramp above. Only a changed fps or an arm from idle
restarts the clock at once; the run clock (`duration_ms`, pulse-train t0)
restarts with the new spec. Verified with the camera as the oscilloscope (a
light channel on D13, no camera line). On a wedged USB CDC link the plugin
auto-recovers via a host `USBDEVFS_RESET` bus reset and surfaces the error loudly.
The firmware ends a finite run on a millisecond clock (idle anywhere in the
duration's last ms), so **`plan_train`** models `triggerbox.ino` and returns both
the arm's `duration_ms` and the camera-pulse count it makes the board emit;
`trigger_train` (pure, no camera reads) and the arm share it, so the recording
counts exactly what is emitted. The run ends after every camera line's delay +
pulse and every strobe's last on-time, before the next frame edge (a strobe that
cannot finish is cut and warned about). From ~400 fps (500 µs pulse) a whole-ms
end fits only some counts, so the count may move by up to `MAX_COUNT_SHIFT` from
`round(fps × duration)`; from ~650 fps (and at 500/625/…) no end is exact — the
plan is marked inexact and the arm warns (ending on a pulse count in the firmware
would remove that limit). The reader blocks for one byte then drains
(`plugins/serial.py :: SerialLink._read_chunk`): a fixed `read(64)` delayed every
token by up to the 0.2 s port timeout.

## Arduino firmware & auto-flash

Each sketch's identify banner ends with a **source-hash fingerprint**
(`"<NAME> <ver> <build>"`). The committed `fw_build_info.h` ships an `UNBAKED`
placeholder; octacam bakes the real hash into a *throwaway copy* of the sketch at
flash time (the repo tree is never dirtied). `firmware.py` (`classify`) +
`SerialPlugin` classify a board (CURRENT/OUTDATED/WRONG_BOARD/UNIDENTIFIED/…) and
offer `arduino-cli` flashing under its re-entrant `port_lock`. `octacam flash` and
`octacam record --yes` drive it. Without the sketch source a board is never
called up to date: `flash` reports its build unknown (or incompatible from the
banner) and exits non-zero. `record --yes` never prompts, but flashes unasked only
a board running an old build of its own sketch (`safe_to_auto_flash`). flywheel
uses a backward-compatible identify *sentinel* over the shared reader
(`FlywheelLink`; its wire protocol is frameless, so no forced reflash).

## Web GUI

FastAPI + a vanilla-JS static frontend (`web/static/`). Preview/telemetry/control
share one WebSocket.

**Deferred startup (serve-first).** `octacam gui` binds uvicorn and serves the
page *before* touching hardware, so time-to-first-paint never waits on vendor-SDK
camera enumeration or a trigger-board handshake. The controller is constructed
`ready=False` against a hardware-free `CameraSystem.pending()` placeholder (0
cameras — every consumer, snapshot/preview-loop/`/api/system`, reports an
"initializing" system safely). A daemon init thread then opens the cameras
(`CameraSystem.for_config`) and arms the serial plugins **in parallel**, calls
`controller.attach_system(real)` (atomic reference swap under the GIL — the
preview/telemetry loops re-read `camera_system` each tick, so a reader never sees
a torn state), starts preview, then pushes a fresh `system` WS message. The
frontend renders the shell up front with a grid placeholder and builds the
grid/View+Camera tabs/Save dialog in `buildCameras()` when the camera list
arrives (from the initial `/api/system`, the WS-connect handshake — which sends
`system` too, closing the connect-vs-init race — or the init broadcast). Each
plugin tab's `applyStatus(info)` is its **only** signal that the cameras are now
open, so a tab that reads camera state at construction (e.g. triggerbox's timing
plot pulling `/api/triggerbox/exposures` for its auto strobe duty) must re-read
it there or show placeholder-era data all session. Camera-open failure calls
`controller.fail_init(msg)` (surfaced in the GUI + logged) instead of aborting
the running server. On shutdown the finally sets a `stopping` event and
`join()`s the init thread before `controller.close()`, so arming can't race
teardown. The **adaptive preview** protocol sends a per-client per-camera "view
spec"; the server encodes each distinct on-screen resolution once and shares it
(cost tracks resolutions, not clients), with server-side crop of a zoomed region
(frame header v2). Each **camera** encodes on its own executor task
(`preview._encode_camera`, one `run_in_executor` per camera per tick, on a
by-value `EncodeJob`, so the workers touch no shared state) — `cv2.imencode`
releases the GIL, so a tick costs the slowest camera, not the sum; serializing it
reintroduces a cost linear in rig size that overruns the 33 ms refresh (8× 2048²
focused: 85 ms serial vs 14 ms parallel). Every push goes
through `web/hub.py`: `Hub.publish(type, payload, key=None)` keeps only the newest
message per (type, key) per client; events always queue (`EVENT_BACKLOG_REPLAY`).
Routes are per-area `APIRouter` factories (sync `def` handlers); `create_app`
reads the config dir from `controller.config_dir`, and a GUI config save
is `config_writer.save_rig_config`. The **Camera tab** is a full GenApi
node-map browser (typed widgets, per-field reset, ROI auto-center; nodes writable
only while not grabbing cycle the preview grab). Plugin tabs live in a responsive
overflow menu; theme is a rig config option overridable per-browser.

Frontend: `app.js` wires the page; `connection.js` the initial load, socket, mode
and footer; `tabs.js` the `TabBar` and `loadPluginTabs`; `util.js` the shared
helpers (`el`, `store`, `request`, `Modal`, `api`, `clampInput`). A plugin tab
extends `serial.js::SerialTab` (the shared serial-status block, port picker,
Reconnect and firmware banner; `setConnected`/`applyStatus`/`applyState`/
`getStartParams`), owns its panel markup and static `label`/`title`, and imports
`/js/util.js` and `/js/serial.js` itself; a `<name>_state` push goes to it by name.

**Keyboard shortcuts** live in one place: `web/static/js/shortcuts.js`
(`initShortcuts({grid, tabs})`, wired once in `app.js main()`; digits click
`tabs.order`, the `TabBar`'s stable order; the plugin context is the active tab
button's `data-plugin`). One document-level `keydown` listener runs one binding
table, which also generates the `?` help overlay and the button `title=` hints
(so they can't drift). Invariants: every binding goes through the `suppressed()`
guard (no bare key fires while a text/`select`/contenteditable is focused or a
modal is open), no binding uses bare `Enter`/`Escape`/`Tab` (owned by
field/modal handlers), and actions **click the real control / call the real grid
method** so gating (`disabled`), state-aware labels and confirms are reused.
Recording start/stop is `Ctrl/Cmd+Enter` on purpose (a stray key must never
abort a live trial). Add a `tests/test_frontend.py` case for any new binding.

To **see** GUI changes without the rig: render the real frontend in the cached
Playwright Chromium and read the screenshot (serve `web/static` over
`http.server`, `import()` the real module, call pure methods on the prototype).
Gotchas: a `ResizeObserver` fires once on `observe()`; SVG text in a `viewBox`
scales with the box (draw at real pixel size); force a theme by setting
`data-theme` (not `--force-dark-mode`).

## CLI notes

Eight commands: `gui`, `doctor`, `config` (scaffold a rig interactively),
`record`, `flash`, `benchmark`, `process`, `check` (screen recordings for missed
pulses / desync; read-only), plus the `jobs` and `cache` groups. `doctor` never
opens a camera (safe during a live session). A rig **instance-lock**
(`locks.instance_lock`, raising `RigInUse`), keyed on the resolved config dir,
prevents two octacams owning one rig; doctor names its holder with
`locks.holder`, a `files.flock_held` probe that never takes or blocks the lock.

`process.run(folders, ProcessOptions, reporter)` drives transcode → grid →
transfer. The run side is `process_jobs`: `worker(jd)` yields the run's reporter
(a `NullReporter` outside a detached job) and records done/cancelled/failed, and
`pause_gate` holds a work unit while a gui/record has the cameras.

**`process` never trusts an output that predates its source** (`process.is_stale`).
A folder recorded into twice keeps the previous take's `*.mp4`/`grid.mp4` (the
overwrite confirmation only replaces what the new take writes), which passed as
finished work and were transferred as the new take's. mtime is the signal: an
output made from this source was written after it. The same check rebuilds a
grid older than its videos, and a dry run reports both (the transcode step
records them in `rewritten` so the grid preview plans around them).

**typer gotcha:** typer 0.26 vendors a *forked* click that drops `flag_value`, so
an optional-value option (bare `--flag` vs `--flag X`, e.g. `process --last`) is
implemented with a custom `TyperCommand.parse_args` (`ProcessCommand` /
`_inject_default_last` in `cli/process.py`), not click internals.

## Benchmark / diagnostics

`diagnostics.py` measures a **grab ceiling**, an **encode ceiling**, and an
**end-to-end trial** to name the bottleneck (acquisition / encode / host /
TRANSFER). It **deliberately re-implements the grab loop** (rather than reusing
`CameraTake`'s) to keep the hot record path zero-overhead and to isolate
ceilings — do NOT "simplify" this away. Two intentional no-op seams support it:
the read-only `Camera.backend` property and `AsyncFrameWriter(profile=False)`.
The ceilings share one `_measure_rate` loop. Progress is a seconds budget
(`Progress(phase, detail, elapsed_s, step_s, total_s)`, a fixed slot per phase
that will run) the CLI bar and `diagnose.js` ease through; the report ships
`bottleneck_label`, so both say the same thing. Free-run / transfer numbers are
not yet calibrated on real hardware.

## Hardware quirks & test rig

- Nano ESP32 **D13 = GPIO48 = 3.3 V LVTTL** (not a classic AVR's 5 V). The
  common-trigger-circuit board does **not** buffer the camera trigger: D13 routes
  raw to screw terminal J7-4 (GND on J5-1). Only the 3 CCS light channels have
  transistors.
- **A GS3 trigger pulse must end before its exposure does.** With
  `TriggerOverlap=ReadOut` an input still asserted at exposure end re-triggers
  the camera at once, so a pulse ≥ the exposure makes it free-run at its readout
  limit (7.697 ms at 2048×1408, i.e. 129.9 fps) and ignore the trigger clock —
  measured with a 2.4 ms pulse against a 2.0 ms exposure; 480–640 µs pulses are
  clean. The default `pulse_us` = 500 is fine; don't size camera pulses like
  strobes.
- **A GS3 ignores its first hardware triggers after an acquisition start**, how
  many depending on its state: three 13-pulse trains in one acquisition give 11,
  13, 13 frames (two ignored); waiting 25–327 ms after `BeginAcquisition` changes
  nothing, and `AcquisitionStatus[FrameTriggerWait]` reports ready after 0.2 ms
  (no readiness signal). On the **first acquisition after a power-up** it ignores
  more: both GS3s, replugged 1–2 min earlier, answered none of four priming pulses
  and delivered 149999 of a 150000-pulse train (likely 5 ignored each; the fake
  reproduces the signature). After a preview's pulses it ignored none. Recordings
  prime until every camera answers (above); an `external` source can't be primed,
  so its first pulses produce no frame on a GS3.
- **A GS3 that misses a trigger's rising edge may fire on its falling edge**, one
  pulse width late (+503 µs at 500 µs pulses, +1003 µs at 1000 µs), then catch up
  at its readout limit (7.697 → ~7.8 → 8.0 ms intervals); missing both, it skips
  the pulse (a 16 ms interval). The camera never exposes a skipped pulse — its
  FrameCounter chunk and the SDK FrameID show no gap and `TransmitFrameCount`
  rises by exactly what arrived — so it is the trigger input, not transport. On
  the rig **17475187 ("top") does this ~20–35× more often than 17475185** on the
  same D13 line (per pulse: ~2e-5 misses, ~1e-4 late), independent of fps (100
  vs 125), pulse width, strobe current and host load: its trigger path (cable,
  connector, opto ground, the unbuffered 3.3 V D13 driving two opto inputs) is
  marginal. octacam fills and reports the misses.
- **GS3 timestamps can jump by exactly +128 s**: the 64-bit ns timestamp is
  extended from a counter whose seconds wrap every 128 s, and a frame ~60 µs
  before a wrap came out 128 s ahead (hexaview 260916/Fly9). Not a lost frame —
  the pulse tracker folds it.
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
"why". `design/refactor-plan.md` holds the module ownership map and the "Keep"
list of load-bearing behavior a refactor must preserve.
