"""`octacam twophoton ...`: the 2P steps run by hand, on a rig's destination
and share (`--config`, the rig's config directory, names both; see
`settings`). `octacam process` runs the data ones itself for its recordings;
these cover the rest: data recorded before, by other means, or the checks.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Annotated

import typer

from octacam.cli._common import Verbose, command, resolve_config_arg

if TYPE_CHECKING:
    from octacam.plugins.twophoton.settings import TwoPhotonSettings

log = logging.getLogger("octacam")

app = typer.Typer(
    help="2-photon data on the destination: verify, sweep, renumber, render.",
    no_args_is_help=True,
)

ConfigDir = Annotated[
    Path,
    typer.Option(
        "--config",
        "-c",
        exists=True,
        file_okay=False,
        help="The rig's config directory: its [transfer].directory and the "
        "twophoton plugin's `source` and other options.",
    ),
]
DryRun = Annotated[
    bool, typer.Option("--dry-run", help="List what would be done, change nothing.")
]


UserOption = Annotated[
    str | None,
    typer.Option(
        "--user",
        "-u",
        help="A `[users.<initials>]` of the rig: their destination, share folder "
        "and 2P options (delete_share_data) instead of the rig's.",
    ),
]


def _rig(config_dir: Path, user: str | None = None) -> tuple[Path, TwoPhotonSettings]:
    """The rig's (or *user*'s) destination root and 2P settings; exits naming
    what's missing.
    """
    from octacam.config import load_config_dir, resolve_dir_template
    from octacam.plugins.twophoton.settings import (
        PLUGIN_NAME,
        TwoPhotonSettings,
        settings_for,
    )

    config_dir = resolve_config_arg(config_dir)
    cfg = load_config_dir(config_dir)
    profile = None
    if user is not None:
        profile = cfg.users.get(user.upper())
        if profile is None:
            sys.exit(f"no [users.{user.upper()}] in {config_dir}")
    directory = (profile.transfer_directory if profile else "") or (
        cfg.transfer.directory if cfg.transfer else ""
    )
    if not directory:
        sys.exit(f"no [transfer].directory in {config_dir}")
    settings = settings_for(cfg, config_dir / "octacam_config.toml")
    own = profile.plugins.get(PLUGIN_NAME, {}) if profile else {}
    if own:  # the user's own 2P options win over the rig's
        base = settings.model_dump() if settings else {}
        settings = TwoPhotonSettings.model_validate({**base, **own})
    if settings is None or not settings.source:
        sys.exit(f"the twophoton plugin has no `source` in {config_dir}")
    return Path(resolve_dir_template(directory)), settings


def _scan(settings: TwoPhotonSettings):
    from octacam.plugins.twophoton.matching import discover_twophoton_folders

    source = settings.source_root
    if source is None or not source.is_dir():
        sys.exit(f"the 2P share {source} is not reachable")
    return source, discover_twophoton_folders(source)


@app.command()
@command
def verify(
    config_dir: ConfigDir,
    paths: Annotated[
        list[Path] | None,
        typer.Argument(help="Only these experiment (or 2P) folders on the share."),
    ] = None,
    delete: Annotated[
        bool,
        typer.Option(
            "--delete",
            help="Delete the share folders found identical at the destination "
            "(only inside the share folder).",
        ),
    ] = False,
    user: UserOption = None,
    verbose: Verbose = False,
) -> None:
    """Which 2P folders on the share are verifiably at the destination, and so
    safe to delete. Compares every file (a stacked channel page by page);
    exits 1 on any mismatch. Read-only unless --delete, which deletes only the
    folders this run verified.
    """
    from octacam.plugins.twophoton.nas import NasIndex
    from octacam.plugins.twophoton.verify import verify as run
    from octacam.plugins.twophoton.verify import within

    dest_root, settings = _rig(config_dir, user)
    _source, folders = _scan(settings)
    if paths:
        folders = within(folders, paths)
    if not folders:
        log.info("2P verify: nothing found")
        return
    report = run(folders, NasIndex.build(dest_root), settings.settle_s)
    if delete and report.safe_paths:
        from octacam.plugins.twophoton.copying import remove_sources

        remove_sources(report.safe_paths, settings.source_root)
    if report.mismatched:
        sys.exit(f"{report.mismatched} folder(s) failed verification")


@app.command()
@command
def sweep(
    config_dir: ConfigDir,
    user: UserOption = None,
    dry_run: DryRun = False,
    verbose: Verbose = False,
) -> None:
    """Copy the share's settled 2P data no take claims, under its fly (else
    2p_only/<experiment>/<date>/), and pair takes still waiting for theirs.
    `octacam process` does this after each run.
    """
    from octacam.config import load_config_dir
    from octacam.plugins.twophoton.copying import CopyMode
    from octacam.plugins.twophoton.nas import NasIndex
    from octacam.plugins.twophoton.sweep import sweep as run

    dest_root, settings = _rig(config_dir, user)
    source, folders = _scan(settings)
    transfer = load_config_dir(resolve_config_arg(config_dir)).transfer
    checksum = bool(transfer and transfer.checksum)
    # The rig's delete after transfer applies to the share's folders too, when
    # their owner chose it (delete_share_data, in their user table).
    deleting = bool(transfer and transfer.delete_after_transfer)
    delete_share = deleting and settings.delete_share_data
    result = run(
        source,
        folders,
        NasIndex.build(dest_root),
        settings,
        CopyMode(
            verify=checksum or deleting,
            by_content=deleting,
            dry_run=dry_run,
            assemble=settings.assemble_tiff_stacks,
        ),
        delete_sources=delete_share,
    )
    if result.failed:
        sys.exit(f"{result.failed} file(s) failed to transfer")


@app.command()
@command
def manifest(
    config_dir: ConfigDir,
    dry_run: DryRun = False,
    verbose: Verbose = False,
) -> None:
    """Rewrite every day's 2p_reconciliation.md on the destination."""
    from octacam.plugins.twophoton.manifest import rebuild_manifests
    from octacam.plugins.twophoton.matching import discover_twophoton_folders
    from octacam.plugins.twophoton.nas import NasIndex
    from octacam.plugins.twophoton.sweep import fly_names

    dest_root, settings = _rig(config_dir)
    source = settings.source_root
    assert source is not None
    folders = discover_twophoton_folders(source) if source.is_dir() else None
    index = NasIndex.build(dest_root)
    names = fly_names(index) if settings.attribute_unclaimed_to_fly else {}
    written = rebuild_manifests(index, source, folders, names, dry_run=dry_run)
    log.info(
        "%s2P manifest: %d file(s) %s",
        "[dry-run] " if dry_run else "",
        written,
        "would be written" if dry_run else "written",
    )


