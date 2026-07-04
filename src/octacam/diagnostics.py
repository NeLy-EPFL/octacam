"""Frame-rate diagnostics: is the target fps achievable, what is the ceiling,
and which pipeline stage limits it.

This engine drives the **real** code paths — the real ``CameraBackend.retrieve``
(the true acquisition path) and the real video writers (:mod:`octacam.writer`) —
in its own tightly-instrumented measurement loops. It never touches the hot
``Camera._record_loop`` / ``AsyncFrameWriter`` steady state, so a normal
recording pays nothing, yet the numbers reflect exactly what recording does.

Three measurement scenarios feed one verdict:

- **Grab ceiling** (per camera): ``trigger_once()`` + ``retrieve()`` as fast as
  the camera returns, no writer → the maximum acquisition fps. This bundles the
  software-trigger fire, the exposure, the USB transfer, and the array copy; the
  vendor SDK cannot split transfer from exposure at the Python level, so it is
  reported as one ``acquire`` stage.
- **Encode ceiling** (per camera): feed the real writer synthetic Mono8 frames of
  the recorded size as fast as it accepts them → ``frames_written / elapsed`` is
  the encoder's drain rate. All cameras' writers run concurrently so the encoders
  contend for the CPU exactly as in a real recording.
- **End-to-end** at the target fps: the full pipeline (one shared trigger timer at
  the target rate → per-camera grab loops that ``retrieve → transform → write``
  into real writers) for a few seconds → achieved fps, drop rate, queue depth, and
  per-stage timing.

The per-camera pipeline ceiling is ``min(grab, encode)``; the synchronized system
ceiling is the slowest camera's, because one timer triggers every camera at the
same rate. The bottleneck is classified from where the target lands relative to
those ceilings (see :func:`_classify`), and :func:`find_max_fps` bisects short
end-to-end trials to confirm the empirical maximum.

Only the **software-trigger** path can be swept for a maximum: an
external-trigger rig runs at whatever rate the hardware source clocks, so the
engine measures what actually arrives and skips the ceiling search.
"""

from __future__ import annotations

import logging
import statistics
import tempfile
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

import numpy as np

from octacam.transform import apply_display_transform
from octacam.trigger import PreciseTimer
from octacam.writer import AsyncFrameWriter, VideoFormat

if TYPE_CHECKING:
    from octacam.cameras.base import Camera
    from octacam.cameras.system import CameraSystem
    from octacam.controller import RecordingSettings

log = logging.getLogger("octacam")

GRAB_TIMEOUT_MS = 100  # matches Camera.GRAB_TIMEOUT_MS
WRITER_QUEUE_SIZE = 20  # matches Camera.WRITER_QUEUE_SIZE
WARMUP_S = 0.5  # discarded settle time before every measurement window
# A trial "passes" (target achievable) when it delivers at least this fraction of
# the requested fps with no more than this drop rate. The drop bar matches the
# capture-feasibility bar used by the Phase-0 benchmarks (sub-1% is encoder noise).
ACHIEVE_FRACTION = 0.97
DROP_THRESHOLD = 0.01

# Bottleneck labels (also the vocabulary the GUI/report render).
ACQUISITION = "acquisition"
ENCODE = "encode"
HOST = "host"
NONE = "none"

# Progress phases reported through the optional callback.
ProgressCallback = Callable[[str, str], None]


# ---------------------------------------------------------------------------
# Result types (all JSON-serializable via to_dict for the CLI --json / GUI)
# ---------------------------------------------------------------------------


def _finite(value: float, ndigits: int = 1) -> float | None:
    """Round a float, or return None if it is inf/nan.

    ``float('inf')`` (an empty stage's implied fps, or a missing ceiling) would
    serialize to a bare ``Infinity`` token — invalid JSON that breaks the browser's
    ``JSON.parse`` — so it is normalized to ``null`` for both the CLI ``--json``
    output and the GUI payload.
    """
    import math

    if not math.isfinite(value):
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


