"""Multi-camera orchestration, independent of any camera SDK.

`CameraSystem` enumerates and opens the cameras, drives them in parallel (each
SDK releases the GIL on its blocking calls, so N cameras take about one camera's
time) and owns the shared software-trigger timer.
"""

import logging
import time
from collections import Counter
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import TYPE_CHECKING, Any

from octacam.cameras.base import WRITER_QUEUE_SIZE, BackendError, Camera
from octacam.cameras.registry import (
    BackendSpec,
    BackendUnavailable,
    resolve_backend_names,
    select_backend,
)
from octacam.pulses import PulseClock
from octacam.transform import DisplayTransform, from_camera_config
from octacam.trigger import PreciseTimer
from octacam.writer import VideoFormat

if TYPE_CHECKING:
    from octacam.config import CameraConfig, OctacamConfig

log = logging.getLogger("octacam")


class CameraSystem:
    def __init__(
        self,
        requested_serial_numbers: list[str] | None = None,
        backend: str = "auto",
        *,
        _defer_open: bool = False,
    ):
        self.cameras: list[Camera] = []
        self._trigger_timer = PreciseTimer(self._trigger_all)

        # What the config asked for, and which never came up (serial -> reason):
        # a 7-of-8 rig runs, but must never look like a healthy one.
        self.requested_serial_numbers: list[str] = list(requested_serial_numbers or [])
        self.missing: dict[str, str] = {}

        # "auto" sweeps every installed backend, so one rig can mix vendors.
        self.backend = backend
        self._teardowns: list[Callable[[], None]] = []  # of the tiers enumerated

        if _defer_open:  # see pending()
            return

        entries = self._enumerate(backend, requested_serial_numbers)
        if not entries:
            self._warn_if_incomplete()
            return
        for _serial, handle, make_backend in entries:
            self.cameras.append(Camera(make_backend(handle)))

        # A camera that fails to open (in use by another process, or a USB3 link
        # that fell back to USB 2.0) is dropped loudly and the rest come up; only
        # a total failure raises.
        failures = [
            (camera, exc)
            for camera, _result, exc in self._run_parallel(lambda c: c.open())
            if exc is not None
        ]
        if failures:
            failed = {id(camera) for camera, _exc in failures}
            for camera, exc in failures:
                camera.close()  # close() no-ops on a camera that never opened
                log.error("Failed to open camera %s: %s", camera.serial_number, exc)
                self.missing[camera.serial_number] = f"failed to open: {exc}"
            self.cameras = [c for c in self.cameras if id(c) not in failed]
        if not self.cameras:
            self._teardown_backends()
            raise failures[0][1]
        self._warn_if_incomplete()

    def _warn_if_incomplete(self) -> None:
        """Warn once, loudly, when fewer cameras opened than were asked for: the
        individual failures scroll past at startup.
        """
        if not self.requested_serial_numbers or not self.missing:
            return
        detail = ", ".join(
            f"{serial} ({reason})" for serial, reason in sorted(self.missing.items())
        )
        log.warning(
            "INCOMPLETE RIG: %d of %d configured cameras opened \N{EM DASH} missing "
            "%s. "
            "Recordings will be short these cameras.",
            len(self.cameras),
            len(self.requested_serial_numbers),
            detail,
        )

    @property
    def incomplete(self) -> bool:
        """True when the config asked for cameras that are not in this system."""
        return bool(self.requested_serial_numbers and self.missing)

    @classmethod
    def for_config(
        cls,
        config: OctacamConfig,
        config_dir: str | Path,
        backend: str | None = None,
    ) -> CameraSystem:
        """Open the rig *config* describes (through *backend*, else the config's):
        each camera named, its parameter file loaded and its display settings
        applied. Raises BackendError when no camera opens.

        Any failure closes the cameras before raising: a camera left open to
        interpreter teardown can crash it (see BaslerBackend.close).
        """
        system = cls(
            [c.serial_number for c in config.cameras], backend=backend or config.backend
        )
        try:
            if not system.cameras:
                raise BackendError("no cameras were opened")
            names = {c.serial_number: c.name for c in config.cameras if c.name}
            for camera in system.cameras:
                camera.name = names.get(camera.serial_number, camera.name)
            system.load_config(config_dir)
            system.apply_display_config(config.cameras)
        except BaseException:
            system.close()
            raise
        return system

    @classmethod
    def pending(cls, backend: str = "auto") -> CameraSystem:
        """A hardware-free placeholder with no cameras, which the GUI serves until
        its init thread swaps in the real system (`attach_system`).
        """
        return cls(backend=backend, _defer_open=True)

    def _enumerate(
        self, backend: str, requested_serial_numbers: list[str] | None
    ) -> list[tuple[str, object, Callable]]:
        """Resolve the selector to `[(serial, handle, backend_factory), ...]`.

        Each tier, in cascade order, is offered only the requested serials:
        enumeration is a device access (Basler's CreateDevice downloads the
        camera's XML). The first tier to report a serial claims it; a None handle
        (present but unusable) claims it without being opened. Results come in
        requested order.
        """
        active: list[tuple[str, BackendSpec]] = []  # in cascade priority order
        unavailable: list[BackendUnavailable] = []
        for name in resolve_backend_names(backend):
            try:
                active.append((name, select_backend(name)))
            except BackendUnavailable as e:
                unavailable.append(e)
        if not active:
            if unavailable:
                raise unavailable[0]
            raise BackendUnavailable(backend, "no camera backend is available")

        self._teardowns = [spec.teardown for _name, spec in active if spec.teardown]

        claimed_by: dict[str, str] = {}  # serial -> the tier that claimed it
        found: dict[str, tuple[str, object, Callable]] = {}
        for name, spec in active:
            for serial, handle in spec.enumerate(requested_serial_numbers):
                if serial in claimed_by:
                    continue  # a higher-priority tier already owns this camera
                claimed_by[serial] = name
                if handle is not None:
                    found[serial] = (serial, handle, spec.factory)
        if not requested_serial_numbers:
            entries = list(found.values())
        else:
            entries = []
            for serial in dict.fromkeys(requested_serial_numbers):
                if serial in found:
                    entries.append(found[serial])
                elif serial in claimed_by:
                    self.missing[serial] = "detected but unusable"
                else:
                    log.warning("Camera with serial number %s not found", serial)
                    self.missing[serial] = "not found"
        if entries and len(active) == 1:
            log.info("Detected %d camera(s) via %s", len(entries), active[0][0])
        elif entries:
            # One line naming each camera's tier (the tiers log only at debug).
            counts = Counter(claimed_by[serial] for serial, _h, _mk in entries)
            breakdown = ", ".join(f"{n} via {name}" for name, n in counts.items())
            log.info("Detected %d camera(s): %s", len(entries), breakdown)
        return entries

    def _teardown_backends(self) -> None:
        """Release session resources for every backend we enumerated."""
        for teardown in self._teardowns:
            teardown()

    @property
    def extensions(self) -> tuple[str, ...]:
        """The distinct parameter-file suffixes of the opened cameras."""
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
        """`fn(camera)` on every camera concurrently: the results in camera
        order, or the first exception raised.
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
        """`fn(camera)` on every camera concurrently, as `[(camera, result,
        exception)]` in camera order (exception None on success).
        """
        if not self.cameras:
            return []
        with ThreadPoolExecutor(
            max_workers=len(self.cameras), thread_name_prefix="cam"
        ) as executor:
            futures = [executor.submit(fn, camera) for camera in self.cameras]
        results: list[tuple[Camera, Any, Exception | None]] = []
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

        for _camera, _result, exc in self._run_parallel(load_one):
            if exc is not None:
                raise exc

    def apply_display_config(self, cameras: list[CameraConfig]) -> None:
        """Set each camera's display transform and ROI centering from config (an
        absent camera gets neither); an enabled axis re-centers now.
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
                        axis,
                        camera.serial_number,
                        e,
                    )

    def start_preview(self, mode: str = "software", fps: float | None = None) -> None:
        """Start preview on every camera (modes: `Camera.start_preview`)."""
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
        video_format: VideoFormat | list[VideoFormat],
        clock: PulseClock,
        *,
        record_form: str = "display",
        writer_queue_size: int = WRITER_QUEUE_SIZE,
        hold: bool = False,
    ) -> list[str]:
        """Start a take on every camera (each camera's `take`, even one that
        fails to start); return the names that started.

        Arguments as `Camera.start_record`. `video_format` is one format,
        or one per camera in `self.cameras` order (a GPU recording's overflow
        goes to the CPU, see `octacam.writer.resolve_capture_formats`).
        """
        self.stop()

        if isinstance(video_format, list):
            if len(video_format) != len(self.cameras):
                raise ValueError(
                    f"start_record got {len(video_format)} formats for "
                    f"{len(self.cameras)} cameras"
                )
            format_for = {
                id(c): fmt for c, fmt in zip(self.cameras, video_format, strict=True)
            }
        else:
            format_for = {id(c): video_format for c in self.cameras}

        def record_one(camera: Camera) -> bool:
            fmt = format_for[id(camera)]
            save_path = Path(save_dir) / f"{camera.name}.{fmt.extension}"
            return camera.start_record(
                str(save_path),
                fps,
                fmt,
                clock,
                record_form=record_form,
                queue_size=writer_queue_size,
                hold=hold,
            )

        started: list[str] = []
        for camera, ok, exc in self._run_parallel(record_one):
            # Log and skip every failure: raising would abandon the others' grab
            # threads and ffmpeg children half-started.
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

    def prime_software_trigger(
        self, pulses: int, fps: float, timeout_s: float = 1.0
    ) -> None:
        """Fire `pulses` sacrificial software triggers at every camera (a GS3
        ignores its first triggers after acquisition start); their frames are
        discarded under the priming hold.
        """
        interval = 1.0 / fps if fps > 0 else 0.01
        for _ in range(pulses):
            self._trigger_all()
            # One at a time: a camera ignoring its first triggers waits out each
            # one's answer deadline, so firing at the fps would overflow its
            # PENDING_MAX and drop the rest.
            deadline = time.monotonic() + timeout_s
            while time.monotonic() < deadline:
                if all(c.backend.trigger.pending == 0 for c in self.cameras):
                    break
                time.sleep(0.002)
            time.sleep(interval)

    def stop(self, fill_to: int | None = None) -> None:
        """Stop and join every grab loop. `fill_to` (a completed train's pulse
        count) pads each recording camera's video to that many frames.
        """
        for camera in self.cameras:
            camera.stop(fill_to)
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
