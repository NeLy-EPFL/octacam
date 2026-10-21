"""Discover, settle-check, and pair ThorSync/ThorImage folders with octacam takes.

Investigation against a real 2-photon rig found the legacy shell script's
assumption -- a sibling ``2p``/``behData`` folder pair at an identical relative
path -- no longer holds: ThorSync/ThorImage write flat, auto-incrementing
folders (``SyncData102``, ``Fly1_004``, ...) directly under an experiment
folder on the Windows share, completely decoupled from octacam's own
``<date>_/<Fly>/<take>`` naming. A take is paired with its 2P counterpart by
the ThorSync DAQ signals when a ``SyncData`` folder exists (the camera pulse
count equals the take's frame count -- see ``twophoton_signals``), else by
start time (within ``match_window_s``).

Two folder kinds are recognized, distinguished structurally rather than by a
fixed naming scheme (ThorImage's own folder name is operator-chosen and
varies: ``Fly1``, ``Fly1_001``, ``Test1_005``, ...):

* **ThorSync** -- a folder named ``SyncData<N>`` holding ``Episode001.h5`` (the
  analog/digital trace) + ``ThorRealTimeDataSettings.xml`` (a settings
  snapshot written once at start). Its own directory mtime is a decent start
  proxy: only those two files are ever created in it, so nothing bumps the
  directory's mtime again after the initial write.
* **ThorImage** -- any folder containing an ``Experiment.xml``. Its directory
  mtime is *not* a usable start proxy (thousands of per-frame ``.tif`` files
  keep bumping it throughout the whole capture); ``Experiment.xml``'s own
  ``<Date uTime="...">`` attribute (a Unix timestamp) is used instead, with
  the directory mtime as a last-resort fallback if that's unparseable.

Neither folder type has a definitive "acquisition complete" marker beyond
mtime quiescence -- this module's ``is_settled`` is a direct port of the old
script's ``find_last_file_access_time`` idea.

This module never touches ThorSync/ThorImage source files' *content*, is
config-agnostic (callers resolve any ``%``-templated source path before
calling in), and does not perform any file transfer itself -- see
``transfer.transfer_tree`` for that.
"""

from __future__ import annotations

import os
import re
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path

from octacam.plugins.twophoton.signals import (
    CaptureSegment,
    camera_edge_diff,
    frameout_edge_diff,
    read_capture_segments,
)

EXPERIMENT_XML_FILENAME = "Experiment.xml"
SYNC_FOLDER_PREFIX = "SyncData"

# Edge-count tolerance for a signal-verified match (camera/frameout edges vs.
# the take's own recorded frame count / ThorImage's own timepoints) -- shared
# by match_takes_to_twophoton_batch and correlate_sync_folder_to_image.
_VERIFY_TOLERANCE = 1
# How late the board's cancel can land after octacam stopped the cameras.
LATE_STOP_S = 0.5
# No acquisition outlasts this (the plugin's ceiling defaults to 1 h): an
# older start rules a folder out without reading its files' mtimes.
_LONGEST_ACQUISITION_S = 4 * 3600.0


@dataclass(frozen=True)
class TwoPhotonFolder:
    """One discovered ThorSync/ThorImage take folder."""

    path: Path
    kind: str  # "sync" | "image"
    start_time: float  # unix seconds, best-effort
    # The latest mtime of any file in it, recursively (see last_write): None
    # until asked, as it stats every frame file (thousands, over a share).
    last_mtime: float | None = field(default=None, compare=False, hash=False)


def last_write(folder: TwoPhotonFolder) -> float:
    """*folder*'s latest file mtime, computed once when first asked."""
    if folder.last_mtime is None:
        object.__setattr__(folder, "last_mtime", _folder_last_mtime(folder.path))
    assert folder.last_mtime is not None
    return folder.last_mtime


@dataclass(frozen=True)
class TwoPhotonMatch:
    """One folder matched to a take, with enough context to audit the pairing."""

    folder: TwoPhotonFolder
    # start-time gap (see _start_gap_seconds) -- the sole gating/ranking criterion
    gap_s: float
    ambiguous: bool  # more than one same-kind candidate also overlapped
    # "verified": confirmed by reading the actual DAQ signal (edge count
    # matches the take's own recorded frame count / ThorImage's own
    # timepoints) -- see twophoton_signals.py. "timestamp": the coarse
    # time-window heuristic only (match_take_to_twophoton), because
    # verification wasn't attempted, wasn't possible (no h5py, no SyncData
    # folder for this session), or found no matching edge count.
    confidence: str = "timestamp"
    # Informational only (see _end_gap_seconds) -- never gates a match. Signed;
    # positive means the 2P folder's last write landed after the take ended
    # (ThorImage writes its TIFFs ~25-30 s after the capture).
    end_gap_s: float = 0.0


@dataclass(frozen=True)
class TakeInfo:
    """One behavior take, as much as the matcher needs to know about it."""

    folder: Path  # the local recording folder (used only as a dict key)
    # unix seconds: the take's first frame (cli._take_match_start_s), not the
    # moment Start was pressed -- octacam is armed before ThorImage starts.
    start_time: float
    duration_s: float
    camera_frame_counts: list[int]  # per-camera recorded frame counts
    # Stopped from octacam (not by ThorImage nor its duration): the board's
    # cancel lands a little after the cameras stopped.
    stopped_early: bool = False
    # The board's stop_idle_ms for a take synced with ThorImage (s); None: the
    # plugin's default (a take from before twophoton.json recorded it).
    stop_idle_s: float | None = None


DELTA = "\N{GREEK CAPITAL LETTER DELTA}"


def _thorimage_start_time(experiment_xml: Path) -> float | None:
    """Parse ``<Date uTime="...">`` from a ThorImage ``Experiment.xml``, or
    None if the file is missing, unparseable, or lacks that attribute.
    """
    try:
        root = ET.parse(experiment_xml).getroot()
    except ET.ParseError, OSError:
        return None
    date_el = root.find("Date")
    if date_el is None:
        return None
    u_time = date_el.get("uTime")
    if u_time is None:
        return None
    try:
        return float(u_time)
    except ValueError:
        return None


