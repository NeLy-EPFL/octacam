"""The post-recording pipeline behind ``octacam process``: transcode each
recording's videos, build its grids, transfer it.

Every step reads the recording's own config snapshot and skips work already
done, so a re-run resumes where the last one stopped. An output older than its
source is not done work (:func:`is_stale`).
"""

from __future__ import annotations

import contextlib
import functools
import logging
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from octacam import process_jobs, session_cache
from octacam.config import (
    OctacamConfig,
    find_config_file,
    load_config_dir,
    resolve_dir_template,
)
from octacam.files import is_partial
from octacam.grid import build_grid_video
from octacam.recording_format import (
    RECORDING_SUMMARY_FILENAME,
    find_recordings,
    is_recording_dir,
    read_summary,
    recording_folder,
    recording_info_dir,
    recording_summary_path,
    walk_folders,
)
from octacam.transcode import transcode_file
from octacam.transfer import transfer_folder

if TYPE_CHECKING:
    from octacam.cli import FileProgressBar
    from octacam.process_jobs import NullReporter

log = logging.getLogger("octacam")


@dataclass(frozen=True)
class ProcessOptions:
    """One run's choices, as ``octacam process`` takes them."""

    transcode: bool = True
    grid: bool = True
    transfer: bool = True
    force: bool = False  # redo transcodes and grids that exist
    recursive: bool = False
    delete_source: bool = False
    dry_run: bool = False
    ignore_capture: bool = False
    config_dir: Path | None = None  # for a recording with no config snapshot
    raw_output: bool = False  # stream ffmpeg's own output, not a progress bar

    def argv(self, folders: list[Path]) -> list[str]:
        """The ``process`` arguments that rerun these options on *folders*, for
        a detached job: every path absolute (the child runs from $HOME), and
        ffmpeg's raw output left out so the job's log stays line-oriented."""
        flags = {
            "--no-transcode": not self.transcode,
            "--no-grid": not self.grid,
            "--no-transfer": not self.transfer,
            "--force": self.force,
            "--recursive": self.recursive,
            "--delete-source": self.delete_source,
            "--dry-run": self.dry_run,
            "--ignore-capture": self.ignore_capture,
        }
        argv = [flag for flag, on in flags.items() if on]
        if self.config_dir is not None:
            argv += ["--config", str(self.config_dir.resolve())]
        return argv + [str(Path(f).resolve()) for f in folders]


@dataclass
class TranscodeJob:
    """One source file to transcode. A ``.raw`` stream carries no geometry, so
    the summary supplies it; encoded inputs leave these None."""

    input_path: Path
    frames: int | None = None
    width: int | None = None
    height: int | None = None
    fps: float | None = None
    pixel_format: str = "Mono8"


@dataclass
class Transcoded:
    """What the transcode step hands the grid and transfer steps."""

    # Each folder's outputs; on a dry run also the planned ones.
    outputs: dict[Path, list[Path]] = field(default_factory=dict)
    # Outputs this run (re)writes: their bytes are still the old ones, so a dry
    # run's grid preview plans around them.
    rewritten: set[Path] = field(default_factory=set)
    completed: int = 0
    failures: int = 0


