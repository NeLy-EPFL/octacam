"""Recording orchestration shared by the web UI and the headless CLI.

A framework-free state machine on top of a CameraSystem:

    preview/idle -> waiting -> recording -> finishing -> preview/idle

Each recording's monitor thread waits for every camera's first frame (plugin
``on_first_frame`` hooks fire then), enforces the deadline and tears down in a
fixed order: trigger off -> grab loops exit -> writers drain -> summary.

``RecordingController._lock`` guards the state and settings, and snapshot(),
stop_recording() and the telemetry take it too. So nothing that can block runs
under it: plugin hooks (serial writes awaiting an ack), node-map reads over USB
and the NVENC probe all run off the lock, or one stalled device would wedge the
GUI's status and its Stop button.
"""

import contextlib
import dataclasses
import datetime
import json
import logging
import os
import shutil
import threading
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Literal, NamedTuple

import numpy as np
from pydantic import Field, PositiveFloat, StrictInt, TypeAdapter, ValidationError

from octacam import config_writer, session_cache
from octacam.cameras import CameraSystem
from octacam.config import (
    FfmpegArgs,
    OctacamConfig,
    PreviewTriggerSource,
    SaveMethod,
    TriggerSource,
    compose_save_dir,
    duration_to_seconds,
    increment_trailing_number,
    normalize_dir,
    resolve_save_path,
    safe_segment,
)
from octacam.plugins.base import PluginManager
from octacam.pulses import PulseClock
from octacam.transform import (
    CONFIG_SNAPSHOT_FILENAME,
    RECORDING_INFO_DIRNAME,
    RECORDING_SUMMARY_FILENAME,
    TIMESTAMPS_FILENAME,
    DisplayTransform,
)
from octacam.writer import (
    DEFAULT_FFMPEG_PARAMS,
    DEFAULT_TRANSCODE_FFMPEG_PARAMS,
    FORMATS,
    NVENC_H264_PARAMS,
    VideoFormat,
    encoder_of,
    nvenc_max_sessions,
    resolve_capture_formats,
)

log = logging.getLogger("octacam")

STARTED_POLL_INTERVAL_S = 0.1
STARTED_WARN_AFTER_S = 3.0
STARTED_FAIL_AFTER_S = 10.0  # then record with whatever cameras started
STOP_GRACE_S = 0.5  # past the duration, for frames still in flight
# Bounds the monitor's wait for the start hooks, so a wedged arm (a serial write
# stalled on its write_timeout) never blocks teardown; a primed recording adds
# its priming (start_sequence_timeout_s).
START_HOOKS_TIMEOUT_S = 5.0
# Sacrificial triggers per priming round (octacam-driven trigger sources only):
# a GS3 ignores its first triggers after an acquisition start (CLAUDE.md,
# hardware quirks). Their frames are discarded.
PRIME_PULSES = 4
# Priming repeats rounds until every camera has answered one, but starts none
# after this long; a camera still silent then is warned about.
PRIME_BUDGET_S = 1.0
# Settle after a priming round before counting starts: above a frame's
# trigger-to-delivery latency and, at a low fps, PRIME_SETTLE_PERIODS periods (a
# long exposure delivers late; a priming straggler is dropped only within a few
# periods of the last primed frame).
PRIME_SETTLE_S = 0.15
PRIME_SETTLE_PERIODS = 4
# A counted train is over this long after its last pulse was due (a GS3
# delivers ~10-30 ms after its pulse); the take then stops on the train's end.
TRAIN_END_MARGIN_S = 0.3
# Summary lists of per-pulse indices are capped at this length (the full series
# is in timestamps.npz); the counts next to them are never capped.
SUMMARY_INDEX_LIMIT = 1000


