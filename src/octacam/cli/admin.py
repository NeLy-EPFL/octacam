"""`octacam jobs` (detached processing jobs) and `octacam cache`."""

import logging
import sys
from typing import TYPE_CHECKING, Annotated

import typer

from octacam.cli._common import stderr_console

if TYPE_CHECKING:
    from collections.abc import Callable

    from octacam.process_jobs import JobStatus

log = logging.getLogger("octacam")


_JobArg = Annotated[
    str | None,
    typer.Argument(
        metavar="[JOB]",
        help="Job id (from `octacam jobs list`). Omit for the most recent job.",
    ),
]


def jobs_list() -> None:
    """List detached processing jobs and their progress."""
    from octacam import process_jobs

    process_jobs.render_table(process_jobs.list_jobs())


def jobs_attach(job_id: _JobArg = None) -> None:
    """Follow a detached job's live log + progress (Ctrl-C detaches, does not cancel)."""
    from octacam import process_jobs

    job = process_jobs.resolve_job(job_id)
    if job is None:
        sys.exit("No such job." if job_id else "No processing jobs to attach to.")
    raise typer.Exit(process_jobs.attach(job, stderr_console()))


def _control_live_job(
    job_id: str | None,
    verb: str,
    action: "Callable[[JobStatus], bool]",
    done: str,
    hint: str = "",
) -> None:
    """Apply ``action`` to a live job (the latest when no id is given); exit on failure."""
    from octacam import process_jobs

    job = process_jobs.resolve_job(job_id, require_live=True)
    if job is None:
        sys.exit("No such live job." if job_id else f"No live processing jobs to {verb}.")
    if not action(job):
        sys.exit(f"Could not {verb} job {job.job_id}{hint}.")
    log.info(done, job.job_id)


def jobs_pause(job_id: _JobArg = None) -> None:
    """Pause a running detached job (it parks at its next file/folder boundary)."""
    from octacam import process_jobs

    _control_live_job(job_id, "pause", process_jobs.pause, "Requested pause of job %s.")


def jobs_resume(job_id: _JobArg = None) -> None:
    """Clear a manual pause (a gui/record auto-pause clears on its own)."""
    from octacam import process_jobs

    _control_live_job(
        job_id, "resume", process_jobs.resume, "Cleared manual pause of job %s."
    )


def jobs_cancel(job_id: _JobArg = None) -> None:
    """Cancel a running detached job (a clean stop; already-done work is kept)."""
    from octacam import process_jobs

    _control_live_job(
        job_id,
        "cancel",
        process_jobs.cancel,
        "Cancelling job %s.",
        hint=" (it may have already finished)",
    )


def _human_size(num_bytes: int) -> str:
    """A short human-readable byte size (e.g. ``0 B``, ``7.0 KB``, ``2.1 MB``)."""
    size = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024:
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"


def _plural(n: int, singular: str, plural: str) -> str:
    return singular if n == 1 else plural


def cache_path() -> None:
    """Print the octacam cache directory (respects OCTACAM_CACHE_DIR / XDG_CACHE_HOME)."""
    from octacam import session_cache

    typer.echo(str(session_cache.cache_dir()))


def cache_info() -> None:
    """Show the cache location, size, and a breakdown of what is cached."""
    from octacam import process_jobs, session_cache

    root = session_cache.cache_dir()
    if not root.exists():
        typer.echo(f"{root}  (nothing cached yet)")
        return

    n_recordings = session_cache.recordings_count()
    live_jobs, finished_jobs = process_jobs.job_dir_counts()
    live_markers = session_cache.transcode_running() + session_cache.capture_running()
    rec_size = _human_size(session_cache.dir_size(root / session_cache.CACHE_FILENAME))
    jobs_size = _human_size(session_cache.dir_size(process_jobs.jobs_dir()))

    typer.echo(f"{root}  ({_human_size(session_cache.dir_size(root))})")
    typer.echo(
        f"  recordings   {rec_size:>8}   "
        f"{n_recordings} {_plural(n_recordings, 'entry', 'entries')}"
    )
    typer.echo(
        f"  jobs         {jobs_size:>8}   {live_jobs} live, {finished_jobs} finished"
    )
    typer.echo(f"  markers      {'—':>8}   {live_markers} live")