def thorimage_planned_duration_s(experiment_xml: Path) -> float | None:
    """How long a streaming ThorImage acquisition is set to run, if not stopped
    early: declared timepoints x scans per timepoint (FastZ planes + flyback,
    frame averaging) / ``LSM frameRate`` (real: 3000 x 1 / 7.7 Hz = 390 s for
    394 s takes). None for a single-timepoint capture (Z-stack) or when a
    field is missing.
    """
    timepoints = thorimage_timepoints(experiment_xml)
    if not timepoints or timepoints <= 1:
        return None
    try:
        lsm = ET.parse(experiment_xml).getroot().find("LSM")
        frame_rate = float(lsm.get("frameRate", "0")) if lsm is not None else 0.0
    except ET.ParseError, OSError, ValueError:
        return None
    if frame_rate <= 0:
        return None
    scans = (thorimage_declared_frameout_edges(experiment_xml) or timepoints) * (
        thorimage_frame_average(experiment_xml)
    )
    return scans / frame_rate


def thorimage_declared_frameout_edges(experiment_xml: Path) -> int | None:
    """FrameOut edges the *declared* acquisition would record, before any
    early stop or frame averaging: timepoints, or for FastZ timepoints x
    (planes + flyback).
    """
    timepoints = thorimage_timepoints(experiment_xml)
    if timepoints is None:
        return None
    geometry = _fastz_geometry(experiment_xml)
    return timepoints * (geometry[0] + geometry[1]) if geometry else timepoints


def latest_thorimage_plan(source_root: Path) -> tuple[Path, float] | None:
    """``(newest streaming ThorImage folder, its planned duration)`` under
    *source_root*'s experiment folders -- what ThorImage is set to record next,
    as far as the share tells. None when there is none.
    """
    try:
        xmls = [
            x for x in source_root.glob(f"*/*/{EXPERIMENT_XML_FILENAME}") if x.is_file()
        ]
        ranked = sorted(xmls, key=lambda x: x.stat().st_mtime, reverse=True)
    except OSError:
        return None
    for xml in ranked[:20]:
        planned = thorimage_planned_duration_s(xml)
        if planned:
            return xml.parent, planned
    return None


def thorimage_timepoints(experiment_xml: Path) -> int | None:
    """Parse ``<Timelapse timepoints="...">`` from a ThorImage ``Experiment.xml``
    -- the acquisition's own recorded frame count, for cross-checking against a
    verified segment's ``FrameOut`` edge count. None if missing/unparseable.
    """
    try:
        root = ET.parse(experiment_xml).getroot()
    except ET.ParseError, OSError:
        return None
    timelapse_el = root.find("Timelapse")
    if timelapse_el is None:
        return None
    timepoints = timelapse_el.get("timepoints")
    if timepoints is None:
        return None
    try:
        return int(timepoints)
    except ValueError:
        return None


def thorimage_expected_frameout_edges(experiment_xml: Path) -> int | None:
    """How many ``FrameOut`` edges ThorSync should record for this ThorImage
    acquisition -- what a verified segment's edge count is compared against.

    ``FrameOut`` pulses once per *scan*: per timepoint that is 1 for a plain
    stream, every Z plane plus the piezo's flyback frame(s) for FastZ
    (confirmed 2026-10-07, ``AllPPL1xCI80``: 500 timepoints x (5 + 1) = 3000
    edges), times ``averageNum`` with frame averaging (2026-10-14 Fly4/003:
    476 frames x 4 ~ 1906 edges). Counted over the timepoints actually
    recorded -- fewer than declared when the capture was stopped early (since
    octacam stops with ThorImage, a real take). Z-stacks (one timepoint) are
    matched by :func:`thorimage_zstack_signature` instead. None if
    missing/unparseable.
    """
    timepoints = thorimage_timepoints(experiment_xml)
    if timepoints is None:
        return None
    if timepoints <= 1:
        return timepoints  # Z-stacks: see thorimage_zstack_signature
    geometry = _fastz_geometry(experiment_xml)
    planes, scans_per_timepoint = (
        (geometry[0], geometry[0] + geometry[1]) if geometry else (1, 1)
    )
    recorded = _timepoints_recorded(experiment_xml.parent, timepoints, planes)
    return recorded * scans_per_timepoint * thorimage_frame_average(experiment_xml)


def _fastz_geometry(experiment_xml: Path) -> tuple[int, int] | None:
    """``(planes, flyback frames)`` for a FastZ (``Streaming zFastEnable``)
    capture, else None.
    """
    try:
        root = ET.parse(experiment_xml).getroot()
    except ET.ParseError, OSError:
        return None
    streaming_el, zstage_el = root.find("Streaming"), root.find("ZStage")
    if (
        streaming_el is None
        or zstage_el is None
        or streaming_el.get("enable") != "1"
        or streaming_el.get("zFastEnable") != "1"
    ):
        return None
    try:
        return int(zstage_el.get("steps", "1")), int(
            streaming_el.get("flybackFrames", "0")
        )
    except ValueError:
        return None


def thorimage_frame_average(experiment_xml: Path) -> int:
    """Scans averaged into each saved frame: ``LSM averageNum`` when
    ``averageMode="1"``, else 1. ``FrameOut`` pulses once per *scan*, so an
    averaged capture records N edges per saved frame -- real 2026-10-14
    Fly4/003 (rolling average left on, N=4): 1906 edges for 476 frames. A
    Z-stack shows the same N edges per slice (thorimage_zstack_signature).
    """
    try:
        lsm = ET.parse(experiment_xml).getroot().find("LSM")
    except ET.ParseError, OSError:
        return 1
    if lsm is None or lsm.get("averageMode") != "1":
        return 1
    try:
        return max(1, int(lsm.get("averageNum", "1")))
    except ValueError:
        return 1


