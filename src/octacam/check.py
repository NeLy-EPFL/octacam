"""Screen recordings for missed trigger pulses and desynchronized cameras.

Backs ``octacam check``. A recording folder (one holding
``recording_summary.json``) is checked camera by camera from its
``timestamps.npz``:

* **Missed pulses.** For recordings made before octacam counted pulses (summary
  schema < 4) they are re-derived from the hardware timestamps with the same
  tracker the recorder now runs live (:func:`octacam.pulses.analyze_timestamps`):
  an interval of k periods is k-1 missed pulses. Each one shifts every later
  frame of that camera by one pulse against a camera that did not miss it. Newer
  recordings carry their own per-frame accounting, which is reported instead —
  there a missed pulse on an octacam-driven train was filled, so it no longer
  shifts anything.
* **Frame counts** that differ between cameras.
* **A start offset**: two cameras whose frame 0 is not the same pulse (the
  pre-fix start-up race). Timestamps carry no common clock, but the trigger
  source's own timing events (the start of a train, a late pulse) reach every
  camera at the same pulse, so lining those up reveals the offset when there are
  enough of them (:func:`octacam.pulses.estimate_offset`); newer recordings also
  record the recorder's own arrival-time check.
* Late exposures (e.g. a camera triggering on the pulse's falling edge) and
  corrected camera-clock jumps are noted.

Pure file reading; nothing here touches hardware or modifies a recording.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from octacam.pulses import TimestampReport, analyze_timestamps, estimate_offset
from octacam.transform import RECORDING_SUMMARY_FILENAME, TIMESTAMPS_FILENAME


@dataclass
class CameraCheck:
    name: str
    frames: int
    # Pulses with no frame of their own, and where they fall in the video: the
    # index of the video frame right before each gap.
    missed: list[int] = field(default_factory=list)
    missed_after_frame: list[int] = field(default_factory=list)
    filled: bool = False  # the recorder filled them (video frame k is pulse k)
    late: list[int] = field(default_factory=list)
    glitches: list[tuple] = field(default_factory=list)
    clock_mismatch: bool = False
    # "recorded" (the recorder's own accounting), "timestamps" (re-derived) or
    # "none" (no timestamps.npz: nothing to check)
    source: str = "none"
    start_offset: int | None = None  # pulses vs. the reference camera (None: unknown)

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "frames": self.frames,
            "missed_pulses": self.missed,
            "missed_after_frame": self.missed_after_frame,
            "filled": self.filled,
            "late_pulses": self.late,
            "timestamp_glitches": [list(g) for g in self.glitches],
            "clock_mismatch": self.clock_mismatch,
            "source": self.source,
            "start_offset": self.start_offset,
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


def find_recordings(paths) -> list[Path]:
    """Every recording folder at or under ``paths`` (sorted naturally)."""
    found: set[Path] = set()
    for raw in paths:
        path = Path(raw)
        if path.is_file() and path.name == RECORDING_SUMMARY_FILENAME:
            found.add(path.parent)
        elif (path / RECORDING_SUMMARY_FILENAME).is_file():
            found.add(path)
        elif path.is_dir():
            found.update(p.parent for p in path.rglob(RECORDING_SUMMARY_FILENAME))
    return sorted(found, key=_natural_key)


def _after_frame(missed: list[int]) -> list[int]:
    """Video frame index just before each missed pulse, when misses were not filled."""
    return [pulse - 1 - i for i, pulse in enumerate(missed)]


def _shown(pulses: list[int], limit: int = 8) -> str:
    text = ", ".join(str(p) for p in pulses[:limit])
    return text + (f" … (+{len(pulses) - limit})" if len(pulses) > limit else "")


def check_recording(folder: str | Path, fps: float | None = None) -> RecordingCheck:
    """Check one recording folder (see the module docstring)."""
    folder = Path(folder)
    summary = json.loads((folder / RECORDING_SUMMARY_FILENAME).read_text())
    schema = summary.get("schema_version")
    fps = fps or summary.get("fps_target")
    cams_meta = summary.get("cameras", [])
    npz_path = folder / TIMESTAMPS_FILENAME
    arrays: dict[str, np.ndarray] = {}
    if npz_path.is_file():
        with np.load(npz_path) as data:
            arrays = {k: data[k] for k in data.files}
    result = RecordingCheck(folder, schema, fps, [])
    reports: dict[str, TimestampReport] = {}
    for meta in cams_meta:
        name = meta.get("name", "?")
        ts = arrays.get(f"{name}/timestamp_ns")
        frames = int(meta.get("frames", 0) if ts is None else len(ts))
        cam = CameraCheck(name, frames)
        result.cameras.append(cam)
        if ts is None:
            continue
        dropped = arrays.get(f"{name}/dropped")
        missed_flags = arrays.get(f"{name}/missed")
        pulse_index = arrays.get(f"{name}/pulse_index")
        if missed_flags is not None and pulse_index is not None:
            # The recorder's own accounting (schema >= 4).
            cam.source = "recorded"
            cam.missed = [int(p) for p in pulse_index[missed_flags.astype(bool)]]
            train = summary.get("pulse_train") or {}
            cam.filled = bool(train.get("fill", True))
            if not cam.filled:
                cam.missed = [int(p) for p in meta.get("missed_pulse_indices", [])]
                cam.missed_after_frame = _after_frame(cam.missed)
            cam.late = [int(p) for p in meta.get("late_pulse_indices", [])]
            cam.glitches = [
                (g.get("pulse"), g.get("kind"), g.get("jump_ns"))
                for g in meta.get("timestamp_glitches", [])
            ]
            cam.clock_mismatch = bool(meta.get("clock_mismatch", False))
            cam.start_offset = meta.get("start_offset_pulses")
            real = ts if dropped is None else ts[~dropped.astype(bool)]
            reports[name] = analyze_timestamps(real, fps)
        else:
            cam.source = "timestamps"
            report = analyze_timestamps(ts, fps)
            reports[name] = report
            cam.missed = report.missed
            cam.missed_after_frame = _after_frame(report.missed)
            cam.late = report.late
            cam.glitches = report.glitches
            cam.clock_mismatch = report.clock_mismatch
    _cross_check(result, summary, reports)
    return result


def _cross_check(result: RecordingCheck, summary: dict, reports: dict) -> None:
    cams = result.cameras
    checked = [c for c in cams if c.source != "none"]
    if len(checked) < len(cams):
        missing = [c.name for c in cams if c.source == "none"]
        result.warnings.append(
            f"no {TIMESTAMPS_FILENAME} data for {', '.join(missing)}: missed pulses "
            "cannot be checked (enable record.save_timestamps)"
        )
    counts = {c.name: c.frames for c in cams}
    if len(set(counts.values())) > 1:
        result.problems.append(
            "unequal frame counts: "
            + ", ".join(f"{name} {n}" for name, n in counts.items())
        )
    for cam in checked:
        if cam.missed and cam.filled:
            result.warnings.append(
                f"{cam.name} missed {len(cam.missed)} pulse(s) ({_shown(cam.missed)}), "
                "filled with the previous frame: frames stay aligned"
            )
        elif cam.missed:
            result.problems.append(
                f"{cam.name} missed {len(cam.missed)} pulse(s); its frames after "
                f"frame {_shown(cam.missed_after_frame)} show later moments than a "
                f"camera that did not miss them ({len(cam.missed)} frame(s) behind "
                "by the end)"
            )
        if cam.clock_mismatch:
            result.problems.append(
                f"{cam.name}: frames did not follow the trigger period (free-running?)"
            )
        if cam.late:
            result.warnings.append(
                f"{cam.name}: {len(cam.late)} frame(s) exposed late for their pulse "
                f"(pulses {_shown(cam.late, 5)})"
            )
        for pulse, kind, jump in cam.glitches:
            result.warnings.append(
                f"{cam.name}: hardware clock jump of {jump / 1e9:+.6f} s at pulse "
                f"{pulse} ({kind}; not a lost frame)"
            )
    # Start alignment: the recorder's own verdict when it has one, else line the
    # cameras up on shared trigger-timing events.
    recorded = [c for c in checked if c.source == "recorded" and c.start_offset is not None]
    if recorded:
        for cam in recorded:
            if cam.start_offset:
                result.problems.append(
                    f"{cam.name} started {cam.start_offset} pulse(s) late: its frame "
                    f"k shows the other cameras' frame k+{cam.start_offset}"
                )
        return
    if len(checked) < 2:
        return
    ref = checked[0]
    for cam in checked[1:]:
        if ref.name not in reports or cam.name not in reports:
            continue
        lag, events = estimate_offset(reports[ref.name], reports[cam.name])
        cam.start_offset = lag
        if lag is None:
            result.warnings.append(
                f"start alignment of {cam.name} vs {ref.name} undetermined "
                f"({events} shared timing event(s))"
            )
        elif lag:
            result.problems.append(
                f"{cam.name} is offset from {ref.name} from the start: {cam.name} "
                f"frame k shows the moment of {ref.name} frame k{-lag:+d} "
                f"({events} shared timing events)"
            )
    if checked:
        ref.start_offset = 0
