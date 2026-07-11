"""Detached ``octacam process`` jobs — spawn, track, attach, pause, cancel.

``octacam process`` normally runs in the foreground and dies with its terminal
(a dropped ``ssh`` session kills a long transcode). ``--detach`` instead re-execs
a fresh ``octacam process`` in its own session (``start_new_session=True``) with
stdout/stderr redirected to a per-job ``log.txt``; the child writes a
``status.json`` it updates as it runs and holds an ``flock`` liveness marker for
its whole lifetime. ``octacam jobs`` (list/attach/pause/resume/cancel) and the
``attach`` viewer are plain readers/tailers of those files — no daemon, no IPC.

The design reuses octacam's crash-safe idioms verbatim: the ``flock``-marker
liveness pattern and atomic temp+``os.replace`` writes from
:mod:`octacam.session_cache` (a marker/lock we *can* take is dead, so liveness
needs no PID bookkeeping), and the ``Popen`` idiom from :mod:`octacam.writer`.

Crucially the worker re-execs the ordinary ``process`` command, which imports no
camera SDK — so a detached job cannot hit the pypylon/genicam native-teardown
crash that only affects camera-owning processes.
"""

from __future__ import annotations

import contextlib
import dataclasses
import datetime
import fcntl
import json
import logging
import os
import shutil
import signal
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from octacam import session_cache

if TYPE_CHECKING:
    from rich.console import Console

    from octacam.transfer import TransferCallback
    from octacam.writer import ProgressCallback

log = logging.getLogger("octacam")

# Job states.
STARTING = "starting"
RUNNING = "running"
DONE = "done"
FAILED = "failed"
CANCELLED = "cancelled"
_TERMINAL = frozenset({DONE, FAILED, CANCELLED})

JOBS_DIR_NAME = "jobs"
STATUS_FILENAME = "status.json"
LOG_FILENAME = "log.txt"
LOCK_FILENAME = "job.lock"
PAUSE_FILENAME = "pause.flag"
STATUS_SCHEMA = 1
RETENTION_DAYS = 30
# A job whose lock is not held but whose status never reached a terminal state is
# treated as crashed — but a just-spawned "starting" job whose child hasn't taken
# the lock yet gets this grace window first.
_STALE_STARTING_AGE_S = 30.0
# Throttle progress-driven status writes so per-ffmpeg-block callbacks don't thrash
# the disk; transitions (phase/file/pause) force an immediate write regardless.
_STATUS_WRITE_MIN_INTERVAL_S = 1.0
_FOLLOW_INTERVAL_S = 0.5


# ---------------------------------------------------------------------------
# Status model + paths
# ---------------------------------------------------------------------------


@dataclass
class JobStatus:
    """A detached job's public state, persisted as ``status.json``."""

    job_id: str
    state: str = STARTING  # starting|running|done|failed|cancelled
    phase: str | None = None  # transcode|grid|transfer|None
    paused: bool = False
    paused_reason: str | None = None  # None|manual|capture-active|capture-active+manual
    pid: int | None = None
    session_id: str | None = None
    started: str = ""
    updated: str = ""
    folders: list[str] = field(default_factory=list)  # resolved absolute
    files_total: int = 0
    files_done: int = 0
    current_file: str | None = None
    percent: float = 0.0  # progress within the current phase, 0..100
    exit_code: int | None = None
    error: str | None = None
    argv: list[str] = field(default_factory=list)
    schema: int = STATUS_SCHEMA

    def to_dict(self) -> dict:
        return dataclasses.asdict(self)

    @classmethod
    def from_dict(cls, data: object) -> JobStatus | None:
        """Build from parsed JSON, tolerantly — bad/partial data returns None."""
        if not isinstance(data, dict):
            return None
        job_id = data.get("job_id")
        if not isinstance(job_id, str) or not job_id:
            return None
        known = {f.name for f in dataclasses.fields(cls)}
        try:
            return cls(**{k: v for k, v in data.items() if k in known})
        except TypeError:
            return None


def jobs_dir() -> Path:
    return session_cache.cache_dir() / JOBS_DIR_NAME


def job_dir(job_id: str) -> Path:
    return jobs_dir() / job_id