def thorimage_frameout_tolerance(experiment_xml: Path) -> int:
    """Allowed ``|FrameOut edges - expected|`` for this capture: an averaged
    capture stopped early can end mid-group, leaving up to N-1 scans that
    never became a saved frame, plus the ones being scanned at the stop --
    for FastZ, whole volumes' scans.
    """
    geometry = _fastz_geometry(experiment_xml)
    scans_per_timepoint = geometry[0] + geometry[1] if geometry else 1
    scans = scans_per_timepoint * thorimage_frame_average(experiment_xml)
    # Stopped early, ThorImage may scan up to two frames it never saves (real
    # 2026-10-16: 93 and 112 saved frames, 95 and 114 FrameOut edges;
    # 2026-10-14: 418 and 419).
    declared = thorimage_timepoints(experiment_xml)
    if declared is not None and declared > 1:
        planes = geometry[0] if geometry else 1
        if _timepoints_recorded(experiment_xml.parent, declared, planes) < declared:
            scans *= 2
    return max(_VERIFY_TOLERANCE, scans)


def _channel_frames(folder: Path) -> int | None:
    """Frames the first channel recorded: its per-frame files plus the pages
    of its assembled stacks (`<chan>.tif`, FastZ's `<chan>_Z01.tif`...), so a
    copy on the destination counts the same as the share's folder. None when
    there is nothing to count (no channel, no files, an unreadable folder).
    """
    from octacam.plugins.twophoton.tiff import (
        _matching_frame_files,
        _thorimage_channel_names,
    )

    channels = _thorimage_channel_names(folder / EXPERIMENT_XML_FILENAME)
    if not channels:
        return None
    chan = channels[0]
    try:
        n = len(_matching_frame_files(folder, chan))
        stacks = [folder / f"{chan}.tif", *sorted(folder.glob(f"{chan}_Z*.tif"))]
    except OSError:
        return None
    for stack in stacks:
        if stack.is_file():
            pages = _stack_pages(stack)
            if pages is None:
                return None  # cannot tell: the caller keeps the declared count
            n += pages
    return n or None


def _stack_pages(stack: Path) -> int | None:
    """An assembled stack's frame count, from its OME header (no page walk)."""
    try:
        import tifffile

        with tifffile.TiffFile(stack) as tif:
            shape = tif.series[0].shape
    except Exception:  # no tifffile, or not a readable stack
        return None
    return int(shape[0]) if len(shape) == 3 else 1


def _timepoints_recorded(folder: Path, declared: int, planes: int = 1) -> int:
    """Timepoints a streaming capture actually wrote -- fewer than the
    *declared* ones when it was stopped early (a real, usable take since
    octacam stops with ThorImage, 2026-10-14). Counted from the first
    channel's frames (one per plane per timepoint, whole volumes only), on
    the share or assembled on the destination; *declared* when they cannot
    be counted.
    """
    if declared <= 1:
        return declared
    frames = _channel_frames(folder)
    n = (frames or 0) // max(planes, 1)
    return n if 0 < n < declared else declared


def twophoton_identity(folder: Path) -> tuple[str, str]:
    """``(name, fingerprint)`` identifying one ThorImage/ThorSync acquisition
    across copies -- what NAS-side dedup compares, never the bare name.

    ThorImage reuses folder names per experiment (``Fly1_Zstack`` exists in
    many experiments) and ThorSync's ``SyncData<N>`` numbering repeats across
    experiment subfolders (real data, 2026-10-07: ``Test/SyncData135`` and
    ``AllPPL1xCI80/SyncData135`` the same day) -- a name-only check silently
    skipped a new day's Z-stacks forever because an older experiment had
    same-named folders. The fingerprint survives a copy: ``Experiment.xml``'s
    ``<Date uTime>`` (the acquisition's own start, unique per capture) for an
    image folder, the ``Episode*.h5`` byte size for a sync folder, ``""`` when
    neither is readable (falls back to name-only, the old behavior).
    """
    start = _thorimage_start_time(folder / EXPERIMENT_XML_FILENAME)
    if start is not None:
        return (folder.name, f"uTime={int(start)}")
    episodes = sorted(folder.glob("Episode*.h5"))
    if episodes:
        try:
            return (folder.name, f"h5size={episodes[0].stat().st_size}")
        except OSError:
            pass
    return (folder.name, "")


def thorimage_zstack_signature(experiment_xml: Path) -> tuple[int, int] | None:
    """``(slices, frameout_edges_per_slice)`` ThorSync records for a
    step-and-shoot Z-stack, or None if this isn't one.

    A Z-stack's ThorSync episode has one ``CaptureOn`` segment *per slice*,
    each carrying one ``FrameOut`` edge per averaged frame -- confirmed on real
    data (2026-10-07, ``AllPPL1xCI80``): ``ZStage steps="201"`` with
    ``LSM averageMode="1" averageNum="4"`` recorded 201 segments x 4 edges;
    with ``averageMode="0"`` 201 x 1. Comparing a single segment against
    ``timepoints`` (1) -- the streaming rule -- left every averaged Z-stack's
    SyncData unattributable.

    Slices are the ones recorded: a stack stopped early has fewer, as many
    as its frames. A Z-stack repeated over time (Z+T, streaming off) is
    assumed to be one segment per slice of every volume: never seen on real
    data, to study on a first real capture (out of scope for now).
    """
    try:
        root = ET.parse(experiment_xml).getroot()
    except ET.ParseError, OSError:
        return None
    z, stream = root.find("ZStage"), root.find("Streaming")
    timepoints = thorimage_timepoints(experiment_xml) or 1
    if z is None or z.get("enable") != "1":
        return None
    if stream is not None and stream.get("enable") == "1":
        return None
    try:
        steps = int(z.get("steps", "0"))
    except ValueError:
        return None
    if steps <= 1:
        return None
    declared = steps * timepoints
    frames = _channel_frames(experiment_xml.parent)
    slices = frames if frames is not None and 0 < frames < declared else declared
    return slices, thorimage_frame_average(experiment_xml)


