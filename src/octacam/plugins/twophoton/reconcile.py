"""One chronological sequence per fly: its behavior takes and 2P-only sessions
renamed `RecordingN_Beh`, `RecordingN_2P[_<detail>]` (a 2P-only session,
`<wrapper>/2P/<name>`, with its own ThorSync) or `RecordingN_Synced[_<detail>]`
(a take paired with 2P data), `<detail>` being ThorImage's (`Zstack`).

Same-filesystem renames on the destination, recorded in the fly's append-only
`reconciliation_log.md` (a revert's record). A re-run renumbers a fly whose
sessions changed and leaves the others untouched.
"""

from __future__ import annotations

import logging
import re
import time
from collections.abc import Callable
from pathlib import Path

from octacam.plugins.twophoton.copying import write_source_marker
from octacam.plugins.twophoton.layout import (
    RECONCILIATION_LOG_FILENAME,
    TWOPHOTON_DIRNAME,
    TWOPHOTON_ONLY_DIRNAME,
)
from octacam.plugins.twophoton.matching import (
    _thorimage_detail,
    _thorimage_group_key,
    classify_twophoton_folder,
    correlate_sync_folder_to_image,
)
from octacam.plugins.twophoton.nas import (
    GENERIC_DIRNAME,
    RECONCILED_2P_DIR_RE,
    NasIndex,
)
from octacam.plugins.twophoton.sweep import FlyNames, attribute
from octacam.plugins.twophoton.takes import match_record, start_s, summary_of
from octacam.recording_format import is_recording_dir

log = logging.getLogger("octacam")

# (start, "Beh" | "Synced" | "2P", the take folder or 2P folder)
Session = tuple[float, str, Path]


def session_of(folder: Path) -> Session | None:
    """*folder* as a session: a take (`Synced` when paired with 2P data, else
    `Beh`) or a 2P-only acquisition; None when it is neither.
    """
    if is_recording_dir(folder):
        start = start_s(summary_of(folder))
        if start is None:
            return None
        return (
            start,
            "Synced" if match_record(folder).get("matched") else "Beh",
            folder,
        )
    classified = classify_twophoton_folder(folder)
    return (classified.start_time, "2P", folder) if classified is not None else None


def _is_2p_only_leaf(leaf: Path) -> bool:
    parent = leaf.parent
    return parent.name == TWOPHOTON_ONLY_DIRNAME or (
        parent.name == TWOPHOTON_DIRNAME
        and bool(RECONCILED_2P_DIR_RE.match(parent.parent.name))
    )


def fly_sessions(
    index: NasIndex, fly_dirs: set[Path] | None = None
) -> dict[Path, list[Session]]:
    """Every session of each fly in *index* (*fly_dirs* only, when given): its
    takes, and its 2P-only folders before (`2P_only/<name>`) and after
    (`RecordingN_2P/2P/<name>`) a renumber.
    """
    sessions: dict[Path, list[Session]] = {}
    for take in index.takes:
        fly_dir = take.fly_dir(index.root)
        if fly_dir is None or (fly_dirs is not None and fly_dir not in fly_dirs):
            continue
        session = session_of(take.path)
        if session is not None:
            sessions.setdefault(fly_dir, []).append(session)
    for leaf in index.twophoton_dirs:
        parts = leaf.relative_to(index.root).parts
        if len(parts) < 4 or not _is_2p_only_leaf(leaf):
            continue
        fly_dir = index.root / parts[0] / parts[1]
        if fly_dirs is not None and fly_dir not in fly_dirs:
            continue
        session = session_of(leaf)
        if session is not None:
            sessions.setdefault(fly_dir, []).append(session)
    return sessions


def _take_detail(take: Path) -> str:
    """The detail word of a take's paired ThorImage folder (`Zstack`), if any."""
    for entry in match_record(take).get("matched", []):
        if entry.get("kind") == "image" and entry.get("path"):
            detail = _thorimage_detail(Path(entry["path"]).name)
            if detail:
                return detail
    return ""