@dataclass
class Ceilings:
    """Isolated per-camera acquisition and encode ceilings (fps)."""

    grab_fps: dict[str, float]
    encode_fps: dict[str, float]

    @property
    def grab_min(self) -> float:
        return min(self.grab_fps.values()) if self.grab_fps else float("inf")

    @property
    def encode_min(self) -> float:
        return min(self.encode_fps.values()) if self.encode_fps else float("inf")

    def to_dict(self) -> dict:
        return {
            "grab_fps": {s: _finite(v) for s, v in self.grab_fps.items()},
            "encode_fps": {s: _finite(v) for s, v in self.encode_fps.items()},
            "grab_min": _finite(self.grab_min),
            "encode_min": _finite(self.encode_min),
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
    duration_s: float
    trials: list[CameraTrial]
    achieved_fps: float  # slowest camera at target (the synchronized rate)
    drop_rate: float  # worst camera's drop rate at target
    achievable: bool
    bottleneck: str
    ceilings: Ceilings | None = None
    predicted_max_fps: float = 0.0
    measured_max_fps: float | None = None
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
            "duration_s": self.duration_s,
            "trials": [t.to_dict() for t in self.trials],
            "achieved_fps": round(self.achieved_fps, 2),
            "drop_rate": round(self.drop_rate, 5),
            "achievable": self.achievable,
            "bottleneck": self.bottleneck,
            "ceilings": self.ceilings.to_dict() if self.ceilings else None,
            "predicted_max_fps": _finite(self.predicted_max_fps),
            "measured_max_fps": (
                _finite(self.measured_max_fps)
                if self.measured_max_fps is not None
                else None
            ),
            "recommendations": self.recommendations,
            "jitter_p99_ms": (
                round(self.jitter_p99_ms, 3) if self.jitter_p99_ms is not None else None
            ),
            "cpu_percent": (
                round(self.cpu_percent, 1) if self.cpu_percent is not None else None
            ),
            "notes": self.notes,
        }


# ---------------------------------------------------------------------------
# Small stats helpers (no numpy dependency for the percentiles)
# ---------------------------------------------------------------------------


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
    """Sleep up to ``seconds``, returning early if ``cancel`` is set.

    Lets a diagnostic abort promptly on server shutdown instead of blocking a
    full measurement window."""
    end = time.perf_counter() + seconds
    while True:
        remaining = end - time.perf_counter()
        if remaining <= 0 or _cancelled(cancel):
            return
        time.sleep(min(0.05, remaining))


def _frame_size(camera: Camera, record_form: str) -> tuple[int, int]:
    """The (width, height) a recording would write for this camera.

    Mirrors :meth:`Camera.start_record`: the sensor size, or the display
    transform's output size when a non-identity transform is baked in ("display"
    form), which a 90°/270° rotation transposes.
    """
    sensor = (camera.backend.width(), camera.backend.height())
    bake = record_form == "display" and not camera.display_transform.is_identity
    return camera.display_transform.output_size(*sensor) if bake else sensor


# ---------------------------------------------------------------------------
# Null sink (isolates acquisition+overhead from the encoder for `--sink null`)
# ---------------------------------------------------------------------------


class _NullWriter(AsyncFrameWriter):
    """A writer that discards frames — measures everything *but* the encoder."""

    def _open_sink(self, filename, fps, frame_size) -> None:
        pass

    def _write_frame(self, frame) -> None:
        pass

    def _close_sink(self) -> None:
        pass


def _make_writer(
    video_format: VideoFormat | None, *, profile: bool
) -> AsyncFrameWriter:
    """A real writer for ``video_format``, or the null sink when it is None."""
    if video_format is None:
        return _NullWriter(WRITER_QUEUE_SIZE, profile=profile)
    return video_format.create_writer(WRITER_QUEUE_SIZE, profile=profile)


# ---------------------------------------------------------------------------
# Jitter / CPU probes (mirrors benchmarks/bench_pipeline.py)
# ---------------------------------------------------------------------------


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
    """Return a (start, read) pair around a psutil CPU sample, or (None, None)."""
    try:
        import psutil
    except ImportError:
        return None
    proc = psutil.Process()
    proc.cpu_percent()  # prime the interval
    return proc


# ---------------------------------------------------------------------------
# Scenario 1: grab ceiling (max acquisition fps, no writer)
# ---------------------------------------------------------------------------


def measure_grab_ceiling(
    cameras: list[Camera],
    duration_s: float,
    warmup_s: float = WARMUP_S,
    cancel: threading.Event | None = None,
) -> dict[str, float]:
    """Max acquisition fps per camera, all cameras grabbing concurrently.

    Drives each camera's real ``trigger_once()`` + ``retrieve()`` back-to-back on
    its own thread (so shared USB-bus / GIL contention is captured) with no writer
    attached, and returns ``{serial: fps}``. The array is materialized on every
    frame, matching what a recording pays.
    """
    results: dict[str, tuple[int, float]] = {}
    stop = threading.Event()

    def loop(camera: Camera) -> None:
        backend = camera.backend
        backend.begin_software_trigger_preview()
        backend.start_grab_preview()
        try:
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
    # Each thread does its own warmup then measures; let all run warmup + window.
    _wait(warmup_s + duration_s, cancel)
    stop.set()
    for t in threads:
        t.join()
    return {
        serial: (grabbed / elapsed if elapsed > 0 else 0.0)
        for serial, (grabbed, elapsed) in results.items()
    }


