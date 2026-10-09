"""The recording cache behind `octacam process --last/--all`, and the
cross-process activity markers.

Each finished recording is one line of `recordings.jsonl` in the cache dir:
`{"folder", "time", "session", "kind"}`, where `session` groups one
`gui`/`record` process. Every write rewrites the file atomically under an
flock (two rigs may record at once) and drops entries past
`RETENTION_DAYS`. Queries skip folders deleted since.
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

from octacam.files import atomic_write_text, flock_held

log = logging.getLogger("octacam")

CACHE_FILENAME = "recordings.jsonl"
LOCK_FILENAME = "recordings.lock"
RETENTION_DAYS = 30
# Activity marker directories (see "Activity markers" below).
TRANSCODE_DIR_NAME = "transcode-active"
CAPTURE_DIR_NAME = "capture-active"
_STALE_MARKER_AGE_S = 60.0


def cache_dir() -> Path:
    """`OCTACAM_CACHE_DIR`, else `$XDG_CACHE_HOME/octacam`, else
    `~/.cache/octacam`.
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
    # Local time, as the rig's folder names use.
    stamp = datetime.datetime.now().strftime("%Y%m%dT%H%M%S")  # noqa: DTZ005
    return f"{stamp}-{os.getpid()}"


def _now() -> datetime.datetime:
    """The current local, timezone-aware time."""
    return datetime.datetime.now().astimezone()


@contextmanager
def _locked() -> Iterator[None]:
    """Hold the cache's flock for a read-modify-write; without a lock file (an
    unwritable cache dir) proceed unlocked: the cache is a convenience.
    """
    directory = cache_dir()
    handle = None
    try:
        directory.mkdir(parents=True, exist_ok=True)
        # The handle outlives this block: it holds the lock.
        handle = open(directory / LOCK_FILENAME, "a+")  # noqa: SIM115
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
    """All valid entries in recorded order; a malformed line (a crashed writer's
    partial line) is skipped.
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
    """Atomically replace the cache file with `entries` (safe unlocked too)."""
    atomic_write_text(_cache_file(), "".join(json.dumps(e) + "\n" for e in entries))


def record_recording(folder: str | Path, session_id: str, kind: str = "gui") -> None:
    """Note that `folder` was just recorded, pruning entries past retention.
    Never raises: a cache failure must not disturb recording teardown.
    """
    entry = {
        "folder": str(Path(folder).resolve()),
        "time": _now().isoformat(),
        "session": session_id,
        "kind": kind,
    }
    try:
        with _locked():
            cutoff = _now() - datetime.timedelta(days=RETENTION_DAYS)
            kept = [
                e
                for e in _read_entries()
                if (t := _parse_time(e.get("time"))) is not None and t >= cutoff
            ]
            kept.append(entry)
            _write_entries(kept)
    except Exception as e:
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
    """Existing folders of one session (the latest when `None`), in recorded
    order.
    """
    entries = _read_entries()
    if session_id is None:
        session_id = _latest_session_id(entries)
    if session_id is None:
        return []
    folders = [Path(e["folder"]) for e in entries if e.get("session") == session_id]
    return _existing(folders)


def all_folders() -> list[Path]:
    """Every existing recording folder in the cache, in recorded order."""
    return _existing([Path(entry["folder"]) for entry in _read_entries()])


# ---------------------------------------------------------------------------
# Cache maintenance (backing `octacam cache info` / `octacam cache clear`)
# ---------------------------------------------------------------------------


def dir_size(path: Path) -> int:
    """Total size in bytes of a file or directory tree, not following directory
    symlinks; 0 on error.
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
    """Delete the recording list and crashed writers' temps; True if it existed.

    The lock file is removed after releasing it, so the held fd is never the one
    unlinked (the next write recreates it).
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
# Activity markers
#
# flock-held files signal liveness across processes without PID bookkeeping: the
# OS drops an flock on exit or crash, so a marker whose lock can be taken is dead.
# `transcode-active`: a running `octacam process`; a `gui`/`record`
# launch warns about the CPU contention. `capture-active`: a `gui`/`record`
# owning the cameras; `octacam process` pauses between work units meanwhile.
# ---------------------------------------------------------------------------


def _transcode_dir() -> Path:
    return cache_dir() / TRANSCODE_DIR_NAME


def _capture_dir() -> Path:
    return cache_dir() / CAPTURE_DIR_NAME


@contextmanager
def _mark_active(directory: Path, detail: str) -> Iterator[None]:
    """Publish an flock-held marker under `directory` for the block's lifetime
    (best-effort: without one if it cannot be created).
    """
    handle = None
    path = None
    try:
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{os.getpid()}-{uuid.uuid4().hex}.lock"
        # The handle outlives this block: it holds the lock.
        handle = open(path, "a+")  # noqa: SIM115
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        handle.seek(0)
        handle.truncate()
        handle.write(f"{os.getpid()} {detail}\n")
        handle.flush()
    except OSError as e:
        log.debug(
            "Could not publish an activity marker in %s (%s); continuing", directory, e
        )
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


def _scan(directory: Path) -> tuple[int, int]:
    """Count the live markers in `directory` and sweep the orphaned ones;
    return (live, swept).

    An orphan is swept only past `_STALE_MARKER_AGE_S`, so a marker created
    but not yet locked is never taken for one.
    """
    try:
        markers = [p for p in directory.iterdir() if p.suffix == ".lock"]
    except OSError:
        return 0, 0
    live = swept = 0
    for marker in markers:
        try:
            # Read-only, so another user's marker is still checkable and one
            # unlinked since iterdir is not recreated.
            held = flock_held(marker)
        except OSError:
            continue
        if held is not False:
            live += 1
            continue
        try:
            if _now().timestamp() - marker.stat().st_mtime > _STALE_MARKER_AGE_S:
                marker.unlink(missing_ok=True)
                swept += 1
        except OSError:
            pass
    return live, swept


def sweep_orphan_markers() -> tuple[int, int]:
    """Sweep orphaned transcode and capture markers; return (removed, live)."""
    removed = live = 0
    for directory in (_transcode_dir(), _capture_dir()):
        held, swept = _scan(directory)
        removed += swept
        live += held
    return removed, live


def transcode_running() -> int:
    """How many `octacam process` runs are transcoding on this machine right now."""
    return _scan(_transcode_dir())[0]


def capture_running() -> int:
    """How many gui/record captures own cameras on this machine (two rigs can)."""
    return _scan(_capture_dir())[0]


def capture_active() -> bool:
    """True while a gui/record on this machine owns cameras."""
    return capture_running() > 0
