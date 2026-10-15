"""Verify a ThorSync↔behavior pairing by reading its actual recorded signals.

`twophoton_transfer.py` pairs a behavior take with a ThorSync/ThorImage folder
by wall-clock time overlap alone — a heuristic. Real-data testing found it can
be made airtight instead, whenever ThorSync actually ran: a `SyncData*`
folder's ``Episode*.h5`` records the Arduino's camera-trigger pulse on its own
DAQ clock (digital input channel ``Cameras``), and its edge count on a real
recording matched **exactly** the paired take's own recorded frame count
(19399 == 19399); ``FrameOut``'s edge count likewise matched ThorImage's own
``Experiment.xml <Timelapse timepoints="...">`` (1500 == 1500). ``CaptureOn``
gates the DAQ's own "recording active" window (one rising + one falling edge
per take actually recorded — treated generically as N ≥ 0 segments here, since
a `SyncData` folder can span more than one take).

This only helps when a `SyncData` folder exists at all — a purely
ThorImage-only session (confirmed real case) has no signal to check against,
and stays on the timestamp-only path in `twophoton_transfer.py`.

Everything here is best-effort and never raises: `h5py` is an optional
dependency (the ``twophoton`` extra) so a rig without it — or a corrupt/
unexpected file — simply gets no verification, not a crashed transfer.
"""

from __future__ import annotations

import logging
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger("octacam")

SETTINGS_XML_FILENAME = "ThorRealTimeDataSettings.xml"
EPISODE_GLOB = "Episode*.h5"

_CAMERAS_CHANNEL = "Cameras"
_FRAMEOUT_CHANNEL = "FrameOut"
_CAPTUREON_CHANNEL = "CaptureOn"


def h5py_available() -> bool:
    """Cheap capability check — used for logging and so tests can force the
    "verification unavailable" path without actually uninstalling h5py."""
    try:
        import h5py  # noqa: F401
    except ImportError:
        return False
    return True


def channel_sample_rate(settings_xml: Path, channel_alias: str) -> float | None:
    """Best-effort DAQ sample rate (Hz) for *channel_alias*, or None.

    ``ThorRealTimeDataSettings.xml`` can list several ``<AcquireBoard>``
    blocks for the same physical device — alternate presets, not all active
    for a given recording — each with its own enabled ``<SampleRate>`` and
    its own ``<DataChannel enable="...">`` children. There is no reliable way
    to tell which block was actually active for *this* recording from the
    settings file alone, so this returns the first board's enabled rate where
    the channel itself is enabled — a diagnostic only (used for a plausibility
    log line), never something matching correctness depends on.
    """
    try:
        root = ET.parse(settings_xml).getroot()
    except (ET.ParseError, OSError):
        return None
    for board in root.iter("AcquireBoard"):
        channel = next(
            (
                c
                for c in board.findall("DataChannel")
                if c.get("alias") == channel_alias
            ),
            None,
        )
        if channel is None or channel.get("enable") != "1":
            continue
        rate_el = next(
            (r for r in board.findall("SampleRate") if r.get("enable") == "1"),
            None,
        )
        if rate_el is None:
            continue
        try:
            return float(rate_el.get("rate", ""))
        except ValueError:
            continue
    return None


@dataclass(frozen=True)
class CaptureSegment:
    """One ``DI/CaptureOn`` rising→falling window within an Episode h5 file."""

    start_sample: int
    end_sample: int
    cameras_edges: int
    frameout_edges: int
    sample_rate: float | None = None  # best-effort; see channel_sample_rate

    @property
    def duration_s(self) -> float | None:
        if self.sample_rate is None or self.sample_rate <= 0:
            return None
        return (self.end_sample - self.start_sample) / self.sample_rate


def _rising_edge_indices(signal) -> list[int]:
    """Sample indices where *signal* transitions from 0 to non-zero."""
    import numpy as np

    nonzero = signal.astype(bool)
    diff = np.diff(nonzero.astype(np.int8))
    return [int(i) for i in np.flatnonzero(diff == 1) + 1]


def _falling_edge_indices(signal) -> list[int]:
    import numpy as np

    nonzero = signal.astype(bool)
    diff = np.diff(nonzero.astype(np.int8))
    return [int(i) for i in np.flatnonzero(diff == -1) + 1]


def _count_edges_in_range(edges: list[int], start: int, end: int) -> int:
    return sum(1 for e in edges if start <= e < end)


