"""`octacam process` (the post-recording pipeline, `octacam.process`) and
`octacam check`.
"""

import json
import logging
import shlex
import sys
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Self

import typer
from typer.core import TyperCommand

from octacam.cli._common import Verbose, command, resolve_config_arg, stderr_console

if TYPE_CHECKING:
    from rich.progress import TaskID

    from octacam.transcode import ProgressCallback
    from octacam.transfer import TransferCallback

log = logging.getLogger("octacam")


class ProgressStyle(StrEnum):
    octacam = "octacam"
    ffmpeg = "ffmpeg"


def _resolve_transcode_paths(
    paths: list[Path],
    last: str | None,
    session_id: str | None,
    all_: bool,
) -> list[Path]:
    """Explicit paths, or the cached folders one selector names (deleted ones
    skipped): `--last [recording]` the newest, `--last session` its whole
    session, `--session-id` an exact one (what the GUI prints), `--all`
    every one. The selectors exclude each other and paths; exits on a bad
    value or combination, or when nothing is found.
    """
    from octacam import session_cache

    if last is not None and last not in ("recording", "session"):
        sys.exit(
            f"--last takes 'recording' or 'session' (or nothing, for the most "
            f"recent recording), not {last!r}."
        )

    chosen = [
        name
        for name, on in (
            ("--last", last is not None),
            ("--session-id", session_id is not None),
            ("--all", all_),
        )
        if on
    ]
    if len(chosen) > 1:
        sys.exit(f"Choose at most one of {', '.join(chosen)}.")
    if chosen and paths:
        sys.exit(f"{chosen[0]} cannot be combined with explicit paths.")
    if not chosen:
        if not paths:
            sys.exit("Provide one or more paths, or one of --last/--session-id/--all.")
        return paths

    if last == "session":
        selector = "--last session"
        folders = session_cache.session_folders()
    elif last == "recording":
        selector = "--last"
        folder = session_cache.last_folder()
        folders = [folder] if folder else []
    elif all_:
        selector = "--all"
        folders = session_cache.all_folders()
    else:
        selector = f"--session-id {shlex.quote(session_id or '')}"
        folders = session_cache.session_folders(session_id)
    if not folders:
        sys.exit(
            f"No recordings found for {selector} in the cache "
            f"({session_cache.cache_dir()}). Record something first, or pass "
            "explicit paths."
        )
    log.info(
        "%s: transcoding %d folder(s) from the recording cache", selector, len(folders)
    )
    return folders


