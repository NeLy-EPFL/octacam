"""Recording orchestration shared by the web UI and the headless CLI.

Extracts the start/stop/abort lifecycle that previously lived in the Qt
MainWindow (timer-driven) and cli.py (sleep-driven) into a framework-free
state machine:

    preview/idle -> waiting -> recording -> finishing -> preview/idle

A monitor thread replaces the Qt timers: it polls for the first frame on
every camera (dispatching plugin hooks at that moment — e.g. a flywheel
stepper command), enforces the recording deadline, and runs the teardown
sequence in the same order as the original code (stop trigger -> grab loops
exit -> writers drain -> CSVs).
"""

import contextlib
import dataclasses
import datetime
import json
import logging
import os
import re
import shlex
import shutil
import threading
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path

from octacam import config_writer
from octacam.camera import GEOMETRY_PARAMS, PARAM_NODES, CameraSystem
from octacam.plugins.base import PluginManager
from octacam.transform import RECORDING_SUMMARY_FILENAME, DisplayTransform
from octacam.writer import (
    DEFAULT_FFMPEG_PARAMS,
    DEFAULT_TRANSCODE_FFMPEG_PARAMS,
    FORMATS,
    VideoFormat,
)

log = logging.getLogger("octacam")

STARTED_POLL_INTERVAL_S = 0.1
STARTED_WARN_AFTER_S = 3.0
STARTED_FAIL_AFTER_S = 10.0  # then record with whatever cameras started
STOP_GRACE_S = 0.5  # matches cli.py's in-flight frame grace period
# Upper bound on how long the monitor waits for the off-lock on_recording_start
# hooks to finish before firing on_first_frame / on_recording_stop. Bounds a
# wedged start hook (e.g. a plugin serial write stalled on its write_timeout) so
# it can never block recording teardown indefinitely.
START_HOOKS_TIMEOUT_S = 5.0

_TRAILING_NUMBER_RE = re.compile(r"\d{3}")


def increment_trailing_number(text: str) -> str:
    """Increment the last 3-digit group: 001-bhv -> 002-bhv (else unchanged)."""
    matches = list(_TRAILING_NUMBER_RE.finditer(text))
    if not matches:
        return text
    last = matches[-1]
    incremented = f"{int(last.group()) + 1:03d}"
    return text[: last.start()] + incremented + text[last.end() :]


def normalize_save_dir(text: str) -> str:
    """Mirror DirectoryEdit's normalization: strip, expand ~, absolute, /."""
    path = Path(text.strip()).expanduser()
    return str(path.absolute()).replace("\\", "/")


def compose_save_dir(record_directory: str, relative_directory: str) -> str:
    """Join a base directory and relative sub-path into a normalized save_dir.

    Mirrors config.resolve_save_dir's join semantics (an absolute
    relative_directory discards the base) so the live-edited GUI values and the
    config-resolved ones land on the same path."""
    rel = relative_directory.strip()
    combined = os.path.join(record_directory, rel) if rel else record_directory
    return normalize_save_dir(combined)


def sanitize_camera_name(name: str) -> str:
    """Validate a camera name as a safe, single-segment video filename stem.

    ``camera.name`` becomes the per-camera output filename (CameraSystem.
    start_record writes ``<name>.<ext>``), so a name must be non-blank and
    contain no path separators or ``.``/``..`` traversal. Mirrors
    config_writer.safe_config_name, kept separate to give a camera-specific
    error message.
    """
    clean = (name or "").strip()
    if (
        not clean
        or clean in (".", "..")
        or "/" in clean
        or "\\" in clean
        or os.sep in clean
        or (os.altsep and os.altsep in clean)
        or Path(clean).name != clean
    ):
        raise ValueError(f"Invalid camera name: {name!r}")
    return clean


