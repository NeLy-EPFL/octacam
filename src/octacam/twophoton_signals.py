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
