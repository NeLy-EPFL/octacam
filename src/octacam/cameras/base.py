"""Vendor-neutral camera core.

`Camera` owns everything that does not touch an SDK: its parameters and
node map, the preview grab loop, the display hand-off and its recordings, each a
`CameraTake`. Each vendor implements the
`CameraBackend` seam and raises `BackendError` for SDK failures,
which `Camera`'s setters turn into `ValueError`.
"""

import logging
import numbers
import threading
import time
from abc import ABC, abstractmethod
from collections import deque
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import ClassVar

import numpy as np

from octacam.cameras._trigger_handoff import SoftwareTrigger
from octacam.cameras.take import GRAB_TIMEOUT_MS, CameraTake
from octacam.pulses import PulseClock
from octacam.transform import DisplayTransform
from octacam.writer import VideoFormat

log = logging.getLogger("octacam")

# Default writer queue depth (record.writer_queue_size overrides it). The grab
# loop never blocks: a frame arriving at a full queue is refused and filled (see
# AsyncFrameWriter.write). 64 frames (~0.8 s at 80 fps) absorbs a transient
# encoder stall, such as several NVENC sessions warming up, while bounding memory.
WRITER_QUEUE_SIZE = 64
# The preview keeps this many timestamps, for the fps readout.
PREVIEW_TIMESTAMPS_MAX = 64
# The fps readout is the rate over this many frame intervals.
FPS_WINDOW = 6

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
RUNTIME_MANAGED_FEATURES = frozenset(
    {
        "PixelFormat",
        "DeviceLinkThroughputLimit",
        "TLParamsLocked",
        "TriggerSelector",
        "TriggerMode",
        "TriggerSource",
        "TriggerOverlap",
        "AcquisitionMode",
    }
)


class BackendError(Exception):
    """An SDK-level failure surfaced by a camera backend (vendor-neutral)."""


@dataclass
class FeatureInfo:
    """One node of the device node map, for the Camera tab's browser.

    `type` is the widget kind: int, float, bool, enum (`entries` of
    `{"value", "display", "available"}`), string, command or category.
    `value` is None for command and category nodes; for an enum it is the
    current symbolic. `category` comes only from a node-map walk: a
    single-node read leaves it blank, and the client keeps its grouping.
    `managed` marks a node octacam drives (`RUNTIME_MANAGED_FEATURES`).
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
    """One physical camera behind its SDK, as `Camera` drives it.

    Device methods raise `BackendError` on SDK failure. The retrieve
    methods never raise: None on a device error or a stop race, since an
    exception would kill the grab thread and orphan its writer. `trigger` is
    the camera's software-trigger hand-off; its grab flag is `is_grabbing`.
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
        """Offer one software trigger. No device call: `retrieve` fires it on
        the camera's own thread (see `octacam.cameras._trigger_handoff`).
        """
        self.trigger.offer()

    def configure_trigger_period(self, period_s: float | None) -> None:
        self.trigger.configure_period(period_s)

    def restart_trigger_sequence(self) -> None:
        self.trigger.restart_sequence()

    @property
    def last_trigger_index(self) -> int | None:
        """The trigger the latest retrieved image answers (see
        `SoftwareTrigger.last_index`).
        """
        return self.trigger.last_index

    def grab_locked_features(self) -> frozenset[str]:
        """Nodes writable only while not grabbing; Camera writes them through a
        preview grab cycle.
        """
        return GEOMETRY_FEATURES

    def stream_statistics(self) -> dict[str, int]:
        """The SDK's transport counters, where it has any."""
        return {}

    def retrieve(
        self, timeout_ms: int, wants_array: Callable[[], bool]
    ) -> Frame | None:
        """One software-triggered frame: fire a claimed trigger, then fetch at
        most one image, which answers the oldest outstanding trigger.
        """
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
        trigger): the un-gated fetch.
        """
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
        """At most one image within `timeout_ms`, its array only if
        `wants_array()`; None on a timeout or an unusable image. With
        `answers_trigger`, any image the SDK hands over, incomplete too, is
        reported to `trigger.answered` (with its timestamp if that counts ns).
        """

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
    # `value` to the node's type.
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

    # Free run: uncapped with `fps=None` (the benchmark's ceiling), else capped
    # at `fps` (free-run preview). False when the backend cannot arm it.
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


def coerce_float(value: object) -> float:
    """A config/feature value as float: a number or numeric text, else TypeError."""
    if isinstance(value, (numbers.Real, str)):
        return float(value)
    raise TypeError(f"not a number: {value!r}")


class LatestFrame:
    """Single-slot frame hand-off to the GUI, and the fps readout shown beside it.

    The producer stores a frame only once the previous one was consumed, so its
    copies happen at most at the GUI refresh rate.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._frame: np.ndarray | None = None
        self.fps = 0.0

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

    def refresh_fps(self, timestamps: Sequence[int]) -> None:
        """Set `fps` from the last FPS_WINDOW intervals of `timestamps`
        (ns), the series a pushed frame ends.
        """
        last = len(timestamps) - 1
        start = max(0, last - FPS_WINDOW)
        span = timestamps[last] - timestamps[start] if last > 0 else 0
        self.fps = (last - start) * 1e9 / span if span > 0 else 0.0


