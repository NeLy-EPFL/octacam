"""Frame-rate benchmark: is the target fps achievable, what is the ceiling, and
which stage limits it.

It drives the real ``CameraBackend.retrieve`` and the real writers, but in loops
of its own: it deliberately re-implements the grab loop rather than reuse
``Camera._record_loop``, so the record path carries no instrumentation and each
ceiling is measured in isolation. Its only seams into the record path, the
read-only ``Camera.backend`` and ``AsyncFrameWriter(profile=...)``, cost a
recording nothing.

- **Grab ceiling** per camera, all grabbing at once: ``trigger_once()`` +
  ``retrieve()`` back to back, no writer. Trigger, exposure, transfer and copy
  are one ``acquire`` stage (the SDK cannot time them apart). The **free-run**
  ceiling overlaps exposure with readout as a hardware trigger does, so it is a
  hardware-triggered rig's ceiling, measured with no wiring.
- **Encode ceiling** per camera: every real writer at once, fed synthetic frames
  as fast as it takes them.
- **End-to-end** at the target fps: one timer triggering every camera, grab
  loops writing into real writers; achieved fps, drops, queue depth and
  per-stage timing.

The system ceiling is the slowest camera's (one timer triggers them all).
:func:`_classify` names the bottleneck, and the max search bisects short trials
toward a *stable* rate (:attr:`TrialOutcome.stable_passed`), confirmed over a
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
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

import numpy as np

from octacam.cameras.base import GRAB_TIMEOUT_MS, WRITER_QUEUE_SIZE, Camera
from octacam.transform import apply_display_transform
from octacam.trigger import PreciseTimer
from octacam.writer import AsyncFrameWriter, VideoFormat, resolve_capture_formats

if TYPE_CHECKING:
    from octacam.cameras.system import CameraSystem
    from octacam.controller import RecordingSettings

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

# Bottleneck labels, as the report and the GUI show them.
ACQUISITION = "acquisition"
TRANSFER = "transfer"
ENCODE = "encode"
HOST = "host"
NONE = "none"

# Concurrent acquisition below this fraction of the solo rate means the cameras
# slow each other down. The acquisition sweep runs no encoder and the SDKs
# release the GIL, so that is the shared transport (one USB3 controller carries
# ~350-400 MB/s), not the CPU: TRANSFER, not HOST.
CONTENTION_RATIO = 0.85
USB3_BUS_MBPS = 384.0  # only for the transfer-bound advice
# Ambient load above which other processes skew the numbers.
SYSTEM_CPU_WARN_PERCENT = 60.0
LOAD_PER_CORE_WARN = 0.7

# Phase labels, matched exactly by _ProgressPlan to weight the progress bar.
PHASE_ACQUIRE = "Measuring acquisition ceiling"
PHASE_ACQUIRE_SOLO = "Measuring per-camera solo ceiling"
PHASE_FREERUN = "Measuring free-run ceiling"
PHASE_ENCODE = "Measuring encode ceiling"
PHASE_TRIAL = "Running end-to-end trial"
PHASE_FREERUN_TRIAL = "Running free-run trial"
PHASE_MAX = "Searching for the stable max fps"


@dataclass
class Progress:
    """One progress update. A front end animates the bar from ``fraction`` (the
    run's completion at this phase's start) toward ``target`` (at its end) over
    ``eta_s``, smooth although updates arrive only at phase boundaries."""

    phase: str
    detail: str
    fraction: float
    target: float
    eta_s: float

    def to_dict(self) -> dict:
        return {
            "phase": self.phase,
            "detail": self.detail,
            "fraction": round(self.fraction, 4),
            "target": round(self.target, 4),
            "eta_s": round(self.eta_s, 3),
        }


ProgressCallback = Callable[["Progress"], None]


class _ProgressPlan:
    """Turns phase labels into :class:`Progress`, each phase that will run
    weighted by its expected wall-clock. A phase that reports many times (the
    max search's probes) passes ``advance=True`` and eases forward through its
    span, never snapping back, however many probes run."""

    _SUB_ADVANCE = 0.5  # an advancing re-emit closes half the remaining gap

    def __init__(self, phases: list[tuple[str, float]]):
        self._phases = phases
        self._total = sum(w for _, w in phases) or 1.0
        self._idx = -1
        self._before = 0.0
        self._within = 0.0  # progress within the current phase span, in [0, 1)

    def step(
        self,
        label: str,
        detail: str,
        *,
        advance: bool = False,
        eta: float | None = None,
    ) -> Progress:
        if self._idx < 0 or self._phases[self._idx][0] != label:
            if self._idx >= 0:
                self._before += self._phases[self._idx][1]
            self._idx += 1
            # Tolerate a label that skips ahead (a conditional phase not run).
            while self._idx < len(self._phases) and self._phases[self._idx][0] != label:
                self._before += self._phases[self._idx][1]
                self._idx += 1
            self._within = 0.0
        if self._idx >= len(self._phases):
            return Progress(label, detail, 1.0, 1.0, 0.0)
        weight = self._phases[self._idx][1]
        start = self._before / self._total
        span = weight / self._total
        eta_s = eta if eta is not None else weight
        if advance:
            before = self._within
            self._within = before + (1.0 - before) * self._SUB_ADVANCE
            return Progress(
                label, detail, start + span * before, start + span * self._within, eta_s
            )
        return Progress(label, detail, start + span * self._within, start + span, eta_s)

    def done(self) -> Progress:
        # A short eta eases the last sliver to 100% instead of snapping.
        return Progress("Done", "", 1.0, 1.0, 0.3)


# --- Result types (to_dict: the CLI's --json and the GUI payload) ---


def _finite(value: float | None, ndigits: int = 1) -> float | None:
    """*value* rounded, or None for None, inf or nan: ``Infinity`` (a missing
    ceiling) is invalid JSON that breaks the browser's ``JSON.parse``."""
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


def _slowest(fps: dict[str, float]) -> float:
    """The slowest camera's rate, or inf when none was measured."""
    return min(fps.values(), default=float("inf"))


@dataclass
class Ceilings:
    """Isolated per-camera ceilings (fps): ``grab_fps`` software-triggered
    (exposure and transfer in series), ``freerun_fps`` free-running (overlapped,
    as under a hardware trigger), and ``grab_solo_fps`` with each camera grabbing
    alone (≥2 cameras; see :attr:`bus_contended`)."""

    grab_fps: dict[str, float]
    encode_fps: dict[str, float]
    freerun_fps: dict[str, float] = field(default_factory=dict)
    grab_solo_fps: dict[str, float] = field(default_factory=dict)

    @property
    def grab_min(self) -> float:
        return _slowest(self.grab_fps)

    @property
    def encode_min(self) -> float:
        return _slowest(self.encode_fps)

    @property
    def freerun_min(self) -> float:
        return _slowest(self.freerun_fps)

    @property
    def grab_solo_min(self) -> float:
        return _slowest(self.grab_solo_fps)

    @property
    def bus_contended(self) -> bool:
        """Whether the cameras throttle each other's acquisition: the slowest
        concurrent rate is under :data:`CONTENTION_RATIO` of the slowest solo
        one. False without a solo pass."""
        solo = self.grab_solo_min
        return bool(self.grab_solo_fps) and math.isfinite(solo) and (
            self.grab_min < solo * CONTENTION_RATIO
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
            "ceilings": self.ceilings.to_dict() if self.ceilings else None,
            "predicted_max_fps": _finite(self.predicted_max_fps),
            "measured_max_fps": _finite(self.measured_max_fps),
            "max_confirmed": self.max_confirmed,
            "hardware_max_fps": _finite(self.hardware_max_fps),
            "throughput_mbps": {
                s: _finite(v) for s, v in self.throughput_mbps.items()
            },
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
    """Linear-interpolated ``q``-th percentile of an already-sorted list."""
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
    """Summarize a list of per-frame ns durations into a :class:`StageTiming`."""
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


def _frame_size(camera: Camera, record_form: str) -> tuple[int, int]:
    """The (width, height) a recording writes for *camera*, as
    :meth:`Camera.start_record` decides: a baked display transform's output size
    (a 90°/270° rotation transposes it), else the sensor's."""
    sensor = (camera.backend.width(), camera.backend.height())
    bake = record_form == "display" and not camera.display_transform.is_identity
    return camera.display_transform.output_size(*sensor) if bake else sensor


class _NullWriter(AsyncFrameWriter):
    """Discards frames: ``--sink null`` measures everything but the encoder."""

    def _open_sink(self, filename, fps, frame_size) -> None:
        pass

    def _write_frame(self, frame) -> None:
        pass

    def _close_sink(self) -> None:
        pass


def _make_writer(
    video_format: VideoFormat | None, queue_size: int, *, profile: bool
) -> AsyncFrameWriter:
    """A real writer for ``video_format``, or the null sink when it is None."""
    if video_format is None:
        return _NullWriter(queue_size, profile=profile)
    return video_format.create_writer(queue_size, profile=profile)


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
        self._stop.set()
        self._thread.join(timeout=1.0)
        if len(self._samples) < 20:
            return None
        return _pct(sorted(self._samples), 99)


def _cpu_percent_probe():
    """This process, primed so its next ``cpu_percent()`` covers the trial; None
    without psutil."""
    try:
        import psutil
    except ImportError:
        return None
    proc = psutil.Process()
    proc.cpu_percent()  # prime the interval
    return proc


# --- Scenarios ---


def measure_grab_ceiling(
    cameras: list[Camera],
    duration_s: float,
    warmup_s: float = WARMUP_S,
    cancel: threading.Event | None = None,
) -> dict[str, float]:
    """Max acquisition fps per camera ({serial: fps}), all grabbing at once on
    their own threads (so bus and GIL contention count) with no writer; every
    frame's array is materialized, as in a recording."""
    results: dict[str, tuple[int, float]] = {}
    stop = threading.Event()

    def loop(camera: Camera) -> None:
        backend = camera.backend
        try:
            # A camera that fails to arm is missing from the results, and
            # diagnose() says so: grab_min must not silently rise to the rest's.
            try:
                backend.begin_software_trigger_preview()
                backend.start_grab_preview()
            except Exception:
                log.debug(
                    "grab-ceiling arm failed on %s",
                    camera.serial_number,
                    exc_info=True,
                )
                return
            warm_deadline = time.perf_counter() + warmup_s
            while time.perf_counter() < warm_deadline and not stop.is_set():
                backend.trigger_once()
                backend.retrieve(GRAB_TIMEOUT_MS, _wants_array)
            grabbed = 0
            t0 = time.perf_counter()
            while not stop.is_set():
                backend.trigger_once()
                if backend.retrieve(GRAB_TIMEOUT_MS, _wants_array) is not None:
                    grabbed += 1
            elapsed = time.perf_counter() - t0
            results[camera.serial_number] = (grabbed, elapsed)
        finally:
            backend.stop_grab()

    threads = [
        threading.Thread(target=loop, args=(c,), name=f"grabceil-{c.serial_number}")
        for c in cameras
    ]
    for t in threads:
        t.start()
    _wait(warmup_s + duration_s, cancel)
    stop.set()
    for t in threads:
        t.join()
    return {
        serial: (grabbed / elapsed if elapsed > 0 else 0.0)
        for serial, (grabbed, elapsed) in results.items()
    }


def measure_grab_ceiling_solo(
    cameras: list[Camera],
    duration_s: float,
    warmup_s: float = WARMUP_S,
    cancel: threading.Event | None = None,
) -> dict[str, float]:
    """Each camera's acquisition ceiling grabbing alone, in turn; against the
    concurrent ceiling it shows transport contention
    (:attr:`Ceilings.bus_contended`)."""
    solo: dict[str, float] = {}
    for camera in cameras:
        if _cancelled(cancel):
            break
        solo.update(measure_grab_ceiling([camera], duration_s, warmup_s, cancel))
    return solo


def _throughput_mbps(
    grab_fps: dict[str, float], sizes: dict[str, tuple[int, int]]
) -> tuple[dict[str, float], float]:
    """Per-camera and total MB/s at the concurrent grab ceiling: frame size × fps
    at Mono8's 1 byte/pixel, since the SDK cannot time transfer apart."""
    per_cam: dict[str, float] = {}
    for serial, fps in grab_fps.items():
        width, height = sizes.get(serial, (0, 0))
        per_cam[serial] = (width * height * fps) / 1e6
    return per_cam, sum(per_cam.values())


def _probe_system_load() -> tuple[float | None, float | None]:
    """System-wide CPU% and 1-min load per core, each None when unavailable.
    Called before the benchmark loads the machine, so it measures other
    processes."""
    cpu: float | None = None
    try:
        import psutil

        cpu = psutil.cpu_percent(interval=0.2)  # system-wide, short blocking sample
    except Exception:
        cpu = None
    load: float | None = None
    try:
        load = os.getloadavg()[0] / (os.cpu_count() or 1)
    except (OSError, AttributeError):  # getloadavg is Unix-only
        load = None
    return cpu, load


def measure_freerun_ceiling(
    cameras: list[Camera],
    duration_s: float,
    warmup_s: float = WARMUP_S,
    cancel: threading.Event | None = None,
) -> tuple[dict[str, float], list[str]]:
    """Max free-run fps per camera (continuous, untriggered): a hardware-triggered
    rig's acquisition ceiling. Returns ({serial: fps}, notes); a camera that
    cannot free-run is left out and named in the notes."""
    results: dict[str, tuple[int, float]] = {}
    unsupported: list[str] = []
    stop = threading.Event()

    def loop(camera: Camera) -> None:
        backend = camera.backend
        try:
            armed = backend.begin_freerun()
        except Exception:
            log.debug("free-run arm failed on %s", camera.serial_number, exc_info=True)
            armed = False
        if not armed:
            unsupported.append(camera.name)
            return
        try:
            # Inside the try: a grab that cannot start is noted and stopped.
            backend.start_grab_record()  # all-frames buffering, like a recording
            warm_deadline = time.perf_counter() + warmup_s
            while time.perf_counter() < warm_deadline and not stop.is_set():
                backend.retrieve_freerun(GRAB_TIMEOUT_MS, _wants_array)
            grabbed = 0
            t0 = time.perf_counter()
            while not stop.is_set():
                if backend.retrieve_freerun(GRAB_TIMEOUT_MS, _wants_array) is not None:
                    grabbed += 1
            elapsed = time.perf_counter() - t0
            results[camera.serial_number] = (grabbed, elapsed)
        except Exception:
            log.debug(
                "free-run grab failed on %s", camera.serial_number, exc_info=True
            )
            unsupported.append(camera.name)
        finally:
            backend.stop_grab()

    threads = [
        threading.Thread(target=loop, args=(c,), name=f"freerun-{c.serial_number}")
        for c in cameras
    ]
    for t in threads:
        t.start()
    _wait(warmup_s + duration_s, cancel)
    stop.set()
    for t in threads:
        t.join()
    fps = {
        serial: (grabbed / elapsed if elapsed > 0 else 0.0)
        for serial, (grabbed, elapsed) in results.items()
    }
    notes: list[str] = []
    if unsupported:
        notes.append(
            "Free-run (external-trigger-equivalent) ceiling not measured for "
            f"{', '.join(unsupported)}: this backend does not support free-run."
        )
    return fps, notes


def measure_encode_ceiling(
    video_format: VideoFormat,
    sizes: dict[str, tuple[int, int]],
    fps: float,
    duration_s: float,
    warmup_s: float = WARMUP_S,
    cancel: threading.Event | None = None,
    formats_by_serial: dict[str, VideoFormat] | None = None,
    queue_size: int = WRITER_QUEUE_SIZE,
) -> dict[str, float]:
    """Max encode fps per camera ({serial: fps}), every real writer at once, each
    fed one synthetic frame as fast as it accepts (a full queue yields to the
    encoder). ``frames_written`` counts only encoded frames, so it gives the
    drain rate however hard the producer pushes. *formats_by_serial* overrides
    *video_format* per camera, as the record path splits NVENC sessions."""
    results: dict[str, float] = {}
    with tempfile.TemporaryDirectory(prefix="octacam-bench-") as tmp:
        tmpdir = Path(tmp)

        def loop(serial: str, size: tuple[int, int]) -> None:
            width, height = size
            # One constant frame, reused across writes: the writer never mutates
            # it, and the content is irrelevant to encoder throughput.
            frame = np.zeros((height, width), dtype=np.uint8)
            fmt = (formats_by_serial or {}).get(serial, video_format)
            writer = fmt.create_writer(queue_size, profile=True)
            path = tmpdir / f"{serial}.{fmt.extension}"
            if not writer.open(str(path), fps, size):
                results[serial] = 0.0
                return
            try:
                warm_deadline = time.perf_counter() + warmup_s
                while time.perf_counter() < warm_deadline and not _cancelled(cancel):
                    if not writer.write(frame):
                        time.sleep(0.0005)
                base = writer.frames_written
                t0 = time.perf_counter()
                deadline = t0 + duration_s
                while time.perf_counter() < deadline and not _cancelled(cancel):
                    if not writer.write(frame):
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
    """Per-camera accumulators for an end-to-end trial (grab-thread owned)."""

    grabbed: int = 0
    dropped: int = 0
    grab_ns: list[int] = field(default_factory=list)
    transform_ns: list[int] = field(default_factory=list)
    enqueue_ns: list[int] = field(default_factory=list)


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
        """The max search's bar: :attr:`passed` with headroom, i.e. the tighter
        drop budget and the queue-saturation guard (inert for the null sink,
        whose queue never fills)."""
        if not self.passed:
            return False
        guard = self.queue_size * QUEUE_SATURATION_FRACTION
        return all(
            t.drop_rate <= STABLE_DROP_THRESHOLD and t.max_queue_depth < guard
            for t in self.trials
        )


def run_target_trial(
    cameras: list[Camera],
    video_format: VideoFormat | None,
    target_fps: float,
    duration_s: float,
    record_form: str = "display",
    warmup_s: float = WARMUP_S,
    cancel: threading.Event | None = None,
    free_run: bool = False,
    formats_by_serial: dict[str, VideoFormat] | None = None,
    queue_size: int = WRITER_QUEUE_SIZE,
) -> TrialOutcome:
    """Run the instrumented pipeline at *target_fps* for *duration_s*.

    One shared timer triggers every camera; each camera's thread runs retrieve →
    transform → write into a profiled writer (the null sink when *video_format*
    is None). Only the window after the warmup counts. With *free_run* the
    cameras clock themselves and *target_fps* is only the container rate: the
    free-run/external record path, encoder contention included.
    """
    backends = [c.backend for c in cameras]
    sizes = [_frame_size(c, record_form) for c in cameras]
    accums = [_CamAccum() for _ in cameras]
    writers = [
        _make_writer(
            (formats_by_serial or {}).get(c.serial_number, video_format),
            queue_size,
            profile=True,
        )
        for c in cameras
    ]

    with tempfile.TemporaryDirectory(prefix="octacam-bench-") as tmp:
        tmpdir = Path(tmp)
        ext = video_format.extension if video_format else "null"

        # Set before the try, for the finally to tear down after an early raise.
        stop = threading.Event()
        timer: PreciseTimer | None = None
        grab_threads: list[threading.Thread] = []
        jitter = _JitterProbe()
        jitter_started = False
        proc = _cpu_percent_probe()
        jitter_p99: float | None = None
        cpu: float | None = None
        end_grabbed: list[int] = []
        end_dropped: list[int] = []
        end_grab_ns: list[int] = []
        end_transform_ns: list[int] = []
        end_enqueue_ns: list[int] = []
        end_encode: list[int] = []
        end_encode_ns: list[int] = []
        window = 0.0
        try:
            for camera, writer, size in zip(cameras, writers, sizes, strict=True):
                path = tmpdir / f"{camera.serial_number}.{ext}"
                if not writer.open(str(path), target_fps, size):
                    # An unopened writer refuses every frame, which would read as
                    # an ENCODE bottleneck. The finally closes those already open.
                    raise RuntimeError(
                        f"writer failed to open for {camera.serial_number}"
                    )

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

            def grab(camera, backend, writer, accum, size) -> None:
                bake = (
                    record_form == "display"
                    and not camera.display_transform.is_identity
                )
                transform = camera.display_transform if bake else None
                retrieve = backend.retrieve_freerun if free_run else backend.retrieve
                while not stop.is_set() and backend.is_grabbing():
                    t0 = time.perf_counter_ns()
                    frame = retrieve(GRAB_TIMEOUT_MS, _wants_array)
                    t1 = time.perf_counter_ns()
                    if frame is None:
                        continue
                    array, _timestamp = frame
                    if array is None:
                        continue
                    accum.grab_ns.append(t1 - t0)
                    accum.grabbed += 1
                    if transform is not None:
                        t2 = time.perf_counter_ns()
                        to_write = apply_display_transform(array, transform)
                        accum.transform_ns.append(time.perf_counter_ns() - t2)
                    else:
                        to_write = array
                    t3 = time.perf_counter_ns()
                    accepted = writer.write(to_write)
                    accum.enqueue_ns.append(time.perf_counter_ns() - t3)
                    if not accepted:
                        accum.dropped += 1

            grab_threads = [
                threading.Thread(
                    target=grab,
                    args=(c, b, w, a, sz),
                    name=f"trial-{c.serial_number}",
                    daemon=True,
                )
                for c, b, w, a, sz in zip(
                    cameras, backends, writers, accums, sizes, strict=True
                )
            ]

            if not free_run:
                timer = PreciseTimer(lambda: [b.trigger_once() for b in backends])
                timer.set_frequency(target_fps)

            for t in grab_threads:
                t.start()
            if timer is not None:
                timer.start()
            jitter.start()
            jitter_started = True

            # Warm up, then snapshot the baselines and measure over the window.
            _wait(warmup_s, cancel)
            base_grabbed = [a.grabbed for a in accums]
            base_dropped = [a.dropped for a in accums]
            base_grab_ns = [len(a.grab_ns) for a in accums]
            base_transform_ns = [len(a.transform_ns) for a in accums]
            base_enqueue_ns = [len(a.enqueue_ns) for a in accums]
            base_encode = [w.frames_written for w in writers]
            base_encode_ns = [len(w.encode_ns_samples) for w in writers]
            t_measure = time.perf_counter()

            _wait(duration_s, cancel)

            # Snapshot before the producers stop, so the counts span exactly
            # `window`, not the stop+join tail.
            window = time.perf_counter() - t_measure
            end_grabbed = [a.grabbed for a in accums]
            end_dropped = [a.dropped for a in accums]
            end_grab_ns = [len(a.grab_ns) for a in accums]
            end_transform_ns = [len(a.transform_ns) for a in accums]
            end_enqueue_ns = [len(a.enqueue_ns) for a in accums]
            end_encode = [w.frames_written for w in writers]
            end_encode_ns = [len(w.encode_ns_samples) for w in writers]
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
            for w in writers:
                w.close()  # idempotent: early-returns when the writer never opened
            # Joining a probe thread that never started would raise.
            jitter_p99 = jitter.stop() if jitter_started else None
            cpu = proc.cpu_percent() if proc is not None else None

    trials: list[CameraTrial] = []
    for i, camera in enumerate(cameras):
        grabbed = end_grabbed[i] - base_grabbed[i]
        dropped = end_dropped[i] - base_dropped[i]
        encoded = end_encode[i] - base_encode[i]
        stages = {
            "acquire": _stage_timing(
                "acquire", accums[i].grab_ns[base_grab_ns[i] : end_grab_ns[i]]
            ),
            "transform": _stage_timing(
                "transform",
                accums[i].transform_ns[base_transform_ns[i] : end_transform_ns[i]],
            ),
            "enqueue": _stage_timing(
                "enqueue",
                accums[i].enqueue_ns[base_enqueue_ns[i] : end_enqueue_ns[i]],
            ),
            "encode": _stage_timing(
                "encode",
                writers[i].encode_ns_samples[base_encode_ns[i] : end_encode_ns[i]],
            ),
        }
        width, height = sizes[i]
        trials.append(
            CameraTrial(
                serial=camera.serial_number,
                name=camera.name,
                width=width,
                height=height,
                target_fps=target_fps,
                achieved_fps=grabbed / window if window > 0 else 0.0,
                grabbed=grabbed,
                dropped=dropped,
                drop_rate=(dropped / grabbed if grabbed else 0.0),
                max_queue_depth=writers[i].max_queue_depth,
                stages=stages,
            )
        )
        # A grabbed-encoded gap with drops is encoder backpressure.
        log.debug(
            "trial %s: grabbed=%d encoded=%d dropped=%d",
            camera.serial_number,
            grabbed,
            encoded,
            dropped,
        )
    return TrialOutcome(
        trials=trials, jitter_p99_ms=jitter_p99, cpu_percent=cpu, queue_size=queue_size
    )


# --- Max-fps search (software trigger only) ---


def find_max_fps(
    probe: Callable[[float], bool],
    lo: float,
    hi: float,
    iterations: int = 4,
) -> float:
    """Bisect for the highest fps *probe* passes, from a known-passing *lo* to an
    upper bound *hi*; never below *lo*."""
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

    A trial passes at :data:`ACHIEVE_FRACTION` of its target, so the winning
    target can exceed the rate delivered, even the acquisition ceiling. Report
    the sustained achieved rate, capped at *ceiling_cap* = min(grab, encode): a
    rate neither stage sustains alone is not stable.
    """
    if confirm.stable_passed:
        return min(candidate, confirm.achieved_fps, ceiling_cap), True
    backed_off = max(lo, candidate * (1 - STABILITY_MARGIN))
    return min(backed_off, confirm.achieved_fps, ceiling_cap), False


# --- Verdict ---


def _classify(
    target_fps: float,
    ceilings: Ceilings,
    outcome: TrialOutcome,
    throughput_total: float = 0.0,
) -> tuple[bool, str, list[str]]:
    """Return ``(achievable, bottleneck, recommendations)`` for the target fps."""
    achievable = outcome.passed
    grab_min = ceilings.grab_min
    encode_min = ceilings.encode_min
    recs: list[str] = []

    # Cameras throttling each other point at the shared transport (TRANSFER).
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
            f"provides — each delivers ≈{solo_min:.0f} fps alone but only "
            f"≈{grab_min:.0f} fps together"
        )
        if throughput_total:
            rec += f" (≈{throughput_total:.0f} MB/s aggregate)"
        rec += (
            f". A single USB3 host controller sustains only ~{USB3_BUS_MBPS:.0f} "
            "MB/s across its cameras; distribute the cameras across separate USB "
            "host controllers, or lower the resolution/fps."
        )
        recs.append(rec)
    elif bottleneck == ACQUISITION:
        rec = (
            "Acquisition-bound: the camera cannot deliver frames fast enough "
            f"(≈{grab_min:.0f} fps software-trigger ceiling). Shorten the exposure, "
            "raise DeviceLinkThroughputLimit, shrink the ROI, or use an external "
            "hardware trigger (which overlaps exposure with transfer)."
        )
        freerun_min = ceilings.freerun_min
        if ceilings.freerun_fps and freerun_min > grab_min * 1.05:
            rec += (
                f" The measured free-run ceiling is ≈{freerun_min:.0f} fps/cam — an "
                "external hardware trigger would reach roughly that, since it "
                "overlaps exposure with transfer just like free-run does."
            )
        recs.append(rec)
    elif bottleneck == ENCODE:
        recs += [
            "Encode-bound: the encoder cannot keep up "
            f"(≈{encode_min:.0f} fps ceiling). Use a faster x264 preset "
            "(ultrafast), lower the resolution, switch save_method to 'raw' and "
            "transcode later, or run fewer cameras per host.",
        ]
    elif bottleneck == HOST:
        recs += [
            "Host-bound: each stage can sustain the target alone, but the full "
            "system falls short under contention (shared USB bus / CPU / GIL). "
            "Reduce the per-host camera count, lower resolution or fps, or split "
            "cameras across USB controllers.",
        ]

    if outcome.jitter_p99_ms is not None and outcome.jitter_p99_ms > 5.0:
        recs.append(
            f"Scheduler jitter is high (p99 sleep overshoot {outcome.jitter_p99_ms:.1f} "
            "ms) — other processes are contending for the CPU; close them or pin "
            "octacam to dedicated cores."
        )
    return achievable, bottleneck, recs


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
    """Benchmark an open :class:`CameraSystem` at *target_fps* (default: the
    settings').

    *sink* ``"config"`` writes the settings' real format, ``"null"`` discards
    frames (acquisition and host only). *find_max* searches for the stable
    software-trigger max; *measure_freerun* adds the free-run ceiling.
    *progress_cb* gets a :class:`Progress` per phase. The caller owns the
    cameras, open with their parameters loaded: this never opens or closes them
    and leaves every backend stopped.
    """
    cameras = list(system)
    target_fps = target_fps if target_fps is not None else settings.fps
    record_form = settings.record_form
    external = settings.trigger_source == "external"
    queue_size = settings.writer_queue_size  # as the record path's writers

    video_format = None if sink == "null" else settings.video_format()
    # The record path's NVENC session split, so no overflow encoder fails.
    formats_by_serial: dict[str, VideoFormat] | None = None
    if video_format is not None:
        per_cam, cap_warnings = resolve_capture_formats(
            video_format, len(cameras), settings.max_nvenc_sessions
        )
        formats_by_serial = {
            c.serial_number: fmt for c, fmt in zip(cameras, per_cam, strict=True)
        }
        for message in cap_warnings:
            log.warning(message)
    sizes = {c.serial_number: _frame_size(c, record_form) for c in cameras}

    run_freerun = measure_freerun
    run_encode = video_format is not None
    run_search = find_max and not external
    # Solo ceilings tell a transfer-bound rig from a host-bound one (≥2 cameras).
    run_solo = len(cameras) >= 2
    solo_warmup = 0.3
    solo_dur = max(1.0, duration_s / 2.0)
    # The free-run trial measures the hardware max with the encoder in the loop;
    # pointless without an encoder, and skipped if free-run turns out unsupported.
    run_freerun_trial = run_freerun and run_encode

    # The max search is weighted for its worst case: every probe plus the
    # confirmation.
    window = WARMUP_S + duration_s
    probe_dur = max(1.5, duration_s / 2.0)
    phases: list[tuple[str, float]] = [(PHASE_ACQUIRE, window)]
    if run_solo:
        phases.append((PHASE_ACQUIRE_SOLO, len(cameras) * (solo_warmup + solo_dur)))
    if run_freerun:
        phases.append((PHASE_FREERUN, window))
    if run_encode:
        phases.append((PHASE_ENCODE, window))
    phases.append((PHASE_TRIAL, window))
    if run_freerun_trial:
        phases.append((PHASE_FREERUN_TRIAL, window))
    if run_search:
        phases.append(
            (PHASE_MAX, (1 + FIND_MAX_ITERATIONS) * (WARMUP_S + probe_dur) + window)
        )
    plan = _ProgressPlan(phases)

    def emit(
        phase: str, detail: str = "", *, advance: bool = False, eta: float | None = None
    ) -> None:
        p = plan.step(phase, detail, advance=advance, eta=eta)
        log.debug("benchmark: %s %s (%.0f%%)", phase, detail, p.fraction * 100)
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
                "skipped — the production rate is set by the external source."
            )
        else:
            report.notes.append(
                "This rig records with an external hardware trigger, so the "
                "acquisition ceiling below is a software-triggered lower bound and "
                "the software max-fps search is skipped. Enable the free-run ceiling "
                "to estimate the external-trigger acquisition rate."
            )

    report.system_cpu_percent, report.load_per_core = _probe_system_load()

    emit(PHASE_ACQUIRE, f"({duration_s:g}s)")
    grab_fps = measure_grab_ceiling(cameras, duration_s, cancel=cancel)
    # A camera that failed to arm is absent, which would flatter grab_min.
    if not _cancelled(cancel):
        missing = [c.name for c in cameras if c.serial_number not in grab_fps]
        if missing:
            report.notes.append(
                "Acquisition ceiling not measured for "
                f"{', '.join(missing)} (the camera failed to arm); the reported "
                "grab ceiling reflects only the cameras that armed successfully."
            )

    grab_solo_fps: dict[str, float] = {}
    if run_solo and not _cancelled(cancel):
        emit(PHASE_ACQUIRE_SOLO, f"({len(cameras)}× {solo_dur:g}s)")
        grab_solo_fps = measure_grab_ceiling_solo(
            cameras, solo_dur, warmup_s=solo_warmup, cancel=cancel
        )

    freerun_fps: dict[str, float] = {}
    if run_freerun and not _cancelled(cancel):
        emit(PHASE_FREERUN, f"({duration_s:g}s)")
        freerun_fps, freerun_notes = measure_freerun_ceiling(
            cameras, duration_s, cancel=cancel
        )
        report.notes.extend(freerun_notes)

    encode_fps: dict[str, float] = {}
    if run_encode and not _cancelled(cancel):
        emit(PHASE_ENCODE, f"({duration_s:g}s)")
        encode_fps = measure_encode_ceiling(
            video_format,
            sizes,
            target_fps,
            duration_s,
            cancel=cancel,
            formats_by_serial=formats_by_serial,
            queue_size=queue_size,
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
    outcome = run_target_trial(
        cameras,
        video_format,
        target_fps,
        duration_s,
        record_form,
        cancel=cancel,
        formats_by_serial=formats_by_serial,
        queue_size=queue_size,
    )
    report.trials = outcome.trials
    report.achieved_fps = outcome.achieved_fps
    report.drop_rate = outcome.drop_rate
    report.jitter_p99_ms = outcome.jitter_p99_ms
    report.cpu_percent = outcome.cpu_percent

    # --- verdict ---
    report.achievable, report.bottleneck, report.recommendations = _classify(
        target_fps, ceilings, outcome, report.throughput_mbps_total
    )
    if report.system_cpu_percent is not None and (
        report.system_cpu_percent > SYSTEM_CPU_WARN_PERCENT
    ):
        report.recommendations.append(
            f"The machine was already ~{report.system_cpu_percent:.0f}% CPU-busy "
            "before the benchmark started — other processes are competing for the "
            "CPU, which skews these numbers and risks dropped frames in a real "
            "recording. Close them and re-run."
        )
    elif report.load_per_core is not None and report.load_per_core > LOAD_PER_CORE_WARN:
        report.recommendations.append(
            f"System load is high ({report.load_per_core:.2f} per core) — other work "
            "is competing for the CPU, which may skew these numbers and risk dropped "
            "frames in a real recording. Close other processes and re-run."
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
        fr = run_target_trial(
            cameras,
            video_format,
            nominal,
            duration_s,
            record_form,
            cancel=cancel,
            free_run=True,
            formats_by_serial=formats_by_serial,
            queue_size=queue_size,
        )
        report.freerun_trials = fr.trials
        if fr.trials:
            # A shared external trigger cannot outrun the slowest camera's
            # written (not dropped) rate.
            sustained = min(
                t.achieved_fps * (1 - t.drop_rate) for t in fr.trials
            )
            if sustained > 0:
                report.hardware_max_fps = sustained

    # --- empirical STABLE max-fps search (software trigger) ---
    if run_search and not _cancelled(cancel):
        predicted = report.predicted_max_fps
        hi = predicted * 1.1
        # lo is a rate already known to be *stable* when the target was.
        lo = target_fps if outcome.stable_passed else min(target_fps, predicted * 0.5)
        # A stable record rate cannot exceed what the camera can acquire or the
        # encoder can drain in isolation, so the reported max is capped here.
        ceiling_cap = predicted

        def probe(fps: float) -> bool:
            if _cancelled(cancel):
                return False
            emit(PHASE_MAX, f"probing {fps:.0f} fps", advance=True, eta=WARMUP_S + probe_dur)
            result = run_target_trial(
                cameras,
                video_format,
                fps,
                probe_dur,
                record_form,
                cancel=cancel,
                formats_by_serial=formats_by_serial,
                queue_size=queue_size,
            )
            return result.stable_passed

        candidate = find_max_fps(probe, lo, hi, iterations=FIND_MAX_ITERATIONS)
        # Retest the marginal edge over the full window: a short probe can miss
        # a queue that builds up over seconds.
        if _cancelled(cancel):
            report.measured_max_fps = min(candidate, ceiling_cap)
        else:
            emit(PHASE_MAX, f"confirming {candidate:.0f} fps", advance=True, eta=window)
            confirm = run_target_trial(
                cameras,
                video_format,
                candidate,
                duration_s,
                record_form,
                cancel=cancel,
                formats_by_serial=formats_by_serial,
                queue_size=queue_size,
            )
            report.measured_max_fps, report.max_confirmed = _reconcile_stable_max(
                candidate, confirm, lo, ceiling_cap
            )
            if not report.max_confirmed:
                report.notes.append(
                    "The stable-max candidate did not hold over the longer "
                    f"confirmation window; reported with a {STABILITY_MARGIN:.0%} "
                    "safety margin."
                )

    if _cancelled(cancel):
        report.notes.append("Benchmark was cancelled before it finished.")
    elif progress_cb is not None:
        progress_cb(plan.done())

    return report