def run(
    folders: list[Path],
    options: ProcessOptions,
    reporter: NullReporter,
    bar: Callable[[int], FileProgressBar] | None = None,
) -> None:
    """Process *folders*: recordings, their parents with ``recursive``, or
    video files. *bar* makes a phase's progress bar over n files (None: none).

    Exits (SystemExit) naming the files that failed to transcode or transfer.
    A Ctrl-C during the transcodes ends the batch once the encode in progress
    is discarded, and re-raises KeyboardInterrupt without grids or transfers."""
    config = functools.cache(lambda folder: config_for_recording(folder, options.config_dir))
    done = Transcoded()
    if options.transcode:
        jobs = transcode_jobs(folders, options.recursive)
        if not jobs:
            log.warning("No videos to transcode in: %s", ", ".join(map(str, folders)))
        else:
            try:
                transcode_videos(jobs, options, config, reporter, bar, done)
            except KeyboardInterrupt:
                log.warning(
                    "Interrupted — stopped after %d file(s); the in-progress "
                    "transcode was discarded.",
                    done.completed,
                )
                raise
    else:
        # The mp4s already present, minus orphaned partials from a hard kill.
        for folder in find_recording_dirs(folders, options.recursive):
            done.outputs.setdefault(
                folder, sorted(p for p in folder.glob("*.mp4") if not is_partial(p))
            )

    transfer_failed = 0
    if (options.grid or options.transfer) and done.outputs:
        configs = {folder: config(folder) for folder in done.outputs}
        grids = (
            build_grids(done.outputs, done.rewritten, options, configs, reporter, bar)
            if options.grid
            else {}
        )
        if options.transfer:
            transfer_failed = transfer_outputs(
                done.outputs, grids, options, configs, reporter, bar
            )

    problems = []
    if done.failures:
        problems.append(f"{done.failures} file(s) failed to transcode")
    if transfer_failed:
        problems.append(f"{transfer_failed} file(s) failed to transfer")
    if problems:
        sys.exit("; ".join(problems))


def transcode_videos(
    jobs: list[TranscodeJob],
    options: ProcessOptions,
    config: Callable[[Path], OctacamConfig],
    reporter: NullReporter,
    bar: Callable[[int], FileProgressBar] | None,
    done: Transcoded,
) -> None:
    """Transcode each job to an ``.mp4`` beside it (on stdout, one per line),
    recording the outputs in *done*. A file that fails is counted, not fatal."""
    skipped = planned = 0
    progress = bar(len(jobs)) if bar else None
    reporter.begin_phase("transcode", len(jobs))
    with (
        # A dry run encodes nothing: no transcode-active marker.
        contextlib.nullcontext()
        if options.dry_run
        else session_cache.mark_transcode_active(f"{len(jobs)} file(s)"),
        progress or contextlib.nullcontext(),
    ):
        for index, job in enumerate(jobs, 1):
            if not options.dry_run:  # a dry run (often wanted mid-session) never waits
                process_jobs.pause_gate(
                    reporter, unit="file", ignore_capture=options.ignore_capture
                )
            source = job.input_path
            output = source.with_suffix(".mp4")
            reporter.item_started(index, len(jobs), source)
            stale = output.exists() and is_stale(output, source)
            if stale:
                log.warning(
                    "%s is older than %s — it is left over from an earlier "
                    "recording in this folder; re-transcoding",
                    output.name,
                    source.name,
                )
                done.rewritten.add(output)
            if output.resolve() == source.resolve():
                log.warning("Skipping %s: already in target format (mp4)", source)
            elif output.exists() and not options.force and not stale:
                skipped += 1  # transcoded atomically, so a present .mp4 is complete
            elif options.dry_run:
                log.info("[dry-run] transcode: %s → %s", source, output.name)
                if options.delete_source:
                    log.info("[dry-run] would delete source: %s", source)
                planned += 1
                done.rewritten.add(output)
            else:
                ffmpeg_params = config(source.parent).transcode.ffmpeg_params
                on_progress = (
                    progress.file(index, source)
                    if progress
                    else reporter.transcode_progress(index, len(jobs))
                )
                try:
                    result = transcode_file(
                        source,
                        output,
                        ffmpeg_params=ffmpeg_params,
                        width=job.width,
                        height=job.height,
                        fps=job.fps,
                        pixel_format=job.pixel_format,
                        frames=job.frames,
                        on_progress=on_progress,
                        raw_output=options.raw_output,
                    )
                    print(result, flush=True)
                except Exception as e:  # one bad file must not abort the batch
                    done.failures += 1
                    log.error("Failed to transcode %s: %s", source, e)
                    continue
                done.completed += 1
                if options.delete_source:
                    _delete_source(source)
            done.outputs.setdefault(source.parent, []).append(output)
            reporter.item_done()
    if options.dry_run:
        log.info("[dry-run] Transcode: %d to transcode, %d already done", planned, skipped)
    else:
        log.info(
            "Transcode: %d done, %d skipped, %d failed%s",
            done.completed,
            skipped,
            done.failures,
            " (use --force to re-transcode existing)" if skipped and not options.force else "",
        )