@dataclass
class RecordingSettings:
    fps: float = 100.0
    duration_s: float = 20.0
    save_dir: str = "./"
    # Resolved base directory (config record.directory) the save_dir sits under,
    # and the relative sub-path (config record.relative_directory) under it. When
    # either is edited, save_dir is recomposed as record_directory/
    # relative_directory. relative_directory is what the transfer step mirrors
    # onto the NAS; empty falls back to the save_dir's own basename.
    record_directory: str = ""
    relative_directory: str = ""
    trigger_source: str = "software"  # "software" | "external"
    save_method: str = "ffmpeg"  # "ffmpeg" | "raw"
    # Verbatim ffmpeg output/encoder args used when save_method == "ffmpeg".
    ffmpeg_params: str = DEFAULT_FFMPEG_PARAMS
    remux_mp4: bool = False
    # "display" bakes each camera's display transform into the video; "sensor"
    # saves the raw, untransformed image. save_frame_timestamps re-enables the
    # per-frame timestamp CSV (debugging; off by default).
    record_form: str = "display"
    save_frame_timestamps: bool = False
    # Post-recording (`octacam process`) params. Not used during capture; they
    # are patched into the recording folder's octacam_config.toml snapshot
    # (_snapshot_config) so a later `octacam process` transcodes and transfers
    # with exactly the values shown in the GUI. Sourced from the config's
    # [transcode]/[transfer] sections at startup; empty transfer_directory
    # disables the transfer step.
    transcode_ffmpeg_params: str = DEFAULT_TRANSCODE_FFMPEG_PARAMS
    transfer_directory: str = ""
    transfer_checksum: bool = True

    def video_format(self) -> VideoFormat:
        video_format = FORMATS[self.save_method]
        if self.save_method == "ffmpeg":
            video_format = dataclasses.replace(
                video_format,
                ffmpeg_params=self.ffmpeg_params,
                remux_mp4=self.remux_mp4,
            )
        return video_format


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


_DROPPED_FRAMES_NOTE = (
    "`dropped` counts only frames the encoder/writer queue could not accept "
    "(the host could not keep up). Frames the camera or transport never "
    "delivered (e.g. USB bandwidth gaps) are NOT detected here; enable "
    "save_frame_timestamps and inspect the inter-frame timestamp gaps to "
    "investigate those."
)


def build_recording_summary(
    settings: RecordingSettings,
    cameras,
    start_wall_ns: int,
    aborted: bool,
) -> dict:
    """Assemble the recording_summary.json payload from finalized camera stats.

    Pure (no I/O) so it can be unit-tested without a recording. Each camera's
    ``transform`` is always recorded (so `octacam transcode --as-displayed` can
    apply it later); ``transform_applied`` is true only when it was baked into
    the saved file (display form + non-identity transform)."""
    extension = settings.video_format().extension
    start_iso = (
        datetime.datetime.fromtimestamp(
            start_wall_ns / 1e9, tz=datetime.UTC
        ).isoformat()
        if start_wall_ns
        else None
    )
    cams = []
    for camera in cameras:
        transform = camera.display_transform
        applied = settings.record_form == "display" and not transform.is_identity
        size = camera.recorded_frame_size
        cams.append(
            {
                "name": camera.name,
                "serial": camera.serial_number,
                "file": f"{camera.name}.{extension}",
                "width": size[0] if size else None,
                "height": size[1] if size else None,
                "pixel_format": camera.pixel_format,
                "fps": round(camera.mean_fps, 3),
                "frames": camera.frames_recorded,
                "dropped": camera.dropped_count,
                "dropped_indices": camera.dropped_indices,
                "start_timestamp_ns": camera.start_timestamp_ns,
                "writer_failed": camera.writer_failed,
                "transform": transform.to_dict(),
                "transform_applied": applied,
            }
        )
    return {
        "schema_version": 2,
        "start_time": start_iso,
        "start_time_ns": start_wall_ns or None,
        "aborted": aborted,
        "fps_target": settings.fps,
        "duration_s": settings.duration_s,
        "trigger_source": settings.trigger_source,
        "save_method": settings.save_method,
        "ffmpeg_params": settings.ffmpeg_params,
        "record_form": settings.record_form,
        # The sub-path (under record.directory) the transfer step mirrors onto
        # the NAS; resolved once here so a later transfer never re-templates the
        # date on a different day.
        "relative_directory": _relative_directory(settings),
        "dropped_frames_note": _DROPPED_FRAMES_NOTE,
        "cameras": cams,
    }


