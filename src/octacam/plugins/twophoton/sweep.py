"""2P data no take claimed: settled share folders are copied under the fly they
belong to (`<fly>/2P_only/<name>`, by ThorImage's own names), else into
`2p_only/<experiment>/<date>/<name>`; and a take transferred before its 2P data
settled is paired once it has.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from pathlib import Path

from octacam import session_cache
from octacam.plugins.twophoton.copying import (
    CopyMode,
    copy_twophoton_folder,
    finish_pairing,
    remove_sources,
    write_source_marker,
)
from octacam.plugins.twophoton.layout import MATCH_FILENAME, TWOPHOTON_ONLY_DIRNAME
from octacam.plugins.twophoton.matching import (
    TakeInfo,
    TwoPhotonFolder,
    _fly_prefix,
    _thorimage_base_name,
    correlate_sync_folder_to_image,
    is_settled,
    match_takes_to_twophoton_batch,
    twophoton_identity,
)
from octacam.plugins.twophoton.nas import GENERIC_DIRNAME, NasIndex
from octacam.plugins.twophoton.settings import TwoPhotonSettings
from octacam.plugins.twophoton.takes import (
    current_take_dest,
    match_start_s,
    pending_record,
    read_json,
    sidecar,
    start_s,
    summary_of,
)

log = logging.getLogger("octacam")

# (experiment, ThorImage base name or "fly:<FlyN>") -> the fly folders whose data
# carries it.
FlyNames = dict[tuple[str, str], set[Path]]


def _add_fly_name(names: FlyNames, experiment: str, name: str, fly_dir: Path) -> None:
    names.setdefault((experiment, _thorimage_base_name(name)), set()).add(fly_dir)
    token = _fly_prefix(name)
    if token is not None:
        names.setdefault((experiment, f"fly:{token}"), set()).add(fly_dir)


def fly_names(index: NasIndex) -> FlyNames:
    """Each fly's ThorImage names: from its takes' paired ThorImage folders,
    and its 2P-only folders' source markers (a fly with no take at all).
    ThorImage names a fly's sessions consistently (`Fly1`, `Fly1_001`,
    `Fly1_Zstack`) even when its numbers differ from octacam's.
    """
    names: FlyNames = {}
    for take in index.takes:
        fly_dir = take.fly_dir(index.root)
        if fly_dir is None:
            continue
        for entry in take.match.get("matched", []):
            if entry.get("kind") == "image" and entry.get("path"):
                p = Path(entry["path"])
                _add_fly_name(names, p.parent.name, p.name, fly_dir)
    for leaf, experiment in index.source_markers.items():
        parts = leaf.relative_to(index.root).parts
        if len(parts) >= 2:
            _add_fly_name(
                names, experiment, leaf.name, index.root / parts[0] / parts[1]
            )
    return names


def attribute(
    folder: TwoPhotonFolder,
    experiment: str,
    names: FlyNames,
    image_candidates: list[TwoPhotonFolder],
) -> Path | None:
    """The fly folder *folder* (unclaimed) belongs to, or None (the generic
    bucket): the one fly of the same day whose ThorImage names it extends
    (`Fly1_Zstack` for `Fly1`), else whose `Fly<N>` token it shares. A ThorSync
    folder carries no name: it goes with the ThorImage folder its signals
    match (`correlate_sync_folder_to_image`). Never guesses between flies.
    """
    name = folder.path.name
    if folder.kind != "image":
        image = correlate_sync_folder_to_image(folder, image_candidates)
        if image is None:
            return None
        name = image.path.name
    # ThorImage reuses names every day (Fly1_Zstack in many experiments): only
    # a fly of the same day (day folders are YYMMDD_...) can claim one. Real
    # 2026-10-14: today's Fly1_Zstack was filed under September's Fly1.
    day = time.strftime("%y%m%d", time.localtime(folder.start_time))

    def same_day(fly_dir: Path) -> bool:
        prefix = fly_dir.parent.name[:6]
        return not prefix.isdigit() or prefix == day

    found = {
        fly_dir
        for (exp, base), fly_dirs in names.items()
        if exp == experiment and (name == base or name.startswith(base + "_"))
        for fly_dir in fly_dirs
        if same_day(fly_dir)
    }
    if not found:
        token = _fly_prefix(name)
        if token is not None:
            found = {
                d for d in names.get((experiment, f"fly:{token}"), set()) if same_day(d)
            }
    return next(iter(found)) if len(found) == 1 else None


def locally_matched(source_root: Path) -> set[Path]:
    """Share folders a cached local take is paired with (it may not be on the
    destination yet: a dry run, or a copy still pending).
    """
    matched: set[Path] = set()
    for folder in session_cache.all_folders():
        record = read_json(sidecar(folder, MATCH_FILENAME)) or {}
        for entry in record.get("matched", []):
            if entry.get("path"):
                matched.add((source_root / entry["path"]).resolve())
    return matched


@dataclass
class PendingTake:
    folder: Path
    take: TakeInfo
    record: dict


def pending_takes(source_root: Path) -> list[PendingTake]:
    """Cached local takes still waiting for 2P data from *source_root*. A take
    whose local folder is gone (deleted, or past the cache's retention) cannot
    be found this way.
    """
    resolved = source_root.resolve()
    pending = []
    for folder in session_cache.all_folders():
        record = pending_record(folder)
        if not record:
            continue
        try:
            if Path(record["source_root"]).resolve() != resolved:
                continue
        except KeyError, OSError:
            continue
        summary = summary_of(folder)
        take = TakeInfo(
            folder=folder,
            start_time=match_start_s(folder, summary) or record["start_time"],
            duration_s=record["duration_s"],
            camera_frame_counts=record.get("camera_frame_counts", []),
        )
        pending.append(PendingTake(folder, take, record))
    return pending


@dataclass
class SweepResult:
    copied: int = 0
    failed: int = 0
    # Synced renders for takes paired late: (take dest, take start).
    renders: list[tuple[Path, float | None]] = field(default_factory=list)


def sweep(
    source_root: Path,
    folders: list[TwoPhotonFolder],
    index: NasIndex,
    settings: TwoPhotonSettings,
    mode: CopyMode,
    *,
    claimed_this_run: frozenset[Path] = frozenset(),
) -> SweepResult:
    """Pair pending takes with share folders that have settled since, then copy
    every settled folder nobody claims: not paired with a take (this run's,
    cached local ones', or any on the destination) and not on the destination
    already (by acquisition identity, as names repeat across experiments).
    *folders* is this run's scan of *source_root*.
    """
    result = SweepResult()
    claimed = (
        index.matched_sources(source_root)
        | locally_matched(source_root)
        | claimed_this_run
    )
    unclaimed = [
        f
        for f in folders
        if f.path.resolve() not in claimed and not _on_destination(f, index)
    ]
    held = _pair_pending(source_root, unclaimed, settings, mode, result, index)
    unclaimed = [f for f in unclaimed if f.path.resolve() not in held]
    todo = [f for f in unclaimed if is_settled(f, settings.settle_s)]
    if not todo:
        log.info("2P sweep: nothing new and settled under %s", source_root)
        return result

    names = fly_names(index) if settings.attribute_unclaimed_to_fly else {}
    for folder in todo:
        experiment = folder.path.parent.name
        images = [
            c for c in folders if c.kind == "image" and c.path.parent.name == experiment
        ]
        fly_dir = attribute(folder, experiment, names, images) if names else None
        if fly_dir is not None:
            dest = fly_dir / TWOPHOTON_ONLY_DIRNAME / folder.path.name
        else:
            day = time.strftime("%y%m%d", time.localtime(folder.start_time))
            dest = index.root / GENERIC_DIRNAME / experiment / day / folder.path.name
        if folder.kind == "sync":
            from octacam.plugins.twophoton.signals import (
                describe_cut_off,
                episode_cut_off,
            )

            cut = episode_cut_off(folder.path)
            if cut is not None:
                log.warning("2P sweep: %s", describe_cut_off(folder.path.name, cut))
        copied = copy_twophoton_folder(folder, dest, mode)
        result.copied += len(copied.copied)
        result.failed += len(copied.failed)
        if not mode.dry_run and not copied.failed:
            log.info("2P sweep: %s -> %s", folder.path, dest)
            if fly_dir is not None:
                write_source_marker(dest, experiment)
            index.note_copy(dest, experiment if fly_dir is not None else None)
    log.info(
        "%s2P sweep: %d folder(s), %d file(s) copied, %d failed",
        "[dry-run] " if mode.dry_run else "",
        len(todo),
        result.copied,
        result.failed,
    )
    return result


def _on_destination(folder: TwoPhotonFolder, index: NasIndex) -> bool:
    """Whether *folder* is anywhere on the destination already: a same-named
    folder there is the same acquisition (its identity: ThorImage's start time,
    ThorSync's episode size).
    """
    same_name = index.twophoton_dirs_named(folder.path.name)
    if not same_name:
        return False
    want = twophoton_identity(folder.path)
    return any(twophoton_identity(p) == want for p in same_name)


def _pair_pending(
    source_root: Path,
    unclaimed: list[TwoPhotonFolder],
    settings: TwoPhotonSettings,
    mode: CopyMode,
    result: SweepResult,
    index: NasIndex,
) -> set[Path]:
    """Pair each pending take with the unclaimed folders that match it now,
    copying them into its destination. Returns the folders taken out of the
    sweep: paired, or matched but still being written (the take waits).
    """
    pending = pending_takes(source_root) if unclaimed else []
    if not pending:
        return set()
    by_folder = {p.folder: p for p in pending}
    batch = match_takes_to_twophoton_batch(
        [p.take for p in pending],
        unclaimed,
        settings.match_window_s,
        settings.verify_window_s,
        settings.verify_with_signals,
    )
    taken: set[Path] = set()
    for take_folder, matches in batch.items():
        if not matches:
            continue
        taken.update(m.folder.path.resolve() for m in matches)
        writing = [m for m in matches if not is_settled(m.folder, settings.settle_s)]
        if writing:
            log.info(
                "2P sweep: %s still being written \N{EM DASH} pending take %s waits",
                ", ".join(m.folder.path.name for m in writing),
                take_folder,
            )
            continue
        item = by_folder[take_folder]
        dest = current_take_dest(
            Path(item.record["dest"]), start_s(summary_of(item.folder))
        )
        take_mode = CopyMode(
            verify=item.record.get("verify", mode.verify),
            by_content=item.record.get("by_content", mode.by_content),
            dry_run=mode.dry_run,
            assemble=mode.assemble,
        )
        paired = finish_pairing(take_folder, dest, matches, source_root, take_mode)
        result.failed += paired.failed
        result.copied += len(paired.copied_ok)
        log.info(
            "2P sweep: paired %s with the earlier take %s",
            ", ".join(str(m.folder.path) for m in matches),
            take_folder,
        )
        index.refresh_take(dest)
        if paired.complete:
            result.renders.append((dest, start_s(summary_of(item.folder))))
            # The take's own choice, made when it was transferred.
            if item.record.get("by_content") and not mode.dry_run:
                remove_sources(paired.copied_ok)
    return taken