def build_grids(
    outputs: dict[Path, list[Path]],
    rewritten: set[Path],
    options: ProcessOptions,
    configs: dict[Path, OctacamConfig],
    reporter: NullReporter,
    bar: Callable[[int], FileProgressBar] | None = None,
) -> dict[Path, list[Path]]:
    """Build each folder's ``[[visualization]]`` grids from its *outputs*;
    return the grid files (built, present or planned) to transfer.

    On a dry run *outputs* may name videos the transcode step only planned, and
    *rewritten* those it will rewrite: a grid of either is listed as work to do
    instead of being probed."""
    targets = [folder for folder in outputs if configs[folder].visualization]
    if not targets:
        log.info(
            "Grid: no [[visualization]] entry in the config — skipping grid "
            "generation (add one to the rig config to build a composite)"
        )
        return {}
    grids: dict[Path, list[Path]] = {}
    skipped = todo = 0
    progress = bar(len(targets)) if bar else None
    reporter.begin_phase("grid", len(targets))
    with progress or contextlib.nullcontext():
        for i, folder in enumerate(targets, 1):
            if not options.dry_run:
                process_jobs.pause_gate(
                    reporter, unit="folder", ignore_capture=options.ignore_capture
                )
            reporter.item_started(i, len(targets), folder)
            cfg = configs[folder]
            planned = (
                [p for p in outputs[folder] if not p.exists() or p in rewritten]
                if options.dry_run
                else []
            )
            # A grid is never an input, not even of another grid: under
            # --no-transcode the outputs are every *.mp4, and two grids would
            # mark each other stale and rebuild on every run.
            grid_paths = {folder / v.name for v in cfg.visualization}
            inputs = [p for p in outputs[folder] if p.exists() and p not in grid_paths]
            built: list[Path] = []
            for visualization in cfg.visualization:
                out_path = folder / visualization.name
                stale = out_path.exists() and any(is_stale(out_path, p) for p in inputs)
                if stale:
                    log.warning(
                        "%s is older than the videos it composites — rebuilding",
                        out_path.name,
                    )
                if out_path.exists() and not options.force and not stale:
                    # Built atomically, so a present grid is complete.
                    skipped += 1
                    built.append(out_path)
                    continue
                cells = {cell for row in visualization.layout for cell in row if cell}
                waiting = [p.name for p in planned if p.stem in cells]
                if waiting:
                    log.info(
                        "[dry-run] grid: %s (waits for: %s)", out_path, ", ".join(waiting)
                    )
                    todo += 1
                    built.append(out_path)
                    continue
                out = build_grid_video(
                    folder,
                    layout=visualization.layout,
                    output=out_path,
                    ffmpeg_params=visualization.ffmpeg_params
                    or cfg.transcode.ffmpeg_params,
                    dry_run=options.dry_run,
                    on_progress=progress.file(i, folder, "grid: ") if progress else None,
                )
                if out is not None:
                    todo += 1
                    built.append(out)
            grids[folder] = built
            reporter.item_done()
    if options.dry_run:
        log.info("[dry-run] Grid: %d to build, %d already exist", todo, skipped)
    elif skipped:
        log.info("Grid: %d already exist — skipping (use --force to rebuild)", skipped)
    return grids