def _folder_last_mtime(folder: Path) -> float:
    """Max ``st_mtime`` across every file under *folder*, recursively.

    Mirrors ``move_files.sh``'s ``find_last_file_access_time``. Only files'
    own mtimes are considered, not directory mtimes: a directory's mtime only
    updates when an entry is added/removed, which understates a file that
    keeps growing without any new directory entries (e.g. ThorSync's
    ``Episode001.h5``, appended to for minutes after its one creation).
    Falls back to the folder's own mtime only if it somehow contains no files
    (shouldn't happen -- discovery already requires at least one).
    """
    latest: float | None = None
    for p in folder.rglob("*"):
        if not p.is_file():
            continue
        try:
            m = p.stat().st_mtime
        except OSError:
            continue
        if latest is None or m > latest:
            latest = m
    return latest if latest is not None else folder.stat().st_mtime


def classify_twophoton_folder(candidate: Path) -> TwoPhotonFolder | None:
    """Classify exactly one folder as a ThorSync/ThorImage take, or ``None``
    if it's neither shape. The per-candidate half of
    :func:`discover_twophoton_folders`'s scan, pulled out so a caller that
    already knows the exact folder to classify (e.g. `cli.py`'s
    reconciliation pass, working directly on an already-transferred
    ``2P_only/<name>`` or ``2p_only/<experiment>/<date>/<name>`` entry) can
    classify it directly instead of re-deriving it from a two-level scan of
    some ancestor directory.
    """
    experiment_xml = candidate / EXPERIMENT_XML_FILENAME
    if experiment_xml.is_file():
        start = _thorimage_start_time(experiment_xml)
        if start is None:
            start = candidate.stat().st_mtime
        return TwoPhotonFolder(candidate, "image", start)
    if candidate.name.startswith(SYNC_FOLDER_PREFIX):
        # ThorSync writes its settings file once, at the start: on the share
        # its mtime equals the folder's (real data: to the 0.1 s), and unlike
        # the folder's, a copy keeps it -- on the NAS the folder's own mtime is
        # the copy time, which misordered a 2P-only Z-stack's RecordingN.
        settings = candidate / "ThorRealTimeDataSettings.xml"
        try:
            start = settings.stat().st_mtime
        except OSError:
            start = candidate.stat().st_mtime
        return TwoPhotonFolder(candidate, "sync", start)
    return None


def discover_twophoton_folders(source_root: Path) -> list[TwoPhotonFolder]:
    """Find ThorSync/ThorImage take folders one level under each experiment
    folder in *source_root* (e.g. ``source_root/MB247_CI63/SyncData102``).

    *source_root* itself does not exist or isn't a directory -> returns an
    empty list rather than raising (the share may not be mounted yet).
    """
    found: list[TwoPhotonFolder] = []
    if not source_root.is_dir():
        return found
    for experiment_dir in sorted(p for p in source_root.iterdir() if p.is_dir()):
        try:
            candidates = sorted(p for p in experiment_dir.iterdir() if p.is_dir())
        except OSError:
            continue
        for candidate in candidates:
            classified = classify_twophoton_folder(candidate)
            if classified is not None:
                found.append(classified)
    return found


# How long an image folder with no image file at all must stay quiet before it
# counts as settled: longer than any acquisition (twophoton max_duration_s
# defaults to 1 h), since ThorImage writes no TIFF until the capture ends.
_EMPTY_IMAGE_FOLDER_SETTLE_S = 2 * 3600.0


def _has_image_data(folder: Path) -> bool:
    """Whether a ThorImage folder holds any image file besides its previews."""
    try:
        with os.scandir(folder) as entries:
            return any(
                e.name.lower().endswith((".tif", ".tiff")) and "_Preview" not in e.name
                for e in entries
            )
    except OSError:
        return False


def is_settled(
    folder: TwoPhotonFolder, settle_s: float, now: float | None = None
) -> bool:
    """Whether *folder* has been mtime-quiescent for at least *settle_s*.

    Neither ThorSync nor ThorImage leaves a definitive "done" marker (see
    module docstring) -- this is the best available signal, same idea as the
    legacy script's delay-based check. One exception: ThorImage writes no
    TIFF until the capture ends, so mid-acquisition its folder holds only an
    ``Experiment.xml`` written at the start -- quiet for the whole capture.
    Such a folder only counts as settled after
    ``_EMPTY_IMAGE_FOLDER_SETTLE_S`` (an aborted, empty capture still gets
    transferred, later), so a sweep running during a long take never copies
    it half-written and then skips the complete one as "already present".
    """
    now = time.time() if now is None else now
    quiet_s = now - last_write(folder)
    if folder.kind == "image" and not _has_image_data(folder.path):
        return quiet_s >= max(settle_s, _EMPTY_IMAGE_FOLDER_SETTLE_S)
    return quiet_s >= settle_s


def _start_gap_seconds(take_start: float, candidate: TwoPhotonFolder) -> float:
    """The sole timestamp-tier acceptance/ranking criterion: how far
    *candidate*'s start drifted from the take's own start.

    On this trigger architecture, octacam is armed and then the 2P
    acquisition's own start signals octacam to start recording (via
    ThorSync) -- so a genuinely paired take and 2P folder should start within
    a couple of seconds of each other, not tens of seconds. Confirmed on
    real data: real matched pairs across two independent experiments showed
    start diffs of 0.5-9.6s (one 36s outlier on a session's very first
    pairing), regardless of `end`/duration. Since 2026-10-14 a synced take
    also stops with ThorImage, but a fixed-duration one still ends
    independently, so the end never decides. See :func:`_end_gap_seconds`
    for that (informational-only) side.
    """
    return abs(candidate.start_time - take_start)


def _end_gap_seconds(take_end: float, candidate: TwoPhotonFolder) -> float:
    """How far *candidate*'s last write landed from the take's own computed
    end -- signed (positive: the 2P folder finished writing after the take
    ended -- ThorImage writes its TIFFs after the capture). Purely
    informational: a fixed-duration take stops independently of ThorImage,
    so this can differ by a lot and must never gate a match -- see
    :func:`_start_gap_seconds` for the one criterion that does.
    """
    return last_write(candidate) - take_end


