# octacam readability refactor

Plan of record for making octacam readable and maintainable by humans, without
changing what it does. Branch: `refactor/readability` (from
`feat/recording-layout` at `1a2e052`). The status log at the end tracks progress.

## Why

octacam works and its hard parts (pulse accounting, priming, the software-trigger
pairing, `plan_train`, the controller's lock discipline) are sound and well
tested. The structure around them is not. A review on 2026-10-04 (one core read,
seven area reviews) found six patterns:

1. **God modules and classes.** `cli.py` is 5,164 lines (nine command groups plus
   the whole transcode → grid → transfer engine). `RecordingController` is ~2,000
   lines. `Camera` is ~1,250 lines, about 25 of them copy-returning properties.
   `create_app` is one 600-line closure. `TriggerboxPlugin` is 860 lines.
2. **Contracts declared, then bypassed.** `CameraBackend` is a Protocol, yet the
   core probes five more methods with `getattr`. Plugins declare their hooks
   twice (a Protocol and a base class), and callers still probe four more with
   `hasattr`. The software-trigger claim → fire → fetch → answer sequence is
   copied into all five backends.
3. **Copy-paste that drifted into bugs.** Three camera-open paths in the CLI, two
   FLIR backends over one SDK, four serial links, three progress bars, five
   recording-folder normalizers, four recording walkers, five atomic-write
   helpers.
4. **Concepts with several owners.** Record settings (four models, three
   validators), the summary schema (written in the controller, read ad hoc in
   four modules), save-path rules, safe-filename checks.
5. **Dead code kept alive by its own tests.** The legacy `/params` camera API
   (which still reads every camera over USB on each WebSocket connect),
   `remux_mp4`, `display_vf_filter`, `transfer_destination`, the `camera.py`
   shim, unused routes, and production branches that exist only for tests.
6. **Prose in place of structure.** 8,100 of 30,400 source lines are comments
   or docstrings. The same hardware story is told up to 15 times, and 42 asides
   narrate history.

`pulses.py` already reads the way the rest should.

## Target style

Every change follows these rules. They are the user's, and they apply to all new
code in this repo, not only to this refactor.

- **Concise and direct.** Write the obvious code. Prefer a dataclass with public
  fields to a class of read-only properties, a module-level function to a
  single-use class, and a plain loop to a framework.
- **Compact classes, one owner per concept.** A class does one job. Shared state
  lives with the object whose lifetime it shares; per-recording state lives on a
  per-recording object, not as attributes reset on a long-lived one.
- **No over-engineering.** No abstraction with a single user, no
  extension point nothing extends, no option nothing sets, no compatibility
  shim (octacam is not a published library: delete and update the callers).
- **Contracts are declared, not probed.** If the core calls it, the base class
  declares it, with a default when it is optional. No `getattr(obj, "method",
  None)` or `hasattr` on objects whose type octacam controls.
- **No production code for tests.** No branch, parameter, re-export or
  tolerant read that exists only so a test double can pass. Tests build real
  objects (or full-field fakes) instead.
- **Comments state invariants.** One or two lines saying what must hold and why,
  at the code that enforces it. No history ("used to", "previously", "the
  original", Qt/C++/SeptaCam), no restating the code, no call-site comment that
  repeats the callee's docstring. A hardware story is told once (CLAUDE.md, or
  the module that owns it) and referenced elsewhere. Docstrings: a one-line
  summary, plus only the contract a caller could get wrong. Target ≤ 15% of a
  module's lines.
- **American English** in new text. Do not mass-rewrite existing British
  spelling unless the line is being edited anyway.

## Ground rules for every task

- **Preserve behavior.** This is a refactor. The only intended behavior changes
  are the Phase 0 bug fixes and the removal of the listed dead code. If you find
  another bug, report it; fix it only if it is trivial and in your files, as a
  separate `fix:` commit with a test.
- **Keep the load-bearing knowledge.** See "Keep" below. Condense its wording,
  never its substance.
- **Stay in your lane.** Edit only the files your task owns. If you need a change
  elsewhere, make the smallest one and say so in your report.
- **Python ≥ 3.10.** A name imported under `if TYPE_CHECKING:` must be quoted in
  any annotation Python evaluates (`tests/test_typing_hygiene.py` checks).
- **Tests.** Update tests that reference moved or renamed internals. When a test
  asserted on internal structure, rewrite it against behavior or the new public
  function. Never weaken an assertion to make it pass. Delete the tests of
  deleted dead code.
- **Gates before you finish** (run from your worktree):
  - `uv run pytest -q -o addopts="" -p no:cacheprovider` — no new failures
    against the baseline below;
  - `uv run ruff check src/` — clean;
  - `uv run pyright src/` — no more errors than at the start of your task;
  - if you touched JS, HTML or CSS: `uv sync --group frontend` once, then
    `uv run --group frontend pytest tests/test_frontend.py -o addopts=""`.
- **Commits.** Small and logical, one concern each, conventional prefix
  (`refactor:`, `fix:`, `test:`, `docs:`), a body saying why, ending with
  `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`. Never push.
- **Do not edit `CLAUDE.md`, `CHANGELOG.md` or this plan.** List the updates they
  need in your report; the orchestrator applies them per phase.

Baseline at `1a2e052`, main venv (has PySpin and Playwright), excluding
`tests/test_frontend.py`: 1,424 tests, all passing except
`test_doctor_report_order_is_deterministic`, a known dev-rig flake (real USB
contention); ruff clean; pyright 41 errors.

## Target ownership

None of this adds layers: each new module is code moved out of a file that does
five jobs, and the class count goes down.

| Concept | Today | Owner after the refactor |
|---|---|---|
| One recording (start sequence, priming, monitor, teardown, sync check, artifacts) | ~12 attributes and ~1,000 lines on `RecordingController` | `take.py: Take`; the controller keeps the state machine, admission gates and preview arming |
| One camera's recording (record loop, fills, rows, stats) | `Camera` internals and ~25 properties | `cameras/take.py: CameraTake` with a `CameraStats` dataclass; the writer owns its overload policy |
| Software-trigger hand-off | a mixin plus five copied `retrieve()` bodies | a composed `SoftwareTrigger` and one `retrieve` in the backend base; backends implement `_fire_trigger()` and `_fetch()` |
| Backend contract | Protocol plus `getattr` probes | a small base class with defaults; one GenICam node-map walker and trigger chain; one FLIR backend over two SDK bindings |
| Plugin contract | Protocol, base class and `hasattr` probes | one `Plugin` class; `SerialPlugin` base for serial hardware; triggerbox as protocol / train / plugin |
| Record settings | four models, three validators | one validated `RecordingSettings` (pydantic `TypeAdapter`), built from config by `RecordingSettings.from_config` |
| Save paths, safe names | config, controller, cli | `config.py` |
| Recording on disk: layout, summary and timestamps schema, discovery | controller, transform, check, cli, transfer | `recording_format.py` |
| Atomic writes, flock liveness, partial temp files | five copies | `files.py` |
| ffmpeg toolchain / capture writers / offline transcode | `writer.py` | `ffmpeg.py` / `writer.py` / `transcode.py` |
| Post-recording pipeline (transcode → grid → transfer) | `cli.py` | `process.py` |
| CLI | one 5,164-line file | `cli/` package, one module per command group, thin |
| Web | a 600-line closure | `web/app.py` (assembly), `hub.py`, `preview.py`, per-area routers |

## Phases

Tasks in the same wave own disjoint files and run in parallel, each in its own
worktree (`.worktrees/<task>`, branch `refactor/<task>`). The orchestrator
rebases each finished branch onto `refactor/readability`, fast-forwards, and runs
the gates in the main venv before the next wave. Hardware checkpoints are run on
the rig by the orchestrator.

### Phase 0: safety net and bug fixes

**P0.1 Test harness.** Add `tests/conftest.py`:
- Session environment defaults before octacam is imported: `PYLON_CAMEMU`,
  `OCTACAM_FAKE_CAMERAS`, `OCTACAM_NO_UPDATE_CHECK=1`.
- Per test: a temporary `OCTACAM_CACHE_DIR`, and a snapshot and restore of the
  `octacam` logger (so plain `caplog` works).
- Autouse stubs so no test can run arduino-cli or reset a USB device.
- A `wait_until(predicate, timeout)` helper (in `tests/helpers.py`).
- `tests/test_frontend.py` is collected only when named on the command line.
- Doctor tests never enumerate real USB devices (removes the dev-rig flake).

Remove what this replaces: the per-file `os.environ` lines, the 13 log-capture
helpers, the per-file cache-dir and no-flash fixtures, the `_restore_octacam_logger`
fixture in `test_cli.py`, and the `CI`/`PYTEST_CURRENT_TEST` sniff in
`web/app.py`. Owns: `tests/**`, that one line in `web/app.py`. Acceptance: the
full suite passes in a fresh worktree venv and in the main venv; a second run
with the test files in reverse order also passes.

**P0.2 Safety-net tests** (new tests only). End-to-end `octacam record` through
`CliRunner` on a temporary fake-backend config: happy path (exit 0, summary
`completed`, every camera's frames equal the train's count, videos exist),
incomplete rig with `--force`, existing save directory non-interactively, and the
non-zero exit when a camera recorded nothing. Web: `POST /api/recording/stop` and
`/abort` during a recording, `/api/diagnostics/cancel`, `GET /api/serial/ports`,
`/api/system.missing_cameras`. Controller: a stalled camera-parameter export or a
blocking plugin start hook leaves `snapshot()` and `stop_recording()` responsive.
Owns: new test files, additions to `tests/test_web.py`. If a test exposes a bug,
mark it `xfail` with the reason and report it.

**P0.3 Bug fixes**, one commit and one regression test each:
1. The rig instance lock hashes an unresolved path in `octacam flash`, so its
   "board may be armed" guard never fires for a relative config dir. Resolve
   inside `_instance_lock_path` (and the holder lookup).
2. `octacam record` leaves the cameras open when the overwrite prompt is declined
   or `load_config`/`apply_display_config` fails. Close on every exit path.
3. `BaslerBackend.set_trigger_source` calls `self.raw.IsOpen()` and raises
   `AttributeError` on a closed camera. Use `self.is_open()`; check every backend
   for the same pattern.
4. The GUI never shows `/api/system.missing_cameras`: an incomplete rig looks like
   a healthy one with a smaller grid. Show a persistent warning naming each missing
   camera and its reason.
5. `flir._begin_acquisition` aborts the start when AcquisitionMode or the buffer
   mode cannot be set; `spinnaker_c` treats both as best-effort. Make `flir`
   best-effort too (only `BeginAcquisition` is fatal).
6. The Benchmark tab says "queue peak N of 20"; the queue is 64. Carry the real
   writer queue size in the report (the benchmark uses
   `settings.writer_queue_size`, like the record path), import
   `GRAB_TIMEOUT_MS`/`WRITER_QUEUE_SIZE` from `cameras.base`, and report the
   resolved encoder args (an NVENC benchmark currently reports the CPU ones).

Owns: `cli.py` (lock, `record`), `cameras/basler.py`, `cameras/flir.py`,
`diagnostics.py`, `web/static/**`, and their tests. Hardware checkpoint after
merge: Basler and FLIR preview and a short record.

Order: P0.1, then P0.2 ∥ P0.3.

### Phase 1: deletions and prose

**P1.0 Dead code** (one task, one commit per item; check src, JS, docs, configs
and the cluster branch's API before deleting):
- The `/params` camera API: the three routes and their models, the controller's
  `read_camera_params`/`set_camera_param`/`reset_camera_params`/`_param_payload`,
  the `camera_params` WebSocket message, `applyParams` in `camera.js`, the `params`
  field of `/api/system` and the stale-descriptor guard it forced
  (`text_seq`/`queue_text_if_current`), and whatever on `Camera` is then unused.
  `_read_delivery_profiles` and `set_geometry` must keep working.
- `remux_mp4`; `display_vf_filter` and the `vf=` plumbing; `default_save_method`;
  `DEFAULT_X264_PARAMS`.
- `transfer_destination`; the unreachable `checksum=` repair mode;
  `files_only=None`; `TransferResult.__bool__`.
- Unused routes: `GET /api/config/configs`, `GET /api/diagnostics/last`, the
  triggerbox and twophoton `GET /status`. Keep `/api/system`, `/api/state`,
  `GET`/`PUT /api/settings` and `/api/recording/*` (the cluster branch uses them).
- triggerbox: the v1 duty override, the `on_preview_start` no-slice fallback,
  `trigger_train`'s `fps` key, the `_fw_spec`/`_fw_needed_build` forwarders.
  `FirmwareCheck` fields only a test reads. The `serial = None` fallbacks and
  `_serial_module()` (pyserial is a hard dependency). `resolve_device`'s unused
  `baud`.
- The `camera.py` shim, `Camera._camera`, `registry.REAL_BACKENDS`,
  `flir.TRIGGER_READY_TIMEOUT_MS`, the `extension` element of `select_backend`'s
  return, `spinnaker_c`'s identical re-raise and `_cam_string`.
- Test-only seams: `session_cache`'s `retention_days` parameters, the controller's
  `config_dir=None`/`session_id=None` branches, `getattr(camera_system, "missing",
  {})` in `web/app.py`.

**P1.A–F Prose and local cleanup**, in parallel. Each task trims prose to the
target style and makes local simplifications only: no moves between modules, no
change to a signature another module uses. Separate commits for prose and code.

| Task | Owns (src) | Owns (tests) |
|---|---|---|
| P1.A | `cli.py` | `test_cli`, `test_cache_cli`, `test_process_detach` |
| P1.B | `web/**` | `test_web`, `test_frontend` |
| P1.C | `cameras/**` | `test_backends`, `test_camera`, `test_cascade`, `test_fake_backend`, `test_flir_backend`, `test_pycameleon_backend`, `test_spinnaker_backend` |
| P1.D | `plugins/**` (not `*/web`), `firmware.py`, `serial_ports.py` | `test_plugins`, `test_triggerbox_plugin`, `test_twophoton_plugin`, `test_flywheel_plugin`, `test_firmware`, `test_serial_ports` |
| P1.E | `writer.py`, `diagnostics.py`, `grid.py`, `transfer.py` | `test_writer`, `test_nvenc`, `test_transcode`, `test_diagnostics`, `test_grid`, `test_transfer` |
| P1.F | `controller.py`, `pulses.py`, `config.py`, `config_writer.py`, `check.py`, `session_cache.py`, `process_jobs.py`, `transform.py`, `updates.py`, `trigger.py`, `_compat.py`, `__main__.py`, `__init__.py` | `test_controller*`, `test_pulse*`, `test_config*`, `test_check`, `test_session_cache`, `test_process_jobs`, `test_transform`, `test_updates`, `test_preview_trigger_source`, `test_recording_layout` |

P1.B also owns `plugins/*/web/**`. Examples of local cleanup: cli's three progress
bars become one; web routes share one error-mapping context manager; the
controller's camera-operation guards become one helper; `getattr` on objects of
known type goes. `updates.py` stays (decision 2026-10-04); tidy it only.

### Phase 2: contracts

**P2.A Plugin contract.**
- One `Plugin` class:
  - delete the Protocol;
  - class attributes `generates_trigger` (replaces `drives_preview_trigger()`) and `web_dir`;
  - `from_options` replaces the `@register` factories;
  - the registry becomes `{name: "module:Class"}` plus `plugin_class(name)`; keep the `arduino` alias.
- `PluginManager.attach(controller, broadcast)` is called by `RecordingController.__init__` and `create_app`, and replaces the duck-typed `set_controller`/`set_broadcast`.
- Each hook receives its own slice (`params.get(plugin.name)`).
- No `hasattr`/`getattr` on plugins anywhere. Plugin facts come from the class: the CLI's `_firmware_spec_for` and `_plugin_default_device`, and `serial_ports.SERIAL_PLUGINS`/`EXPECTED_BANNER`.

Owns: `plugins/**`, the plugin call sites in `controller.py`, `web/app.py`, `cli.py`, `serial_ports.py`.

**P2.B Backend contract.**
- `CameraBackend` becomes a small base class with defaults:
  - `stream_statistics() -> {}`;
  - `grab_locked_features`;
  - the trigger-period and sequence hooks;
  - `last_trigger_index`.
- The hand-off becomes a composed `SoftwareTrigger` with a public `pending`.
- One `retrieve` template in the base; backends implement `_fire_trigger()` and `_fetch(timeout_ms, wants_array, answers_trigger)`, and pycameleon keeps its device lock.
- Remove every `getattr` probe in `Camera`, `CameraSystem` and `diagnostics`.
- Shared helpers:
  - `select_serials(detected, requested)`, with the not-found warning only in `CameraSystem`;
  - a registry spec table plus `is_auto()`, used at the CLI's seven sites;
  - one `IncompleteLog` for flir/spinnaker_c;
  - one `INT_PARAMS`.
- Type Basler's handle as `Any` (pyright −21).

Owns: `cameras/**`, the probe sites in `diagnostics.py`, the `is_auto`/enumeration sites in `cli.py`.

**P2.C Settings and paths.**
- Shared annotated types in `config.py` (`ScalarStr`, `FfmpegArgs`, `TriggerSource`, `SaveMethod`).
- `RecordingSettings` validated by a `TypeAdapter`: `update_settings` shrinks to the unknown-key check plus one validation, and `SettingsPatch` goes.
- `RecordingSettings.from_config` replaces `cli._settings_from_record`, next to `record_config_values`.
- Save-path rules live in `config.py`: resolve, compose, the next take's increment, an explicit `--output`.
- One `safe_segment(name, what)` replaces three copies.
- The config validator boilerplate collapses.
- Do not rename `record_form`/`save_frame_timestamps` (optional follow-up; the summary key stays).

Owns: `config.py`, `config_writer.py` (safe names), settings and path code in `controller.py`, `web/app.py`, `cli.py`.

Order: P2.A ∥ P2.B, then P2.C. Hardware checkpoint after P2.B: every backend on
the rig (Basler, FLIR on PySpin and on ctypes) previews, records and opens the
Camera tab.

### Phase 3: ownership moves

**P3.C Writer split.**
- `ffmpeg.py`:
  - binary discovery;
  - encoder and NVENC probes (`functools.cache`);
  - one option-skipping helper;
  - the 4:2:0/full-range policy (`output_args`, `rawvideo_input_args`, a public `is_limited_range_yuv`);
  - a `quiet_argv` helper so `-nostdin` cannot be forgotten;
  - the doctor's ffmpeg helpers, moved from `cli.py`.
- `writer.py`: the capture writers, `VideoFormat`/`FORMATS`, `resolve_capture_formats`.
- `transcode.py`: one `transcode_file`, `run_ffmpeg` with progress, and atomic partial outputs.
- `grid.py`:
  - uses the public API;
  - `build_grid_video` splits into probe / filtergraph / command.

Owns: those modules, plus import updates elsewhere.

**P3.D Web split.**
- Modules: `web/app.py` (assembly), `hub.py` (clients, publish), `preview.py` (view specs, crop, encode, preview loop), and per-area `APIRouter` factories.
- Logic moves out of the routes:
  - `save_config`'s work goes to `config_writer.save_rig_config`;
  - the reset routes' parameter-file reading goes to the controller.
- Plugin asset discovery collapses to one comprehension.

Owns: `web/*.py`, `tests/test_web.py`.

**P3.A1 Pipeline out of the CLI.**
- `process.py`: `ProcessOptions`, the transcode / grid / transfer phases, one config cache per run.
- `process_jobs` gains:
  - a worker context manager, replacing the exit-code ladder;
  - a `NullReporter`;
  - `_pause_gate`.
- `cli process`/`check` become thin.
- `_rebuild_process_argv` is derived from `ProcessOptions`.

**P3.B `recording_format.py` and `files.py`.**
- `recording_format.py` owns:
  - the recording filenames;
  - `recording_info_dir`;
  - one `recording_folder(path)` replacing five normalizers;
  - one `find_recordings` replacing three walkers (the CLI keeps its own warnings and ordering);
  - the summary and timestamps schema: build, read and write, with `SCHEMA_VERSION`, `SUMMARY_INDEX_LIMIT` and the notes.
- `files.py`: atomic text write (with fsync), flock liveness, partial-temp naming.
- `session_cache`'s marker scans become one.

**P3.A2 `cli/` package.**
- Split what is left of `cli.py` into:
  - `cli/__init__.py`: app, main, logging;
  - `gui`, `doctor`, `wizard`, `record`, `flash`, `benchmark`, `process`;
  - `admin` (jobs and cache).
- `CameraSystem.for_config` becomes the one rig-open path (gui, record, benchmark), closing on any failure.
- `locks.py`: the instance lock as a context manager that normalizes its path.
- The doctor and the wizard enumerate cameras through the registry.

Order: P3.C ∥ P3.D, then P3.A1, then P3.B, then P3.A2.

### Phase 4: class surgery

**P4.A `CameraTake`.**
- `cameras/take.py` holds one camera's recording:
  - the record loop as a short driver;
  - placement and fill helpers;
  - the rows;
  - a `CameraStats` dataclass that replaces the ~25 `Camera` properties.
- The writer owns the overload policy: `write()` reports written / refused / skipped.
- `Camera` keeps the device, parameters, node map and preview.
- The summary reads `CameraStats`, so the tolerant `getattr` reads go. Tests use a full-field stats factory.

**P4.B `Take`.**
- `take.py` holds one recording:
  - clock, start sequence and priming;
  - monitor and countdown;
  - teardown;
  - missed-pulse reporting;
  - delivery profiles;
  - artifacts;
  - `check_sync` as a pure function.
- `RecordingController` keeps:
  - the state machine and one admission check (one table of BUSY reasons);
  - preview arming;
  - one camera-operation guard;
  - settings, snapshot and listeners;
  - the benchmark runner.
- Rewrite the tests that build the controller with `__new__` or inject private state.

**P4.C `SerialPlugin`.**
- `plugins/serial.py`:
  - one serial link (subclasses implement `_feed(chunk)`);
  - open, identify, USB recovery, flash and provisioning (`FirmwareProvisioner` folds in);
  - status, busy reason;
  - the shared reconnect / firmware / flash routes.
- `FirmwareSpec` always exists (`sketch_dir` optional).
- triggerbox splits into `protocol.py`, `train.py` and `plugin.py`, with a pure `_resolve_arm` and a `_send_arm`.
- `Camera.trigger_window_us()` replaces the plugin's own GenICam reads (after P4.A).

**P4.D Diagnostics compaction.**
- One `trial()` helper; one `formats` map.
- `_CamAccum.mark` bookkeeping; one `_measure_rate` loop.
- Progress as a seconds budget, simplified in both renderers.
- Report cleanup.
- `benchmark` calls `diagnose` once.

Owns: `diagnostics.py`, the CLI benchmark module, `diagnose.js`.

**P4.E Frontend.**
- `util.js` gains `el`, `store`, `request` and a `Modal` base.
- A `SerialTab` base:
  - each plugin owns its panel markup;
  - triggerbox reuses `FirmwareFlash`;
  - the plugin list comes from the DOM;
  - no `api` injection.
- `record.js`: one fields table.
- `grid.js`: one effective-transform helper and one drag start.
- `app.js`: `main()` split into tabs and connection modules.

Owns: `web/static/**` except `diagnose.js`, `plugins/*/web/**`.

Order: P4.A ∥ P4.C ∥ P4.D, then P4.B ∥ P4.E.

### Phase 5: hardware-verified unification

**P5.A GenICam unification.**
- `cameras/genicam.py`:
  - one node-map walker over a small per-SDK adapter;
  - the shared trigger chain on the typed seam;
  - the TSV persistence (from `_genicam_config.py`).
- Basler, FLIR, ctypes and the fake use it.
- Basler moves onto the shared chain. That adds best-effort TriggerOverlap and frame-rate-auto writes, so verify on the rig.

**P5.B One FLIR backend.**
- One `FlirBackend` orchestration over two SDK bindings: the existing ctypes `_Spinnaker`, and a new PySpin binding with the same 36-method shape.
- Compact `_Spinnaker` with `_out`/`_try` helpers and a kind table.
- Both registry tiers stay.

**P5.C Final.**
- Full rig run:
  - doctor;
  - GUI preview on every camera;
  - a managed recording on 2 FLIR and the Baslers at 100 fps;
  - `octacam check`.
- Refresh CLAUDE.md (names, commands, test count) and CHANGELOG.

## Decisions

- **Recording layout** was committed on `feat/recording-layout` (`eb2b29c`); this
  branch stacks on it.
- **`feat/cluster-recording`** is left as is and re-ported afterwards. Its
  pieces map onto: `node_router` → a web router factory; `extra_routers` →
  `create_app`'s router list; `ClusterController` → `Take`/controller seams; its
  `max_frames` → `PulseClock` count.
- **`updates.py`** stays; tidy only.
- **Hardware checkpoints** are run by the orchestrator on this rig.
- **Optional follow-ups, not scheduled:** rename `record_form` /
  `save_frame_timestamps` to the config's names.

## Keep

Load-bearing behavior and knowledge. Condense the words, keep the substance.

**Controller and pipeline:**
- Plugin hooks run off the controller lock, behind `hooks_done`; a stop never overtakes the arm.
- Camera control is locked while recording, diagnosing or starting.
- The parameter export and delivery profiles are read off the lock, before `start_record`.
- A managed preview's arm is canceled before the record grab starts.
- Priming runs until every camera answers, within a budget.
- The train end is anchored after the arm returns.
- The completed-train `fill_to`.
- Teardown order: trigger → grab loops → writers → summary.
- The `_tearing_down` gate.
- The monitor-crash guard that forces idle.
- Snapshot rules: the directory templates are never patched; a byte-verbatim copy when nothing changed live.

**Pulses, fills and sync:**
- Per-interval rounding against the measured period.
- 128 s wrap folding.
- The one-sided host check.
- Fill, don't skip, except for sustained writer overload (skip, `sync.ok` false).
- Fill rows are stamped from the tracker clock, never 0.
- `_check_sync` compares only cameras with the same delivery profile; the software trigger records offset 0.

**Software-trigger hand-off:**
- One frame per trigger fired.
- Outstanding triggers are answered oldest-first, under the answer deadlines and the priming deadline.
- Stale-trigger drop.
- The camera-clock stale-image check with its settled reference.
- The drain window.
- The `PRIMING_TRIGGER`/`UNMATCHED_TRIGGER` meanings.
- `PENDING_MAX` drops the newest.
- The fake's `fetch_blocks`.

**Camera and backends:**
- `retrieve()` never raises.
- `TriggerOverlap=ReadOut` on the GS3.
- ROI: zero the origins, apply the offsets last, warn loudly.
- Typed setters raise `BackendError`, never a raw SDK exception.
- `normalize_trigger_source` on save.
- Mono8 guards.
- Pylon:
  - the `tl_factory`/GENICAM_GENTL64_PATH rule;
  - DestroyDevice before PylonTerminate;
  - the non-`with`, non-daemon CreateDevice pool with a deadline;
  - the straggler release.
- FLIR:
  - the 128-buffer rationale;
  - argtypes on 64-bit;
  - CDLL releases the GIL;
  - release every image;
  - release the System last;
  - stride handling.
- pycameleon:
  - exclusive borrow;
  - bounded receive;
  - `asyncio.TimeoutError` before 3.11.
- The fake's two-way ROI coupling and its silent ignore.

**Writer and ffmpeg:**
- Never write 4:0:0 H.264 (`_playable_pix_fmt`); odd sizes stay 4:0:0.
- `-color_range` tags while `out_range=full` converts.
- `-nostdin` with `stdin=DEVNULL` on every launch except the capture pipe.
- The real one-frame encoder probe.
- NVENC `-cq`; the session probe runs off the lock.
- `bufsize=0`.
- Fills ride on the next item.
- Atomic partial temps: real extension last, pid+uuid names, flock liveness, `glob.escape`.

**CLI:**
- fd limit.
- SO_REUSEADDR port check.
- The flock instance lock.
- Close the cameras on any failure after open.
- Capture-active marker only after the cameras attach.
- Shutdown order.
- uvicorn's ws settings.
- Doctor scan: SDK imports on the calling thread, `tl_factory` before the workers.
- `_is_stale` mtime rule.
- Grids never take a grid as input.
- Detached-job argv uses absolute paths.
- `_inject_default_last` (the typer fork).
- `record`:
  - the incomplete-rig gate;
  - `default_start_params`;
  - `controller.close()`;
  - the zero-frame exit.
- The external-trigger benchmark exit rule.

**Web:**
- Sync `def` handlers.
- One executor task per camera encode, by value.
- `_preview_factor`'s HiDPI guard and `ceil`.
- Never merge different crops.
- Newest-only queues.
- Mount order (plugins before the SPA catch-all, no `html=True`).
- WebSocket hooks in the executor.
- Shutdown returns 202.
- Camera-name sanitization.
- Shortcut invariants.
- grid.js pointer capture only past the drag threshold.

**Plugins and firmware:**
- Serial link:
  - `_read_chunk` blocks for one byte, then drains;
  - `_mark_broken`.
- triggerbox:
  - arms serialized;
  - `on_preview_stop` waits for 'C';
  - USB-reset retry never on a reject;
  - `plan_train` mirrors `triggerbox.ino`;
  - `trigger_train` makes no camera reads;
  - `PIN_LABELS` is the single source of truth.
- twophoton's banner and its cancel-on-every-stop.
- flywheel:
  - the identify sentinel;
  - the JogClock generation counter;
  - jog ownership.
- Never auto-flash a blank or foreign board.
- The re-entrant port lock across close → upload → reopen.
- The process-group kill.
- `reset_usb_device`.
- `resolve_device` never guesses.

**Config and recording files:**
- `ConfigError` on an unparseable file.
- `_scalar_str`.
- The `writer_queue_size` floor.
- `resolve_config_dir` rules.
- None values are omitted from TOML.
- Inline tables.
- Both recording layouts are read forever; the subfolder summary wins.
- transfer:
  - temp file → fsync → verify → rename;
  - size-only skip;
  - metadata compared by content.
- check:
  - capped lists are never counted;
  - schema-4 start offsets are never re-derived;
  - an unreadable recording is a problem, not an exception.

## Status log

| Date | Step | Result |
|---|---|---|
| 2026-10-04 | Review, plan | This document |
| 2026-10-04 | P0.1 test harness | `tests/conftest.py` + `helpers.py`; 950 lines of per-file setup gone; doctor tests hermetic; emulated-rig tests pinned to `backend="basler"` (suite 9.5 → 6 min) |
| 2026-10-04 | P0.2 safety net | 17 tests: `octacam record` end to end, take/benchmark/serial/rig routes, controller lock discipline |
| 2026-10-04 | P0.3 bug fixes | the six planned fixes, plus `benchmark` leaving the cameras open |
| 2026-10-04 | Rig checkpoint | 2 Basler + 2 GS3 + triggerbox, 80 fps: 240/240 on every camera. Found and fixed: full-sensor GS3 pairs could not record (128 stream buffers > 1000 MB usbfs; now halved until they fit), abort at exit after a failed FLIR start (`gc.collect()` before teardown), a camera that never started left `sync.ok` true. Fixed a flaky benchmark test (fake cameras starving each other of the GIL) |
| 2026-10-05 | Phase 1 (P1.0, P1.A–F) | Dead code and prose: src 30,432 → 25,210 lines, prose 27% → 15%, pyright 40 → 16. Six area tasks, each reviewed by two independent lenses (behavior, lost knowledge) and fixed: 41 findings, incl. one behavior regression (record's capture marker outside the close/teardown finally). Gate 1,458 passed; rig: managed 4-camera take 240/240, software take matches pre-refactor (full-sensor GS3 software-trigger ceiling) |
| 2026-10-05 | Phase 2 (P2.A–C) | Declared contracts. P2.A: one `Plugin` class (`generates_trigger`, `web_dir`, `firmware`, `default_device`, `from_options`), `PluginManager.attach`/`trigger_plugin`/`_call`, per-plugin slices, registry table, no hasattr probing. P2.B: `CameraBackend` ABC with defaults, composed `SoftwareTrigger` (public API, injected clock, tests in test_trigger_handoff.py), one `retrieve` template (`_fire_trigger`/`_fetch`), `BackendSpec` per module + `is_auto` + `select_serials`, one incomplete-image log. P2.C: shared config field types, `RecordingSettings` moved to config.py and validated by one TypeAdapter (changed fields only), `SettingsPatch` gone, `from_config`/`record_config_values`, save-path rules and one `safe_segment` in config.py. Each task: implement, 3 review lenses, 2 rounds, ~65 findings fixed. Found, not fixed: `octacam flash` without sketch sources says "up to date"; `record --yes` on a TTY still prompts before reflashing |
| 2026-10-05 | Phase 2 gate + rig | Gate 1,545 passed, frontend 57, pyright 11. Rig: managed 4-camera take 240/240 (PySpin + pylon), software take = pre-refactor, ctypes Spinnaker 240/240, pycameleon 40/40 (its Baslers lose the first 4 software triggers — the same on pre-refactor code; fallback tier, not fixed), GUI smoke (Camera-tab walk + write on Basler and FLIR, managed preview over the WebSocket, GUI take 160/160, clean shutdown), doctor --probe-serial and flash --check read firmware facts from the plugin class |
| 2026-10-05 | Phase 3 wave A | P3.C: ffmpeg.py (toolchain, quiet_argv), writer.py capture-only (1,194 → 428), transcode.py (one transcode_file, atomic_output). P3.D: web/app.py 1,122 → 127 lines; hub.py, preview.py, state.py, routers; save_rig_config; feature reset reads the camera's own file. P3.F: the two firmware CLI bugs found in P2.A fixed. Gate 1,577 passed |