class FileProgressBar:
    """`process`'s progress bar on the stderr console, one rich task per file.

    Each file gets a fresh task: rich keeps a task's total when updated with
    `total=None`, so a reused task would give a file of unknown length the
    previous file's total.
    """

    def __init__(self, total_files: int = 0) -> None:
        from rich.progress import (
            BarColumn,
            Progress,
            SpinnerColumn,
            TaskProgressColumn,
            TextColumn,
            TimeElapsedColumn,
        )

        self._total_files = total_files
        self._progress = Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            TaskProgressColumn(),
            TextColumn("{task.fields[stats]}"),
            TimeElapsedColumn(),
            console=stderr_console(),
            transient=True,
        )
        self._task: TaskID | None = None

    def __enter__(self) -> Self:
        self._progress.start()
        return self

    def __exit__(self, *exc) -> None:
        self._progress.stop()

    def _start(self, description: str, total: float | None) -> TaskID:
        if self._task is not None:
            self._progress.remove_task(self._task)
        self._task = self._progress.add_task(description, total=total, stats="")
        return self._task

    def file(self, index: int, path: Path, label: str = "") -> ProgressCallback:
        """Start the bar for one ffmpeg encode and return its progress callback."""
        from octacam.transcode import TranscodeProgress

        task = self._start(f"[{index}/{self._total_files}] {label}{path.name}", None)

        def on_progress(p: TranscodeProgress) -> None:
            stats = [f"{p.frame} frames"]
            if p.fps:
                stats.append(f"{p.fps:.0f} fps")
            if p.speed:
                stats.append(f"{p.speed:.3g}x")
            # The frame total is only a hint (dropped frames, or none at all): the
            # final block adopts the frame count, so the bar ends on a clean 100%.
            completed = p.frame
            total = p.total_frames
            if p.done:
                total = max(p.frame, total or 0) or None
                completed = p.frame if total is None else total
            self._progress.update(
                task,
                total=total,
                completed=completed,
                stats="  ".join(stats),
                # rich repaints on a timer that may not tick before the task is
                # removed or the transient bar wiped, leaving it short of 100%.
                refresh=p.done,
            )

        return on_progress

    def transfer_callback(self) -> TransferCallback:
        """A transfer_folder callback: a fresh task for each file's copy and verify."""
        from octacam.transfer import TransferProgress

        current: tuple[int, str] | None = None

        def on_progress(p: TransferProgress) -> None:
            nonlocal current
            if (p.file_index, p.phase) != current:
                current = (p.file_index, p.phase)
                verb = "verify" if p.phase == "verify" else "copy"
                desc = f"[{p.file_index}/{p.file_count}] {verb}: {p.filename}"
                self._start(desc, p.file_size)
            assert self._task is not None  # started above for the first event
            speed = f"{p.speed_mbs:.1f} MB/s" if p.speed_mbs > 0 else ""
            self._progress.update(
                self._task, completed=p.bytes_done, stats=speed, refresh=p.done
            )

        return on_progress


def _inject_default_last(args: list[str]) -> list[str]:
    """Give a bare `--last` (last token, or followed by an option) its default
    `recording`. typer's vendored click drops `flag_value`, so the raw args
    are normalized instead; tokens after `--` are left alone.
    """
    out: list[str] = []
    seen_ddash = False
    for i, tok in enumerate(args):
        out.append(tok)
        if seen_ddash:
            continue
        if tok == "--":
            seen_ddash = True
        elif tok == "--last":
            nxt = args[i + 1] if i + 1 < len(args) else None
            if nxt is None or nxt.startswith("-"):
                out.append("recording")
    return out


class ProcessCommand(TyperCommand):
    """`process`, whose bare `--last` means `--last recording`."""

    def parse_args(self, ctx, args):  # type: ignore[override]
        return super().parse_args(ctx, _inject_default_last(args))


@command
def check(
    paths: Annotated[
        list[Path] | None,
        typer.Argument(
            exists=True,
            help="Recording folders, or directories to search for them "
            "(default: the current directory).",
        ),
    ] = None,
    fps: Annotated[
        float | None,
        typer.Option(
            "--fps", help="Trigger rate to check against (default: each summary's)."
        ),
    ] = None,
    as_json: Annotated[
        bool, typer.Option("--json", help="Print machine-readable results.")
    ] = False,
    quiet: Annotated[
        bool,
        typer.Option("--quiet", "-q", help="Only list recordings with problems."),
    ] = False,
    verbose: Verbose = False,
) -> None:
    """Check recordings for missed trigger pulses and desynchronized cameras.

    Reads each recording's summary and timestamps.npz (never modifies anything)
    and reports, per camera, the trigger pulses it delivered no frame for -- an
    unfilled one shifts its later frames by one against a camera that did not
    miss it -- plus unequal frame counts, a start offset between cameras, the
    recorder's own sync verdict, late exposures and camera-clock jumps.
    Recordings made before octacam counted pulses are re-derived from the
    hardware timestamps. A recording that cannot be read is a problem too.
    Exits 1 if any recording has a problem.
    """
    from rich.console import Console
    from rich.text import Text

    from octacam.check import check_recordings

    # A damaged recording is reported as a problem, never raised.
    results = check_recordings(paths or [Path(".")], fps)
    if not results:
        sys.exit("No recording folders (recording_summary.json) found.")
    if as_json:
        typer.echo(json.dumps([r.to_dict() for r in results], indent=2))
    else:
        console = Console()
        for result in results:
            if quiet and result.ok:
                continue
            verdict = (
                Text("ok", style="green")
                if result.ok
                else Text("PROBLEM", style="bold red")
            )
            console.print(Text(f"{result.folder}  ", style="bold") + verdict)
            for cam in result.cameras:
                if cam.source == "none":
                    detail = "not checked"
                else:
                    detail = f"{cam.missed_count} missed pulse(s)" + (
                        " (filled)" if cam.filled and cam.missed_count else ""
                    )
                    if cam.late_count:
                        detail += f", {cam.late_count} late"
                    if cam.writer_dropped:
                        detail += f", {cam.writer_dropped} writer-dropped"
                console.print(f"    {cam.name:12s} {cam.frames:8d} frames  {detail}")
            for problem in result.problems:
                console.print(Text(f"    ! {problem}", style="red"))
            for warning in result.warnings:
                console.print(Text(f"    - {warning}", style="yellow"))
        bad = sum(1 for r in results if not r.ok)
        console.print()
        console.print(
            f"{len(results)} recording(s) checked: "
            + (f"[bold red]{bad} with problems[/]" if bad else "[green]all ok[/]")
        )
    if any(not r.ok for r in results):
        raise typer.Exit(1)