@dataclass
class RecordingSettings:
    """The live Record and Process settings; :meth:`updated` checks the
    constraints declared here."""

    fps: PositiveFloat = 100.0
    duration_s: PositiveFloat = 20.0
    save_dir: str = "./"
    # save_dir is record_directory/relative_directory once either is set; the
    # transfer mirrors relative_directory (else save_dir's basename).
    record_directory: str = ""
    relative_directory: str = ""
    trigger_source: TriggerSource = "software"
    preview_trigger_source: PreviewTriggerSource = "auto"
    save_method: SaveMethod = "ffmpeg"
    # Encoder args per method, kept apart so switching keeps both presets.
    ffmpeg_params: FfmpegArgs = DEFAULT_FFMPEG_PARAMS
    nvenc_params: FfmpegArgs = NVENC_H264_PARAMS
    # None = the GPU's detected session cap; cameras beyond it encode on CPU.
    max_nvenc_sessions: Annotated[StrictInt, Field(ge=0)] | None = None
    writer_queue_size: Annotated[StrictInt, Field(ge=1)] = 64
    # "display" bakes the display transform into the video; "sensor" does not.
    record_form: Literal["display", "sensor"] = "display"
    save_frame_timestamps: bool = False
    # `octacam process` params: unused during capture, patched into each
    # recording's config snapshot. An empty transfer_directory skips the transfer.
    transcode_ffmpeg_params: FfmpegArgs = DEFAULT_TRANSCODE_FFMPEG_PARAMS
    transfer_directory: str = ""
    transfer_checksum: bool = True

    @classmethod
    def from_config(
        cls, config: OctacamConfig, *, fps: float | None = None
    ) -> "RecordingSettings":
        """The settings ``config`` loads as (its tolerance stands: nothing is
        re-checked), with the save dirs resolved now. ``fps`` overrides the
        config's, before a frame-count duration converts at it."""
        record, transfer = config.record, config.transfer
        fps = record.fps if fps is None else fps
        path = resolve_save_path(record)
        return cls(
            fps=fps,
            duration_s=duration_to_seconds(record.duration, record.duration_unit, fps),
            save_dir=path.save_dir,
            record_directory=path.directory,
            relative_directory=path.relative,
            trigger_source=record.trigger_source,
            preview_trigger_source=record.preview_trigger_source,
            save_method=record.save_method,
            ffmpeg_params=record.ffmpeg_params,
            nvenc_params=record.nvenc_params,
            max_nvenc_sessions=record.max_nvenc_sessions,
            writer_queue_size=record.writer_queue_size,
            record_form="display" if record.save_transformed else "sensor",
            save_frame_timestamps=record.save_timestamps,
            transcode_ffmpeg_params=config.transcode.ffmpeg_params,
            transfer_directory=transfer.directory if transfer else "",
            transfer_checksum=transfer.checksum if transfer else True,
        )

    def record_config_values(self) -> dict:
        """The settings as ``[record]`` keys, the inverse of :meth:`from_config`
        with ``duration_s`` for ``duration``/``duration_unit``
        (config_writer.with_record_settings). The save path is left out: a
        snapshot keeps the config's templates so a relaunch resolves a fresh
        folder, and the path a recording used is in its summary."""
        return {
            "fps": self.fps,
            "duration_s": self.duration_s,
            "trigger_source": self.trigger_source,
            "preview_trigger_source": self.preview_trigger_source,
            "save_method": self.save_method,
            "ffmpeg_params": self.ffmpeg_params,
            "nvenc_params": self.nvenc_params,
            "max_nvenc_sessions": self.max_nvenc_sessions,
            "writer_queue_size": self.writer_queue_size,
            "save_transformed": self.record_form == "display",
            "save_timestamps": self.save_frame_timestamps,
        }

    def updated(self, /, **changes) -> "RecordingSettings":
        """A copy with ``changes`` validated and applied, else ValueError naming
        each bad field. A ``record_directory`` or ``relative_directory`` edit
        recomposes save_dir from the split; a lone ``save_dir`` clears it."""
        unknown = changes.keys() - _SETTINGS_FIELDS
        if unknown:
            raise ValueError(f"Unknown settings: {sorted(unknown)}")
        try:
            # Only the changed fields: a config value the GUI would refuse
            # (fps 0) must not block editing another one.
            valid = _SETTINGS.validate_python(changes)
        except ValidationError as e:
            raise ValueError(
                "; ".join(f"{err['loc'][0]}: {err['msg']}" for err in e.errors())
            ) from None
        new = dataclasses.replace(self, **{key: getattr(valid, key) for key in changes})
        if "record_directory" in changes:
            new.record_directory = normalize_dir(new.record_directory)
        if "record_directory" in changes or "relative_directory" in changes:
            new.save_dir = new._composed_save_dir()
        elif "save_dir" in changes:
            new = new.with_save_dir(new.save_dir)
        return new

    def video_format(self) -> VideoFormat:
        video_format = FORMATS[self.save_method]
        if self.save_method == "ffmpeg":
            params = self.ffmpeg_params
        elif self.save_method == "nvenc":
            params = self.nvenc_params
        else:
            return video_format
        return dataclasses.replace(video_format, ffmpeg_params=params)

    def with_save_dir(self, path: str) -> "RecordingSettings":
        """An explicit save dir (``--output``, a lone GUI edit). It clears the
        split, or the transfer and next_take would recompose the old path."""
        return dataclasses.replace(
            self,
            save_dir=normalize_dir(path),
            record_directory="",
            relative_directory="",
        )

    def next_take(self) -> "RecordingSettings":
        """The next recording's folder: the relative part's trailing number
        bumped, else save_dir's."""
        if not self.relative_directory.strip():
            return dataclasses.replace(
                self, save_dir=increment_trailing_number(self.save_dir)
            )
        new = dataclasses.replace(
            self, relative_directory=increment_trailing_number(self.relative_directory)
        )
        new.save_dir = new._composed_save_dir()
        return new

    def _composed_save_dir(self) -> str:
        # A live edit and the next take join the relative part stripped; the
        # first take (resolve_save_path) joins it as the config wrote it.
        return compose_save_dir(self.record_directory, self.relative_directory.strip())

    def relative_save_dir(self) -> str:
        """The recording folder relative to the base directory: the explicit
        ``relative_directory``, else save_dir relative to the base, else (no
        base, or a folder outside it such as an --output override) its name."""
        if self.relative_directory.strip():
            return self.relative_directory
        if self.record_directory:
            try:
                rel = os.path.relpath(self.save_dir, self.record_directory)
                if not rel.startswith(".."):
                    return rel
            except ValueError:  # e.g. different drives on Windows
                pass
        return Path(self.save_dir).name


_SETTINGS = TypeAdapter(RecordingSettings)
_SETTINGS_FIELDS = frozenset(f.name for f in dataclasses.fields(RecordingSettings))


def capture_frame_count(settings: RecordingSettings) -> int | None:
    """``round(fps * duration)``, the pulses an octacam-driven trigger emits, so
    no camera's grab loop takes a trailing pulse the others miss at teardown.

    None (uncapped, bounded by the deadline) for an ``external`` trigger, whose
    pulse count is unknown, and for a non-positive fps or duration."""
    if settings.trigger_source == "external":
        return None
    if settings.fps <= 0 or settings.duration_s <= 0:
        return None
    return max(1, round(settings.fps * settings.duration_s))


def resolve_pulse_clock(
    settings: RecordingSettings,
    plugins: PluginManager | None = None,
    plugin_params: dict | None = None,
) -> PulseClock:
    """The trigger train a recording's frames are counted against.

    ``managed``: the train the driving plugin will emit (``trigger_train``), else
    one derived from the settings; ``software``: octacam's own timer. Both are
    filled (a missed pulse repeats the previous frame, so video frame k is pulse
    k in every camera). ``external`` has no known length and is never filled: an
    external clock may be irregular by design.
    """
    period = int(round(1e9 / settings.fps)) if settings.fps > 0 else 0
    if settings.trigger_source == "external":
        return PulseClock(period, None, "external", fill=False)
    if settings.trigger_source == "managed" and plugins is not None:
        train = plugins.trigger_train(plugin_params)
        if train:
            return PulseClock(int(train["period_ns"]), int(train["count"]), "managed")
    return PulseClock(period, capture_frame_count(settings), settings.trigger_source)


