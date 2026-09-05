"""Discover, settle-check, and pair ThorSync/ThorImage folders with octacam takes.

Investigation against a real 2-photon rig found the legacy shell script's
assumption — a sibling ``2p``/``behData`` folder pair at an identical relative
path — no longer holds: ThorSync/ThorImage write flat, auto-incrementing
folders (``SyncData102``, ``Fly1_004``, ...) directly under an experiment
folder on the Windows share, completely decoupled from octacam's own
``<date>_/<Fly>/<take>`` naming. The only thing that reliably correlates a
behavior take with its 2P counterpart is wall-clock time overlap.

Two folder kinds are recognized, distinguished structurally rather than by a
fixed naming scheme (ThorImage's own folder name is operator-chosen and
varies: ``Fly1``, ``Fly1_001``, ``Test1_005``, ...):

* **ThorSync** — a folder named ``SyncData<N>`` holding ``Episode001.h5`` (the
  analog/digital trace) + ``ThorRealTimeDataSettings.xml`` (a settings
  snapshot written once at start). Its own directory mtime is a decent start
  proxy: only those two files are ever created in it, so nothing bumps the
  directory's mtime again after the initial write.
* **ThorImage** — any folder containing an ``Experiment.xml``. Its directory
  mtime is *not* a usable start proxy (thousands of per-frame ``.tif`` files
  keep bumping it throughout the whole capture); ``Experiment.xml``'s own
  ``<Date uTime="...">`` attribute (a Unix timestamp) is used instead, with
  the directory mtime as a last-resort fallback if that's unparseable.

Neither folder type has a definitive "acquisition complete" marker beyond
mtime quiescence — this module's ``is_settled`` is a direct port of the old
script's ``find_last_file_access_time`` idea.

This module never touches ThorSync/ThorImage source files' *content*, is
config-agnostic (callers resolve any ``%``-templated source path before
calling in), and does not perform any file transfer itself — see
``transfer.transfer_tree`` for that.
"""

from __future__ import annotations

import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path

from octacam.twophoton_signals import (
    CaptureSegment,
    camera_edge_diff,
    frameout_edge_diff,
    read_capture_segments,
)

EXPERIMENT_XML_FILENAME = "Experiment.xml"
SYNC_FOLDER_PREFIX = "SyncData"


@dataclass(frozen=True)
class TwoPhotonFolder:
    """One discovered ThorSync/ThorImage take folder."""

    path: Path
    kind: str  # "sync" | "image"
    start_time: float  # unix seconds, best-effort
    last_mtime: float  # max mtime across every file in the folder, recursively


@dataclass(frozen=True)
class TwoPhotonMatch:
    """One folder matched to a take, with enough context to audit the pairing."""

    folder: TwoPhotonFolder
    gap_s: float  # time gap between the take's and the folder's windows
    ambiguous: bool  # more than one same-kind candidate also overlapped
    # "verified": confirmed by reading the actual DAQ signal (edge count
    # matches the take's own recorded frame count / ThorImage's own
    # timepoints) — see twophoton_signals.py. "timestamp": the coarse
    # time-window heuristic only (match_take_to_twophoton), because
    # verification wasn't attempted, wasn't possible (no h5py, no SyncData
    # folder for this session), or found no matching edge count.
    confidence: str = "timestamp"


@dataclass(frozen=True)
class TakeInfo:
    """One behavior take, as much as the matcher needs to know about it."""

    folder: Path  # the local recording folder (used only as a dict key)
    start_time: float  # unix seconds (recording_summary.json's start_time_ns)
    duration_s: float
    camera_frame_counts: list[int]  # per-camera recorded frame counts


def _thorimage_start_time(experiment_xml: Path) -> float | None:
    """Parse ``<Date uTime="...">`` from a ThorImage ``Experiment.xml``, or
    None if the file is missing, unparseable, or lacks that attribute."""
    try:
        root = ET.parse(experiment_xml).getroot()
    except (ET.ParseError, OSError):
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


def thorimage_timepoints(experiment_xml: Path) -> int | None:
    """Parse ``<Timelapse timepoints="...">`` from a ThorImage ``Experiment.xml``
    — the acquisition's own recorded frame count, for cross-checking against a
    verified segment's ``FrameOut`` edge count. None if missing/unparseable."""
    try:
        root = ET.parse(experiment_xml).getroot()
    except (ET.ParseError, OSError):
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


def _folder_last_mtime(folder: Path) -> float:
    """Max ``st_mtime`` across every file under *folder*, recursively.

    Mirrors ``move_files.sh``'s ``find_last_file_access_time``. Only files'
    own mtimes are considered, not directory mtimes: a directory's mtime only
    updates when an entry is added/removed, which understates a file that
    keeps growing without any new directory entries (e.g. ThorSync's
    ``Episode001.h5``, appended to for minutes after its one creation).
    Falls back to the folder's own mtime only if it somehow contains no files
    (shouldn't happen — discovery already requires at least one)."""
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