def read_capture_segments(sync_folder: Path) -> list[CaptureSegment] | None:
    """Parse *sync_folder*'s ``Episode*.h5`` into one :class:`CaptureSegment`
    per ``DI/CaptureOn`` on/off window.

    Returns ``None`` when verification isn't possible at all — h5py missing,
    no episode file, a required dataset absent, or a corrupt/unreadable file
    — distinct from an empty list, which means the file opened fine but
    ``CaptureOn`` never toggled (also a real, meaningful outcome). Never
    raises.
    """
    if not h5py_available():
        return None
    episodes = sorted(sync_folder.glob(EPISODE_GLOB))
    if not episodes:
        return None
    if len(episodes) > 1:
        log.warning(
            "twophoton verify: %s has more than one Episode*.h5 (%s) — using %s",
            sync_folder,
            ", ".join(p.name for p in episodes),
            episodes[0].name,
        )
    episode_path = episodes[0]

    sample_rate = channel_sample_rate(
        sync_folder / SETTINGS_XML_FILENAME, _CAPTUREON_CHANNEL
    )

    import h5py

    def _column(group: h5py.Group, name: str):
        node = group.get(name)
        return node[:, 0] if isinstance(node, h5py.Dataset) else None

    try:
        with h5py.File(episode_path, "r") as f:
            di = f.get("DI")
            if not isinstance(di, h5py.Group):
                return None
            capture_on = _column(di, _CAPTUREON_CHANNEL)
            if capture_on is None:
                return None
            cameras = _column(di, _CAMERAS_CHANNEL)
            frameout = _column(di, _FRAMEOUT_CHANNEL)
    except OSError:
        log.warning("twophoton verify: could not read %s", episode_path, exc_info=True)
        return None

    rises = _rising_edge_indices(capture_on)
    falls = _falling_edge_indices(capture_on)
    cameras_edges_all = _rising_edge_indices(cameras) if cameras is not None else []
    frameout_edges_all = _rising_edge_indices(frameout) if frameout is not None else []

    segments: list[CaptureSegment] = []
    # Pair each rise with the next fall after it — tolerates a trailing
    # unclosed rise (still-being-written / crashed capture) by just skipping
    # it rather than guessing an end sample.
    fall_iter = iter(falls)
    next_fall = next(fall_iter, None)
    for rise in rises:
        while next_fall is not None and next_fall <= rise:
            next_fall = next(fall_iter, None)
        if next_fall is None:
            break
        segments.append(
            CaptureSegment(
                start_sample=rise,
                end_sample=next_fall,
                cameras_edges=_count_edges_in_range(
                    cameras_edges_all, rise, next_fall
                ),
                frameout_edges=_count_edges_in_range(
                    frameout_edges_all, rise, next_fall
                ),
                sample_rate=sample_rate,
            )
        )
        next_fall = next(fall_iter, None)
    return segments


def camera_edge_diff(segment: CaptureSegment, camera_frame_counts: list[int]) -> int | None:
    """Smallest ``|Cameras edges - frame count|`` across *camera_frame_counts*,
    or None if the list is empty. A caller comparing several candidate
    segments against the same take should rank by this (smaller is better,
    0 = exact) rather than just checking :func:`verify_camera_match`'s
    boolean — real data found more than one segment can fall within
    tolerance of a take's frame count when nearby takes ran for similar
    durations, and the exact match is the one actually confirmed correct."""
    if not camera_frame_counts:
        return None
    return min(abs(segment.cameras_edges - n) for n in camera_frame_counts)


def frameout_edge_diff(segment: CaptureSegment, timepoints: int) -> int:
    """``|FrameOut edges - timepoints|`` — see :func:`camera_edge_diff`."""
    return abs(segment.frameout_edges - timepoints)


def verify_camera_match(
    segment: CaptureSegment, camera_frame_counts: list[int], tolerance: int = 1
) -> bool:
    """Whether *segment*'s Cameras edge count matches any recorded camera's
    own frame count within *tolerance* (per-camera counts can differ by ±1
    from independent grab-thread stop races).

    A quick yes/no check; prefer :func:`camera_edge_diff` when ranking more
    than one candidate against the same take (see its docstring for why)."""
    diff = camera_edge_diff(segment, camera_frame_counts)
    return diff is not None and diff <= tolerance


def verify_frameout_match(
    segment: CaptureSegment, timepoints: int, tolerance: int = 1
) -> bool:
    """Whether *segment*'s FrameOut edge count matches ThorImage's own
    ``Experiment.xml <Timelapse timepoints="...">`` within *tolerance*."""
    return frameout_edge_diff(segment, timepoints) <= tolerance


def read_segment_edge_times(
    sync_folder: Path, segment: CaptureSegment
) -> tuple[list[float], list[float]] | None:
    """``(cameras, frameout)`` rising-edge times in seconds, relative to
    *segment*'s own ``CaptureOn`` rise, for every edge inside that segment —
    the per-frame alignment both halves of a synced behavior+2P render share
    (one DAQ clock, so no wall-clock drift between the two). Falls back to
    ThorSync's usual 100 kHz when the settings file doesn't say. None when
    the episode can't be read; never raises."""
    if not h5py_available():
        return None
    episodes = sorted(sync_folder.glob(EPISODE_GLOB))
    if not episodes:
        return None
    import h5py

    try:
        with h5py.File(episodes[0], "r") as f:
            di = f.get("DI")
            if not isinstance(di, h5py.Group):
                return None
            cameras = di.get(_CAMERAS_CHANNEL)
            frameout = di.get(_FRAMEOUT_CHANNEL)
            if not isinstance(cameras, h5py.Dataset) or not isinstance(frameout, h5py.Dataset):
                return None
            cameras_col = cameras[:, 0]
            frameout_col = frameout[:, 0]
    except OSError:
        log.warning("synced render: could not read %s", episodes[0], exc_info=True)
        return None
    rate = segment.sample_rate or 100_000.0
    start, end = segment.start_sample, segment.end_sample

    def _times(col) -> list[float]:
        return [(e - start) / rate for e in _rising_edge_indices(col) if start <= e < end]

    return _times(cameras_col), _times(frameout_col)


