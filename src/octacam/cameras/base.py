"""Vendor-neutral camera core.

:class:`Camera` owns everything that does not touch an SDK: the preview and
record grab loops, the display hand-off, pulse accounting, the writer wiring and
the start/stop lifecycle. Each vendor implements the :class:`CameraBackend` seam
and raises :class:`BackendError` for SDK failures, which ``Camera``'s setters
turn into ``ValueError``.
"""

import logging
import threading
import time
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass
from typing import ClassVar

import numpy as np

from octacam.cameras._trigger_handoff import PRIMING_TRIGGER, SoftwareTrigger
from octacam.pulses import Assignment, PulseClock, PulseTracker
from octacam.transform import DisplayTransform, apply_display_transform
from octacam.writer import AsyncFrameWriter, VideoFormat, WriteResult

log = logging.getLogger("octacam")

GRAB_TIMEOUT_MS = 100
# Default writer queue depth (record.writer_queue_size overrides it). The grab
# loop never blocks: a frame arriving at a full queue is refused and filled (see
# Camera._record_loop). 64 frames (~0.8 s at 80 fps) absorbs a transient encoder
# stall, such as several NVENC sessions warming up, while bounding memory.
WRITER_QUEUE_SIZE = 64
# After priming (see Camera.arm_counting), a frame within max(PRIME_STRAGGLER_NS,
# PRIME_STRAGGLER_PERIODS periods) of the last priming frame is a priming
# straggler, not pulse 0. Both must stay below the controller's settle before
# the train (PRIME_SETTLE_S, at least four periods).
PRIME_STRAGGLER_NS = 50_000_000
PRIME_STRAGGLER_PERIODS = 2.5
# Preview keeps only this many timestamps (the fps readout reads the last few);
# a recording keeps its whole series.
PREVIEW_TIMESTAMPS_MAX = 64

# octacam's parameter names -> SFNC node names (Camera.read_param). The geometry
# ones are writable only while not grabbing (set_geometry cycles the preview).
GEOMETRY_PARAMS = {"width": "Width", "height": "Height"}
PARAM_NODES = {
    **GEOMETRY_PARAMS,
    "exposure": "ExposureTime",
    "gain": "Gain",
    "offset_x": "OffsetX",
    "offset_y": "OffsetY",
}

# --- Camera-tab policy, in SFNC node names -------------------------------------
# ROI size, refused mid-grab on every camera, so written through set_geometry.
GEOMETRY_FEATURES = frozenset({"Width", "Height"})
# ROI origins and the center flag that derives each (the UI then locks the field).
OFFSET_FEATURES = {"OffsetX": "center_x", "OffsetY": "center_y"}
# Nodes octacam programs itself, shown read-only so an edit cannot break preview
# or recording: Mono8 (the GRAY8 writer), the link throughput (maxed at open on
# FLIR), the trigger chain and AcquisitionMode (set by every grab path) and
# TLParamsLocked (transport state).
RUNTIME_MANAGED_FEATURES = frozenset({
    "PixelFormat",
    "DeviceLinkThroughputLimit",
    "TLParamsLocked",
    "TriggerSelector",
    "TriggerMode",
    "TriggerSource",
    "TriggerOverlap",
    "AcquisitionMode",
})


class BackendError(Exception):
    """An SDK-level failure surfaced by a camera backend (vendor-neutral)."""


@dataclass
class FeatureInfo:
    """One node of the device node map, for the Camera tab's browser.

    ``type`` is the widget kind: int, float, bool, enum (``entries`` of
    ``{"value", "display", "available"}``), string, command or category.
    ``value`` is None for command and category nodes; for an enum it is the
    current symbolic. ``category`` comes only from a node-map walk: a
    single-node read leaves it blank, and the client keeps its grouping.
    ``managed`` marks a node octacam drives (:data:`RUNTIME_MANAGED_FEATURES`).
    """

    name: str
    display_name: str
    type: str
    category: str = ""
    value: object | None = None
    min: float | None = None
    max: float | None = None
    inc: float | None = None
    unit: str | None = None
    entries: list[dict] | None = None
    readable: bool = True
    writable: bool = False
    visibility: str = "beginner"
    tooltip: str | None = None
    managed: bool = False

    def as_dict(self) -> dict:
        d = {
            "name": self.name,
            "display_name": self.display_name,
            "type": self.type,
            "category": self.category,
            "value": self.value,
            "readable": self.readable,
            "writable": self.writable,
            "visibility": self.visibility,
            "managed": self.managed,
        }
        for key in ("min", "max", "inc", "unit", "tooltip", "entries"):
            v = getattr(self, key)
            if v is not None:
                d[key] = v
        return d


# A retrieved frame: the owned image (None when the caller did not want it) and
# its timestamp in ns (0 when the camera supplied none).
Frame = tuple[np.ndarray | None, int]


