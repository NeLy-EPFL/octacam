"""Recording orchestration shared by the web UI and the headless CLI.

A framework-free state machine on top of a CameraSystem:

    preview/idle -> waiting -> recording -> finishing -> preview/idle

Each recording is a :class:`~octacam.take.Take`, which the controller admits and
drives on a monitor thread: it waits for every camera's first frame (plugin
``on_first_frame`` hooks fire then), counts down and tears down in a fixed
order: trigger off -> grab loops exit -> writers drain -> summary.

``RecordingController._lock`` guards the state and settings, and snapshot(),
stop_recording() and the telemetry take it too. So nothing that can block runs
under it: plugin hooks (serial writes awaiting an ack), node-map reads over USB
and the NVENC probe all run off the lock, or one stalled device would wedge the
GUI's status and its Stop button.
"""

import contextlib
import dataclasses
import logging
import os
import shutil
import threading
import time
from collections import deque
from pathlib import Path

from octacam import session_cache
from octacam.cameras import CameraSystem
from octacam.config import RecordingSettings, normalize_dir, safe_segment
from octacam.plugins.base import PluginManager
from octacam.recording_format import RECORDING_INFO_DIRNAME
from octacam.take import (
    DeliveryProfile,
    Take,
    export_camera_params,
    read_delivery_profiles,
)
from octacam.transform import DisplayTransform
from octacam.writer import resolve_capture_formats

log = logging.getLogger("octacam")


class StartResult:
    OK = "ok"
    BUSY = "busy"
    NEEDS_CONFIRM = "needs_confirm"
    ERROR = "error"

    def __init__(self, status: str, message: str = ""):
        self.status = status
        self.message = message

    @property
    def ok(self) -> bool:
        return self.status == self.OK


