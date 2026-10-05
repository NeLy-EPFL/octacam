"""Screen recordings for missed trigger pulses and desynchronized cameras
(``octacam check``; read-only).

* **Missed pulses.** A schema-4 recording carries the recorder's accounting:
  per frame in ``timestamps.npz``, else the summary's counts (never its index
  lists, which are capped), and its ``sync`` verdict is honored. An older one
  is re-derived from its hardware timestamps with the recorder's tracker
  (:func:`octacam.pulses.analyze_timestamps`); host-clock timestamps are not,
  as their delivery jitter would read as misses.
* **Unequal frame counts**, a problem only for a take that ran to its end.
* **A start offset** (frame 0 not the same pulse everywhere): a schema-4
  recording's own, never re-derived; an older one's from the trigger timing
  events every camera shares (:func:`octacam.pulses.estimate_offset`). An
  offset n means the camera's frame k shows the earliest camera's frame k+n.
* Late exposures, writer-queue fills and corrected clock jumps are noted.

A recording that cannot be read is reported as a problem, never raised, so a
scan still covers the others.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from octacam.pulses import TimestampReport, analyze_timestamps, estimate_offset
from octacam.recording_format import (
    RECORDING_SUMMARY_FILENAME,
    TIMESTAMPS_FILENAME,
    find_recordings,
    read_summary,
    recording_folder,
    recording_info_dir,
)

log = logging.getLogger("octacam")

# time.time_ns() has exceeded this since 2001 and a camera clock (from
# power-up) never will: tells host-clocked timestamps from hardware ones
# when the summary does not say.
_WALL_CLOCK_NS = 10**18


@dataclass
class CameraCheck:
    name: str
    frames: int
    # Pulses without a frame; the list may be the summary's capped one, so
    # missed_count is the total. Unfilled, missed_after_frame is the video frame
    # before each gap.
    missed: list[int] = field(default_factory=list)
    missed_count: int = 0
    missed_after_frame: list[int] = field(default_factory=list)
    filled: bool = False  # the recorder filled them (video frame k is pulse k)
    late: list[int] = field(default_factory=list)
    late_count: int = 0
    writer_dropped: int = 0  # frames the writer queue refused (filled)
    unclocked_frames: int = 0  # frames that had no hardware timestamp
    glitches: list[tuple] = field(default_factory=list)
    clock_mismatch: bool = False
    # "recorded" (timestamps.npz accounting), "summary" (its counts),
    # "timestamps" (re-derived, pre-schema-4) or "none" (nothing to check).
    source: str = "none"
    start_offset: int | None = None  # pulses after the earliest camera, if known

    def to_dict(self) -> dict:
        # The keys follow recording_summary.json's per-camera fields.
        return {
            "name": self.name,
            "frames": self.frames,
            "missed_pulses": self.missed_count,
            "missed_pulse_indices": self.missed,
            "missed_after_frame": self.missed_after_frame,
            "filled": self.filled,
            "late_frames": self.late_count,
            "late_pulse_indices": self.late,
            "writer_dropped": self.writer_dropped,
            "unclocked_frames": self.unclocked_frames,
            "timestamp_glitches": [list(g) for g in self.glitches],
            "clock_mismatch": self.clock_mismatch,
            "source": self.source,
            "start_offset_pulses": self.start_offset,
        }


@dataclass
class RecordingCheck:
    folder: Path
    schema: int | None
    fps: float | None
    cameras: list[CameraCheck]
    problems: list[str] = field(default_factory=list)  # break frame alignment
    warnings: list[str] = field(default_factory=list)  # data quality only

    @property
    def ok(self) -> bool:
        return not self.problems

    def to_dict(self) -> dict:
        return {
            "folder": str(self.folder),
            "schema": self.schema,
            "fps": self.fps,
            "ok": self.ok,
            "problems": self.problems,
            "warnings": self.warnings,
            "cameras": [c.to_dict() for c in self.cameras],
        }


def _natural_key(path: Path) -> list:
    return [int(t) if t.isdigit() else t for t in re.split(r"(\d+)", str(path))]


def check_recordings(paths, fps: float | None = None) -> list[RecordingCheck]:
    """Check every recording at or under *paths* (each may also name a summary
    file or an ``octacam_recording`` subfolder), in natural order."""
    folders = sorted(find_recordings(paths, recursive=True), key=_natural_key)
    return [check_recording(folder, fps) for folder in folders]


def _after_frame(missed: list[int]) -> list[int]:
    """Video frame index just before each missed pulse, when misses were not filled."""
    return [pulse - 1 - i for i, pulse in enumerate(missed)]


def _shown(pulses: list[int], total: int | None = None, limit: int = 8) -> str:
    """The first ``limit`` of ``pulses``, and how many of ``total`` are not shown."""
    total = len(pulses) if total is None else total
    shown = pulses[:limit]
    text = ", ".join(str(p) for p in shown)
    return text + (f" … (+{total - len(shown)})" if total > len(shown) else "")


def _error(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {exc}" if str(exc) else type(exc).__name__


def _count(meta: dict, count_key: str, list_key: str) -> int:
    """A schema-4 per-camera count; its capped index list stands in only when
    the count is absent."""
    value = meta.get(count_key)
    if value is None:
        return len(meta.get(list_key) or [])
    return len(value) if isinstance(value, list) else int(value)


def _read_arrays(path: Path) -> dict[str, np.ndarray]:
    # Every array is decompressed here, so a truncated or corrupt member fails
    # now rather than halfway through the checks.
    with np.load(path) as data:
        return {k: np.asarray(data[k]) for k in data.files}


def _unreadable(folder: Path, fps: float | None, what: str) -> RecordingCheck:
    result = RecordingCheck(folder, None, fps, [])
    result.problems.append(f"unreadable: {what}")
    return result


def check_recording(folder: str | Path, fps: float | None = None) -> RecordingCheck:
    """Check one recording folder (or its summary file or subfolder). A damaged
    recording is reported as an "unreadable: ..." problem, never raised."""
    folder = recording_folder(folder)
    try:
        summary = read_summary(folder)
    except Exception as e:
        return _unreadable(folder, fps, f"{RECORDING_SUMMARY_FILENAME} ({_error(e)})")
    try:
        return _check_summary(folder, summary, fps)
    except Exception as e:
        # Content the checks cannot interpret (a field of the wrong type...).
        log.debug("Could not check %s", folder, exc_info=True)
        return _unreadable(
            folder, fps, f"could not interpret the recording ({_error(e)})"
        )


def _check_summary(folder: Path, summary: dict, fps: float | None) -> RecordingCheck:
    schema = summary.get("schema_version")
    fps = fps or summary.get("fps_target")
    result = RecordingCheck(folder, schema, fps, [])
    # Beside the summary that was read, not an older flat take's.
    npz_path = recording_info_dir(folder) / TIMESTAMPS_FILENAME
    arrays: dict[str, np.ndarray] = {}
    npz_failed = False
    if npz_path.is_file():
        try:
            arrays = _read_arrays(npz_path)
        except Exception as e:
            npz_failed = True
            result.problems.append(f"unreadable: {TIMESTAMPS_FILENAME} ({_error(e)})")
    # Schema 4 onward the recorder accounted for every pulse itself.
    counted = isinstance(schema, int) and schema >= 4
    fill = (summary.get("pulse_train") or {}).get("fill")
    if fill is None:
        # Only an external trigger (a source octacam does not drive) is not filled.
        fill = summary.get("trigger_source") != "external"
    filled = bool(fill)
    reports: dict[str, TimestampReport] = {}
    series: dict[str, np.ndarray] = {}  # real frames' timestamps, for start offsets
    no_data: list[str] = []
    for meta in summary.get("cameras", []):
        name = meta.get("name", "?")
        ts = arrays.get(f"{name}/timestamp_ns")
        frames = int(meta.get("frames", 0) if ts is None else len(ts))
        cam = CameraCheck(name, frames)
        result.cameras.append(cam)
        if counted:
            _from_summary(cam, meta, filled)
            missed_flags = arrays.get(f"{name}/missed")
            pulse_index = arrays.get(f"{name}/pulse_index")
            if ts is not None and missed_flags is not None and pulse_index is not None:
                _from_accounting(cam, missed_flags, pulse_index)
                dropped = arrays.get(f"{name}/dropped")
                series[name] = ts if dropped is None else ts[~dropped.astype(bool)]
            continue
        if ts is None:
            no_data.append(name)
            continue
        host = _host_clocked(meta.get("timestamp_source"), ts)
        if host:
            result.warnings.append(
                f"{name}: {host}; their delivery jitter would read as missed "
                "pulses, so its missed pulses cannot be checked"
            )
            continue
        cam.source = "timestamps"
        report = analyze_timestamps(ts, fps)
        reports[name] = report
        cam.missed = report.missed
        cam.missed_count = len(report.missed)
        cam.missed_after_frame = _after_frame(report.missed)
        cam.late = report.late
        cam.late_count = len(report.late)
        cam.glitches = report.glitches
        cam.clock_mismatch = report.clock_mismatch
    if no_data and not npz_failed:
        result.warnings.append(
            f"no {TIMESTAMPS_FILENAME} data for {', '.join(no_data)}: missed pulses "
            "cannot be checked (enable record.save_timestamps)"
        )
    _cross_check(result, summary, reports, series)
    return result


def _from_summary(cam: CameraCheck, meta: dict, filled: bool) -> None:
    """The recorder's per-camera accounting as recording_summary.json holds it."""
    cam.source = "summary"
    cam.filled = filled
    cam.missed = [int(p) for p in meta.get("missed_pulse_indices") or []]
    cam.missed_count = _count(meta, "missed_pulses", "missed_pulse_indices")
    if not filled:
        cam.missed_after_frame = _after_frame(cam.missed)
    cam.late = [int(p) for p in meta.get("late_pulse_indices") or []]
    cam.late_count = _count(meta, "late_frames", "late_pulse_indices")
    cam.writer_dropped = int(meta.get("writer_dropped") or 0)
    cam.unclocked_frames = int(meta.get("unclocked_frames") or 0)
    cam.glitches = [
        (g.get("pulse"), g.get("kind"), g.get("jump_ns"))
        for g in meta.get("timestamp_glitches") or []
    ]
    cam.clock_mismatch = bool(meta.get("clock_mismatch", False))
    offset = meta.get("start_offset_pulses")
    cam.start_offset = None if offset is None else int(offset)