class Camera:
    """Vendor-neutral camera: parameters, node map, preview and recordings.

    All device access goes through `self._backend` (a `CameraBackend`).
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
        # The recording the camera runs, or its last until the preview restarts
        # (so the preview never shows a stale recording's counts).
        self.take: CameraTake | None = None
        self.preview_timestamps: deque[int] = deque(maxlen=PREVIEW_TIMESTAMPS_MAX)
        self._stop_flag = threading.Event()
        # Serializes external node access (read/set/save and the W/H grab
        # cycle) against itself; the preview loop stays lock-free.
        self._param_lock = threading.RLock()
        self._thread: threading.Thread | None = None

    @property
    def backend(self) -> CameraBackend:
        """The backend, for the diagnostics engine's own grab loops; never use it
        to bypass the recording state machine.
        """
        return self._backend

    @property
    def extension(self) -> str:
        """The backend's parameter-file suffix (no dot): a mixed rig keeps each
        camera's params in its native format.
        """
        return type(self._backend).extension

    def open(self) -> None:
        """Open the underlying device (a blocking USB round-trip)."""
        self._backend.open()

    @property
    def pixel_format(self) -> str:
        """Mono8 on every backend; in the summary so a raw dump can be decoded."""
        return "Mono8"

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
        """`(trigger delay, exposure)` in us: the exposure a trigger opens, for
        a strobe sized to cover it. A delay the camera lacks reads 0, an
        unreadable exposure None.
        """
        with self._param_lock:
            try:
                exposure: float | None = coerce_float(
                    self._backend.read_feature("ExposureTime").value
                )
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
        SDK refuses it mid-grab); the preview restarts even on a rejected value.
        """
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

    def _annotate(
        self, feature: FeatureInfo, grab_locked: frozenset[str]
    ) -> FeatureInfo:
        """Apply octacam's policy to one backend feature: managed nodes and an
        auto-centered offset are read-only, a grab-locked node is editable while
        open (set_feature cycles the grab).
        """
        if feature.name in RUNTIME_MANAGED_FEATURES:
            feature.managed = True
            feature.writable = False
        elif feature.name in OFFSET_FEATURES and getattr(
            self, OFFSET_FEATURES[feature.name]
        ):
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
        """The offset that centers the ROI on `node_name`'s axis, or None."""
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
                log.debug(
                    "Could not center %s on %s: %s", node_name, self.serial_number, e
                )

    def _run_stopped(self, fn: Callable[[], None]) -> None:
        """Run `fn` with the preview grab stopped (caller holds `_param_lock`);
        the preview restarts even if `fn` raises.
        """
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
        """Toggle ROI auto-centering on axis `"x"` or `"y"`; enabling
        re-centers now.
        """
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
        and auto-centered offsets are refused.
        """
        if name in RUNTIME_MANAGED_FEATURES:
            raise ValueError(f"{name} is managed by octacam and cannot be edited")
        if name in OFFSET_FEATURES and getattr(self, OFFSET_FEATURES[name]):
            raise ValueError(f"{name} is auto-centered; disable centering to set it")
        if name == "Width":
            self.set_geometry(width=int(coerce_float(value)))
            return
        if name == "Height":
            self.set_geometry(height=int(coerce_float(value)))
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
        `_default_cache`); with neither it is left as is.
        """
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
        config files ship TriggerMode Off.
        """
        self._backend.enable_frame_trigger()

    def set_trigger_source(self, use_software_trigger: bool) -> None:
        self._backend.set_trigger_source(use_software_trigger)

    def trigger_once(self) -> None:
        self._backend.trigger_once()

    # ------------------------------------------------------------- grabbing

    def start_preview(self, mode: str = "software", fps: float | None = None) -> None:
        """Start preview, clocked as the recording will be.

        `"software"`: the controller's software trigger, through the hand-off.
        `"free_running"`: free run capped at `fps`, drawing an fps-matched
        recording's bandwidth (stands in for an external trigger octacam cannot
        drive). `"managed"`: hardware-trigger mode, clocked by a trigger plugin.
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
        self.take = None
        self.preview_timestamps.clear()
        self._backend.configure_trigger_period(
            None
        )  # no recording counts preview triggers
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
        clock: PulseClock,
        *,
        record_form: str = "display",
        queue_size: int = WRITER_QUEUE_SIZE,
        hold: bool = False,
    ) -> bool:
        """Record into `save_path` as a fresh `take` counted against
        `clock` (`record_form` and `hold` as for
        `CameraTake`); True iff its record loop was
        launched.
        """
        self.take = CameraTake(
            self,
            clock,
            video_format=video_format,
            queue_size=queue_size,
            record_form=record_form,
            hold=hold,
        )
        if not self._backend.is_open():
            return False
        return self.take.start(save_path, fps)

    def stop(self, fill_to: int | None = None) -> None:
        """Stop the grab loop. `fill_to` (a completed train's count) pads a
        recording camera that missed the last pulses, so it ends aligned.
        """
        self._stop_flag.set()
        if self.take is not None:
            self.take.stop(fill_to)

    def join(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            self._thread.join()
        self._thread = None
        if self.take is not None:
            self.take.join()

    def close(self) -> None:
        self.stop()
        self.join()
        self._backend.close()

    # ------------------------------------------------------------ preview

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
                self.preview_timestamps.append(timestamp or time.time_ns())
                if array is not None and self.frame_for_display.push(array):
                    self.frame_for_display.refresh_fps(self.preview_timestamps)
        backend.stop_grab()