def _status_path(jd: Path) -> Path:
    return jd / STATUS_FILENAME


def _log_path(jd: Path) -> Path:
    return jd / LOG_FILENAME


def _lock_path(jd: Path) -> Path:
    return jd / LOCK_FILENAME


def _pause_path(jd: Path) -> Path:
    return jd / PAUSE_FILENAME


def new_job_id() -> str:
    """A sortable id grouping one detached run, like session ids."""
    return f"{datetime.datetime.now().strftime('%Y%m%dT%H%M%S')}-{os.getpid()}"


def _now_iso() -> str:
    return datetime.datetime.now().astimezone().isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# Store I/O (atomic writes, tolerant reads, flock liveness)
# ---------------------------------------------------------------------------


def write_status(jd: Path, status: JobStatus) -> None:
    """Atomically write ``status.json`` (temp + os.replace). Best-effort."""
    status.updated = _now_iso()
    try:
        jd.mkdir(parents=True, exist_ok=True)
        tmp = jd / f".{STATUS_FILENAME}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
        tmp.write_text(json.dumps(status.to_dict(), indent=2) + "\n")
        os.replace(tmp, _status_path(jd))
    except OSError as e:
        log.debug("Could not write job status in %s (%s)", jd, e)


def read_status(jd: Path) -> JobStatus | None:
    """Read ``status.json``; missing/corrupt/partial returns None (never raises)."""
    try:
        raw = _status_path(jd).read_text()
    except OSError:
        return None
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return None
    return JobStatus.from_dict(data)


def _flock_is_live(path: Path) -> bool:
    """True when another process holds the exclusive flock on ``path``."""
    try:
        handle = open(path)
    except OSError:
        return False  # no lock file -> nobody is holding it
    try:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return True  # held elsewhere -> live
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        return False
    finally:
        handle.close()


def is_live(jd: Path) -> bool:
    """True while the job's worker process is running (holds ``job.lock``)."""
    return _flock_is_live(_lock_path(jd))


def is_manually_paused(jd: Path) -> bool:
    return _pause_path(jd).exists()


def _reconcile(jd: Path, status: JobStatus) -> JobStatus:
    """Turn a non-terminal status whose worker is gone into ``failed``.

    Reconcile-on-read: never mutates the file, only the in-memory object returned
    to a caller. A ``starting`` job gets a short grace so a just-spawned child
    that hasn't taken the lock yet isn't reported dead.
    """
    if status.state in _TERMINAL or is_live(jd):
        return status
    try:
        age = time.time() - _status_path(jd).stat().st_mtime
    except OSError:
        age = _STALE_STARTING_AGE_S + 1
    if status.state == STARTING and age < _STALE_STARTING_AGE_S:
        return status
    status.state = FAILED
    status.paused = False
    status.paused_reason = None
    if status.error is None:
        status.error = "worker exited without finishing"
    if status.exit_code is None:
        status.exit_code = -1
    return status


def _cleanup_dir(jd: Path) -> None:
    with contextlib.suppress(OSError):
        shutil.rmtree(jd)


# ---------------------------------------------------------------------------
# Spawner (parent side)
# ---------------------------------------------------------------------------


def _reserve_job_dir() -> Path:
    base = new_job_id()
    name = base
    suffix = 1
    while True:
        jd = job_dir(name)
        try:
            jd.mkdir(parents=True, exist_ok=False)
            return jd
        except FileExistsError:
            name = f"{base}-{suffix}"
            suffix += 1
        except OSError as e:
            raise RuntimeError(f"Could not create job directory: {e}") from e


