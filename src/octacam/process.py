"""The post-recording pipeline behind `octacam process`: transcode each
recording's videos, build its grids, transfer it.

Every step reads the recording's own config snapshot and skips work already
done, so a re-run resumes where the last one stopped. An output older than its
source is not done work (`is_stale`).
"""

from __future__ import annotations

import contextlib
import functools
import logging
import shutil
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
    from octacam.cli.process import FileProgressBar
    from octacam.process_jobs import NullReporter

log = logging.getLogger("octacam")


@dataclass(frozen=True)
class ProcessOptions:
    """One run's choices, as `octacam process` takes them."""

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
    # Remove each local recording once it is verified at its destination
    # (None: its config's `[transfer].delete_after_transfer`).
    delete_after_transfer: bool | None = None

    def argv(self, folders: list[Path]) -> list[str]:
        """The `process` arguments that rerun these options on *folders*, for
        a detached job: every path absolute (the child runs from $HOME), and
        ffmpeg's raw output left out so the job's log stays line-oriented.
        """
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
        if self.delete_after_transfer is not None:
            argv.append(
                "--delete-after-transfer"
                if self.delete_after_transfer
                else "--no-delete-after-transfer"
            )
        if self.config_dir is not None:
            argv += ["--config", str(self.config_dir.resolve())]
        return argv + [str(Path(f).resolve()) for f in folders]


@dataclass
class ProcessContext:
    """What a plugin's processing steps get: this run's recordings that enable
    the plugin, where each was transferred (None: not transferred), each one's
    config, the run's options and its reporter (phases, pause gate).
    """

    folders: list[Path]
    dests: dict[Path, Path | None]
    configs: dict[Path, OctacamConfig]
    options: ProcessOptions
    reporter: NullReporter


class ProcessSteps:
    """A plugin's post-processing (`Plugin.process_steps`), every step a no-op
    by default. `after_transfer` runs once the recordings' own data is on its
    destination and before the grids: the plugin's data steps (e.g. pairing and
    copying its own acquisition data), so the data is usable early; a step that
    moves a transferred recording updates `ctx.dests`, where the grids then go.
    `render` runs last, after the grids: slow presentation outputs. Each
    returns how many files failed; neither should raise.
    """

    def transfer_dest(self, folder: Path, dest: Path) -> Path:
        """Where *folder* goes instead of *dest*: the folder an earlier run's
        steps moved its copy to, so a re-run does not create a second one.
        """
        return dest

    def after_transfer(self, ctx: ProcessContext) -> int:
        return 0

    def render(self, ctx: ProcessContext) -> int:
        return 0


@dataclass
class TranscodeJob:
    """One source file to transcode. A `.raw` stream carries no geometry, so
    the summary supplies it; encoded inputs leave these None.
    """

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
    failed_folders: set[Path] = field(default_factory=set)


