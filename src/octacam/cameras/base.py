"""Vendor-neutral camera core.

The concrete :class:`Camera` owns everything that does not touch a camera SDK:
the preview/record grab loops, the lock-free display handoff
(:class:`LatestFrame`), timestamp/FPS tracking, drop accounting, the video
writer wiring, the geometry/reset grab-cycling, and the start/stop/join
lifecycle. The thin, SDK-specific seam is the :class:`CameraBackend` protocol —
each vendor (Basler, FLIR, the in-memory fake) implements those ~20 primitives
and nothing else, so the fragile loop/lifecycle code is written exactly once.

Backends raise :class:`BackendError` for SDK-level failures; the core catches it
where the original pypylon code caught ``genicam.GenericException`` and converts
it to ``ValueError`` at the points that previously did.
"""

import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import ClassVar, Protocol

import numpy as np

from octacam.pulses import PulseClock, PulseTracker
from octacam.transform import DisplayTransform, apply_display_transform
from octacam.writer import AsyncFrameWriter, VideoFormat

log = logging.getLogger("octacam")

GRAB_TIMEOUT_MS = 100
# Default depth of each camera's writer queue (frames buffered between the grab
# loop and the encoder). The grab loop never blocks: a frame that arrives while
# the queue is full is dropped. The bound must absorb a *transient* encoder
# stall (e.g. several GPU NVENC sessions warming up together) without dropping,
# while capping worst-case memory (depth x frame bytes x cameras). 64 frames is
# ~0.8 s of headroom at 80 fps; rigs can override it via record.writer_queue_size
# (raise it for high camera counts / bursty encoders, lower it to save memory).
WRITER_QUEUE_SIZE = 64
# Upper bound on the per-frame timestamp series kept during *preview* (the GUI's
# idle steady state, never periodically restarted): preview only needs the last
# few for the rolling fps readout, so the series is trimmed to this many instead
# of growing without bound. Recording keeps the full, untrimmed series.
PREVIEW_TIMESTAMPS_MAX = 64

# Editable sensor parameters, mapped from the GUI's snake_case names to their
# GenICam node names. These SFNC names (ExposureTime, Gain, Width, ...) are the
# standard ones shared by Basler and FLIR/Spinnaker, so every GenICam backend
# reuses this mapping. GEOMETRY_PARAMS can only be written while the camera is
# NOT grabbing (the SDK raises otherwise), so set_geometry cycles the preview;
# LIVE_PARAMS are writable on a running camera.
GEOMETRY_PARAMS = {"width": "Width", "height": "Height"}
LIVE_PARAMS = {
    "exposure": "ExposureTime",
    "gain": "Gain",
    "offset_x": "OffsetX",
    "offset_y": "OffsetY",
}
PARAM_NODES = {**GEOMETRY_PARAMS, **LIVE_PARAMS}

# --- Full-node-map (Camera tab) policy, in SFNC node names ---------------------
# The two ROI-size nodes: the SDK refuses these writes while grabbing, so a write
# is routed through set_geometry (which cycles the preview). Every other feature
# is written live.
GEOMETRY_FEATURES = frozenset({"Width", "Height"})
# The ROI-origin nodes and the per-axis center flag that auto-computes each. When
# a flag is on the node is derived from the sensor size and the ROI size, so the
# UI locks the field and octacam owns the value.
OFFSET_FEATURES = {"OffsetX": "center_x", "OffsetY": "center_y"}
# Nodes octacam drives itself at runtime; surfaced in the Camera tab read-only so
# an operator can see the value but cannot break preview/recording by editing it.
# PixelFormat is forced to Mono8 (the GRAY8 writer); DeviceLinkThroughputLimit is
# maximised at open() on the FLIR/Spinnaker backends (the other backends do not
# touch it) and is kept read-only everywhere so it can't be edited into a
# bandwidth mismatch; the Trigger* chain and AcquisitionMode are reprogrammed by
# the preview/record/benchmark grab paths; TLParamsLocked is transport state.
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
class NodeInfo:
    """One sensor parameter's current value, bounds, unit, and writability."""

    value: float | int
    min: float | None = None
    max: float | None = None
    inc: float | None = None
    unit: str | None = None
    writable: bool = False


