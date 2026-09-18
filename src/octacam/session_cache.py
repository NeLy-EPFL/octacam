"""Recording-session cache for ``octacam transcode``.

Each finished recording's save directory is appended to a small JSONL under the
user cache dir (``~/.cache/octacam/recordings.jsonl`` by default) so that, later,
``octacam transcode`` can target the *last* recording, the *last GUI session*, or
*every* recording without the operator re-typing paths.

Design notes:

- One line per recording: ``{"folder", "time", "session", "kind"}``. ``time`` is a
  timezone-aware local ISO timestamp; ``session`` groups every recording made by a
  single ``octacam gui``/``record`` process; ``kind`` is informational
  ("gui"/"record").
- The file is rewritten atomically on every write, dropping entries older than
  :data:`RETENTION_DAYS`, so it never grows without bound ("clear the cache when it
  is no longer needed"). Concurrent writers are serialized with an flock — the
  rig's instance lock already stops two GUIs sharing one config, but two different
  rigs may run at once.
- Queries skip folders that have since been deleted (recordings are routinely
  moved or cleaned up between capture and transcoding), so a stale entry is simply
  ignored, never an error.
"""

from __future__ import annotations

import datetime
import fcntl
import json
import logging
import os
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

log = logging.getLogger("octacam")

CACHE_FILENAME = "recordings.jsonl"
LOCK_FILENAME = "recordings.lock"
# Keep a month of history: enough to still find "the last session" after a gap,
# while bounding the file to a few hundred tiny lines even on a busy rig.
RETENTION_DAYS = 30
# A running `octacam transcode` publishes an flock-held marker here so a `gui`/
# `record` launch can warn about the CPU contention. A marker the checker can
# lock is orphaned (the transcode crashed); once clearly old it is swept away.
TRANSCODE_DIR_NAME = "transcode-active"
# A `gui`/`record` publishes an flock-held marker here while it owns the cameras;
# `octacam process` pauses between work units while one is live.
CAPTURE_DIR_NAME = "capture-active"
_STALE_MARKER_AGE_S = 60.0


def cache_dir() -> Path:
    """The octacam cache directory.

    Honors ``OCTACAM_CACHE_DIR`` (used by tests and to relocate it), then
    ``XDG_CACHE_HOME``, falling back to ``~/.cache/octacam``.
    """
    override = os.environ.get("OCTACAM_CACHE_DIR")
    if override:
        return Path(override).expanduser()
    xdg = os.environ.get("XDG_CACHE_HOME")
    base = Path(xdg).expanduser() if xdg else Path.home() / ".cache"
    return base / "octacam"


def _cache_file() -> Path:
    return cache_dir() / CACHE_FILENAME


def new_session_id() -> str:
    """A unique id grouping all recordings made by one octacam process."""
    stamp = datetime.datetime.now().strftime("%Y%m%dT%H%M%S")
    return f"{stamp}-{os.getpid()}"


def _now() -> datetime.datetime:
    """The current local, timezone-aware time."""
    return datetime.datetime.now().astimezone()


@contextmanager
def _locked() -> Iterator[None]:
    """Hold an exclusive flock for the duration of a read-modify-write.

    Best-effort: if the lock file cannot be opened (e.g. an unwritable cache
    dir) we proceed without locking rather than failing — the cache is a
    convenience, never a hard dependency.
    """
    directory = cache_dir()
    handle = None
    try:
        directory.mkdir(parents=True, exist_ok=True)
        handle = open(directory / LOCK_FILENAME, "a+")
    except OSError as e:
        log.debug("Recording cache lock unavailable (%s); proceeding unlocked", e)
        yield
        return
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()


def _parse_time(value: object) -> datetime.datetime | None:
    """Parse a stored ISO timestamp into an aware datetime, or None."""
    if not isinstance(value, str):
        return None
    try:
        dt = datetime.datetime.fromisoformat(value)
    except ValueError:
        return None
    return dt if dt.tzinfo is not None else dt.astimezone()


def _read_entries() -> list[dict]:
    """All valid entries in file (chronological) order; bad lines are skipped.

    Tolerant by design: a half-written final line from a crashed writer, or a
    line from a future schema, is ignored rather than raising.
    """
    try:
        text = _cache_file().read_text()
    except OSError:
        return []
    entries: list[dict] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if isinstance(entry, dict) and entry.get("folder"):
            entries.append(entry)
    return entries


def _write_entries(entries: list[dict]) -> None:
    """Atomically replace the cache file with ``entries`` (temp + os.replace).

    The temp file name is unique per writer (pid + random) so that even on the
    degraded unlocked path (lock file unopenable) two concurrent writers never
    rename the same temp out from under each other.
    """
    path = _cache_file()
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    try:
        tmp.write_text("".join(json.dumps(e) + "\n" for e in entries))
        os.replace(tmp, path)
    except OSError:
        try:
            tmp.unlink(missing_ok=True)  # don't leave a half-written temp behind
        except OSError:
            pass
        raise


