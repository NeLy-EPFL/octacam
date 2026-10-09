"""Frame-rate benchmark: is the target fps achievable, what is the ceiling, and
which stage limits it.

It drives the real `CameraBackend.retrieve` and the real writers, but in loops
of its own: it deliberately re-implements the grab loop rather than reuse the
record loop (`CameraTake`), so the record path carries no instrumentation and
each ceiling is measured in isolation. Its only seams into the record path, the
read-only `Camera.backend` and `AsyncFrameWriter(profile=...)`, cost a
recording nothing.

- **Grab ceiling** per camera, all grabbing at once: `trigger_once()` +
  `retrieve()` back to back, no writer. Trigger, exposure, transfer and copy
  are one `acquire` stage (the SDK cannot time them apart). The **free-run**
  ceiling overlaps exposure with readout as a hardware trigger does, so it is a
  hardware-triggered rig's ceiling, measured with no wiring.
- **Encode ceiling** per camera: every real writer at once, fed synthetic frames
  as fast as it takes them.
- **End-to-end** at the target fps: one timer triggering every camera, grab
  loops writing into real writers; achieved fps, drops, queue depth and
  per-stage timing.

The system ceiling is the slowest camera's (one timer triggers them all).
`_classify` names the bottleneck, and the max search bisects short trials
toward a *stable* rate (`TrialOutcome.stable_passed`), confirmed over a
longer window.
"""

from __future__ import annotations

import contextlib
import logging
import math
import os
import statistics
import tempfile
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

import numpy as np

from octacam.cameras.base import WRITER_QUEUE_SIZE, Camera, CameraBackend
from octacam.cameras.take import GRAB_TIMEOUT_MS
from octacam.transform import DisplayTransform, apply_display_transform
from octacam.trigger import PreciseTimer
from octacam.writer import (
    AsyncFrameWriter,
    VideoFormat,
    WriteResult,
    resolve_capture_formats,
)

if TYPE_CHECKING:
    from octacam.cameras.system import CameraSystem
    from octacam.config import RecordingSettings

log = logging.getLogger("octacam")

WARMUP_S = 0.5  # discarded settle time before every measurement window
# A trial passes at this fraction of its target fps and at most this drop rate
# (drops under 1% are encoder noise).
ACHIEVE_FRACTION = 0.97
DROP_THRESHOLD = 0.01

# The max search's stricter bars (TrialOutcome.stable_passed): a bisection
# converges on the marginal pass/fail edge, so it wants a tenth of the drop
# budget and a writer queue under 75% of its bound. A filling queue is the
# earliest sign the encoder is falling behind, before any frame drops.
STABLE_DROP_THRESHOLD = 0.001
QUEUE_SATURATION_FRACTION = 0.75
# A candidate that fails the longer confirmation is reported this much lower.
STABILITY_MARGIN = 0.05
FIND_MAX_ITERATIONS = 4

# Bottleneck kinds, and the label the CLI and the GUI show for each.
ACQUISITION = "acquisition"
TRANSFER = "transfer"
ENCODE = "encode"
HOST = "host"
NONE = "none"
BOTTLENECK_LABELS = {
    ACQUISITION: "acquisition (the camera can't deliver frames fast enough)",
    TRANSFER: "transfer (the cameras share more bus bandwidth than the link provides)",
    ENCODE: "encoding (the encoder can't keep up)",
    HOST: "host contention (CPU / GIL)",
    NONE: "none",
}

# Concurrent acquisition below this fraction of the solo rate means the cameras
# slow each other down. The acquisition sweep runs no encoder and the SDKs
# release the GIL, so that is the shared transport (one USB3 controller carries
# ~350-400 MB/s), not the CPU: TRANSFER, not HOST.
CONTENTION_RATIO = 0.85
USB3_BUS_MBPS = 384.0  # only for the transfer-bound advice
# Ambient load above which other processes skew the numbers.
SYSTEM_CPU_WARN_PERCENT = 60.0
LOAD_PER_CORE_WARN = 0.7

PHASE_ACQUIRE = "Measuring acquisition ceiling"
PHASE_ACQUIRE_SOLO = "Measuring per-camera solo ceiling"
PHASE_FREERUN = "Measuring free-run ceiling"
PHASE_ENCODE = "Measuring encode ceiling"
PHASE_TRIAL = "Running end-to-end trial"
PHASE_FREERUN_TRIAL = "Running free-run trial"
PHASE_MAX = "Searching for the stable max fps"
PHASE_DONE = "Done"


@dataclass
class Progress:
    """One progress update in budgeted seconds: this step starts *elapsed_s*
    into a run budgeted at *total_s* and should take *step_s*. A front end eases
    its bar toward `elapsed_s + step_s` in real time and never moves it back;
    the run ends on a `PHASE_DONE` update at `total_s`.
    """

    phase: str
    detail: str
    elapsed_s: float
    step_s: float
    total_s: float

    def to_dict(self) -> dict:
        return {
            "phase": self.phase,
            "detail": self.detail,
            "elapsed_s": round(self.elapsed_s, 3),
            "step_s": round(self.step_s, 3),
            "total_s": round(self.total_s, 3),
        }


ProgressCallback = Callable[["Progress"], None]


# --- Result types (to_dict: the CLI's --json and the GUI payload) ---


def _finite(value: float | None, ndigits: int = 1) -> float | None:
    """*value* rounded, or None for None, inf or nan: `Infinity` (a missing
    ceiling) is invalid JSON that breaks the browser's `JSON.parse`.
    """
    if value is None or not math.isfinite(value):
        return None
    return round(value, ndigits)


@dataclass
class StageTiming:
    """Per-frame cost of one pipeline stage over a measurement window."""

    name: str
    samples: int
    mean_ms: float
    p50_ms: float
    p95_ms: float
    p99_ms: float
    max_ms: float

    @property
    def implied_max_fps(self) -> float:
        """Rate this stage alone could sustain, from its mean per-frame cost."""
        return 1000.0 / self.mean_ms if self.mean_ms > 0 else float("inf")

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "samples": self.samples,
            "mean_ms": round(self.mean_ms, 4),
            "p50_ms": round(self.p50_ms, 4),
            "p95_ms": round(self.p95_ms, 4),
            "p99_ms": round(self.p99_ms, 4),
            "max_ms": round(self.max_ms, 4),
            "implied_max_fps": _finite(self.implied_max_fps),
        }


