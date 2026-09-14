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

import re
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

# Edge-count tolerance for a signal-verified match (camera/frameout edges vs.
# the take's own recorded frame count / ThorImage's own timepoints) — shared
# by match_takes_to_twophoton_batch and correlate_sync_folder_to_image.
_VERIFY_TOLERANCE = 1


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
    gap_s: float  # start-time gap (see _start_gap_seconds) — the sole gating/ranking criterion
    ambiguous: bool  # more than one same-kind candidate also overlapped
    # "verified": confirmed by reading the actual DAQ signal (edge count
    # matches the take's own recorded frame count / ThorImage's own
    # timepoints) — see twophoton_signals.py. "timestamp": the coarse
    # time-window heuristic only (match_take_to_twophoton), because
    # verification wasn't attempted, wasn't possible (no h5py, no SyncData
    # folder for this session), or found no matching edge count.
    confidence: str = "timestamp"
    # Informational only (see _end_gap_seconds) — never gates a match. Signed;
    # positive means the 2P folder's last write landed after the take ended,
    # expected today since nothing stops octacam when the 2P side finishes.
    end_gap_s: float = 0.0


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


def _start_gap_seconds(take_start: float, candidate: TwoPhotonFolder) -> float:
    """The sole timestamp-tier acceptance/ranking criterion: how far
    *candidate*'s start drifted from the take's own start.

    On this trigger architecture, octacam is armed and then the 2P
    acquisition's own start signals octacam to start recording (via
    ThorSync) — so a genuinely paired take and 2P folder should start within
    a couple of seconds of each other, not tens of seconds. Confirmed on
    real data: real matched pairs across two independent experiments showed
    start diffs of 0.5-9.6s (one 36s outlier on a session's very first
    pairing), regardless of `end`/duration, which routinely differ by tens of
    seconds today — there is no signal that stops octacam when the 2P
    acquisition finishes, so the two ends drift independently (a separate,
    not-yet-implemented fix). See :func:`_end_gap_seconds` for that
    (informational-only) side."""
    return abs(candidate.start_time - take_start)


def _end_gap_seconds(take_end: float, candidate: TwoPhotonFolder) -> float:
    """How far *candidate*'s last write landed from the take's own computed
    end — signed (positive: the 2P folder finished writing after the take
    ended, the expected direction today). Purely informational: octacam and
    the 2P acquisition currently stop independently (no stop signal yet), so
    this is expected to differ and must never gate a match — see
    :func:`_start_gap_seconds` for the one criterion that does."""
    return candidate.last_mtime - take_end