def discover_twophoton_folders(source_root: Path) -> list[TwoPhotonFolder]:
    """Find ThorSync/ThorImage take folders one level under each experiment
    folder in *source_root* (e.g. ``source_root/MB247_CI63/SyncData102``).

    *source_root* itself does not exist or isn't a directory → returns an
    empty list rather than raising (the share may not be mounted yet)."""
    found: list[TwoPhotonFolder] = []
    if not source_root.is_dir():
        return found
    for experiment_dir in sorted(p for p in source_root.iterdir() if p.is_dir()):
        try:
            candidates = sorted(p for p in experiment_dir.iterdir() if p.is_dir())
        except OSError:
            continue
        for candidate in candidates:
            experiment_xml = candidate / EXPERIMENT_XML_FILENAME
            if experiment_xml.is_file():
                kind = "image"
                start = _thorimage_start_time(experiment_xml)
                if start is None:
                    start = candidate.stat().st_mtime
            elif candidate.name.startswith(SYNC_FOLDER_PREFIX):
                kind = "sync"
                start = candidate.stat().st_mtime
            else:
                continue
            found.append(
                TwoPhotonFolder(candidate, kind, start, _folder_last_mtime(candidate))
            )
    return found


def is_settled(folder: TwoPhotonFolder, settle_s: float, now: float | None = None) -> bool:
    """Whether *folder* has been mtime-quiescent for at least *settle_s*.

    Neither ThorSync nor ThorImage leaves a definitive "done" marker (see
    module docstring) — this is the best available signal, same idea as the
    legacy script's delay-based check."""
    now = time.time() if now is None else now
    return (now - folder.last_mtime) >= settle_s


def _gap_seconds(take_start: float, take_end: float, candidate: TwoPhotonFolder) -> float:
    """How far *candidate* is from being a genuinely-paired acquisition: the
    worst of (a) how far its start drifted from the take's start, (b) how far
    its end drifted from the take's end, and (c) how much its own total
    duration differs from the take's. A proper behavior/2P pair runs for
    essentially the same length of time — anything that doesn't is far more
    likely a standalone check/tuning/2P-only recording (safe to leave for the
    2P-only sweep, see --twophoton-sweep) than a real pair. (a)+(b) alone
    isn't enough: a much *longer* candidate can loosely straddle a short take
    with both endpoints individually "close enough" while being nowhere near
    the same duration. Deliberately not "0 whenever the two windows merely
    overlap" — real data found a short, unrelated 2P snapshot nested entirely
    inside a much longer behavior take (ThorImage-only sessions running
    17-33s of a 129s take, confirmed on a real rig with no ThorSync data to
    verify against) satisfies a plain overlap check while being a spurious
    match."""
    take_duration = take_end - take_start
    candidate_duration = candidate.last_mtime - candidate.start_time
    return max(
        abs(candidate.start_time - take_start),
        abs(candidate.last_mtime - take_end),
        abs(candidate_duration - take_duration),
    )


def _overlaps(
    take_start: float, take_end: float, candidate: TwoPhotonFolder, window_s: float
) -> bool:
    """Whether *candidate*'s ``[start_time, last_mtime]`` window overlaps
    ``[take_start, take_end]``, each padded by *window_s* — a loose "is this
    even worth investigating" pre-filter, used only to bound which
    ``SyncData`` folders get opened for signal verification (deliberately
    generous: verification itself, not timing shape, is what confirms or
    rejects the candidate — see match_takes_to_twophoton_batch). Not used for
    the timestamp-only decision itself; see :func:`_gap_seconds` for that."""
    return not (
        candidate.start_time > take_end + window_s
        or candidate.last_mtime < take_start - window_s
    )


