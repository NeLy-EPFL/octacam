"""Behavior <-> 2P alignment from the trigger board alone, for a take ThorSync did
not record (ThorImage's ThorSync box unticked: real 2026-10-20, a whole day).

The board times both halves on one clock. Its camera pulses come from its own
timer at the recording's fps, started on the first 2P frame's rising edge, and
its end-of-run report (``twophoton.json`` ``stop``) counts the 2P frame pulses
and times the run in its ``millis()``. The 2P frame period follows:

    period = (run_ms - idle_ms - high_ms / edges) / (edges - 1)

(the run ends ``idle_ms`` after the input last went LOW, which is one HIGH
time past the last frame's start). Checked against ThorSync on 2026-10-16:
0.1 camera frame off by the end of a 5000-frame take.

A capture stopped before ThorImage completed it has a longer, irregular end
(up to 250 frames off), so such a take borrows the period of completed ones
with the same ThorImage frame rate (the scanner's period drifted by 0.5 camera
frames per 5000 frames over a day); without any, ThorImage's nominal
frameRate (~0.1 % off: 0.4 s by the end of 5000 frames).

The first camera pulse fires half a camera period after the first 2P frame. A
board running an older build (before 2026-10-21) carried its pin toggle over
from the previous run, which made it half or a whole period: such a take's
start is known to within a quarter period (2.5 ms at 100 fps). The plugin notes
a current build in ``twophoton.json`` (``camera_phase``).

A board too old to report (before 2026-10-14) leaves no count nor run time.
Its start rule is the same, so such a take is modeled from ThorImage instead:
its saved scans, at its nominal frameRate times this rig's factor for that
scan configuration (pixelX x pixelY): the board's clock against ThorImage's
nominal rate, measured on takes that have a report or a ThorSync recording
(1.00111 at 1024x1024 / 7.7 Hz, 1.00121 at 1024x512 / 15.3 Hz, each stable to
about 1e-5 over weeks).

``twophoton_board_sync.json`` (the take's info folder) holds the model; the
synced render and scapepp read it in place of ThorSync's edges.
"""

from __future__ import annotations

import json
import logging
import statistics
import xml.etree.ElementTree as ET
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from octacam.plugins.twophoton.layout import (
    MATCH_FILENAME,
    TAKE_INFO_FILENAME,
    TWOPHOTON_DIRNAME,
    info_file,
)

log = logging.getLogger("octacam")

BOARD_SYNC_FILENAME = "twophoton_board_sync.json"
BOARD_SYNC_VERSION = 1

# The board: an Arduino Mega 2560 (16 MHz) and its Timer1 prescalers, finest
# first, as 2photon_trigger.ino picks them.
_F_CPU = 16_000_000
_PRESCALERS = (1, 8, 64, 256, 1024)
# Where the first camera pulse lands, in camera periods after the first 2P frame
# (module doc), and how far off that may be: an older build's 0.5 or 1.0, or
# the current build's 0.5 behind the loop's edge poll (tens of us).
_FIRST_PULSE_PERIODS = 0.75
_FIRST_PULSE_UNCERTAINTY_PERIODS = 0.25
_HALF_PERIOD_PHASE = "half_period"
_FIRST_PULSE_POLL_MS = 0.1
# Completed captures whose period is shared must agree with ThorImage's frame
# rate to this much (a different scan configuration has another period).
_SAME_RATE_REL = 1e-4


def camera_period_ms(fps: float) -> float:
    """The board's camera-pulse period at *fps* (its clock's ms): Timer1 toggles
    the pin every `ticks` counts, at the finest prescaler that fits 16 bits.
    """
    fps_i = max(1, round(fps))
    for divisor in _PRESCALERS:
        ticks = _F_CPU // (divisor * 2 * fps_i)
        if 1 <= ticks <= 65536:
            return 2 * ticks * divisor * 1000 / _F_CPU
    return 2 * 65536 * 1024 * 1000 / _F_CPU