def cache_clear(
    all_: Annotated[
        bool,
        typer.Option(
            "--all",
            help="Also remove finished detached-job logs (kept by default).",
        ),
    ] = False,
    yes: Annotated[
        bool,
        typer.Option("--yes", "-y", help="Clear without the confirmation prompt."),
    ] = False,
) -> None:
    """Clear cached state under the cache dir.

    Removes the recording list and orphaned activity markers; with `--all` it also
    removes finished detached-job logs. A live capture, transcode, or job is never
    touched.
    """
    from octacam import process_jobs, session_cache

    # For the preview only: the report re-reads after clearing, so a job ending
    # at the prompt is never both "Cleared" and "Kept: live".
    n_recordings = session_cache.recordings_count()
    rec_exists = (session_cache.cache_dir() / session_cache.CACHE_FILENAME).exists()

    if not yes:
        _, pre_finished = process_jobs.job_dir_counts()
        targets = []
        if rec_exists:
            targets.append(
                f"the recording list ({n_recordings} "
                f"{_plural(n_recordings, 'entry', 'entries')})"
            )
        if all_ and pre_finished:
            targets.append(
                f"{pre_finished} finished detached-job "
                f"{_plural(pre_finished, 'log', 'logs')}"
            )
        targets.append("stale activity markers")
        typer.echo(f"Cache dir: {session_cache.cache_dir()}")
        typer.echo("Will clear: " + "; ".join(targets) + ".")
        protected = _live_summary(
            *_live_counts(session_cache, process_jobs)
        )
        if protected:
            typer.echo(f"Protected (kept live): {protected}.")
        typer.confirm("Proceed?", abort=True)

    removed_rec = session_cache.clear_recordings()
    swept, _live_markers = session_cache.sweep_orphan_markers()
    removed_jobs = 0
    if all_:
        removed_jobs, _ = process_jobs.clear_finished()

    cleared = []
    if removed_rec:
        cleared.append("recording list")
    if swept:
        cleared.append(f"{swept} stale {_plural(swept, 'marker', 'markers')}")
    if removed_jobs:
        cleared.append(
            f"{removed_jobs} finished job {_plural(removed_jobs, 'log', 'logs')}"
        )
    typer.echo("Cleared: " + ", ".join(cleared) + "." if cleared else "Nothing needed clearing.")

    live_jobs, live_transcode, live_capture = _live_counts(session_cache, process_jobs)
    finished_jobs = process_jobs.job_dir_counts()[1]
    kept = _live_summary(live_jobs, live_transcode, live_capture)
    if not all_ and finished_jobs:
        note = (
            f"{finished_jobs} finished job "
            f"{_plural(finished_jobs, 'log', 'logs')} (use --all to remove)"
        )
        kept = f"{kept}, {note}" if kept else note
    if kept:
        typer.echo(f"Kept: {kept}.")


def _live_counts(session_cache, process_jobs) -> tuple[int, int, int]:
    """(live_jobs, live_transcodes, live_captures) on this machine right now."""
    return (
        process_jobs.job_dir_counts()[0],
        session_cache.transcode_running(),
        session_cache.capture_running(),
    )


def _live_summary(live_jobs: int, live_transcode: int, live_capture: int) -> str:
    """Join the currently-live processes into a human phrase (empty if none)."""
    bits = []
    if live_jobs:
        bits.append(f"{live_jobs} live {_plural(live_jobs, 'job', 'jobs')}")
    if live_transcode:
        bits.append(
            f"{live_transcode} live {_plural(live_transcode, 'transcode', 'transcodes')}"
        )
    if live_capture:
        bits.append(f"{live_capture} live {_plural(live_capture, 'capture', 'captures')}")
    return ", ".join(bits)