def _from_accounting(cam: CameraCheck, missed_flags, pulse_index) -> None:
    """The recorder's per-frame accounting from timestamps.npz: every missed
    pulse, where the summary's list stops at its cap."""
    cam.source = "recorded"
    pulse_index = np.asarray(pulse_index, dtype=np.int64)
    if cam.filled:
        missed = pulse_index[np.asarray(missed_flags, dtype=bool)]
    else:
        # Unfilled: the misses are the gaps in pulse_index (never decreasing).
        span = int(pulse_index.max()) + 1 if len(pulse_index) else 0
        missed = np.setdiff1d(np.arange(span), pulse_index)
        cam.missed_after_frame = [
            int(i) - 1 for i in np.searchsorted(pulse_index, missed)
        ]
    cam.missed = [int(p) for p in missed]
    cam.missed_count = len(cam.missed)


def _host_clocked(source, ts) -> str | None:
    """Why a pre-schema-4 camera's timestamps are host delivery times rather
    than a camera clock, or None when they are the camera's own."""
    if source == "host":
        return 'its timestamps are host delivery times (timestamp_source "host")'
    if source == "mixed":
        return (
            'some of its timestamps are host delivery times (timestamp_source "mixed")'
        )
    if source is None and len(ts) and float(np.median(ts)) >= _WALL_CLOCK_NS:
        return "its timestamps look like host wall-clock times, not a camera clock"
    return None