# ---------------------------------------------------------------------------
# Scenario 2: encode ceiling (max encoder drain rate, synthetic frames)
# ---------------------------------------------------------------------------


def measure_encode_ceiling(
    video_format: VideoFormat,
    sizes: dict[str, tuple[int, int]],
    fps: float,
    duration_s: float,
    warmup_s: float = WARMUP_S,
    cancel: threading.Event | None = None,
) -> dict[str, float]:
    """Max encode fps per camera, one real writer per camera running concurrently.

    Each writer is fed a constant synthetic Mono8 frame of that camera's recorded
    size as fast as it will accept it (a short sleep when the bounded queue is full
    yields the CPU to the encoder rather than busy-spinning). ``frames_written``
    counts only frames the sink actually encoded, so ``frames_written / window`` is
    the true drain rate regardless of how hard the producer pushes. Returns
    ``{serial: fps}``. Output files go to a temp dir that is removed afterwards.
    """
    results: dict[str, float] = {}
    with tempfile.TemporaryDirectory(prefix="octacam-bench-") as tmp:
        tmpdir = Path(tmp)

        def loop(serial: str, size: tuple[int, int]) -> None:
            width, height = size
            # One constant frame, reused across writes: the writer never mutates
            # it, and the content is irrelevant to encoder throughput.
            frame = np.zeros((height, width), dtype=np.uint8)
            writer = video_format.create_writer(WRITER_QUEUE_SIZE, profile=True)
            path = tmpdir / f"{serial}.{video_format.extension}"
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
                # Stop feeding, then let the queue drain so frames_written counts
                # only what was encoded during the window (close() drains fully).
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