class CameraBackend(ABC):
    """One physical camera behind its SDK, as :class:`Camera` drives it.

    Device methods raise :class:`BackendError` on SDK failure. The retrieve
    methods never raise: None on a device error or a stop race, since an
    exception would kill the grab thread and orphan its writer. ``trigger`` is
    the camera's software-trigger hand-off; its grab flag is ``is_grabbing``.
    """

    extension: ClassVar[str]  # the parameter-file suffix, no dot

    def __init__(self, serial: str):
        self._serial = serial
        self.trigger = SoftwareTrigger(serial)

    @property
    def serial_number(self) -> str:
        return self._serial

    def is_grabbing(self) -> bool:
        return self.trigger.grabbing

    def trigger_once(self) -> None:
        """Offer one software trigger. No device call: ``retrieve`` fires it on
        the camera's own thread (see :mod:`octacam.cameras._trigger_handoff`)."""
        self.trigger.offer()

    def configure_trigger_period(self, period_s: float | None) -> None:
        self.trigger.configure_period(period_s)

    def restart_trigger_sequence(self) -> None:
        self.trigger.restart_sequence()

    @property
    def last_trigger_index(self) -> int | None:
        """The trigger the latest retrieved image answers (see
        :attr:`SoftwareTrigger.last_index`)."""
        return self.trigger.last_index

    def grab_locked_features(self) -> frozenset[str]:
        """Nodes writable only while not grabbing; Camera writes them through a
        preview grab cycle."""
        return GEOMETRY_FEATURES

    def stream_statistics(self) -> dict[str, int]:
        """The SDK's transport counters, where it has any."""
        return {}

    def retrieve(
        self, timeout_ms: int, wants_array: Callable[[], bool]
    ) -> Frame | None:
        """One software-triggered frame: fire a claimed trigger, then fetch at
        most one image, which answers the oldest outstanding trigger."""
        fire = self.trigger.claim(timeout_ms)
        if fire is None or not self.trigger.grabbing:
            return None
        if fire and not self._fire_trigger():
            self.trigger.unfired()
            return None
        return self._fetch(
            self.trigger.fetch_timeout_ms(timeout_ms), wants_array, answers_trigger=True
        )

    def retrieve_freerun(
        self, timeout_ms: int, wants_array: Callable[[], bool]
    ) -> Frame | None:
        """One frame the camera made on its own clock (free run, or a hardware
        trigger): the un-gated fetch."""
        if not self.trigger.grabbing:
            return None
        return self._fetch(timeout_ms, wants_array, answers_trigger=False)

    def retrieve_external(
        self, timeout_ms: int, wants_array: Callable[[], bool]
    ) -> Frame | None:
        """One frame of an externally triggered recording: the un-gated fetch."""
        return self.retrieve_freerun(timeout_ms, wants_array)

    @abstractmethod
    def _fire_trigger(self) -> bool:
        """Fire one device software trigger; False if the device refused it."""

    @abstractmethod
    def _fetch(
        self, timeout_ms: int, wants_array: Callable[[], bool], answers_trigger: bool
    ) -> Frame | None:
        """At most one image within ``timeout_ms``, its array only if
        ``wants_array()``; None on a timeout or an unusable image. With
        ``answers_trigger``, any image the SDK hands over, incomplete too, is
        reported to ``trigger.answered`` (with its timestamp if that counts ns)."""

    @abstractmethod
    def open(self) -> None: ...
    @abstractmethod
    def close(self) -> None: ...
    @abstractmethod
    def is_open(self) -> bool: ...
    @abstractmethod
    def width(self) -> int: ...
    @abstractmethod
    def height(self) -> int: ...

    # The node map (Camera tab), by GenApi node name; the backend coerces
    # ``value`` to the node's type.
    @abstractmethod
    def list_features(self) -> list[FeatureInfo]: ...
    @abstractmethod
    def read_feature(self, name: str) -> FeatureInfo: ...
    @abstractmethod
    def write_feature(self, name: str, value: object) -> None: ...
    @abstractmethod
    def execute_command(self, name: str) -> None: ...

    @abstractmethod
    def load_params(self, config_str: str) -> None: ...
    @abstractmethod
    def save_params(self) -> str: ...

    # Saved config text -> {node: value string} ({} if unparseable), for the
    # per-field reset.
    @abstractmethod
    def config_values(self, config_str: str) -> dict[str, str]: ...

    @abstractmethod
    def enable_frame_trigger(self) -> None: ...
    @abstractmethod
    def set_trigger_source(self, use_software: bool) -> None: ...
    @abstractmethod
    def begin_software_trigger_preview(self) -> None: ...

    # Free run: uncapped with ``fps=None`` (the benchmark's ceiling), else capped
    # at ``fps`` (free-run preview). False when the backend cannot arm it.
    @abstractmethod
    def begin_freerun(self, fps: float | None = None) -> bool: ...

    # Streaming: a start calls trigger.begin_grab() once the SDK streams, a stop
    # calls trigger.end_grab() before the native stop. A record start that
    # returns False has left the camera not grabbing.
    @abstractmethod
    def start_grab_preview(self) -> None: ...
    @abstractmethod
    def start_grab_record(self) -> bool: ...
    @abstractmethod
    def stop_grab(self) -> None: ...


def snap_value(value: float, info: FeatureInfo) -> float:
    """Clamp to [min, max] and round to the node's increment grid."""
    lo, hi, inc = info.min, info.max, info.inc
    if inc:
        base = lo if lo is not None else 0.0
        value = base + round((value - base) / inc) * inc
    if lo is not None:
        value = max(lo, value)
    if hi is not None:
        value = min(hi, value)
    return value


def coerce_bool(value: object) -> bool:
    """A config/feature value as bool: numbers by non-zero, text by 1/true/yes/on."""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    return str(value).strip().lower() in ("1", "true", "yes", "on")