def _pair_sync_folders(
    entries: list[Session],
) -> tuple[list[Session], dict[Path, list[Path]]]:
    """Fold each 2P-only ThorSync folder into the acquisition it recorded (a
    Z-stack's: one CaptureOn segment per slice; a stream's: its FrameOut count):
    ThorSync is per session, so both belong in one RecordingN. Returns the
    entries without them and {ThorImage folder: [its ThorSync folders]}.
    """
    folders = {
        path: classify_twophoton_folder(path)
        for _s, kind, path in entries
        if kind == "2P"
    }
    images = [f for f in folders.values() if f is not None and f.kind == "image"]
    companions: dict[Path, list[Path]] = {}
    if not images:
        return entries, companions
    paired: set[Path] = set()
    for path, folder in folders.items():
        if folder is None or folder.kind != "sync":
            continue
        image = correlate_sync_folder_to_image(folder, images)
        if image is not None:
            companions.setdefault(image.path, []).append(path)
            paired.add(path)
    return [e for e in entries if e[2] not in paired], companions


def prune_empty_dirs(folder: Path, stop_at: Path) -> None:
    """Remove *folder* and its emptied ancestors, up to (not) *stop_at*."""
    try:
        stop_at = stop_at.resolve()
        current = folder.resolve()
    except OSError:
        return
    while current != stop_at and stop_at in current.parents:
        try:
            current.rmdir()
        except OSError:
            break
        current = current.parent


def _append_log(fly_dir: Path, root: Path, moves: list[tuple[Path, Path]]) -> None:
    """Append this run's renames of *fly_dir* to its reconciliation log."""
    if not moves:
        return
    path = fly_dir / RECONCILIATION_LOG_FILENAME
    lines: list[str] = []
    if not path.is_file():
        lines += [
            "# Reconciliation log",
            "",
            (
                "Append-only record of every rename octacam has made for this "
                "fly \N{EM DASH} never rewritten, so a folder's original name and "
                "place stay here after a later renumber. Useful to revert a move "
                "by hand."
            ),
            "",
        ]
    lines += [
        f"## {time.strftime('%Y-%m-%d %H:%M:%S')}",
        "",
        "| Recorded as | Original path |",
        "|---|---|",
    ]
    for source, target in moves:
        try:
            original = source.relative_to(root)
        except ValueError:
            original = source
        lines.append(f"| {target.relative_to(root)} | {original} |")
    lines.append("")
    try:
        with path.open("a") as f:
            f.write("\n".join(lines) + "\n")
    except OSError as e:
        log.warning("reconcile: could not write %s: %s", path, e)


def _rename(source: Path, target: Path, what: str) -> bool:
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        source.rename(target)
    except OSError as e:
        log.error("reconcile: failed to move %s -> %s: %s", source, target, e)
        return False
    log.info("reconcile: %s -> %s%s", source, target, what)
    return True