def record_recording(
    folder: str | Path,
    session_id: str,
    kind: str = "gui",
    *,
    retention_days: int = RETENTION_DAYS,
) -> None:
    """Note that ``folder`` was just recorded, pruning anything past retention.

    Best-effort: a cache failure is logged but never raised, so it cannot
    disturb recording teardown. The whole file is rewritten under the lock, so
    the prune and the append are one atomic update.
    """
    entry = {
        "folder": str(Path(folder).resolve()),
        "time": _now().isoformat(),
        "session": session_id,
        "kind": kind,
    }
    try:
        with _locked():
            cutoff = _now() - datetime.timedelta(days=retention_days)
            kept = [
                e
                for e in _read_entries()
                if (t := _parse_time(e.get("time"))) is not None and t >= cutoff
            ]
            kept.append(entry)
            _write_entries(kept)
    except Exception as e:
        # Best-effort by contract: a cache failure (I/O, a clock that makes
        # timedelta overflow, anything) must never propagate into recording
        # teardown.
        log.warning("Could not update the recording cache: %s", e)


def _existing(folders: list[Path]) -> list[Path]:
    """Deduplicate (preserving order) and drop folders that no longer exist."""
    out: list[Path] = []
    seen: set[str] = set()
    for folder in folders:
        key = str(folder)
        if key in seen:
            continue
        seen.add(key)
        if folder.exists():
            out.append(folder)
        else:
            log.debug("Ignoring cached recording folder (no longer exists): %s", folder)
    return out


def _latest_session_id(entries: list[dict]) -> str | None:
    """The session id of the most recent entry that has one."""
    for entry in reversed(entries):
        session = entry.get("session")
        if session:
            return session
    return None


def last_folder() -> Path | None:
    """The most recent recording folder that still exists, or None."""
    for entry in reversed(_read_entries()):
        folder = Path(entry["folder"])
        if folder.exists():
            return folder
    return None


def session_folders(session_id: str | None = None) -> list[Path]:
    """Existing folders from one session (the most recent one when ``None``).

    Folders are returned in the order they were recorded, deduplicated, with any
    since-deleted ones dropped.
    """
    entries = _read_entries()
    if session_id is None:
        session_id = _latest_session_id(entries)
    if session_id is None:
        return []
    folders = [Path(e["folder"]) for e in entries if e.get("session") == session_id]
    return _existing(folders)


def all_folders() -> list[Path]:
    """Every existing recording folder in the cache, in record order.

    Spans every session and day the cache still holds (pruned to the last
    :data:`RETENTION_DAYS` on each write). Deduplicated, with any since-deleted
    folders dropped, so a stale entry is silently ignored, never an error.
    """
    return _existing([Path(entry["folder"]) for entry in _read_entries()])


# ---------------------------------------------------------------------------
# Cache maintenance (backing `octacam cache info` / `octacam cache clear`)
# ---------------------------------------------------------------------------


def dir_size(path: Path) -> int:
    """Total size in bytes of a file or directory tree (best-effort; 0 on error).

    Does not follow directory symlinks (``os.walk`` default), so a marker or log
    pointing outside the tree can't inflate the total.
    """
    try:
        if path.is_file():
            return path.stat().st_size
        if not path.exists():
            return 0
    except OSError:
        return 0
    total = 0
    for root, _dirs, files in os.walk(path):
        for name in files:
            try:
                total += (Path(root) / name).stat().st_size
            except OSError:
                continue
    return total


def recordings_count() -> int:
    """How many recording entries the cache currently holds."""
    return len(_read_entries())


def clear_recordings() -> bool:
    """Delete the recording list (and any crashed-writer temp). Returns True if it existed.

    Held under the recordings lock so a concurrent writer isn't caught mid-rewrite.
    The now-idle lock file is removed afterwards, outside the lock (best-effort; it
    self-recreates on the next write) — so the fd we hold is never the one unlinked.
    """
    path = _cache_file()
    directory = cache_dir()
    removed = False
    with _locked():
        try:
            if path.exists():
                path.unlink()
                removed = True
        except OSError as e:
            log.debug("Could not remove the recording cache %s: %s", path, e)
        try:
            for tmp in directory.glob(f".{CACHE_FILENAME}.*.tmp"):
                tmp.unlink(missing_ok=True)
        except OSError:
            pass
    try:
        (directory / LOCK_FILENAME).unlink(missing_ok=True)
    except OSError:
        pass
    return removed


# ---------------------------------------------------------------------------
# Activity markers (transcode + capture)
#
# Two flock-held marker namespaces under the cache dir signal cross-process
# liveness with no PID bookkeeping — the OS drops an flock on exit or crash, so a
# marker whose lock we *can* take is dead (and, once clearly old, swept away):
#
# - ``transcode-active`` is published by a running ``octacam process`` and read
#   by a ``gui``/``record`` launch, which warns about the CPU contention.
# - ``capture-active`` is published by ``gui``/``record`` while they own the
#   cameras and read by ``octacam process``, which parks between work units until
#   it clears — so a detached job never fights a live recording for CPU/GPU/disk.
# ---------------------------------------------------------------------------