def match_take_to_twophoton(
    take_start: float,
    take_duration_s: float,
    candidates: list[TwoPhotonFolder],
    match_window_s: float,
) -> list[TwoPhotonMatch]:
    """Match a behavior take's time window against discovered 2P folders.

    A candidate matches only when its start, its end, AND its own overall
    duration each land within *match_window_s* of the take's corresponding
    value (see :func:`_gap_seconds`) — not merely "the two windows touch
    somewhere". ThorSync/ThorImage are started independently by the operator,
    so a few seconds to tens of seconds of drift is normal and allowed; what
    this rules out is a short, unrelated acquisition (a focus check, an ROI
    tune, a calibration snapshot) that just happens to fall within a much
    longer take's window without actually running for anything like the same
    length of time — a proper behavior/2P pair should have matching overall
    durations, not just overlapping windows.

    Returns 0-2 matches — at most one per kind (``sync``/``image``); never
    forces a pairing that isn't there. When more than one same-kind candidate
    matches, the best-aligned (smallest `_gap_seconds`) wins and the match is
    flagged ``ambiguous=True`` so the caller can log/audit rather than
    silently trust it (real 2P sessions were found where ThorSync/ThorImage
    are *not* always started 1:1 with each other or with a single take).

    This is the timestamp-only fallback — see
    :func:`match_takes_to_twophoton_batch` for the verified/no-double-booking
    matcher `octacam process` actually drives."""
    take_end = take_start + take_duration_s

    by_kind: dict[str, list[tuple[float, TwoPhotonFolder]]] = {}
    for c in candidates:
        gap = _gap_seconds(take_start, take_end, c)
        if gap > match_window_s:
            continue
        by_kind.setdefault(c.kind, []).append((gap, c))

    matches: list[TwoPhotonMatch] = []
    for kind_matches in by_kind.values():
        kind_matches.sort(key=lambda item: item[0])
        best_gap, best_folder = kind_matches[0]
        matches.append(
            TwoPhotonMatch(
                folder=best_folder, gap_s=best_gap, ambiguous=len(kind_matches) > 1
            )
        )
    return matches


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
       per-camera frame count. An exact/near match is decisive — used even if
       its coarse timestamp gap exceeds *match_window_s* — and claims that
       *segment* specifically (not the whole folder), so a ``SyncData`` folder
       spanning multiple takes can still verify-match a different segment
       against a different take. Within that same verified segment, an
       ``image``-kind candidate whose own ``Experiment.xml`` ``timepoints``
       matches the segment's ``FrameOut`` edge count is the transitively
       verified image match.
    2. **Timestamp** (``confidence="timestamp"``): whatever tier 1 didn't
       resolve falls back to :func:`match_take_to_twophoton`, run only
       against the pool of folders no earlier take in this batch has already
       claimed — this is what actually removes the double-booking that a
       real test found producing most `ambiguous` flags, using nothing but
       timestamps already available today.

    *verify_with_signals* off (or ``h5py`` unavailable, or no ``SyncData``
    folder for this session at all — the common no-ThorSync case) skips
    straight to tier 2 for every take.
    """
    ordered = sorted(takes, key=lambda t: t.start_time)
    claimed_folders: set[Path] = set()
    claimed_segments: set[tuple[Path, int]] = set()
    segment_cache: dict[Path, list[CaptureSegment] | None] = {}
    timepoints_cache: dict[Path, int | None] = {}
    result: dict[Path, list[TwoPhotonMatch]] = {}

    _VERIFY_TOLERANCE = 1

    for take in ordered:
        take_end = take.start_time + take.duration_s
        matches: list[TwoPhotonMatch] = []
        verified_segment_key: tuple[Path, int] | None = None

        if verify_with_signals:
            # Rank every (folder, segment) within tolerance rather than
            # taking the first one found: real data has more than one
            # SyncData folder land within a frame or two of a take's count
            # when nearby takes ran similar durations — the exact match is
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
                gap = _gap_seconds(take.start_time, take_end, c)
                for idx, seg in enumerate(segments):
                    if (c.path, idx) in claimed_segments:
                        continue
                    diff = camera_edge_diff(seg, take.camera_frame_counts)
                    if diff is not None and diff <= _VERIFY_TOLERANCE:
                        ranked.append((diff, gap, c, idx))
            if ranked:
                ranked.sort(key=lambda r: (r[0], r[1]))
                best_diff, best_gap, best_folder, best_idx = ranked[0]
                tied = sum(
                    1 for d, g, *_ in ranked if d == best_diff and g == best_gap
                )
                matches.append(
                    TwoPhotonMatch(
                        folder=best_folder,
                        gap_s=best_gap,
                        ambiguous=tied > 1,
                        confidence="verified",
                    )
                )
                verified_segment_key = (best_folder.path, best_idx)

        if verified_segment_key is not None:
            claimed_segments.add(verified_segment_key)
            sync_path, seg_idx = verified_segment_key
            sync_segments = segment_cache[sync_path]
            assert sync_segments is not None  # guaranteed: we just matched a segment in it
            segment = sync_segments[seg_idx]
            image_ranked: list[tuple[int, float, TwoPhotonFolder]] = []
            for c in candidates:
                if c.kind != "image" or c.path in claimed_folders:
                    continue
                if c.path not in timepoints_cache:
                    timepoints_cache[c.path] = thorimage_timepoints(
                        c.path / EXPERIMENT_XML_FILENAME
                    )
                timepoints = timepoints_cache[c.path]
                if timepoints is None:
                    continue
                diff = frameout_edge_diff(segment, timepoints)
                if diff <= _VERIFY_TOLERANCE:
                    image_ranked.append(
                        (diff, _gap_seconds(take.start_time, take_end, c), c)
                    )
            if image_ranked:
                image_ranked.sort(key=lambda r: (r[0], r[1]))
                best_diff, best_gap, best_folder = image_ranked[0]
                tied = sum(1 for d, g, _ in image_ranked if d == best_diff and g == best_gap)
                matches.append(
                    TwoPhotonMatch(
                        folder=best_folder,
                        gap_s=best_gap,
                        ambiguous=tied > 1,
                        confidence="verified",
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


def build_match_record(
    matches: list[TwoPhotonMatch], source_root: Path
) -> dict:
    """A JSON-able record of what was matched, for auditability.

    Paths are recorded relative to *source_root* (not absolute) so the record
    stays portable and doesn't leak the share's local mount point."""
    return {
        "matched": [
            {
                "path": str(m.folder.path.relative_to(source_root)),
                "kind": m.folder.kind,
                "gap_s": round(m.gap_s, 1),
                "ambiguous": m.ambiguous,
                "confidence": m.confidence,
            }
            for m in matches
        ]
    }