def prime_settle_s(period_ns: int) -> float:
    """How long priming waits after a round for its frames to land: at least
    PRIME_SETTLE_S, and PRIME_SETTLE_PERIODS periods at a low fps."""
    return max(PRIME_SETTLE_S, PRIME_SETTLE_PERIODS * period_ns / 1e9)


def start_sequence_timeout_s(period_ns: int, primed: bool) -> float:
    """How long the monitor waits for a recording's start sequence (priming,
    then the arm): START_HOOKS_TIMEOUT_S plus, when primed, the priming's upper
    bound (rounds start until PRIME_BUDGET_S, and the last sends PRIME_PULSES a
    period apart and settles; at 1 fps that round alone is 8 s)."""
    if not primed:
        return START_HOOKS_TIMEOUT_S
    priming = PRIME_BUDGET_S + PRIME_PULSES * period_ns / 1e9 + prime_settle_s(period_ns)
    return START_HOOKS_TIMEOUT_S + priming


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
    "Every frame is assigned to the trigger pulse that exposed it, from the "
    "camera's hardware timestamps (or, under a software trigger, the trigger's "
    "sequence number). `missed_pulses` lists pulses the camera delivered no frame "
    "for (a missed trigger, or a frame lost in transport — `stream` shows the "
    "SDK's own loss counters) and `writer_dropped` counts frames the writer queue "
    "could not accept. On an octacam-driven train (software/managed) both are "
    "filled with the previous frame, so video frame k is pulse k in every camera "
    "and `dropped`/`dropped_indices` count/list those filled frames; "
    "timestamps.npz marks them per frame (`dropped`, `missed`) with each frame's "
    "`pulse_index`. On an external trigger missed pulses are reported only. "
    "`writer_skipped` counts frames the writer could not accept that were skipped "
    "instead of filled (a sustained encoder or disk shortfall): after one, video "
    "frame k is no longer pulse k, so map frames to pulses with `pulse_index`. "
    "`late_pulse_indices` are frames exposed markedly after their pulse."
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
    """``"hardware"``, ``"host"`` (a backend without timestamps, e.g.
    pycameleon) or ``"mixed"`` (stray zero timestamps); None without frames."""
    if frames <= 0:
        return None
    if host_fallback_count <= 0:
        return "hardware"
    if host_fallback_count >= frames:
        return "host"
    return "mixed"


def _capped(indices: list[int]) -> list[int]:
    return list(indices[:SUMMARY_INDEX_LIMIT])


class DeliveryProfile(NamedTuple):
    """What a frame's trigger-to-host delay depends on (see
    RecordingController._check_sync)."""

    backend: str
    model: str | None
    width: int
    height: int
    pixel_format: str
    exposure_us: int


# The profile fields an operator can act on, and how the sync note shows each.
_PROFILE_FIELDS = {
    "camera backend": lambda p: p.backend,
    "model": lambda p: p.model or "unknown",
    "frame size": lambda p: f"{p.width}×{p.height}",
    "pixel format": lambda p: p.pixel_format,
    "exposure": lambda p: f"{p.exposure_us} µs",
}


def _unlike_profiles_note(groups: dict[DeliveryProfile | str, list[str]]) -> str:
    """The note for cameras whose start alignment could not be compared.

    *groups* maps a delivery profile — or the name of a camera whose profile
    could not be read — to the cameras that share it. The note names only the
    fields that differ, so an operator can tell a deliberate difference (two
    ROIs) from an accidental one (a mistyped exposure)."""

    def label(members: list[str]) -> str:
        return members[0] if len(members) == 1 else f"[{', '.join(members)}]"

    read = [
        (key, members)
        for key, members in groups.items()
        if isinstance(key, DeliveryProfile)
    ]
    unread = [key for key in groups if isinstance(key, str)]
    differences = []
    for field, show in _PROFILE_FIELDS.items():
        values = [(members, show(key)) for key, members in read]
        if len({value for _members, value in values}) > 1:
            shown = ", ".join(f"{label(members)} {value}" for members, value in values)
            differences.append(f"{field} ({shown})")
    reasons = []
    if differences:
        reasons.append("they differ in " + "; ".join(differences))
    if unread:
        reasons.append(f"the delivery profile of {', '.join(unread)} could not be read")
    listing = " vs ".join(f"[{', '.join(members)}]" for members in groups.values())
    return (
        f"Start alignment of {listing} was not checked (informational, not an "
        f"error): {' and '.join(reasons)}, so their frames reach the host after "
        "different delays. Only cameras alike in model, frame size, pixel format "
        "and exposure are compared."
    )