def transfer_outputs(
    outputs: dict[Path, list[Path]],
    grids: dict[Path, list[Path]],
    options: ProcessOptions,
    configs: dict[Path, OctacamConfig],
    reporter: NullReporter,
    bar: Callable[[int], FileProgressBar] | None = None,
) -> int:
    """Copy each folder's outputs, grids and metadata to its ``[transfer]``
    destination, under its summary's ``relative_directory``; return how many
    files failed."""
    copied = skipped = failed = 0
    progress = bar(0) if bar else None
    reporter.begin_phase("transfer", len(outputs))
    with progress or contextlib.nullcontext():
        on_bar = progress.transfer_callback() if progress else None
        for i, (folder, files) in enumerate(outputs.items(), 1):
            if not options.dry_run:
                process_jobs.pause_gate(
                    reporter, unit="folder", ignore_capture=options.ignore_capture
                )
            reporter.item_started(i, len(outputs), folder)
            transfer = configs[folder].transfer
            if transfer is None or not transfer.directory:
                log.warning(
                    "No [transfer].directory resolvable for %s; skipping transfer", folder
                )
            else:
                relative = (_read_summary(folder) or {}).get("relative_directory")
                result = transfer_folder(
                    folder,
                    Path(resolve_dir_template(transfer.directory)) / (relative or folder.name),
                    files_only=files + grids.get(folder, []),
                    dry_run=options.dry_run,
                    verify=transfer.checksum,
                    on_progress=on_bar or reporter.transfer_progress(i, len(outputs)),
                )
                copied += len(result.copied)
                skipped += len(result.skipped)
                failed += len(result.failed)
            reporter.item_done()
    if options.dry_run:
        log.info("[dry-run] Transfer: %d to copy, %d already up to date", copied, skipped)
    else:
        log.info("Transfer: %d copied, %d skipped, %d failed", copied, skipped, failed)
        if failed:
            log.error("%d file(s) failed to transfer", failed)
    return failed


def is_stale(output: Path, source: Path) -> bool:
    """Whether a derived file predates its source.

    A folder recorded into twice keeps the previous take's ``.mp4``/``grid.mp4``,
    which would otherwise pass as finished and be transferred as this take's.
    Equal mtimes count as current, a stat error as not stale."""
    try:
        return output.stat().st_mtime_ns < source.stat().st_mtime_ns
    except OSError:
        return False


def config_for_recording(folder: Path, fallback: Path | None) -> OctacamConfig:
    """The config governing one recording: its own snapshot (either layout),
    else the *fallback* config dir (``--config``), else built-in defaults."""
    info_dir = recording_info_dir(folder)
    if find_config_file(info_dir).exists():
        return load_config_dir(info_dir)
    if fallback is not None:
        log.warning(
            "%s has no embedded config; falling back to --config %s", folder, fallback
        )
        return load_config_dir(fallback)
    log.warning(
        "%s has no embedded config and no --config given; using built-in defaults",
        folder,
    )
    return OctacamConfig()