class LatestFrame:
    """Single-slot frame hand-off to the GUI.

    The producer stores a frame only once the previous one was consumed, so its
    copies happen at most at the GUI refresh rate.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._frame: np.ndarray | None = None

    @property
    def wants_frame(self) -> bool:
        # Racy peek: with one producer a stale value costs one extra attempt or
        # one skipped preview frame.
        return self._frame is None

    def push(self, frame: np.ndarray) -> bool:
        if not self._lock.acquire(blocking=False):
            return False
        try:
            if self._frame is None:
                self._frame = frame
                return True
            return False
        finally:
            self._lock.release()

    def pop(self) -> np.ndarray | None:
        with self._lock:
            frame, self._frame = self._frame, None
            return frame


class Camera:
    """Vendor-neutral camera: grab loops, display handoff, recording lifecycle.

    All device access goes through ``self._backend`` (a :class:`CameraBackend`).
    """

    def __init__(self, backend: CameraBackend):
        self._backend = backend
        # The serial comes from enumeration: open() stays out of the constructor
        # so CameraSystem can open the cameras in parallel.
        self.serial_number: str = backend.serial_number
        self.name: str = self.serial_number
        self.width = 0
        self.height = 0
        self.display_transform = DisplayTransform()
        # Keep the ROI centered: derive OffsetX/OffsetY on every geometry change.
        self.center_x = False
        self.center_y = False
        # First-seen value per node. octacam never writes a node outside the
        # saved config, so this is its power-on default: the reset's fallback.
        self._default_cache: dict[str, object] = {}
        self.frame_for_display = LatestFrame()
        self._video_writer: AsyncFrameWriter | None = None
        self._recorded_frame_size: tuple[int, int] | None = None
        self._stop_flag = threading.Event()
        # Serializes external node access (read/set/save and the W/H grab
        # cycle) against itself; the preview loop stays lock-free.
        self._param_lock = threading.RLock()
        self._thread: threading.Thread | None = None
        # The last recording's per-video-frame series, all parallel (see the
        # frame_* properties).
        self._timestamps: list[int] = []
        self._dropped: list[bool] = []
        self._missed: list[bool] = []
        self._pulse_index: list[int] = []
        self._arrival_ns: list[int] = []
        self._dropped_count = 0
        self._writer_dropped = 0
        self._writer_skipped: list[int] = []
        self._tracker: PulseTracker | None = None
        self._pulse_clock: PulseClock | None = None
        self._extra_frames = 0
        self._primed_frames = 0
        self._unclocked_frames = 0
        self._armed = threading.Event()
        self._armed.set()
        self._fill_to: int | None = None
        self._stream_at_start: dict[str, int] = {}
        self._stream_at_stop: dict[str, int] = {}
        self._host_fallback_count = 0
        self._resulting_fps = 0.0
        self._started = False

    @property
    def backend(self) -> CameraBackend:
        """The backend, for the diagnostics engine's own grab loops; never use it
        to bypass the recording state machine."""
        return self._backend

    @property
    def extension(self) -> str:
        """The backend's parameter-file suffix (no dot): a mixed rig keeps each
        camera's params in its native format."""
        return type(self._backend).extension

    def open(self) -> None:
        """Open the underlying device (a blocking USB round-trip)."""
        self._backend.open()

    @property
    def started(self) -> bool:
        return self._started

    @property
    def resulting_fps(self) -> float:
        return self._resulting_fps

    @property
    def frames_recorded(self) -> int:
        return len(self._timestamps)

    @property
    def dropped_count(self) -> int:
        """Fills in the last recording: missed pulses plus refused frames, each a
        repeat of the previous frame (:attr:`writer_skipped` ones are not)."""
        return self._dropped_count

    @property
    def dropped_indices(self) -> list[int]:
        """Video frame indices that are fills (see :attr:`dropped_count`)."""
        return [i for i, dropped in enumerate(self._dropped) if dropped]

    @property
    def missed_count(self) -> int:
        """Pulses missed so far (cheap, for live telemetry)."""
        return len(self._tracker.missed) if self._tracker is not None else 0

    @property
    def stream_statistics(self) -> dict[str, int]:
        """The SDK's transport counters over the last recording ({} if none). A
        missed pulse with none of them moving was never exposed by the camera."""
        start, stop = self._stream_at_start, self._stream_at_stop
        # A counter the SDK restarts at acquisition start reads below its start
        # value; its stop value is then already the recording's own count.
        return {k: v - start.get(k, 0) if v >= start.get(k, 0) else v for k, v in stop.items()}

    @property
    def writer_dropped(self) -> int:
        """Delivered frames the writer queue refused, each filled (see
        :attr:`writer_skipped`)."""
        return self._writer_dropped

    @property
    def writer_skipped(self) -> int:
        """Frames refused once the writer was overloaded: skipped, not filled, so
        the video and the series end that many frames short of the train."""
        return len(self._writer_skipped)

    @property
    def writer_skipped_pulses(self) -> list[int]:
        """The pulses of the :attr:`writer_skipped` frames."""
        return list(self._writer_skipped)

    @property
    def missed_pulses(self) -> list[int]:
        """Trigger pulses of the last recording the camera delivered no frame for."""
        return list(self._tracker.missed) if self._tracker is not None else []

    @property
    def late_pulses(self) -> list[int]:
        """Pulses whose frame was exposed markedly later than the trigger clock
        predicts (e.g. a camera firing on the trigger pulse's falling edge)."""
        return list(self._tracker.late) if self._tracker is not None else []

    @property
    def extra_frames(self) -> int:
        """Frames discarded because they belong to no pulse of the train."""
        return self._extra_frames

    @property
    def primed_frames(self) -> int:
        """Frames delivered while the cameras were primed, before counting."""
        return self._primed_frames

    @property
    def pulse_clock(self) -> PulseClock | None:
        return self._pulse_clock

    @property
    def pulses_complete(self) -> bool:
        """True once every pulse of a counted train is accounted for."""
        return self._tracker is not None and self._tracker.complete

    @property
    def clock_mismatch(self) -> bool:
        """True when the frames did not follow the trigger clock's period."""
        return self._tracker is not None and self._tracker.clock_mismatch

    @property
    def timestamp_glitches(self) -> list[dict]:
        """Camera-clock jumps the pulse accounting corrected (see octacam.pulses)."""
        if self._tracker is None:
            return []
        return [
            {"pulse": g.pulse, "kind": g.kind, "jump_ns": g.jump_ns}
            for g in self._tracker.glitches
        ]

    @property
    def unclocked_frames(self) -> int:
        """Frames placed without evidence of their pulse (no hardware timestamp)."""
        return self._unclocked_frames

    @property
    def frame_timestamps(self) -> list[int]:
        """Per-frame timestamps (ns) of the last recording: the camera's where it
        supplied one (see :attr:`host_fallback_count`). A copy."""
        return list(self._timestamps)

    @property
    def frame_dropped(self) -> list[bool]:
        """Per-frame: a repeat of the previous frame, not its pulse's image."""
        return list(self._dropped)

    @property
    def frame_missed(self) -> list[bool]:
        """Per-frame: a fill for a pulse the camera missed (vs a refused frame)."""
        return list(self._missed)

    @property
    def frame_pulse_index(self) -> list[int]:
        """Per-frame pulse index (frame k is pulse k whenever the train is filled)."""
        return list(self._pulse_index)

    @property
    def frame_arrival_ns(self) -> list[int]:
        """Per-frame host wall-clock delivery time (0 for a filled frame)."""
        return list(self._arrival_ns)

    @property
    def host_fallback_count(self) -> int:
        """Rows whose timestamp the camera did not supply (stamped by host time or
        by when the pulse was due), and fills stamped from them. Normally 0 or
        every row (a host-clocked backend); in between is a stray-zero anomaly."""
        return self._host_fallback_count

    @property
    def start_timestamp_ns(self) -> int | None:
        """Timestamp of the first recorded frame, or None if none were grabbed."""
        return self._timestamps[0] if self._timestamps else None

    @property
    def mean_fps(self) -> float:
        """Average fps across the whole recording (vs the rolling resulting_fps)."""
        timestamps = self._timestamps
        if len(timestamps) < 2:
            return 0.0
        span_ns = timestamps[-1] - timestamps[0]
        # A span that is not positive (a clock that jumped back) has no rate.
        return (len(timestamps) - 1) * 1e9 / span_ns if span_ns > 0 else 0.0

    @property
    def recorded_frame_size(self) -> tuple[int, int] | None:
        """(width, height) written by the last recording: the transform's output
        size when the display transform was baked in."""
        return self._recorded_frame_size

    @property
    def pixel_format(self) -> str:
        """Mono8 on every backend; in the summary so a raw dump can be decoded."""
        return "Mono8"

    @property
    def writer_failed(self) -> bool:
        return self._video_writer is not None and self._video_writer.failed

    def load_params(self, config_str: str) -> None:
        self._backend.load_params(config_str)
        self.width = self._backend.width()
        self.height = self._backend.height()
        self.frame_for_display.push(np.zeros((self.height, self.width), dtype=np.uint8))

    # ----------------------------------------------------- sensor parameters

    def read_param(self, name: str) -> dict:
        """Descriptor for one editable param: value, bounds, writability."""
        if name not in PARAM_NODES:
            raise ValueError(f"Unknown camera parameter: {name}")
        with self._param_lock:
            info = self._backend.read_feature(PARAM_NODES[name])
            # Geometry is editable while open (set_geometry cycles the grab); a
            # live param's own writability counts (a model without Gain).
            writable = (
                self._backend.is_open() if name in GEOMETRY_PARAMS else info.writable
            )
            return {
                "name": name,
                "value": info.value,
                "min": info.min,
                "max": info.max,
                "inc": info.inc,
                "unit": info.unit,
                "writable": writable,
            }

    def trigger_window_us(self) -> tuple[float, float | None]:
        """``(trigger delay, exposure)`` in µs: the exposure a trigger opens, for
        a strobe sized to cover it. A delay the camera lacks reads 0, an
        unreadable exposure None."""
        with self._param_lock:
            try:
                exposure: float | None = float(self._backend.read_feature("ExposureTime").value)  # type: ignore[arg-type]
            except Exception:
                exposure = None
            try:
                delay = self._backend.read_feature("TriggerDelay").value
            except Exception:
                delay = None
        return (float(delay) if isinstance(delay, (int, float)) else 0.0, exposure)

    def set_geometry(
        self, *, width: int | None = None, height: int | None = None
    ) -> None:
        """Set Width/Height with the preview grab cycled around the write (the
        SDK refuses it mid-grab); the preview restarts even on a rejected value."""
        with self._param_lock:
            was_grabbing = self._backend.is_grabbing()
            if was_grabbing:
                self.stop()
                self.join()
            error: ValueError | None = None
            try:
                if height is not None:
                    info = self._backend.read_feature("Height")
                    self._backend.write_feature("Height", int(snap_value(height, info)))
                if width is not None:
                    info = self._backend.read_feature("Width")
                    self._backend.write_feature("Width", int(snap_value(width, info)))
            except BackendError as e:
                error = ValueError(str(e))
            self.width = self._backend.width()
            self.height = self._backend.height()
            # A resize changes each offset's range: re-center while stopped, after
            # refreshing the cached size it reads. Best-effort.
            if error is None:
                self._recenter_offsets_locked()
            self.frame_for_display.pop()
            self.frame_for_display.push(
                np.zeros((self.height, self.width), dtype=np.uint8)
            )
            if was_grabbing:
                self.start_preview()
            if error is not None:
                raise error

    def save_params(self) -> str:
        """Full config text of the current parameters (round-trips load_params)."""
        if not self._backend.is_open():
            return ""
        with self._param_lock:
            return self._backend.save_params()

    # ------------------------------------------------- full device node map

    def _annotate(self, feature: FeatureInfo, grab_locked: frozenset[str]) -> FeatureInfo:
        """Apply octacam's policy to one backend feature: managed nodes and an
        auto-centered offset are read-only, a grab-locked node is editable while
        open (set_feature cycles the grab)."""
        if feature.name in RUNTIME_MANAGED_FEATURES:
            feature.managed = True
            feature.writable = False
        elif feature.name in OFFSET_FEATURES and getattr(self, OFFSET_FEATURES[feature.name]):
            feature.writable = False  # octacam derives it; UI shows locked
        elif feature.name in grab_locked:
            feature.writable = self._backend.is_open()
        if feature.value is not None:
            self._default_cache.setdefault(feature.name, feature.value)
        return feature

    def list_features(self) -> list[dict]:
        """Every node the backend exposes, annotated with octacam's policy."""
        with self._param_lock:
            features = self._backend.list_features()
            grab_locked = self._backend.grab_locked_features()
        return [self._annotate(f, grab_locked).as_dict() for f in features]

    def read_feature(self, name: str) -> dict:
        """One node's current descriptor, annotated with octacam's policy."""
        with self._param_lock:
            feature = self._backend.read_feature(name)
            grab_locked = self._backend.grab_locked_features()
        return self._annotate(feature, grab_locked).as_dict()

    def _centered_offset(self, node_name: str) -> int | None:
        """The offset that centers the ROI on ``node_name``'s axis, or None."""
        try:
            info = self._backend.read_feature(node_name)
        except BackendError:
            return None
        size = self.width if node_name == "OffsetX" else self.height
        sensor_max = "WidthMax" if node_name == "OffsetX" else "HeightMax"
        full: float | None = None
        try:
            full = self._backend.read_feature(sensor_max).value  # type: ignore[assignment]
        except BackendError:
            full = None
        if full is None and info.max is not None:
            full = info.max + size  # offset max is (sensor - size) per SFNC
        if full is None:
            return None
        lo = info.min or 0
        centered = lo + (float(full) - lo - size) / 2
        return int(snap_value(max(lo, centered), info))

    def _recenter_offsets_locked(self) -> None:
        """Recompute and write each auto-centered offset. Caller holds the lock."""
        for node_name, flag in OFFSET_FEATURES.items():
            if not getattr(self, flag):
                continue
            target = self._centered_offset(node_name)
            if target is None:
                continue
            try:
                self._backend.write_feature(node_name, target)
            except BackendError as e:
                log.debug("Could not center %s on %s: %s", node_name, self.serial_number, e)

    def _run_stopped(self, fn: Callable[[], None]) -> None:
        """Run ``fn`` with the preview grab stopped (caller holds ``_param_lock``);
        the preview restarts even if ``fn`` raises."""
        was_grabbing = self._backend.is_grabbing()
        if was_grabbing:
            self.stop()
            self.join()
        try:
            fn()
        finally:
            if was_grabbing:
                self.start_preview()

    def _write_feature_stopped(self, name: str, value: object) -> None:
        """Write a node the backend locks mid-grab, cycling the preview around it."""
        def _write() -> None:
            try:
                self._backend.write_feature(name, value)
            except BackendError as e:
                raise ValueError(str(e)) from None

        self._run_stopped(_write)

    def set_center(self, axis: str, enabled: bool) -> dict:
        """Toggle ROI auto-centering on axis ``"x"`` or ``"y"``; enabling
        re-centers now."""
        if axis not in ("x", "y"):
            raise ValueError(f"Unknown center axis: {axis}")
        with self._param_lock:
            setattr(self, f"center_{axis}", bool(enabled))
            if enabled:
                if OFFSET_FEATURES.keys() & self._backend.grab_locked_features():
                    self._run_stopped(self._recenter_offsets_locked)
                else:
                    self._recenter_offsets_locked()
        return {"center_x": self.center_x, "center_y": self.center_y}

    def set_feature(self, name: str, value: object) -> None:
        """Write one node, cycling the grab for a grab-locked one; managed nodes
        and auto-centered offsets are refused."""
        if name in RUNTIME_MANAGED_FEATURES:
            raise ValueError(f"{name} is managed by octacam and cannot be edited")
        if name in OFFSET_FEATURES and getattr(self, OFFSET_FEATURES[name]):
            raise ValueError(f"{name} is auto-centered; disable centering to set it")
        if name == "Width":
            self.set_geometry(width=int(float(value)))  # type: ignore[arg-type]
            return
        if name == "Height":
            self.set_geometry(height=int(float(value)))  # type: ignore[arg-type]
            return
        with self._param_lock:
            if name in self._backend.grab_locked_features():
                self._write_feature_stopped(name, value)
            else:
                try:
                    self._backend.write_feature(name, value)
                except BackendError as e:
                    raise ValueError(str(e)) from None

    def reset_feature(self, name: str, config_str: str) -> None:
        """Reset a node to its saved-config value, else its first-seen value (see
        ``_default_cache``); with neither it is left as is."""
        if name in RUNTIME_MANAGED_FEATURES:
            raise ValueError(f"{name} is managed by octacam and cannot be reset")
        value: object | None = None
        if config_str:
            with self._param_lock:
                value = self._backend.config_values(config_str).get(name)
        if value is None:
            value = self._default_cache.get(name)
        if value is None:
            return  # nothing to reset to
        self.set_feature(name, value)

    def execute_command(self, name: str) -> None:
        """Execute a command node (e.g. TimestampLatch)."""
        with self._param_lock:
            try:
                self._backend.execute_command(name)
            except BackendError as e:
                raise ValueError(str(e)) from None

    # ----------------------------------------------------------- triggering

    def enable_frame_trigger(self) -> None:
        """Arm the FrameStart trigger: a headless recording needs it, since the
        config files ship TriggerMode Off."""
        self._backend.enable_frame_trigger()

    def set_trigger_source(self, use_software_trigger: bool) -> None:
        self._backend.set_trigger_source(use_software_trigger)

    def trigger_once(self) -> None:
        self._backend.trigger_once()

    # ------------------------------------------------------------- grabbing

    def start_preview(self, mode: str = "software", fps: float | None = None) -> None:
        """Start preview, clocked as the recording will be.

        ``"software"``: the controller's software trigger, through the hand-off.
        ``"free_running"``: free run capped at ``fps``, drawing an fps-matched
        recording's bandwidth (stands in for an external trigger octacam cannot
        drive). ``"managed"``: hardware-trigger mode, clocked by a trigger plugin.
        """
        self._stop_flag.clear()
        if not self._backend.is_open():
            return
        if mode == "free_running":
            # A backend that cannot free-run previews on the software trigger.
            if not self._backend.begin_freerun(fps):
                mode = "software"
                self._backend.begin_software_trigger_preview()
        elif mode == "managed":
            self._backend.enable_frame_trigger()
            self._backend.set_trigger_source(False)  # restore the hardware line
        else:
            mode = "software"
            self._backend.begin_software_trigger_preview()
        self._reset_series()  # so preview never shows a stale recording count
        self._backend.configure_trigger_period(None)  # no recording counts preview triggers
        self._backend.start_grab_preview()
        self._thread = threading.Thread(
            target=self._preview_loop, args=(mode,), daemon=True
        )
        self._thread.start()

    def start_record(
        self,
        save_path: str,
        fps: float,
        video_format: VideoFormat,
        record_form: str = "display",
        software_trigger: bool = True,
        queue_size: int = WRITER_QUEUE_SIZE,
        max_frames: int | None = None,
        pulse_clock: PulseClock | None = None,
        hold: bool = False,
    ) -> bool:
        """Start recording; True iff the record loop was launched.

        ``record_form`` "display" bakes the display transform into the video.
        ``software_trigger`` False fetches externally triggered frames un-gated
        (the hand-off's ``retrieve`` would wait for a software trigger forever).
        Every frame is assigned to its pulse of ``pulse_clock`` (derived from
        ``fps`` and ``max_frames`` when None, see :mod:`octacam.pulses`), and the
        loop ends once the train's last pulse is accounted for. ``hold`` discards
        frames until :meth:`arm_counting`, while the recording primes the cameras.
        """
        self._stop_flag.clear()
        self._started = False
        if not self._backend.is_open():
            return False
        if pulse_clock is None:
            pulse_clock = PulseClock(
                period_ns=int(round(1e9 / fps)) if fps > 0 else 0,
                count=max_frames,
                source="software" if software_trigger else "external",
                fill=max_frames is not None,
            )
        self._reset_series()
        self._pulse_clock = pulse_clock
        self._tracker = PulseTracker(pulse_clock)
        self._fill_to = None
        self._stream_at_start = {}
        self._stream_at_stop = {}
        if hold:
            self._armed.clear()
        else:
            self._armed.set()

        bake = record_form == "display" and not self.display_transform.is_identity
        transform = self.display_transform if bake else None

        sensor_size = (self._backend.width(), self._backend.height())
        frame_size = (
            self.display_transform.output_size(*sensor_size) if bake else sensor_size
        )
        self._recorded_frame_size = frame_size
        self._video_writer = video_format.create_writer(max(1, queue_size))
        if not self._video_writer.open(save_path, fps, frame_size):
            log.error("Failed to open video writer for: %s", save_path)
            return False

        # The writer's ffmpeg child is live: close it on any failure below.
        try:
            # A software train's period sets the hand-off's deadlines.
            software_period = (
                pulse_clock.period_ns / 1e9
                if software_trigger and pulse_clock is not None and pulse_clock.period_ns
                else None
            )
            self._backend.configure_trigger_period(software_period)
            if not self._backend.start_grab_record():
                log.error(
                    "Failed to start grabbing for recording on camera %s",
                    self.serial_number,
                )
                self._video_writer.close()
                return False
        except Exception:
            self._video_writer.close()
            raise
        # Baseline the transport counters before any trigger: some restart with
        # each acquisition, others run on; both diff to this recording's.
        self._stream_at_start = self._read_stream_statistics()

        self._thread = threading.Thread(
            target=self._record_loop,
            args=(transform, software_trigger),
            daemon=True,
        )
        self._thread.start()
        return True

    def arm_counting(self) -> None:
        """End the priming ``hold``: the next trigger is the recording's first,
        and a software-trigger sequence restarts so it is trigger 0."""
        self._backend.restart_trigger_sequence()
        self._armed.set()

    def stop(self, fill_to: int | None = None) -> None:
        """Stop the grab loop. ``fill_to`` (a completed train's count) pads a
        camera that missed the last pulses, so it ends aligned."""
        if fill_to is not None:
            self._fill_to = fill_to
        self._stop_flag.set()

    def join(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            self._thread.join()
        self._thread = None

    def close(self) -> None:
        self.stop()
        self.join()
        self._backend.close()

    # ------------------------------------------------------- grab loop bodies

    def _store_timestamp(self, timestamp: int) -> None:
        if not timestamp:  # backend supplied no hardware timestamp for this frame
            self._host_fallback_count += 1
        self._timestamps.append(timestamp or time.time_ns())

    def _update_resulting_fps(self, n_frames: int = 6) -> None:
        timestamps = self._timestamps
        if len(timestamps) < 2 or n_frames < 1:
            self._resulting_fps = 0.0
            return
        last = len(timestamps) - 1
        start = last - n_frames if last > n_frames else 0
        delta_ns = timestamps[last] - timestamps[start]
        self._resulting_fps = (last - start) * 1e9 / delta_ns if delta_ns > 0 else 0.0

    def _preview_loop(self, mode: str = "software") -> None:
        backend = self._backend
        # Free-running and plugin-clocked cameras make frames on their own: the
        # un-gated fetch (not retrieve_external, whose wait for a software pulse
        # would freeze a managed preview).
        retrieve = backend.retrieve if mode == "software" else backend.retrieve_freerun
        while not self._stop_flag.is_set() and backend.is_grabbing():
            # Copy the array only when the display slot is free.
            frame = retrieve(
                GRAB_TIMEOUT_MS, lambda: self.frame_for_display.wants_frame
            )
            if frame is not None:
                array, timestamp = frame
                self._store_timestamp(timestamp)
                if len(self._timestamps) > PREVIEW_TIMESTAMPS_MAX:
                    del self._timestamps[0]
                if array is not None and self.frame_for_display.push(array):
                    self._update_resulting_fps()
        backend.stop_grab()

    def _read_stream_statistics(self) -> dict[str, int]:
        try:
            return self._backend.stream_statistics()
        except Exception as e:  # diagnostics must never break a recording
            log.debug("Could not read stream statistics of %s: %s", self.serial_number, e)
            return {}

    def _reset_series(self) -> None:
        self._timestamps.clear()
        self._dropped.clear()
        self._missed.clear()
        self._pulse_index.clear()
        self._arrival_ns.clear()
        self._dropped_count = 0
        self._writer_dropped = 0
        self._writer_skipped.clear()
        self._host_fallback_count = 0
        self._extra_frames = 0
        self._primed_frames = 0
        self._unclocked_frames = 0
        self._tracker = None

    def _append_row(
        self,
        timestamp: int,
        pulse: int,
        *,
        missed: bool,
        dropped: bool,
        arrival: int,
        unstamped: bool = False,
    ) -> None:
        """One video frame's row. ``unstamped``: the camera did not supply its
        timestamp (see :attr:`host_fallback_count`)."""
        self._timestamps.append(timestamp)
        self._pulse_index.append(pulse)
        self._missed.append(missed)
        self._dropped.append(dropped)
        self._arrival_ns.append(arrival)
        if dropped:
            self._dropped_count += 1
        if unstamped:
            self._host_fallback_count += 1

    @staticmethod
    def _fill_stamp(
        tracker: PulseTracker, pulse: int, anchor: tuple[int, int, bool]
    ) -> tuple[int, bool]:
        """A fill's timestamp: when ``pulse`` was due, on the camera clock where
        the tracker follows it, else whole periods from ``anchor`` (timestamp,
        pulse, unstamped) of a delivered frame. Returns it with whether it
        inherits an unstamped anchor; never 0 or negative.
        """
        due = tracker.expected_ts(pulse)
        if due is not None:
            return max(due, 1), False
        stamp, at_pulse, unstamped = anchor
        # An integer offset: a host-time stamp (~1.8e18 ns) is past float precision.
        offset = int(round((pulse - at_pulse) * tracker.period_ns))
        return max(stamp + offset, 1), unstamped

    def _place_frame(
        self, tracker: PulseTracker, index: int | None, timestamp: int, host_ns: int
    ) -> tuple[Assignment, int | None]:
        """Assign a delivered frame to its pulse; returns ``(assignment, stamp)``,
        ``stamp`` being the timestamp its row carries (None: host arrival time).

        ``index`` is the software-trigger sequence number of the trigger the frame
        answers (exact), else the frame is placed from its camera ``timestamp``.
        """
        if index is not None:
            return tracker.assign_index(index), (timestamp or None)
        if timestamp:
            if tracker.last_pulse is not None and tracker.expected_ts(tracker.next_pulse) is None:
                # The first timed frame after untimed ones: anchor the camera
                # clock at this frame's pulse, not the train's first (which would
                # restart the count and shift every frame).
                tracker.first_pulse = tracker.next_pulse
            return tracker.assign(timestamp, host_ns), timestamp
        self._unclocked_frames += 1
        due = tracker.expected_ts(tracker.next_pulse)
        if due is not None and due > 0:
            # A stray untimed frame on a clocked camera: the next pulse, stamped
            # when it was due, so the next interval is measured from an anchor
            # that agrees with it (else it reads as two periods: an invented
            # miss). Host arrival is no clock: buffered frames arrive bunched. A
            # real miss just before stays local: this frame is a pulse early.
            return tracker.assign(due, host_ns), due
        # A host-clocked backend: nothing places the frame, so it is the next pulse.
        return tracker.assign_index(tracker.next_pulse), None

    def _record_loop(
        self,
        transform: DisplayTransform | None = None,
        software_trigger: bool = True,
    ) -> None:
        backend = self._backend
        tracker = self._tracker
        clock = self._pulse_clock
        assert tracker is not None and clock is not None
        retrieve = backend.retrieve if software_trigger else backend.retrieve_external
        # Frames owed to the video and not yet queued (fills, refused frames):
        # they ride on the next queued frame or on close, so the video keeps one
        # frame per pulse.
        owed = 0
        writer = self._video_writer
        assert writer is not None
        last_primed_ts: int | None = None
        straggler_ns = max(
            PRIME_STRAGGLER_NS, int(PRIME_STRAGGLER_PERIODS * clock.period_ns)
        )
        # (timestamp, pulse, unstamped) of the last delivered frame (_fill_stamp).
        anchor: tuple[int, int, bool] | None = None
        while not self._stop_flag.is_set() and backend.is_grabbing():
            # Stop on the pulse count, so every camera ends on the same pulse.
            if tracker.complete:
                break
            frame = retrieve(GRAB_TIMEOUT_MS, _ALWAYS)
            if frame is None:
                continue
            array, timestamp = frame
            if array is None:  # record always requests the array; defensive
                continue
            host_ns = time.monotonic_ns()
            if not self._armed.is_set():  # an answer to a priming trigger
                self._primed_frames += 1
                last_primed_ts = timestamp or last_primed_ts
                continue
            # Read after the armed check: an image answered before the sequence
            # restart reports PRIMING_TRIGGER however late this read comes.
            index = backend.last_trigger_index
            if index is not None and index < 0:
                # Answers no trigger of the recording (see _trigger_handoff).
                if index == PRIMING_TRIGGER:
                    self._primed_frames += 1
                else:
                    self._extra_frames += 1
                continue
            if (
                index is None
                and timestamp
                and last_primed_ts is not None
                and 0 <= timestamp - last_primed_ts < straggler_ns
            ):
                # A priming frame still buffered at arm time (a hardware trigger
                # has no sequence number: only the timestamp tells).
                self._primed_frames += 1
                last_primed_ts = timestamp
                continue
            arrival = time.time_ns()
            assignment, placed = self._place_frame(tracker, index, timestamp, host_ns)
            stamp = placed or arrival
            if assignment.extra:
                if clock.fill:
                    self._extra_frames += 1
                    continue
                # An external clock octacam cannot see: report, never discard.
                pulse = tracker.last_pulse if tracker.last_pulse is not None else 0
            else:
                pulse = assignment.pulse
                assert pulse is not None
            if clock.fill and assignment.missed:
                # Fills are stamped from the last delivered frame; a leading miss
                # counts back from this one.
                ref = anchor if anchor is not None else (stamp, pulse, not timestamp)
                for missed_pulse in assignment.missed:
                    due, unstamped = self._fill_stamp(tracker, missed_pulse, ref)
                    self._append_row(
                        due,
                        missed_pulse,
                        missed=True,
                        dropped=True,
                        arrival=0,
                        unstamped=unstamped,
                    )
                owed += len(assignment.missed)
                log.warning(
                    "Camera %s missed trigger pulse(s) %s; filled with the "
                    "previous frame",
                    self.serial_number,
                    _format_pulses(assignment.missed),
                )
            anchor = (stamp, pulse, not timestamp)

            # The preview gets the raw array: the browser applies the transform.
            to_write = apply_display_transform(array, transform) if transform else array
            result = writer.write(to_write, fill_before=owed)
            if result is WriteResult.WRITTEN:
                owed = 0
            elif result is WriteResult.REFUSED:
                owed += 1
                self._writer_dropped += 1
                log.warning(
                    "Frame for pulse %d dropped for camera %s (writer queue full); "
                    "it will be filled with the previous frame",
                    pulse,
                    self.serial_number,
                )
            else:
                if not self._writer_skipped:
                    log.error(
                        "Camera %s: the video writer cannot keep up — %d frames "
                        "refused before it caught up. From pulse %d on, a frame it "
                        "refuses is skipped instead of filled: this camera's video "
                        "will be short of the train (pulse_index in the timestamps "
                        "maps each frame to its pulse). The encoder or disk is "
                        "slower than the camera: use a faster save method or disk, "
                        "or a lower frame rate.",
                        self.serial_number,
                        writer.max_queue_size,
                        pulse,
                    )
                self._writer_skipped.append(pulse)
            if result is not WriteResult.SKIPPED:  # a skipped frame has no row
                self._append_row(
                    stamp,
                    pulse,
                    missed=False,
                    dropped=result is WriteResult.REFUSED,
                    arrival=arrival,
                    unstamped=not timestamp,
                )

            if self.frame_for_display.push(array):
                self._update_resulting_fps()

            self._started = True
        self._stream_at_stop = self._read_stream_statistics()
        backend.stop_grab()
        # A train that ran to its end: pad the last pulses this camera missed so
        # it ends with the others (given a frame to repeat). A stopped or aborted
        # take (fill_to None) ends where it was.
        fill_to = self._fill_to
        if (
            clock.fill
            and fill_to is not None
            and clock.count is not None
            and self._timestamps
            and anchor is not None
        ):
            end = min(fill_to, clock.count)
            trailing = range(tracker.next_pulse, end)
            for missed_pulse in trailing:
                due, unstamped = self._fill_stamp(tracker, missed_pulse, anchor)
                self._append_row(
                    due,
                    missed_pulse,
                    missed=True,
                    dropped=True,
                    arrival=0,
                    unstamped=unstamped,
                )
            if trailing:
                tracker.missed.extend(trailing)
                tracker.last_pulse = end - 1
                owed += len(trailing)
                log.warning(
                    "Camera %s missed the last trigger pulse(s) %s; filled with "
                    "the previous frame",
                    self.serial_number,
                    _format_pulses(trailing),
                )
        writer.close(fill_after=owed)
        self._reconcile_unwritten_frames()

        log.info(
            "Camera %s: %d frames recorded (%d missed pulses and %d writer drops "
            "filled, %d writer drops skipped), %d extra frames discarded",
            self.serial_number,
            len(self._timestamps),
            sum(self._missed),
            self._writer_dropped,
            len(self._writer_skipped),
            self._extra_frames,
        )

    def _reconcile_unwritten_frames(self) -> None:
        """After a writer failure the file ends at ``frames_written``: mark the
        later rows dropped."""
        if self._video_writer is None or not self._video_writer.failed:
            return
        written = self._video_writer.frames_written
        for index in range(written, len(self._dropped)):
            if not self._dropped[index]:
                self._dropped[index] = True
                self._dropped_count += 1


def _ALWAYS() -> bool:
    return True


def _format_pulses(pulses: range) -> str:
    if len(pulses) == 1:
        return str(pulses.start)
    return f"{pulses.start}-{pulses.stop - 1}"