def build_recording_summary(
    settings: RecordingSettings,
    cameras,
    start_wall_ns: int,
    aborted: bool,
    pulse_clock: PulseClock | None = None,
    sync: dict | None = None,
    primed_pulses: int = 0,
    completed: bool | None = None,
) -> dict:
    """The recording_summary.json payload, from finished camera stats (no I/O).

    ``transform_applied`` is true only when the transform was baked into the
    video (display form and a non-identity transform)."""
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
        missed, late = camera.missed_pulses, camera.late_pulses
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
                "dropped_indices": _capped(camera.dropped_indices),
                "missed_pulses": len(missed),
                "missed_pulse_indices": _capped(missed),
                "writer_dropped": camera.writer_dropped,
                "writer_skipped": camera.writer_skipped,
                "writer_skipped_pulse_indices": _capped(camera.writer_skipped_pulses),
                "late_frames": len(late),
                "late_pulse_indices": _capped(late),
                "extra_frames": camera.extra_frames,
                "primed_frames": camera.primed_frames,
                "clock_mismatch": camera.clock_mismatch,
                "timestamp_glitches": camera.timestamp_glitches,
                "unclocked_frames": camera.unclocked_frames,
                "stream": camera.stream_statistics,
                "start_offset_pulses": (sync or {}).get("start_offsets", {}).get(
                    camera.name
                ),
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
    summary = {
        "schema_version": 4,
        "start_time": start_iso,
        "start_time_ns": start_wall_ns or None,
        "aborted": aborted,
        # Ran to its end, not stopped: octacam check tells an early stop from
        # cameras that disagree by it.
        "completed": completed,
        "fps_target": settings.fps,
        "duration_s": settings.duration_s,
        "trigger_source": settings.trigger_source,
        "save_method": settings.save_method,
        # The args of the encoder used (nvenc's for save_method="nvenc").
        "ffmpeg_params": settings.video_format().ffmpeg_params,
        "record_form": settings.record_form,
        # Resolved now, so a transfer on a later day never re-templates the date.
        "relative_directory": settings.relative_save_dir(),
        "pulse_train": (
            {**pulse_clock.to_dict(), "primed": primed_pulses}
            if pulse_clock is not None
            else None
        ),
        "sync": {
            "ok": (sync or {}).get("ok", True),
            "warnings": list((sync or {}).get("warnings", [])),
            "notes": list((sync or {}).get("notes", [])),
        },
        "dropped_frames_note": _DROPPED_FRAMES_NOTE,
        "timestamp_note": _TIMESTAMP_NOTE,
        "cameras": cams,
    }
    if settings.save_method == "nvenc":
        # Cameras beyond the cap encoded on CPU. Auto (None) resolves to the cap
        # the off-lock warm-up cached for this encoder: never a fresh GPU probe.
        cap = settings.max_nvenc_sessions
        if cap is None:
            encoder = encoder_of(settings.video_format().ffmpeg_params) or "h264_nvenc"
            cap = nvenc_max_sessions(encoder)
        summary["max_nvenc_sessions"] = cap
    return summary


