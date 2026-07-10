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
end-to-end trials to find the empirical maximum.

The max search targets a **stable** rate, not the marginal pass/fail edge a plain
bisection converges to: it uses a stricter bar (a tighter drop budget plus a
queue-saturation guard — a queue climbing toward its bound is the earliest sign
the producer is outrunning the encoder) and confirms the candidate over a longer
window before reporting it (:attr:`TrialOutcome.stable_passed`).

Two acquisition ceilings are measured. The **software-trigger** ceiling
(:func:`measure_grab_ceiling`) runs exposure and transfer serially. The
**free-run** ceiling (:func:`measure_freerun_ceiling`) drives the camera in
continuous mode, which overlaps exposure with readout exactly as an external
hardware trigger does — so it is the honest acquisition ceiling for a
hardware-triggered rig, measurable on the bench with no external wiring. The
software max-fps *sweep* is still skipped for an external rig (its production rate
is set by the hardware source), but the free-run ceiling gives it a hardware max.
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

from octacam.transform import apply_display_transform
from octacam.trigger import PreciseTimer
from octacam.writer import AsyncFrameWriter, VideoFormat

if TYPE_CHECKING:
    from octacam.cameras.base import Camera
    from octacam.cameras.system import CameraSystem
    from octacam.controller import RecordingSettings

log = logging.getLogger("octacam")

GRAB_TIMEOUT_MS = 100  # matches Camera.GRAB_TIMEOUT_MS
WRITER_QUEUE_SIZE = 64  # matches Camera.WRITER_QUEUE_SIZE (record.writer_queue_size default)
WARMUP_S = 0.5  # discarded settle time before every measurement window
# A trial "passes" (target achievable) when it delivers at least this fraction of
# the requested fps with no more than this drop rate. The drop bar matches the
# capture-feasibility bar used by the Phase-0 benchmarks (sub-1% is encoder noise).
ACHIEVE_FRACTION = 0.97
DROP_THRESHOLD = 0.01

# Stricter bars for the *stable* max-fps search (see TrialOutcome.stable_passed).
# A bisection converges to the pass/fail boundary — the marginal, least
# reproducible rate — so the search uses tighter criteria and a longer
# confirmation trial to report a rate that actually holds on a re-run:
#  - a tenth of the "achievable" drop budget (0.1% vs 1%), and
#  - the writer queue must stay clear of saturation: a queue climbing toward its
#    bound is the *earliest* sign the producer is outrunning the encoder, and it
#    shows up before drops do, so a short probe that would otherwise "pass" is
#    rejected while it still has headroom.
STABLE_DROP_THRESHOLD = 0.001
QUEUE_SATURATION_FRACTION = 0.75  # reject once max depth reaches 75% of the bound
# If the bisection's short-probe candidate fails the longer confirmation window,
# report it reduced by this margin rather than the unconfirmed edge.
STABILITY_MARGIN = 0.05
# Bisection depth for the max-fps search (also used to size the progress plan).
FIND_MAX_ITERATIONS = 4

# Bottleneck labels (also the vocabulary the GUI/report render).
ACQUISITION = "acquisition"
TRANSFER = "transfer"
ENCODE = "encode"
HOST = "host"
NONE = "none"

# Transfer-vs-host contention: each camera is also measured *alone* (the solo
# acquisition ceiling). When the concurrent per-camera rate falls below this
# fraction of the solo rate, the cameras are slowing each other down — and since
# the acquisition sweep runs no encoder (the grab loops are light and the SDKs
# release the GIL on their blocking calls), that contention is the shared data
# transport (one USB3 controller sustains ~350-400 MB/s across all its cameras),
# not the CPU. That is the signal that separates a TRANSFER bottleneck from HOST.
CONTENTION_RATIO = 0.85
# A single USB3 host controller sustains roughly this much across its cameras;
# used only in the transfer-bound recommendation text, not in the detection.
USB3_BUS_MBPS = 384.0
# Ambient machine load above which the benchmark warns that other processes will
# skew the numbers and risk dropped frames in a real recording.
SYSTEM_CPU_WARN_PERCENT = 60.0
LOAD_PER_CORE_WARN = 0.7