def _overlaps(
    take_start: float, take_end: float, candidate: TwoPhotonFolder, window_s: float
) -> bool:
    """Whether *candidate*'s ``[start_time, last_mtime]`` window overlaps
    ``[take_start, take_end]``, each padded by *window_s* — a loose "is this
    even worth investigating" pre-filter, used only to bound which
    ``SyncData`` folders get opened for signal verification (deliberately
    generous: verification itself, not timing shape, is what confirms or
    rejects the candidate — see match_takes_to_twophoton_batch). Not used for
    the timestamp-only decision itself; see :func:`_start_gap_seconds` for
    that."""
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

    A candidate matches only when its **start** lands within *match_window_s*
    of the take's own start (see :func:`_start_gap_seconds`) — octacam is
    triggered directly by the 2P acquisition's own start (via ThorSync), so a
    genuine pair's start should differ by at most a couple of seconds, not
    "somewhere within a loose window". This is *more* selective than the old
    duration-aware check against the false positive it was built to catch (a
    short, unrelated acquisition nested inside a much longer take): such a
    snapshot's own start essentially never coincidentally lands within a few
    seconds of the take's start, so the tight start window excludes it
    without needing to compare duration at all. End time and duration are
    **not** part of this decision — octacam and the 2P acquisition currently
    stop independently (no stop signal yet, a separate not-yet-implemented
    fix), so they're expected to differ and are only carried informationally
    (:func:`_end_gap_seconds`, ``TwoPhotonMatch.end_gap_s``).

    Returns 0-2 matches — at most one per kind (``sync``/``image``); never
    forces a pairing that isn't there. When more than one same-kind candidate
    matches, the best-aligned (smallest start gap) wins and the match is
    flagged ``ambiguous=True`` so the caller can log/audit rather than
    silently trust it (real 2P sessions were found where ThorSync/ThorImage
    are *not* always started 1:1 with each other or with a single take).

    This is the timestamp-only fallback — see
    :func:`match_takes_to_twophoton_batch` for the verified/no-double-booking
    matcher `octacam process` actually drives."""
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
                # gap only breaks ties among edge-count-tied candidates below
                # — verified confidence never depends on timing shape.
                gap = _start_gap_seconds(take.start_time, c)
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
                        end_gap_s=_end_gap_seconds(take_end, best_folder),
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
                        (diff, _start_gap_seconds(take.start_time, c), c)
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
) -> str:
    """Render a human-readable ``2p_reconciliation.md`` for one day/session
    destination folder: every behavior take transferred that day against its
    matched 2P folder(s) (or "unmatched"), plus every 2P folder discovered on
    the share that day no take claimed — the single legible file this feature
    replaces per-take ``twophoton_match.json`` digging with. Pure/deterministic
    (no filesystem access) so it's cheap to unit test in isolation from the
    disk-scanning caller.

    *takes* — one dict per take: ``{"name": str, "start_time": float,
    "duration_s": float, "armed": bool, "matches": list[dict]}``, where each
    match dict has :func:`build_match_record`'s per-entry shape (``path``,
    ``kind``, ``gap_s``, ``end_gap_s``, ``ambiguous``, ``confidence``).
    *unclaimed* is every still-live 2P folder that day no take's
    ``twophoton_match.json`` references. *source_reachable* — False when the
    live rescan of ``[transfer.twophoton].source`` couldn't happen (share
    unmounted), in which case the unclaimed-folder section is replaced by a
    note instead of silently claiming there's nothing unclaimed. *attributed*
    — ``{folder.path: fly_folder_name}`` for whichever unclaimed folders the
    caller has decided belong to a specific fly (same decision the sweep
    itself makes, shown here purely as a preview — see
    ``cli._attribute_unclaimed_folder``); absent entries default to the
    generic bucket.
    """

    def fmt_time(t: float) -> str:
        return time.strftime("%H:%M:%S", time.localtime(t))

    lines = [
        f"# 2P reconciliation — {day_label}",
        "",
        f"Generated {time.strftime('%Y-%m-%d %H:%M:%S')} — this file is "
        "fully regenerated on every `octacam process` run; hand edits will "
        "be overwritten.",
        "",
        "## Behavior takes",
        "",
        "Start Δ is what decided the match (octacam is triggered directly by "
        "the 2P acquisition's own start, so a real pair's start should agree "
        "to within a couple of seconds). End Δ is informational only, never "
        "used to accept or reject a match — octacam and the 2P side "
        "currently stop independently, so it's expected to differ.",
        "",
        "| Take | Start | Duration | Armed | 2P match | Kind | Confidence | Start Δ | End Δ (info) |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
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
            end_gap = m.get("end_gap_s", 0.0)
            lines.append(
                f"| {prefix} | {m['path']}{flag} | {m['kind']} | "
                f"{m['confidence']} | {m['gap_s']}s | {end_gap:+.1f}s |"
            )

    lines += ["", "## Unclaimed 2P folders on the share"]
    if not source_reachable:
        lines += [
            "",
            "`[transfer.twophoton].source` wasn't reachable when this file "
            "was generated — this section couldn't be checked.",
        ]
    elif not unclaimed:
        lines += ["", "None."]
    else:
        attributed = attributed or {}
        lines += [
            "",
            "Not referenced by any take's `twophoton_match.json` above. "
            "\"Filed under\" is a preview of where `process` will actually "
            "put it: a specific fly's own `2P_only/` when its name matches "
            "that fly's already-claimed ThorImage prefix (e.g. a Z-stack done "
            "before the paired behavior takes), otherwise the generic "
            "`2p_only/<experiment>/<date>/` bucket — may be a discarded/test "
            "recording either way.",
            "",
            "| Folder | Kind | Start | Filed under |",
            "|---|---|---|---|",
        ]
        for f in sorted(unclaimed, key=lambda f: f.start_time):
            filed_under = attributed.get(f.path)
            filed = f"{filed_under}/2P_only" if filed_under else "2p_only/ (generic)"
            lines.append(f"| {f.path.name} | {f.kind} | {fmt_time(f.start_time)} | {filed} |")

    return "\n".join(lines) + "\n"


def build_match_record(
    matches: list[TwoPhotonMatch], source_root: Path
) -> dict:
    """A JSON-able record of what was matched, for auditability.

    Paths are recorded relative to *source_root* (not absolute) so the record
    stays portable and doesn't leak the share's local mount point. ``gap_s``
    is the start-time gap that decided the match (or ranked it, for a
    verified match); ``end_gap_s`` is informational only — see
    ``TwoPhotonMatch``."""
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
    claimed matches — an unclaimed candidate is compared against that prefix
    with a direct ``startswith`` check (see ``cli._attribute_unclaimed_folder``),
    not by computing its own base name, so a same-session folder like
    ``Fly1_Zstack_000`` still correctly matches a known ``Fly1`` prefix even
    though its own base name would be ``Fly1_Zstack``."""
    return _RECORDING_NUMBER_SUFFIX_RE.sub("", name)


def correlate_sync_folder_to_image(
    sync_folder: TwoPhotonFolder,
    image_candidates: list[TwoPhotonFolder],
    tolerance: int = _VERIFY_TOLERANCE,
) -> TwoPhotonFolder | None:
    """Find which discovered ThorImage folder *sync_folder* actually paired
    with, purely from the recorded DAQ signal — no octacam take involved.

    A ``SyncData*`` folder's ``FrameOut`` edge count (per ``CaptureOn``
    segment) is checked against every candidate's own ``Experiment.xml
    <Timelapse timepoints="...">`` (:func:`thorimage_timepoints`); an
    exact/near match is decisive, the same signal already used to confirm a
    behavior-take pairing transitively (see ``match_takes_to_twophoton_batch``)
    — this is that same check with no take in the loop, for a ``SyncData``
    folder that never matched any take at all (e.g. a same-fly Z-stack ThorSync
    happened to be running for). Returns the best (lowest edge-count diff)
    candidate within *tolerance*, or ``None`` (no h5py, no segments, or
    nothing within tolerance) — never raises."""
    segments = read_capture_segments(sync_folder.path)
    if not segments:
        return None
    ranked: list[tuple[int, TwoPhotonFolder]] = []
    for candidate in image_candidates:
        timepoints = thorimage_timepoints(candidate.path / EXPERIMENT_XML_FILENAME)
        if timepoints is None:
            continue
        for segment in segments:
            diff = frameout_edge_diff(segment, timepoints)
            if diff <= tolerance:
                ranked.append((diff, candidate))
    if not ranked:
        return None
    ranked.sort(key=lambda r: r[0])
    return ranked[0][1]