@command
def process(
    paths: Annotated[
        list[Path] | None,
        typer.Argument(
            exists=True,
            help="Recording folders (or parent directories with -r). Omit when "
            "using --last/--session-id/--all.",
        ),
    ] = None,
    last: Annotated[
        str | None,
        typer.Option(
            "--last",
            metavar="[recording|session]",
            help="Process the most recent recording (--last or --last recording) "
            "or every folder from the last GUI session (--last session).",
        ),
    ] = None,
    session_id: Annotated[
        str | None,
        typer.Option(
            "--session-id",
            help="Process every folder from one exact session id (what the GUI "
            "prints on exit).",
        ),
    ] = None,
    all_: Annotated[
        bool,
        typer.Option(
            "--all", help="Process every recording folder still in the cache."
        ),
    ] = False,
    recursive: Annotated[
        bool,
        typer.Option("-r", "--recursive", help="Recurse into the given folders."),
    ] = False,
    no_transcode: Annotated[
        bool,
        typer.Option(
            "--no-transcode",
            help="Skip transcoding; grid/transfer act on the existing *.mp4 files.",
        ),
    ] = False,
    no_grid: Annotated[
        bool,
        typer.Option(
            "--no-grid", help="Skip building the configured visualization grid(s)."
        ),
    ] = False,
    no_transfer: Annotated[
        bool,
        typer.Option(
            "--no-transfer", help="Skip transferring to the [transfer] destination."
        ),
    ] = False,
    ignore_capture: Annotated[
        bool,
        typer.Option(
            "--ignore-capture",
            help=(
                "Do not pause while an octacam gui/record holds the cameras on "
                "this machine. Processing then competes with capture for "
                "CPU/GPU/disk."
            ),
        ),
    ] = False,
    force: Annotated[
        bool,
        typer.Option(
            "--force",
            help="Re-transcode and rebuild grids even when the output already "
            "exists. By default existing transcode/grid outputs are skipped (as "
            "the transfer step skips files already at the destination).",
        ),
    ] = False,
    config_dir: Annotated[
        Path | None,
        typer.Option(
            "--config",
            "-c",
            exists=True,
            file_okay=False,
            dir_okay=True,
            help="Fallback config dir for recordings that lack an embedded "
            "octacam_config.toml (older recordings). Normally not needed.",
        ),
    ] = None,
    delete_source: Annotated[
        bool,
        typer.Option(
            "--delete-source",
            "-d",
            help="Delete each source .mkv/.raw once it transcodes successfully. "
            "The recording's summary, timestamps and config snapshot are kept.",
        ),
    ] = False,
    progress_style: Annotated[
        ProgressStyle,
        typer.Option(
            "--progress-style",
            help="How to show transcode progress. octacam (default): an "
            "octacam-style bar. ffmpeg: stream ffmpeg's own output verbatim.",
        ),
    ] = ProgressStyle.octacam,
    dry_run: Annotated[
        bool,
        typer.Option(
            "--dry-run",
            help="List what each step would do (files to transcode, grids to build, "
            "files to transfer) without running ffmpeg, copying, or deleting "
            "anything. Work that is already done is only counted.",
        ),
    ] = False,
    detach: Annotated[
        bool,
        typer.Option(
            "--detach",
            help="Run the pipeline as a detached background job that survives an "
            "SSH disconnect, then return its id. Watch it with `octacam jobs "
            "attach`; a running gui/record auto-pauses it.",
        ),
    ] = False,
    job_dir: Annotated[
        Path | None,
        typer.Option("--_job-dir", hidden=True),  # the detached child's job dir
    ] = None,
    verbose: Verbose = False,
) -> None:
    """Post-recording pipeline: transcode, build grids, and transfer recordings.

    Every setting -- encoder args, grid layouts, transfer destination -- is read
    from each recording's own octacam_config.toml (copied in at record time), so
    no --config is needed. Transcode and transfer run by default (disable with
    --no-transcode / --no-transfer); the composite grid is opt-in -- it is built
    only for a rig whose config carries a [[visualization]] entry, and --no-grid
    skips even those.

    Re-running is safe and resumes where it left off: each step skips outputs
    that already exist -- a finished transcode .mp4, a built grid, or a file
    already at the transfer destination -- so only missing work is redone. Pass
    --force to rebuild existing transcodes and grids anyway (e.g. after changing
    the encoder params or grid layout). --dry-run lists that missing work without
    doing any of it, so `octacam process --all --dry-run` shows what is left to
    process.

    Instead of `paths`, pass --last (the most recent recording; same as --last
    recording), --last session (the last GUI session), --session-id (an exact
    session), or --all (every cached folder). Deleted folders are silently
    skipped.
    """
    from octacam import process as pipeline
    from octacam import process_jobs

    if no_transcode and no_grid and no_transfer:
        sys.exit("Nothing to do: --no-transcode, --no-grid and --no-transfer all set.")
    folders = _resolve_transcode_paths(list(paths or []), last, session_id, all_)
    options = pipeline.ProcessOptions(
        transcode=not no_transcode,
        grid=not no_grid,
        transfer=not no_transfer,
        force=force,
        recursive=recursive,
        delete_source=delete_source,
        dry_run=dry_run,
        ignore_capture=ignore_capture,
        # Resolved before a --detach re-exec, so the child is handed the same dir.
        config_dir=resolve_config_arg(config_dir) if config_dir is not None else None,
        raw_output=progress_style is ProgressStyle.ffmpeg,
    )

    # The detached child re-runs this command with --_job-dir, as the job's worker.
    if detach and job_dir is None:
        status = process_jobs.spawn_detached(
            argv_tail=options.argv(folders), folders=folders, verbose=verbose
        )
        typer.echo(status.job_id)  # stdout: scriptable
        log.info(
            "Detached processing job %s \N{EM DASH} watch it with: octacam jobs "
            "attach %s",
            status.job_id,
            status.job_id,
        )
        raise typer.Exit()

    # A worker's forced-color log makes stderr look like a terminal, but its
    # progress belongs in status.json (what `jobs attach` renders), not in
    # cursor codes in log.txt.
    show_bar = (
        not options.raw_output
        and not dry_run
        and job_dir is None
        and stderr_console().is_terminal
    )
    try:
        with process_jobs.worker(job_dir) as reporter:
            pipeline.run(
                folders, options, reporter, FileProgressBar if show_bar else None
            )
    except process_jobs.JobLockError as e:  # already written into status.json
        sys.exit(str(e))