def _ended_early(summary: dict, cams: list[CameraCheck]) -> str | None:
    """Why the take may have ended its cameras on different pulses (it did not
    run to its end), or None."""
    if summary.get("aborted"):
        return "the recording was aborted"
    completed = summary.get("completed")
    if completed is False:
        return "the recording was stopped before its train ended"
    if completed is True:
        return None
    # An early schema-4 summary does not say: infer it from the frame counts.
    # Before schema 4 an unequal count may be the only trace of a start-up
    # race, so it stays a problem.
    schema = summary.get("schema_version")
    if not (isinstance(schema, int) and schema >= 4) or not cams:
        return None
    # A completed driven train pads every camera to its count (fill_to) and an
    # external take runs to its duration, so even the fullest camera short of
    # it means the take was stopped.
    expected = (summary.get("pulse_train") or {}).get("count")
    if expected is None:
        rate, duration = summary.get("fps_target"), summary.get("duration_s")
        expected = round(rate * duration) if rate and duration else None
    if expected and max(c.frames for c in cams) < expected:
        return "the recording was stopped before its train ended"
    return None


def _cross_check(
    result: RecordingCheck,
    summary: dict,
    reports: dict[str, TimestampReport],
    series: dict[str, np.ndarray],
) -> None:
    cams = result.cameras
    checked = [c for c in cams if c.source != "none"]
    counts = {c.name: c.frames for c in cams}
    if len(set(counts.values())) > 1:
        listed = ", ".join(f"{name} {n}" for name, n in counts.items())
        early = _ended_early(summary, cams)
        if early:
            result.warnings.append(
                f"unequal frame counts ({early}, so its cameras may end on "
                f"different pulses): {listed}"
            )
        else:
            result.problems.append(f"unequal frame counts: {listed}")
    for cam in checked:
        if cam.missed_count and cam.filled:
            result.warnings.append(
                f"{cam.name} missed {cam.missed_count} pulse(s) "
                f"({_shown(cam.missed, cam.missed_count)}), filled with the previous "
                "frame: frames stay aligned"
            )
        elif cam.missed_count:
            after = _shown(cam.missed_after_frame, cam.missed_count)
            result.problems.append(
                f"{cam.name} missed {cam.missed_count} pulse(s); its frames after "
                f"frame {after} show later moments than a camera that did not miss "
                f"them ({cam.missed_count} frame(s) behind by the end)"
            )
        if cam.clock_mismatch:
            result.problems.append(
                f"{cam.name}: frames did not follow the trigger period (free-running?)"
            )
        if cam.unclocked_frames:
            result.problems.append(
                f"{cam.name}: {cam.unclocked_frames} frame(s) had no hardware "
                "timestamp, so its missed pulses cannot be detected"
            )
        if cam.writer_dropped:
            result.warnings.append(
                f"{cam.name}: {cam.writer_dropped} frame(s) the writer queue could "
                "not accept, filled with the previous frame (the encoder could not "
                "keep up)"
            )
        if cam.late_count:
            result.warnings.append(
                f"{cam.name}: {cam.late_count} frame(s) exposed late for their pulse "
                f"(pulses {_shown(cam.late, cam.late_count, 5)})"
            )
        for pulse, kind, jump in cam.glitches:
            result.warnings.append(
                f"{cam.name}: hardware clock jump of {jump / 1e9:+.6f} s at pulse "
                f"{pulse} ({kind}; not a lost frame)"
            )
    _start_offsets(result, reports, series)
    sync = summary.get("sync")
    if isinstance(sync, dict) and sync.get("ok") is False:
        reasons = "; ".join(str(w) for w in sync.get("warnings") or [])
        result.problems.append(
            "the recorder's sync check failed" + (f": {reasons}" if reasons else "")
        )


