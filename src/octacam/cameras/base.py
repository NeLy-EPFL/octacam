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
from pathlib import Path
from typing import ClassVar, Protocol

import numpy as np

from octacam.transform import DisplayTransform, apply_display_transform
from octacam.writer import AsyncFrameWriter, VideoFormat

log = logging.getLogger("octacam")

GRAB_TIMEOUT_MS = 100
WRITER_QUEUE_SIZE = 20

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

    def load_params(self, config_str: str) -> None: ...
    def save_params(self) -> str: ...

    def enable_frame_trigger(self) -> None: ...
    def set_trigger_source(self, use_software: bool) -> None: ...
    def begin_software_trigger_preview(self) -> None: ...
    def trigger_once(self) -> None: ...

    def start_grab_preview(self) -> None: ...
    def start_grab_record(self) -> bool: ...
    def stop_grab(self) -> None: ...
    def retrieve(
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
        self.frame_for_display = LatestFrame()
        self._video_writer: AsyncFrameWriter | None = None
        self._recorded_frame_size: tuple[int, int] | None = None
        self._stop_flag = threading.Event()
        # Serializes external node access (read/set/save and the W/H grab
        # cycle) against itself; the preview loop stays lock-free.
        self._param_lock = threading.RLock()
        self._thread: threading.Thread | None = None
        self._timestamps: list[int] = []
        self._dropped: list[bool] = []
        self._dropped_count = 0
        self._resulting_fps = 0.0
        self._started = False

    @property
    def _camera(self):
        """Back-compat/test shim: the underlying SDK camera handle, if any.

        Only meaningful for the Basler backend (whose ``raw`` is the pylon
        ``InstantCamera``); other backends expose no such handle.
        """
        return getattr(self._backend, "raw", None)

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
        return self._dropped_count

    @property
    def dropped_indices(self) -> list[int]:
        """Frame indices that were dropped (encoder/queue could not accept)."""
        return [i for i, dropped in enumerate(self._dropped) if dropped]

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

    def start_preview(self) -> None:
        self._stop_flag.clear()
        if not self._backend.is_open():
            return
        self._backend.begin_software_trigger_preview()
        self._timestamps.clear()
        self._dropped.clear()
        self._dropped_count = 0  # so preview never shows a stale recording count
        self._backend.start_grab_preview()
        self._thread = threading.Thread(target=self._preview_loop, daemon=True)
        self._thread.start()

    def start_record(
        self,
        save_path: str,
        fps: float,
        video_format: VideoFormat,
        record_form: str = "display",
        save_frame_timestamps: bool = False,
    ) -> bool:
        """Start recording; returns True iff the record loop was launched.

        ``record_form`` selects "display" (bake the camera's display transform
        into the video) or "sensor" (raw, untransformed). ``save_frame_timestamps``
        re-enables the per-frame timestamp CSV (off by default).
        """
        self._stop_flag.clear()
        self._started = False
        if not self._backend.is_open():
            return False
        self._timestamps.clear()
        self._dropped.clear()
        self._dropped_count = 0

        bake = record_form == "display" and not self.display_transform.is_identity
        transform = self.display_transform if bake else None

        sensor_size = (self._backend.width(), self._backend.height())
        frame_size = (
            self.display_transform.output_size(*sensor_size) if bake else sensor_size
        )
        self._recorded_frame_size = frame_size
        self._video_writer = video_format.create_writer(WRITER_QUEUE_SIZE)
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
            args=(save_path, transform, save_frame_timestamps),
            daemon=True,
        )
        self._thread.start()
        return True

    def stop(self) -> None:
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
        if not timestamp:
            timestamp = time.time_ns()
        self._timestamps.append(timestamp)

    def _update_resulting_fps(self, n_frames: int = 6) -> None:
        timestamps = self._timestamps
        if len(timestamps) < 2 or n_frames < 1:
            self._resulting_fps = 0.0
            return
        last = len(timestamps) - 1
        start = last - n_frames if last > n_frames else 0
        delta_ns = timestamps[last] - timestamps[start]
        self._resulting_fps = (last - start) * 1e9 / delta_ns if delta_ns else 0.0

    def _preview_loop(self) -> None:
        backend = self._backend
        while not self._stop_flag.is_set() and backend.is_grabbing():
            # Materialize the (copying) array only when the display slot is
            # free, keeping the per-frame copy off the steady state. The
            # timestamp is recorded for every successful grab regardless.
            frame = backend.retrieve(
                GRAB_TIMEOUT_MS, lambda: self.frame_for_display.wants_frame
            )
            if frame is not None:
                array, timestamp = frame
                self._store_timestamp(timestamp)
                if array is not None and self.frame_for_display.push(array):
                    self._update_resulting_fps()
        backend.stop_grab()

    def _record_loop(
        self,
        save_path: str,
        transform: DisplayTransform | None = None,
        save_frame_timestamps: bool = False,
    ) -> None:
        backend = self._backend
        frame_count = 0
        while not self._stop_flag.is_set() and backend.is_grabbing():
            frame = backend.retrieve(GRAB_TIMEOUT_MS, _ALWAYS)
            if frame is None:
                continue
            array, timestamp = frame
            if array is None:  # record always requests the array; defensive
                continue
            self._store_timestamp(timestamp)

            # Bake the display orientation into the recorded frame when asked;
            # the preview still gets the raw array (the browser applies the
            # transform via CSS), and identity/sensor recordings pay nothing.
            to_write = apply_display_transform(array, transform) if transform else array
            written = self._video_writer.write(to_write)  # pyright: ignore[reportOptionalMemberAccess]
            if not written:
                self._dropped_count += 1
                log.warning(
                    "Frame %d dropped for camera %s",
                    frame_count,
                    self.serial_number,
                )
            self._dropped.append(not written)

            if self.frame_for_display.push(array):
                self._update_resulting_fps()

            self._started = True
            frame_count += 1
        backend.stop_grab()
        self._video_writer.close()  # pyright: ignore[reportOptionalMemberAccess]
        self._reconcile_unwritten_frames()

        dropped_count = sum(self._dropped)
        log.info(
            "Camera %s: %d frames recorded, %d frames dropped",
            self.serial_number,
            frame_count,
            dropped_count,
        )
        if save_frame_timestamps:
            self._write_timestamps_csv(save_path)

    def _reconcile_unwritten_frames(self) -> None:
        """If the sink died, frames accepted into the queue after the failure
        were discarded rather than written. Mark that trailing run of
        accepted frames as dropped so the CSV reflects what reached the file.
        """
        if self._video_writer is None or not self._video_writer.failed:
            return
        written = self._video_writer.frames_written
        accepted = [i for i, dropped in enumerate(self._dropped) if not dropped]
        for index in accepted[written:]:
            self._dropped[index] = True
            self._dropped_count += 1

    def _write_timestamps_csv(self, save_path: str) -> None:
        csv_path = Path(save_path).with_suffix(".csv")
        with open(csv_path, "w") as csv_file:
            csv_file.write("frame_index,timestamp,dropped\n")
            for i, (timestamp, dropped) in enumerate(
                zip(self._timestamps, self._dropped, strict=False)
            ):
                csv_file.write(f"{i},{timestamp},{int(dropped)}\n")


def _ALWAYS() -> bool:
    return True