@dataclass
class FeatureInfo:
    """One node from the full device node map, for the Camera tab's browser.

    ``type`` is the widget kind — ``"int"``/``"float"`` (number box with
    min/max/inc), ``"bool"`` (checkbox), ``"enum"`` (dropdown of ``entries``),
    ``"string"`` (text), ``"command"`` (button), or ``"category"`` (a group
    header). ``value`` is ``None`` for command/category nodes and for an enum
    holds the current symbolic string. ``entries`` lists an enum's selectable
    symbolics (each ``{"value", "display", "available"}``). ``category`` is the
    enclosing GenApi category, used to group the browser. ``managed`` is set by
    the Camera core for nodes octacam drives itself (see
    :data:`RUNTIME_MANAGED_FEATURES`) so the UI locks them with a note.
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
        # Only include the type-specific keys that apply, keeping the payload
        # small and the client's rendering unambiguous.
        for key in ("min", "max", "inc", "unit", "tooltip", "entries"):
            v = getattr(self, key)
            if v is not None:
                d[key] = v
        return d


# --- Curated feature fallback --------------------------------------------------
# For a backend that cannot introspect its full node map (the pycameleon floor
# and the Spinnaker C-API tier), the Camera tab degrades to the six PARAM_NODES,
# built as FeatureInfo from the backend's existing read_node/write_node. The
# basler backend and the fake override this with a full GenApi node-map walk.
_SFNC_TO_SNAKE = {sfnc: snake for snake, sfnc in PARAM_NODES.items()}
_CURATED_CATEGORY = {
    "Width": "ImageFormatControl",
    "Height": "ImageFormatControl",
    "OffsetX": "ImageFormatControl",
    "OffsetY": "ImageFormatControl",
    "ExposureTime": "AcquisitionControl",
    "Gain": "AnalogControl",
}


def curated_list_features(backend) -> list["FeatureInfo"]:
    """The six PARAM_NODES as FeatureInfo (fallback for non-introspectable SDKs)."""
    out: list[FeatureInfo] = []
    for snake, sfnc in PARAM_NODES.items():
        try:
            info = backend.read_node(snake)
        except BackendError:
            continue
        out.append(_curated_feature(sfnc, info))
    return out


def _curated_feature(sfnc: str, info: "NodeInfo") -> "FeatureInfo":
    kind = "int" if _SFNC_TO_SNAKE.get(sfnc) in ("width", "height", "offset_x", "offset_y") else "float"
    return FeatureInfo(
        name=sfnc,
        display_name=sfnc,
        type=kind,
        category=_CURATED_CATEGORY.get(sfnc, "Other"),
        value=info.value,
        min=info.min,
        max=info.max,
        inc=info.inc,
        unit=info.unit,
        readable=True,
        writable=info.writable,
    )


def curated_read_feature(backend, name: str) -> "FeatureInfo":
    snake = _SFNC_TO_SNAKE.get(name)
    if snake is None:
        raise BackendError(f"{name} is not available on this backend")
    return _curated_feature(name, backend.read_node(snake))


def curated_write_feature(backend, name: str, value: object) -> None:
    snake = _SFNC_TO_SNAKE.get(name)
    if snake is None:
        raise BackendError(f"{name} is not editable on this backend")
    is_int = snake in ("width", "height", "offset_x", "offset_y")
    backend.write_node(snake, int(float(value)) if is_int else float(value))


# A retrieved frame: the (owned) image array — or None when the caller did not
# ask for it (preview drops the copy when the display slot is full) — plus the
# camera/host timestamp in nanoseconds.
Frame = tuple[np.ndarray | None, int]


class CameraBackend(Protocol):
    """The SDK-specific seam the concrete :class:`Camera` drives.

    Implementations wrap a single physical camera. Methods that touch the
    device raise :class:`BackendError` on SDK failure.
    """

    extension: ClassVar[str]

    @property
    def serial_number(self) -> str: ...
    def open(self) -> None: ...
    def close(self) -> None: ...
    def is_open(self) -> bool: ...
    def is_grabbing(self) -> bool: ...
    def width(self) -> int: ...
    def height(self) -> int: ...

    def read_node(self, name: str) -> NodeInfo: ...
    def write_node(self, name: str, value: float) -> None: ...

    # Full device node map (Camera tab). A backend that cannot introspect its
    # node map returns an empty list from ``list_features`` and the Camera tab
    # falls back to the curated PARAM_NODES quick controls. ``name`` is a GenApi
    # (SFNC or vendor) node name; ``value`` is coerced to the node's type by the
    # backend. All four raise :class:`BackendError` on SDK failure.
    def list_features(self) -> list[FeatureInfo]: ...
    def read_feature(self, name: str) -> FeatureInfo: ...
    def write_feature(self, name: str, value: object) -> None: ...
    def execute_command(self, name: str) -> None: ...

    # The SFNC node names this camera can write only while it is NOT grabbing
    # (the SDK/firmware locks them during acquisition). Width/Height are locked
    # on every GenICam camera; some vendors (Basler) additionally lock the ROI
    # offsets, while others (FLIR/Teledyne) leave them live-writable. The shared
    # Camera layer presents these editable whenever the camera is open and routes
    # their writes through a preview grab cycle. Defaults to Width/Height.
    def grab_locked_features(self) -> frozenset[str]: ...

    def load_params(self, config_str: str) -> None: ...
    def save_params(self) -> str: ...

    # Parse a saved per-camera config text (this backend's native format) into a
    # ``{node_name: value_string}`` map, used by the per-field "reset to config"
    # button. An unparseable/empty text yields ``{}``.
    def config_values(self, config_str: str) -> dict[str, str]: ...

    def enable_frame_trigger(self) -> None: ...
    def set_trigger_source(self, use_software: bool) -> None: ...
    def begin_software_trigger_preview(self) -> None: ...
    def trigger_once(self) -> None: ...

    # Free-run (continuous, no per-frame trigger). Used by the benchmark to
    # measure the external-trigger-equivalent acquisition ceiling (``fps=None``,
    # uncapped) and by free-run *preview* (``fps`` set, capping the rate at the
    # target so the preview matches an fps-equal recording's bandwidth).
    # ``begin_freerun`` returns False when the backend cannot arm free-run so the
    # caller skips it, and ``retrieve_freerun`` fetches the next produced frame.
    def begin_freerun(self, fps: float | None = None) -> bool: ...
    def retrieve_freerun(
        self, timeout_ms: int, wants_array: Callable[[], bool]
    ) -> Frame | None: ...

    def start_grab_preview(self) -> None: ...
    def start_grab_record(self) -> bool: ...
    def stop_grab(self) -> None: ...
    def retrieve(
        self, timeout_ms: int, wants_array: Callable[[], bool]
    ) -> Frame | None: ...

    # Optional external-trigger record fetch. The record loop discovers it with
    # ``getattr(backend, "retrieve_external", None)`` and falls back to
    # ``retrieve_freerun`` when a backend does not define it, so a real hardware
    # backend (whose externally-triggered frames arrive on their own) simply omits
    # it. Only a backend that models the external source itself — the in-memory
    # fake, via its trigger counter — implements it, so an external recording with
    # no pulses correctly yields nothing. Declared here to document the seam the
    # core relies on; it is not required for structural conformance.
    def retrieve_external(
        self, timeout_ms: int, wants_array: Callable[[], bool]
    ) -> Frame | None: ...


def snap_value(value: float, info: NodeInfo) -> float:
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
    """Coerce an arbitrary config/feature value to ``bool``.

    A real ``bool`` passes through unchanged; a number is truthy when non-zero;
    anything else is parsed as a string flag (``"1"``/``"true"``/``"yes"``/``"on"``,
    case-insensitive). Shared by every backend's boolean feature write and the
    native GenApi TSV config applier so the coercion is defined once.
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    return str(value).strip().lower() in ("1", "true", "yes", "on")