def spawn_detached(
    *,
    argv_tail: list[str],
    folders: list[Path],
    session_id: str | None = None,
    log_level: str = "info",
) -> JobStatus:
    """Re-exec ``octacam process <argv_tail>`` as a detached background job.

    ``argv_tail`` must already carry absolute paths (the child runs from ``$HOME``
    with no inherited cwd). Returns the initial :class:`JobStatus`. POSIX-only.
    """
    if os.name != "posix":
        raise RuntimeError("Detached processing (--detach) is only supported on Linux/macOS.")
    jd = _reserve_job_dir()
    status = JobStatus(
        job_id=jd.name,
        state=STARTING,
        session_id=session_id,
        started=_now_iso(),
        folders=[str(f) for f in folders],
        argv=list(argv_tail),
    )
    # Write the initial status *before* spawning so a job always has one, even if
    # the child never manages to start.
    write_status(jd, status)
    cmd = [
        sys.executable,
        "-m",
        "octacam",
        "--log-level",
        log_level,
        "process",
        *argv_tail,
        "--_job-dir",
        str(jd),
    ]
    try:
        log_fd = open(_log_path(jd), "ab")
    except OSError as e:
        _cleanup_dir(jd)
        raise RuntimeError(f"Could not open the job log: {e}") from e
    # The child's stdout/stderr are a pipe to log.txt, not a tty, so rich would
    # strip color. Force it on so the log carries ANSI and `jobs attach` can
    # replay it in color (a NO_COLOR in the environment still wins in rich).
    env = os.environ.copy()  # carries OCTACAM_CACHE_DIR to the child
    env.setdefault("FORCE_COLOR", "1")
    try:
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=log_fd,
            stderr=subprocess.STDOUT,
            start_new_session=True,  # own process group -> killpg reaches ffmpeg too
            close_fds=True,
            cwd=str(Path.home()),
            env=env,
        )
    except OSError as e:
        with contextlib.suppress(OSError):
            log_fd.close()
        _cleanup_dir(jd)
        raise RuntimeError(f"Could not start the detached job: {e}") from e
    with contextlib.suppress(OSError):
        log_fd.close()
    status.pid = proc.pid
    write_status(jd, status)
    return status


# ---------------------------------------------------------------------------
# Worker side (used by the detached child running `octacam process --_job-dir`)
# ---------------------------------------------------------------------------


class JobReporter:
    """Best-effort status/log updates written by the running worker."""

    def __init__(self, jd: Path, status: JobStatus) -> None:
        self._dir = jd
        self._status = status
        self._last_write = 0.0

    def _flush(self, *, force: bool = False) -> None:
        now = time.time()
        if force or (now - self._last_write) >= _STATUS_WRITE_MIN_INTERVAL_S:
            self._last_write = now
            write_status(self._dir, self._status)

    def _set_percent(self, done: float, total: float) -> None:
        if total > 0:
            self._status.percent = round(max(0.0, min(100.0, 100.0 * done / total)), 1)

    def begin_phase(self, phase: str, total: int) -> None:
        self._status.phase = phase
        self._status.files_total = total
        self._status.files_done = 0
        self._status.current_file = None
        self._status.percent = 0.0
        log.info("Phase: %s (%d item(s))", phase, total)
        self._flush(force=True)

    def item_started(self, index: int, total: int, path: str | os.PathLike[str]) -> None:
        name = Path(path).name
        self._status.files_total = total
        self._status.current_file = name
        self._set_percent(index - 1, total)
        log.info("[%s %d/%d] %s", self._status.phase or "?", index, total, name)
        self._flush(force=True)

    def item_done(self) -> None:
        self._status.files_done += 1
        self._set_percent(self._status.files_done, max(self._status.files_total, 1))
        self._flush(force=True)

    def set_paused(self, flag: bool, reason: str | None) -> None:
        self._status.paused = flag
        self._status.paused_reason = reason
        self._flush(force=True)

    def transcode_progress(self, index: int, total: int) -> ProgressCallback:
        """A writer.ProgressCallback refining within-file transcode percent."""
        base = index - 1

        def _cb(prog: object) -> None:
            frac = 0.0
            frames = getattr(prog, "frame", 0)
            tf = getattr(prog, "total_frames", None)
            if tf:
                frac = min(1.0, frames / tf)
            self._set_percent(base + frac, total)
            self._flush()

        return _cb

    def transfer_progress(self, index: int, total: int) -> TransferCallback:
        """A transfer.TransferCallback tracking the current file + folder percent."""
        base = index - 1

        def _cb(prog: object) -> None:
            self._status.current_file = getattr(prog, "filename", None)
            count = max(getattr(prog, "file_count", 1), 1)
            fi = getattr(prog, "file_index", 1)
            size = getattr(prog, "file_size", 0) or 0
            done = getattr(prog, "bytes_done", 0)
            within = (fi - 1 + (done / size if size else 0.0)) / count
            self._set_percent(base + within, total)
            self._flush()

        return _cb