def _active_board(settings_xml: Path):
    """ThorSync's active ``<AcquireBoard active="1">`` element, or None."""
    try:
        root = ET.parse(settings_xml).getroot()
    except (ET.ParseError, OSError):
        return None
    return next((b for b in root.iter("AcquireBoard") if b.get("active") == "1"), None)


def _active_board_rate(settings_xml: Path) -> float | None:
    """Sample rate (Hz) of the active board — unlike :func:`channel_sample_rate`,
    never an inactive preset's."""
    board = _active_board(settings_xml)
    if board is None:
        return None
    for rate_el in board.findall("SampleRate"):
        if rate_el.get("enable") == "1":
            try:
                return float(rate_el.get("rate", ""))
            except ValueError:
                return None
    return None


def thorsync_duration_limit(settings_xml: Path) -> float | None:
    """ThorSync's acquisition length (s) — the active ``<AcquireBoard
    active="1">``'s ``duration``. ThorSync stops recording after it whatever
    the capture is doing: real 2026-10-14 ``duration="320"`` cut every ~394 s
    take's episode at exactly 32 M samples (100 kHz). None when missing,
    unreadable or 0."""
    board = _active_board(settings_xml)
    if board is None:
        return None
    try:
        duration = float(board.get("duration", "0"))
    except ValueError:
        return None
    return duration if duration > 0 else None


@dataclass(frozen=True)
class EpisodeCutOff:
    """A ThorSync episode that ended while ``CaptureOn`` was still high: the
    acquisition went on, but ThorSync stopped recording (its duration limit)
    — every channel (timing, AirFlow, ...) is missing for the rest of it."""

    recorded_s: float | None  # length of what was recorded
    limit_s: float | None  # ThorSync's duration setting, when readable


def episode_cut_off(sync_folder: Path) -> EpisodeCutOff | None:
    """An :class:`EpisodeCutOff` when *sync_folder*'s episode stops mid-capture
    (``CaptureOn`` high at its last sample), else None — also None when it
    can't be read (no h5py, no episode, no CaptureOn). Reads only the last
    sample, so it is cheap even on a multi-GB episode over the network. Only
    meaningful for a settled folder (a still-recording one is "cut" too)."""
    if not h5py_available():
        return None
    episodes = sorted(sync_folder.glob(EPISODE_GLOB))
    if not episodes:
        return None
    import h5py

    try:
        with h5py.File(episodes[0], "r") as f:
            node = f.get(f"DI/{_CAPTUREON_CHANNEL}")
            if not isinstance(node, h5py.Dataset) or node.shape[0] == 0:
                return None
            n = int(node.shape[0])
            still_on = bool(node[n - 1, 0])
    except (OSError, KeyError, ValueError):
        return None
    if not still_on:
        return None
    settings = sync_folder / SETTINGS_XML_FILENAME
    rate = _active_board_rate(settings) or channel_sample_rate(settings, _CAPTUREON_CHANNEL)
    return EpisodeCutOff(
        recorded_s=n / rate if rate else None,
        limit_s=thorsync_duration_limit(settings),
    )


def describe_cut_off(name: str, cut: EpisodeCutOff) -> str:
    """One explicit sentence for logs, manifests and the GUI."""
    limit = f" at its {cut.limit_s:g} s duration limit" if cut.limit_s else ""
    recorded = f" after {cut.recorded_s:.0f} s" if cut.recorded_s and not cut.limit_s else ""
    return (
        f"ThorSync recording {name} stopped{limit}{recorded} while the 2P "
        "capture was still running — its timing, AirFlow and every other "
        "ThorSync channel are missing for the rest of the take. Raise the "
        "acquisition duration in ThorSync."
    )


def latest_thorsync_limit(source_root: Path) -> tuple[Path, float | None] | None:
    """``(newest SyncData folder, its ThorSync duration limit)`` under
    *source_root*'s experiment folders — the settings ThorSync used last,
    hence (until the next acquisition) the ones the next take will get. None
    when the share holds no SyncData folder or can't be listed."""
    try:
        folders = [d for d in source_root.glob("*/SyncData*") if d.is_dir()]
        newest = max(folders, key=lambda d: d.stat().st_mtime, default=None)
    except OSError:
        return None
    if newest is None:
        return None
    return newest, thorsync_duration_limit(newest / SETTINGS_XML_FILENAME)