@dataclass
class CameraTrial:
    """One camera's end-to-end result at the target fps."""

    serial: str
    name: str
    width: int
    height: int
    target_fps: float
    achieved_fps: float
    grabbed: int
    dropped: int
    drop_rate: float
    max_queue_depth: int
    stages: dict[str, StageTiming]

    def to_dict(self) -> dict:
        return {
            "serial": self.serial,
            "name": self.name,
            "width": self.width,
            "height": self.height,
            "target_fps": round(self.target_fps, 2),
            "achieved_fps": round(self.achieved_fps, 2),
            "grabbed": self.grabbed,
            "dropped": self.dropped,
            "drop_rate": round(self.drop_rate, 5),
            "max_queue_depth": self.max_queue_depth,
            "stages": {name: s.to_dict() for name, s in self.stages.items()},
        }


def _slowest(rates: str) -> property:
    """A property: the slowest camera's rate in the *rates* field, or inf when
    none was measured.
    """
    return property(lambda self: min(getattr(self, rates).values(), default=math.inf))


@dataclass
class Ceilings:
    """Isolated per-camera ceilings (fps): `grab_fps` software-triggered
    (exposure and transfer in series), `freerun_fps` free-running (overlapped,
    as under a hardware trigger), and `grab_solo_fps` with each camera grabbing
    alone (>=2 cameras; see `bus_contended`).
    """

    grab_fps: dict[str, float]
    encode_fps: dict[str, float]
    freerun_fps: dict[str, float] = field(default_factory=dict)
    grab_solo_fps: dict[str, float] = field(default_factory=dict)

    grab_min = _slowest("grab_fps")
    encode_min = _slowest("encode_fps")
    freerun_min = _slowest("freerun_fps")
    grab_solo_min = _slowest("grab_solo_fps")

    @property
    def bus_contended(self) -> bool:
        """Whether the cameras throttle each other's acquisition: the slowest
        concurrent rate is under `CONTENTION_RATIO` of the slowest solo
        one. False without a solo pass.
        """
        solo = self.grab_solo_min
        return (
            bool(self.grab_solo_fps)
            and math.isfinite(solo)
            and (self.grab_min < solo * CONTENTION_RATIO)
        )

    def to_dict(self) -> dict:
        return {
            "grab_fps": {s: _finite(v) for s, v in self.grab_fps.items()},
            "encode_fps": {s: _finite(v) for s, v in self.encode_fps.items()},
            "freerun_fps": {s: _finite(v) for s, v in self.freerun_fps.items()},
            "grab_solo_fps": {s: _finite(v) for s, v in self.grab_solo_fps.items()},
            "grab_min": _finite(self.grab_min),
            "encode_min": _finite(self.encode_min),
            "freerun_min": _finite(self.freerun_min),
            "grab_solo_min": _finite(self.grab_solo_min),
            "bus_contended": self.bus_contended,
        }


@dataclass
class DiagnosticReport:
    """The full diagnostic result, ready to render (CLI) or serialize (GUI)."""

    backend: str
    n_cameras: int
    target_fps: float
    trigger_source: str
    save_method: str
    ffmpeg_params: str
    writer_queue_size: int
    duration_s: float
    trials: list[CameraTrial]
    achieved_fps: float  # slowest camera at target (the synchronized rate)
    drop_rate: float  # worst camera's drop rate at target
    achievable: bool
    bottleneck: str
    ceilings: Ceilings | None = None
    predicted_max_fps: float = 0.0
    measured_max_fps: float | None = None  # the stable software-trigger max
    max_confirmed: bool = False  # it held the longer confirmation trial
    # External/free-run system max: the free-run trial's, else min(free-run, encode).
    hardware_max_fps: float | None = None
    throughput_mbps: dict[str, float] = field(default_factory=dict)
    throughput_mbps_total: float = 0.0
    freerun_trials: list[CameraTrial] = field(default_factory=list)
    # Other processes' load, sampled before the benchmark's own.
    system_cpu_percent: float | None = None
    load_per_core: float | None = None
    recommendations: list[str] = field(default_factory=list)
    jitter_p99_ms: float | None = None
    cpu_percent: float | None = None
    notes: list[str] = field(default_factory=list)

    @property
    def bottleneck_label(self) -> str:
        return BOTTLENECK_LABELS[self.bottleneck]

    def to_dict(self) -> dict:
        return {
            "backend": self.backend,
            "n_cameras": self.n_cameras,
            "target_fps": round(self.target_fps, 2),
            "trigger_source": self.trigger_source,
            "save_method": self.save_method,
            "ffmpeg_params": self.ffmpeg_params,
            "writer_queue_size": self.writer_queue_size,
            "duration_s": self.duration_s,
            "trials": [t.to_dict() for t in self.trials],
            "achieved_fps": round(self.achieved_fps, 2),
            "drop_rate": round(self.drop_rate, 5),
            "achievable": self.achievable,
            "bottleneck": self.bottleneck,
            "bottleneck_label": self.bottleneck_label,
            "ceilings": self.ceilings.to_dict() if self.ceilings else None,
            "predicted_max_fps": _finite(self.predicted_max_fps),
            "measured_max_fps": _finite(self.measured_max_fps),
            "max_confirmed": self.max_confirmed,
            "hardware_max_fps": _finite(self.hardware_max_fps),
            "throughput_mbps": {s: _finite(v) for s, v in self.throughput_mbps.items()},
            "throughput_mbps_total": _finite(self.throughput_mbps_total),
            "freerun_trials": [t.to_dict() for t in self.freerun_trials],
            "system_cpu_percent": _finite(self.system_cpu_percent),
            "load_per_core": _finite(self.load_per_core, 2),
            "recommendations": self.recommendations,
            "jitter_p99_ms": _finite(self.jitter_p99_ms, 3),
            "cpu_percent": _finite(self.cpu_percent),
            "notes": self.notes,
        }


# --- Helpers ---


