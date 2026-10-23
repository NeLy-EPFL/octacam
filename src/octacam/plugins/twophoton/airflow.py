"""airflow_control's logged air commands, put beside a take ThorSync did not
record, in place of the AirFlow channel ThorSync would have held.

airflow_control (the rig's MFC controller, on the PC that runs octacam) logs
every setpoint it writes as one JSON line with the host's wall clock
(`airflow_*.jsonl`): the clock octacam stamps its frames with. It logs only
when something changes, so a constant flow leaves no line during a take: a
session runs from its first line to its shutdown (`stop` lines with a
"shutdown" reason, its last), else, cut short, until the next session starts.
The AirFlow sensor follows those commands closely (2026-10-16: the rebuilt signal gives
the sensor's air on/off on 99.9 % of camera frames; scapepp's
`analysis/dan_screen/airlog.py` does the rebuild). So for a take paired
without ThorSync, `octacam process` copies the commands that cover it into
`octacam_recording/airflow_commands.jsonl`: every line its airflow_control
session logged during the take (plus a second on either side), and that
session's last setpoint of each channel before it, the flow the take started
with.

Set by the twophoton plugin option `airflow_logs` (the log folder).
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path

from octacam.plugins.twophoton.layout import info_file

log = logging.getLogger("octacam")

AIRFLOW_FILENAME = "airflow_commands.jsonl"
LOG_GLOB = "airflow_*.jsonl"
# Around the take: the flow lags its command by up to ~0.4 s.
MARGIN_S = 1.0


def _take_span(take: Path) -> tuple[float, float] | None:
    """The take's first frame and end on the host clock (s), or None. Its
    length is its frames at its fps: `duration_s` is the set duration for a
    take stopped by hand, longer than what ran.
    """
    from octacam.recording_format import read_summary

    try:
        summary = read_summary(take)
    except OSError, ValueError:
        return None
    first = summary.get("first_frame_time_ns") or summary.get("start_time_ns")
    fps = summary.get("fps_target")
    frames = max((c.get("frames") or 0 for c in summary.get("cameras", [])), default=0)
    length = frames / fps if fps and frames else summary.get("duration_s")
    if not first or not length:
        return None
    return first / 1e9, first / 1e9 + float(length)


def _read_log(path: Path) -> list[tuple[float, str, dict]]:
    """(host time, raw line, parsed) for every readable line of one log."""
    rows = []
    try:
        lines = path.read_text().splitlines()
    except OSError as e:
        log.warning("airflow: could not read %s: %s", path, e)
        return rows
    for line in lines:
        try:
            r = json.loads(line)
            t = datetime.fromisoformat(r["t"]).timestamp()
        except ValueError, KeyError, TypeError:
            continue
        rows.append((t, line, r))
    return rows


def _shut_down(row: tuple[float, str, dict]) -> bool:
    r = row[2]
    return r.get("event") == "stop" and str(r.get("reason", "")).startswith("shutdown")


def _sessions(log_dir: Path) -> list[tuple[float, float, list]]:
    """Each airflow_control session as (start, end, rows), oldest first: its
    end is its shutdown, else the next session's start (module doc).
    """
    logs = [
        rows for path in sorted(log_dir.glob(LOG_GLOB)) if (rows := _read_log(path))
    ]
    logs.sort(key=lambda rows: rows[0][0])
    sessions = []
    for i, rows in enumerate(logs):
        if _shut_down(rows[-1]):
            end = rows[-1][0]
        elif i + 1 < len(logs):
            end = logs[i + 1][0][0]
        else:
            end = float("inf")  # still running, or cut short
        sessions.append((rows[0][0], end, rows))
    return sessions


def commands_for(take: Path, log_dir: Path) -> list[str] | None:
    """The log lines that describe *take*'s air (module doc), or None when no
    airflow_control session overlaps it.
    """
    span = _take_span(take)
    if span is None:
        return None
    start, end = span[0] - MARGIN_S, span[1] + MARGIN_S
    covered = False
    before: dict[str, tuple[float, str]] = {}
    during: list[tuple[float, str]] = []
    for first, last, rows in _sessions(log_dir):
        if not (first <= end and start <= last):
            continue  # another session: its flows are not this take's
        covered = True
        for t, line, r in rows:
            if t < start:
                # The flow each channel held when the take started.
                if "setpoint" in r:
                    before[str(r.get("channel"))] = (t, line)
            elif t <= end:
                during.append((t, line))
    if not covered:
        return None
    return [line for _, line in sorted([*before.values(), *during])]


def attach_airflow_commands(
    takes: list[Path], log_dir: Path, *, dry_run: bool = False
) -> list[Path]:
    """Write `airflow_commands.jsonl` beside each of *takes* (paired without
    ThorSync) that an airflow_control session covers. Returns those takes.
    """
    from octacam.files import atomic_write_text

    if not log_dir.is_dir():
        log.warning("airflow: no airflow_control logs at %s", log_dir)
        return []
    done = []
    for take in takes:
        lines = commands_for(take, log_dir)
        if lines is None:
            log.info(
                "airflow: no airflow_control session covers %s; its air is unknown",
                take,
            )
            continue
        path = info_file(take, AIRFLOW_FILENAME)
        log.info(
            "airflow: %s has no ThorSync AirFlow -- %d logged air command(s) from "
            "airflow_control go beside it",
            take,
            len(lines),
        )
        if not dry_run:
            path.parent.mkdir(parents=True, exist_ok=True)
            atomic_write_text(path, "\n".join(lines) + "\n")
        done.append(take)
    return done