def reconcile(
    sessions: dict[Path, list[Session]],
    root: Path,
    dry_run: bool,
    on_move: Callable[[Path, Path], object] | None = None,
) -> tuple[int, int, int]:
    """Renumber each fly's *sessions* chronologically. Returns (moved,
    unchanged, conflicts); a target held by a session that is not itself moving
    is a conflict, logged, never overwritten. *on_move(source, target)* hears
    each move made.
    """
    n_moved = n_unchanged = n_conflict = 0
    for fly_dir, entries in sorted(sessions.items()):
        if not entries:
            continue
        entries, companions = _pair_sync_folders(entries)
        entries.sort(key=lambda e: e[0])
        planned: list[tuple[Path, Path]] = []
        companion_moves: list[tuple[Path, Path]] = []
        for i, (_start, kind, current) in enumerate(entries, 1):
            detail = (
                _thorimage_detail(current.name)
                if kind == "2P"
                else _take_detail(current)
                if kind == "Synced"
                else ""
            )
            name = f"Recording{i}_{kind}" + (f"_{detail}" if detail else "")
            wrapped = (
                kind == "2P"
                and current.parent.name == TWOPHOTON_DIRNAME
                and bool(RECONCILED_2P_DIR_RE.match(current.parent.parent.name))
            )
            if wrapped:
                # Move the whole wrapper, so its other content (a Z-stack's
                # renders) is not stranded under the old name.
                source, target = current.parent.parent, fly_dir / name
            elif kind == "2P":
                source, target = (
                    current,
                    fly_dir / name / TWOPHOTON_DIRNAME / current.name,
                )
            else:
                source, target = current, fly_dir / name
            for sync_leaf in companions.get(current, []):
                if wrapped and sync_leaf.parent.parent == current.parent.parent:
                    continue  # already in the wrapper, which moves with it
                sync_target = fly_dir / name / TWOPHOTON_DIRNAME / sync_leaf.name
                if sync_leaf != sync_target:
                    companion_moves.append((sync_leaf, sync_target))
            if source == target:
                n_unchanged += 1
            else:
                planned.append((source, target))
        # A renumber shifts names onto ones other sessions of this fly still
        # hold (Recording5 -> 7 while 7 -> 9 has not moved): only a name held
        # by a session that is not moving is a real conflict.
        moving_away = {src for src, _ in planned if src.parent == fly_dir}
        # A ThorSync folder alone in its wrapper leaves it empty.
        moving_away |= {
            leaf.parent.parent
            for leaf, _ in companion_moves
            if leaf.parent.name == TWOPHOTON_DIRNAME
            and RECONCILED_2P_DIR_RE.match(leaf.parent.parent.name)
            and [c.name for c in leaf.parent.parent.iterdir()] == [TWOPHOTON_DIRNAME]
            and [c.name for c in leaf.parent.iterdir()] == [leaf.name]
        }
        moves: list[tuple[Path, Path]] = []
        for source, target in planned:
            top = fly_dir / target.relative_to(fly_dir).parts[0]
            if top.exists() and top not in moving_away:
                n_conflict += 1
                log.error(
                    "reconcile: cannot move %s -> %s: %s already exists",
                    source,
                    target,
                    top,
                )
            else:
                moves.append((source, target))
        if dry_run:
            for source, target in [*moves, *companion_moves]:
                log.info("[dry-run] reconcile: %s -> %s", source, target)
            continue
        done: list[tuple[Path, Path]] = []
        # Park every mover under a temporary name first (ThorSync folders too,
        # freeing a wrapper of their own), then place each: no target is
        # still held when it is placed.
        parked: list[tuple[Path, Path, Path, str]] = []
        for k, (source, target) in enumerate(companion_moves):
            tmp = fly_dir / f".reconcile-sync-{k}-{source.name}"
            if _rename(source, tmp, ""):
                prune_empty_dirs(source.parent, root)
                parked.append((source, tmp, target, " (its acquisition's ThorSync)"))
            else:
                n_conflict += 1
        placing: list[tuple[Path, Path, Path, str]] = []
        for k, (source, target) in enumerate(moves):
            if source not in moving_away:  # a 2P_only/ leaf blocks nobody
                placing.append((source, source, target, ""))
                continue
            tmp = fly_dir / f".reconcile-tmp-{k}-{source.name}"
            try:
                source.rename(tmp)
            except OSError as e:
                n_conflict += 1
                log.error("reconcile: failed to move %s -> %s: %s", source, target, e)
                continue
            placing.append((source, tmp, target, ""))
        for source, tmp, target, what in [*placing, *parked]:
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
                tmp.rename(target)
            except OSError as e:
                n_conflict += 1
                log.error(
                    "reconcile: failed to move %s -> %s (left at %s): %s",
                    source,
                    target,
                    tmp,
                    e,
                )
                continue
            n_moved += 1
            log.info("reconcile: %s -> %s%s", source, target, what)
            done.append((source, target))
            if on_move is not None:
                on_move(source, target)
            prune_empty_dirs(source.parent, root)
        _append_log(fly_dir, root, done)
    return n_moved, n_unchanged, n_conflict