@app.command()
@command
def reconcile(
    config_dir: ConfigDir,
    paths: Annotated[
        list[Path] | None,
        typer.Argument(help="Day (experiment) or fly folders on the destination."),
    ] = None,
    all_: Annotated[
        bool,
        typer.Option(
            "--all",
            help="Every fly on the destination (slow over a share), and turn the "
            "generic bucket's groups into new flies.",
        ),
    ] = False,
    dry_run: DryRun = False,
    verbose: Verbose = False,
) -> None:
    """Renumber flies' sessions into one RecordingN_<Beh|2P|Synced> sequence.
    `octacam process` does this for the flies it touches; this covers older
    data and flies spread over several runs. Every rename goes to the fly's
    reconciliation_log.md.
    """
    from octacam.plugins.twophoton.nas import NasIndex
    from octacam.plugins.twophoton.reconcile import (
        fly_dirs_under,
        fly_sessions,
        promote_generic,
        reconcile_flies,
    )
    from octacam.plugins.twophoton.reconcile import reconcile as run
    from octacam.plugins.twophoton.sweep import fly_names

    dest_root, _settings = _rig(config_dir)
    if not paths and not all_:
        sys.exit(
            "name day or fly folders, or pass --all (walks the whole destination "
            f"{dest_root})"
        )
    index = NasIndex.build(dest_root)
    if not all_:
        flies = fly_dirs_under(dest_root, list(paths or []))
        if not flies:
            sys.exit(f"no Fly*/ folder under {', '.join(map(str, paths or []))}")
        reconcile_flies(index, flies, dry_run)
        return
    sessions = fly_sessions(index)
    promoted = promote_generic(index, fly_names(index), dry_run)
    for fly_dir, entries in promoted.items():
        sessions.setdefault(fly_dir, []).extend(entries)
    moved, unchanged, conflicts = run(sessions, dest_root, dry_run)
    log.info(
        "%sreconcile: %d moved, %d already right, %d needing attention, %d new fly "
        "folder(s)",
        "[dry-run] " if dry_run else "",
        moved,
        unchanged,
        conflicts,
        len(promoted),
    )


