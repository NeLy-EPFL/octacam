"""Camera acquisition on pypylon. Port of cpp/src/camera.{hpp,cpp}."""

import logging
import threading
import time
from pathlib import Path

import numpy as np
from pypylon import genicam, pylon

from octacam.trigger import PreciseTimer
from octacam.writer import AsyncVideoWriter

log = logging.getLogger("octacam")

GRAB_TIMEOUT_MS = 100
TRIGGER_READY_TIMEOUT_MS = 1000
WRITER_QUEUE_SIZE = 20


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


def _drop_empty_pfs_values(content: str) -> str:
    """Remove .pfs entries with empty values (e.g. "ImageFilename\\t").

    Older pylon versions wrote such no-op entries; current GenICam
    persistence parsers reject the whole stream over them.
    """
    lines = []
    for line in content.splitlines():
        if not line.startswith("#") and "\t" in line:
            key, _, value = line.partition("\t")
            if not value.strip():
                log.debug("Dropping empty .pfs entry: %s", key)
                continue
        lines.append(line)
    return "\n".join(lines) + "\n"


class Camera:
    def __init__(self, device):
        self._camera = pylon.InstantCamera(device)
        self._camera.Open()
        self.serial_number: str = str(
            self._camera.GetDeviceInfo().GetSerialNumber()
        )
        self.name: str = self.serial_number
        self.frame_for_display = LatestFrame()
        self._video_writer = AsyncVideoWriter(WRITER_QUEUE_SIZE)
        self._stop_flag = threading.Event()
        self._thread: threading.Thread | None = None
        self._timestamps: list[int] = []
        self._dropped: list[bool] = []
        self._resulting_fps = 0.0
        self._started = False
        self._original_trigger_source: str | None = None

    @property
    def started(self) -> bool:
        return self._started

    @property
    def resulting_fps(self) -> float:
        return self._resulting_fps

    def load_params(self, config_str: str) -> None:
        if config_str:
            pylon.FeaturePersistence.LoadFromString(
                _drop_empty_pfs_values(config_str),
                self._camera.GetNodeMap(),
                True,
            )
        height = self._camera.Height.Value
        width = self._camera.Width.Value
        self.frame_for_display.push(np.zeros((height, width), dtype=np.uint8))
        try:
            self._original_trigger_source = self._camera.TriggerSource.Value
        except genicam.GenericException:
            self._original_trigger_source = None

    def enable_frame_trigger(self) -> None:
        """Set TriggerMode On for FrameStart, as start_preview does in C++.

        Needed before headless recording: the .pfs files ship with
        TriggerMode Off, and without it ExecuteSoftwareTrigger is ignored.
        """
        if not self._camera.IsOpen():
            return
        self._camera.TriggerSelector.Value = "FrameStart"
        self._camera.TriggerMode.Value = "On"

    def set_trigger_source(self, use_software_trigger: bool) -> None:
        if not self._camera.IsOpen():
            return
        try:
            if use_software_trigger:
                self._camera.TriggerSource.Value = "Software"
            elif self._original_trigger_source is not None:
                self._camera.TriggerSource.Value = self._original_trigger_source
        except genicam.GenericException as e:
            log.warning(
                "Failed to set trigger source on camera %s: %s",
                self.serial_number,
                e,
            )

    def trigger_once(self) -> None:
        if self._camera.IsGrabbing():
            self._camera.ExecuteSoftwareTrigger()

    def start_preview(self) -> None:
        self._stop_flag.clear()
        if not self._camera.IsOpen():
            return
        self._camera.TriggerSelector.Value = "FrameStart"
        self._camera.TriggerMode.Value = "On"
        self._camera.TriggerSource.Value = "Software"
        self._timestamps.clear()
        self._camera.StartGrabbing(pylon.GrabStrategy_LatestImageOnly)
        self._thread = threading.Thread(target=self._preview_loop, daemon=True)
        self._thread.start()

    def start_record(self, save_path: str, fps: float, fourcc: str) -> None:
        self._stop_flag.clear()
        self._started = False
        if not self._camera.IsOpen():
            return
        self._timestamps.clear()
        self._dropped.clear()

        frame_size = (self._camera.Width.Value, self._camera.Height.Value)
        if not self._video_writer.open(
            save_path, fourcc, fps, frame_size, is_color=False
        ):
            log.error("Failed to open video writer for: %s", save_path)
            return

        self._camera.StartGrabbing(pylon.GrabStrategy_OneByOne)
        ready = self._camera.WaitForFrameTriggerReady(
            TRIGGER_READY_TIMEOUT_MS, pylon.TimeoutHandling_Return
        )
        if not ready:
            log.error(
                "Failed to start grabbing for recording on camera %s",
                self.serial_number,
            )
            self._video_writer.close()
            return

        self._thread = threading.Thread(
            target=self._record_loop, args=(save_path,), daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop_flag.set()

    def join(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            self._thread.join()
        self._thread = None

    def close(self) -> None:
        self.stop()
        self.join()
        if self._camera.IsOpen():
            self._camera.Close()

    def _store_timestamp(self, grab_result) -> None:
        timestamp = grab_result.TimeStamp
        if timestamp == 0:
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
        self._resulting_fps = (
            (last - start) * 1e9 / delta_ns if delta_ns else 0.0
        )

    def _preview_loop(self) -> None:
        camera = self._camera
        while not self._stop_flag.is_set() and camera.IsGrabbing():
            result = camera.RetrieveResult(
                GRAB_TIMEOUT_MS, pylon.TimeoutHandling_Return
            )
            # IsValid is the pypylon equivalent of C++'s `if (grab_result)`:
            # a timed-out RetrieveResult returns an empty result whose other
            # accessors throw.
            if result.IsValid() and result.GrabSucceeded():
                self._store_timestamp(result)
                # Materialize the (copying) Array only when the display slot
                # is free, keeping the per-frame cost off the steady state.
                if self.frame_for_display.wants_frame:
                    if self.frame_for_display.push(result.Array):
                        self._update_resulting_fps()
                result.Release()
        camera.StopGrabbing()

    def _record_loop(self, save_path: str) -> None:
        camera = self._camera
        frame_count = 0
        while not self._stop_flag.is_set() and camera.IsGrabbing():
            result = camera.RetrieveResult(
                GRAB_TIMEOUT_MS, pylon.TimeoutHandling_Return
            )
            if result.IsValid() and result.GrabSucceeded():
                self._store_timestamp(result)
                frame = result.Array  # owned copy; writer takes ownership
                result.Release()

                written = self._video_writer.write(frame)
                if not written:
                    log.warning(
                        "Frame %d dropped for camera %s",
                        frame_count,
                        self.serial_number,
                    )
                self._dropped.append(not written)

                if self.frame_for_display.push(frame):
                    self._update_resulting_fps()

                self._started = True
                frame_count += 1
            else:
                result.Release()
        camera.StopGrabbing()
        self._video_writer.close()

        dropped_count = sum(self._dropped)
        log.info(
            "Camera %s: %d frames recorded, %d frames dropped",
            self.serial_number,
            frame_count,
            dropped_count,
        )
        self._write_timestamps_csv(save_path)

    def _write_timestamps_csv(self, save_path: str) -> None:
        csv_path = Path(save_path).with_suffix(".csv")
        with open(csv_path, "w") as csv_file:
            csv_file.write("frame_index,timestamp,dropped\n")
            for i, (timestamp, dropped) in enumerate(
                zip(self._timestamps, self._dropped)
            ):
                csv_file.write(f"{i},{timestamp},{int(dropped)}\n")


class CameraSystem:
    def __init__(self, requested_serial_numbers: list[str] | None = None):
        self.cameras: list[Camera] = []
        self._trigger_timer = PreciseTimer(self._trigger_all)

        tl_factory = pylon.TlFactory.GetInstance()
        devices = tl_factory.EnumerateDevices()
        if not devices:
            return
        log.info("Detected %d camera(s)", len(devices))

        detected_serial_numbers = [
            str(device.GetSerialNumber()) for device in devices
        ]
        if not requested_serial_numbers:
            final_serial_numbers = sorted(detected_serial_numbers)
        else:
            final_serial_numbers = list(requested_serial_numbers)

        for serial_number in final_serial_numbers:
            try:
                index = detected_serial_numbers.index(serial_number)
            except ValueError:
                log.warning(
                    "Camera with serial number %s not found", serial_number
                )
                continue
            self.cameras.append(Camera(tl_factory.CreateDevice(devices[index])))

    def __len__(self) -> int:
        return len(self.cameras)

    def __iter__(self):
        return iter(self.cameras)

    def load_config(self, directory: str | Path) -> None:
        directory = Path(directory)
        for camera in self.cameras:
            config_path = directory / f"{camera.serial_number}.pfs"
            if config_path.exists():
                log.info(
                    "Loading parameters for camera: %s", camera.serial_number
                )
                camera.load_params(config_path.read_text())
            else:
                camera.load_params("")
                log.warning("Parameters file not found at %s", config_path)

    def start_preview(self) -> None:
        self.stop()
        for camera in self.cameras:
            camera.start_preview()

    def start_record(
        self, save_dir: str | Path, fps: float, fourcc: str, extension: str
    ) -> None:
        self.stop()
        for camera in self.cameras:
            save_path = Path(save_dir) / f"{camera.name}.{extension}"
            camera.start_record(str(save_path), fps, fourcc)

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

    def _trigger_all(self) -> None:
        for camera in self.cameras:
            try:
                camera.trigger_once()
            except genicam.GenericException:
                pass