def _overlaps(
    take_start: float, take_end: float, candidate: TwoPhotonFolder, window_s: float
) -> bool:
    """Whether *candidate*'s ``[start_time, last_mtime]`` window overlaps
    ``[take_start, take_end]``, each padded by *window_s* -- a loose "is this
    even worth investigating" pre-filter, used only to bound which
    ``SyncData`` folders get opened for signal verification (deliberately
    generous: verification itself, not timing shape, is what confirms or
    rejects the candidate -- see match_takes_to_twophoton_batch). Not used for
    the timestamp-only decision itself; see :func:`_start_gap_seconds` for
    that.
    """
    if candidate.start_time > take_end + window_s:
        return False
    if candidate.start_time < take_start - window_s - _LONGEST_ACQUISITION_S:
        return False  # ended long before, whatever its last write
    return last_write(candidate) >= take_start - window_s


def match_take_to_twophoton(
    take_start: float,
    take_duration_s: float,
    candidates: list[TwoPhotonFolder],
    match_window_s: float,
) -> list[TwoPhotonMatch]:
    """Match a behavior take's time window against discovered 2P folders.

    A candidate matches only when its **start** lands within *match_window_s*
    of the take's own start (see :func:`_start_gap_seconds`) -- octacam is
    triggered directly by the 2P acquisition's own start (via ThorSync), so a
    genuine pair's start should differ by at most a couple of seconds, not
    "somewhere within a loose window". This is *more* selective than the old
    duration-aware check against the false positive it was built to catch (a
    short, unrelated acquisition nested inside a much longer take): such a
    snapshot's own start essentially never coincidentally lands within a few
    seconds of the take's start, so the tight start window excludes it
    without needing to compare duration at all. End time and duration are
    **not** part of this decision -- a fixed-duration take stops independently
    of ThorImage, so they can differ and are only carried informationally
    (:func:`_end_gap_seconds`, ``TwoPhotonMatch.end_gap_s``).

    Returns 0-2 matches -- at most one per kind (``sync``/``image``); never
    forces a pairing that isn't there. When more than one same-kind candidate
    matches, the best-aligned (smallest start gap) wins and the match is
    flagged ``ambiguous=True`` so the caller can log/audit rather than
    silently trust it (real 2P sessions were found where ThorSync/ThorImage
    are *not* always started 1:1 with each other or with a single take).

    This is the timestamp-only fallback -- see
    :func:`match_takes_to_twophoton_batch` for the verified/no-double-booking
    matcher `octacam process` actually drives.
    """
    take_end = take_start + take_duration_s

    by_kind: dict[str, list[tuple[float, TwoPhotonFolder]]] = {}
    for c in candidates:
        gap = _start_gap_seconds(take_start, c)
        if gap > match_window_s:
            continue
        by_kind.setdefault(c.kind, []).append((gap, c))

    matches: list[TwoPhotonMatch] = []
    for kind_matches in by_kind.values():
        kind_matches.sort(key=lambda item: item[0])
        best_gap, best_folder = kind_matches[0]
        matches.append(
            TwoPhotonMatch(
                folder=best_folder,
                gap_s=best_gap,
                ambiguous=len(kind_matches) > 1,
                end_gap_s=_end_gap_seconds(take_end, best_folder),
            )
        )
    return matches


def _tied(ranked: list, best_diff: int, best_gap: float, window_s: float) -> int:
    """Folders in *ranked* (diff, gap, folder, ...) as good as the best: the
    same edge-count diff and a start gap within *window_s* of the best's.
    """
    return len(
        {
            id(r[2])
            for r in ranked
            if r[0] == best_diff and abs(r[1] - best_gap) <= window_s
        }
    )