def _transcode_dir() -> Path:
    return cache_dir() / TRANSCODE_DIR_NAME


def _capture_dir() -> Path:
    return cache_dir() / CAPTURE_DIR_NAME


@contextmanager
def _mark_active(directory: Path, detail: str) -> Iterator[None]:
    """Publish an flock-held marker under ``directory`` for the block's lifetime.

    Best-effort: if the marker cannot be created (e.g. an unwritable cache dir)
    the block runs without it rather than failing.
    """
    handle = None
    path = None
    try:
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{os.getpid()}-{uuid.uuid4().hex}.lock"
        handle = open(path, "a+")
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        handle.seek(0)
        handle.truncate()
        handle.write(f"{os.getpid()} {detail}\n")
        handle.flush()
    except OSError as e:
        log.debug("Could not publish an activity marker in %s (%s); continuing", directory, e)
        if handle is not None:
            handle.close()
            handle = None
    try:
        yield
    finally:
        if handle is not None:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            finally:
                handle.close()
        if path is not None:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass


@contextmanager
def mark_transcode_active(detail: str = "") -> Iterator[None]:
    """Publish an flock-held marker for the lifetime of a transcode run."""
    with _mark_active(_transcode_dir(), detail):
        yield


@contextmanager
def mark_capture_active(detail: str = "") -> Iterator[None]:
    """Publish an flock-held marker while a gui/record owns the cameras."""
    with _mark_active(_capture_dir(), detail):
        yield


def _maybe_remove_stale(marker: Path) -> None:
    """Remove an unlocked marker once it is clearly past any publish window."""
    try:
        age = _now().timestamp() - marker.stat().st_mtime
    except OSError:
        return
    if age > _STALE_MARKER_AGE_S:
        try:
            marker.unlink(missing_ok=True)
        except OSError:
            pass


def _count_live_markers(directory: Path) -> int:
    """How many flock-held markers in ``directory`` are still live.

    A marker whose flock we can take is orphaned (its publisher exited or
    crashed); it is ignored and, once stale, swept away.
    """
    try:
        markers = [p for p in directory.iterdir() if p.suffix == ".lock"]
    except OSError:
        return 0
    active = 0
    for marker in markers:
        try:
            # Read-only: the handle is used only for flock (advisory, independent
            # of the access mode), so this needs only the read bit — a marker
            # owned by another user on a shared rig is still detectable. "r" also
            # refuses to create, so a marker unlinked between iterdir and here is
            # skipped (FileNotFoundError) rather than re-created as an empty file.
            handle = open(marker)
        except OSError:
            continue
        try:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                active += 1  # still held -> a live publisher
                continue
            # We took the lock, so nobody owns it: orphaned (or mid-publish).
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            _maybe_remove_stale(marker)
        finally:
            handle.close()
    return active


def _sweep_markers(directory: Path) -> tuple[int, int]:
    """Remove clearly-orphaned markers in ``directory``; return (removed, live_kept).

    Mirrors :func:`_count_live_markers`, but for an explicit cache clear. A marker
    whose flock is still held signals a live publisher and is left alone (counted
    in ``live_kept``). An orphaned (lock-takeable) marker is removed only once it
    is past the mid-publish window (:data:`_STALE_MARKER_AGE_S`) — the same
    conservative rule the liveness checks use — so a clear can never delete a
    marker a just-armed capture/transcode is still holding open but hasn't locked.
    """
    try:
        markers = [p for p in directory.iterdir() if p.suffix == ".lock"]
    except OSError:
        return 0, 0
    removed = live = 0
    for marker in markers:
        try:
            handle = open(marker)
        except OSError:
            continue
        held = False
        try:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                held = True
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()
        if held:
            live += 1
            continue
        try:
            age = _now().timestamp() - marker.stat().st_mtime
        except OSError:
            continue
        if age > _STALE_MARKER_AGE_S:
            try:
                marker.unlink(missing_ok=True)
                removed += 1
            except OSError:
                pass
    return removed, live


def sweep_orphan_markers() -> tuple[int, int]:
    """Sweep clearly-orphaned transcode/capture markers; return (removed, live_kept).

    Summed across both marker namespaces. Used by ``octacam cache clear`` to tidy
    markers left by crashed runs while never disturbing a currently-live capture
    or transcode.
    """
    removed = live = 0
    for directory in (_transcode_dir(), _capture_dir()):
        r, held = _sweep_markers(directory)
        removed += r
        live += held
    return removed, live


def transcode_running() -> int:
    """How many `octacam process` runs are transcoding on this machine right now."""
    return _count_live_markers(_transcode_dir())


def capture_running() -> int:
    """How many gui/record captures currently own cameras on this machine.

    Usually 0 or 1, but two different rigs can capture at once (the instance lock
    only stops two octacams sharing one config), so this is a count, not a flag.
    """
    return _count_live_markers(_capture_dir())


def capture_active() -> bool:
    """True while a gui/record on this machine currently owns the cameras.

    ``octacam process`` polls this at each work-unit boundary and pauses while it
    holds, so a detached job never contends with a live recording.
    """
    return capture_running() > 0