def run_period_ms(stop: dict, idle_ms: int) -> float | None:
    """The 2P frame period measured over one run (module doc), or None for a
    run too short to tell.
    """
    edges = int(stop.get("edges", 0))
    if edges < 2:
        return None
    return (stop["run_ms"] - idle_ms - stop["high_ms"] / edges) / (edges - 1)


def _nominal_rate(experiment_xml: Path) -> float | None:
    """ThorImage's ``<LSM frameRate>`` (Hz), or None."""
    try:
        lsm = ET.parse(experiment_xml).getroot().find("LSM")
        return float(lsm.get("frameRate", "")) if lsm is not None else None
    except ET.ParseError, OSError, ValueError:
        return None


def _scan_config(experiment_xml: Path) -> str | None:
    """The scan configuration a rig factor belongs to: ``"1024x512"``."""
    try:
        lsm = ET.parse(experiment_xml).getroot().find("LSM")
    except ET.ParseError, OSError:
        return None
    if lsm is None or not lsm.get("pixelX") or not lsm.get("pixelY"):
        return None
    return f"{lsm.get('pixelX')}x{lsm.get('pixelY')}"


@dataclass(frozen=True)
class BoardTake:
    """One transferred take's board facts."""

    take: Path
    # The board's end-of-run report; None from a board too old to send one.
    stop: dict | None
    idle_ms: int
    fps: float
    camera_pulses: int
    experiment_xml: Path
    nominal_hz: float | None
    has_thorsync: bool
    # The board's build starts the camera pulses half a period in (module doc).
    half_period_phase: bool = False
    scan_config: str | None = None
    # The paired SyncData folder, when there is one (a rig-factor reference).
    sync_dir: Path | None = None

    @property
    def completed(self) -> bool:
        """ThorImage ran the capture to its end and the board saw every pulse
        of it: the run's own period is exact.
        """
        from octacam.plugins.twophoton.matching import (
            thorimage_expected_frameout_edges,
        )

        expected = thorimage_expected_frameout_edges(self.experiment_xml)
        return (
            self.stop is not None
            and self.stop.get("reason") == "thorimage"
            and expected is not None
            and self.stop.get("edges") == expected
        )


def read_board_take(take: Path) -> BoardTake | None:
    """*take*'s board facts (a transferred take: its summary, its paired
    ThorImage folder and, from a board that sends one, the end-of-run report in
    twophoton.json), or None.
    """
    from octacam.recording_format import read_summary

    try:
        summary = read_summary(take)
    except OSError, ValueError:
        return None
    try:
        info = json.loads(info_file(take, TAKE_INFO_FILENAME).read_text())
    except OSError, ValueError:
        info = {}  # an older take: no plugin facts
    stop = info.get("stop")
    if not isinstance(stop, dict) or not {"edges", "high_ms", "run_ms"} <= set(stop):
        stop = None
    try:
        record = json.loads(info_file(take, MATCH_FILENAME).read_text())
        kinds = {
            m.get("kind"): Path(m.get("path", "")).name
            for m in record.get("matched", [])
        }
    except OSError, ValueError:
        kinds = {}
    twop = take / TWOPHOTON_DIRNAME
    if not kinds and twop.is_dir():
        # An older take with no match record: what was copied into it.
        for d in sorted(twop.iterdir()):
            if (d / "Experiment.xml").is_file():
                kinds.setdefault("image", d.name)
            elif d.name.startswith("SyncData"):
                kinds.setdefault("sync", d.name)
    if "image" not in kinds:
        return None
    xml = twop / kinds["image"] / "Experiment.xml"
    if not xml.is_file():
        return None
    fps = summary.get("fps_target") or (summary.get("pulse_train") or {}).get("fps")
    cameras = summary.get("cameras") or []
    pulses = max(
        ((c.get("frames") or 0) + (c.get("missed_pulses") or 0) for c in cameras),
        default=0,
    )
    if not fps or not pulses:
        return None
    sync_dir = twop / kinds["sync"] if "sync" in kinds else None
    return BoardTake(
        take=take,
        stop=stop,
        idle_ms=int(info.get("stop_idle_ms") or 0),
        fps=float(fps),
        camera_pulses=pulses,
        experiment_xml=xml,
        nominal_hz=_nominal_rate(xml),
        has_thorsync=sync_dir is not None,
        half_period_phase=info.get("camera_phase") == _HALF_PERIOD_PHASE,
        scan_config=_scan_config(xml),
        sync_dir=sync_dir if sync_dir is not None and sync_dir.is_dir() else None,
    )