def _pct(sorted_vals: list[float], q: float) -> float:
    """Linear-interpolated `q`-th percentile of an already-sorted list."""
    if not sorted_vals:
        return 0.0
    if len(sorted_vals) == 1:
        return sorted_vals[0]
    pos = (len(sorted_vals) - 1) * (q / 100.0)
    lo = int(pos)
    hi = min(lo + 1, len(sorted_vals) - 1)
    frac = pos - lo
    return sorted_vals[lo] * (1 - frac) + sorted_vals[hi] * frac


def _stage_timing(name: str, samples_ns: list[int]) -> StageTiming:
    """Summarize a list of per-frame ns durations into a `StageTiming`."""
    if not samples_ns:
        return StageTiming(name, 0, 0.0, 0.0, 0.0, 0.0, 0.0)
    ms = sorted(s / 1e6 for s in samples_ns)
    return StageTiming(
        name=name,
        samples=len(ms),
        mean_ms=statistics.fmean(ms),
        p50_ms=_pct(ms, 50),
        p95_ms=_pct(ms, 95),
        p99_ms=_pct(ms, 99),
        max_ms=ms[-1],
    )


def _wants_array() -> bool:
    """Diagnostics always materializes the frame array (recording always does)."""
    return True


def _cancelled(cancel: threading.Event | None) -> bool:
    return cancel is not None and cancel.is_set()


def _wait(seconds: float, cancel: threading.Event | None) -> None:
    """Sleep up to *seconds*, returning early once *cancel* is set (shutdown)."""
    end = time.perf_counter() + seconds
    while True:
        remaining = end - time.perf_counter()
        if remaining <= 0 or _cancelled(cancel):
            return
        time.sleep(min(0.05, remaining))


def _baked_transform(camera: Camera, record_form: str) -> DisplayTransform | None:
    """The display transform a recording bakes into *camera*'s frames, if any (as
    a `CameraTake` decides).
    """
    if record_form == "display" and not camera.display_transform.is_identity:
        return camera.display_transform
    return None


def _frame_size(camera: Camera, record_form: str) -> tuple[int, int]:
    """The (width, height) a recording writes for *camera*: a baked transform's
    output size (a 90 deg/270 deg rotation transposes it), else the sensor's.
    """
    sensor = (camera.backend.width(), camera.backend.height())
    transform = _baked_transform(camera, record_form)
    return transform.output_size(*sensor) if transform else sensor


class _NullWriter(AsyncFrameWriter):
    """Discards frames: `--sink null` measures everything but the encoder."""

    def _open_sink(self, filename, fps, frame_size) -> None:
        pass

    def _write_frame(self, frame) -> None:
        pass

    def _close_sink(self) -> None:
        pass


def _make_writer(
    video_format: VideoFormat | None, queue_size: int, *, profile: bool
) -> AsyncFrameWriter:
    """A real writer for `video_format`, or the null sink when it is None."""
    if video_format is None:
        return _NullWriter(queue_size, profile=profile)
    return video_format.create_writer(queue_size, profile=profile)


def _sink_path(tmpdir: Path, serial: str, video_format: VideoFormat | None) -> Path:
    return tmpdir / f"{serial}.{video_format.extension if video_format else 'null'}"


class _JitterProbe:
    """Measures sleep overshoot (a proxy for GIL convoys / scheduler delay)."""

    def __init__(self) -> None:
        self._samples: list[float] = []
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def _run(self) -> None:
        while not self._stop.is_set():
            t0 = time.perf_counter_ns()
            time.sleep(0.005)
            self._samples.append((time.perf_counter_ns() - t0) / 1e6 - 5.0)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> float | None:
        """The p99 overshoot in ms, or None with too few samples (or never started)."""
        self._stop.set()
        if self._thread.ident is not None:
            self._thread.join(timeout=1.0)
        if len(self._samples) < 20:
            return None
        return _pct(sorted(self._samples), 99)


def _cpu_percent_probe():
    """This process, primed so its next `cpu_percent()` covers the trial; None
    without psutil.
    """
    try:
        import psutil
    except ImportError:
        return None
    proc = psutil.Process()
    proc.cpu_percent()  # prime the interval
    return proc


# --- Scenarios ---


def _measure_rate(
    cameras: list[Camera],
    arm: Callable[[CameraBackend], None],
    fetch: Callable[[CameraBackend], object],
    duration_s: float,
    warmup_s: float,
    cancel: threading.Event | None,
) -> dict[str, float]:
    """Each camera's delivered fps ({serial: fps}), all fetching back to back on
    their own threads (so bus and GIL contention count). A camera whose *arm* or
    grab raises is left out, for the caller to note: a slowest rate must not
    silently rise to the rest's.
    """
    results: dict[str, float] = {}
    stop = threading.Event()

    def loop(camera: Camera) -> None:
        backend = camera.backend
        try:
            arm(backend)
            warm_deadline = time.perf_counter() + warmup_s
            while time.perf_counter() < warm_deadline and not stop.is_set():
                fetch(backend)
            grabbed = 0
            t0 = time.perf_counter()
            while not stop.is_set():
                if fetch(backend) is not None:
                    grabbed += 1
            elapsed = time.perf_counter() - t0
            results[camera.serial_number] = grabbed / elapsed if elapsed > 0 else 0.0
        except Exception:
            log.debug(
                "rate measurement failed on %s", camera.serial_number, exc_info=True
            )
        finally:
            backend.stop_grab()

    threads = [
        threading.Thread(target=loop, args=(c,), name=f"rate-{c.serial_number}")
        for c in cameras
    ]
    for t in threads:
        t.start()
    _wait(warmup_s + duration_s, cancel)
    stop.set()
    for t in threads:
        t.join()
    return results


def _arm_software(backend: CameraBackend) -> None:
    backend.begin_software_trigger_preview()
    backend.start_grab_preview()


def _fetch_software(backend: CameraBackend) -> object:
    backend.trigger_once()
    return backend.retrieve(GRAB_TIMEOUT_MS, _wants_array)


def _arm_freerun(backend: CameraBackend) -> None:
    if not backend.begin_freerun():
        raise RuntimeError("this backend does not support free-run")
    backend.start_grab_record()  # all-frames buffering, like a recording


def _fetch_freerun(backend: CameraBackend) -> object:
    return backend.retrieve_freerun(GRAB_TIMEOUT_MS, _wants_array)