class RecordingController:
    """Owns the recording state machine on top of a CameraSystem.

    Listeners (the web layer) are called as fn(kind, payload) from
    controller threads: kind "state" on every transition and "event" for
    operator-facing messages (writer failures, warnings).
    """

    def __init__(
        self,
        camera_system: CameraSystem,
        settings: RecordingSettings,
        plugins: PluginManager | None = None,
        auto_preview: bool = True,
        session_id: str | None = None,
        record_kind: str = "gui",
        config_dir: str | Path | None = None,
        ready: bool = True,
    ):
        self.camera_system = camera_system
        # False while the GUI's init thread opens the cameras behind a
        # placeholder system (attach_system).
        self._ready = ready
        self._init_error: str | None = None
        self.plugins = plugins if plugins is not None else PluginManager([])
        self.plugins.attach(controller=self)
        self._settings = settings
        self._auto_preview = auto_preview
        # A config predating "managed" says "external" with a trigger-driving
        # plugin. octacam drives that trigger, so it is managed: the recording is
        # the same, and auto preview drives the plugin instead of free-running.
        if (
            self._settings.trigger_source == "external"
            and self.managed_trigger_available
        ):
            self._settings = dataclasses.replace(self._settings, trigger_source="managed")
            log.info(
                "trigger_source 'external' with a trigger-driving plugin loaded; "
                "treating it as 'managed' (octacam drives the trigger)."
            )
        # The rig's config dir: each recording snapshots its octacam_config.toml
        # and a feature reset reads its parameter files; None (as in tests): neither.
        self.config_dir = Path(config_dir) if config_dir is not None else None
        # Finished recordings are noted in the session cache under this id.
        self._session_id = session_id or session_cache.new_session_id()
        self._record_kind = record_kind
        self._recordings_made = 0
        self._lock = threading.RLock()
        self._state = "idle"
        # Gates that refuse a recording or a benchmark while set (see _busy_reason):
        # a camera reconfiguration is running off the lock;
        self._reconfiguring = False
        # a finished take's off-lock tail (plugin disarm, preview re-arm) still
        # runs over the shared trigger plugin;
        self._tearing_down = False
        # a start is claiming the cameras (see start_recording).
        self._starting = False
        # The current or last recording, and the thread that drives it.
        self._take: Take | None = None
        self._monitor: threading.Thread | None = None
        # Bumped per countdown, so a client that missed the states between two
        # recordings can still tell them apart.
        self._recording_seq = 0
        self._listeners: list = []
        self.events: deque = deque(maxlen=100)
        self._last_diagnostic: dict | None = None
        self._diag_thread: threading.Thread | None = None
        self._diag_cancel = threading.Event()

    # ------------------------------------------------------------ listeners

    def add_listener(self, fn) -> None:
        self._listeners.append(fn)

    def _notify(self, kind: str, payload: dict) -> None:
        for fn in list(self._listeners):
            try:
                fn(kind, payload)
            except Exception:
                log.exception("Controller listener failed")

    def _set_state(self, state: str) -> None:
        self._state = state
        self._notify("state", self.snapshot())

    def _event(self, level: str, message: str) -> None:
        """Log an operator-facing message and send it to the listeners; ``level``
        is ``"info"``, ``"warning"`` or ``"error"``."""
        getattr(log, level)(message)
        entry = {"time": time.time(), "level": level, "message": message}
        self.events.append(entry)
        self._notify("event", entry)

    # ------------------------------------------------------------- settings

    @property
    def state(self) -> str:
        return self._state

    @property
    def ready(self) -> bool:
        """False while the GUI's background init is still opening the cameras."""
        return self._ready

    @property
    def init_error(self) -> str | None:
        """Why the GUI's background camera init failed, or None."""
        return self._init_error

    def attach_system(self, camera_system: CameraSystem) -> None:
        """Swap the hardware-free placeholder for the real, opened system.

        The swap is atomic and the preview and telemetry loops re-read
        ``camera_system`` every tick, so a reader sees the empty placeholder or
        the real system, never a torn state.
        """
        with self._lock:
            self.camera_system = camera_system
            self._ready = True
            self._init_error = None

    def fail_init(self, message: str) -> None:
        """Record why the GUI's background camera init failed (``ready`` stays
        False), for the web UI and the event log."""
        with self._lock:
            self._init_error = message
        self._event("error", message)

    def notify_state(self) -> None:
        """Broadcast the current snapshot to listeners."""
        self._notify("state", self.snapshot())

    @property
    def recording_active(self) -> bool:
        return self._state in ("waiting", "recording", "finishing")

    @property
    def diagnosing(self) -> bool:
        """True while a benchmark (octacam.diagnostics) is running."""
        return self._state == "diagnosing"

    @property
    def _camera_locked(self) -> bool:
        """Device-touching operations are refused while recording, while a
        benchmark drives the cameras itself, and while a start claims them (a
        preview re-arm or grab-cycling write then would hand the cameras a fresh
        trigger clock just before the recording's arm)."""
        return self.recording_active or self.diagnosing or self._starting

    def _require_camera_control(
        self, refusal: str, *, recording_only: bool = False
    ) -> None:
        """Raise ``RuntimeError(refusal)`` while camera control is locked, or, for
        an operation only a recording conflicts with, while recording. Caller
        holds the lock."""
        if self.recording_active if recording_only else self._camera_locked:
            raise RuntimeError(refusal)

    def _busy_reason(self, *, benchmark: bool = False) -> str | None:
        """Why a recording (or a benchmark) cannot claim the cameras now, else
        None. Caller holds the lock."""
        # (busy, the reason a recording is told, a benchmark's if it differs)
        reasons = (
            (self.recording_active, "Recording in progress", None),
            (
                self.diagnosing,
                "A benchmark is in progress",
                "A benchmark is already running",
            ),
            (self._reconfiguring, "Camera reconfiguration in progress", None),
            (self._tearing_down, "Previous recording is still finishing", None),
            (self._starting, "Recording is already starting", "A recording is starting"),
        )
        for busy, reason, benchmark_reason in reasons:
            if busy:
                return (benchmark_reason or reason) if benchmark else reason
        return None

    def get_settings(self) -> RecordingSettings:
        with self._lock:
            return dataclasses.replace(self._settings)

    def update_settings(self, /, **changes) -> RecordingSettings:
        """Apply settings changes (:meth:`RecordingSettings.updated`). A change
        that fails validation is a ValueError in any state; a valid one is
        refused (RuntimeError) while a recording is active or starting."""
        with self._lock:
            settings = self._settings.updated(**changes)
            if self.recording_active:
                raise RuntimeError("Settings are locked while recording")
            if self._starting:
                # A change could re-arm the preview under the starting recording.
                raise RuntimeError("Settings are locked while a recording is starting")
            self._settings = settings
            if "fps" in changes:
                self.camera_system.set_software_trigger_frequency(self._settings.fps)
            # Re-arm the preview when its trigger changes; a software preview
            # takes a new fps live, so only another mode re-arms for one.
            rearm = (
                "trigger_source" in changes or "preview_trigger_source" in changes
            ) or ("fps" in changes and self._effective_preview_mode() != "software")
            arm_mode = (
                self._arm_preview_locked()
                if rearm and self._state == "preview"
                else None
            )
            new_settings = dataclasses.replace(self._settings)
        if arm_mode is not None:
            self._dispatch_preview_arm(arm_mode)
        return new_settings

    def validate_save_dir(self, path_str: str) -> dict:
        resolved = Path(normalize_dir(path_str))
        parent = next((p for p in [resolved, *resolved.parents] if p.exists()), None)
        free_bytes = shutil.disk_usage(parent).free if parent else 0
        return {
            "resolved": str(resolved),
            "exists": resolved.exists(),
            "creatable": parent is not None and os.access(parent, os.W_OK),
            "free_bytes": free_bytes,
        }

    def browse_directory(self, path_str: str = "") -> dict:
        """List the subdirectories of a path on the rig, for the GUI's save
        directory picker.

        A blank path opens at the save directory, and one that does not exist yet
        at its nearest existing ancestor. Hidden directories and recordings'
        ``octacam_recording`` subfolders (never a place to record into) are left
        out.
        """
        raw = (path_str or "").strip() or (self._settings.save_dir or "")
        base = Path(normalize_dir(raw)) if raw.strip() else Path.home()
        current = next(
            (p for p in [base, *base.parents] if p.is_dir()),
            Path(base.anchor or "/"),
        )
        try:
            entries = sorted(
                (
                    child.name
                    for child in current.iterdir()
                    if not child.name.startswith(".")
                    and child.name != RECORDING_INFO_DIRNAME
                    and child.is_dir()
                ),
                key=str.lower,
            )
        except OSError:
            entries = []
        parent = str(current.parent) if current.parent != current else None
        return {
            "path": str(current),
            "parent": parent,
            "writable": os.access(current, os.W_OK),
            "entries": entries,
        }

    # -------------------------------------------------- full device node map

    @staticmethod
    def _features_payload(index: int, camera) -> dict:
        return {
            "index": index,
            "serial": camera.serial_number,
            "width": camera.width,
            "height": camera.height,
            "center_x": camera.center_x,
            "center_y": camera.center_y,
            "features": camera.list_features(),
        }

    def read_camera_features(self, index: int) -> dict:
        """Full node-map descriptors for one camera (lazily fetched by the tab)."""
        with self._lock:
            camera = self.camera_system.camera_at(index)
        return self._features_payload(index, camera)

    def _reconfigure(self, index: int, scope: str, apply) -> dict:
        """Run ``apply(camera)`` on one camera or all, off the lock under
        ``_reconfiguring``; return the touched cameras' refreshed features."""
        with self._lock:
            self._require_camera_control(
                "Camera parameters are locked while recording or benchmarking"
            )
            if self._reconfiguring:
                raise RuntimeError("A camera reconfiguration is already in progress")
            if scope == "all":
                targets = list(enumerate(self.camera_system))
            else:
                targets = [(index, self.camera_system.camera_at(index))]
            self._reconfiguring = True
        try:
            if scope == "all":
                self.camera_system.apply_to_all(apply)
            else:
                apply(targets[0][1])
            updated = [self._features_payload(i, camera) for i, camera in targets]
        finally:
            with self._lock:
                self._reconfiguring = False
        return {"updated": updated}

    def set_camera_feature(
        self, index: int, name: str, value, scope: str = "selected"
    ) -> dict:
        """Write one node-map feature on one camera or all. The whole feature
        list is returned: a write can change other nodes (a ROI resize moves the
        offsets)."""
        return self._reconfigure(index, scope, lambda camera: camera.set_feature(name, value))

    def reset_camera_feature(self, index: int, name: str, scope: str = "selected") -> dict:
        """Reset one feature to its saved-config value (else its factory default)."""
        return self._reconfigure(
            index,
            scope,
            lambda camera: camera.reset_feature(name, self._saved_params(camera)),
        )

    def _saved_params(self, camera) -> str:
        """``camera``'s parameter file text in the config dir ("" without one)."""
        if self.config_dir is None:
            return ""
        path = self.config_dir / f"{camera.serial_number}.{camera.extension}"
        try:
            return path.read_text()
        except OSError:
            return ""

    def execute_camera_command(self, index: int, name: str) -> dict:
        """Execute a command node on one camera; refreshes its feature list."""
        return self._reconfigure(index, "selected", lambda camera: camera.execute_command(name))

    def set_camera_center(
        self, index: int, axis: str, enabled: bool, scope: str = "selected"
    ) -> dict:
        """Toggle ROI auto-centering on an axis for one camera or all."""
        return self._reconfigure(index, scope, lambda camera: camera.set_center(axis, enabled))

    def export_camera_params(self) -> dict[str, str]:
        """Snapshot every camera's parameter text (Basler .pfs / FLIR .txt);
        rejected while recording/benchmarking."""
        with self._lock:
            self._require_camera_control(
                "Cannot save camera parameters while recording or benchmarking"
            )
        return self.camera_system.save_all_params()

    def set_camera_name(self, index: int, name: str) -> dict:
        """Rename one camera, in memory (a GUI save persists it). The name is
        its video's filename, so it must be safe and unique across the rig."""
        clean = safe_segment(name, "camera name")
        with self._lock:
            self._require_camera_control(
                "Camera names are locked while recording", recording_only=True
            )
            camera = self.camera_system.camera_at(index)
            for other_index, other in enumerate(self.camera_system):
                if other_index != index and other.name == clean:
                    raise ValueError(f"Another camera already uses the name {clean!r}")
            camera.name = clean
            return {"index": index, "serial": camera.serial_number, "name": clean}

    def set_camera_transform(
        self, index: int, scale_x: float, scale_y: float, rotation_deg: float
    ) -> dict:
        """Set one camera's display transform, which a "display"-form recording
        bakes in, so what is recorded matches the View tab without a save."""
        with self._lock:
            self._require_camera_control(
                "Camera transforms are locked while recording", recording_only=True
            )
            camera = self.camera_system.camera_at(index)
            camera.display_transform = DisplayTransform.from_scale_rotation(
                scale_x, scale_y, rotation_deg
            )
            return {
                "index": index,
                "serial": camera.serial_number,
                "transform": camera.display_transform.to_dict(),
            }

    # -------------------------------------------------------------- preview

    @property
    def managed_trigger_available(self) -> bool:
        """Whether a loaded plugin can drive the trigger (``managed`` is usable)."""
        return self.plugins.trigger_plugin() is not None

    def _effective_preview_mode(self) -> str:
        """The preview trigger mode: software | free_running | managed.

        ``auto`` mirrors ``trigger_source``; external, or managed without a
        driving plugin, free-runs."""
        pref = self._settings.preview_trigger_source
        if pref == "software":
            return "software"
        if pref == "free_running":
            return "free_running"
        src = self._settings.trigger_source  # auto
        if src == "software":
            return "software"
        if src == "managed" and self.managed_trigger_available:
            return "managed"
        return "free_running"

    def _arm_preview_locked(self) -> str:
        """Arm the cameras for preview and return the mode; caller holds the lock
        and then arms the plugin with :meth:`_dispatch_preview_arm`."""
        mode = self._effective_preview_mode()
        fps = self._settings.fps
        self.camera_system.stop_software_trigger()
        self.camera_system.set_software_trigger_frequency(fps)
        self.camera_system.start_preview(mode, fps)
        if mode == "software":
            self.camera_system.start_software_trigger()
        self._set_state("preview")
        return mode

    def _dispatch_preview_arm(self, mode: str | None) -> None:
        """Arm the driving plugin for a managed preview; any other mode (or None,
        idle) cancels a preview arm that may be running. Off the lock."""
        if mode == "managed":
            self.plugins.on_preview_start(self._preview_arm_params())
        else:
            self.plugins.on_preview_stop()

    def _preview_arm_params(self) -> dict:
        """The recording's arm parameters, so preview strobes as the recording
        will (the plugin makes the preview arm indefinite)."""
        return self.plugins.default_start_params(
            self._settings.fps, self._settings.duration_s
        )

    def start_preview(self) -> None:
        """(Re)start live preview in the resolved preview trigger mode."""
        with self._lock:
            self._require_camera_control(
                "Cannot start preview while recording or benchmarking"
            )
            mode = self._arm_preview_locked()
        self._dispatch_preview_arm(mode)

    # ------------------------------------------------------------ recording

    def start_recording(
        self,
        confirm_overwrite: bool = False,
        plugin_params: dict | None = None,
    ) -> StartResult:
        # Warm the NVENC probe off the lock: the resolve under it reuses the cache.
        pre = self._settings
        if pre.save_method == "nvenc":
            with contextlib.suppress(Exception):
                resolve_capture_formats(
                    pre.video_format(),
                    len(self.camera_system),
                    pre.max_nvenc_sessions,
                )
        # The parameter export (a node-map walk over USB with no timeout) and the
        # delivery profiles are read off the lock while the cameras still
        # preview; skipped when something already owns the cameras (the
        # admission checks below then refuse this start).
        pre_params = profiles = None
        if not self._camera_locked:
            pre_params = export_camera_params(
                self.camera_system, self.config_dir, pre.save_dir
            )
            profiles = read_delivery_profiles(self.camera_system)
        with self._lock:
            busy = self._busy_reason()
            if busy:
                return StartResult(StartResult.BUSY, busy)
            save_dir = Path(self._settings.save_dir)
            if save_dir.exists() and not confirm_overwrite:
                return StartResult(
                    StartResult.NEEDS_CONFIRM,
                    f"Directory already exists: {save_dir}\n\n"
                    "Existing data will be overwritten.",
                )
            try:
                save_dir.mkdir(parents=True, exist_ok=True)
            except OSError as e:
                return StartResult(
                    StartResult.ERROR, f"Could not create directory: {e}"
                )

            # The start is admitted: claim the cameras. A managed preview's
            # trigger arm is canceled before the record grab starts, never
            # superseded by the recording's arm once the cameras grab: a re-arm
            # restarts the board's frame clock at an arbitrary phase, and a camera
            # in overlapped readout then opens the take with a dark ramp (CLAUDE.md,
            # recording pipeline). The preview grab stops here, while pulses still
            # flow, so every grab loop exits within a frame. `_starting` holds off
            # anything that could re-arm the cameras until the record grab runs.
            disarm_preview = (
                self._state == "preview"
                and self._effective_preview_mode() == "managed"
            )
            if disarm_preview:
                self.camera_system.stop()
            self._starting = True
        try:
            if disarm_preview:
                self.plugins.on_preview_stop()
            if profiles is None:  # skipped above; `_starting` now holds the cameras
                profiles = read_delivery_profiles(self.camera_system)
            return self._start_take(plugin_params, pre_params, profiles)
        finally:
            with self._lock:
                self._starting = False

    def _start_take(
        self,
        plugin_params: dict | None,
        pre_params: dict[str, str] | None,
        profiles: dict[str, DeliveryProfile | None],
    ) -> StartResult:
        """Second half of :meth:`start_recording`, under the caller's
        ``_starting`` gate: start the take's cameras under the lock, then its
        start sequence off it. ``pre_params`` is the caller's parameter export,
        or None."""
        resume_mode: str | None = None
        with self._lock:
            take = self._take = Take(
                self.camera_system,
                self._settings,
                self.plugins,
                plugin_params,
                profiles=profiles,
                event=self._event,
                config_dir=self.config_dir,
                session_id=self._session_id,
                record_kind=self._record_kind,
            )
            # Under the lock only in the rare race where the export was skipped.
            if pre_params is None:
                pre_params = export_camera_params(
                    self.camera_system, self.config_dir, take.settings.save_dir
                )
            error = take.start_cameras()
            if error is not None:
                resume_mode = self._resume_preview()
            else:
                take.write_start_files(pre_params)
                self._set_state("waiting")
                self._monitor = threading.Thread(
                    target=self._monitor_loop, args=(take,), daemon=True
                )
                self._monitor.start()
        if error is not None:
            self._dispatch_preview_arm(resume_mode)
            return StartResult(StartResult.ERROR, error)
        take.start_sequence()
        return StartResult(StartResult.OK)

    def _monitor_loop(self, take: Take) -> None:
        """Drive ``take`` from its first frame to its files, then resume the
        preview; on any exception still disarm the trigger plugin and go idle,
        or the controller would stay recording forever."""
        try:
            if take.wait_for_first_frames():
                take.begin_countdown()
                with self._lock:
                    self._recording_seq += 1
                    self._set_state("recording")
                take.countdown()
            take.end_capture()
            with self._lock:
                self._set_state("finishing")
            take.teardown()
            self._recordings_made += 1
            self._finish_take(take)
        except Exception:
            log.exception("Recording monitor crashed; forcing idle")
            with contextlib.suppress(Exception):
                self.plugins.on_recording_stop(take.aborted)
            with self._lock:
                self._tearing_down = False
                self._set_state("idle")
            self._event("error", "Recording monitor crashed (see log); forced idle")

    def _finish_take(self, take: Take) -> None:
        """Advance the save dir, resume the preview and disarm the plugins. The
        state leaves the active set before this off-lock tail, which
        ``_tearing_down`` covers."""
        with self._lock:
            self._tearing_down = True
        try:
            with self._lock:
                aborted = take.aborted
                if not aborted:
                    self._settings = self._settings.next_take()
                # A camera dropped mid-recording can make the re-arm raise: go
                # idle rather than stay "finishing", and still disarm below.
                try:
                    resume_mode = self._resume_preview()
                except Exception:
                    log.exception("Preview re-arm failed after recording; going idle")
                    self._event("error", "Preview could not resume after the recording")
                    self._set_state("idle")
                    resume_mode = None
            take.hooks_done.wait(take.hooks_timeout_s)
            self.plugins.on_recording_stop(aborted)
            # After the recording's cancel, so the record arm is torn down first.
            self._dispatch_preview_arm(resume_mode)
            self._event("info", "Recording aborted" if aborted else "Recording finished")
        finally:
            with self._lock:
                self._tearing_down = False

    def _resume_preview(self) -> str | None:
        """Arm preview (or go idle) after a recording or benchmark; caller holds
        the lock. Returns the mode for :meth:`_dispatch_preview_arm` (None: idle)."""
        if self._auto_preview:
            return self._arm_preview_locked()
        self._set_state("idle")
        return None

    def stop_recording(self, abort: bool = False) -> None:
        """Finish (or abort) the current recording early."""
        with self._lock:
            if not self.recording_active:
                return
            assert self._take is not None
            self._take.stop(abort)

    def join(self, timeout: float | None = None) -> None:
        """Block until the current recording has fully finished."""
        monitor = self._monitor
        if monitor is not None:
            monitor.join(timeout)

    def close(self) -> None:
        self.stop_recording(abort=True)
        self.join()
        # A benchmark drives the cameras itself: it must unwind before they close
        # (the timeout only guards a wedged SDK call).
        self._diag_cancel.set()
        diag = self._diag_thread
        if diag is not None and diag.is_alive():
            diag.join(timeout=30)
        self.camera_system.close()

    # ---------------------------------------------------------- benchmark

    def run_diagnostic(
        self,
        *,
        target_fps: float | None = None,
        duration_s: float = 5.0,
        find_max: bool = True,
        sink: str = "config",
    ) -> StartResult:
        """Start a benchmark (:mod:`octacam.diagnostics`) on its own thread, in
        place of the preview. The report is broadcast as ``"diagnostics"``."""
        with self._lock:
            busy = self._busy_reason(benchmark=True)
            if busy:
                return StartResult(StartResult.BUSY, busy)
            settings = dataclasses.replace(self._settings)
            self._diag_cancel.clear()
            self._set_state("diagnosing")
            self._diag_thread = threading.Thread(
                target=self._diagnostic_loop,
                args=(settings, target_fps, duration_s, find_max, sink),
                name="octacam-benchmark",
                daemon=True,
            )
            self._diag_thread.start()
        return StartResult(StartResult.OK)

    def _diagnostic_loop(
        self, settings, target_fps, duration_s, find_max, sink
    ) -> None:
        from octacam import diagnostics

        try:
            # The benchmark free-runs the cameras: stop the preview and disarm a
            # plugin that would keep pulsing the trigger line.
            self.camera_system.stop_software_trigger()
            self.plugins.on_preview_stop()
            self.camera_system.stop()

            def progress(p) -> None:
                self._notify("diagnostics_progress", p.to_dict())

            report = diagnostics.diagnose(
                self.camera_system,
                settings,
                target_fps=target_fps,
                duration_s=duration_s,
                find_max=find_max,
                sink=sink,
                progress_cb=progress,
                cancel=self._diag_cancel,
            )
            self._last_diagnostic = report.to_dict()
            self._notify("diagnostics", self._last_diagnostic)
            if self._diag_cancel.is_set():
                self._event("info", "Benchmark cancelled")
            elif report.achievable:
                self._event(
                    "info", f"Benchmark: {report.target_fps:g} fps is achievable"
                )
            else:
                self._event(
                    "warning",
                    f"Benchmark: {report.target_fps:g} fps is NOT achievable "
                    f"(bottleneck: {report.bottleneck})",
                )
        except Exception:
            log.exception("Benchmark failed")
            self._event("error", "Benchmark failed (see the log for details)")
        finally:
            # Always leave "diagnosing" (it locks camera control), even when a
            # camera that dropped out makes the preview re-arm raise.
            try:
                with self._lock:
                    resume_mode = self._resume_preview()
            except Exception:
                log.exception("Preview re-arm failed after benchmark; going idle")
                self._event("error", "Preview could not resume after the benchmark")
                with self._lock:
                    self._set_state("idle")
                resume_mode = None
            try:
                self._dispatch_preview_arm(resume_mode)
            except Exception:
                log.exception("Preview plugin re-arm failed after benchmark")

    def cancel_diagnostic(self) -> None:
        """Ask a running benchmark to stop early (no-op if none is running)."""
        self._diag_cancel.set()

    def get_last_diagnostic(self) -> dict | None:
        """The most recent benchmark report (as a dict), or None if none has run."""
        return self._last_diagnostic

    # ---------------------------------------------------------------- status

    def snapshot(self) -> dict:
        with self._lock:
            settings = self._settings
            take = self._take
            remaining_ms = None
            recording_id = None
            if self._state == "recording" and take is not None and take.deadline is not None:
                remaining_ms = max(0, round((take.deadline - time.monotonic()) * 1000))
                recording_id = self._recording_seq
        # The last take's writer failure stays shown until the next take.
        failed = {t.serial for t in take.camera_takes if t.writer.failed} if take else set()
        try:
            free_bytes = shutil.disk_usage(
                next(
                    p
                    for p in [Path(settings.save_dir), *Path(settings.save_dir).parents]
                    if p.exists()
                )
            ).free
        except (StopIteration, OSError):
            free_bytes = 0
        return {
            "state": self._state,
            "ready": self._ready,
            "init_error": self._init_error,
            "remaining_ms": remaining_ms,
            "recording_id": recording_id,
            "recordings_made": self._recordings_made,
            "save_dir": settings.save_dir,
            "disk_free_bytes": free_bytes,
            "settings": dataclasses.asdict(settings),
            "cameras": [
                _camera_status(camera, camera.serial_number in failed)
                for camera in self.camera_system
            ],
        }


def _camera_status(camera, writer_failed: bool) -> dict:
    """One camera's telemetry: the fps readout and its take's live counts (0
    without one: the preview never shows a stale recording's)."""
    take = camera.take
    return {
        "name": camera.name,
        "serial": camera.serial_number,
        "fps": round(camera.frame_for_display.fps, 2),
        "frames": take.frames if take else 0,
        "dropped": take.dropped_count if take else 0,
        "missed": len(take.tracker.missed) if take else 0,
        "writer_failed": writer_failed,
    }