def _saved_scans(take: BoardTake) -> int | None:
    """The 2P pulses of a take whose board did not count them: every scan of a
    capture ThorImage completed, else the scans of the frames it saved.
    """
    from octacam.plugins.twophoton.matching import (
        _channel_frames,
        thorimage_expected_frameout_edges,
        thorimage_frame_average,
        thorimage_timepoints,
    )

    xml = take.experiment_xml
    declared = thorimage_timepoints(xml)
    frames = _channel_frames(xml.parent)
    if declared and frames is not None and frames >= declared:
        return thorimage_expected_frameout_edges(xml)
    if frames:
        return frames * thorimage_frame_average(xml)
    return thorimage_expected_frameout_edges(xml)


def rig_factor(take: BoardTake) -> float | None:
    """This rig's board-clock 2P period over ThorImage's nominal one, measured
    on *take*: from its board report when its capture completed, else from its
    ThorSync recording. None when it measures nothing.
    """
    if not take.nominal_hz:
        return None
    nominal_ms = 1000 / take.nominal_hz
    if take.completed:
        assert take.stop is not None
        period = run_period_ms(take.stop, take.idle_ms)
        return period / nominal_ms if period else None
    if take.sync_dir is None:
        return None
    from octacam.plugins.twophoton.signals import (
        read_capture_segments,
        read_segment_edge_times,
    )

    segments = read_capture_segments(take.sync_dir) or []
    if not segments:
        return None
    segment = max(segments, key=lambda s: s.frameout_edges)
    edges = read_segment_edge_times(take.sync_dir, segment)
    if edges is None:
        return None
    cameras, frames = edges
    if len(cameras) < 100 or len(frames) < 50:
        return None
    # The 2P period in camera periods, which are the board's camera_period_ms.
    ratio = ((frames[-1] - frames[0]) / (len(frames) - 1)) / (
        (cameras[-1] - cameras[0]) / (len(cameras) - 1)
    )
    return ratio * camera_period_ms(take.fps) / nominal_ms


def _same_rate(a: float | None, b: float | None) -> bool:
    return a is not None and b is not None and abs(a - b) <= _SAME_RATE_REL * a


def board_sync(
    take: BoardTake,
    references: list[BoardTake],
    factors: dict[Path, float | None] | None = None,
) -> dict | None:
    """The board-clock model for *take* (module doc). With the board's report:
    its own period when its capture completed, else the median of the
    completed *references* with the same ThorImage frame rate, else
    ThorImage's nominal rate. Without one: its saved scans at the nominal rate
    times the rig factor the *references* measure for its scan configuration
    (*factors* caches `rig_factor` per take). None when there is not enough.
    """
    if take.stop is None:
        model = _unreported(take, references, {} if factors is None else factors)
        if model is None:
            return None
        pulses, period, source = model
    else:
        pulses = int(take.stop["edges"])
        period = run_period_ms(take.stop, take.idle_ms) if take.completed else None
        source = "this take"
    if period is None:
        assert take.stop is not None
        periods = [
            p
            for r in references
            if r.completed
            and r.stop is not None
            and _same_rate(r.nominal_hz, take.nominal_hz)
            and (p := run_period_ms(r.stop, r.idle_ms)) is not None
        ]
        if periods:
            period = statistics.median(periods)
            source = f"median of {len(periods)} completed take(s) of the session"
        elif take.nominal_hz:
            period = 1000 / take.nominal_hz
            source = "ThorImage's nominal frameRate (~0.1 % off: approximate)"
        else:
            return None
    camera_ms = camera_period_ms(take.fps)
    if take.half_period_phase:
        first_ms, uncertainty_ms = camera_ms / 2, _FIRST_PULSE_POLL_MS
    else:
        first_ms = _FIRST_PULSE_PERIODS * camera_ms
        uncertainty_ms = _FIRST_PULSE_UNCERTAINTY_PERIODS * camera_ms
    return {
        "version": BOARD_SYNC_VERSION,
        "source": "board",
        "twop_pulses": pulses,
        "twop_period_ms": period,
        "twop_period_from": source,
        "camera_pulses": take.camera_pulses,
        "camera_period_ms": camera_ms,
        "first_camera_pulse_ms": first_ms,
        "start_uncertainty_ms": uncertainty_ms,
        "note": (
            "No ThorSync recording: times on the trigger board's clock, seconds "
            "from the first 2P frame (octacam.plugins.twophoton.board_sync)."
        ),
    }


