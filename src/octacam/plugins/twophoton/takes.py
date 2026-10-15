"""A behavior take's 2P facts, from either recording layout: whether it was
armed, when its first frame came, what it was paired with, and where it sits
on the destination once renumbered (`RecordingN_*`).
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from octacam.plugins.twophoton.layout import (
    MATCH_FILENAME,
    PENDING_FILENAME,
    TAKE_INFO_FILENAME,
    info_file,
)
from octacam.recording_format import (
    recording_info_dir,
    recording_summary_path,
)

log = logging.getLogger("octacam")


def read_json(path: Path) -> dict | None:
    """*path*'s JSON object, None when absent or unreadable (warned)."""
    try:
        data = json.loads(path.read_text())
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as e:
        log.warning("twophoton: could not read %s: %s", path, e)
        return None
    return data if isinstance(data, dict) else None


def write_json(path: Path, data: dict) -> bool:
    """Write *data* to *path* (its folder created); False when that failed."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=2) + "\n")
    except OSError as e:
        log.warning("twophoton: could not write %s: %s", path, e)
        return False
    return True


def summary_of(take: Path) -> dict:
    return read_json(recording_summary_path(take)) or {}


def sidecar(take: Path, name: str) -> Path:
    """Where *take* keeps (or gets) the 2P sidecar *name*: beside its summary."""
    return recording_info_dir(take) / name


def take_info(take: Path, summary: dict | None = None) -> dict:
    """The plugin's facts about *take* (`twophoton.json`), else an older
    summary's `plugins.twophoton` section; {} when neither.
    """
    info = read_json(info_file(take, TAKE_INFO_FILENAME))
    if info is not None:
        return info
    summary = summary_of(take) if summary is None else summary
    legacy = (summary.get("plugins") or {}).get("twophoton")
    return legacy if isinstance(legacy, dict) else {}


def match_record(take: Path) -> dict:
    """What *take* was paired with (`twophoton_match.json`); {} when nothing."""
    return read_json(info_file(take, MATCH_FILENAME)) or {}


def pending_record(take: Path) -> dict | None:
    return read_json(info_file(take, PENDING_FILENAME))


def is_armed(take: Path, summary: dict) -> bool:
    """Whether *take* was armed for the 2P trigger. A take recorded before
    octacam noted it (summary schema < 4, no plugin facts) may have been:
    pairing is attempted.
    """
    info = take_info(take, summary)
    if "armed" in info:
        return bool(info["armed"])
    return summary.get("schema_version", 0) < 4


def start_s(summary: dict) -> float | None:
    """When Start was pressed (epoch s)."""
    start_ns = summary.get("start_time_ns")
    return start_ns / 1e9 if isinstance(start_ns, (int, float)) else None


def match_start_s(take: Path, summary: dict) -> float | None:
    """When the take's first frame came (epoch s): what pairing compares, as a
    synced take starts on ThorImage's trigger, up to tens of seconds after Start
    (real 2026-10-14: 22 s, past the 5 s window). `first_frame_time_ns` when
    recorded; for an older take that stopped with ThorImage, its summary's write
    time minus the board's run length; else the Start time.
    """
    first_ns = summary.get("first_frame_time_ns")
    if isinstance(first_ns, (int, float)) and first_ns > 0:
        return first_ns / 1e9
    arm_s = start_s(summary)
    run_ms = (take_info(take, summary).get("stop") or {}).get("run_ms")
    if isinstance(run_ms, (int, float)) and run_ms > 0:
        try:
            written = recording_summary_path(take).stat().st_mtime
        except OSError:
            return arm_s
        estimate = written - run_ms / 1000
        # Never before Start (a summary rewritten later).
        return estimate if arm_s is None or estimate >= arm_s else arm_s
    return arm_s


def current_take_dest(dest: Path, start_time_s: float | None) -> Path:
    """Where the take transferred to *dest* is now: *dest*, or the renumbered
    `RecordingN_*` sibling with the same start, so a re-run copies into it
    rather than re-creating *dest*.
    """
    if dest.is_dir() or start_time_s is None or not dest.parent.is_dir():
        return dest
    for candidate in sorted(dest.parent.glob("Recording*")):
        if not recording_summary_path(candidate).is_file():  # a 2P-only session
            continue
        start = start_s(summary_of(candidate))
        if start is not None and abs(start - start_time_s) < 1e-3:
            return candidate
    return dest