def match_takes_to_twophoton_batch(
    takes: list[TakeInfo],
    candidates: list[TwoPhotonFolder],
    match_window_s: float,
    verify_window_s: float,
    verify_with_signals: bool,
) -> dict[Path, list[TwoPhotonMatch]]:
    """Match a whole batch of takes (sharing one ``[transfer.twophoton]``
    source) against discovered 2P folders, processed in chronological order
    so the same folder is never double-booked across two different takes.

    Two tiers, tried per take in this order:

    1. **Verified** (``confidence="verified"``): for each not-yet-claimed
       ``sync``-kind candidate whose window overlaps the take within the
       (wider) *verify_window_s*, read its actual DAQ signal
       (``twophoton_signals.read_capture_segments``) and look for a segment
       whose ``Cameras`` edge count matches this take's own recorded
       per-camera frame count. An exact/near match is decisive -- used even if
       its coarse timestamp gap exceeds *match_window_s* -- and claims that
       *segment* specifically (not the whole folder), so a ``SyncData`` folder
       spanning multiple takes can still verify-match a different segment
       against a different take. Within that same verified segment, an
       ``image``-kind candidate whose own ``Experiment.xml`` ``timepoints``
       matches the segment's ``FrameOut`` edge count is the transitively
       verified image match.
    2. **Timestamp** (``confidence="timestamp"``): whatever tier 1 didn't
       resolve falls back to :func:`match_take_to_twophoton`, run only
       against the pool of folders no earlier take in this batch has already
       claimed -- this is what actually removes the double-booking that a
       real test found producing most `ambiguous` flags, using nothing but
       timestamps already available today.

    *verify_with_signals* off (or ``h5py`` unavailable, or no ``SyncData``
    folder for this session at all -- the common no-ThorSync case) skips
    straight to tier 2 for every take.
    """
    ordered = sorted(takes, key=lambda t: t.start_time)
    claimed_folders: set[Path] = set()
    claimed_segments: set[tuple[Path, int]] = set()
    segment_cache: dict[Path, list[CaptureSegment] | None] = {}
    timepoints_cache: dict[Path, int | None] = {}
    result: dict[Path, list[TwoPhotonMatch]] = {}

    for take in ordered:
        take_end = take.start_time + take.duration_s
        matches: list[TwoPhotonMatch] = []
        verified_segment_key: tuple[Path, int] | None = None

        if verify_with_signals:
            # Rank every (folder, segment) within tolerance rather than
            # taking the first one found: real data has more than one
            # SyncData folder land within a frame or two of a take's count
            # when nearby takes ran similar durations -- the exact match is
            # the one actually confirmed correct, and iteration order alone
            # (e.g. alphabetical) is not a reason to prefer a different one.
            ranked: list[tuple[int, float, TwoPhotonFolder, int]] = []
            for c in candidates:
                if c.kind != "sync" or c.path in claimed_folders:
                    continue
                if not _overlaps(take.start_time, take_end, c, verify_window_s):
                    continue
                if c.path not in segment_cache:
                    segment_cache[c.path] = read_capture_segments(c.path)
                segments = segment_cache[c.path]
                if not segments:
                    continue
                # gap only breaks ties among edge-count-tied candidates below
                # -- verified confidence never depends on timing shape.
                gap = _start_gap_seconds(take.start_time, c)
                for idx, seg in enumerate(segments):
                    if (c.path, idx) in claimed_segments:
                        continue
                    diff = camera_edge_diff(
                        seg,
                        take.camera_frame_counts,
                        late_stop_s=LATE_STOP_S if take.stopped_early else 0.0,
                        stop_idle_s=take.stop_idle_s,
                    )
                    if diff is not None and diff <= _VERIFY_TOLERANCE:
                        ranked.append((diff, gap, c, idx))
            if ranked:
                ranked.sort(key=lambda r: (r[0], r[1]))
                best_diff, best_gap, best_folder, best_idx = ranked[0]
                # Ambiguous when another folder fits the pulse count equally
                # well and starts about as close: the start gap then barely
                # decided. Takes of one length all fit each other's counts
                # (real 2026-10-16: every 67 s take flagged), but one started
                # seconds from the take and the others minutes away.
                tied = _tied(ranked, best_diff, best_gap, match_window_s)
                matches.append(
                    TwoPhotonMatch(
                        folder=best_folder,
                        gap_s=best_gap,
                        ambiguous=tied > 1,
                        confidence="verified",
                        end_gap_s=_end_gap_seconds(take_end, best_folder),
                    )
                )
                verified_segment_key = (best_folder.path, best_idx)

        if verified_segment_key is not None:
            claimed_segments.add(verified_segment_key)
            sync_path, seg_idx = verified_segment_key
            sync_segments = segment_cache[sync_path]
            assert (
                sync_segments is not None
            )  # guaranteed: we just matched a segment in it
            segment = sync_segments[seg_idx]
            image_ranked: list[tuple[int, float, TwoPhotonFolder]] = []
            for c in candidates:
                if c.kind != "image" or c.path in claimed_folders:
                    continue
                # An edge count alone is not identifying (many captures share
                # a frame count) -- without this window a weeks-old folder
                # elsewhere on the share with the same count was claimed
                # (real bug, 2026-10-07: September's AllPAM Fly4_Streaming_*
                # verified against October takes).
                image_gap = _start_gap_seconds(take.start_time, c)
                if image_gap > verify_window_s:
                    continue
                if c.path not in timepoints_cache:
                    timepoints_cache[c.path] = thorimage_expected_frameout_edges(
                        c.path / EXPERIMENT_XML_FILENAME
                    )
                timepoints = timepoints_cache[c.path]
                if timepoints is None:
                    continue
                diff = frameout_edge_diff(segment, timepoints)
                if diff <= thorimage_frameout_tolerance(
                    c.path / EXPERIMENT_XML_FILENAME
                ):
                    image_ranked.append((diff, image_gap, c))
            if image_ranked:
                image_ranked.sort(key=lambda r: (r[0], r[1]))
                best_diff, best_gap, best_folder = image_ranked[0]
                tied = _tied(image_ranked, best_diff, best_gap, match_window_s)
                matches.append(
                    TwoPhotonMatch(
                        folder=best_folder,
                        gap_s=best_gap,
                        ambiguous=tied > 1,
                        confidence="verified",
                        end_gap_s=_end_gap_seconds(take_end, best_folder),
                    )
                )
                claimed_folders.add(best_folder.path)

        resolved_kinds = {m.folder.kind for m in matches}
        remaining = [
            c
            for c in candidates
            if c.path not in claimed_folders and c.kind not in resolved_kinds
        ]
        for m in match_take_to_twophoton(
            take.start_time, take.duration_s, remaining, match_window_s
        ):
            matches.append(m)
            claimed_folders.add(m.folder.path)

        result[take.folder] = matches
    return result