@app.command()
@command
def reassemble(
    config_dir: ConfigDir,
    paths: Annotated[
        list[Path] | None,
        typer.Argument(help="Folders to search (default: the whole destination)."),
    ] = None,
    dry_run: DryRun = False,
    verbose: Verbose = False,
) -> None:
    """Stack ThorImage folders still holding per-frame TIFFs (copied by hand,
    or before stacking existed): one stack per channel, every page compared
    with its frame, then the frames deleted.
    """
    from octacam.plugins.twophoton.matching import EXPERIMENT_XML_FILENAME
    from octacam.plugins.twophoton.tiff import (
        assemble_channel_stack,
        make_progress_logger,
        plan_assembly,
        tifffile_available,
    )

    if not tifffile_available():
        sys.exit("stacking needs tifffile: install octacam[twophoton]")
    dest_root, _settings = _rig(config_dir)
    roots = list(paths or [dest_root])
    assembled = problems = 0
    for xml in sorted(
        {x for root in roots for x in root.rglob(EXPERIMENT_XML_FILENAME)}
    ):
        folder = xml.parent
        plan = plan_assembly(folder)
        for ch in plan.problems:
            problems += 1
            log.warning(
                "reassemble: %s %s: %s \N{EM DASH} left as is",
                folder,
                ch.channel,
                ch.reason,
            )
        for ch in plan.ready:
            stack = folder / ch.dest_name
            if dry_run:
                log.info(
                    "[dry-run] reassemble: %s %s: %d frame(s) -> %s, then delete them",
                    folder,
                    ch.channel,
                    len(ch.source_files),
                    stack,
                )
                continue
            result = assemble_channel_stack(
                ch.source_files,
                stack,
                axis=ch.axis or "T",
                experiment_xml=xml,
                full_verify=True,
                on_progress=make_progress_logger(folder, ch.channel),
                declared_count=ch.declared_count,
            )
            if not result.ok:
                problems += 1
                log.error(
                    "reassemble: %s %s: %s \N{EM DASH} frames left untouched",
                    folder,
                    ch.channel,
                    result.error,
                )
                continue
            assembled += 1
            removed = 0
            for src in ch.source_files:
                try:
                    src.unlink()
                    removed += 1
                except OSError as e:
                    log.warning("reassemble: could not remove %s: %s", src, e)
            log.info(
                "reassemble: %s %s: %d frame(s) -> %s (verified); %d removed",
                folder,
                ch.channel,
                result.frame_count,
                stack,
                removed,
            )
    if not dry_run:
        log.info("reassemble: %d stack(s), %d needing attention", assembled, problems)
    if problems:
        sys.exit(f"{problems} channel(s) need attention \N{EM DASH} see the warnings")