def _relative_directory(settings: RecordingSettings) -> str:
    """The recording folder's path relative to the configured base directory.

    Prefers the explicit ``relative_directory`` (the value save_dir was composed
    from), then falls back to computing it from save_dir, and finally to the
    folder's own basename when no base is known or the folder lives outside it
    (e.g. an ad-hoc --output override)."""
    if settings.relative_directory.strip():
        return settings.relative_directory
    base = settings.record_directory
    if base:
        try:
            rel = os.path.relpath(settings.save_dir, base)
            if not rel.startswith(".."):
                return rel
        except ValueError:  # e.g. different drives on Windows
            pass
    return Path(settings.save_dir).name


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
    ):
        self.camera_system = camera_system
        self.plugins = plugins if plugins is not None else PluginManager([])
        self._settings = settings
        self._auto_preview = auto_preview
        # The rig config dir whose octacam_config.toml is copied into each
        # recording folder (so `octacam process` needs no --config later). None
        # skips the snapshot (e.g. unit tests constructing a controller directly).
        self._config_dir = Path(config_dir) if config_dir is not None else None
        # When set, each finished recording's folder is noted in the session
        # cache (octacam.session_cache) under this id so `octacam transcode
        # --last/--session/--all` can find it later. None disables the cache
        # (e.g. in unit tests that construct a controller directly).
        self._session_id = session_id
        self._record_kind = record_kind
        self._lock = threading.RLock()
        self._state = "idle"
        # True while a camera's geometry is being changed (preview stopped and
        # restarted off-lock); blocks a recording from starting mid-cycle.
        self._reconfiguring = False
        self._aborted = False
        self._stop_event = threading.Event()
        # Set once the off-lock on_recording_start plugin hooks have finished
        # dispatching. The monitor waits on it before on_first_frame and
        # on_recording_stop, so the start -> first_frame -> stop hook order holds
        # even though the arm runs on the caller thread — closing the race where
        # an abort's cancel could overtake a not-yet-sent hardware arm.
        self._start_hooks_done = threading.Event()
        self._monitor: threading.Thread | None = None
        self._deadline: float | None = None
        # Host wall-clock (ns) captured when the current/last recording started,
        # written into recording_summary.json as the real-world start time.
        self._recording_start_wall_ns = 0
        # Bumped each time a countdown starts so clients can tell one recording
        # from the next even if they miss the intervening non-recording states.
        self._recording_seq = 0
        self._listeners: list = []
        self.events: deque = deque(maxlen=100)

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
        getattr(log, level if level != "error" else "error")(message)
        entry = {"time": time.time(), "level": level, "message": message}
        self.events.append(entry)
        self._notify("event", entry)

    # ------------------------------------------------------------- settings

    @property
    def state(self) -> str:
        return self._state

    @property
    def recording_active(self) -> bool:
        return self._state in ("waiting", "recording", "finishing")

    def get_settings(self) -> RecordingSettings:
        with self._lock:
            return dataclasses.replace(self._settings)

    def update_settings(self, **changes) -> RecordingSettings:
        """Apply settings changes; rejected while a recording is active."""
        with self._lock:
            if self.recording_active:
                raise RuntimeError("Settings are locked while recording")
            unknown = set(changes) - {
                f.name for f in dataclasses.fields(RecordingSettings)
            }
            if unknown:
                raise ValueError(f"Unknown settings: {sorted(unknown)}")
            if "save_method" in changes and changes["save_method"] not in FORMATS:
                raise ValueError(f"Unknown save_method: {changes['save_method']}")
            if "fps" in changes and not changes["fps"] > 0:
                raise ValueError("fps must be > 0")
            if "duration_s" in changes and not changes["duration_s"] > 0:
                raise ValueError("duration_s must be > 0")
            if "trigger_source" in changes and changes["trigger_source"] not in (
                "software",
                "external",
            ):
                raise ValueError("trigger_source must be software or external")
            if "record_form" in changes and changes["record_form"] not in (
                "display",
                "sensor",
            ):
                raise ValueError("record_form must be display or sensor")
            if "transcode_ffmpeg_params" in changes:
                # Reject args ffmpeg could never parse (bad quoting) up front:
                # the config loader would otherwise silently drop them back to
                # the default when `octacam process` reads the snapshot.
                try:
                    shlex.split(changes["transcode_ffmpeg_params"])
                except ValueError as e:
                    raise ValueError(f"invalid transcode_ffmpeg_params: {e}") from e
            if "record_directory" in changes:
                changes["record_directory"] = normalize_save_dir(
                    changes["record_directory"]
                )
            if "save_dir" in changes:
                changes["save_dir"] = normalize_save_dir(changes["save_dir"])
            merged = dataclasses.replace(self._settings, **changes)
            # Editing either half of the split path re-derives the combined
            # save_dir the recording machinery uses (config.resolve_save_dir does
            # the same join at record time).
            if "record_directory" in changes or "relative_directory" in changes:
                merged = dataclasses.replace(
                    merged,
                    save_dir=compose_save_dir(
                        merged.record_directory, merged.relative_directory
                    ),
                )
            self._settings = merged
            if "fps" in changes:  # live-updates the preview trigger rate
                self.camera_system.set_software_trigger_frequency(self._settings.fps)
            return dataclasses.replace(self._settings)

    def validate_save_dir(self, path_str: str) -> dict:
        resolved = Path(normalize_save_dir(path_str))
        parent = next((p for p in [resolved, *resolved.parents] if p.exists()), None)
        free_bytes = shutil.disk_usage(parent).free if parent else 0
        return {
            "resolved": str(resolved),
            "exists": resolved.exists(),
            "creatable": parent is not None and os.access(parent, os.W_OK),
            "free_bytes": free_bytes,
        }

    def browse_directory(self, path_str: str = "") -> dict:
        """List the immediate subdirectories of a server-side path.

        Recording happens on the rig, so the save directory is a *server-side*
        path the browser cannot pick natively; this backs an in-app directory
        picker. A blank path opens at the current save directory; a partially
        typed or not-yet-created path falls back to its nearest existing
        ancestor, so the picker always lands somewhere it can list. Hidden
        directories (``.``-prefixed) are omitted.
        """
        raw = (path_str or "").strip() or (self._settings.save_dir or "")
        base = Path(normalize_save_dir(raw)) if raw.strip() else Path.home()
        current = next(
            (p for p in [base, *base.parents] if p.is_dir()),
            Path(base.anchor or "/"),
        )
        try:
            entries = sorted(
                (
                    child.name
                    for child in current.iterdir()
                    if not child.name.startswith(".") and child.is_dir()
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

    # ------------------------------------------------------ camera parameters

    @staticmethod
    def _param_payload(index: int, camera, params: dict) -> dict:
        return {
            "index": index,
            "serial": camera.serial_number,
            "width": camera.width,
            "height": camera.height,
            "params": params,
        }

    def read_camera_params(self, index: int) -> dict:
        """Current sensor-parameter descriptors for one camera."""
        with self._lock:
            camera = self.camera_system.camera_at(index)
        return self._param_payload(index, camera, camera.read_params())

    def set_camera_param(
        self, index: int, name: str, value: float, scope: str = "selected"
    ) -> dict:
        """Set a sensor parameter on one camera or all; rejected while recording.

        Width/Height require cycling the preview grab, which can take ~100 ms;
        that work is done OFF the controller lock (guarded by ``_reconfiguring``
        so a recording cannot start mid-cycle) to keep snapshot()/state polling
        responsive.
        """
        if name not in PARAM_NODES:
            raise ValueError(f"Unknown camera parameter: {name}")
        with self._lock:
            if self.recording_active:
                raise RuntimeError("Camera parameters are locked while recording")
            if self._reconfiguring:
                raise RuntimeError("A camera reconfiguration is already in progress")
            if scope == "all":
                targets = list(enumerate(self.camera_system))
            else:
                targets = [(index, self.camera_system.camera_at(index))]
            self._reconfiguring = True

        is_geometry = name in GEOMETRY_PARAMS

        def apply(camera) -> dict:
            if is_geometry:
                return camera.set_geometry(**{name: int(value)})["params"]
            return {name: camera.set_live_param(name, value)}

        try:
            if scope == "all":
                # Run every camera at once: each geometry change cycles only its
                # own preview, so 8 reconfigure in roughly one camera's time.
                results = self.camera_system.apply_to_all(apply)
                updated = [
                    self._param_payload(i, camera, params)
                    for (i, camera), params in zip(targets, results, strict=True)
                ]
            else:
                i, camera = targets[0]
                updated = [self._param_payload(i, camera, apply(camera))]
        finally:
            with self._lock:
                self._reconfiguring = False
        return {"updated": updated}

    def reset_camera_params(
        self, index: int, pfs_by_serial: dict[str, str], scope: str = "selected"
    ) -> dict:
        """Restore one camera's (or all cameras') sensor parameters to the config.

        ``pfs_by_serial`` maps a serial number to its ``<serial>.pfs`` text from
        the active config dir. Cameras without a saved ``.pfs`` are left
        unchanged; if none of the targeted cameras has one, ``FileNotFoundError``
        is raised so the caller can report that there is nothing to reset to. As
        in set_camera_param, the grab-cycling reload runs OFF the controller
        lock, guarded by ``_reconfiguring`` so a recording cannot start
        mid-cycle.
        """
        with self._lock:
            if self.recording_active:
                raise RuntimeError("Camera parameters are locked while recording")
            if self._reconfiguring:
                raise RuntimeError("A camera reconfiguration is already in progress")
            if scope == "all":
                targets = list(enumerate(self.camera_system))
            else:
                targets = [(index, self.camera_system.camera_at(index))]
            if not any(
                pfs_by_serial.get(camera.serial_number) for _, camera in targets
            ):
                raise FileNotFoundError(
                    "No saved camera parameters in the active config to reset to"
                )
            self._reconfiguring = True

        def apply(camera) -> dict:
            return camera.reset_params(pfs_by_serial.get(camera.serial_number, ""))[
                "params"
            ]

        try:
            if scope == "all":
                results = self.camera_system.apply_to_all(apply)
                updated = [
                    self._param_payload(i, camera, params)
                    for (i, camera), params in zip(targets, results, strict=True)
                ]
            else:
                i, camera = targets[0]
                updated = [self._param_payload(i, camera, apply(camera))]
        finally:
            with self._lock:
                self._reconfiguring = False
        return {"updated": updated}

    def export_camera_params(self) -> dict[str, str]:
        """Snapshot every camera's .pfs text; rejected while recording."""
        with self._lock:
            if self.recording_active:
                raise RuntimeError("Cannot save camera parameters while recording")
        return self.camera_system.save_all_params()

    def set_camera_name(self, index: int, name: str) -> dict:
        """Rename one camera live; rejected while recording or on a clash.

        ``camera.name`` is the per-camera output filename, so the name must be
        a safe single segment (``sanitize_camera_name``) and unique across the
        rig — two cameras sharing a name would write to the same video file.
        The change is in-memory only; it is persisted to the config solely by
        an explicit save (the GUI sends each camera's name with the layout).
        """
        clean = sanitize_camera_name(name)
        with self._lock:
            if self.recording_active:
                raise RuntimeError("Camera names are locked while recording")
            camera = self.camera_system.camera_at(index)
            for other_index, other in enumerate(self.camera_system):
                if other_index != index and other.name == clean:
                    raise ValueError(f"Another camera already uses the name {clean!r}")
            camera.name = clean
            return {"index": index, "serial": camera.serial_number, "name": clean}

    def set_camera_transform(
        self, index: int, scale_x: float, scale_y: float, rotation_deg: float
    ) -> dict:
        """Set one camera's display transform live; rejected while recording.

        This is what gets baked into a "display"-form recording, so the GUI's
        View-tab rotate/flip pushes here as the operator works — keeping "what
        you see" and "what is recorded" in sync without a config save."""
        with self._lock:
            if self.recording_active:
                raise RuntimeError("Camera transforms are locked while recording")
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

    def start_preview(self) -> None:
        """(Re)start live preview with the free-running software trigger."""
        with self._lock:
            if self.recording_active:
                raise RuntimeError("Cannot start preview while recording")
            self.camera_system.set_software_trigger_frequency(self._settings.fps)
            self.camera_system.start_preview()
            self.camera_system.start_software_trigger()
            self._set_state("preview")

    # ------------------------------------------------------------ recording

    def start_recording(
        self,
        confirm_overwrite: bool = False,
        plugin_params: dict | None = None,
    ) -> StartResult:
        with self._lock:
            if self.recording_active:
                return StartResult(StartResult.BUSY, "Recording in progress")
            if self._reconfiguring:
                return StartResult(
                    StartResult.BUSY, "Camera reconfiguration in progress"
                )
            settings = self._settings
            save_dir = Path(settings.save_dir)
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

            use_software_trigger = settings.trigger_source == "software"
            self.camera_system.stop_software_trigger()
            self.camera_system.enable_frame_trigger()
            self.camera_system.set_trigger_source(use_software_trigger)
            self.camera_system.set_software_trigger_frequency(settings.fps)
            self._recording_start_wall_ns = time.time_ns()
            started = self.camera_system.start_record(
                save_dir,
                settings.fps,
                settings.video_format(),
                settings.record_form,
                settings.save_frame_timestamps,
            )
            total = len(self.camera_system)
            if not started:
                self._event("error", "No camera could start recording")
                self._resume_preview()
                return StartResult(StartResult.ERROR, "No camera could start recording")
            if len(started) < total:
                missing = [
                    camera.name
                    for camera in self.camera_system
                    if camera.name not in started
                ]
                self._event(
                    "warning",
                    f"Only {len(started)}/{total} cameras started "
                    f"recording (missing: {', '.join(missing)})",
                )
            if use_software_trigger:
                self.camera_system.start_software_trigger(settings.duration_s)

            # Snapshot the config and write an initial summary (pessimistically
            # marked aborted, frames=0) as soon as the cameras start, so a raw
            # recording keeps its geometry — and stays transcodable — even if the
            # process is hard-killed before the final summary is written at
            # teardown. Both are overwritten with final data when recording ends.
            self._snapshot_config()
            self._write_recording_summary(aborted=True)

            self._aborted = False
            self._stop_event.clear()
            self._start_hooks_done.clear()
            self._deadline = None
            self._set_state("waiting")
            self._monitor = threading.Thread(
                target=self._monitor_loop,
                args=(
                    settings.duration_s,
                    plugin_params,
                    len(started),
                    not use_software_trigger,
                ),
                daemon=True,
            )
            self._monitor.start()
        # Arm plugins off the controller lock — like on_first_frame below — so a
        # plugin's blocking serial write (e.g. the twophoton arm, write_timeout=1
        # s) can't stall snapshot()/telemetry while self._lock is held. The
        # monitor only fires on_first_frame once cameras deliver a frame, which on
        # an external-trigger rig cannot happen until this arm runs.
        #
        # Skip the arm if the recording was already stopped/aborted in the window
        # between releasing the lock and reaching here: otherwise an abort's
        # teardown (which dispatches on_recording_stop — e.g. the twophoton
        # cancel) could race ahead of this arm and leave the hardware armed after
        # the cameras have already stopped.
        try:
            if not self._stop_event.is_set():
                self.plugins.dispatch("on_recording_start", plugin_params)
        finally:
            # Unblock the monitor's on_first_frame / on_recording_stop regardless
            # of whether the arm ran or raised — the event guards ordering, not
            # success. The monitor waits on it so a cancel can never overtake the
            # arm even when this dispatch is slow (e.g. the bounded ack wait).
            self._start_hooks_done.set()
        return StartResult(StartResult.OK)

    def _resume_preview(self) -> None:
        """Return to preview (or idle) after a recording ends or fails."""
        if self._auto_preview:
            self.camera_system.set_software_trigger_frequency(self._settings.fps)
            self.camera_system.start_preview()
            self.camera_system.start_software_trigger()
            self._set_state("preview")
        else:
            self._set_state("idle")

    def stop_recording(self, abort: bool = False) -> None:
        """Finish (or abort) the current recording early."""
        with self._lock:
            if not self.recording_active:
                return
            self._aborted = abort
            self._stop_event.set()

    def join(self, timeout: float | None = None) -> None:
        """Block until the current recording has fully finished."""
        monitor = self._monitor
        if monitor is not None:
            monitor.join(timeout)

    def close(self) -> None:
        self.stop_recording(abort=True)
        self.join()
        self.camera_system.close()

    def _monitor_loop(
        self, duration_s, plugin_params, expected_started, external_trigger
    ) -> None:
        # --- wait for the first frame from the cameras that started (Qt's
        # check_record_started_timer). Warn after 3 s. With the software
        # trigger - unlike the unbounded Qt/headless wait - give up after
        # STARTED_FAIL_AFTER_S and record with whatever started, so a single
        # stalled camera cannot hang the whole recording (and the deadline)
        # indefinitely. With an external trigger the frames only arrive once
        # the external source fires, which may be arbitrarily far in the
        # future, so there is no such deadline: wait indefinitely (until the
        # first frame, or the user stops the recording).
        start = time.monotonic()
        warned = False
        while not self._stop_event.is_set():
            if self._count_started() >= expected_started:
                break
            elapsed = time.monotonic() - start
            if not warned and elapsed > STARTED_WARN_AFTER_S:
                if external_trigger:
                    self._event(
                        "info",
                        "Waiting for the external trigger; recording will "
                        "begin on the first frame",
                    )
                else:
                    self._event(
                        "warning",
                        "Not all cameras delivered a frame within "
                        f"{STARTED_WARN_AFTER_S:g} s; still waiting",
                    )
                warned = True
            if not external_trigger and elapsed > STARTED_FAIL_AFTER_S:
                self._event(
                    "error",
                    f"Only {self._count_started()}/{expected_started} cameras "
                    f"delivered a frame within {STARTED_FAIL_AFTER_S:g} s; "
                    "starting the countdown anyway",
                )
                break
            self._stop_event.wait(STARTED_POLL_INTERVAL_S)

        if not self._stop_event.is_set():
            # Let the off-lock on_recording_start hooks finish first (normally
            # done long before a frame arrives; only blocks in the rare case a
            # frame lands mid-arm) so first-frame motion can't precede the arm.
            self._start_hooks_done.wait(START_HOOKS_TIMEOUT_S)
            # Fire plugin first-frame hooks at the t0 of the countdown, in the
            # same place the inline flywheel write used to live, so stepper
            # motion (or any plugin) stays synchronised to actual capture.
            self.plugins.dispatch("on_first_frame", plugin_params)
            with self._lock:
                deadline = time.monotonic() + duration_s + STOP_GRACE_S
                self._deadline = deadline
                self._recording_seq += 1
                self._set_state("recording")
            # --- countdown
            while not self._stop_event.is_set():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                self._stop_event.wait(min(remaining, 0.2))

        # --- finishing: same teardown order as MainWindow._stop_record and
        # cli.record: trigger off -> grab loops exit -> writers drain -> CSVs
        with self._lock:
            self._set_state("finishing")
        self.camera_system.stop_software_trigger()
        self.camera_system.stop()
        for camera in self.camera_system:
            if camera.writer_failed:
                self._event(
                    "error",
                    f"Writer for camera {camera.name} failed during the "
                    "recording (see log for ffmpeg output)",
                )

        # A camera that captured 0 frames produced only a header-only file (no
        # video). The writer never fails for this - it opened fine and just got
        # no frames - so without this check the recording is reported as a
        # normal success. The usual cause is an external trigger that never
        # fired during the window; flag it loudly here, while frames_recorded is
        # final, rather than letting it surface later as a cryptic transcode
        # error on the empty file.
        empty = [c.name for c in self.camera_system if c.frames_recorded == 0]
        if empty:
            self._event(
                "error",
                f"{len(empty)} camera(s) captured 0 frames (no video written): "
                f"{', '.join(empty)}. "
                + (
                    "No external trigger pulses were received during the "
                    "recording window."
                    if self._settings.trigger_source == "external"
                    else "The cameras delivered no frames."
                ),
            )

        # All camera threads are joined now (stats/CSVs final) and save_dir is
        # still the recording's own directory (it is incremented below). Write
        # the session summary here so both the duration-elapsed and manual-stop
        # paths produce exactly one; never let a summary error abort teardown.
        self._write_recording_summary(self._aborted)
        self._note_in_session_cache()

        with self._lock:
            self._deadline = None
            aborted = self._aborted
            if not aborted:
                # Bump the trailing 3-digit run so the next recording lands in a
                # fresh folder. Increment the relative sub-path (keeping the base
                # fixed) and recompose so both halves stay consistent; fall back
                # to bumping save_dir directly when there is no relative part.
                if self._settings.relative_directory.strip():
                    next_rel = increment_trailing_number(
                        self._settings.relative_directory
                    )
                    self._settings = dataclasses.replace(
                        self._settings,
                        relative_directory=next_rel,
                        save_dir=compose_save_dir(
                            self._settings.record_directory, next_rel
                        ),
                    )
                else:
                    self._settings = dataclasses.replace(
                        self._settings,
                        save_dir=increment_trailing_number(self._settings.save_dir),
                    )
            self._resume_preview()
        # Wait out the on_recording_start hooks so a stop/abort cancel can never
        # overtake a not-yet-sent arm (e.g. the twophoton hardware trigger) when
        # the recording is stopped in the window right after it starts.
        self._start_hooks_done.wait(START_HOOKS_TIMEOUT_S)
        self.plugins.dispatch("on_recording_stop", aborted)
        self._event("info", "Recording aborted" if aborted else "Recording finished")

    def _snapshot_config(self) -> None:
        """Copy the rig config into the recording folder for `octacam process`.

        The snapshot carries the GUI's live [transcode]/[transfer] values (the
        Process section), patched into the folder's octacam_config.toml so the
        post-recording step transcodes and transfers with exactly what the
        operator set — no --config, no touching the rig's own config file. When
        those fields are untouched the copy is byte-verbatim (comments and the
        unexpanded directory/relative_directory templates preserved); only a
        real edit triggers a re-emit. Best-effort: any failure falls back to the
        verbatim copy and never disturbs recording; no-ops without a config dir
        (tests)."""
        if self._config_dir is None:
            return
        src = self._config_dir / "octacam_config.toml"
        dst = Path(self._settings.save_dir) / "octacam_config.toml"
        if not src.exists():
            return
        s = self._settings
        try:
            raw = config_writer.load_raw_config(self._config_dir)
            patched = config_writer.with_process_params(
                raw,
                transcode_ffmpeg_params=s.transcode_ffmpeg_params,
                transfer_directory=s.transfer_directory,
                transfer_checksum=s.transfer_checksum,
            )
            if raw and patched != raw:
                config_writer.write_config(s.save_dir, patched)
            else:
                shutil.copyfile(src, dst)
        except Exception:
            # A re-emit edge (e.g. an exotic value the writer can't serialize)
            # must not lose the snapshot: fall back to the verbatim copy.
            log.exception("Failed to write patched config snapshot to %s", dst)
            with contextlib.suppress(Exception):
                shutil.copyfile(src, dst)

    def _write_recording_summary(self, aborted: bool) -> None:
        """Write recording_summary.json into the recording's save directory."""
        path = Path(self._settings.save_dir) / RECORDING_SUMMARY_FILENAME
        try:
            summary = build_recording_summary(
                self._settings,
                list(self.camera_system),
                self._recording_start_wall_ns,
                aborted,
            )
            path.write_text(json.dumps(summary, indent=2) + "\n")
            log.info("Wrote recording summary: %s", path)
        except Exception:
            log.exception("Failed to write recording summary to %s", path)

    def _note_in_session_cache(self) -> None:
        """Record this recording's folder in the session cache for `transcode`.

        Lets `octacam transcode --last/--session/--all` rediscover it later.
        No-ops without a session id (direct controller construction in tests);
        best-effort, so a cache failure never disturbs recording teardown. Runs
        before save_dir is incremented, so it captures the just-written folder.
        """
        if not self._session_id:
            return
        folder = Path(self._settings.save_dir)
        try:
            from octacam import session_cache

            session_cache.record_recording(folder, self._session_id, self._record_kind)
        except Exception:
            log.exception("Failed to note %s in the recording cache", folder)

    def _count_started(self) -> int:
        return sum(1 for camera in self.camera_system if camera.started)

    # ---------------------------------------------------------------- status

    def snapshot(self) -> dict:
        with self._lock:
            settings = self._settings
            remaining_ms = None
            recording_id = None
            if self._state == "recording" and self._deadline is not None:
                remaining_ms = max(0, round((self._deadline - time.monotonic()) * 1000))
                recording_id = self._recording_seq
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
            "remaining_ms": remaining_ms,
            "recording_id": recording_id,
            "save_dir": settings.save_dir,
            "disk_free_bytes": free_bytes,
            "settings": dataclasses.asdict(settings),
            "cameras": [
                {
                    "name": camera.name,
                    "serial": camera.serial_number,
                    "fps": round(camera.resulting_fps, 2),
                    "frames": camera.frames_recorded,
                    "dropped": camera.dropped_count,
                    "writer_failed": camera.writer_failed,
                }
                for camera in self.camera_system
            ],
        }
