"""Recording orchestration shared by the web UI and the headless CLI.

Extracts the start/stop/abort lifecycle that previously lived in the Qt
MainWindow (timer-driven) and cli.py (sleep-driven) into a framework-free
state machine:

    preview/idle -> waiting -> recording -> finishing -> preview/idle

A monitor thread replaces the Qt timers: it polls for the first frame on
every camera (dispatching plugin hooks at that moment — e.g. a flywheel
stepper command), enforces the recording deadline, and runs the teardown
sequence in the same order as the original code (stop trigger -> grab loops
exit -> writers drain -> summary + timestamps).
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

import numpy as np

from octacam import config_writer
from octacam.camera import GEOMETRY_PARAMS, PARAM_NODES, CameraSystem
from octacam.plugins.base import PluginManager
from octacam.transform import (
    RECORDING_SUMMARY_FILENAME,
    TIMESTAMPS_FILENAME,
    DisplayTransform,
)
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
    # onto the destination; empty falls back to the save_dir's own basename.
    record_directory: str = ""
    relative_directory: str = ""
    trigger_source: str = "software"  # "software" | "managed" | "external"
    # How preview is triggered. "auto" mirrors trigger_source (software->software,
    # managed->drive the plugin, external->free-run approximation); "software" and
    # "free_running" force that mode regardless of the recording trigger source.
    preview_trigger_source: str = "auto"  # "auto" | "software" | "free_running"
    save_method: str = "ffmpeg"  # "ffmpeg" | "raw"
    # Verbatim ffmpeg output/encoder args used when save_method == "ffmpeg".
    ffmpeg_params: str = DEFAULT_FFMPEG_PARAMS
    remux_mp4: bool = False
    # "display" bakes each camera's display transform into the video; "sensor"
    # saves the raw, untransformed image. save_frame_timestamps writes the
    # per-frame timestamp file (timestamps.npz; debugging; off by default).
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

_TIMESTAMP_NOTE = (
    "Per-camera `timestamp_source` records where each camera's per-frame "
    "timestamps came from. `hardware` = the camera/SDK timestamp (a free-running "
    "counter with a per-camera epoch — precise for relative timing, but NOT "
    "wall-clock and NOT aligned across cameras). `host` = host `time.time_ns()` "
    "(UTC wall clock; used when the backend supplies none). The full per-frame "
    f"series is written to {TIMESTAMPS_FILENAME} when save_timestamps is on."
)


def _timestamp_source(frames: int, host_fallback_count: int) -> str | None:
    """Where a camera's per-frame timestamps came from, from the fallback count.

    ``None`` when no frames were recorded; ``"hardware"`` when none fell back;
    ``"host"`` when all did (a host-only backend like pycameleon);
    ``"mixed"`` when only some did (a stray-zero anomaly, surfaced honestly)."""
    if frames <= 0:
        return None
    if host_fallback_count <= 0:
        return "hardware"
    if host_fallback_count >= frames:
        return "host"
    return "mixed"


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
            start_wall_ns / 1e9, tz=datetime.timezone.utc
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
                "timestamp_source": _timestamp_source(
                    camera.frames_recorded, camera.host_fallback_count
                ),
                "host_fallback_count": camera.host_fallback_count,
                "writer_failed": camera.writer_failed,
                "transform": transform.to_dict(),
                "transform_applied": applied,
            }
        )
    return {
        "schema_version": 3,
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
        # the destination; resolved once here so a later transfer never
        # re-templates the date on a different day.
        "relative_directory": _relative_directory(settings),
        "dropped_frames_note": _DROPPED_FRAMES_NOTE,
        "timestamp_note": _TIMESTAMP_NOTE,
        "cameras": cams,
    }


def build_timestamps_arrays(cameras) -> dict[str, np.ndarray]:
    """Assemble the {key: array} payload for ``timestamps.npz`` from finalized
    camera stats.

    Pure (no I/O) so it can be unit-tested without a recording. Per camera, two
    parallel arrays keyed ``"<name>/timestamp_ns"`` (int64) and
    ``"<name>/dropped"`` (bool); ``frame_index`` is implicit (array position).
    Lengths are truncated to the shared minimum (mirrors the defensive
    non-strict zip the old CSV used) so a rare skew truncates rather than raises;
    a zero-frame camera contributes empty arrays."""
    arrays: dict[str, np.ndarray] = {}
    for camera in cameras:
        timestamps = camera.frame_timestamps
        dropped = camera.frame_dropped
        n = min(len(timestamps), len(dropped))
        arrays[f"{camera.name}/timestamp_ns"] = np.asarray(
            timestamps[:n], dtype=np.int64
        )
        arrays[f"{camera.name}/dropped"] = np.asarray(dropped[:n], dtype=bool)
    return arrays


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
        # Back-compat: a rig that predates the "managed" trigger source declares
        # trigger_source="external" plus a trigger-driving plugin (the shipped
        # triggerbox configs). octacam already drives that trigger, so promote it
        # to "managed" — the recording behaves identically, but `auto` preview now
        # resolves to the plugin-driven (synchronized, strobe-lit) preview instead
        # of the free-run approximation reserved for a truly external source.
        if (
            self._settings.trigger_source == "external"
            and self._preview_trigger_plugin() is not None
        ):
            self._settings = dataclasses.replace(self._settings, trigger_source="managed")
            log.info(
                "trigger_source 'external' with a trigger-driving plugin loaded; "
                "treating it as 'managed' (octacam drives the trigger)."
            )
        # The rig config dir whose octacam_config.toml is copied into each
        # recording folder (so `octacam process` needs no --config later). None
        # skips the snapshot (e.g. unit tests constructing a controller directly).
        self._config_dir = Path(config_dir) if config_dir is not None else None
        # When set, each finished recording's folder is noted in the session
        # cache (octacam.session_cache) under this id so `octacam process
        # --last/--last session/--all` can find it later. None disables the cache
        # (e.g. in unit tests that construct a controller directly).
        self._session_id = session_id
        self._record_kind = record_kind
        self._lock = threading.RLock()
        self._state = "idle"
        # True while a camera's geometry is being changed (preview stopped and
        # restarted off-lock); blocks a recording from starting mid-cycle.
        self._reconfiguring = False
        # True while a finished recording's teardown is still running its off-lock
        # tail (on_recording_stop disarm + preview re-arm) after the state has
        # already left the recording-active set; blocks a new recording from
        # racing that tail over the shared trigger plugin.
        self._tearing_down = False
        self._aborted = False
        self._stop_event = threading.Event()
        # Set once the off-lock on_recording_start plugin hooks have finished
        # dispatching. The monitor waits on it before on_first_frame and
        # on_recording_stop, so the start -> first_frame -> stop hook order holds
        # even though the arm runs on the caller thread — closing the race where
        # an abort's cancel could overtake a not-yet-sent hardware arm. A fresh
        # Event is minted per recording in start_recording (and captured by that
        # recording's monitor), so an old monitor can never be released early by —
        # or block on — a subsequent recording's hooks.
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
        # Benchmark (octacam.diagnostics): the last report (as a dict, for the
        # GUI's Benchmark tab and the last-diagnostic endpoint) plus the
        # background thread that runs it and the flag that cancels it on shutdown.
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

    @property
    def diagnosing(self) -> bool:
        """True while a benchmark (octacam.diagnostics) is running."""
        return self._state == "diagnosing"

    @property
    def _camera_locked(self) -> bool:
        """Camera control is locked while recording *or* benchmarking.

        A benchmark drives the cameras directly (its own grab loops + trigger
        timer), so any device-touching operation — starting a recording/preview,
        writing sensor parameters, snapshotting the nodemap — must be refused
        until it finishes, exactly as during a recording."""
        return self.recording_active or self.diagnosing

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
                "managed",
                "external",
            ):
                raise ValueError("trigger_source must be software, managed or external")
            if "preview_trigger_source" in changes and changes[
                "preview_trigger_source"
            ] not in ("auto", "software", "free_running"):
                raise ValueError(
                    "preview_trigger_source must be auto, software or free_running"
                )
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
            elif "save_dir" in changes:
                # A lone save_dir edit (no split-path change) clears the stale
                # split halves, mirroring the CLI --output precedent: otherwise
                # _relative_directory would keep preferring the old
                # relative_directory (mirroring to the wrong sub-path) and the
                # post-recording increment would recompose save_dir from it,
                # discarding the explicitly set path.
                merged = dataclasses.replace(
                    merged, record_directory="", relative_directory=""
                )
            self._settings = merged
            if "fps" in changes:  # live-updates the software-trigger rate
                self.camera_system.set_software_trigger_frequency(self._settings.fps)
            # Re-arm live preview when a change alters how it should be triggered:
            # the trigger source, the preview override, or (for a non-software
            # preview, whose rate is baked into the arm) the fps. A software
            # preview absorbs an fps change through set_software_trigger_frequency
            # above, so it needs no restart (keeps the fps slider smooth).
            rearm = (
                "trigger_source" in changes or "preview_trigger_source" in changes
            ) or ("fps" in changes and self._effective_preview_mode() != "software")
            arm_mode = (
                self._arm_preview_locked()
                if rearm and self._state == "preview"
                else None
            )
            new_settings = dataclasses.replace(self._settings)
        # Arm the driving plugin (managed preview only) off the lock — a plugin
        # arm can block on a serial write + ack; no-op for software/free-run.
        if arm_mode is not None:
            self._dispatch_preview_arm(arm_mode)
        return new_settings

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
            if self._camera_locked:
                raise RuntimeError(
                    "Camera parameters are locked while recording or benchmarking"
                )
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

        ``pfs_by_serial`` maps a serial number to its per-camera parameter text
        from the active config dir, in whatever format the backend persists
        (Basler ``.pfs`` / FLIR-GenICam ``.txt``). Cameras without a saved file are
        left unchanged; if none of the targeted cameras has one, ``FileNotFoundError``
        is raised so the caller can report that there is nothing to reset to. As
        in set_camera_param, the grab-cycling reload runs OFF the controller
        lock, guarded by ``_reconfiguring`` so a recording cannot start
        mid-cycle.
        """
        with self._lock:
            if self._camera_locked:
                raise RuntimeError(
                    "Camera parameters are locked while recording or benchmarking"
                )
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
        """Run ``apply(camera)`` on one camera or all, off the controller lock.

        Shared by the feature write/reset/command/center paths: it takes the
        same recording/reconfigure guards as set_camera_param, resolves the
        scope, then runs the (possibly grab-cycling) work under
        ``_reconfiguring`` so a recording cannot start mid-change. Returns the
        refreshed feature payload for every camera it touched."""
        with self._lock:
            if self._camera_locked:
                raise RuntimeError(
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
        """Write one node-map feature on one camera or all; refreshes the list.

        A write can change other nodes (an Auto mode locking its value, a ROI
        resize shifting the offsets), so every touched camera's full feature
        list is re-read and returned. Rejected while recording/benchmarking."""
        return self._reconfigure(index, scope, lambda camera: camera.set_feature(name, value))

    def reset_camera_feature(
        self, index: int, name: str, pfs_by_serial: dict[str, str], scope: str = "selected"
    ) -> dict:
        """Reset one feature to its saved-config value (else its factory default)."""
        return self._reconfigure(
            index,
            scope,
            lambda camera: camera.reset_feature(
                name, pfs_by_serial.get(camera.serial_number, "")
            ),
        )

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
            if self._camera_locked:
                raise RuntimeError(
                    "Cannot save camera parameters while recording or benchmarking"
                )
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

    def _preview_trigger_plugin(self):
        """First loaded plugin that can drive the trigger during preview, else None.

        Duck-typed (like ``set_controller``/``set_broadcast`` probing in the web
        app): a trigger-generating plugin (triggerbox) exposes
        ``drives_preview_trigger() -> True``; relay/other plugins (twophoton,
        flywheel) do not, so a ``managed`` preview falls back to free-run."""
        for plugin in self.plugins.plugins:
            drives = getattr(plugin, "drives_preview_trigger", None)
            try:
                if callable(drives) and drives():
                    return plugin
            except Exception:
                log.exception("plugin drives_preview_trigger check failed")
        return None

    @property
    def managed_trigger_available(self) -> bool:
        """True when a loaded plugin can drive the trigger during preview, so the
        ``managed`` trigger source is usable (surfaced to the GUI to enable it)."""
        return self._preview_trigger_plugin() is not None

    def _effective_preview_mode(self) -> str:
        """Resolve the preview trigger mode: software | free_running | managed.

        ``preview_trigger_source`` forces ``software``/``free_running``; ``auto``
        mirrors the recording ``trigger_source`` — software→software,
        managed→managed (drive the plugin) when a driving plugin is present, and
        external (or managed with no driving plugin) → free-run approximation."""
        pref = self._settings.preview_trigger_source
        if pref == "software":
            return "software"
        if pref == "free_running":
            return "free_running"
        src = self._settings.trigger_source  # auto
        if src == "software":
            return "software"
        if src == "managed" and self._preview_trigger_plugin() is not None:
            return "managed"
        return "free_running"

    def _arm_preview_locked(self) -> str:
        """Arm the cameras for live preview in the resolved mode; caller holds the
        lock. Returns the mode. For ``managed`` the driving plugin is armed
        separately, OFF the lock, via :meth:`_dispatch_preview_arm`."""
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
        """Arm/disarm the driving plugin for a preview mode, OFF the controller
        lock. Managed preview arms the plugin (indefinite, strobe-as-recording);
        any other mode (or None/idle) cancels a preview arm that may be running."""
        if mode == "managed":
            self.plugins.dispatch("on_preview_start", self._preview_arm_params())
        else:
            self.plugins.dispatch("on_preview_stop")

    def _preview_arm_params(self) -> dict:
        """The recording-start plugin slice reused for an indefinite preview arm.

        Same fps + light spec the recording would use (so preview strobes exactly
        as the recording will); the plugin substitutes an indefinite duration for
        preview."""
        return self.plugins.default_start_params(
            self._settings.fps, self._settings.duration_s
        )

    def start_preview(self) -> None:
        """(Re)start live preview in the resolved preview trigger mode."""
        with self._lock:
            if self._camera_locked:
                raise RuntimeError(
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
        # Bound up front so the off-lock tail below is provably assigned on every
        # path (they are only *read* on the matching branch, but pyright can't
        # connect the two `if not started` blocks / the early return).
        failed_resume_mode: str | None = None
        hooks_done: threading.Event | None = None
        with self._lock:
            if self.recording_active:
                return StartResult(StartResult.BUSY, "Recording in progress")
            if self.diagnosing:
                return StartResult(StartResult.BUSY, "A benchmark is in progress")
            if self._reconfiguring:
                return StartResult(
                    StartResult.BUSY, "Camera reconfiguration in progress"
                )
            if self._tearing_down:
                # The previous recording's teardown tail (disarm + preview re-arm)
                # is still running off-lock; starting now would race it over the
                # shared trigger plugin.
                return StartResult(
                    StartResult.BUSY, "Previous recording is still finishing"
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
            start_error: str | None = None
            try:
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
                    use_software_trigger=use_software_trigger,
                )
            except Exception as e:
                # A non-BackendError escaping the trigger-config or start_record
                # (CameraSystem only log-and-skips per-camera failures) can leave
                # some cameras already grabbing to disk with live ffmpeg writers
                # and no monitor. Tear them all down and fall into the `not
                # started` re-arm path below, so no orphaned recording cameras
                # are left behind and the state never stays half-advanced.
                log.exception("Recording failed to start")
                with contextlib.suppress(Exception):
                    self.camera_system.stop()
                start_error = f"Recording failed to start: {e}"
                started = None
            total = len(self.camera_system)
            if not started:
                if start_error is None:
                    self._event("error", "No camera could start recording")
                # Re-arm the preview cameras under the lock; the (possibly
                # blocking) driving-plugin arm is dispatched off the lock below.
                failed_resume_mode = self._resume_preview()
            else:
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
                # recording keeps its geometry — and stays transcodable — even if
                # the process is hard-killed before the final summary is written at
                # teardown. Both are overwritten with final data when recording ends.
                self._snapshot_config()
                self._write_recording_summary(aborted=True)

                self._aborted = False
                self._stop_event.clear()
                # Mint a fresh per-recording hooks-done event and capture it (both
                # as the current attribute and in a local this monitor closes over)
                # so an old monitor can never be released early by — or block on —
                # a later recording's hooks.
                hooks_done = threading.Event()
                self._start_hooks_done = hooks_done
                self._deadline = None
                self._set_state("waiting")
                self._monitor = threading.Thread(
                    target=self._monitor_loop,
                    args=(
                        settings.duration_s,
                        plugin_params,
                        len(started),
                        not use_software_trigger,
                        hooks_done,
                    ),
                    daemon=True,
                )
                self._monitor.start()
        if not started:
            # No camera recorded: dispatch the preview re-arm OFF the lock (a
            # driving-plugin arm can block on a serial write + ack), then bail.
            self._dispatch_preview_arm(failed_resume_mode)
            return StartResult(
                StartResult.ERROR, start_error or "No camera could start recording"
            )
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
            # Set this recording's own event (captured locally) rather than
            # self._start_hooks_done, which a later recording may have replaced.
            if hooks_done is not None:
                hooks_done.set()
        return StartResult(StartResult.OK)

    def _resume_preview(self) -> str | None:
        """Arm preview (or go idle) after a recording/benchmark ends; caller holds
        the lock.

        Returns the resolved preview mode so the caller can arm the driving plugin
        OFF the lock via :meth:`_dispatch_preview_arm` (None = went idle, which
        still disarms any preview arm)."""
        if self._auto_preview:
            return self._arm_preview_locked()
        self._set_state("idle")
        return None

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
        # Cancel a running benchmark and wait for it to unwind (it drives the
        # cameras directly, so it must finish before we close them). Cancellation
        # is checked at every measurement-window boundary, so this returns
        # quickly; the timeout is a backstop against a wedged SDK call.
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
        """Start a background benchmark (:mod:`octacam.diagnostics`).

        Pauses live preview, runs the diagnostic on its own thread, then resumes
        preview. Returns immediately with ``StartResult.OK`` once launched, or
        ``BUSY`` if a recording, reconfiguration, or another benchmark is active.
        Progress is emitted as events; the final report is broadcast to listeners
        under the ``"diagnostics"`` kind and cached for :meth:`get_last_diagnostic`.
        """
        with self._lock:
            if self.recording_active:
                return StartResult(StartResult.BUSY, "Recording in progress")
            if self.diagnosing:
                return StartResult(StartResult.BUSY, "A benchmark is already running")
            if self._reconfiguring:
                return StartResult(
                    StartResult.BUSY, "Camera reconfiguration in progress"
                )
            # Snapshot the settings so a concurrent edit can't shift the target
            # mid-run, and flip to the diagnosing state (which locks camera
            # control) before releasing the lock.
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
            # Stop live preview (and its trigger) off the lock — the diagnostic
            # owns the cameras for its duration via its own grab loops + timer.
            # Also disarm any managed preview arm: the benchmark drives cameras via
            # begin_freerun (TriggerMode Off), which would ignore — and be fought by
            # — a plugin still pulsing the external trigger line.
            self.camera_system.stop_software_trigger()
            self.plugins.dispatch("on_preview_stop")
            self.camera_system.stop()

            def progress(p) -> None:
                # Structured progress for the Benchmark tab's determinate bar
                # (broadcast newest-only, so intermediate updates collapse). The
                # bar replaces the per-phase log spam; start/finish still log.
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
            # Always leave the "diagnosing" state, even on failure or cancel — a
            # camera dropping out during the benchmark can make the preview re-arm
            # raise BackendError, which would otherwise wedge the controller in
            # "diagnosing" (camera control locked) for the rest of the process.
            try:
                with self._lock:
                    resume_mode = self._resume_preview()
            except Exception:
                log.exception("Preview re-arm failed after benchmark; going idle")
                self._event("error", "Preview could not resume after the benchmark")
                with self._lock:
                    self._set_state("idle")
                resume_mode = None
            # re-arm managed preview off-lock; a plugin dispatch error here must
            # not leak out of the thread either (the state is already terminal).
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

    def _monitor_loop(self, *args) -> None:
        """Run the recording monitor, guaranteeing a terminal, non-active state.

        Wraps :meth:`_run_monitor_loop` so that ANY unexpected exception (which in
        a daemon thread would otherwise die silently, leaking recording_active=True
        and wedging the whole controller) still leaves it idle, the trigger plugin
        disarmed, and the teardown gate cleared."""
        try:
            self._run_monitor_loop(*args)
        except Exception:
            log.exception("Recording monitor crashed; forcing idle")
            with contextlib.suppress(Exception):
                self.plugins.dispatch("on_recording_stop", self._aborted)
            with self._lock:
                self._tearing_down = False
                self._set_state("idle")
            self._event("error", "Recording monitor crashed (see log); forced idle")

    def _run_monitor_loop(
        self, duration_s, plugin_params, expected_started, external_trigger, hooks_done
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
            # Wait on this recording's own captured event (not the attribute,
            # which a later recording may have replaced).
            hooks_done.wait(START_HOOKS_TIMEOUT_S)
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

        # All camera threads are joined now (stats/timestamps final) and save_dir
        # is still the recording's own directory (it is incremented below). Write
        # the session summary here so both the duration-elapsed and manual-stop
        # paths produce exactly one; never let a summary error abort teardown.
        self._write_recording_summary(self._aborted)
        if self._settings.save_frame_timestamps:
            self._write_timestamps()
        self._note_in_session_cache()

        # From here the recording leaves the active set (state -> preview/idle),
        # but the off-lock disarm + preview re-arm below are still pending. Hold a
        # `_tearing_down` gate across them so a new start cannot race this tail
        # over the shared trigger plugin, and clear it in the finally.
        with self._lock:
            self._tearing_down = True
        try:
            with self._lock:
                self._deadline = None
                aborted = self._aborted
                if not aborted:
                    # Bump the trailing 3-digit run so the next recording lands in
                    # a fresh folder. Increment the relative sub-path (keeping the
                    # base fixed) and recompose so both halves stay consistent;
                    # fall back to bumping save_dir directly when there is no
                    # relative part.
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
                            save_dir=increment_trailing_number(
                                self._settings.save_dir
                            ),
                        )
                # Guard the preview re-arm: a camera dropped mid-recording can make
                # start_preview raise BackendError, which — since _set_state runs
                # only after it succeeds — would otherwise wedge the controller in
                # "finishing". On failure go idle (recording_active False) and
                # still run on_recording_stop below so the trigger plugin is
                # disarmed.
                try:
                    resume_mode = self._resume_preview()
                except Exception:
                    log.exception(
                        "Preview re-arm failed after recording; going idle"
                    )
                    self._event(
                        "error", "Preview could not resume after the recording"
                    )
                    self._set_state("idle")
                    resume_mode = None
            # Wait out the on_recording_start hooks so a stop/abort cancel can never
            # overtake a not-yet-sent arm (e.g. the twophoton hardware trigger) when
            # the recording is stopped in the window right after it starts. Wait on
            # this recording's own captured event, not the attribute.
            hooks_done.wait(START_HOOKS_TIMEOUT_S)
            self.plugins.dispatch("on_recording_stop", aborted)
            # Re-arm the driving plugin for preview (managed only) AFTER the
            # recording cancel, off the lock, so the record arm is fully torn down
            # first.
            self._dispatch_preview_arm(resume_mode)
            self._event(
                "info", "Recording aborted" if aborted else "Recording finished"
            )
        finally:
            with self._lock:
                self._tearing_down = False

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

    def _write_timestamps(self) -> None:
        """Write the per-frame timestamp series for every camera into one
        compressed ``timestamps.npz`` in the recording's save directory.

        Called only when save_timestamps is on, after every grab thread is
        joined (so the series are final). Best-effort like the summary: a
        failure is logged, never allowed to abort teardown."""
        path = Path(self._settings.save_dir) / TIMESTAMPS_FILENAME
        try:
            arrays = build_timestamps_arrays(list(self.camera_system))
            np.savez_compressed(path, **arrays)
            log.info("Wrote frame timestamps: %s", path)
        except Exception:
            log.exception("Failed to write frame timestamps to %s", path)

    def _note_in_session_cache(self) -> None:
        """Record this recording's folder in the session cache for `transcode`.

        Lets `octacam process --last/--last session/--all` rediscover it later.
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