def reconcile_flies(index: NasIndex, fly_dirs: set[Path], dry_run: bool) -> None:
    """Renumber *fly_dirs* (the flies a run touched, or named by hand)."""
    moved, unchanged, conflicts = reconcile(
        fly_sessions(index, fly_dirs), index.root, dry_run, on_move=index.relocate
    )
    if moved or conflicts:
        log.info(
            "%sreconcile: %d moved, %d already right, %d needing attention",
            "[dry-run] " if dry_run else "",
            moved,
            unchanged,
            conflicts,
        )


def promote_generic(
    index: NasIndex, names: FlyNames, dry_run: bool
) -> dict[Path, list[Session]]:
    """The generic bucket's folders as fly sessions: each folder a known fly's
    names claim joins it; the others, grouped by ThorImage's fly name, become a
    new `FlyN` under their experiment's day folder (made when none matches).
    No take anchors this: run by hand (`octacam twophoton reconcile --all`),
    previewed with --dry-run; nothing is destroyed if a group was a test.
    """
    root = index.root
    generic = root / GENERIC_DIRNAME
    if not generic.is_dir():
        return {}
    groups: dict[tuple[str, str], list[Path]] = {}
    sessions: dict[Path, list[Session]] = {}
    for date_dir in sorted(p for p in generic.glob("*/*") if p.is_dir()):
        experiment = date_dir.parent.name
        found = [
            f
            for entry in sorted(date_dir.iterdir())
            if entry.is_dir() and (f := classify_twophoton_folder(entry))
        ]
        images = [f for f in found if f.kind == "image"]
        for candidate in found:
            existing = attribute(candidate, experiment, names, images)
            if existing is not None:
                session = session_of(candidate.path)
                if session is not None:
                    sessions.setdefault(existing, []).append(session)
                continue
            if candidate.kind == "image":
                key = _thorimage_group_key(candidate.path.name)
            else:
                image = correlate_sync_folder_to_image(candidate, images)
                if image is None:
                    continue
                key = _thorimage_group_key(image.path.name)
            groups.setdefault((experiment, key), []).append(candidate.path)

    counters: dict[Path, int] = {}
    for (experiment, _key), paths in sorted(groups.items()):
        # The share's experiment name and the day folder's can differ in digits
        # (PAM7xCI80 vs 260903_PAM07xCI80): compare them without.
        letters = re.sub(r"\d", "", experiment)
        day_dir = next(
            (
                d
                for d in sorted(root.iterdir())
                if d.is_dir() and letters in re.sub(r"\d", "", d.name)
            ),
            None,
        )
        if day_dir is None:
            first = session_of(paths[0])
            date = (
                time.strftime("%y%m%d", time.localtime(first[0]))
                if first
                else "unknown"
            )
            day_dir = root / f"{date}_{experiment}"
        if day_dir not in counters:
            counters[day_dir] = max(
                (
                    int(m.group(1))
                    for d in (day_dir.glob("Fly*") if day_dir.is_dir() else [])
                    if (m := re.match(r"^Fly(\d+)$", d.name))
                ),
                default=0,
            )
        counters[day_dir] += 1
        fly_dir = day_dir / f"Fly{counters[day_dir]}"
        for path in paths:
            session = session_of(path)
            if session is not None:
                sessions.setdefault(fly_dir, []).append(session)
                # A fly with no take: later data of it is attributed by this.
                if not dry_run:
                    write_source_marker(path, experiment)
    return sessions


def fly_dirs_under(root: Path, paths: list[Path]) -> set[Path]:
    """The fly folders *paths* name under *root*: a day folder's `Fly*`, or the
    `<day>/<fly>` a deeper path lies in. Paths outside *root* are ignored.
    """
    found: set[Path] = set()
    root = root.resolve()
    for raw in paths:
        p = raw.resolve()
        try:
            rel = p.relative_to(root)
        except ValueError:
            continue
        if len(rel.parts) == 1:
            found.update(d for d in p.glob("Fly*") if d.is_dir())
        elif rel.parts:
            found.add(root / rel.parts[0] / rel.parts[1])
    return found