def render_twophoton_manifest(
    day_label: str,
    takes: list[dict],
    unclaimed: list[TwoPhotonFolder],
    source_reachable: bool,
    attributed: dict[Path, str] | None = None,
    cut_offs: dict[str, str] | None = None,
) -> str:
    """Render a human-readable ``2p_reconciliation.md`` for one day/session
    destination folder: every behavior take transferred that day against its
    matched 2P folder(s) (or "unmatched"), plus every 2P folder discovered on
    the share that day no take claimed -- the single legible file this feature
    replaces per-take ``twophoton_match.json`` digging with. Pure/deterministic
    (no filesystem access) so it's cheap to unit test in isolation from the
    disk-scanning caller.

    *takes* -- one dict per take: ``{"name": str, "start_time": float,
    "duration_s": float, "armed": bool, "matches": list[dict]}``, where each
    match dict has :func:`build_match_record`'s per-entry shape (``path``,
    ``kind``, ``gap_s``, ``end_gap_s``, ``ambiguous``, ``confidence``).
    *unclaimed* is every still-live 2P folder that day no take's
    ``twophoton_match.json`` references. *source_reachable* -- False when the
    live rescan of ``[transfer.twophoton].source`` couldn't happen (share
    unmounted), in which case the unclaimed-folder section is replaced by a
    note instead of silently claiming there's nothing unclaimed. *attributed*
    -- ``{folder.path: fly_folder_name}`` for whichever unclaimed folders the
    caller has decided belong to a specific fly (same decision the sweep
    itself makes, shown here purely as a preview -- see
    ``cli._attribute_unclaimed_folder``); absent entries default to the
    generic bucket. *cut_offs* -- ``{folder name: message}`` for unclaimed
    ThorSync folders whose recording was cut off; a matched one carries its
    own ``thorsync_cut_off`` in the match entry. Both are listed up front
    under **Warnings**.
    """

    def fmt_time(t: float) -> str:
        return time.strftime("%H:%M:%S", time.localtime(t))

    lines = [
        f"# 2P reconciliation -- {day_label}",
        "",
        (
            f"Generated {time.strftime('%Y-%m-%d %H:%M:%S')} -- this file is "
            "fully regenerated on every `octacam process` run; hand edits will "
            "be overwritten."
        ),
        "",
        "<!-- warnings -->",
        "## Behavior takes",
        "",
        (
            f"Start {DELTA} is what decided the match (octacam is triggered "
            "directly by the 2P acquisition's own start, so a real pair's start "
            f"should agree to within a couple of seconds). End {DELTA} is "
            "informational only, never used to accept or reject a match: the 2P "
            "folder's last file write against the take's end, so it also counts "
            "ThorImage/ThorSync still writing after the capture (minutes after a "
            "long one), and it differs when octacam and the 2P side stopped "
            "independently (a take not synced with ThorImage)."
        ),
        "",
        (
            "| Take | Start | Duration | Armed | 2P match | Kind | Confidence "
            f"| Start {DELTA} | End {DELTA} (info) |"
        ),
        "|---|---|---|---|---|---|---|---|---|",
    ]
    warnings: list[str] = []
    for take in sorted(takes, key=lambda t: t["start_time"]):
        start = fmt_time(take["start_time"])
        duration = f"{take['duration_s']:.1f}s"
        armed = "yes" if take["armed"] else "no"
        matches = take["matches"]
        if not matches:
            lines.append(
                f"| {take['name']} | {start} | {duration} | {armed} | "
                "**unmatched** | | | | |"
            )
            continue
        for i, m in enumerate(matches):
            prefix = (
                f"{take['name']} | {start} | {duration} | {armed}"
                if i == 0
                else " |  |  | "
            )
            flag = " (ambiguous)" if m.get("ambiguous") else ""
            if m.get("no_images"):
                flag += " (no images saved)"
            cut = m.get("thorsync_cut_off")
            if cut:
                flag += " (!) cut off"
                warnings.append(
                    f"**{take['name']}**: "
                    + cut.get("message", "ThorSync recording cut off")
                )
            end_gap = m.get("end_gap_s", 0.0)
            lines.append(
                f"| {prefix} | {m['path']}{flag} | {m['kind']} | "
                f"{m['confidence']} | {m['gap_s']}s | {end_gap:+.1f}s |"
            )

    lines += ["", "## Unclaimed 2P folders on the share"]
    if not source_reachable:
        lines += [
            "",
            (
                "`[transfer.twophoton].source` wasn't reachable when this file "
                "was generated -- this section couldn't be checked."
            ),
        ]
    elif not unclaimed:
        lines += ["", "None."]
    else:
        attributed = attributed or {}
        lines += [
            "",
            (
                "Not referenced by any take's `twophoton_match.json` above. "
                '"Filed under" is a preview of where `process` will actually '
                "put it: a specific fly's own `2P_only/` when its name matches "
                "that fly's already-claimed ThorImage prefix (e.g. a Z-stack done "
                "before the paired behavior takes), otherwise the generic "
                "`2p_only/<experiment>/<date>/` bucket -- may be a discarded/test "
                "recording either way."
            ),
            "",
            "| Folder | Kind | Start | Filed under |",
            "|---|---|---|---|",
        ]
        for f in sorted(unclaimed, key=lambda f: f.start_time):
            filed_under = attributed.get(f.path)
            filed = f"{filed_under}/2P_only" if filed_under else "2p_only/ (generic)"
            cut_msg = (cut_offs or {}).get(f.path.name)
            name = f"{f.path.name} (!) cut off" if cut_msg else f.path.name
            if cut_msg:
                warnings.append(f"**{f.path.name}** (unclaimed): {cut_msg}")
            lines.append(f"| {name} | {f.kind} | {fmt_time(f.start_time)} | {filed} |")

    marker = lines.index("<!-- warnings -->")
    lines[marker : marker + 1] = (
        ["## (!) Warnings", "", *[f"- {w}" for w in warnings], ""] if warnings else []
    )
    return "\n".join(lines) + "\n"


def build_match_record(matches: list[TwoPhotonMatch], source_root: Path) -> dict:
    """A JSON-able record of what was matched, for auditability.

    Paths are recorded relative to *source_root* (not absolute) so the record
    stays portable and doesn't leak the share's local mount point. ``gap_s``
    is the start-time gap that decided the match (or ranked it, for a
    verified match); ``end_gap_s`` is informational only -- see
    ``TwoPhotonMatch``.
    """
    return {
        "matched": [
            {
                "path": str(m.folder.path.relative_to(source_root)),
                "kind": m.folder.kind,
                "gap_s": round(m.gap_s, 1),
                "end_gap_s": round(m.end_gap_s, 1),
                "ambiguous": m.ambiguous,
                "confidence": m.confidence,
            }
            for m in matches
        ]
    }


_RECORDING_NUMBER_SUFFIX_RE = re.compile(r"_\d+$")


def _thorimage_base_name(name: str) -> str:
    """Strip a trailing ``_<digits>`` recording-number suffix, e.g.
    ``"Fly1_004"`` -> ``"Fly1"``; ``"Fly1"`` (no ``_<digits>`` at the end,
    just a bare trailing digit that's part of the fly label itself) is
    unchanged, as is ``"Fly1_Zstack"`` (no digit suffix at all).

    Used only to derive a fly's *known* ThorImage prefix from its already-
    claimed matches -- an unclaimed candidate is compared against that prefix
    with a direct ``startswith`` check (see ``cli._attribute_unclaimed_folder``),
    not by computing its own base name, so a same-session folder like
    ``Fly1_Zstack_000`` still correctly matches a known ``Fly1`` prefix even
    though its own base name would be ``Fly1_Zstack``.
    """
    return _RECORDING_NUMBER_SUFFIX_RE.sub("", name)


