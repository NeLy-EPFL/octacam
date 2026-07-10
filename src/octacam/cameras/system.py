"""Multi-camera orchestration, independent of any camera SDK.

``CameraSystem`` enumerates and opens the selected backend's cameras, drives
them in parallel (each SDK releases the GIL on its blocking calls, so opening /
loading / starting N cameras takes about one camera's time), and owns the
shared software-trigger timer. The backend is chosen once at construction; the
rest of the system only ever sees :class:`~octacam.cameras.base.Camera`.
"""

import logging
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import TYPE_CHECKING

import numpy as np

from octacam.cameras.base import WRITER_QUEUE_SIZE, BackendError, Camera
from octacam.cameras.registry import (
    BackendUnavailable,
    resolve_backend_names,
    select_backend,
    teardown_backend,
)
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
        backend: str = "auto",
    ):
        self.cameras: list[Camera] = []
        self._trigger_timer = PreciseTimer(self._trigger_all)

        # ``backend`` is a selector: "auto" (the default) sweeps every installed
        # hardware backend so one rig can mix vendors; a concrete name restricts
        # to it. Track the backends we actually enumerate so close() releases
        # each one's session resources (only FLIR needs it).
        self.backend = backend
        self._backends_used: set[str] = set()

        entries = self._enumerate(backend, requested_serial_numbers)
        if not entries:
            return
        for _serial, handle, make_backend in entries:
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
            self._teardown_backends()
            camera, exc = failures[0]
            log.error("Failed to open camera %s", camera.serial_number)
            raise exc

    def _enumerate(
        self, backend: str, requested_serial_numbers: list[str] | None
    ) -> list[tuple[str, object, "Callable"]]:
        """Resolve the selector to ``[(serial, handle, backend_factory), ...]``.

        With a single active backend (a concrete selector, or "auto" resolving to
        one available tier) the requested serials are passed straight to that
        backend's enumeration, preserving its ordering and its "not found"
        warnings. With several active backends (the cascade) each is enumerated
        in full, in priority order, and a camera is claimed by the *first*
        backend that reports its serial — a lower tier that also sees an
        already-claimed serial is skipped, so a camera served by a vendor SDK is
        never double-opened by the pycameleon floor.
        """
        active = []  # (name, enumerate_fn, factory), in cascade priority order
        unavailable: list[BackendUnavailable] = []
        for name in resolve_backend_names(backend):
            try:
                enumerate_fn, make_backend, _extension = select_backend(name)
            except BackendUnavailable as e:
                unavailable.append(e)
                continue
            active.append((name, enumerate_fn, make_backend))
        if not active:
            # An explicit backend whose SDK is missing (or an unknown name) must
            # surface as it did before; "auto" with nothing installed is its own
            # clear error rather than a silent empty system.
            if unavailable:
                raise unavailable[0]
            raise BackendUnavailable(backend, "no camera backend is available")

        # Release order matters for FLIR/spinnaker; record every backend we
        # enumerate (as a set — teardown order is not significant among them).
        self._backends_used = {name for name, _fn, _mk in active}

        if len(active) == 1:
            name, enumerate_fn, make_backend = active[0]
            entries = [
                (serial, handle, make_backend)
                for serial, handle in enumerate_fn(requested_serial_numbers)
            ]
            if entries:
                log.info("Detected %d camera(s) via %s", len(entries), name)
            return entries

        # The cascade: enumerate every active tier in priority order and let the
        # highest one claim each serial. Lower tiers still enumerate (so a camera
        # a vendor tier missed can fall through) but skip serials already claimed.
        claimed: set[str] = set()
        claimed_by: dict[str, str] = {}  # serial -> winning backend name (for logs)
        collected: list[tuple[str, object, Callable]] = []
        for name, enumerate_fn, make_backend in active:
            for serial, handle in enumerate_fn(None):
                if serial in claimed:
                    continue  # a higher-priority tier already owns this camera
                claimed.add(serial)
                claimed_by[serial] = name
                collected.append((serial, handle, make_backend))
        if not requested_serial_numbers:
            self._log_detected(collected, claimed_by)
            return collected
        by_serial = {entry[0]: entry for entry in collected}
        ordered: list[tuple[str, object, Callable]] = []
        for serial in requested_serial_numbers:
            entry = by_serial.get(serial)
            if entry is None:
                log.warning("Camera with serial number %s not found", serial)
                continue
            ordered.append(entry)
        self._log_detected(ordered, claimed_by)
        return ordered

    @staticmethod
    def _log_detected(
        entries: list[tuple[str, object, "Callable"]], claimed_by: dict[str, str]
    ) -> None:
        """Log one attributed "Detected N camera(s)" line for the cascade.

        Rolls the per-tier enumeration (each tier logs only at debug) into a
        single summary that also says which backend won each camera, e.g.
        ``Detected 3 camera(s): 2 via spinnaker, 1 via basler`` — instead of
        the several overlapping per-tier counts that confused operators.
        """
        if not entries:
            return
        counts: dict[str, int] = {}
        for serial, _handle, _make in entries:
            name = claimed_by.get(serial, "?")
            counts[name] = counts.get(name, 0) + 1
        breakdown = ", ".join(f"{n} via {name}" for name, n in counts.items())
        log.info("Detected %d camera(s): %s", len(entries), breakdown)

    def _teardown_backends(self) -> None:
        """Release session resources for every backend we enumerated."""
        for name in self._backends_used:
            teardown_backend(name)

    @property
    def extensions(self) -> tuple[str, ...]:
        """The distinct parameter-file suffixes across the opened cameras.

        A single-vendor rig has one (``("pfs",)``); a mixed rig has several.
        Used to glob every camera's parameter files out of a config dir.
        """
        return tuple(sorted({camera.extension for camera in self.cameras}))

    def extension_by_serial(self) -> dict[str, str]:
        """Map each opened camera's serial to its parameter-file suffix."""
        return {camera.serial_number: camera.extension for camera in self.cameras}

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
            config_path = directory / f"{camera.serial_number}.{camera.extension}"
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
        """Set each camera's display transform and ROI centering from config.

        The transform (rotation/flips) is baked into the video when recording
        in "display" form; a camera absent from the config keeps the identity.
        The center_x/center_y flags auto-derive the ROI offsets, applied via
        set_center so an enabled axis is re-centered immediately.
        """
        by_serial = {c.serial_number: c for c in cameras}
        for camera in self.cameras:
            cfg = by_serial.get(camera.serial_number)
            camera.display_transform = (
                from_camera_config(cfg) if cfg is not None else DisplayTransform()
            )
            for axis, enabled in (
                ("x", bool(cfg.center_x) if cfg else False),
                ("y", bool(cfg.center_y) if cfg else False),
            ):
                try:
                    camera.set_center(axis, enabled)
                except (BackendError, ValueError) as e:
                    log.debug(
                        "Could not apply center_%s on %s: %s",
                        axis, camera.serial_number, e,
                    )

    def start_preview(self, mode: str = "software", fps: float | None = None) -> None:
        """Start preview on every camera in the given trigger mode (see
        :meth:`Camera.start_preview`): ``"software"``, ``"free_running"`` (rate
        capped at ``fps``), or ``"managed"`` (octacam-driven hardware trigger)."""
        self.stop()
        for _camera, _result, exc in self._run_parallel(
            lambda camera: camera.start_preview(mode, fps)
        ):
            if exc is not None:
                raise exc

    def start_record(
        self,
        save_dir: str | Path,
        fps: float,
        video_format: VideoFormat,
        record_form: str = "display",
        use_software_trigger: bool = True,
        writer_queue_size: int = WRITER_QUEUE_SIZE,
        max_frames: int | None = None,
    ) -> list[str]:
        """Start recording on all cameras; return the names that started.

        A single camera failing (writer open, trigger-ready timeout, or a
        start "insufficient resources" error) no longer abandons the others
        half-started: it is logged and skipped. ``use_software_trigger`` is
        forwarded so an external-trigger recording fetches frames without the
        software-trigger hand-off (see :meth:`Camera.start_record`).
        ``writer_queue_size`` bounds each camera's frame buffer to the encoder.
        ``max_frames`` caps every camera at the same frame count so a teardown
        race can't leave cameras one frame apart (None = uncapped).
        """
        self.stop()

        def record_one(camera: Camera) -> bool:
            save_path = Path(save_dir) / f"{camera.name}.{video_format.extension}"
            return camera.start_record(
                str(save_path),
                fps,
                video_format,
                record_form,
                software_trigger=use_software_trigger,
                queue_size=writer_queue_size,
                max_frames=max_frames,
            )

        # Start every camera at once so they begin grabbing closer together
        # (and the operator waits one start, not eight back to back).
        started: list[str] = []
        for camera, ok, exc in self._run_parallel(record_one):
            # Log-and-skip EVERY per-camera failure (not just BackendError): the
            # other cameras have already launched their grab thread + ffmpeg
            # child, so re-raising here would abandon them half-started. An
            # unexpected (non-BackendError) failure still gets a full traceback.
            if exc is not None:
                log.error(
                    "Camera %s failed to start recording",
                    camera.name,
                    exc_info=not isinstance(exc, BackendError),
                )
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
        self._teardown_backends()

    def _trigger_all(self) -> None:
        for camera in self.cameras:
            try:
                camera.trigger_once()
            except BackendError:
                pass