def transcode_jobs(paths: list[Path], recursive: bool) -> list[TranscodeJob]:
    """Resolve folders/files to a deduped list of :class:`TranscodeJob`.

    A recording's summary (in either layout) supplies each camera's geometry;
    loose .mkv/.raw without one are transcoded with defaults and a warning. An
    ``octacam_recording`` folder named directly means its recording, and a
    recursive walk never enters one."""
    jobs: dict[Path, TranscodeJob] = {}

    def add(job: TranscodeJob) -> None:
        jobs.setdefault(job.input_path.resolve(), job)

    def handle_dir(directory: Path) -> None:
        summary_path = recording_summary_path(directory)
        if summary_path.exists():
            data = _read_summary(directory)
            if data is not None:
                fps_target = data.get("fps_target")
                for entry in data.get("cameras", []):
                    name = entry.get("file")
                    if not name:
                        continue
                    video = directory / name
                    if not video.exists():
                        log.warning("%s lists %s but it is missing", summary_path, name)
                    elif entry.get("frames") == 0:
                        _warn_zero_frames(video)
                    else:
                        add(_job_from_entry(video, entry, fps_target))
                return
        loose = sorted(
            p
            for p in directory.iterdir()
            if p.suffix in (".mkv", ".raw") and not is_partial(p)
        )
        if loose:
            log.warning(
                "No %s in %s; transcoding %d file(s) with defaults",
                RECORDING_SUMMARY_FILENAME,
                directory,
                len(loose),
            )
        for video in loose:
            add(TranscodeJob(input_path=video))

    for raw in paths:
        path = recording_folder(raw)
        if path != raw:
            log.info("%s is part of the recording in %s", raw, path)
        if path.is_dir():
            for directory in walk_folders(path) if recursive else [path]:
                handle_dir(directory)
        elif is_partial(path):  # an orphan from a hard kill
            log.warning("Skipping orphaned partial transcode: %s", path)
        else:
            entry = None
            fps_target = None
            if recording_summary_path(path.parent).exists():
                data = _read_summary(path.parent)
                if data is not None:
                    fps_target = data.get("fps_target")
                    entry = next(
                        (e for e in data.get("cameras", []) if e.get("file") == path.name),
                        None,
                    )
            if entry is not None and entry.get("frames") == 0:
                _warn_zero_frames(path)
            elif entry is not None:
                add(_job_from_entry(path, entry, fps_target))
            else:
                log.warning(
                    "No %s entry for %s; transcoding with defaults",
                    RECORDING_SUMMARY_FILENAME,
                    path,
                )
                add(TranscodeJob(input_path=path))

    return list(jobs.values())


def find_recording_dirs(roots: list[Path], recursive: bool) -> list[Path]:
    """The recordings (either layout) at *roots*, or under them when
    *recursive*, deduped (see :func:`~octacam.recording_format.find_recordings`).

    A root that is not a recording is warned about and skipped, so a stray
    folder never aborts the batch; if nothing is left and recordings lie
    beneath, it exits suggesting ``-r``."""
    result = find_recordings(roots, recursive)
    saw_nested = False
    for root in roots:
        folder = recording_folder(root)
        if folder != root:
            log.info("%s is part of the recording in %s", root, folder)
        if recursive or is_recording_dir(folder):
            continue
        nested = find_recordings([folder], recursive=True)
        if nested:
            saw_nested = True
            log.warning(
                "%s is not a recording directory; %d recording(s) found beneath "
                "it — pass -r/--recursive to include them. Skipping.",
                folder,
                len(nested),
            )
        else:
            log.warning(
                "%s is not a recording directory (no %s). Skipping.",
                folder,
                RECORDING_SUMMARY_FILENAME,
            )

    if not recursive and not result and saw_nested:
        sys.exit(
            "No recording directory given directly — re-run with -r/--recursive "
            "to copy the recordings found beneath the path(s) above."
        )
    return result


def _job_from_entry(video: Path, entry: dict, fps_target: float | None) -> TranscodeJob:
    frames = entry.get("frames")
    return TranscodeJob(
        input_path=video,
        frames=frames if isinstance(frames, int) else None,
        width=entry.get("width"),
        height=entry.get("height"),
        fps=entry.get("fps") or fps_target,
        pixel_format=entry.get("pixel_format") or "Mono8",
    )


def _warn_zero_frames(video: Path) -> None:
    # A header-only file: ffmpeg would fail with a cryptic EBML error.
    log.warning("Skipping %s: recording captured 0 frames (empty header-only file)", video)


def _read_summary(folder: Path) -> dict | None:
    """*folder*'s summary, or None (with a warning) if unreadable."""
    try:
        return read_summary(folder)
    except (OSError, ValueError) as e:
        log.warning("Could not read %s: %s", recording_summary_path(folder), e)
        return None


def _delete_source(source: Path) -> None:
    """Delete a source video that has transcoded: only that file goes (never the
    recording's metadata), and a failure is only logged."""
    try:
        source.unlink(missing_ok=True)
    except OSError as e:
        log.warning("Could not remove %s: %s", source, e)