# Progress phase labels — shared between the plan (which weights them by expected
# wall-clock) and the emit() calls, so a determinate progress bar can be driven
# from the fraction each Progress carries. Matched by exact string in _ProgressPlan.
PHASE_ACQUIRE = "Measuring acquisition ceiling"
PHASE_ACQUIRE_SOLO = "Measuring per-camera solo ceiling"
PHASE_FREERUN = "Measuring free-run ceiling"
PHASE_ENCODE = "Measuring encode ceiling"
PHASE_TRIAL = "Running end-to-end trial"
PHASE_FREERUN_TRIAL = "Running free-run trial"
PHASE_MAX = "Searching for the stable max fps"


@dataclass
class Progress:
    """One progress update: which phase, and how far through the whole run.

    ``fraction`` is the overall completion at the *start* of this phase and
    ``target`` at its *end*; a front-end animates the bar from ``fraction`` toward
    ``target`` over ``eta_s`` seconds (the phase's expected duration), giving a
    smooth determinate bar even though updates only arrive at phase boundaries.
    """

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
    """Weighted phase plan that turns a phase label into a :class:`Progress`.

    Built up front from the phases that will actually run (encode/free-run/max are
    conditional) and their expected wall-clock weights. ``step`` advances the
    cursor when the label changes. A phase that only reports once spans its whole
    weight in a single emit. A phase that reports many times (the max-fps search
    probes) passes ``advance=True``: each emit then eases *forward* within the
    phase span — closing a fixed fraction of the remaining gap — so the bar keeps
    creeping ahead across probes and never snaps back to the phase start, however
    many probes the search happens to run.
    """

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
        # A short eta so the final sliver (a phase can finish a touch under its
        # target) eases to 100% instead of snapping.
        return Progress("Done", "", 1.0, 1.0, 0.3)


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
    """Isolated per-camera acquisition and encode ceilings (fps).

    ``grab_fps`` is the *software*-triggered acquisition ceiling (exposure and
    transfer run serially). ``freerun_fps`` is the continuous / free-run ceiling
    where the camera overlaps exposure with readout exactly as an external
    hardware trigger does, so it is the honest acquisition ceiling for a
    hardware-triggered rig — measurable on the bench with no external wiring.
    """

    grab_fps: dict[str, float]
    encode_fps: dict[str, float]
    freerun_fps: dict[str, float] = field(default_factory=dict)
    # Per-camera acquisition ceiling measured with ONLY that camera grabbing (the
    # others idle). Comparing it against grab_fps (all cameras concurrent) reveals
    # shared-transport contention: see CONTENTION_RATIO. Empty for a single-camera
    # rig (where solo == concurrent) or when not measured.
    grab_solo_fps: dict[str, float] = field(default_factory=dict)

    @property
    def grab_min(self) -> float:
        return min(self.grab_fps.values()) if self.grab_fps else float("inf")

    @property
    def encode_min(self) -> float:
        return min(self.encode_fps.values()) if self.encode_fps else float("inf")

    @property
    def freerun_min(self) -> float:
        return min(self.freerun_fps.values()) if self.freerun_fps else float("inf")

    @property
    def grab_solo_min(self) -> float:
        return min(self.grab_solo_fps.values()) if self.grab_solo_fps else float("inf")

    @property
    def bus_contended(self) -> bool:
        """True when cameras throttle each other's acquisition (shared transport).

        Only meaningful with a solo measurement (≥2 cameras): the slowest
        concurrent rate has fallen below :data:`CONTENTION_RATIO` of the slowest
        solo rate, i.e. running them together costs bandwidth a single bus can't
        supply. Inert (False) for one camera or when the solo pass was skipped.
        """
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
    duration_s: float
    trials: list[CameraTrial]
    achieved_fps: float  # slowest camera at target (the synchronized rate)
    drop_rate: float  # worst camera's drop rate at target
    achievable: bool
    bottleneck: str
    ceilings: Ceilings | None = None
    predicted_max_fps: float = 0.0
    measured_max_fps: float | None = None  # STABLE software-trigger max (confirmed)
    max_confirmed: bool = False  # did measured_max hold the longer confirmation trial
    freerun_max_fps: float | None = None  # free-run acquisition ceiling (slowest cam)
    hardware_max_fps: float | None = (
        None  # external/free-run system max (measured free-run trial, or min(freerun, encode))
    )
    transfer_bound: bool = False  # is shared bus bandwidth the limiting factor?
    # Per-camera and aggregate acquisition throughput at the concurrent grab
    # ceiling (MB/s) — the "transfer" dimension, derived from frame size × fps.
    throughput_mbps: dict[str, float] = field(default_factory=dict)
    throughput_mbps_total: float = 0.0
    # Measured free-run end-to-end trial (the real free-run/external pipeline,
    # encoder included) — an honest hardware max rather than min(ceilings).
    freerun_trials: list[CameraTrial] = field(default_factory=list)
    # Ambient machine load sampled BEFORE the benchmark, so it reflects OTHER
    # processes (not our own): system-wide CPU% and 1-min load average per core.
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
            "max_confirmed": self.max_confirmed,
            "freerun_max_fps": (
                _finite(self.freerun_max_fps)
                if self.freerun_max_fps is not None
                else None
            ),
            "hardware_max_fps": (
                _finite(self.hardware_max_fps)
                if self.hardware_max_fps is not None
                else None
            ),
            "transfer_bound": self.transfer_bound,
            "throughput_mbps": {
                s: _finite(v) for s, v in self.throughput_mbps.items()
            },
            "throughput_mbps_total": _finite(self.throughput_mbps_total),
            "freerun_trials": [t.to_dict() for t in self.freerun_trials],
            "system_cpu_percent": (
                round(self.system_cpu_percent, 1)
                if self.system_cpu_percent is not None
                else None
            ),
            "load_per_core": (
                round(self.load_per_core, 2) if self.load_per_core is not None else None
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
# Jitter / CPU probes
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
        try:
            # Arm inside the try so stop_grab() always runs (harmless if the arm
            # never completed). A BackendError here (e.g. a camera dropped off the
            # bus) must not silently drop the camera from `results` — that would
            # make grab_min improve over the survivors — so the runner detects the
            # missing serial and notes it.
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
    # Each thread does its own warmup then measures; let all run warmup + window.
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
    """Per-camera acquisition ceiling with only that one camera grabbing.

    Runs :func:`measure_grab_ceiling` on each camera **alone** (the others idle),
    in turn. Comparing the result against the all-cameras-concurrent ceiling shows
    how much the cameras throttle each other — the signal that separates shared
    data-transport (USB-bus) contention from a per-camera intrinsic limit (see
    :meth:`Ceilings.bus_contended`). Only worth running for ≥2 cameras.
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
    """Per-camera and aggregate acquisition throughput (MB/s) at the grab ceiling.

    Mono8 is 1 byte/pixel (``Camera.pixel_format``), so bytes/frame = width×height.
    This is the sustained bus bandwidth each camera pulls at its concurrent
    acquisition ceiling — the "transfer" dimension. Transfer is not separately
    timeable from exposure through the vendor SDK, so it is derived from frame
    size × delivered fps (the same quantity Basler's Bandwidth Manager reports).
    """
    per_cam: dict[str, float] = {}
    for serial, fps in grab_fps.items():
        width, height = sizes.get(serial, (0, 0))
        per_cam[serial] = (width * height * fps) / 1e6
    return per_cam, sum(per_cam.values())


def _probe_system_load() -> tuple[float | None, float | None]:
    """Ambient system-wide CPU% and 1-min load average per core.

    Sampled once, up front (before the benchmark's own load starts), so it
    reflects *other* processes competing for the machine — which would skew the
    measurements and risk dropped frames in a real recording. Either element is
    ``None`` when unavailable (no psutil / no ``getloadavg``). Best-effort: any
    failure returns ``None`` rather than disturbing the benchmark.
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
    except (OSError, AttributeError):  # getloadavg is Unix-only
        load = None
    return cpu, load


# ---------------------------------------------------------------------------
# Scenario 1b: free-run ceiling (external-trigger-equivalent acquisition ceiling)
# ---------------------------------------------------------------------------


def measure_freerun_ceiling(
    cameras: list[Camera],
    duration_s: float,
    warmup_s: float = WARMUP_S,
    cancel: threading.Event | None = None,
) -> tuple[dict[str, float], list[str]]:
    """Max continuous / free-run acquisition fps per camera (no software trigger).

    Puts each camera into free-run (``TriggerMode Off``, continuous) via the
    backend's optional ``begin_freerun`` seam and pulls frames as fast as the
    camera pushes them with ``retrieve_freerun`` — no per-frame trigger. Because
    free-run overlaps exposure with readout the same way an external hardware
    trigger does, the delivered rate is the honest acquisition ceiling for a
    hardware-triggered rig, measured with no external wiring.

    Returns ``({serial: fps}, notes)``. A camera whose backend does not support
    free-run (or fails to arm it) is skipped and named in ``notes`` rather than
    failing the whole benchmark; the record/preview paths are never touched.
    """
    results: dict[str, tuple[int, float]] = {}
    unsupported: list[str] = []
    stop = threading.Event()

    def loop(camera: Camera) -> None:
        backend = camera.backend
        begin = getattr(backend, "begin_freerun", None)
        fetch = getattr(backend, "retrieve_freerun", None)
        if begin is None or fetch is None:
            unsupported.append(camera.name)
            return
        try:
            if not begin():
                unsupported.append(camera.name)
                return
        except Exception:
            log.debug("free-run arm failed on %s", camera.serial_number, exc_info=True)
            unsupported.append(camera.name)
            return
        try:
            # start_grab_record inside the try so stop_grab() runs on failure and
            # a raise here (grab could not start) routes into the unsupported/notes
            # path rather than dropping the camera from both results and notes or
            # leaving it armed-but-not-grabbing.
            backend.start_grab_record()  # all-frames buffering, like a recording
            warm_deadline = time.perf_counter() + warmup_s
            while time.perf_counter() < warm_deadline and not stop.is_set():
                fetch(GRAB_TIMEOUT_MS, _wants_array)
            grabbed = 0
            t0 = time.perf_counter()
            while not stop.is_set():
                if fetch(GRAB_TIMEOUT_MS, _wants_array) is not None:
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
        """Strict bar for the max-fps search: passes *and* leaves headroom.

        Adds a tighter drop budget and a queue-saturation guard on top of
        :attr:`passed`, so the search converges on a rate that is sustainable
        rather than one balanced on the pass/fail edge. The queue guard is inert
        for the null sink (no encoder → the queue never fills), which is correct:
        an acquisition-only run has nothing to back up.
        """
        if not self.passed:
            return False
        guard = WRITER_QUEUE_SIZE * QUEUE_SATURATION_FRACTION
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
) -> TrialOutcome:
    """Run the full instrumented pipeline at ``target_fps`` for ``duration_s``.

    One shared :class:`PreciseTimer` fires ``trigger_once()`` on every camera at
    the target rate; each camera's own grab thread runs the real
    ``retrieve → transform → write`` chain into a profiled writer (or the null
    sink when ``video_format`` is None). Per-stage timings and drops are measured
    only over the window *after* a short warmup, so encoder start-up and settle
    do not skew the numbers.

    With ``free_run=True`` the shared trigger timer is dropped and each camera is
    put into continuous free-run (``begin_freerun`` / ``retrieve_freerun``): the
    cameras clock themselves at their overlapped exposure+transfer ceiling, so
    the trial measures the real free-run/external-trigger record pipeline —
    including encoder contention with no software-trigger back-pressure —
    rather than the isolated ceilings. ``target_fps`` is then only the writer's
    nominal container rate.
    """
    backends = [c.backend for c in cameras]
    sizes = [_frame_size(c, record_form) for c in cameras]
    accums = [_CamAccum() for _ in cameras]
    writers = [_make_writer(video_format, profile=True) for _ in cameras]

    with tempfile.TemporaryDirectory(prefix="octacam-bench-") as tmp:
        tmpdir = Path(tmp)
        ext = video_format.extension if video_format else "null"

        # Initialized before the try so the finally can tear everything down even
        # if an arm/open raises before they are built.
        stop = threading.Event()
        timer: PreciseTimer | None = None
        grab_threads: list[threading.Thread] = []
        jitter = _JitterProbe()
        jitter_started = False
        proc = _cpu_percent_probe()
        jitter_p99: float | None = None
        cpu: float | None = None
        # Snapshotted at the instant the measurement window closes (before the
        # producers are stopped), so the achieved-fps numerator spans exactly the
        # window rather than the longer stop+join interval.
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
                    # A failed encoder open (ffmpeg missing, bad params) leaves the
                    # writer not running, so every write() would count as a drop
                    # and the trial would misreport ~100% ENCODE bottleneck. Abort
                    # with a clear setup error instead; the finally closes any
                    # writers already opened.
                    raise RuntimeError(
                        f"writer failed to open for {camera.serial_number}"
                    )

            for backend in backends:
                if free_run:
                    # Best-effort: a backend that cannot arm free-run just delivers
                    # no frames below (retrieve_freerun times out) — a low-fps row,
                    # never a crash. The orchestrator only runs the free-run trial
                    # once the free-run ceiling proved the mode works.
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

            # Free-run has no software trigger: the cameras self-clock
            # continuously, so there is no timer to fire trigger_once().
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

            # Close the window and snapshot the counters at the SAME instant, while
            # the producers are still running, so the grabbed/dropped numerator
            # matches the `window` denominator (before the ~1-frame stop+join tail).
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
                # wakes any retrieve parked in _wait_pending; harmless if the
                # backend never started grabbing.
                with contextlib.suppress(Exception):
                    backend.stop_grab()
            for t in grab_threads:
                t.join(timeout=GRAB_TIMEOUT_MS / 1000.0 + 1.0)
            for w in writers:
                w.close()  # idempotent: early-returns when the writer never opened
            # Only stop the jitter probe if it was actually started (its stop()
            # joins its thread, which would raise on a never-started thread when
            # an arm/open failed before the probe launched).
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


def _reconcile_stable_max(
    candidate: float, confirm: TrialOutcome, lo: float, ceiling_cap: float
) -> tuple[float, bool]:
    """Reported stable max (fps) + confirmed flag from the winning target's trial.

    The search returns the highest *target* fps that still passed, but a trial
    passes at :data:`ACHIEVE_FRACTION` (97%) of its target — so the winning target
    can sit a few percent above the rate the pipeline actually delivered, which
    would read as a "stable max" *above* the acquisition ceiling (you cannot record
    faster than you acquire). Report the **sustained achieved** rate instead, and
    cap it at the isolated ceilings (``ceiling_cap = min(grab, encode)``): a rate
    neither the camera nor the encoder can sustain in isolation is not a stable
    maximum. This keeps the reported max internally consistent with the ceiling
    line. Returns ``(fps, confirmed)``.
    """
    if confirm.stable_passed:
        return min(candidate, confirm.achieved_fps, ceiling_cap), True
    backed_off = max(lo, candidate * (1 - STABILITY_MARGIN))
    return min(backed_off, confirm.achieved_fps, ceiling_cap), False


# ---------------------------------------------------------------------------
# Verdict
# ---------------------------------------------------------------------------


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

    # When the acquisition ceiling is limited *and* the cameras throttle each
    # other (concurrent << solo, measured with no encoder running), the wall is
    # the shared data transport (one USB3 controller's bandwidth), not a
    # per-camera limit — report that as TRANSFER rather than ACQUISITION/HOST.
    bus_contended = ceilings.bus_contended

    # The limiting isolated ceiling, with a small tolerance so a target sitting
    # right at a ceiling is attributed to that stage.
    if target_fps > grab_min * (1 + DROP_THRESHOLD) and grab_min <= encode_min:
        bottleneck = TRANSFER if bus_contended else ACQUISITION
    elif target_fps > encode_min * (1 + DROP_THRESHOLD):
        bottleneck = ENCODE
    elif not achievable:
        # Both stages could sustain the target in isolation, yet the full system
        # falls short: the loss is contention — the shared USB bus (transfer) if
        # the cameras throttle each other, otherwise CPU/GIL across the
        # per-camera grab/encode threads (host).
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
    measure_freerun: bool = True,
    sink: str = "config",
    progress_cb: ProgressCallback | None = None,
    cancel: threading.Event | None = None,
) -> DiagnosticReport:
    """Run the full diagnostic against an open :class:`CameraSystem`.

    ``target_fps`` defaults to the settings' fps. ``sink`` selects what the
    end-to-end and encode-ceiling scenarios write through: ``"config"`` uses the
    settings' real video format (the truthful encode cost), ``"null"`` discards
    frames (isolates acquisition + host overhead, skipping the encoder). ``find_max``
    bisects for the *stable* software-trigger maximum (confirmed over a longer
    window). ``measure_freerun`` also measures the free-run acquisition ceiling —
    the external-hardware-trigger-equivalent rate — with no external wiring.
    ``progress_cb`` receives a :class:`Progress` at each stage so a CLI/GUI can
    show a determinate progress bar.

    The cameras must be open with their parameters loaded; the caller owns their
    lifecycle (this never opens or closes the system, and leaves every backend
    stopped, not grabbing).
    """
    cameras = list(system)
    target_fps = target_fps if target_fps is not None else settings.fps
    record_form = settings.record_form
    external = settings.trigger_source == "external"

    video_format = None if sink == "null" else settings.video_format()
    sizes = {c.serial_number: _frame_size(c, record_form) for c in cameras}

    run_freerun = measure_freerun
    run_encode = video_format is not None
    run_search = find_max and not external
    # Solo per-camera ceiling: only meaningful with ≥2 cameras (solo == concurrent
    # for one), and it's what tells a transfer-bound rig from a host-bound one.
    run_solo = len(cameras) >= 2
    solo_warmup = 0.3
    solo_dur = max(1.0, duration_s / 2.0)
    # A real free-run end-to-end trial (encoder in the loop) upgrades the hardware
    # max from a min-of-ceilings estimate to a measured rate. Pointless with a null
    # sink (no encoder to contend) and predicted here (skipped at run time if
    # free-run turns out unsupported on this rig).
    run_freerun_trial = run_freerun and run_encode

    # Weighted progress plan: each phase's expected wall-clock drives a determinate
    # bar. The max search is sized for its worst case (a full bisection + the
    # confirmation trial); it completes the bar early if it converges sooner.
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

    # The diagnostic drives the cameras with software triggers for the grab
    # ceiling, encode ceiling and end-to-end trial, so those measure the
    # software-triggered pipeline capacity even on a rig that records with an
    # external hardware trigger. The free-run ceiling is the honest
    # external-trigger-equivalent acquisition rate (free-run overlaps exposure
    # with transfer the same way), so a hardware max can still be reported. The
    # *software* max-fps sweep is skipped for external rigs (their production rate
    # is set by the hardware source, not by us).
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

    # Sample ambient machine load *before* the benchmark loads the CPU itself, so
    # it reflects other processes competing for the machine.
    report.system_cpu_percent, report.load_per_core = _probe_system_load()

    emit(PHASE_ACQUIRE, f"({duration_s:g}s)")
    grab_fps = measure_grab_ceiling(cameras, duration_s, cancel=cancel)
    # A camera that failed to arm is absent from grab_fps, so grab_min would be
    # taken over the survivors only (an over-optimistic ceiling). Flag any missing
    # so the number is not silently improved by a broken camera dropping out.
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
            video_format, sizes, target_fps, duration_s, cancel=cancel
        )
    ceilings = Ceilings(
        grab_fps=grab_fps,
        encode_fps=encode_fps,
        freerun_fps=freerun_fps,
        grab_solo_fps=grab_solo_fps,
    )
    report.ceilings = ceilings

    # Derived acquisition throughput (MB/s) at the concurrent grab ceiling — the
    # "transfer" dimension. Used both for the report and (its total) the
    # transfer-bound recommendation.
    report.throughput_mbps, report.throughput_mbps_total = _throughput_mbps(
        grab_fps, sizes
    )

    # --- end-to-end at the target fps ---
    emit(PHASE_TRIAL, f"@ {target_fps:g} fps ({duration_s:g}s)")
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
        target_fps, ceilings, outcome, report.throughput_mbps_total
    )
    report.transfer_bound = report.bottleneck == TRANSFER
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
    # With a null sink the encoder is not measured, so the predicted ceiling is
    # the acquisition ceiling alone; otherwise it is the slower of the two.
    encode_min = ceilings.encode_min if video_format is not None else float("inf")
    report.predicted_max_fps = min(ceilings.grab_min, encode_min)

    # Hardware (external / free-run) system max: the free-run acquisition ceiling,
    # capped by the encoder when one is measured (a measured free-run trial below
    # refines this).
    if freerun_fps:
        report.freerun_max_fps = ceilings.freerun_min
        report.hardware_max_fps = min(ceilings.freerun_min, encode_min)

    # --- measured free-run end-to-end trial (real free-run/external pipeline) ---
    # Drives the actual free-run path with the encoder in the loop, so the
    # hardware max becomes a number that reflects encoder contention and the
    # absence of software-trigger back-pressure, not just min(ceilings).
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
        )
        report.freerun_trials = fr.trials
        if fr.trials:
            # The slowest camera's sustained *written* rate (acquired minus drops)
            # is the honest hardware system max — a synchronized external trigger
            # cannot outrun the slowest camera without dropping frames.
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
                cameras, video_format, fps, probe_dur, record_form, cancel=cancel
            )
            return result.stable_passed

        candidate = find_max_fps(probe, lo, hi, iterations=FIND_MAX_ITERATIONS)
        # Confirm over the full (longer) window: a short probe can miss queue
        # buildup that only manifests after several seconds, so the bisection's
        # marginal edge is retested before it is reported.
        if _cancelled(cancel):
            report.measured_max_fps = min(candidate, ceiling_cap)
        else:
            emit(PHASE_MAX, f"confirming {candidate:.0f} fps", advance=True, eta=window)
            confirm = run_target_trial(
                cameras, video_format, candidate, duration_s, record_form, cancel=cancel
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