class LatestFrame:
    """Single-slot frame handoff to the GUI (FrameForDisplay in C++).

    The producer stores a frame only when the previous one has been consumed,
    so producer-side copies happen at most at the GUI refresh rate.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._frame: np.ndarray | None = None

    @property
    def wants_frame(self) -> bool:
        # Racy peek, but with a single producer a stale True only costs one
        # extra attempt and a stale False one skipped preview frame.
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
        # The serial number comes from enumeration, so it is available before
        # open(). Keeping open() out of the constructor lets CameraSystem open
        # every camera concurrently (see CameraSystem._run_parallel).
        self.serial_number: str = backend.serial_number
        self.name: str = self.serial_number
        self.width = 0
        self.height = 0
        # The persisted display orientation (rotation/flips), applied to the
        # video when recording in "display" form. Set from config at load time;
        # identity until then.
        self.display_transform = DisplayTransform()
        # Auto-center the ROI: when set, OffsetX/OffsetY are derived from the
        # sensor size and the ROI size and recomputed on every geometry change,
        # so the ROI stays centered. Set from config at load time (see
        # CameraSystem.apply_display_config).
        self.center_x = False
        self.center_y = False
        # First-seen value of each node, captured lazily in list_features(). A
        # node outside the saved config file is never written by octacam, so its
        # first-read value is the camera's factory/power-on default — the
        # fallback for a per-field reset when the config has no saved value.
        self._default_cache: dict[str, object] = {}
        self.frame_for_display = LatestFrame()
        self._video_writer: AsyncFrameWriter | None = None
        self._recorded_frame_size: tuple[int, int] | None = None
        self._stop_flag = threading.Event()
        # Serializes external node access (read/set/save and the W/H grab
        # cycle) against itself; the preview loop stays lock-free.
        self._param_lock = threading.RLock()
        self._thread: threading.Thread | None = None
        # The per-video-frame series of the last recording, all parallel: the
        # frame's timestamp (for a filled frame, when its pulse was due), whether
        # it is a fill rather than an image of its own pulse (``_dropped``), and
        # of those whether the camera never delivered that pulse at all
        # (``_missed``, vs. a frame the writer queue refused), its trigger pulse,
        # and the host wall-clock time it was delivered (0 for a fill).
        self._timestamps: list[int] = []
        self._dropped: list[bool] = []
        self._missed: list[bool] = []
        self._pulse_index: list[int] = []
        self._arrival_ns: list[int] = []
        self._dropped_count = 0
        self._writer_dropped = 0
        # Pulse accounting of the last recording (see octacam.pulses). Frames
        # that belong to no pulse of the train are discarded as extra.
        self._tracker: PulseTracker | None = None
        self._pulse_clock: PulseClock | None = None
        self._extra_frames = 0
        self._unclocked_frames = 0
        self._fill_to: int | None = None
        # Frames whose backend timestamp was 0 and fell back to host time_ns.
        # A per-recording provenance signal (see timestamp_source in the summary):
        # normally 0 (all hardware) or == frames (host-only backend like
        # pycameleon); anything in between flags a stray-zero anomaly. (The fake
        # deliberately supplies a nonzero host-derived timestamp, so it never
        # falls back and is classified "hardware".)
        self._host_fallback_count = 0
        self._resulting_fps = 0.0
        self._started = False

    @property
    def _camera(self):
        """Back-compat/test shim: the underlying SDK camera handle, if any.

        Only meaningful for the Basler backend (whose ``raw`` is the pylon
        ``InstantCamera``); other backends expose no such handle.
        """
        return getattr(self._backend, "raw", None)

    @property
    def backend(self) -> CameraBackend:
        """The underlying :class:`CameraBackend` (for diagnostics/advanced drivers).

        The normal grab/record lifecycle goes through this ``Camera``'s own
        methods; this read-only accessor exists so the diagnostics engine
        (:mod:`octacam.diagnostics`) can drive the *real* acquisition path
        (``trigger_once``/``retrieve``) in its own instrumented measurement
        loops without reaching into a private attribute. It must not be used to
        bypass the recording state machine during normal operation.
        """
        return self._backend

    @property
    def extension(self) -> str:
        """The parameter-file suffix this camera's backend persists (no dot).

        Per-camera (``pfs`` for Basler, ``txt`` — the native GenApi persistence
        TSV — for the FLIR/GenICam backends) so a rig mixing vendors reads and
        writes each camera's sensor params in its own native format.
        """
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
        """Video frames of the last recording that are not an image of their own
        pulse: pulses the camera missed plus frames the writer refused — each is
        filled with the previous frame so the video keeps one frame per pulse."""
        return self._dropped_count

    @property
    def dropped_indices(self) -> list[int]:
        """Video frame indices that are fills (see :attr:`dropped_count`)."""
        return [i for i, dropped in enumerate(self._dropped) if dropped]

    @property
    def missed_count(self) -> int:
        """How many trigger pulses the current/last recording has missed (cheap:
        for live telemetry)."""
        return len(self._tracker.missed) if self._tracker is not None else 0

    @property
    def writer_dropped(self) -> int:
        """Frames the camera delivered but the writer queue could not accept."""
        return self._writer_dropped

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
        """Per-frame timestamps (ns) of the last recording — hardware when the
        backend supplied one, else host ``time.time_ns()`` (see
        :attr:`host_fallback_count`). A copy, so the caller can't mutate state."""
        return list(self._timestamps)

    @property
    def frame_dropped(self) -> list[bool]:
        """Per-frame fill flags of the last recording (parallel to
        :attr:`frame_timestamps`). A copy."""
        return list(self._dropped)

    @property
    def frame_missed(self) -> list[bool]:
        """Per-frame: the camera delivered no image for this frame's pulse."""
        return list(self._missed)

    @property
    def frame_pulse_index(self) -> list[int]:
        """Per-frame trigger pulse index (``frame k`` is pulse ``k`` whenever the
        train is filled)."""
        return list(self._pulse_index)

    @property
    def frame_arrival_ns(self) -> list[int]:
        """Per-frame host wall-clock delivery time (0 for a filled frame)."""
        return list(self._arrival_ns)

    @property
    def host_fallback_count(self) -> int:
        """How many of the recorded frames fell back to host time because the
        backend reported no hardware timestamp."""
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
        return (len(timestamps) - 1) * 1e9 / span_ns if span_ns else 0.0

    @property
    def recorded_frame_size(self) -> tuple[int, int] | None:
        """The (width, height) actually written for the last recording.

        Equals the sensor size, or the transform's output size when the
        display transform was baked in (a 90°/270° rotation swaps the axes).
        """
        return self._recorded_frame_size

    @property
    def pixel_format(self) -> str:
        """Pixel format of recorded frames. Mono8 is the invariant across every
        backend today (FLIR forces it; Basler/fake frames are GRAY8); it is
        recorded into recording_summary.json so a later transcode of a raw dump
        knows how to interpret the byte stream."""
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
            info = self._backend.read_node(name)
            # Geometry is "editable" whenever the camera is open even though
            # IsWritable is False mid-preview (set_geometry cycles the grab);
            # for live params the raw writability is meaningful (e.g. a model
            # without Gain control).
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

    def read_params(self) -> dict[str, dict]:
        """Descriptors for every editable param the camera actually exposes."""
        params: dict[str, dict] = {}
        for name in PARAM_NODES:
            try:
                params[name] = self.read_param(name)
            except BackendError:
                continue  # node unavailable on this model
        return params

    def set_live_param(self, name: str, value: float) -> dict:
        """Set a param writable on a running camera (exposure/gain/offset)."""
        if name not in LIVE_PARAMS:
            raise ValueError(f"{name} cannot be set live")
        with self._param_lock:
            info = self._backend.read_node(name)
            target = snap_value(float(value), info)
            if isinstance(info.value, int):
                target = int(round(target))
            try:
                self._backend.write_node(name, target)
            except BackendError as e:
                raise ValueError(str(e)) from None
        return self.read_param(name)

    def set_geometry(
        self, *, width: int | None = None, height: int | None = None
    ) -> dict:
        """Set Width/Height, transparently cycling this camera's preview grab.

        The SDK refuses Width/Height writes while grabbing, so the preview loop
        is stopped and (if it was running) restarted around the write. The
        cached size and the display placeholder are refreshed so downstream
        consumers (preview encoder, GUI) immediately see the new ROI. Preview
        is always restored, even when the device rejects the value.
        """
        with self._param_lock:
            was_grabbing = self._backend.is_grabbing()
            if was_grabbing:
                self.stop()
                self.join()
            error: ValueError | None = None
            try:
                if height is not None:
                    info = self._backend.read_node("height")
                    self._backend.write_node("height", int(snap_value(height, info)))
                if width is not None:
                    info = self._backend.read_node("width")
                    self._backend.write_node("width", int(snap_value(width, info)))
            except BackendError as e:
                error = ValueError(str(e))
            self.width = self._backend.width()
            self.height = self._backend.height()
            # A ROI resize changes each offset's valid range, so re-center any
            # auto-centered axis while still stopped — AFTER refreshing the
            # cached size, since centering derives the offset from it. Best-effort:
            # a device that rejects it keeps its offset.
            if error is None:
                self._recenter_offsets_locked()
            self.frame_for_display.pop()
            self.frame_for_display.push(
                np.zeros((self.height, self.width), dtype=np.uint8)
            )
            params = self.read_params()  # read while stopped for clean values
            if was_grabbing:
                self.start_preview()
            if error is not None:
                raise error
        return {"width": self.width, "height": self.height, "params": params}

    def reset_params(self, config_str: str) -> dict:
        """Re-apply this camera's config snapshot, cycling the preview grab.

        Restores every sensor parameter to the value the active config shipped
        (exactly what load_config applied at startup). The full reload includes
        Width/Height, which the SDK refuses mid-grab, so the preview is stopped
        and restored around the write, as set_geometry does. An empty
        ``config_str`` (no saved params for this camera) leaves the camera
        untouched and just reports its current parameters.
        """
        with self._param_lock:
            if not config_str:
                return {
                    "width": self.width,
                    "height": self.height,
                    "params": self.read_params(),
                }
            was_grabbing = self._backend.is_grabbing()
            if was_grabbing:
                self.stop()
                self.join()
            self.frame_for_display.pop()  # drop stale frame so it reshapes
            error: ValueError | None = None
            try:
                self.load_params(config_str)
            except BackendError as e:
                # A config the device rejects (wrong model/firmware, hand-edited,
                # out-of-range value) must not strand the preview: mirror
                # set_geometry and always restore it, refreshing the placeholder
                # to the current ROI, before re-raising as a ValueError.
                error = ValueError(str(e))
                self.width = self._backend.width()
                self.height = self._backend.height()
                self.frame_for_display.push(
                    np.zeros((self.height, self.width), dtype=np.uint8)
                )
            params = self.read_params()  # read while stopped for clean values
            if was_grabbing:
                self.start_preview()
            if error is not None:
                raise error
        return {"width": self.width, "height": self.height, "params": params}

    def save_params(self) -> str:
        """Full config text of the current parameters (round-trips load_params)."""
        if not self._backend.is_open():
            return ""
        with self._param_lock:
            return self._backend.save_params()

    # ------------------------------------------------- full device node map

    def _grab_locked_features(self) -> frozenset[str]:
        """SFNC nodes this camera can write only while not grabbing.

        Declared by the backend (Width/Height universally; Basler also locks the
        ROI offsets during acquisition). The Camera tab presents these editable
        whenever the camera is open and routes their writes through a grab cycle.
        A backend that predates the seam falls back to Width/Height."""
        getter = getattr(self._backend, "grab_locked_features", None)
        return frozenset(getter()) if getter else GEOMETRY_FEATURES

    def _annotate(self, feature: FeatureInfo, grab_locked: frozenset[str]) -> FeatureInfo:
        """Apply octacam's cross-backend policy to one raw backend feature.

        Marks runtime-managed nodes read-only (so the operator sees them but
        cannot break preview/recording); locks an offset node whose axis is
        auto-centered; and keeps a grab-locked node (one the SDK refuses to write
        mid-preview — Width/Height everywhere, plus the ROI offsets on vendors
        like Basler that lock them during acquisition) editable while the camera
        is open, since set_feature cycles the grab to write it. A node the SDK
        reports writable mid-grab (e.g. a FLIR offset) keeps that live writability
        untouched."""
        if feature.name in RUNTIME_MANAGED_FEATURES:
            feature.managed = True
            feature.writable = False
        elif feature.name in OFFSET_FEATURES and getattr(self, OFFSET_FEATURES[feature.name]):
            feature.writable = False  # octacam derives it; UI shows locked
        elif feature.name in grab_locked:
            feature.writable = self._backend.is_open()
        if feature.value is not None:
            # Cache the first-seen value as the factory-default fallback (see
            # _default_cache); managed nodes are never reset so are irrelevant.
            self._default_cache.setdefault(feature.name, feature.value)
        return feature

    def list_features(self) -> list[dict]:
        """Every node the backend exposes, annotated with octacam's policy.

        Returns ``[]`` for a backend that cannot introspect its node map; the
        Camera tab then falls back to the curated PARAM_NODES quick controls."""
        with self._param_lock:
            features = self._backend.list_features()
            grab_locked = self._grab_locked_features()
        return [self._annotate(f, grab_locked).as_dict() for f in features]

    def read_feature(self, name: str) -> dict:
        """One node's current descriptor, annotated with octacam's policy."""
        with self._param_lock:
            feature = self._backend.read_feature(name)
            grab_locked = self._grab_locked_features()
        return self._annotate(feature, grab_locked).as_dict()

    def _centered_offset(self, node_name: str) -> int | None:
        """The offset value that centers the ROI on ``node_name``'s axis.

        Sensor size comes from ``WidthMax``/``HeightMax`` when the model exposes
        it, else from the offset node's own max (which the SDK reports as
        ``sensor - size``). Returns None if the offset node is unavailable."""
        try:
            info = self._backend.read_node(
                "offset_x" if node_name == "OffsetX" else "offset_y"
            )
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
            snake = "offset_x" if node_name == "OffsetX" else "offset_y"
            try:
                self._backend.write_node(snake, target)
            except BackendError as e:
                log.debug("Could not center %s on %s: %s", node_name, self.serial_number, e)

    def _run_stopped(self, fn: Callable[[], None]) -> None:
        """Run ``fn`` with the preview grab stopped, restoring it afterwards.

        Caller holds ``_param_lock``. For node writes the SDK refuses mid-grab
        (a Basler ROI offset, like Width/Height): the preview is stopped and — if
        it was running — restarted around ``fn``, whose exception still propagates
        after the restart (mirrors set_geometry's restore-on-error guarantee)."""
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
        """Toggle ROI auto-centering on an axis and re-center it immediately.

        ``axis`` is ``"x"`` or ``"y"``. Enabling derives the offset now (and on
        every later geometry change); disabling frees the field. Backends that
        lock the offsets mid-grab (Basler) need the preview cycled around the
        write; where the offsets stay live-writable (FLIR) it happens in place."""
        if axis not in ("x", "y"):
            raise ValueError(f"Unknown center axis: {axis}")
        with self._param_lock:
            setattr(self, f"center_{axis}", bool(enabled))
            if enabled:
                if OFFSET_FEATURES.keys() & self._grab_locked_features():
                    self._run_stopped(self._recenter_offsets_locked)
                else:
                    self._recenter_offsets_locked()
        return {"center_x": self.center_x, "center_y": self.center_y}

    def set_feature(self, name: str, value: object) -> None:
        """Write one arbitrary node, routing grab-locked nodes through a grab cycle.

        Rejects nodes octacam manages and offset nodes on an auto-centered axis.
        ROI size (Width/Height) and any other node the backend locks mid-grab
        (e.g. a Basler ROI offset) are written with the preview grab cycled; a
        geometry resize also recomputes any auto-centered offset. Everything else
        is written live on the running camera."""
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
            if name in self._grab_locked_features():
                self._write_feature_stopped(name, value)
            else:
                try:
                    self._backend.write_feature(name, value)
                except BackendError as e:
                    raise ValueError(str(e)) from None

    def reset_feature(self, name: str, config_str: str) -> None:
        """Reset one node to its saved-config value, else its factory default.

        The config's value for ``name`` wins; a node absent from the config
        falls back to the value first read this session (its factory/power-on
        state, since octacam never wrote it). A node with neither is left as-is.
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
        """Arm the FrameStart software trigger, as start_preview does.

        Needed before headless recording: the config files ship with
        TriggerMode Off, and without it a software trigger is ignored.
        """
        self._backend.enable_frame_trigger()

    def set_trigger_source(self, use_software_trigger: bool) -> None:
        self._backend.set_trigger_source(use_software_trigger)

    def trigger_once(self) -> None:
        self._backend.trigger_once()

    # ------------------------------------------------------------- grabbing

    def start_preview(self, mode: str = "software", fps: float | None = None) -> None:
        """Start live preview in one of three trigger modes.

        ``mode`` selects how the cameras are clocked during preview so it can
        approximate how the recording will be triggered:

        * ``"software"`` — octacam's FrameStart software trigger (driven by the
          shared software-trigger timer the controller starts), synchronized across
          cameras; frames fetched via the software-trigger hand-off.
        * ``"free_running"`` — the camera free-runs with its rate capped at ``fps``
          (so preview draws the same bandwidth, and reports the same rate, as an
          fps-matched recording). Approximates a truly external / unmanaged trigger
          octacam cannot drive.
        * ``"managed"`` — the cameras run in *hardware*-trigger mode waiting for an
          octacam-driven external source (a trigger plugin the controller arms);
          frames fetched un-gated like an external recording.

        ``free_running`` and ``managed`` fetch without firing a software trigger
        (mirroring :meth:`start_record`'s external path); neither uses the shared
        software-trigger timer.
        """
        self._stop_flag.clear()
        if not self._backend.is_open():
            return
        if mode == "free_running":
            # begin_freerun returns False when the backend cannot arm free-run;
            # fall back to a software-trigger preview so preview shows frames
            # instead of grabbing forever in a mode the camera was never armed for.
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
    ) -> bool:
        """Start recording; returns True iff the record loop was launched.

        ``record_form`` selects "display" (bake the camera's display transform
        into the video) or "sensor" (raw, untransformed). The per-frame timestamp
        series is always accumulated (it feeds recording_summary.json); the
        controller decides whether to persist it. ``software_trigger``
        picks the grab path: the software-trigger hand-off (this process drives each
        frame) when True, or a plain fetch of externally-triggered frames when
        False. An external trigger never bumps the hand-off counter, so gating the
        record loop on it (``retrieve``) would capture nothing — external recording
        must use the un-gated ``retrieve_freerun`` fetch instead. ``queue_size``
        bounds the writer queue that buffers frames between the grab loop and the
        encoder (see :data:`WRITER_QUEUE_SIZE`).

        ``pulse_clock`` is the trigger train the recording is clocked by: every
        frame is assigned to its pulse (see :mod:`octacam.pulses`), a missed pulse
        is filled with the previous frame when the clock says so, and the loop
        ends once the train's last pulse is accounted for. Without one, a clock is
        derived from ``fps`` and ``max_frames`` (a legacy fixed frame count).
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

        # The writer (ffmpeg child + threads) is now live; close it on ANY
        # failure below so a failed start (e.g. "insufficient resources")
        # cannot orphan the child process and its threads.
        try:
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

        self._thread = threading.Thread(
            target=self._record_loop,
            args=(transform, software_trigger),
            daemon=True,
        )
        self._thread.start()
        return True

    def stop(self, fill_to: int | None = None) -> None:
        """Stop the grab loop. ``fill_to`` (a completed train's pulse count) has
        a recording pad the video with repeats up to that many frames, so a
        camera that missed the train's last pulses still ends aligned."""
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
        self._store_timestamp_fallback(timestamp)
        self._timestamps.append(timestamp or time.time_ns())

    def _store_timestamp_fallback(self, timestamp: int) -> None:
        if not timestamp:  # backend supplied no hardware timestamp for this frame
            self._host_fallback_count += 1

    def _update_resulting_fps(self, n_frames: int = 6) -> None:
        timestamps = self._timestamps
        if len(timestamps) < 2 or n_frames < 1:
            self._resulting_fps = 0.0
            return
        last = len(timestamps) - 1
        start = last - n_frames if last > n_frames else 0
        delta_ns = timestamps[last] - timestamps[start]
        self._resulting_fps = (last - start) * 1e9 / delta_ns if delta_ns else 0.0

    def _preview_loop(self, mode: str = "software") -> None:
        backend = self._backend
        # Match the fetch to the arm chosen in start_preview: the software-trigger
        # hand-off for "software", else the un-gated free-run fetch. Both
        # free_running and managed use retrieve_freerun — a free-running or
        # plugin/hardware-triggered camera produces frames on its own, so preview
        # just grabs the next one. (Unlike _record_loop's external path, preview
        # does NOT use retrieve_external: that gates on a software-trigger pulse to
        # model an unpulsed external *recording*, which would wrongly freeze a
        # managed preview that is clocked by the plugin, not the software timer.)
        retrieve = backend.retrieve if mode == "software" else backend.retrieve_freerun
        while not self._stop_flag.is_set() and backend.is_grabbing():
            # Materialize the (copying) array only when the display slot is
            # free, keeping the per-frame copy off the steady state. The
            # timestamp is recorded for every successful grab regardless.
            frame = retrieve(
                GRAB_TIMEOUT_MS, lambda: self.frame_for_display.wants_frame
            )
            if frame is not None:
                array, timestamp = frame
                self._store_timestamp(timestamp)
                # Preview runs indefinitely, so bound the series (the fps readout
                # only reads the last few) instead of leaking a timestamp per frame.
                if len(self._timestamps) > PREVIEW_TIMESTAMPS_MAX:
                    del self._timestamps[0]
                if array is not None and self.frame_for_display.push(array):
                    self._update_resulting_fps()
        backend.stop_grab()

    def _reset_series(self) -> None:
        self._timestamps.clear()
        self._dropped.clear()
        self._missed.clear()
        self._pulse_index.clear()
        self._arrival_ns.clear()
        self._dropped_count = 0
        self._writer_dropped = 0
        self._host_fallback_count = 0
        self._extra_frames = 0
        self._unclocked_frames = 0
        self._tracker = None

    def _append_row(
        self, timestamp: int, pulse: int, *, missed: bool, dropped: bool, arrival: int
    ) -> None:
        self._timestamps.append(timestamp)
        self._pulse_index.append(pulse)
        self._missed.append(missed)
        self._dropped.append(dropped)
        self._arrival_ns.append(arrival)
        if dropped:
            self._dropped_count += 1

    def _record_loop(
        self,
        transform: DisplayTransform | None = None,
        software_trigger: bool = True,
    ) -> None:
        backend = self._backend
        tracker = self._tracker
        clock = self._pulse_clock
        assert tracker is not None and clock is not None
        # With an external trigger the frames arrive on their own; the
        # software-trigger hand-off (retrieve) would block forever waiting for a
        # pending count that only the software trigger timer bumps. Fetch
        # externally-triggered frames without firing a software trigger instead:
        # real backends just grab the next produced frame (retrieve_freerun); a
        # backend that models the external source itself (the fake, via its
        # trigger counter) provides retrieve_external.
        if software_trigger:
            retrieve = backend.retrieve
        else:
            retrieve = getattr(backend, "retrieve_external", None) or (
                backend.retrieve_freerun
            )
        # Video frames owed to the file and not yet queued: fills for missed
        # pulses (when the clock fills) and frames the writer refused. They ride
        # on the next queued frame (or on close) so the video keeps exactly one
        # frame per pulse.
        owed = 0
        writer = self._video_writer
        assert writer is not None
        while not self._stop_flag.is_set() and backend.is_grabbing():
            # Stop on the pulse count, not a frame count: the train's last pulse
            # has its frame (or was accounted missed), so every camera ends on
            # the same pulse whatever it missed on the way.
            if tracker.complete:
                break
            frame = retrieve(GRAB_TIMEOUT_MS, _ALWAYS)
            if frame is None:
                continue
            array, timestamp = frame
            if array is None:  # record always requests the array; defensive
                continue
            host_ns = time.monotonic_ns()
            arrival = time.time_ns()
            index = getattr(backend, "last_trigger_index", None)
            if index is not None:
                assignment = tracker.assign_index(index)
            elif timestamp:
                assignment = tracker.assign(timestamp, host_ns)
            else:
                # No hardware timestamp (a host-clocked backend): nothing places
                # the frame on the trigger clock, so take it as the next pulse.
                assignment = tracker.assign_index(tracker.next_pulse)
                self._unclocked_frames += 1
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
                for missed_pulse in assignment.missed:
                    due = tracker.expected_ts(missed_pulse)
                    if due is None:  # a leading miss: count back from this frame
                        due = (timestamp or 0) - (pulse - missed_pulse) * clock.period_ns
                    self._append_row(
                        due, missed_pulse, missed=True, dropped=True, arrival=0
                    )
                owed += len(assignment.missed)
                log.warning(
                    "Camera %s missed trigger pulse(s) %s; filled with the "
                    "previous frame",
                    self.serial_number,
                    _format_pulses(assignment.missed),
                )
            self._store_timestamp_fallback(timestamp)

            # Bake the display orientation into the recorded frame when asked;
            # the preview still gets the raw array (the browser applies the
            # transform via CSS), and identity/sensor recordings pay nothing.
            to_write = apply_display_transform(array, transform) if transform else array
            written = writer.write(to_write, fill_before=owed)
            if written:
                owed = 0
            else:
                owed += 1
                self._writer_dropped += 1
                log.warning(
                    "Frame for pulse %d dropped for camera %s (writer queue full); "
                    "it will be filled with the previous frame",
                    pulse,
                    self.serial_number,
                )
            self._append_row(
                timestamp or time.time_ns(),
                pulse,
                missed=False,
                dropped=not written,
                arrival=arrival,
            )

            if self.frame_for_display.push(array):
                self._update_resulting_fps()

            self._started = True
        backend.stop_grab()
        # A counted train that ran to its end: pad the pulses this camera missed
        # at the very end, so it ends on the same pulse as the others. (Only
        # after at least one frame — there is nothing to repeat otherwise.)
        fill_to = self._fill_to
        if (
            clock.fill
            and fill_to is not None
            and clock.count is not None
            and self._timestamps
        ):
            end = min(fill_to, clock.count)
            trailing = range(tracker.next_pulse, end)
            for missed_pulse in trailing:
                self._append_row(
                    tracker.expected_ts(missed_pulse) or 0,
                    missed_pulse,
                    missed=True,
                    dropped=True,
                    arrival=0,
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
            "filled), %d extra frames discarded",
            self.serial_number,
            len(self._timestamps),
            sum(self._missed),
            self._writer_dropped,
            self._extra_frames,
        )

    def _reconcile_unwritten_frames(self) -> None:
        """If the sink died, everything queued after the failure was discarded
        rather than written, so the file ends at ``frames_written``. Mark the
        rows past that point as dropped so the series reflects what reached the
        file.
        """
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