def measure_grab_ceiling(
    cameras: list[Camera],
    duration_s: float,
    warmup_s: float = WARMUP_S,
    cancel: threading.Event | None = None,
) -> dict[str, float]:
    """Max software-triggered acquisition fps per camera ({serial: fps}), all
    grabbing at once with no writer; every frame's array is materialized, as in a
    recording. A camera that fails to arm is left out.
    """
    return _measure_rate(
        cameras, _arm_software, _fetch_software, duration_s, warmup_s, cancel
    )


def measure_grab_ceiling_solo(
    cameras: list[Camera],
    duration_s: float,
    warmup_s: float = WARMUP_S,
    cancel: threading.Event | None = None,
) -> dict[str, float]:
    """Each camera's acquisition ceiling grabbing alone, in turn; against the
    concurrent ceiling it shows transport contention
    (`Ceilings.bus_contended`).
    """
    solo: dict[str, float] = {}
    for camera in cameras:
        if _cancelled(cancel):
            break
        solo.update(measure_grab_ceiling([camera], duration_s, warmup_s, cancel))
    return solo


def _throughput_mbps(
    grab_fps: dict[str, float], sizes: dict[str, tuple[int, int]]
) -> tuple[dict[str, float], float]:
    """Per-camera and total MB/s at the concurrent grab ceiling: frame size x fps
    at Mono8's 1 byte/pixel, since the SDK cannot time transfer apart.
    """
    per_cam: dict[str, float] = {}
    for serial, fps in grab_fps.items():
        width, height = sizes.get(serial, (0, 0))
        per_cam[serial] = (width * height * fps) / 1e6
    return per_cam, sum(per_cam.values())


def _probe_system_load() -> tuple[float | None, float | None]:
    """System-wide CPU% and 1-min load per core, each None when unavailable.
    Called before the benchmark loads the machine, so it measures other
    processes.
    """
    cpu: float | None = None
    try:
        import psutil

        cpu = psutil.cpu_percent(interval=0.2)  # system-wide, short blocking sample
    except Exception:
        cpu = None
    load: float | None = None
    try:
        load = os.getloadavg()[0] / (os.cpu_count() or 1)
    except OSError, AttributeError:  # getloadavg is Unix-only
        load = None
    return cpu, load


def measure_freerun_ceiling(
    cameras: list[Camera],
    duration_s: float,
    warmup_s: float = WARMUP_S,
    cancel: threading.Event | None = None,
) -> dict[str, float]:
    """Max free-run fps per camera ({serial: fps}; continuous, untriggered): a
    hardware-triggered rig's acquisition ceiling. A camera that cannot free-run
    is left out.
    """
    return _measure_rate(
        cameras, _arm_freerun, _fetch_freerun, duration_s, warmup_s, cancel
    )


