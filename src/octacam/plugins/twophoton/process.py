"""`octacam process`'s 2P steps (`TwoPhotonSteps`), for recordings that enable
the twophoton plugin and name a 2P `source` (see `settings`).

Right after the recordings' own transfer (the data first):

1. pair each armed take with its ThorSync/ThorImage folders on the share (by
   the ThorSync signals, else start time; `matching`) and copy them into it as
   `<take>/2P/<name>`, ThorImage's frames as one stack per channel; a take whose
   2P data is still being written waits (`twophoton_pending.json`);
2. sweep the share for settled 2P data nobody claims, filed under its fly;
3. rewrite the touched days' `2p_reconciliation.md`;
4. renumber the touched flies' sessions (`RecordingN_<Beh|2P|Synced>`), and
   point the grids at the renamed takes.

Last, after the grids: the synced behavior + 2P video of each newly paired take
and the touched flies' Z-stack projections, from the destination's copy.

The share is scanned and the destination walked once per run.
"""

from __future__ import annotations

import functools
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from octacam.config import resolve_dir_template
from octacam.plugins.twophoton.copying import (
    CopyMode,
    finish_pairing,
    remove_sources,
    write_pending,
)
from octacam.plugins.twophoton.matching import (
    TakeInfo,
    TwoPhotonFolder,
    discover_twophoton_folders,
    is_settled,
    match_takes_to_twophoton_batch,
)
from octacam.plugins.twophoton.nas import NasIndex
from octacam.plugins.twophoton.settings import TwoPhotonSettings, settings_for
from octacam.plugins.twophoton.takes import (
    current_take_dest,
    is_armed,
    match_start_s,
    start_s,
    stop_idle_s,
    stopped_early,
    summary_of,
)
from octacam.process import ProcessContext, ProcessSteps, deletes_local
from octacam.recording_format import CONFIG_SNAPSHOT_FILENAME, recording_info_dir

log = logging.getLogger("octacam")


@dataclass
class _Target:
    """The run's recordings sharing one share and destination."""

    source_root: Path
    dest_root: Path
    settings: TwoPhotonSettings
    folders: list[Path] = field(default_factory=list)


@dataclass
class _Render:
    """A synced render to make: the take's destination when queued, its start."""

    dest: Path
    start: float | None
    settings: TwoPhotonSettings