class Worker:
    """Handle held by the detached child for the life of the run."""

    def __init__(self, jd: Path, lock_handle, status: JobStatus) -> None:
        self.dir = jd
        self.status = status
        self._lock = lock_handle
        self.reporter = JobReporter(jd, status)

    def finish(self, state: str, exit_code: int | None, error: str | None = None) -> None:
        self.status.state = state
        self.status.exit_code = exit_code
        self.status.error = error
        self.status.paused = False
        self.status.paused_reason = None
        self.status.current_file = None
        if state == DONE:
            self.status.percent = 100.0
        write_status(self.dir, self.status)
        with contextlib.suppress(OSError):
            _pause_path(self.dir).unlink(missing_ok=True)
        self._release()

    def _release(self) -> None:
        if self._lock is not None:
            try:
                fcntl.flock(self._lock.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
            finally:
                with contextlib.suppress(OSError):
                    self._lock.close()
            self._lock = None


def worker_start(jd: Path) -> Worker:
    """Attach the current process to ``jd`` as its worker: lock + mark running."""
    status = read_status(jd) or JobStatus(job_id=jd.name)
    status.pid = os.getpid()
    status.state = RUNNING
    if not status.started:
        status.started = _now_iso()
    lock_handle = None
    try:
        lock_handle = open(_lock_path(jd), "a+")
        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
    except OSError as e:
        log.debug("Could not take the job lock in %s (%s); running unmarked", jd, e)
        if lock_handle is not None:
            with contextlib.suppress(OSError):
                lock_handle.close()
            lock_handle = None
    write_status(jd, status)
    return Worker(jd, lock_handle, status)


# ---------------------------------------------------------------------------
# Admin / read API
# ---------------------------------------------------------------------------


def list_jobs() -> list[JobStatus]:
    """All jobs (reconciled + pruned), newest first."""
    prune()
    out: list[JobStatus] = []
    try:
        entries = list(jobs_dir().iterdir())
    except OSError:
        return out
    for jd in entries:
        if not jd.is_dir():
            continue
        status = read_status(jd)
        if status is None:
            continue
        out.append(_reconcile(jd, status))
    out.sort(key=lambda s: s.started, reverse=True)
    return out


def live_jobs() -> list[JobStatus]:
    return [s for s in list_jobs() if s.state not in _TERMINAL]


def latest_live_job() -> JobStatus | None:
    jobs = live_jobs()
    return jobs[0] if jobs else None


def latest_job() -> JobStatus | None:
    jobs = list_jobs()
    return jobs[0] if jobs else None


def resolve_job(id_or_none: str | None, *, require_live: bool = False) -> JobStatus | None:
    """Resolve an explicit job id, or the most recent (live) job when None."""
    if id_or_none is None:
        return latest_live_job() if require_live else latest_job()
    jd = job_dir(id_or_none)
    status = read_status(jd)
    if status is None:
        return None
    status = _reconcile(jd, status)
    if require_live and status.state in _TERMINAL:
        return None
    return status


def cancel(status: JobStatus) -> bool:
    """Gracefully stop a live job (SIGINT to its group = a clean Ctrl-C)."""
    jd = job_dir(status.job_id)
    if status.pid is None or not is_live(jd):
        return False
    try:
        os.killpg(os.getpgid(status.pid), signal.SIGINT)
        return True
    except OSError:
        return False


def pause(status: JobStatus) -> bool:
    """Request a manual pause (the worker parks at its next work-unit boundary)."""
    jd = job_dir(status.job_id)
    try:
        _pause_path(jd).touch()
        return True
    except OSError:
        return False


def resume(status: JobStatus) -> bool:
    """Clear a manual pause (a capture-active auto-pause is unaffected)."""
    jd = job_dir(status.job_id)
    try:
        _pause_path(jd).unlink(missing_ok=True)
        return True
    except OSError:
        return False


def prune(retention_days: int = RETENTION_DAYS) -> None:
    """Remove finished job dirs older than the retention window. Best-effort."""
    cutoff = time.time() - retention_days * 86400
    try:
        entries = list(jobs_dir().iterdir())
    except OSError:
        return
    for jd in entries:
        if not jd.is_dir() or is_live(jd):
            continue
        try:
            mtime = _status_path(jd).stat().st_mtime
        except OSError:
            try:
                mtime = jd.stat().st_mtime
            except OSError:
                continue
        if mtime < cutoff:
            _cleanup_dir(jd)


def clear_finished() -> tuple[int, int]:
    """Remove every finished (non-live) job directory now; return (removed, live_kept).

    Like :func:`prune` but ignores the retention window — used by
    ``octacam cache clear --all`` to drop finished job logs immediately. A live
    job (its worker still holds ``job.lock``) is never touched.
    """
    removed = live = 0
    try:
        entries = list(jobs_dir().iterdir())
    except OSError:
        return 0, 0
    for jd in entries:
        if not jd.is_dir():
            continue
        if is_live(jd):
            live += 1
            continue
        _cleanup_dir(jd)
        removed += 1
    return removed, live


def job_dir_counts() -> tuple[int, int]:
    """(live, finished) job-directory counts on disk, without pruning.

    Counts directories, not reconciled statuses, so it matches exactly what
    :func:`clear_finished` would remove (finished) vs keep (live).
    """
    live = finished = 0
    try:
        entries = list(jobs_dir().iterdir())
    except OSError:
        return 0, 0
    for jd in entries:
        if not jd.is_dir():
            continue
        if is_live(jd):
            live += 1
        else:
            finished += 1
    return live, finished


# ---------------------------------------------------------------------------
# Attach viewer (tmux-like: follow log + status; Ctrl-C detaches, never cancels)
# ---------------------------------------------------------------------------


def render_state(status: JobStatus) -> str:
    if status.paused and status.state == RUNNING:
        return f"paused ({status.paused_reason or 'paused'})"
    return status.state


def _age(started: str) -> str:
    try:
        dt = datetime.datetime.fromisoformat(started)
    except (ValueError, TypeError):
        return "-"
    secs = int((datetime.datetime.now().astimezone() - dt).total_seconds())
    if secs < 60:
        return f"{secs}s"
    if secs < 3600:
        return f"{secs // 60}m"
    if secs < 86400:
        return f"{secs // 3600}h{(secs % 3600) // 60:02d}m"
    return f"{secs // 86400}d"


def render_table(jobs: list[JobStatus], console: Console | None = None) -> None:
    from rich.console import Console
    from rich.table import Table

    if console is None:
        console = Console()
    if not jobs:
        console.print("No processing jobs.")
        return
    table = Table(show_edge=False, pad_edge=False)
    table.add_column("Job id")
    table.add_column("State")
    table.add_column("Phase")
    table.add_column("Progress", justify="right")
    table.add_column("Age", justify="right")
    table.add_column("Folders", justify="right")
    for s in jobs:
        table.add_row(
            s.job_id,
            render_state(s),
            s.phase or "-",
            f"{s.percent:.0f}%",
            _age(s.started),
            str(len(s.folders)),
        )
    console.print(table)


class _LogFollower:
    """Line-buffered tail of a job's ``log.txt`` (tolerant of truncation)."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._offset = 0
        self._partial = ""

    def _read_new(self) -> str:
        try:
            size = self._path.stat().st_size
        except OSError:
            return ""
        if size < self._offset:  # truncated / rotated
            self._offset = 0
            self._partial = ""
        if size == self._offset:
            return ""
        try:
            with open(self._path, "rb") as f:
                f.seek(self._offset)
                chunk = f.read()
        except OSError:
            return ""
        self._offset += len(chunk)
        return chunk.decode("utf-8", "replace")

    def lines(self) -> list[str]:
        """Complete lines appended since the last call (a partial tail is held)."""
        text = self._partial + self._read_new()
        if not text:
            return []
        parts = text.split("\n")
        self._partial = parts.pop()  # trailing partial line (or "")
        return parts

    def drain(self) -> list[str]:
        """Remaining lines incl. any final partial — call once the job has ended."""
        out = self.lines()
        if self._partial:
            out.append(self._partial)
            self._partial = ""
        return out


def _print_log_line(console: Console, line: str) -> None:
    """Replay one worker log line, preserving its ANSI color, never as markup.

    Worker logs carry their own SGR codes (the child forces color), so rebuild the
    styled text from ANSI rather than letting rich parse ``[transcode 1/6]`` as
    markup."""
    from rich.text import Text

    try:
        console.print(Text.from_ansi(line))
    except Exception:  # a rendering hiccup must never kill the viewer
        with contextlib.suppress(Exception):
            console.print(line, markup=False, highlight=False)


def _make_progress(console: Console):
    from rich.progress import (
        BarColumn,
        Progress,
        SpinnerColumn,
        TaskProgressColumn,
        TextColumn,
        TimeElapsedColumn,
    )

    return Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TaskProgressColumn(),
        TextColumn("{task.fields[counts]}"),
        TimeElapsedColumn(),
        console=console,
        transient=True,
    )


def _bar_description(status: JobStatus) -> str:
    if status.paused and status.state == RUNNING:
        return f"paused ({status.paused_reason or 'paused'})"
    if status.phase:
        if status.current_file:
            return f"{status.phase}: {status.current_file}"
        return status.phase
    return status.state


def _bar_counts(status: JobStatus) -> str:
    return f"{status.files_done}/{status.files_total}" if status.files_total else ""


def _update_bar(progress, task_id, status: JobStatus) -> None:
    progress.update(
        task_id,
        description=_bar_description(status),
        completed=max(0.0, min(100.0, status.percent)),
        total=100.0,
        counts=_bar_counts(status),
    )


def _print_final(status: JobStatus, console: Console) -> None:
    if status.state == DONE:
        console.print(f"[green]Job {status.job_id} finished.[/]")
    elif status.state == CANCELLED:
        console.print(f"[yellow]Job {status.job_id} was cancelled.[/]")
    else:
        console.print(f"[red]Job {status.job_id} failed: {status.error or 'unknown error'}[/]")


def attach(status: JobStatus, console: Console | None = None) -> int:
    """Follow a detached job's log + a live progress bar until it ends.

    Ctrl-C detaches the viewer (returns 0); it never cancels the job. An
    already-finished job replays its log, prints the outcome, and returns.
    """
    from rich.console import Console

    if console is None:
        console = Console(stderr=True)
    jd = job_dir(status.job_id)
    console.print(
        f"[bold]Attached to job {status.job_id}[/] ({render_state(status)}). "
        "Press Ctrl-C to stop watching — this does NOT cancel the job."
    )
    follower = _LogFollower(_log_path(jd))
    # Replay whatever the log already holds before starting the live bar.
    for line in follower.lines():
        _print_log_line(console, line)
    fresh = _reconcile(jd, read_status(jd) or status)
    if fresh.state in _TERMINAL:
        for line in follower.drain():
            _print_log_line(console, line)
        _print_final(fresh, console)
        return 0

    detached = False
    final = fresh
    with _make_progress(console) as progress:
        task = progress.add_task(_bar_description(fresh), total=100.0, counts=_bar_counts(fresh))
        _update_bar(progress, task, fresh)  # seed completed% (update takes a float)
        try:
            while True:
                for line in follower.lines():
                    _print_log_line(console, line)  # scrolls above the live bar
                current = read_status(jd)
                if current is not None:
                    current = _reconcile(jd, current)
                    final = current
                    _update_bar(progress, task, current)
                    if current.state in _TERMINAL:
                        for line in follower.drain():
                            _print_log_line(console, line)
                        break
                elif not is_live(jd):
                    final = _reconcile(jd, read_status(jd) or status)
                    break
                time.sleep(_FOLLOW_INTERVAL_S)
        except KeyboardInterrupt:
            detached = True
    if detached:
        console.print(
            f"\n[dim]Detached from job {status.job_id} — it keeps running. "
            f"Reattach: octacam jobs attach {status.job_id}[/]"
        )
        return 0
    _print_final(final, console)
    return 0