def measure_encode_ceiling(
    formats: Mapping[str, VideoFormat | None],
    sizes: dict[str, tuple[int, int]],
    fps: float,
    duration_s: float,
    warmup_s: float = WARMUP_S,
    cancel: threading.Event | None = None,
    queue_size: int = WRITER_QUEUE_SIZE,
) -> dict[str, float]:
    """Max encode fps per camera ({serial: fps}), every camera's writer at once,
    each fed one synthetic frame as fast as it accepts (a full queue yields to
    the encoder). `frames_written` counts only encoded frames, so it gives the
    drain rate however hard the producer pushes.
    """
    results: dict[str, float] = {}
    with tempfile.TemporaryDirectory(prefix="octacam-bench-") as tmp:
        tmpdir = Path(tmp)

        def loop(serial: str, size: tuple[int, int]) -> None:
            width, height = size
            # One constant frame, reused across writes: the writer never mutates
            # it, and the content is irrelevant to encoder throughput.
            frame = np.zeros((height, width), dtype=np.uint8)
            fmt = formats[serial]
            writer = _make_writer(fmt, queue_size, profile=True)
            if not writer.open(str(_sink_path(tmpdir, serial, fmt)), fps, size):
                results[serial] = 0.0
                return
            try:
                warm_deadline = time.perf_counter() + warmup_s
                while time.perf_counter() < warm_deadline and not _cancelled(cancel):
                    if writer.write(frame) is not WriteResult.WRITTEN:
                        time.sleep(0.0005)
                base = writer.frames_written
                t0 = time.perf_counter()
                deadline = t0 + duration_s
                while time.perf_counter() < deadline and not _cancelled(cancel):
                    if writer.write(frame) is not WriteResult.WRITTEN:
                        time.sleep(0.0005)
                # Read before close(), which drains the queue past the window.
                window = time.perf_counter() - t0
                drained_before_close = writer.frames_written - base
            finally:
                writer.close()
            results[serial] = drained_before_close / window if window > 0 else 0.0

        threads = [
            threading.Thread(target=loop, args=(s, sz), name=f"encceil-{s}")
            for s, sz in sizes.items()
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
    return results


@dataclass
class _CamAccum:
    """One camera's end-to-end trial counters, written by its grab thread.
    `mark` snapshots them, so a window counts only what happened between
    two marks.
    """

    camera: Camera
    writer: AsyncFrameWriter
    size: tuple[int, int]
    grabbed: int = 0
    dropped: int = 0
    acquire_ns: list[int] = field(default_factory=list)
    transform_ns: list[int] = field(default_factory=list)
    enqueue_ns: list[int] = field(default_factory=list)

    def _samples(self) -> dict[str, list[int]]:
        return {
            "acquire": self.acquire_ns,
            "transform": self.transform_ns,
            "enqueue": self.enqueue_ns,
            "encode": self.writer.encode_ns_samples,
        }

    def mark(self) -> dict[str, int]:
        """Every counter now: the frame counts and each stage's sample count."""
        return {
            "grabbed": self.grabbed,
            "dropped": self.dropped,
            "encoded": self.writer.frames_written,
        } | {name: len(samples) for name, samples in self._samples().items()}

    def result(
        self,
        start: dict[str, int],
        end: dict[str, int],
        target_fps: float,
        window: float,
    ) -> CameraTrial:
        """The trial between the *start* and *end* marks, *window* seconds apart."""
        grabbed = end["grabbed"] - start["grabbed"]
        dropped = end["dropped"] - start["dropped"]
        serial = self.camera.serial_number
        # A grabbed-encoded gap with drops is encoder backpressure.
        log.debug(
            "trial %s: grabbed=%d encoded=%d dropped=%d",
            serial,
            grabbed,
            end["encoded"] - start["encoded"],
            dropped,
        )
        return CameraTrial(
            serial=serial,
            name=self.camera.name,
            width=self.size[0],
            height=self.size[1],
            target_fps=target_fps,
            achieved_fps=grabbed / window if window > 0 else 0.0,
            grabbed=grabbed,
            dropped=dropped,
            drop_rate=dropped / grabbed if grabbed else 0.0,
            max_queue_depth=self.writer.max_queue_depth,
            stages={
                name: _stage_timing(name, samples[start[name] : end[name]])
                for name, samples in self._samples().items()
            },
        )


def _trial_grab(
    accum: _CamAccum, stop: threading.Event, record_form: str, free_run: bool
) -> None:
    """A trial's grab loop: retrieve -> transform -> write, each stage timed."""
    backend = accum.camera.backend
    transform = _baked_transform(accum.camera, record_form)
    retrieve = backend.retrieve_freerun if free_run else backend.retrieve
    while not stop.is_set() and backend.is_grabbing():
        t0 = time.perf_counter_ns()
        frame = retrieve(GRAB_TIMEOUT_MS, _wants_array)
        t1 = time.perf_counter_ns()
        if frame is None or frame[0] is None:
            continue
        array = frame[0]
        accum.acquire_ns.append(t1 - t0)
        accum.grabbed += 1
        if transform is not None:
            t2 = time.perf_counter_ns()
            array = apply_display_transform(array, transform)
            accum.transform_ns.append(time.perf_counter_ns() - t2)
        t3 = time.perf_counter_ns()
        accepted = accum.writer.write(array) is WriteResult.WRITTEN
        accum.enqueue_ns.append(time.perf_counter_ns() - t3)
        if not accepted:
            accum.dropped += 1


@dataclass
class TrialOutcome:
    """Aggregate of one end-to-end trial (per-camera trials + system probes)."""

    trials: list[CameraTrial]
    jitter_p99_ms: float | None
    cpu_percent: float | None
    queue_size: int = WRITER_QUEUE_SIZE  # each writer's queue bound

    @property
    def achieved_fps(self) -> float:
        return min((t.achieved_fps for t in self.trials), default=0.0)

    @property
    def drop_rate(self) -> float:
        return max((t.drop_rate for t in self.trials), default=0.0)

    @property
    def max_queue_depth(self) -> int:
        return max((t.max_queue_depth for t in self.trials), default=0)

    @property
    def passed(self) -> bool:
        """Loose 'achievable' bar: enough fps delivered, drops under 1%."""
        if not self.trials:
            return False
        return all(
            t.achieved_fps >= t.target_fps * ACHIEVE_FRACTION
            and t.drop_rate <= DROP_THRESHOLD
            for t in self.trials
        )

    @property
    def stable_passed(self) -> bool:
        """The max search's bar: `passed` with headroom, i.e. the tighter
        drop budget and the queue-saturation guard (inert for the null sink,
        whose queue never fills).
        """
        if not self.passed:
            return False
        guard = self.queue_size * QUEUE_SATURATION_FRACTION
        return all(
            t.drop_rate <= STABLE_DROP_THRESHOLD and t.max_queue_depth < guard
            for t in self.trials
        )


def run_target_trial(
    cameras: list[Camera],
    formats: Mapping[str, VideoFormat | None],
    target_fps: float,
    duration_s: float,
    record_form: str = "display",
    warmup_s: float = WARMUP_S,
    cancel: threading.Event | None = None,
    free_run: bool = False,
    queue_size: int = WRITER_QUEUE_SIZE,
) -> TrialOutcome:
    """Run the instrumented pipeline at *target_fps* for *duration_s*.

    One shared timer triggers every camera; each camera's thread runs retrieve ->
    transform -> write into a profiled writer for its entry in *formats* (the null
    sink for None). Only the window after the warmup counts. With *free_run* the
    cameras clock themselves and *target_fps* is only the container rate: the
    free-run/external record path, encoder contention included.
    """
    accums = [
        _CamAccum(
            c,
            _make_writer(formats[c.serial_number], queue_size, profile=True),
            _frame_size(c, record_form),
        )
        for c in cameras
    ]
    backends = [c.backend for c in cameras]

    with tempfile.TemporaryDirectory(prefix="octacam-bench-") as tmp:
        tmpdir = Path(tmp)
        # Set before the try, for the finally to tear down after an early raise.
        stop = threading.Event()
        timer: PreciseTimer | None = None
        grab_threads: list[threading.Thread] = []
        jitter = _JitterProbe()
        proc = _cpu_percent_probe()
        try:
            for a in accums:
                serial = a.camera.serial_number
                path = _sink_path(tmpdir, serial, formats[serial])
                if not a.writer.open(str(path), target_fps, a.size):
                    # An unopened writer refuses every frame, which would read as
                    # an ENCODE bottleneck. The finally closes those already open.
                    raise RuntimeError(f"writer failed to open for {serial}")

            for backend in backends:
                if free_run:
                    # Best effort: a camera that cannot free-run shows as a
                    # low-fps row (diagnose runs this only after the ceiling did).
                    try:
                        backend.begin_freerun()
                    except Exception:
                        log.debug("free-run arm failed in trial", exc_info=True)
                else:
                    backend.begin_software_trigger_preview()
                backend.start_grab_record()

            grab_threads = [
                threading.Thread(
                    target=_trial_grab,
                    args=(a, stop, record_form, free_run),
                    name=f"trial-{a.camera.serial_number}",
                    daemon=True,
                )
                for a in accums
            ]
            if not free_run:
                timer = PreciseTimer(lambda: [b.trigger_once() for b in backends])
                timer.set_frequency(target_fps)
            for t in grab_threads:
                t.start()
            if timer is not None:
                timer.start()
            jitter.start()

            _wait(warmup_s, cancel)
            starts = [a.mark() for a in accums]
            t_measure = time.perf_counter()
            _wait(duration_s, cancel)
            # Snapshot before the producers stop, so the counts span exactly
            # `window`, not the stop+join tail.
            window = time.perf_counter() - t_measure
            ends = [a.mark() for a in accums]
        finally:
            stop.set()
            if timer is not None:
                timer.stop()
            for backend in backends:
                # Wakes a parked retrieve; harmless if never grabbing.
                with contextlib.suppress(Exception):
                    backend.stop_grab()
            for t in grab_threads:
                t.join(timeout=GRAB_TIMEOUT_MS / 1000.0 + 1.0)
            for a in accums:
                a.writer.close()  # idempotent: a no-op for a writer never opened
            jitter_p99 = jitter.stop()
            cpu = proc.cpu_percent() if proc is not None else None

    return TrialOutcome(
        trials=[
            a.result(start, end, target_fps, window)
            for a, start, end in zip(accums, starts, ends, strict=True)
        ],
        jitter_p99_ms=jitter_p99,
        cpu_percent=cpu,
        queue_size=queue_size,
    )


# --- Max-fps search (software trigger only) ---


def find_max_fps(
    probe: Callable[[float], bool],
    lo: float,
    hi: float,
    iterations: int = 4,
) -> float:
    """Bisect for the highest fps *probe* passes, from a known-passing *lo* to an
    upper bound *hi*; never below *lo*.
    """
    best = lo
    if not probe(hi):
        low, high = lo, hi
        for _ in range(iterations):
            mid = (low + high) / 2.0
            if probe(mid):
                best = mid
                low = mid
            else:
                high = mid
    else:
        best = hi
    return best


def _reconcile_stable_max(
    candidate: float, confirm: TrialOutcome, lo: float, ceiling_cap: float
) -> tuple[float, bool]:
    """The stable max to report, and whether the confirmation trial held.

    A trial passes at `ACHIEVE_FRACTION` of its target, so the winning
    target can exceed the rate delivered, even the acquisition ceiling. Report
    the sustained achieved rate, capped at *ceiling_cap* = min(grab, encode): a
    rate neither stage sustains alone is not stable.
    """
    if confirm.stable_passed:
        return min(candidate, confirm.achieved_fps, ceiling_cap), True
    backed_off = max(lo, candidate * (1 - STABILITY_MARGIN))
    return min(backed_off, confirm.achieved_fps, ceiling_cap), False


def _search_stable_max(
    trial: Callable[[float, float], TrialOutcome],
    lo: float,
    cap: float,
    probe_s: float,
    confirm_s: float,
    progress: Callable[[str, float, float], None],
    cancel: threading.Event | None,
) -> tuple[float, bool | None]:
    """Bisect short `trial(fps, seconds)` runs from *lo* (known stable) to 10%
    over *cap* (the predicted max) for the highest stable rate, then retest it
    over *confirm_s*: a short probe can miss a queue that builds up over seconds.

    Returns the max to report and whether the confirmation held (None when
    cancelled before it ran). *progress* gets (detail, offset, budget) in seconds
    into the search's budget of `(1 + FIND_MAX_ITERATIONS)` probes plus the
    confirmation.
    """
    probe_slot = WARMUP_S + probe_s
    probes = 0

    def probe(fps: float) -> bool:
        nonlocal probes
        if _cancelled(cancel):
            return False
        progress(f"probing {fps:.0f} fps", probes * probe_slot, probe_slot)
        probes += 1
        return trial(fps, probe_s).stable_passed

    candidate = find_max_fps(probe, lo, cap * 1.1, iterations=FIND_MAX_ITERATIONS)
    if _cancelled(cancel):
        return min(candidate, cap), None
    progress(
        f"confirming {candidate:.0f} fps",
        (1 + FIND_MAX_ITERATIONS) * probe_slot,
        WARMUP_S + confirm_s,
    )
    return _reconcile_stable_max(candidate, trial(candidate, confirm_s), lo, cap)


# --- Verdict ---


def _classify(
    target_fps: float,
    ceilings: Ceilings,
    outcome: TrialOutcome,
    throughput_total: float = 0.0,
) -> tuple[bool, str, list[str]]:
    """Return `(achievable, bottleneck, recommendations)` for the target fps."""
    achievable = outcome.passed
    grab_min = ceilings.grab_min
    encode_min = ceilings.encode_min
    recs: list[str] = []

    bus_contended = ceilings.bus_contended

    # A target right at a ceiling is attributed to that stage.
    if target_fps > grab_min * (1 + DROP_THRESHOLD) and grab_min <= encode_min:
        bottleneck = TRANSFER if bus_contended else ACQUISITION
    elif target_fps > encode_min * (1 + DROP_THRESHOLD):
        bottleneck = ENCODE
    elif not achievable:
        # Each stage sustains the target alone: the loss is contention, on the
        # bus if the cameras throttle each other, else for CPU/GIL (host).
        bottleneck = TRANSFER if bus_contended else HOST
    else:
        bottleneck = NONE

    if bottleneck == TRANSFER:
        solo_min = ceilings.grab_solo_min
        rec = (
            "Transfer-bound: the cameras need more bus bandwidth than the link "
            f"provides \N{EM DASH} each delivers \N{ALMOST EQUAL TO}{solo_min:.0f} "
            "fps alone but only "
            f"\N{ALMOST EQUAL TO}{grab_min:.0f} fps together"
        )
        if throughput_total:
            rec += f" (\N{ALMOST EQUAL TO}{throughput_total:.0f} MB/s aggregate)"
        rec += (
            f". A single USB3 host controller sustains only ~{USB3_BUS_MBPS:.0f} "
            "MB/s across its cameras; distribute the cameras across separate USB "
            "host controllers, or lower the resolution/fps."
        )
        recs.append(rec)
    elif bottleneck == ACQUISITION:
        rec = (
            "Acquisition-bound: the camera cannot deliver frames fast enough "
            f"(\N{ALMOST EQUAL TO}{grab_min:.0f} fps software-trigger ceiling). "
            "Shorten the exposure, "
            "raise DeviceLinkThroughputLimit, shrink the ROI, or use an external "
            "hardware trigger (which overlaps exposure with transfer)."
        )
        freerun_min = ceilings.freerun_min
        if ceilings.freerun_fps and freerun_min > grab_min * 1.05:
            rec += (
                " The measured free-run ceiling is "
                f"\N{ALMOST EQUAL TO}{freerun_min:.0f} fps/cam \N{EM DASH} an "
                "external hardware trigger would reach roughly that, since it "
                "overlaps exposure with transfer just like free-run does."
            )
        recs.append(rec)
    elif bottleneck == ENCODE:
        recs += [
            (
                "Encode-bound: the encoder cannot keep up "
                f"(\N{ALMOST EQUAL TO}{encode_min:.0f} fps ceiling). Use a faster "
                "x264 preset "
                "(ultrafast), lower the resolution, switch save_method to 'raw' and "
                "transcode later, or run fewer cameras per host."
            ),
        ]
    elif bottleneck == HOST:
        recs += [
            (
                "Host-bound: each stage can sustain the target alone, but the full "
                "system falls short under contention (shared USB bus / CPU / GIL). "
                "Reduce the per-host camera count, lower resolution or fps, or split "
                "cameras across USB controllers."
            ),
        ]

    if outcome.jitter_p99_ms is not None and outcome.jitter_p99_ms > 5.0:
        recs.append(
            "Scheduler jitter is high (p99 sleep overshoot "
            f"{outcome.jitter_p99_ms:.1f} "
            "ms) \N{EM DASH} other processes are contending for the CPU; close them "
            "or pin "
            "octacam to dedicated cores."
        )
    return achievable, bottleneck, recs


def _load_recommendations(
    system_cpu_percent: float | None, load_per_core: float | None
) -> list[str]:
    """Advice when other processes were already loading the machine."""
    if system_cpu_percent is not None and system_cpu_percent > SYSTEM_CPU_WARN_PERCENT:
        return [
            (
                f"The machine was already ~{system_cpu_percent:.0f}% CPU-busy "
                "before the benchmark started \N{EM DASH} other processes are "
                "competing for the "
                "CPU, which skews these numbers and risks dropped frames in a real "
                "recording. Close them and re-run."
            )
        ]
    if load_per_core is not None and load_per_core > LOAD_PER_CORE_WARN:
        return [
            (
                f"System load is high ({load_per_core:.2f} per core) \N{EM DASH} "
                "other work "
                "is competing for the CPU, which may skew these numbers and risk "
                "dropped "
                "frames in a real recording. Close other processes and re-run."
            )
        ]
    return []


# --- Orchestrator ---


def diagnose(
    system: CameraSystem,
    settings: RecordingSettings,
    *,
    target_fps: float | None = None,
    duration_s: float = 5.0,
    find_max: bool = True,
    measure_freerun: bool = True,
    sink: str = "config",
    progress_cb: ProgressCallback | None = None,
    cancel: threading.Event | None = None,
) -> DiagnosticReport:
    """Benchmark an open `CameraSystem` at *target_fps* (default: the
    settings').

    *sink* `"config"` writes the settings' real format, `"null"` discards
    frames (acquisition and host only). *find_max* searches for the stable
    software-trigger max; *measure_freerun* adds the free-run ceiling.
    *progress_cb* gets a `Progress` per step. The caller owns the
    cameras, open with their parameters loaded: this never opens or closes them
    and leaves every backend stopped.
    """
    cameras = list(system)
    target_fps = target_fps if target_fps is not None else settings.fps
    record_form = settings.record_form
    external = settings.trigger_source == "external"
    queue_size = settings.writer_queue_size  # as the record path's writers

    serials = [c.serial_number for c in cameras]
    video_format = None if sink == "null" else settings.video_format()
    formats: dict[str, VideoFormat | None] = dict.fromkeys(serials)  # null sink
    if video_format is not None:
        # The record path's NVENC session split, so no overflow encoder fails.
        per_cam, cap_warnings = resolve_capture_formats(
            video_format, len(cameras), settings.max_nvenc_sessions
        )
        formats.update(zip(serials, per_cam, strict=True))
        for message in cap_warnings:
            log.warning(message)
    sizes = {c.serial_number: _frame_size(c, record_form) for c in cameras}

    def trial(fps: float, seconds: float, free_run: bool = False) -> TrialOutcome:
        return run_target_trial(
            cameras,
            formats,
            fps,
            seconds,
            record_form,
            cancel=cancel,
            free_run=free_run,
            queue_size=queue_size,
        )

    run_freerun = measure_freerun
    run_encode = video_format is not None
    run_search = find_max and not external
    # Solo ceilings tell a transfer-bound rig from a host-bound one (>=2 cameras).
    run_solo = len(cameras) >= 2
    solo_warmup = 0.3
    solo_dur = max(1.0, duration_s / 2.0)
    # The free-run trial measures the hardware max with the encoder in the loop;
    # pointless without an encoder, and skipped if free-run turns out unsupported.
    run_freerun_trial = run_freerun and run_encode

    # Every phase that will run has a fixed slot in the budget, so one that is
    # skipped or ends early moves the bar forward, never back. The max search's
    # slot is its worst case: every probe plus the confirmation.
    window = WARMUP_S + duration_s
    probe_dur = max(1.5, duration_s / 2.0)
    budget = {PHASE_ACQUIRE: window}
    if run_solo:
        budget[PHASE_ACQUIRE_SOLO] = len(cameras) * (solo_warmup + solo_dur)
    if run_freerun:
        budget[PHASE_FREERUN] = window
    if run_encode:
        budget[PHASE_ENCODE] = window
    budget[PHASE_TRIAL] = window
    if run_freerun_trial:
        budget[PHASE_FREERUN_TRIAL] = window
    if run_search:
        budget[PHASE_MAX] = (1 + FIND_MAX_ITERATIONS) * (WARMUP_S + probe_dur) + window
    starts: dict[str, float] = {}
    total_s = 0.0
    for phase, seconds in budget.items():
        starts[phase] = total_s
        total_s += seconds

    def emit(
        phase: str, detail: str, offset_s: float = 0.0, step_s: float | None = None
    ) -> None:
        step = budget[phase] if step_s is None else step_s
        p = Progress(phase, detail, starts[phase] + offset_s, step, total_s)
        log.debug("benchmark: %s %s (at %.0fs)", phase, detail, p.elapsed_s)
        if progress_cb is not None:
            progress_cb(p)

    report = DiagnosticReport(
        backend=system.backend,
        n_cameras=len(cameras),
        target_fps=target_fps,
        trigger_source=settings.trigger_source,
        save_method="null" if sink == "null" else settings.save_method,
        # The args the encoder runs with (nvenc_params for "nvenc"); VideoFormat's
        # save_method is the writer kind, and a raw writer encodes nothing.
        ffmpeg_params=(
            video_format.ffmpeg_params
            if video_format is not None and video_format.save_method == "ffmpeg"
            else ""
        ),
        writer_queue_size=queue_size,
        duration_s=duration_s,
        trials=[],
        achieved_fps=0.0,
        drop_rate=0.0,
        achievable=False,
        bottleneck=NONE,
    )

    if not cameras:
        report.notes.append("No cameras are open; nothing to diagnose.")
        return report

    # Everything but the free-run runs on software triggers; an external rig's
    # rate is set by its source, so its software max search is skipped.
    if external:
        if run_freerun:
            report.notes.append(
                "This rig records with an external hardware trigger. The "
                "software-triggered acquisition ceiling below is a lower bound; the "
                "free-run ceiling is the external-trigger-equivalent acquisition rate "
                "(free-run overlaps exposure with transfer the same way), so the "
                "hardware max is derived from it. The software max-fps search is "
                "skipped \N{EM DASH} the production rate is set by the external source."
            )
        else:
            report.notes.append(
                "This rig records with an external hardware trigger, so the "
                "acquisition ceiling below is a software-triggered lower bound and "
                "the software max-fps search is skipped. Enable the free-run ceiling "
                "to estimate the external-trigger acquisition rate."
            )

    report.system_cpu_percent, report.load_per_core = _probe_system_load()

    def note_unmeasured(fps: dict[str, float], message: str) -> None:
        missing = [c.name for c in cameras if c.serial_number not in fps]
        if missing and not _cancelled(cancel):
            report.notes.append(message.format(", ".join(missing)))

    emit(PHASE_ACQUIRE, f"({duration_s:g}s)")
    grab_fps = measure_grab_ceiling(cameras, duration_s, cancel=cancel)
    note_unmeasured(
        grab_fps,
        "Acquisition ceiling not measured for {} (the camera failed to arm); the "
        "reported grab ceiling reflects only the cameras that armed successfully.",
    )

    grab_solo_fps: dict[str, float] = {}
    if run_solo and not _cancelled(cancel):
        emit(
            PHASE_ACQUIRE_SOLO, f"({len(cameras)}\N{MULTIPLICATION SIGN} {solo_dur:g}s)"
        )
        grab_solo_fps = measure_grab_ceiling_solo(
            cameras, solo_dur, warmup_s=solo_warmup, cancel=cancel
        )

    freerun_fps: dict[str, float] = {}
    if run_freerun and not _cancelled(cancel):
        emit(PHASE_FREERUN, f"({duration_s:g}s)")
        freerun_fps = measure_freerun_ceiling(cameras, duration_s, cancel=cancel)
        note_unmeasured(
            freerun_fps,
            "Free-run (external-trigger-equivalent) ceiling not measured for {}: "
            "this backend does not support free-run.",
        )

    encode_fps: dict[str, float] = {}
    if run_encode and not _cancelled(cancel):
        emit(PHASE_ENCODE, f"({duration_s:g}s)")
        encode_fps = measure_encode_ceiling(
            formats, sizes, target_fps, duration_s, cancel=cancel, queue_size=queue_size
        )
    ceilings = Ceilings(
        grab_fps=grab_fps,
        encode_fps=encode_fps,
        freerun_fps=freerun_fps,
        grab_solo_fps=grab_solo_fps,
    )
    report.ceilings = ceilings

    report.throughput_mbps, report.throughput_mbps_total = _throughput_mbps(
        grab_fps, sizes
    )

    # --- end-to-end at the target fps ---
    emit(PHASE_TRIAL, f"@ {target_fps:g} fps ({duration_s:g}s)")
    outcome = trial(target_fps, duration_s)
    report.trials = outcome.trials
    report.achieved_fps = outcome.achieved_fps
    report.drop_rate = outcome.drop_rate
    report.jitter_p99_ms = outcome.jitter_p99_ms
    report.cpu_percent = outcome.cpu_percent

    # --- verdict ---
    report.achievable, report.bottleneck, report.recommendations = _classify(
        target_fps, ceilings, outcome, report.throughput_mbps_total
    )
    report.recommendations += _load_recommendations(
        report.system_cpu_percent, report.load_per_core
    )
    # A null sink measures no encoder: the acquisition ceiling alone.
    encode_min = ceilings.encode_min if video_format is not None else float("inf")
    report.predicted_max_fps = min(ceilings.grab_min, encode_min)

    # The hardware max, until the free-run trial below measures it.
    if freerun_fps:
        report.hardware_max_fps = min(ceilings.freerun_min, encode_min)

    # --- free-run end-to-end trial ---
    if run_freerun_trial and freerun_fps and not _cancelled(cancel):
        nominal = (
            ceilings.freerun_min if math.isfinite(ceilings.freerun_min) else target_fps
        )
        emit(PHASE_FREERUN_TRIAL, f"(~{nominal:g} fps, {duration_s:g}s)")
        fr = trial(nominal, duration_s, free_run=True)
        report.freerun_trials = fr.trials
        if fr.trials:
            # A shared external trigger cannot outrun the slowest camera's
            # written (not dropped) rate.
            sustained = min(t.achieved_fps * (1 - t.drop_rate) for t in fr.trials)
            if sustained > 0:
                report.hardware_max_fps = sustained

    # --- empirical stable max-fps search (software trigger) ---
    if run_search and not _cancelled(cancel):
        predicted = report.predicted_max_fps
        # lo is a rate already known to be stable when the target was.
        lo = target_fps if outcome.stable_passed else min(target_fps, predicted * 0.5)
        report.measured_max_fps, confirmed = _search_stable_max(
            trial,
            lo,
            predicted,
            probe_dur,
            duration_s,
            lambda detail, offset, step: emit(PHASE_MAX, detail, offset, step),
            cancel,
        )
        report.max_confirmed = bool(confirmed)
        if confirmed is False:
            report.notes.append(
                "The stable-max candidate did not hold over the longer "
                f"confirmation window; reported with a {STABILITY_MARGIN:.0%} "
                "safety margin."
            )

    if _cancelled(cancel):
        report.notes.append("Benchmark was cancelled before it finished.")
    elif progress_cb is not None:
        progress_cb(Progress(PHASE_DONE, "", total_s, 0.0, total_s))

    return report