def run(
    folders: list[Path],
    options: ProcessOptions,
    reporter: NullReporter,
    bar: Callable[[int], FileProgressBar] | None = None,
) -> None:
    """Process *folders*: recordings, their parents with `recursive`, or
    video files. *bar* makes a phase's progress bar over n files (None: none).

    Exits (SystemExit) naming the files that failed to transcode or transfer.
    A Ctrl-C during the transcodes ends the batch once the encode in progress
    is discarded, and re-raises KeyboardInterrupt without grids or transfers.
    """
    config = functools.cache(
        lambda folder: config_for_recording(folder, options.config_dir)
    )
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
                    "Interrupted \N{EM DASH} stopped after %d file(s); the in-progress "
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

    # The data first, the presentation outputs last: transfer the videos and
    # metadata, run the plugins' data steps, then build and transfer the grids
    # (hours for a full day) and run the plugins' renders.
    transfer_failed = 0
    if (options.grid or options.transfer) and done.outputs:
        configs = {folder: config(folder) for folder in done.outputs}
        steps = _plugin_steps(configs)

        def resolve_dest(folder: Path, dest: Path) -> Path:
            for plugin_steps, plugin_folders in steps:
                if folder in plugin_folders:
                    dest = plugin_steps.transfer_dest(folder, dest)
            return dest

        dests: dict[Path, Path | None] = {}
        # Each folder's failed copies: a folder with any keeps its local copy.
        failed_by_folder: dict[Path, int] = {}
        if options.transfer:
            failed, dests = transfer_outputs(
                done.outputs,
                {},
                options,
                configs,
                reporter,
                bar,
                resolve_dest=resolve_dest,
                failed_by_folder=failed_by_folder,
            )
            transfer_failed += failed
        for plugin_steps, plugin_folders in steps:
            failed = plugin_steps.after_transfer(
                ProcessContext(plugin_folders, dests, configs, options, reporter)
            )
            transfer_failed += failed
            for folder in plugin_folders if failed else ():
                failed_by_folder[folder] = failed_by_folder.get(folder, 0) + failed
        grids = (
            build_grids(done.outputs, done.rewritten, options, configs, reporter, bar)
            if options.grid
            else {}
        )
        if options.transfer and any(grids.values()):
            failed, _ = transfer_outputs(
                {folder: [] for folder in grids},
                grids,
                options,
                configs,
                reporter,
                bar,
                metadata=False,
                label="Transfer (grids)",
                dests=dests,  # as the plugins' steps left them
                failed_by_folder=failed_by_folder,
            )
            transfer_failed += failed
        if options.transfer:
            # After the grids, which are built from the local videos.
            delete_transferred(
                dests, failed_by_folder, done.failed_folders, options, configs
            )
        for plugin_steps, plugin_folders in steps:
            transfer_failed += plugin_steps.render(
                ProcessContext(plugin_folders, dests, configs, options, reporter)
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
    """Transcode each job to an `.mp4` beside it (on stdout, one per line),
    recording the outputs in *done*. A file that fails is counted, not fatal.
    """
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
                    "%s is older than %s \N{EM DASH} it is left over from an earlier "
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
                log.info(
                    "[dry-run] transcode: %s \N{RIGHTWARDS ARROW} %s",
                    source,
                    output.name,
                )
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
                    done.failed_folders.add(source.parent)
                    log.error("Failed to transcode %s: %s", source, e)
                    continue
                done.completed += 1
                if options.delete_source:
                    _delete_source(source)
            done.outputs.setdefault(source.parent, []).append(output)
            reporter.item_done()
    if options.dry_run:
        log.info(
            "[dry-run] Transcode: %d to transcode, %d already done", planned, skipped
        )
    else:
        log.info(
            "Transcode: %d done, %d skipped, %d failed%s",
            done.completed,
            skipped,
            done.failures,
            " (use --force to re-transcode existing)"
            if skipped and not options.force
            else "",
        )


def build_grids(
    outputs: dict[Path, list[Path]],
    rewritten: set[Path],
    options: ProcessOptions,
    configs: dict[Path, OctacamConfig],
    reporter: NullReporter,
    bar: Callable[[int], FileProgressBar] | None = None,
) -> dict[Path, list[Path]]:
    """Build each folder's `[[visualization]]` grids from its *outputs*;
    return the grid files (built, present or planned) to transfer.

    On a dry run *outputs* may name videos the transcode step only planned, and
    *rewritten* those it will rewrite: a grid of either is listed as work to do
    instead of being probed.
    """
    targets = [folder for folder in outputs if configs[folder].visualization]
    if not targets:
        log.info(
            "Grid: no [[visualization]] entry in the config \N{EM DASH} skipping grid "
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
                        "%s is older than the videos it composites \N{EM DASH} "
                        "rebuilding",
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
                        "[dry-run] grid: %s (waits for: %s)",
                        out_path,
                        ", ".join(waiting),
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
                    # A detached job has no bar: report the encode's own
                    # progress, so the GUI can say how long is left.
                    on_progress=progress.file(i, folder, "grid: ")
                    if progress
                    else reporter.transcode_progress(i, len(targets)),
                )
                if out is not None:
                    todo += 1
                    built.append(out)
            grids[folder] = built
            reporter.item_done()
    if options.dry_run:
        log.info("[dry-run] Grid: %d to build, %d already exist", todo, skipped)
    elif skipped:
        log.info(
            "Grid: %d already exist \N{EM DASH} skipping (use --force to rebuild)",
            skipped,
        )
    return grids


def _plugin_steps(
    configs: dict[Path, OctacamConfig],
) -> list[tuple[ProcessSteps, list[Path]]]:
    """Each enabled plugin's processing steps (`Plugin.process_steps`), with
    the recordings whose config enables it. A plugin that can't be imported is
    warned about and skipped: processing never needs its hardware.
    """
    from octacam.plugins import _ALIASES, plugin_class

    by_name: dict[str, list[Path]] = {}
    for folder, cfg in configs.items():
        for entry in cfg.plugins:
            name = _ALIASES.get(entry.name, entry.name)
            by_name.setdefault(name, [])
            if folder not in by_name[name]:
                by_name[name].append(folder)
    steps: list[tuple[ProcessSteps, list[Path]]] = []
    for name, folders in by_name.items():
        try:
            cls = plugin_class(name).process_steps
        except Exception as e:  # an unknown or unimportable plugin
            log.warning("Plugin %r: no processing steps (%s)", name, e)
            continue
        if cls is not None:
            steps.append((cls(), folders))
    return steps


def transfer_outputs(
    outputs: dict[Path, list[Path]],
    grids: dict[Path, list[Path]],
    options: ProcessOptions,
    configs: dict[Path, OctacamConfig],
    reporter: NullReporter,
    bar: Callable[[int], FileProgressBar] | None = None,
    *,
    metadata: bool = True,
    label: str = "Transfer",
    dests: dict[Path, Path | None] | None = None,
    resolve_dest: Callable[[Path, Path], Path] | None = None,
    failed_by_folder: dict[Path, int] | None = None,
) -> tuple[int, dict[Path, Path | None]]:
    """Copy each folder's outputs, grids and (with *metadata*) metadata to its
    `[transfer]` destination, under its summary's `relative_directory`; return
    how many files failed and each folder's destination (None: not
    transferred). *dests* gives the destinations instead (an earlier pass's);
    *resolve_dest* may move a computed one (`ProcessSteps.transfer_dest`).
    *label* names the pass in its log line; *failed_by_folder* adds up each
    folder's failures. A folder to be deleted afterwards (`deletes_local`) is
    content-verified, already-present files included.
    """
    copied = skipped = failed = 0
    given = dests
    result_dests: dict[Path, Path | None] = {}
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
            dest = given.get(folder) if given is not None else None
            result_dests[folder] = None
            if transfer is None or not transfer.directory:
                log.warning(
                    "No [transfer].directory resolvable for %s; skipping transfer",
                    folder,
                )
            elif given is None or dest is not None:
                if dest is None:
                    relative = (_read_summary(folder) or {}).get("relative_directory")
                    dest = Path(resolve_dir_template(transfer.directory)) / (
                        relative or folder.name
                    )
                    if resolve_dest is not None:
                        dest = resolve_dest(folder, dest)
                result_dests[folder] = dest
                deleting = deletes_local(folder, options, configs)
                result = transfer_folder(
                    folder,
                    dest,
                    files_only=files + grids.get(folder, []),
                    dry_run=options.dry_run,
                    verify=transfer.checksum or deleting,
                    on_progress=on_bar or reporter.transfer_progress(i, len(outputs)),
                    metadata=metadata,
                    by_content=deleting,
                )
                copied += len(result.copied)
                skipped += len(result.skipped)
                failed += len(result.failed)
                if failed_by_folder is not None and result.failed:
                    failed_by_folder[folder] = failed_by_folder.get(folder, 0) + len(
                        result.failed
                    )
            reporter.item_done()
    if options.dry_run:
        log.info(
            "[dry-run] %s: %d to copy, %d already up to date", label, copied, skipped
        )
    else:
        log.info("%s: %d copied, %d skipped, %d failed", label, copied, skipped, failed)
        if failed:
            log.error("%d file(s) failed to transfer", failed)
    return failed, result_dests


def deletes_local(
    folder: Path, options: ProcessOptions, configs: dict[Path, OctacamConfig]
) -> bool:
    """Whether this run removes *folder* once it is on its destination: the
    `--delete-after-transfer` flag, else its config's setting.
    """
    if options.delete_after_transfer is not None:
        return options.delete_after_transfer
    transfer = configs[folder].transfer
    return bool(transfer and transfer.delete_after_transfer)


def delete_transferred(
    dests: dict[Path, Path | None],
    failed_by_folder: dict[Path, int],
    transcode_failed: set[Path],
    options: ProcessOptions,
    configs: dict[Path, OctacamConfig],
) -> None:
    """Remove each local recording `deletes_local` names whose every file is
    content-verified at its destination: none failed to transcode, transfer or
    in a plugin's data step. A dry run lists them.
    """
    for folder, dest in dests.items():
        if dest is None or not deletes_local(folder, options, configs):
            continue
        if folder in transcode_failed:
            reason = "a file failed to transcode"
        elif failed_by_folder.get(folder):
            reason = f"{failed_by_folder[folder]} file(s) failed to transfer"
        else:
            reason = None
        if reason is not None:
            log.warning("Keeping local %s: %s", folder, reason)
        elif options.dry_run:
            log.info("[dry-run] would remove local %s (verified at %s)", folder, dest)
        else:
            try:
                shutil.rmtree(folder)
            except OSError as e:
                log.error("Could not remove local %s: %s", folder, e)
            else:
                log.info("Removed local %s (verified at %s)", folder, dest)


def is_stale(output: Path, source: Path) -> bool:
    """Whether a derived file predates its source.

    A folder recorded into twice keeps the previous take's `.mp4`/`grid.mp4`,
    which would otherwise pass as finished and be transferred as this take's.
    Equal mtimes count as current, a stat error as not stale.
    """
    try:
        return output.stat().st_mtime_ns < source.stat().st_mtime_ns
    except OSError:
        return False


def config_for_recording(folder: Path, fallback: Path | None) -> OctacamConfig:
    """The config governing one recording: its own snapshot (either layout),
    else the *fallback* config dir (`--config`), else built-in defaults.
    """
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
    """Resolve folders/files to a deduped list of `TranscodeJob`.

    A recording's summary (in either layout) supplies each camera's geometry;
    loose .mkv/.raw without one are transcoded with defaults and a warning. An
    `octacam_recording` folder named directly means its recording, and a
    recursive walk never enters one.
    """
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
                        (
                            e
                            for e in data.get("cameras", [])
                            if e.get("file") == path.name
                        ),
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
    *recursive*, deduped (see `find_recordings`).

    A root that is not a recording is warned about and skipped, so a stray
    folder never aborts the batch; if nothing is left and recordings lie
    beneath, it exits suggesting `-r`.
    """
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
                "it \N{EM DASH} pass -r/--recursive to include them. Skipping.",
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
            "No recording directory given directly \N{EM DASH} re-run with "
            "-r/--recursive "
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
    log.warning(
        "Skipping %s: recording captured 0 frames (empty header-only file)", video
    )


def _read_summary(folder: Path) -> dict | None:
    """*folder*'s summary, or None (with a warning) if unreadable."""
    try:
        return read_summary(folder)
    except (OSError, ValueError) as e:
        log.warning("Could not read %s: %s", recording_summary_path(folder), e)
        return None


def _delete_source(source: Path) -> None:
    """Delete a source video that has transcoded: only that file goes (never the
    recording's metadata), and a failure is only logged.
    """
    try:
        source.unlink(missing_ok=True)
    except OSError as e:
        log.warning("Could not remove %s: %s", source, e)
