"""Recording orchestration shared by the web UI and the headless CLI.

Extracts the start/stop/abort lifecycle that previously lived in the Qt
MainWindow (timer-driven) and cli.py (sleep-driven) into a framework-free
state machine:

    preview/idle -> waiting -> recording -> finishing -> preview/idle

A monitor thread replaces the Qt timers: it polls for the first frame on
every camera (firing an armed Arduino command at that moment), enforces the
recording deadline, and runs the teardown sequence in the same order as the
original code (stop trigger -> grab loops exit -> writers drain -> CSVs).
"""

import dataclasses
import logging
import os
import re
import shutil
import threading
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path

from octacam.camera import CameraSystem
from octacam.serial_link import Command, SerialLink
from octacam.writer import FORMATS, VideoFormat

log = logging.getLogger("octacam")

STARTED_POLL_INTERVAL_S = 0.1
STARTED_WARN_AFTER_S = 3.0
STOP_GRACE_S = 0.5  # matches cli.py's in-flight frame grace period

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


@dataclass
class RecordingSettings:
    fps: float = 100.0
    duration_s: float = 20.0
    save_dir: str = "./"
    trigger_source: str = "software"  # "software" | "external"
    codec: str = "x264"
    crf: int = 16
    preset: str = "ultrafast"
    pix_fmt: str = "gray"
    remux_mp4: bool = False

    def video_format(self) -> VideoFormat:
        video_format = FORMATS[self.codec]
        if self.codec == "x264":
            video_format = dataclasses.replace(
                video_format,
                crf=self.crf,
                preset=self.preset,
                pix_fmt=self.pix_fmt,
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
        serial_link: SerialLink | None = None,
        auto_preview: bool = True,
    ):
        self.camera_system = camera_system
        self.serial_link = serial_link
        self._settings = settings
        self._auto_preview = auto_preview
        self._lock = threading.RLock()
        self._state = "idle"
        self._aborted = False
        self._stop_event = threading.Event()
        self._monitor: threading.Thread | None = None
        self._deadline: float | None = None
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
            if "codec" in changes and changes["codec"] not in FORMATS:
                raise ValueError(f"Unknown codec: {changes['codec']}")
            if "fps" in changes and not changes["fps"] > 0:
                raise ValueError("fps must be > 0")
            if "duration_s" in changes and not changes["duration_s"] > 0:
                raise ValueError("duration_s must be > 0")
            if "trigger_source" in changes and changes[
                "trigger_source"
            ] not in ("software", "external"):
                raise ValueError("trigger_source must be software or external")
            if "save_dir" in changes:
                changes["save_dir"] = normalize_save_dir(changes["save_dir"])
            self._settings = dataclasses.replace(self._settings, **changes)
            if "fps" in changes:  # live-updates the preview trigger rate
                self.camera_system.set_software_trigger_frequency(
                    self._settings.fps
                )
            return dataclasses.replace(self._settings)

    def validate_save_dir(self, path_str: str) -> dict:
        resolved = Path(normalize_save_dir(path_str))
        parent = next(
            (p for p in [resolved, *resolved.parents] if p.exists()), None
        )
        free_bytes = shutil.disk_usage(parent).free if parent else 0
        return {
            "resolved": str(resolved),
            "exists": resolved.exists(),
            "creatable": parent is not None and os.access(parent, os.W_OK),
            "free_bytes": free_bytes,
        }

    # -------------------------------------------------------------- preview

    def start_preview(self) -> None:
        """(Re)start live preview with the free-running software trigger."""
        with self._lock:
            if self.recording_active:
                raise RuntimeError("Cannot start preview while recording")
            self.camera_system.set_software_trigger_frequency(
                self._settings.fps
            )
            self.camera_system.start_preview()
            self.camera_system.start_software_trigger()
            self._set_state("preview")

    # ------------------------------------------------------------ recording

    def start_recording(
        self,
        confirm_overwrite: bool = False,
        arduino_command: Command | None = None,
    ) -> StartResult:
        with self._lock:
            if self.recording_active:
                return StartResult(StartResult.BUSY, "Recording in progress")
            settings = self._settings
            save_dir = Path(settings.save_dir)
            if save_dir.exists() and not confirm_overwrite:
                return StartResult(
                    StartResult.NEEDS_CONFIRM,
                    f"Directory already exists: {save_dir}",
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
            self.camera_system.start_record(
                save_dir, settings.fps, settings.video_format()
            )
            if use_software_trigger:
                self.camera_system.start_software_trigger(settings.duration_s)

            self._aborted = False
            self._stop_event.clear()
            self._deadline = None
            self._set_state("waiting")
            self._monitor = threading.Thread(
                target=self._monitor_loop,
                args=(settings.duration_s, arduino_command),
                daemon=True,
            )
            self._monitor.start()
            return StartResult(StartResult.OK)

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

    def _monitor_loop(self, duration_s, arduino_command) -> None:
        # --- wait for the first frame on every camera (Qt's
        # check_record_started_timer); warn once after 3 s but keep waiting,
        # since an external trigger may legitimately start late.
        start = time.monotonic()
        warned = False
        while not self._stop_event.is_set():
            if self.camera_system.all_cameras_started:
                break
            if not warned and time.monotonic() - start > STARTED_WARN_AFTER_S:
                self._event(
                    "warning",
                    "Not all cameras delivered a frame within "
                    f"{STARTED_WARN_AFTER_S:g} s",
                )
                warned = True
            self._stop_event.wait(STARTED_POLL_INTERVAL_S)

        if not self._stop_event.is_set():
            if arduino_command is not None and self.serial_link is not None:
                self.serial_link.write_command(arduino_command)
            with self._lock:
                self._deadline = (
                    time.monotonic() + duration_s + STOP_GRACE_S
                )
                self._set_state("recording")
            # --- countdown
            while not self._stop_event.is_set():
                remaining = self._deadline - time.monotonic()
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

        with self._lock:
            self._deadline = None
            aborted = self._aborted
            if not aborted:
                self._settings = dataclasses.replace(
                    self._settings,
                    save_dir=increment_trailing_number(
                        self._settings.save_dir
                    ),
                )
            if self._auto_preview:
                self.camera_system.set_software_trigger_frequency(
                    self._settings.fps
                )
                self.camera_system.start_preview()
                self.camera_system.start_software_trigger()
                self._set_state("preview")
            else:
                self._set_state("idle")
        self._event(
            "info", "Recording aborted" if aborted else "Recording finished"
        )

    # ---------------------------------------------------------------- status

    def snapshot(self) -> dict:
        with self._lock:
            settings = self._settings
            remaining_ms = None
            if self._state == "recording" and self._deadline is not None:
                remaining_ms = max(
                    0, round((self._deadline - time.monotonic()) * 1000)
                )
        try:
            free_bytes = shutil.disk_usage(
                next(
                    p
                    for p in [Path(settings.save_dir), *Path(
                        settings.save_dir
                    ).parents]
                    if p.exists()
                )
            ).free
        except (StopIteration, OSError):
            free_bytes = 0
        return {
            "state": self._state,
            "remaining_ms": remaining_ms,
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