# ---------------------------------------------------------------------------
# Scenario 3: end-to-end trial at a target fps (full instrumented pipeline)
# ---------------------------------------------------------------------------


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

    @property
    def achieved_fps(self) -> float:
        return min((t.achieved_fps for t in self.trials), default=0.0)

    @property
    def drop_rate(self) -> float:
        return max((t.drop_rate for t in self.trials), default=0.0)

    @property
    def passed(self) -> bool:
        if not self.trials:
            return False
        return all(
            t.achieved_fps >= t.target_fps * ACHIEVE_FRACTION
            and t.drop_rate <= DROP_THRESHOLD
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
) -> TrialOutcome:
    """Run the full instrumented pipeline at ``target_fps`` for ``duration_s``.

    One shared :class:`PreciseTimer` fires ``trigger_once()`` on every camera at
    the target rate; each camera's own grab thread runs the real
    ``retrieve → transform → write`` chain into a profiled writer (or the null
    sink when ``video_format`` is None). Per-stage timings and drops are measured
    only over the window *after* a short warmup, so encoder start-up and settle
    do not skew the numbers.
    """
    backends = [c.backend for c in cameras]
    sizes = [_frame_size(c, record_form) for c in cameras]
    accums = [_CamAccum() for _ in cameras]
    writers = [_make_writer(video_format, profile=True) for _ in cameras]

    with tempfile.TemporaryDirectory(prefix="octacam-bench-") as tmp:
        tmpdir = Path(tmp)
        ext = video_format.extension if video_format else "null"
        for camera, writer, size in zip(cameras, writers, sizes, strict=True):
            path = tmpdir / f"{camera.serial_number}.{ext}"
            writer.open(str(path), target_fps, size)

        for backend in backends:
            backend.begin_software_trigger_preview()
            backend.start_grab_record()

        stop = threading.Event()

        def grab(camera, backend, writer, accum, size) -> None:
            bake = record_form == "display" and not camera.display_transform.is_identity
            transform = camera.display_transform if bake else None
            while not stop.is_set() and backend.is_grabbing():
                t0 = time.perf_counter_ns()
                frame = backend.retrieve(GRAB_TIMEOUT_MS, _wants_array)
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

        timer = PreciseTimer(lambda: [b.trigger_once() for b in backends])
        timer.set_frequency(target_fps)
        jitter = _JitterProbe()
        proc = _cpu_percent_probe()

        for t in grab_threads:
            t.start()
        timer.start()
        jitter.start()

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

        window = time.perf_counter() - t_measure
        timer.stop()
        stop.set()
        for backend in backends:
            backend.stop_grab()  # wakes any retrieve parked in _wait_pending
        for t in grab_threads:
            t.join(timeout=GRAB_TIMEOUT_MS / 1000.0 + 1.0)
        for w in writers:
            w.close()
        jitter_p99 = jitter.stop()
        cpu = proc.cpu_percent() if proc is not None else None

    trials: list[CameraTrial] = []
    for i, camera in enumerate(cameras):
        grabbed = accums[i].grabbed - base_grabbed[i]
        dropped = accums[i].dropped - base_dropped[i]
        encoded = writers[i].frames_written - base_encode[i]
        stages = {
            "acquire": _stage_timing("acquire", accums[i].grab_ns[base_grab_ns[i] :]),
            "transform": _stage_timing(
                "transform", accums[i].transform_ns[base_transform_ns[i] :]
            ),
            "enqueue": _stage_timing(
                "enqueue", accums[i].enqueue_ns[base_enqueue_ns[i] :]
            ),
            "encode": _stage_timing(
                "encode", writers[i].encode_ns_samples[base_encode_ns[i] :]
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
        # encoded is informational (sink drain during the window); a large gap
        # between grabbed and encoded with drops signals encoder backpressure.
        log.debug(
            "trial %s: grabbed=%d encoded=%d dropped=%d",
            camera.serial_number,
            grabbed,
            encoded,
            dropped,
        )
    return TrialOutcome(trials=trials, jitter_p99_ms=jitter_p99, cpu_percent=cpu)


# ---------------------------------------------------------------------------
# Max-fps search (software trigger only)
# ---------------------------------------------------------------------------


def find_max_fps(
    probe: Callable[[float], bool],
    lo: float,
    hi: float,
    iterations: int = 4,
) -> float:
    """Bisect ``[lo, hi]`` for the highest fps that ``probe`` still passes.

    ``lo`` should be a known-passing rate and ``hi`` an upper bound (typically the
    predicted ceiling with a little headroom). Each ``probe(fps)`` runs a short
    end-to-end trial and returns whether it met the achieve/drop bars. Returns the
    best passing rate found (never below ``lo``).
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


# ---------------------------------------------------------------------------
# Verdict
# ---------------------------------------------------------------------------


def _classify(
    target_fps: float,
    ceilings: Ceilings,
    outcome: TrialOutcome,
) -> tuple[bool, str, list[str]]:
    """Return ``(achievable, bottleneck, recommendations)`` for the target fps."""
    achievable = outcome.passed
    grab_min = ceilings.grab_min
    encode_min = ceilings.encode_min
    recs: list[str] = []

    # The limiting isolated ceiling, with a small tolerance so a target sitting
    # right at a ceiling is attributed to that stage.
    if target_fps > grab_min * (1 + DROP_THRESHOLD) and grab_min <= encode_min:
        bottleneck = ACQUISITION
    elif target_fps > encode_min * (1 + DROP_THRESHOLD):
        bottleneck = ENCODE
    elif not achievable:
        # Both stages could sustain the target in isolation, yet the full system
        # falls short: the loss is contention (shared USB bus, CPU, or the GIL
        # across the per-camera grab/encode threads).
        bottleneck = HOST
    else:
        bottleneck = NONE

    if bottleneck == ACQUISITION:
        recs += [
            "Acquisition-bound: the camera cannot deliver frames fast enough "
            f"(≈{grab_min:.0f} fps ceiling). Shorten the exposure, raise "
            "DeviceLinkThroughputLimit, shrink the ROI, or use an external "
            "hardware trigger (which overlaps exposure with transfer).",
        ]
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


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------


def diagnose(
    system: CameraSystem,
    settings: RecordingSettings,
    *,
    target_fps: float | None = None,
    duration_s: float = 5.0,
    find_max: bool = True,
    sink: str = "config",
    progress_cb: ProgressCallback | None = None,
    cancel: threading.Event | None = None,
) -> DiagnosticReport:
    """Run the full diagnostic against an open :class:`CameraSystem`.

    ``target_fps`` defaults to the settings' fps. ``sink`` selects what the
    end-to-end and encode-ceiling scenarios write through: ``"config"`` uses the
    settings' real video format (the truthful encode cost), ``"null"`` discards
    frames (isolates acquisition + host overhead, skipping the encoder). ``find_max``
    bisects for the empirical maximum (software trigger only). ``progress_cb`` is
    called ``(phase, detail)`` at each stage so a CLI/GUI can show progress.

    The cameras must be open with their parameters loaded; the caller owns their
    lifecycle (this never opens or closes the system, and leaves every backend
    stopped, not grabbing).
    """
    cameras = list(system)
    target_fps = target_fps if target_fps is not None else settings.fps
    record_form = settings.record_form
    external = settings.trigger_source == "external"

    def emit(phase: str, detail: str = "") -> None:
        log.info("benchmark: %s %s", phase, detail)
        if progress_cb is not None:
            progress_cb(phase, detail)

    video_format = None if sink == "null" else settings.video_format()
    sizes = {c.serial_number: _frame_size(c, record_form) for c in cameras}

    report = DiagnosticReport(
        backend=system.backend,
        n_cameras=len(cameras),
        target_fps=target_fps,
        trigger_source=settings.trigger_source,
        save_method="null" if sink == "null" else settings.save_method,
        ffmpeg_params=settings.ffmpeg_params if video_format else "",
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

    # The diagnostic always drives the cameras with software triggers (it arms
    # the software FrameStart trigger for every scenario), so the ceilings and
    # trial measure the software-triggered pipeline capacity even on a rig that
    # records with an external hardware trigger. That is an honest *lower bound*
    # for an external rig (a hardware trigger overlaps exposure with transfer and
    # is usually faster), and the encode/host figures apply unchanged — but the
    # production frame rate of an external rig is set by the hardware source, not
    # by us, so the max-fps sweep is meaningless there and is skipped.
    if external:
        report.notes.append(
            "This rig records with an external hardware trigger. The benchmark "
            "drives the cameras with software triggers, so the acquisition ceiling "
            "below is a software-triggered lower bound (external triggering overlaps "
            "exposure with transfer and is usually faster); the encode ceiling and "
            "host capacity apply unchanged. The production frame rate is set by the "
            "external trigger source, so the max-fps search is skipped."
        )
        find_max = False

    emit("Measuring acquisition ceiling", f"({duration_s:g}s)")
    grab_fps = measure_grab_ceiling(cameras, duration_s, cancel=cancel)
    encode_fps: dict[str, float] = {}
    if video_format is not None and not _cancelled(cancel):
        emit("Measuring encode ceiling", f"({duration_s:g}s)")
        encode_fps = measure_encode_ceiling(
            video_format, sizes, target_fps, duration_s, cancel=cancel
        )
    ceilings = Ceilings(grab_fps=grab_fps, encode_fps=encode_fps)
    report.ceilings = ceilings

    # --- end-to-end at the target fps ---
    emit("Running end-to-end trial", f"@ {target_fps:g} fps ({duration_s:g}s)")
    outcome = run_target_trial(
        cameras, video_format, target_fps, duration_s, record_form, cancel=cancel
    )
    report.trials = outcome.trials
    report.achieved_fps = outcome.achieved_fps
    report.drop_rate = outcome.drop_rate
    report.jitter_p99_ms = outcome.jitter_p99_ms
    report.cpu_percent = outcome.cpu_percent

    # --- verdict ---
    report.achievable, report.bottleneck, report.recommendations = _classify(
        target_fps, ceilings, outcome
    )
    # With a null sink the encoder is not measured, so the predicted ceiling is
    # the acquisition ceiling alone; otherwise it is the slower of the two.
    encode_min = ceilings.encode_min if video_format is not None else float("inf")
    report.predicted_max_fps = min(ceilings.grab_min, encode_min)

    # --- empirical max-fps search ---
    if find_max and not _cancelled(cancel):
        predicted = report.predicted_max_fps
        hi = predicted * 1.1
        # A short probe trial (half the main duration, floored) keeps the search
        # bounded; lo is a rate we already know passes when the target did.
        probe_dur = max(1.5, duration_s / 2.0)
        lo = target_fps if outcome.passed else min(target_fps, predicted * 0.5)

        def probe(fps: float) -> bool:
            if _cancelled(cancel):
                return False
            emit("Searching max fps", f"probing {fps:.0f} fps")
            result = run_target_trial(
                cameras, video_format, fps, probe_dur, record_form, cancel=cancel
            )
            return result.passed

        report.measured_max_fps = find_max_fps(probe, lo, hi)

    if _cancelled(cancel):
        report.notes.append("Benchmark was cancelled before it finished.")

    return report