_FLY_PREFIX_RE = re.compile(r"^(Fly\d+)(?:_|$)")


def _fly_prefix(name: str) -> str | None:
    """The leading ``Fly<N>`` token in *name*, or ``None`` if it doesn't start
    with one.

    ``_thorimage_base_name`` only strips a trailing ``_<digits>`` suffix, so
    it can't unify two folders for the same fly-session when the modality
    word is appended directly to the fly name itself with no digit to strip
    at all (confirmed on real data: a fly-session's own folders can be
    ``Fly1_Streaming``, ``Fly1_Streaming_000``, ``Fly1_Zstack``,
    ``Fly1_FastZ_Test``, ``Fly1_ZT_Test`` -- an open-ended, unenumerable set of
    modality vocabulary, not a suffix pattern). This extracts the fly
    identity itself instead of trying to strip every possible modality word,
    for use as a coarser fallback signal alongside ``_thorimage_base_name``'s
    more precise (modality-preserving) prefix check.
    """
    m = _FLY_PREFIX_RE.match(name)
    return m.group(1) if m else None


def _thorimage_group_key(name: str) -> str:
    """The key used to group same-fly ThorImage folders when there's no
    *existing* claimed fly to attribute against at all (see
    ``cli._promote_generic_2p_only_flies``) -- the leading ``Fly<N>`` token
    when present, else the plain trailing-recording-number-stripped base
    name.

    A real bug found building this: grouping by ``_thorimage_base_name``
    alone can't unify a brand-new fly's *own first-ever* batch when it mixes
    modality words with no shared digit suffix to strip (e.g. ``Fly1_Zstack``
    and ``Fly1_004`` -- the same fly, but ``_thorimage_base_name`` gives
    ``"Fly1_Zstack"`` and ``"Fly1"``, two different keys) -- confirmed via a
    real promotion run that split one such batch into two separate new Fly
    folders. ``_fly_prefix`` already exists as exactly this coarser,
    modality-agnostic signal elsewhere (``_attribute_unclaimed_folder``'s own
    fallback), so grouping prefers it first and only falls back to
    ``_thorimage_base_name`` for a name with no ``Fly<N>`` token at all (a
    sync-correlated or otherwise non-Fly-prefixed name).
    """
    return _fly_prefix(name) or _thorimage_base_name(name)


def _thorimage_detail(name: str) -> str:
    """The modality/detail word(s) in a ThorImage folder name, beyond the fly
    number and any trailing recording-number suffix -- e.g. ``"Zstack"`` for
    ``"Fly1_Zstack"`` or ``"Fly1_Zstack_000"``, ``"FastZ_Test"`` for
    ``"Fly1_FastZ_Test"``, and ``""`` for a plain ``"Fly1"``/``"Fly1_004"``
    (no detail beyond the fly/recording number) or for a name with no leading
    ``Fly<N>`` token at all (e.g. a ``SyncData102`` folder, which carries no
    fly-identifying name to extract a detail from).

    Used to fold a matched 2P folder's own detail into the reconciled
    ``RecordingN_2P``/``RecordingN_Synced`` directory name (see
    ``cli._reconcile_sessions``), so the surface-level name hints at what's
    inside without opening it -- e.g. ``Recording1_2P_Zstack`` instead of a
    bare ``Recording1_2P``.
    """
    base = _thorimage_base_name(name)
    m = _FLY_PREFIX_RE.match(base)
    if m is None:
        return ""
    return base[m.end() :]


def correlate_sync_folder_to_image(
    sync_folder: TwoPhotonFolder,
    image_candidates: list[TwoPhotonFolder],
    tolerance: int = _VERIFY_TOLERANCE,
    window_s: float = 3600.0,
) -> TwoPhotonFolder | None:
    """Find which discovered ThorImage folder *sync_folder* actually paired
    with, purely from the recorded DAQ signal -- no octacam take involved.

    A ``SyncData*`` folder's ``FrameOut`` edge count (per ``CaptureOn``
    segment) is checked against every candidate's own ``Experiment.xml
    <Timelapse timepoints="...">`` (scaled for FastZ, see
    :func:`thorimage_expected_frameout_edges`), within *window_s* of the
    sync folder's own start; an
    exact/near match is decisive, the same signal already used to confirm a
    behavior-take pairing transitively (see ``match_takes_to_twophoton_batch``)
    -- this is that same check with no take in the loop, for a ``SyncData``
    folder that never matched any take at all (e.g. a same-fly Z-stack ThorSync
    happened to be running for). Returns the best (lowest edge-count diff)
    candidate within *tolerance*, or ``None`` (no h5py, no segments, or
    nothing within tolerance) -- never raises.
    """
    segments = read_capture_segments(sync_folder.path)
    if not segments:
        return None
    ranked: list[tuple[int, float, TwoPhotonFolder]] = []
    for candidate in image_candidates:
        gap = abs(candidate.start_time - sync_folder.start_time)
        if gap > window_s:
            continue
        timepoints = thorimage_expected_frameout_edges(
            candidate.path / EXPERIMENT_XML_FILENAME
        )
        if timepoints is None:
            continue
        zstack = thorimage_zstack_signature(candidate.path / EXPERIMENT_XML_FILENAME)
        if zstack is not None:
            # Whole-episode signature: one segment per slice, each with the
            # averaged-frame edge count (see thorimage_zstack_signature).
            slices, per_slice = zstack
            if all(seg.frameout_edges == per_slice for seg in segments):
                diff = abs(len(segments) - slices)
                if diff <= tolerance:
                    ranked.append((diff, gap, candidate))
            continue
        seg_tolerance = max(
            tolerance,
            thorimage_frameout_tolerance(candidate.path / EXPERIMENT_XML_FILENAME),
        )
        for segment in segments:
            diff = frameout_edge_diff(segment, timepoints)
            if diff <= seg_tolerance:
                ranked.append((diff, gap, candidate))
    if not ranked:
        return None
    ranked.sort(key=lambda r: (r[0], r[1]))
    return ranked[0][2]