@app.command()
@command
def render(
    paths: Annotated[
        list[Path],
        typer.Argument(
            exists=True,
            file_okay=False,
            help="Transferred takes, or any folder above them (searched).",
        ),
    ],
    synced: Annotated[
        bool, typer.Option("--synced/--no-synced", help="Behavior + 2P videos.")
    ] = True,
    mip: Annotated[bool, typer.Option("--mip/--no-mip", help="Z-stack MIPs.")] = True,
    force: Annotated[
        bool, typer.Option("--force", help="Re-render existing outputs.")
    ] = False,
    green: Annotated[
        str, typer.Option(help="ThorImage channel shown green.")
    ] = "ChanA",
    red: Annotated[str, typer.Option(help="ThorImage channel shown red.")] = "ChanB",
    fastz_projection: Annotated[
        str, typer.Option(help="A FastZ timepoint's planes: mean or max.")
    ] = "mean",
    fps: Annotated[float, typer.Option(help="Synced video frame rate.")] = 25.0,
    height: Annotated[int, typer.Option(help="Synced video height (px).")] = 720,
    smooth: Annotated[int, typer.Option(help="2P box-blur radius (px, 0: off).")] = 1,
    dry_run: DryRun = False,
    verbose: Verbose = False,
) -> None:
    """Render the synced behavior + 2P video (synced_2p.mp4, beside the grid)
    of each paired take and the MIPs of each Z-stack under *paths*: older data,
    or other settings (--force). `octacam process` renders new data itself.
    """
    from octacam.plugins.twophoton.layout import MATCH_FILENAME
    from octacam.plugins.twophoton.mip import (
        MipOptions,
        find_zstacks,
        render_zstack_mips,
    )
    from octacam.plugins.twophoton.render import (
        SyncedRenderOptions,
        find_synced_inputs,
        render_synced_video,
    )
    from octacam.recording_format import recording_folder

    if fastz_projection not in ("mean", "max"):
        sys.exit("--fastz-projection is mean or max")
    takes = (
        sorted(
            {recording_folder(m.parent) for p in paths for m in p.rglob(MATCH_FILENAME)}
        )
        if synced
        else []
    )
    zstacks = sorted({z for p in paths for z in find_zstacks(p)}) if mip else []
    if not takes and not zstacks:
        log.warning("render: nothing to render under %s", ", ".join(map(str, paths)))
        return
    synced_options = SyncedRenderOptions(
        green_channel=green,
        red_channel=red,
        fastz_projection=fastz_projection,
        fps=fps,
        height=height,
        smooth_px=smooth,
    )
    mip_options = MipOptions(green_channel=green, red_channel=red)
    done = skipped = 0
    for take in takes:
        if dry_run:
            inputs = find_synced_inputs(take)
            log.info(
                "[dry-run] render synced: %s: %s",
                take,
                inputs if isinstance(inputs, str) else "would render",
            )
        elif render_synced_video(take, synced_options, force=force) is not None:
            done += 1
        else:
            skipped += 1
    for zstack in zstacks:
        if dry_run:
            log.info("[dry-run] render MIP: %s", zstack)
        elif render_zstack_mips(zstack, mip_options, force=force) is not None:
            done += 1
        else:
            skipped += 1
    if not dry_run:
        log.info("render: %d rendered or present, %d skipped or failed", done, skipped)


@app.command("board-sync")
@command
def board_sync(
    paths: Annotated[
        list[Path],
        typer.Argument(
            exists=True,
            file_okay=False,
            help="Transferred takes, or any folder above them (searched).",
        ),
    ],
    reference: Annotated[
        list[Path] | None,
        typer.Option(
            "--reference",
            "-r",
            exists=True,
            file_okay=False,
            help="More takes (or folders above them) to measure this rig's scan "
            "timing on, for takes whose board sent no report (before "
            "2026-10-14): ones with ThorSync or a board report, other days too.",
        ),
    ] = None,
    airflow_logs: Annotated[
        Path | None,
        typer.Option(
            "--airflow-logs",
            exists=True,
            file_okay=False,
            help="airflow_control's log folder: each take aligned here also gets "
            "the air commands logged during it (its missing AirFlow channel).",
        ),
    ] = None,
    dry_run: DryRun = False,
    verbose: Verbose = False,
) -> None:
    """Align each take under *paths* paired without ThorSync on the trigger
    board's clock (twophoton_board_sync.json, read by the synced render): for
    takes transferred before `octacam process` did it. Search a whole day, so
    its completed takes lend their 2P period to the stopped-early ones.
    """
    from octacam.plugins.twophoton.board_sync import write_board_syncs
    from octacam.plugins.twophoton.layout import MATCH_FILENAME
    from octacam.recording_format import recording_folder

    def takes_under(roots: list[Path]) -> list[Path]:
        return sorted(
            {recording_folder(m.parent) for p in roots for m in p.rglob(MATCH_FILENAME)}
        )

    takes = takes_under(paths)
    written = write_board_syncs(
        takes, references=takes_under(reference or []), dry_run=dry_run
    )
    if airflow_logs is not None and written:
        from octacam.plugins.twophoton.airflow import attach_airflow_commands

        attach_airflow_commands(written, airflow_logs, dry_run=dry_run)
    log.info(
        "board-sync: %d of %d take(s) aligned on the board's clock%s",
        len(written),
        len(takes),
        " (dry run)" if dry_run else "",
    )