def build_timestamps_arrays(cameras) -> dict[str, np.ndarray]:
    """The ``timestamps.npz`` arrays, from finished camera stats (no I/O).

    Per camera, one entry per video frame: ``"<name>/timestamp_ns"`` (int64; a
    fill carries the time its pulse was due), ``"<name>/dropped"`` (bool: a
    fill), ``"<name>/missed"`` (bool: of those, a pulse the camera never
    delivered), ``"<name>/pulse_index"`` (int64) and ``"<name>/arrival_ns"``
    (int64: host wall-clock delivery, 0 for a fill). A camera's series are
    truncated to their shortest."""
    arrays: dict[str, np.ndarray] = {}
    for camera in cameras:
        series = {
            "timestamp_ns": (camera.frame_timestamps, np.int64),
            "dropped": (camera.frame_dropped, bool),
            "missed": (camera.frame_missed, bool),
            "pulse_index": (camera.frame_pulse_index, np.int64),
            "arrival_ns": (camera.frame_arrival_ns, np.int64),
        }
        n = min(len(values) for values, _dtype in series.values())
        for key, (values, dtype) in series.items():
            arrays[f"{camera.name}/{key}"] = np.asarray(values[:n], dtype=dtype)
    return arrays


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
        # Its octacam_config.toml is snapshotted into each recording; None (as
        # in tests) records without a snapshot.
        self._config_dir = Path(config_dir) if config_dir is not None else None
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
        self._aborted = False
        self._stop_event = threading.Event()
        # Set when a recording's start sequence (priming, arm) is done. Minted per
        # recording and captured by its monitor, which waits on it before any
        # later hook or teardown: a stop never overtakes the arm.
        self._start_hooks_done = threading.Event()
        self._monitor: threading.Thread | None = None
        self._deadline: float | None = None
        # The current or last recording's start time and results, for its summary.
        self._recording_start_wall_ns = 0
        self._pulse_clock: PulseClock | None = None
        self._primed_pulses = 0
        self._sync: dict | None = None
        self._completed: bool | None = None  # ran to its end (None: no take yet)
        self._recording_cameras: list[str] = []  # whose record grab started
        self._delivery_profiles: dict[str, DeliveryProfile | None] = {}  # by serial
        self._train_end: float | None = None  # monotonic, once the train started
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
        if self.recording_active:
            return "Recording in progress"
        if self.diagnosing:
            if benchmark:
                return "A benchmark is already running"
            return "A benchmark is in progress"
        if self._reconfiguring:
            return "Camera reconfiguration in progress"
        if self._tearing_down:
            return "Previous recording is still finishing"
        if self._starting:
            if benchmark:
                return "A recording is starting"
            return "Recording is already starting"
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
        pre_params: dict[str, str] | None = None
        profiles: dict[str, DeliveryProfile | None] | None = None
        if not self._camera_locked:
            pre_params = self._export_camera_params()
            profiles = self._read_delivery_profiles()
        with self._lock:
            busy = self._busy_reason()
            if busy:
                return StartResult(StartResult.BUSY, busy)
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
                profiles = self._read_delivery_profiles()
            return self._start_recording_admitted(plugin_params, pre_params, profiles)
        finally:
            with self._lock:
                self._starting = False

    def _start_recording_admitted(
        self,
        plugin_params: dict | None,
        pre_params: dict[str, str] | None,
        profiles: dict[str, DeliveryProfile | None],
    ) -> StartResult:
        """Second half of :meth:`start_recording`, under the caller's
        ``_starting`` gate: start the record grab under the lock, then prime and
        arm off it. ``pre_params`` is the caller's parameter export, or None.
        """
        failed_resume_mode: str | None = None
        hooks_done: threading.Event | None = None
        with self._lock:
            settings = self._settings
            save_dir = Path(settings.save_dir)

            use_software_trigger = settings.trigger_source == "software"
            clock = resolve_pulse_clock(settings, self.plugins, plugin_params)
            prime = settings.trigger_source in ("software", "managed")
            self._pulse_clock = clock
            self._primed_pulses = 0
            self._sync = None
            self._completed = None
            self._train_end = None
            self._recording_cameras = []
            self._delivery_profiles = profiles
            hooks_timeout_s = start_sequence_timeout_s(clock.period_ns, prime)
            # Under the lock only in the rare race where the export was skipped.
            camera_params = (
                pre_params if pre_params is not None else self._export_camera_params()
            )
            start_error: str | None = None
            try:
                self.camera_system.stop_software_trigger()
                self.camera_system.enable_frame_trigger()
                self.camera_system.set_trigger_source(use_software_trigger)
                self.camera_system.set_software_trigger_frequency(settings.fps)
                self._recording_start_wall_ns = time.time_ns()
                # One format per camera: cameras past the NVENC session cap (or
                # all, without NVENC) encode on CPU, and the operator is told.
                formats, fmt_warnings = resolve_capture_formats(
                    settings.video_format(),
                    len(self.camera_system),
                    settings.max_nvenc_sessions,
                )
                for message in fmt_warnings:
                    self._event("warning", message)
                started = self.camera_system.start_record(
                    save_dir,
                    settings.fps,
                    formats,
                    settings.record_form,
                    use_software_trigger=use_software_trigger,
                    writer_queue_size=settings.writer_queue_size,
                    pulse_clock=clock,
                    hold=prime,
                )
            except Exception as e:
                # Some cameras may already be writing with no monitor to stop
                # them: stop them all and fall into the preview re-arm below.
                log.exception("Recording failed to start")
                with contextlib.suppress(Exception):
                    self.camera_system.stop()
                start_error = f"Recording failed to start: {e}"
                started = None
            total = len(self.camera_system)
            if not started:
                if start_error is None:
                    self._event("error", "No camera could start recording")
                failed_resume_mode = self._resume_preview()
            else:
                self._recording_cameras = list(started)
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

                # A provisional summary (aborted, no frames) now, so a raw take
                # keeps its geometry and stays transcodable if the process dies
                # before teardown rewrites it.
                self._snapshot_config(plugin_params, camera_params)
                self._write_recording_summary(aborted=True)

                self._aborted = False
                self._stop_event.clear()
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
                        hooks_timeout_s,
                    ),
                    daemon=True,
                )
                self._monitor.start()
        if not started:
            self._dispatch_preview_arm(failed_resume_mode)
            return StartResult(
                StartResult.ERROR, start_error or "No camera could start recording"
            )
        # Prime, then count, then start the train, all before hooks_done. Each
        # step is skipped once a stop came in, so the hardware is never armed
        # after the cameras stopped.
        try:
            if prime and not self._stop_event.is_set():
                self._prime_cameras(settings, plugin_params, started, clock.period_ns)
                self.camera_system.arm_counting()
            if not self._stop_event.is_set():
                if use_software_trigger:
                    self.camera_system.start_software_trigger(settings.duration_s)
                self.plugins.on_recording_start(plugin_params)
                # Anchored after the arm returns: it can take seconds (a board's
                # USB-reset recovery), which an earlier anchor cuts from the take.
                if clock.count and clock.fill:
                    self._train_end = (
                        time.monotonic()
                        + clock.count * clock.period_ns / 1e9
                        + TRAIN_END_MARGIN_S
                    )
        finally:
            # Ordering, not success: set this recording's own event regardless.
            if hooks_done is not None:
                hooks_done.set()
        return StartResult(StartResult.OK)

    def _prime_cameras(
        self,
        settings: RecordingSettings,
        plugin_params: dict | None,
        recording: list[str],
        period_ns: int,
    ) -> None:
        """Send sacrificial triggers until every camera in ``recording`` has
        answered one, so the triggers a camera ignores after its acquisition
        start (see PRIME_PULSES) are behind it when the train starts.

        Rounds of PRIME_PULSES repeat within PRIME_BUDGET_S, each settling for
        :func:`prime_settle_s`; the record grabs discard what they produce."""
        deadline = time.monotonic() + PRIME_BUDGET_S
        settle = prime_settle_s(period_ns)
        sent = 0
        while not self._stop_event.is_set():
            if settings.trigger_source == "software":
                self.camera_system.prime_software_trigger(PRIME_PULSES, settings.fps)
            elif not self.plugins.prime_trigger(plugin_params, PRIME_PULSES):
                # A burst the board never acknowledged may still land: let it
                # land under the hold, not in the count.
                self._stop_event.wait(settle)
                break
            sent += PRIME_PULSES
            self._stop_event.wait(settle)
            if not self._unprimed_cameras(recording) or time.monotonic() >= deadline:
                break
        self._primed_pulses = sent
        if self._stop_event.is_set():
            return
        if not sent:
            self._event(
                "warning",
                "The trigger source could not prime the cameras; a camera that "
                "ignores its first triggers after acquisition start (e.g. a FLIR "
                "Grasshopper3) will start this recording a few pulses late",
            )
            return
        silent = self._unprimed_cameras(recording)
        if silent:
            names = ", ".join(silent)
            self._event(
                "warning",
                f"Camera{'s' if len(silent) > 1 else ''} {names} answered none of "
                f"the {sent} priming pulses: {'they' if len(silent) > 1 else 'it'} "
                "may start this recording a few pulses late, or not be receiving "
                "the trigger",
            )

    def _unprimed_cameras(self, recording: list[str]) -> list[str]:
        """Recording cameras that have answered no priming pulse yet (a camera
        whose record grab failed to start can never answer one)."""
        return [
            camera.name
            for camera in self.camera_system
            if camera.name in recording and camera.primed_frames == 0
        ]

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

    def _monitor_loop(
        self,
        duration_s: float,
        plugin_params: dict | None,
        expected_started: int,
        external_trigger: bool,
        hooks_done: threading.Event,
        hooks_timeout_s: float,
    ) -> None:
        """Run the recording monitor; on any exception still disarm the trigger
        plugin and go idle, or the controller would stay recording forever."""
        try:
            self._run_monitor_loop(
                duration_s,
                plugin_params,
                expected_started,
                external_trigger,
                hooks_done,
                hooks_timeout_s,
            )
        except Exception:
            log.exception("Recording monitor crashed; forcing idle")
            with contextlib.suppress(Exception):
                self.plugins.on_recording_stop(self._aborted)
            with self._lock:
                self._tearing_down = False
                self._set_state("idle")
            self._event("error", "Recording monitor crashed (see log); forced idle")

    def _run_monitor_loop(
        self,
        duration_s: float,
        plugin_params: dict | None,
        expected_started: int,
        external_trigger: bool,
        hooks_done: threading.Event,
        hooks_timeout_s: float,
    ) -> None:
        # Wait for every started camera's first frame. Under the software
        # trigger, give up after STARTED_FAIL_AFTER_S and record with what
        # started, so one stalled camera cannot hang the take; under a hardware
        # trigger (managed or external: ``external_trigger``) the first pulse
        # may come arbitrarily late, so wait until stopped. Both thresholds
        # count from the end of the start sequence: no counted frame comes
        # before it, and at a low fps priming alone outlasts them.
        start = time.monotonic()
        armed_at: float | None = None
        warned = False
        while not self._stop_event.is_set():
            if self._count_started() >= expected_started:
                break
            now = time.monotonic()
            if armed_at is None and (
                hooks_done.is_set() or now - start >= hooks_timeout_s
            ):
                armed_at = now
            elapsed = now - armed_at if armed_at is not None else 0.0
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
            # First-frame hooks (flywheel motion) start the countdown, never
            # before the arm.
            hooks_done.wait(hooks_timeout_s)
            self.plugins.on_first_frame(plugin_params)
            with self._lock:
                deadline = time.monotonic() + duration_s + STOP_GRACE_S
                self._deadline = deadline
                self._recording_seq += 1
                self._set_state("recording")
            # --- countdown. A counted train ends on its pulse count: as soon as
            # every camera has the train's last pulse, or once the train is over
            # (a camera that missed its last pulses cannot know it before then);
            # the duration's deadline is only the backstop.
            reported: dict[str, int] = {}
            while not self._stop_event.is_set():
                if self.camera_system.all_pulses_complete:
                    break
                train_end = self._train_end
                if train_end is not None and time.monotonic() >= train_end:
                    break
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                self._report_missed_pulses(reported)
                self._stop_event.wait(min(remaining, 0.2))

        # Teardown: trigger off -> grab loops exit -> writers drain -> summary.
        # The start sequence finishes first, so a stop never leaves a trigger
        # running.
        completed = not self._stop_event.is_set()
        self._completed = completed
        hooks_done.wait(hooks_timeout_s)
        with self._lock:
            self._set_state("finishing")
        self.camera_system.stop_software_trigger()
        # A completed train pads every video to its pulse count (a camera that
        # missed the last pulses still ends on the last pulse); a stopped take
        # ends where it was.
        clock = self._pulse_clock
        fill_to = clock.count if completed and clock is not None and clock.fill else None
        self.camera_system.stop(fill_to)
        self._sync = self._check_sync(completed)
        for message in self._sync["warnings"]:
            self._event("warning", message)
        for message in self._sync["notes"]:
            self._event("info", message)
        for camera in self.camera_system:
            if camera.writer_failed:
                self._event(
                    "error",
                    f"Writer for camera {camera.name} failed during the "
                    "recording (see log for ffmpeg output)",
                )

        # A camera without frames wrote an empty file and its writer did not
        # fail, so say it here (usually an external trigger that never fired).
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

        # The grab threads are joined (stats final) and save_dir is still this
        # recording's (it is incremented below).
        self._write_recording_summary(self._aborted)
        if self._settings.save_frame_timestamps:
            self._write_timestamps()
        self._note_in_session_cache()

        # The state leaves the active set below, before the off-lock disarm and
        # preview re-arm: `_tearing_down` covers that tail.
        with self._lock:
            self._tearing_down = True
        try:
            with self._lock:
                self._deadline = None
                aborted = self._aborted
                if not aborted:
                    self._settings = self._settings.next_take()
                # A camera dropped mid-recording can make the re-arm raise: go
                # idle rather than stay "finishing", and still disarm below.
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
            hooks_done.wait(hooks_timeout_s)
            self.plugins.on_recording_stop(aborted)
            # After the recording's cancel, so the record arm is torn down first.
            self._dispatch_preview_arm(resume_mode)
            self._event(
                "info", "Recording aborted" if aborted else "Recording finished"
            )
        finally:
            with self._lock:
                self._tearing_down = False

    def _report_missed_pulses(self, reported: dict[str, int]) -> None:
        """Tell the operator, while recording, that a camera is missing pulses."""
        for camera in self.camera_system:
            missed = camera.missed_pulses
            before = reported.get(camera.name, 0)
            if len(missed) > before:
                reported[camera.name] = len(missed)
                new = missed[before:]
                shown = ", ".join(str(p) for p in new[:5]) + (" …" if len(new) > 5 else "")
                self._event(
                    "warning",
                    f"Camera {camera.name} missed trigger pulse(s) {shown} "
                    f"({len(missed)} so far)"
                    + (
                        "; filled with the previous frame to keep the cameras aligned"
                        if camera.pulse_clock is not None and camera.pulse_clock.fill
                        else ""
                    ),
                )

    def _check_sync(self, completed: bool) -> dict:
        """Whether frame k is the same trigger pulse in every camera, and why not.

        Fills keep the cameras aligned, so missed pulses and writer drops are
        reported without breaking sync. What breaks it: frames off the trigger
        clock or without timestamps, unfilled misses (external trigger), writer
        skips, a camera without frames, unequal ends after a completed train,
        and a camera that started late.

        A late start shows in when the first frames reached the host, which is
        comparable only between cameras of one delivery profile (a 2048² GS3
        lands ~4.5 ms after an acA1920): those deliver a pulse within ~0.5 ms of
        each other, so a pulse late is a whole period. Across profiles a note
        says the start was not checked.
        """
        clock = self._pulse_clock
        warnings: list[str] = []
        notes: list[str] = []
        offsets: dict[str, int | None] = {}
        ok = True
        not_started = [
            c.name for c in self.camera_system if c.name not in self._recording_cameras
        ]
        if not_started:
            ok = False
            warnings.append(
                f"Camera(s) {', '.join(not_started)} did not start recording: the take "
                "has no video from them"
            )
        silent = [
            c.name
            for c in self.camera_system
            if c.name in self._recording_cameras and not c.frames_recorded
        ]
        if silent:
            ok = False
            warnings.append(
                f"Camera(s) {', '.join(silent)} recorded no frame: there is no video "
                "to align with the other cameras"
            )
        cams = [c for c in self.camera_system if c.frames_recorded]
        for camera in cams:
            name = camera.name
            missed = camera.missed_pulses
            if missed:
                shown = ", ".join(str(p) for p in missed[:10])
                more = f" and {len(missed) - 10} more" if len(missed) > 10 else ""
                if clock is not None and clock.fill:
                    warnings.append(
                        f"Camera {name} missed {len(missed)} trigger pulse(s) "
                        f"({shown}{more}); each was filled with the previous frame"
                    )
                else:
                    ok = False
                    warnings.append(
                        f"Camera {name} missed {len(missed)} trigger pulse(s) "
                        f"({shown}{more}); on an external trigger they are not "
                        "filled — map frames to pulses with pulse_index in "
                        f"{TIMESTAMPS_FILENAME}"
                    )
            if camera.writer_dropped:
                warnings.append(
                    f"Camera {name}: {camera.writer_dropped} frame(s) arrived while "
                    "the writer queue was full and were filled with the previous "
                    "frame (the encoder could not keep up)"
                )
            if camera.writer_skipped:
                ok = False
                warnings.append(
                    f"Camera {name}: {camera.writer_skipped} frame(s) the writer "
                    "could not accept were skipped, not filled (the encoder or disk "
                    "could not keep up), so video frame k is no longer pulse k; map "
                    f"frames to pulses with pulse_index in {TIMESTAMPS_FILENAME}"
                )
            if camera.extra_frames:
                warnings.append(
                    f"Camera {name}: {camera.extra_frames} frame(s) belonged to no "
                    "pulse of the train and were discarded"
                )
            if camera.clock_mismatch:
                ok = False
                warnings.append(
                    f"Camera {name}: its frames did not follow the trigger clock's "
                    "period (is it free-running? A trigger pulse longer than the "
                    "exposure re-triggers some cameras at their readout limit)"
                )
            if camera.unclocked_frames:
                ok = False
                real_frames = camera.frames_recorded - len(camera.dropped_indices)
                if camera.unclocked_frames < real_frames:
                    # A stray frame without a timestamp on a clocked camera: it
                    # was placed as the next pulse, so only a miss right before
                    # it could go unseen.
                    warnings.append(
                        f"Camera {name}: {camera.unclocked_frames} frame(s) had no "
                        "hardware timestamp and were placed as the next pulse; a "
                        "pulse missed just before one of them cannot be detected"
                    )
                else:
                    warnings.append(
                        f"Camera {name}: {camera.unclocked_frames} frame(s) had no "
                        "hardware timestamp, so missed pulses cannot be detected"
                    )
            for glitch in camera.timestamp_glitches:
                warnings.append(
                    f"Camera {name}: its hardware clock jumped by "
                    f"{glitch['jump_ns'] / 1e9:+.6f} s at pulse {glitch['pulse']} "
                    "(corrected; not a lost frame)"
                )
        if completed and clock is not None and clock.fill and clock.count:
            short = [c.name for c in cams if c.frames_recorded != clock.count]
            if short:
                ok = False
                warnings.append(
                    f"Camera(s) {', '.join(short)} did not end on the train's last "
                    f"pulse ({clock.count} expected)"
                )
        period = clock.period_ns if clock is not None else 0
        if clock is not None and clock.source == "software":
            # Frame 0 answers trigger 0 in every camera by construction; a camera
            # that merely delivers later must not read as late.
            for camera in cams:
                offsets[camera.name] = 0
            return {"ok": ok, "warnings": warnings, "notes": notes, "start_offsets": offsets}
        delays: dict[str, float] = {}
        for camera in cams:
            rows = [
                (arrival, pulse)
                for arrival, pulse in zip(
                    camera.frame_arrival_ns, camera.frame_pulse_index, strict=False
                )
                if arrival
            ][:16]
            if period and len(rows) >= 4:
                delays[camera.name] = float(
                    np.median([arrival - pulse * period for arrival, pulse in rows])
                )
        # Group the cameras by delivery profile; one whose profile could not be
        # read is a group of its own, under its name, and compared with none.
        groups: dict[DeliveryProfile | str, list[str]] = {}
        for camera in cams:
            if camera.name in delays:
                profile = self._delivery_profiles.get(camera.serial_number)
                key = profile if profile is not None else camera.name
                groups.setdefault(key, []).append(camera.name)
        for members in groups.values():
            if len(members) < 2:
                continue
            earliest = min(delays[name] for name in members)
            for name in members:
                pulses = (delays[name] - earliest) / period
                nearest = round(pulses)
                if abs(pulses - nearest) > 0.25:
                    offsets[name] = None
                    ok = False
                    warnings.append(
                        f"Camera {name}: could not verify that its first frame is "
                        f"the others' first pulse (first frames arrived "
                        f"{pulses:+.2f} periods from the earliest like camera's)"
                    )
                else:
                    offsets[name] = nearest
                    if nearest:
                        ok = False
                        warnings.append(
                            f"Camera {name} started {nearest} pulse(s) late: its "
                            f"frame i shows the moment of the other cameras' frame "
                            f"i+{nearest} (it missed the train's first pulse(s))"
                        )
        if len(groups) > 1:
            notes.append(_unlike_profiles_note(groups))
        return {
            "ok": ok,
            "warnings": warnings,
            "notes": notes,
            "start_offsets": offsets,
        }

    def _read_delivery_profiles(self) -> dict[str, DeliveryProfile | None]:
        """Each camera's :class:`DeliveryProfile`, by serial, for
        :meth:`_check_sync`.

        Read before the record grab, never at teardown (the Camera tab is
        unlocked again by then). A camera whose exposure or size cannot be read
        gets None and is compared with no other."""

        def profile(camera) -> DeliveryProfile | None:
            try:
                exposure = float(camera.read_param("exposure")["value"])
                width = int(camera.read_param("width")["value"])
                height = int(camera.read_param("height")["value"])
            except Exception as e:
                log.debug("Could not read the delivery profile of %s: %s", camera.name, e)
                return None
            try:
                model = camera.read_feature("DeviceModelName")["value"]
            except Exception:
                model = None
            # Same-model cameras snap a configured exposure identically, so whole
            # microseconds only absorb float noise.
            return DeliveryProfile(
                type(camera.backend).__name__,
                model,
                width,
                height,
                camera.pixel_format,
                round(exposure),
            )

        try:
            results = self.camera_system.apply_to_all(profile)
        except Exception:
            log.exception("Could not read the cameras' delivery profiles")
            return {}
        return {
            camera.serial_number: result
            for camera, result in zip(self.camera_system, results, strict=True)
        }

    def _snapshot_source(self) -> Path | None:
        """The rig config file each recording's snapshot is made from, or None
        (no config dir or file)."""
        if self._config_dir is None:
            return None
        src = self._config_dir / CONFIG_SNAPSHOT_FILENAME
        dst = self._recording_info_dir() / CONFIG_SNAPSHOT_FILENAME
        # A rig relaunched from a recording that records into that same folder
        # must not rewrite its own config.
        if not src.exists() or src.resolve() == dst.resolve():
            return None
        return src

    def _recording_info_dir(self) -> Path:
        """The ``octacam_recording`` subfolder, where everything but the videos
        goes, even beside an older take's flat files (readers prefer it). Each
        writer creates it."""
        return Path(self._settings.save_dir) / RECORDING_INFO_DIRNAME

    def _export_camera_params(self) -> dict[str, str]:
        """Each camera's current parameter text (unsaved Camera-tab edits
        included), for the snapshot. A camera that cannot be read is left out."""
        if self._snapshot_source() is None:
            return {}
        try:
            params = self.camera_system.save_all_params()
        except Exception:
            log.exception("Could not read the camera parameters for the config snapshot")
            return {}
        missing = [c.name for c in self.camera_system if c.serial_number not in params]
        if missing:
            log.warning(
                "Could not read the parameters of camera(s) %s; their settings "
                "are not saved with the recording",
                ", ".join(missing),
            )
        return params

    def _snapshot_config(
        self, plugin_params: dict | None, camera_params: dict[str, str]
    ) -> None:
        """Save the recording's config into its ``octacam_recording`` subfolder,
        a config directory `octacam gui <recording>` relaunches the rig from.

        The rig TOML gets the live Record-tab settings, plugin options, View-tab
        transforms and Process params patched in, beside every camera's
        parameter file. Unchanged, it stays a byte-verbatim copy; the directory
        templates are never patched, so a relaunch resolves a fresh folder. A
        failed re-emit falls back to the verbatim copy."""
        src = self._snapshot_source()
        if src is None:
            return
        config_dir = src.parent
        info_dir = self._recording_info_dir()
        dst = info_dir / CONFIG_SNAPSHOT_FILENAME
        s = self._settings
        try:
            info_dir.mkdir(parents=True, exist_ok=True)
            raw = config_writer.load_raw_config(config_dir)
            patched = config_writer.with_process_params(
                raw,
                transcode_ffmpeg_params=s.transcode_ffmpeg_params,
                transfer_directory=s.transfer_directory,
                transfer_checksum=s.transfer_checksum,
            )
            patched = config_writer.with_record_settings(
                patched, s.record_config_values()
            )
            patched = config_writer.with_plugin_options(
                patched, self.plugins.snapshot_options(plugin_params)
            )
            patched = config_writer.with_camera_transforms(
                patched,
                {c.serial_number: c.display_transform.to_dict() for c in self.camera_system},
            )
            if raw and patched != raw:
                config_writer.write_config(info_dir, patched)
            else:
                shutil.copyfile(src, dst)
        except Exception:
            log.exception("Failed to write patched config snapshot to %s", dst)
            with contextlib.suppress(Exception):
                shutil.copyfile(src, dst)
        try:
            # Beside the TOML: a relaunch loads them as one config directory.
            config_writer.write_pfs_files(
                info_dir, camera_params, self.camera_system.extension_by_serial()
            )
            config_writer.copy_auxiliary_pfs(
                config_dir,
                info_dir,
                set(camera_params),
                self.camera_system.extensions,
            )
        except Exception:
            log.exception("Failed to save the camera parameter files to %s", info_dir)

    def _write_recording_summary(self, aborted: bool) -> None:
        """Write recording_summary.json (its ``file`` entries name videos in the
        recording folder)."""
        path = self._recording_info_dir() / RECORDING_SUMMARY_FILENAME
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            summary = build_recording_summary(
                self._settings,
                list(self.camera_system),
                self._recording_start_wall_ns,
                aborted,
                pulse_clock=self._pulse_clock,
                sync=self._sync,
                primed_pulses=self._primed_pulses,
                completed=self._completed,
            )
            path.write_text(json.dumps(summary, indent=2) + "\n")
            log.info("Wrote recording summary: %s", path)
        except Exception:
            log.exception("Failed to write recording summary to %s", path)

    def _write_timestamps(self) -> None:
        """Write every camera's per-frame series into ``timestamps.npz``."""
        path = self._recording_info_dir() / TIMESTAMPS_FILENAME
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            arrays = build_timestamps_arrays(list(self.camera_system))
            np.savez_compressed(path, **arrays)
            log.info("Wrote frame timestamps: %s", path)
        except Exception:
            log.exception("Failed to write frame timestamps to %s", path)

    def _note_in_session_cache(self) -> None:
        """Note the recording's folder in the session cache (`octacam process
        --last`), before save_dir is incremented."""
        self._recordings_made += 1
        folder = Path(self._settings.save_dir)
        try:
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
            "ready": self._ready,
            "init_error": self._init_error,
            "remaining_ms": remaining_ms,
            "recording_id": recording_id,
            "recordings_made": self._recordings_made,
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
                    "missed": camera.missed_count,
                    "writer_failed": camera.writer_failed,
                }
                for camera in self.camera_system
            ],
        }
