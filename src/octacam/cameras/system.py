"""Multi-camera orchestration, independent of any camera SDK.

``CameraSystem`` enumerates and opens the selected backend's cameras, drives
them in parallel (each SDK releases the GIL on its blocking calls, so opening /
loading / starting N cameras takes about one camera's time), and owns the
shared software-trigger timer. The backend is chosen once at construction; the
rest of the system only ever sees :class:`~octacam.cameras.base.Camera`.
"""

import logging
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import TYPE_CHECKING

import numpy as np

from octacam.cameras.base import BackendError, Camera
from octacam.cameras.registry import select_backend, teardown_backend
from octacam.transform import DisplayTransform, from_camera_config
from octacam.trigger import PreciseTimer
from octacam.writer import VideoFormat

if TYPE_CHECKING:
    from octacam.config import CameraConfig

log = logging.getLogger("octacam")


class CameraSystem:
    def __init__(
        self,
        requested_serial_numbers: list[str] | None = None,
        backend: str = "basler",
    ):
        self.cameras: list[Camera] = []
        self._trigger_timer = PreciseTimer(self._trigger_all)

        enumerate_fn, make_backend, extension = select_backend(backend)
        self.backend = backend
        self.extension = extension

        # Enumerate on this thread (the factory is shared and creating device
        # handles is cheap), then open them all at once below.
        entries = enumerate_fn(requested_serial_numbers)
        if not entries:
            return
        for _serial, handle in entries:
            self.cameras.append(Camera(make_backend(handle)))

        # Open in parallel: each open() blocks on USB round-trips with the GIL
        # released, so 8 cameras open in roughly the time one used to take.
        failures = [
            (camera, exc)
            for camera, _result, exc in self._run_parallel(lambda c: c.open())
            if exc is not None
        ]
        if failures:
            for camera in self.cameras:
                camera.close()  # close() no-ops on cameras that never opened
            teardown_backend(self.backend)
            camera, exc = failures[0]
            log.error("Failed to open camera %s", camera.serial_number)
            raise exc

    def __len__(self) -> int:
        return len(self.cameras)

    def __iter__(self):
        return iter(self.cameras)

    def camera_at(self, index: int) -> Camera:
        if not 0 <= index < len(self.cameras):
            raise IndexError(f"No camera at index {index}")
        return self.cameras[index]

    def apply_to_all(self, fn) -> list:
        """Run fn(camera) across all cameras concurrently, in camera order.

        Raises the first exception (e.g. a rejected parameter value) so the
        caller can surface it; otherwise returns each camera's result.
        """
        results = []
        for _camera, result, exc in self._run_parallel(fn):
            if exc is not None:
                raise exc
            results.append(result)
        return results

    def save_all_params(self) -> dict[str, str]:
        """Map serial_number -> current parameter text, snapshotting in parallel."""
        out: dict[str, str] = {}
        for camera, text, exc in self._run_parallel(lambda c: c.save_params()):
            if exc is None and text:
                out[camera.serial_number] = text
        return out

    def _run_parallel(self, fn):
        """Call fn(camera) on every camera concurrently, preserving order.

        Returns a list of (camera, result, exception) tuples in self.cameras
        order; exception is None on success, otherwise the raised exception
        (result is then None). The SDK releases the GIL during its blocking
        calls, so the per-camera open / parameter-load / start work overlaps
        instead of running one camera at a time.
        """
        if not self.cameras:
            return []
        with ThreadPoolExecutor(
            max_workers=len(self.cameras), thread_name_prefix="cam"
        ) as executor:
            futures = [executor.submit(fn, camera) for camera in self.cameras]
        results = []
        for camera, future in zip(self.cameras, futures, strict=True):
            try:
                results.append((camera, future.result(), None))
            except Exception as exc:  # re-raised / handled by the caller
                results.append((camera, None, exc))
        return results

    def load_config(self, directory: str | Path) -> None:
        directory = Path(directory)

        def load_one(camera: Camera) -> None:
            config_path = directory / f"{camera.serial_number}.{self.extension}"
            if config_path.exists():
                log.info("Loading parameters for camera: %s", camera.serial_number)
                camera.load_params(config_path.read_text())
            else:
                camera.load_params("")
                log.warning("Parameters file not found at %s", config_path)

        # Loading a config writes many registers over USB per camera; run the
        # cameras in parallel so the whole load takes one camera's time, not N.
        for _camera, _result, exc in self._run_parallel(load_one):
            if exc is not None:
                raise exc

    def apply_display_config(self, cameras: "list[CameraConfig]") -> None:
        """Set each camera's display transform from its persisted config entry.

        The transform (rotation/flips) is baked into the video when recording
        in "display" form; a camera absent from the config keeps the identity.
        """
        by_serial = {c.serial_number: c for c in cameras}
        for camera in self.cameras:
            cfg = by_serial.get(camera.serial_number)
            camera.display_transform = (
                from_camera_config(cfg) if cfg is not None else DisplayTransform()
            )

    def start_preview(self) -> None:
        self.stop()
        for _camera, _result, exc in self._run_parallel(
            lambda camera: camera.start_preview()
        ):
            if exc is not None:
                raise exc

    def start_record(
        self,
        save_dir: str | Path,
        fps: float,
        video_format: VideoFormat,
        record_form: str = "display",
        save_frame_timestamps: bool = False,
    ) -> list[str]:
        """Start recording on all cameras; return the names that started.

        A single camera failing (writer open, trigger-ready timeout, or a
        start "insufficient resources" error) no longer abandons the others
        half-started: it is logged and skipped.
        """
        self.stop()

        def record_one(camera: Camera) -> bool:
            save_path = Path(save_dir) / f"{camera.name}.{video_format.extension}"
            return camera.start_record(
                str(save_path),
                fps,
                video_format,
                record_form,
                save_frame_timestamps,
            )

        # Start every camera at once so they begin grabbing closer together
        # (and the operator waits one start, not eight back to back).
        started: list[str] = []
        for camera, ok, exc in self._run_parallel(record_one):
            if isinstance(exc, BackendError):
                log.error("Camera %s failed to start recording", camera.name)
            elif exc is not None:
                raise exc
            elif ok:
                started.append(camera.name)
        return started

    def set_software_trigger_frequency(self, hz: float) -> None:
        self._trigger_timer.set_frequency(hz)

    def start_software_trigger(self, duration: float | None = None) -> None:
        self._trigger_timer.start(duration)

    def stop_software_trigger(self) -> None:
        self._trigger_timer.stop()

    def enable_frame_trigger(self) -> None:
        for camera in self.cameras:
            camera.enable_frame_trigger()

    def set_trigger_source(self, use_software_trigger: bool) -> None:
        for camera in self.cameras:
            camera.set_trigger_source(use_software_trigger)

    @property
    def all_cameras_started(self) -> bool:
        return all(camera.started for camera in self.cameras)

    def get_frames_and_fps(self) -> list[tuple[np.ndarray | None, float]]:
        return [
            (camera.frame_for_display.pop(), camera.resulting_fps)
            for camera in self.cameras
        ]

    def stop(self) -> None:
        for camera in self.cameras:
            camera.stop()
        for camera in self.cameras:
            camera.join()

    def close(self) -> None:
        self.stop_software_trigger()
        for camera in self.cameras:
            camera.close()
        teardown_backend(self.backend)

    def _trigger_all(self) -> None:
        for camera in self.cameras:
            try:
                camera.trigger_once()
            except BackendError:
                pass