class TwoPhotonSteps(ProcessSteps):
    def __init__(self) -> None:
        self._renders: list[_Render] = []
        # Fly folders this run touched, with their settings (Z-stack renders).
        self._flies: dict[Path, TwoPhotonSettings] = {}

    # ------------------------------------------------------------ dests

    def transfer_dest(self, folder: Path, dest: Path) -> Path:
        """An earlier run may have renumbered the take (`RecordingN_*`)."""
        return current_take_dest(dest, start_s(summary_of(folder)))

    # ------------------------------------------------------------ data

    def after_transfer(self, ctx: ProcessContext) -> int:
        targets = self._targets(ctx)
        failed = 0
        for target in targets:
            try:
                failed += self._run_target(target, ctx)
            except Exception:  # a step must never take down the run
                log.exception("2P processing of %s failed", target.source_root)
                failed += 1
        return failed

    def _targets(self, ctx: ProcessContext) -> list[_Target]:
        targets: dict[tuple[Path, Path, str], _Target] = {}
        for folder in ctx.folders:
            dest = ctx.dests.get(folder)
            transfer = ctx.configs[folder].transfer
            if dest is None or transfer is None or not transfer.directory:
                continue
            snapshot = recording_info_dir(folder) / CONFIG_SNAPSHOT_FILENAME
            settings = settings_for(ctx.configs[folder], snapshot)
            if settings is None or settings.source_root is None:
                continue
            dest_root = Path(resolve_dir_template(transfer.directory))
            key = (settings.source_root, dest_root, settings.model_dump_json())
            target = targets.setdefault(
                key, _Target(settings.source_root, dest_root, settings)
            )
            target.folders.append(folder)
        return list(targets.values())

    def _run_target(self, target: _Target, ctx: ProcessContext) -> int:
        settings = target.settings
        options = ctx.options
        deleting = {f: deletes_local(f, options, ctx.configs) for f in target.folders}
        transfer = ctx.configs[target.folders[0]].transfer
        checksum = bool(transfer and transfer.checksum)
        scan = (
            discover_twophoton_folders(target.source_root)
            if target.source_root.is_dir()
            else None
        )
        if scan is None:
            log.warning(
                "2P: the share %s is not reachable \N{EM DASH} no 2P data paired or "
                "copied this run",
                target.source_root,
            )
        failed, claimed = self._pair(target, ctx, scan or [], deleting, checksum)

        index = NasIndex.build(target.dest_root)
        mode = CopyMode(
            verify=checksum or any(deleting.values()),
            by_content=any(deleting.values()),
            dry_run=options.dry_run,
            assemble=settings.assemble_tiff_stacks,
        )
        from octacam.plugins.twophoton.sweep import fly_names, sweep

        if scan:
            swept = sweep(
                target.source_root,
                scan,
                index,
                settings,
                mode,
                claimed_this_run=frozenset(claimed),
                delete_sources=any(deleting.values()),
            )
            failed += swept.failed
            self._renders += [_Render(d, s, settings) for d, s in swept.renders]

        # A take paired without ThorSync is aligned on the trigger board's clock
        # (its box in ThorImage unticked); the run's other takes lend their
        # periods to a stopped-early one.
        from octacam.plugins.twophoton.board_sync import write_board_syncs

        write_board_syncs(
            sorted(
                {d for f in target.folders if (d := ctx.dests.get(f)) is not None}
                | {r.dest for r in self._renders if r.settings is settings}
            ),
            dry_run=options.dry_run,
        )

        days = {
            target.dest_root / dest.relative_to(target.dest_root).parts[0]
            for f in target.folders
            if (dest := ctx.dests.get(f)) is not None
            and dest.is_relative_to(target.dest_root)
        }
        fly_dirs = {dest.parent for f in target.folders if (dest := ctx.dests.get(f))}
        if settings.reconcile_recordings:
            from octacam.plugins.twophoton.reconcile import reconcile_flies

            reconcile_flies(index, fly_dirs, options.dry_run)
            for folder in target.folders:  # the grids go to the renamed take
                dest = ctx.dests.get(folder)
                if dest is not None:
                    ctx.dests[folder] = current_take_dest(
                        dest, start_s(summary_of(folder))
                    )

        if settings.write_manifest:
            # After the sweep and the reconcile: the takes under their final
            # names, and only what is still on the share.
            from octacam.plugins.twophoton.manifest import rebuild_manifests

            names = fly_names(index) if settings.attribute_unclaimed_to_fly else {}
            remaining = (
                [f for f in scan if options.dry_run or f.path.exists()]
                if scan is not None
                else None
            )
            rebuild_manifests(
                index,
                target.source_root,
                remaining,
                names,
                dry_run=options.dry_run,
                days=days,
            )
        for fly_dir in fly_dirs:
            self._flies.setdefault(fly_dir, settings)
        return failed

    def _pair(
        self,
        target: _Target,
        ctx: ProcessContext,
        scan: list[TwoPhotonFolder],
        deleting: dict[Path, bool],
        checksum: bool,
    ) -> tuple[int, set[Path]]:
        """Pair and copy each armed take's 2P data. Returns the failures and
        the share folders this run claimed (a dry run writes no record).
        """
        settings = target.settings
        takes: dict[Path, TakeInfo] = {}
        for folder in target.folders:
            summary = summary_of(folder)
            if not is_armed(folder, summary):
                continue
            start = match_start_s(folder, summary)
            duration_s = summary.get("duration_s")
            if start is None or duration_s is None:
                log.warning("2P: %s has no start time or duration; not paired", folder)
                continue
            takes[folder] = TakeInfo(
                folder=folder,
                start_time=start,
                duration_s=duration_s,
                camera_frame_counts=[
                    c["frames"]
                    for c in summary.get("cameras", [])
                    if isinstance(c.get("frames"), int)
                ],
                stopped_early=stopped_early(summary),
                stop_idle_s=stop_idle_s(folder, summary),
            )
        if not takes:
            return 0, set()
        batch = match_takes_to_twophoton_batch(
            list(takes.values()),
            scan,
            settings.match_window_s,
            settings.verify_window_s,
            settings.verify_with_signals,
        )
        failed = 0
        claimed: set[Path] = set()
        for folder, matches in batch.items():
            dest = ctx.dests[folder]
            assert dest is not None
            mode = CopyMode(
                verify=checksum or deleting[folder],
                by_content=deleting[folder],
                dry_run=ctx.options.dry_run,
                assemble=settings.assemble_tiff_stacks,
            )
            for m in matches:
                if m.ambiguous:
                    log.warning(
                        "2P: more than one %s folder fits %s \N{EM DASH} took the "
                        "closest (%s, %.1f s off)",
                        m.folder.kind,
                        folder,
                        m.folder.path,
                        m.gap_s,
                    )
            claimed.update(m.folder.path.resolve() for m in matches)
            writing = [
                m for m in matches if not is_settled(m.folder, settings.settle_s)
            ]
            if not matches or writing:
                # Never half a pair: the settled ThorSync folder alone, while
                # ThorImage still writes its TIFFs, lost the rest for good.
                if writing:
                    log.info(
                        "2P: %s still being written for %s \N{EM DASH} the take "
                        "waits for a later run",
                        ", ".join(m.folder.path.name for m in writing),
                        folder,
                    )
                else:
                    log.info("2P: no 2P data found for %s (yet)", folder)
                if not ctx.options.dry_run:
                    write_pending(folder, takes[folder], dest, target.source_root, mode)
                continue
            if not any(m.folder.kind == "sync" for m in matches):
                log.warning(
                    "2P: no ThorSync recording for %s (ThorImage %s) \N{EM DASH} "
                    "was ThorSync ticked in ThorImage? No AirFlow; paired by "
                    "time and aligned on the trigger board's clock instead",
                    folder,
                    ", ".join(m.folder.path.name for m in matches),
                )
            paired = finish_pairing(folder, dest, matches, target.source_root, mode)
            failed += paired.failed
            if paired.complete:
                self._renders.append(
                    _Render(dest, start_s(summary_of(folder)), settings)
                )
                if deleting[folder] and not ctx.options.dry_run:
                    remove_sources(paired.copied_ok)
        return failed, claimed

    # ------------------------------------------------------------ renders

    def render(self, ctx: ProcessContext) -> int:
        if ctx.options.dry_run:
            return 0
        from octacam.plugins.twophoton.mip import find_zstacks, render_zstack_mips
        from octacam.plugins.twophoton.mip import options_from_config as mip_options
        from octacam.plugins.twophoton.render import (
            options_from_config,
            render_synced_video,
        )

        # Each job renders one output: a take's synced video, a Z-stack's MIPs.
        jobs: list[tuple[Path, Callable[[], object]]] = []
        for item in self._renders:
            synced = options_from_config(item.settings)
            if synced is not None:
                take = current_take_dest(item.dest, item.start)
                jobs.append(
                    (take, functools.partial(render_synced_video, take, synced))
                )
        for fly_dir, settings in self._flies.items():
            mips = mip_options(settings)
            if mips is not None and fly_dir.is_dir():
                jobs += [
                    (z, functools.partial(render_zstack_mips, z, mips))
                    for z in find_zstacks(fly_dir)
                ]
        if not jobs:
            return 0
        from octacam import process_jobs

        reporter = ctx.reporter
        reporter.begin_phase("render", len(jobs))
        failed = 0
        for i, (path, job) in enumerate(jobs, 1):
            process_jobs.pause_gate(
                reporter, unit="render", ignore_capture=ctx.options.ignore_capture
            )
            reporter.item_started(i, len(jobs), path)
            try:
                job()
            except Exception:
                log.exception("2P render of %s failed", path)
                failed += 1
            reporter.item_done()
        return failed