def _unreported(
    take: BoardTake, references: list[BoardTake], factors: dict[Path, float | None]
) -> tuple[int, float, str] | None:
    """``(2P pulses, period, where the period came from)`` for a take whose
    board sent no report (module doc), or None.
    """
    pulses = _saved_scans(take)
    if not pulses or not take.nominal_hz or take.scan_config is None:
        return None
    measured: list[float] = []
    for r in references:
        if r.take == take.take or r.scan_config != take.scan_config:
            continue
        if r.take not in factors:
            factors[r.take] = rig_factor(r)
        if (f := factors[r.take]) is not None:
            measured.append(f)
    if not measured:
        log.warning(
            "2P board sync: no take with a board report or ThorSync measures this "
            "rig's %s scans; %s is not aligned",
            take.scan_config,
            take.take,
        )
        return None
    factor = statistics.median(measured)
    source = (
        f"ThorImage's nominal frameRate x {factor:.6f}, this rig's factor for "
        f"{take.scan_config} scans (median of {len(measured)} measured take(s); "
        "the board sent no report)"
    )
    return pulses, 1000 / take.nominal_hz * factor, source


def edge_times(model: dict) -> tuple[list[float], list[float]]:
    """``(cameras, frameout)`` pulse times in seconds from the first 2P frame,
    as `signals.read_segment_edge_times` gives them from ThorSync.
    """
    cam_s = model["camera_period_ms"] / 1000
    first = model["first_camera_pulse_ms"] / 1000
    twop_s = model["twop_period_ms"] / 1000
    return (
        [first + k * cam_s for k in range(model["camera_pulses"])],
        [j * twop_s for j in range(model["twop_pulses"])],
    )


def read_board_sync(take: Path) -> dict | None:
    """*take*'s board-clock model, or None."""
    try:
        model = json.loads(info_file(take, BOARD_SYNC_FILENAME).read_text())
    except OSError, ValueError:
        return None
    return model if model.get("source") == "board" else None


def write_board_syncs(
    takes: list[Path],
    *,
    references: Sequence[Path] = (),
    dry_run: bool = False,
) -> list[Path]:
    """Write the board-clock model of each of *takes* paired without ThorSync;
    every take given (ThorSync or not), and every one of *references*, is a
    reference: for the stopped-early ones, and for the rig factor a take whose
    board sent no report needs. Returns the takes written.
    """
    from octacam.files import atomic_write_text

    boards = [b for t in takes if (b := read_board_take(t)) is not None]
    given = {b.take for b in boards}
    pool = boards + [
        b for t in references if t not in given and (b := read_board_take(t))
    ]
    factors: dict[Path, float | None] = {}
    written: list[Path] = []
    for b in boards:
        if b.has_thorsync:
            continue
        model = board_sync(b, pool, factors)
        if model is None:
            log.warning("2P board sync: %s cannot be aligned without ThorSync", b.take)
            continue
        path = info_file(b.take, BOARD_SYNC_FILENAME)
        log.info(
            "2P board sync: %s -- %d 2P pulses every %.4f ms (%s), aligned on the "
            "trigger board's clock (no ThorSync)",
            b.take,
            model["twop_pulses"],
            model["twop_period_ms"],
            model["twop_period_from"],
        )
        if not dry_run:
            path.parent.mkdir(parents=True, exist_ok=True)
            atomic_write_text(path, json.dumps(model, indent=2) + "\n")
        written.append(b.take)
    return written