def _start_offsets(
    result: RecordingCheck,
    reports: dict[str, TimestampReport],
    series: dict[str, np.ndarray],
) -> None:
    """Start alignment: the recorder's own verdict when it has one, else line the
    cameras up on shared trigger-timing events."""
    checked = [c for c in result.cameras if c.source != "none"]
    recorded = [
        c
        for c in checked
        if c.source in ("recorded", "summary") and c.start_offset is not None
    ]
    if recorded:
        for cam in recorded:
            if cam.start_offset:
                result.problems.append(
                    f"{cam.name} started {cam.start_offset} pulse(s) late: its frame "
                    f"k shows the other cameras' frame k+{cam.start_offset}"
                )
        return
    if isinstance(result.schema, int) and result.schema >= 4:
        # The recorder's pulse_index is the word: re-derived from timing events,
        # offsets come out of jitter.
        return
    cands = [c for c in checked if c.name in reports or c.name in series]
    if len(cands) < 2:
        return

    def report(name: str) -> TimestampReport:
        if name not in reports:
            reports[name] = analyze_timestamps(series[name], result.fps)
        return reports[name]

    ref = cands[0]
    # A camera that started after ``ref`` has a negative lag.
    after_ref: dict[str, int] = {ref.name: 0}
    events: dict[str, int] = {}
    for cam in cands[1:]:
        lag, n = estimate_offset(report(ref.name), report(cam.name))
        if lag is None:
            result.warnings.append(
                f"start alignment of {cam.name} vs {ref.name} undetermined "
                f"({n} shared timing event(s))"
            )
            continue
        after_ref[cam.name] = -lag
        events[cam.name] = n
    if len(after_ref) < 2:
        return
    earliest = min(after_ref, key=lambda name: after_ref[name])
    for cam in cands:
        if cam.name not in after_ref:
            continue
        cam.start_offset = after_ref[cam.name] - after_ref[earliest]
        if cam.start_offset:
            shared = events.get(cam.name, events.get(earliest))
            result.problems.append(
                f"{cam.name} is offset from {earliest} from the start: it started "
                f"{cam.start_offset} pulse(s) late, so its frame k shows the moment "
                f"of {earliest} frame k+{cam.start_offset} ({shared} shared timing "
                "events)"
            )
