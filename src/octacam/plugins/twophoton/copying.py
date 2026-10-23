"""Copying 2P folders to the destination: a ThorImage folder's per-frame TIFFs
become one verified stack per channel (and FastZ plane), everything else is
copied as is (`transfer.transfer_tree`). Also what a paired take records about
its 2P data, and the share-side cleanup after a verified copy.
"""

from __future__ import annotations

import logging
import shutil
from dataclasses import dataclass
from pathlib import Path

from octacam.plugins.twophoton.layout import (
    MATCH_FILENAME,
    PENDING_FILENAME,
    TWOPHOTON_2P_ONLY_SOURCE_FILENAME,
    TWOPHOTON_DIRNAME,
)
from octacam.plugins.twophoton.matching import (
    EXPERIMENT_XML_FILENAME,
    TakeInfo,
    TwoPhotonFolder,
    TwoPhotonMatch,
    build_match_record,
)
from octacam.plugins.twophoton.settings import TwoPhotonSettings
from octacam.plugins.twophoton.takes import sidecar, write_json
from octacam.transfer import TransferResult, transfer_folder, transfer_tree

log = logging.getLogger("octacam")


@dataclass(frozen=True)
class CopyMode:
    """How a run copies: *verify* each copy's content (`[transfer].checksum`, or
    a delete depends on it), compare files already there *by_content* (a delete
    depends on it), or only list (*dry_run*).
    """

    verify: bool
    by_content: bool
    dry_run: bool
    assemble: bool = True


def log_assembly_preview(folder: Path) -> None:
    """A dry run's one line per channel (never per frame)."""
    from octacam.plugins.twophoton.tiff import plan_assembly

    plan = plan_assembly(folder)
    for ch in plan.ready:
        log.info(
            "[dry-run] 2P stacks: %s %s: would assemble %d frame(s) -> %s",
            folder,
            ch.channel,
            len(ch.source_files),
            ch.dest_name,
        )
    for ch in plan.problems:
        log.warning(
            "[dry-run] 2P stacks: %s %s: %s \N{EM DASH} would copy %d frame file(s)",
            folder,
            ch.channel,
            ch.reason,
            len(ch.source_files),
        )


def verify_stack(stack: Path, source_files: list[Path]) -> str | None:
    """Pixel-compare an assembled *stack* with the frames it was built from:
    page i must equal source_files[i]. The problem, or None when identical.
    """
    import numpy as np
    import tifffile

    from octacam.plugins.twophoton.tiff import _read_single_page

    try:
        with tifffile.TiffFile(stack) as tf:
            pages = tf.pages
            if len(pages) != len(source_files):
                return (
                    f"stack {stack.name}: {len(pages)} page(s) for "
                    f"{len(source_files)} source frame(s)"
                )
            bad = 0
            for i, src in enumerate(source_files):
                got = tf.asarray(key=i)  # page i alone, never imread()
                want = _read_single_page(src)
                if (
                    got.shape != want.shape
                    or got.dtype != want.dtype
                    or not np.array_equal(got, want)
                ):
                    bad += 1
    except Exception as e:  # unreadable is not verified
        return f"stack {stack.name}: unreadable ({e})"
    if bad:
        return f"stack {stack.name}: {bad} of {len(source_files)} frame(s) differ"
    return None


def _stack_is_complete(stack: Path, source_files: list[Path], *, recheck: bool) -> bool:
    """Whether an already-assembled *stack* holds every frame: its page count,
    and with *recheck* (a delete depends on it) every page's pixels.
    """
    if not stack.is_file():
        return False
    if recheck:
        problem = verify_stack(stack, source_files)
        if problem:
            log.warning("2P stacks: rebuilding %s \N{EM DASH} %s", stack, problem)
        return problem is None
    try:
        import tifffile

        with tifffile.TiffFile(stack) as tf:
            return len(tf.pages) == len(source_files)
    except Exception:  # unreadable: rebuild
        return False


def copy_twophoton_folder(
    folder: TwoPhotonFolder, dest: Path, mode: CopyMode
) -> TransferResult:
    """Copy *folder* to *dest*. A ThorImage folder's channels that can be
    stacked are (every page pixel-checked with `mode.verify`); a channel that
    cannot, or whose stacking fails, is copied frame by frame: every source
    frame lands one way or the other. A complete stack from an earlier run is
    kept (re-checked when a delete depends on it).
    """
    from octacam.plugins.twophoton.tiff import (
        assemble_channel_stack,
        make_progress_logger,
        plan_assembly,
        tifffile_available,
    )

    src = folder.path
    if mode.dry_run:
        log.info("[dry-run] 2P transfer: %s -> %s", src, dest)
        if folder.kind == "image" and mode.assemble:
            log_assembly_preview(src)
        return TransferResult(dest=dest)
    source_files = _listing(src)
    if _already_verified(dest, source_files, mode):
        return TransferResult(dest=dest, skipped=sorted(source_files))
    ready = (
        plan_assembly(src).ready
        if folder.kind == "image" and mode.assemble and tifffile_available()
        else []
    )
    exclude = frozenset(f for ch in ready for f in ch.source_files)
    result = transfer_tree(
        src, dest, verify=mode.verify, by_content=mode.by_content, exclude=exclude
    )
    experiment_xml = src / EXPERIMENT_XML_FILENAME
    for ch in ready:
        stack = dest / ch.dest_name
        if _stack_is_complete(stack, ch.source_files, recheck=mode.by_content):
            result.skipped.append(stack.name)
            continue
        assembled = assemble_channel_stack(
            ch.source_files,
            stack,
            axis=ch.axis or "T",
            experiment_xml=experiment_xml if experiment_xml.is_file() else None,
            full_verify=mode.verify,
            on_progress=make_progress_logger(src, ch.channel),
            declared_count=ch.declared_count,
        )
        if assembled.ok:
            result.copied.append(stack.name)
            log.info(
                "2P stacks: %s %s: %d frame(s) -> %s%s%s",
                src,
                ch.channel,
                assembled.frame_count,
                stack,
                " (every frame verified)" if mode.verify else "",
                f" \N{EM DASH} stopped early, {ch.declared_count} declared"
                if ch.declared_count is not None
                else "",
            )
        else:
            log.error(
                "2P stacks: %s %s: %s \N{EM DASH} copying its frames instead",
                src,
                ch.channel,
                assembled.error,
            )
            fallback = transfer_folder(
                src,
                dest,
                ch.source_files,
                verify=mode.verify,
                metadata=False,
                by_content=mode.by_content,
            )
            result.copied.extend(fallback.copied)
            result.failed.extend(fallback.failed)
    if result.failed:
        log.error("2P transfer: %d file(s) of %s failed", len(result.failed), src)
    elif mode.verify:
        _mark_verified(dest, source_files, mode)
    return result


# Left in a 2P folder's copy once all of it was copied and verified: a re-run
# finding the same source files (names, sizes, mtimes) and the same copy
# (names, sizes) trusts that check instead of re-hashing gigabytes over the
# share and the NAS. `octacam twophoton verify` never uses it: it always
# compares afresh.
VERIFIED_MARKER = ".octacam_verified.json"


def _listing(folder: Path) -> dict[str, list[int]]:
    """{relative path: [size, mtime_ns]} of every file under *folder*."""
    listing: dict[str, list[int]] = {}
    for path in folder.rglob("*"):
        if path.name.startswith(".octacam_"):  # octacam's own notes, not data
            continue
        try:
            st = path.stat()
        except OSError:
            continue
        if path.is_file():
            listing[path.relative_to(folder).as_posix()] = [st.st_size, st.st_mtime_ns]
    return listing


def _copy_sizes(dest: Path) -> dict[str, int]:
    return {rel: size for rel, (size, _mtime) in _listing(dest).items()}


def _already_verified(dest: Path, source: dict[str, list[int]], mode: CopyMode) -> bool:
    from octacam.plugins.twophoton.takes import read_json

    marker = read_json(dest / VERIFIED_MARKER)
    if not marker or marker.get("source") != source:
        return False
    if mode.by_content and not marker.get("by_content"):
        return False  # a delete needs the stronger check it never had
    return marker.get("copy") == _copy_sizes(dest)


def _mark_verified(dest: Path, source: dict[str, list[int]], mode: CopyMode) -> None:
    write_json(
        dest / VERIFIED_MARKER,
        {
            "source": source,
            "copy": _copy_sizes(dest),
            # Every file already there was compared by content too.
            "by_content": mode.by_content,
        },
    )


def copy_sidecar(take: Path, path: Path, dest: Path, mode: CopyMode) -> int:
    """Copy *take*'s sidecar *path* to the same place under *dest*; failures."""
    target_dir = dest / path.parent.relative_to(take)
    result = transfer_folder(
        path.parent, target_dir, [path], verify=mode.verify, metadata=False
    )
    return len(result.failed)


@dataclass
class Paired:
    """What finishing a take's pairing did: the share folders it copied
    cleanly (deletable), how many files failed, and whether all of it landed.
    """

    copied_ok: list[Path]
    failed: int

    @property
    def complete(self) -> bool:
        return not self.failed


def finish_pairing(
    take: Path,
    dest: Path,
    matches: list[TwoPhotonMatch],
    source_root: Path,
    mode: CopyMode,
) -> Paired:
    """Copy each settled match to `<dest>/2P/<name>` (a ThorImage folder's
    Experiment.xml also beside the take), then record the pairing in the take's
    `twophoton_match.json`, here and at *dest*. A ThorSync episode cut short by
    its duration limit is warned about and noted in the record. A copy that
    failed marks the record incomplete and leaves the take pending, so a later
    run finishes it.
    """
    from octacam.plugins.twophoton.signals import describe_cut_off, episode_cut_off

    copied_ok: list[Path] = []
    failed = 0
    for m in matches:
        dest_2p = dest / TWOPHOTON_DIRNAME / m.folder.path.name
        result = copy_twophoton_folder(m.folder, dest_2p, mode)
        failed += len(result.failed)
        if not mode.dry_run and not result.failed:
            copied_ok.append(m.folder.path)
            log.info("2P transfer: %s -> %s (%s)", m.folder.path, dest_2p, m.confidence)
        xml = m.folder.path / EXPERIMENT_XML_FILENAME
        if m.folder.kind == "image" and xml.is_file() and not mode.dry_run:
            failed += len(
                transfer_folder(
                    m.folder.path, dest, [xml], verify=mode.verify, metadata=False
                ).failed
            )
    record = build_match_record(matches, source_root)
    for entry, m in zip(record["matched"], matches, strict=True):
        if m.folder.kind != "sync":
            continue
        cut = episode_cut_off(m.folder.path)
        if cut is not None:
            message = describe_cut_off(m.folder.path.name, cut)
            log.warning("2P transfer: %s (take %s)", message, take)
            entry["thorsync_cut_off"] = {
                "recorded_s": cut.recorded_s,
                "limit_s": cut.limit_s,
                "message": message,
            }
    if failed:
        # Its share folders stay claimed by name (`NasIndex.matched_sources`)
        # and held for the retry (`sweep`), never filed as unpaired data.
        record["complete"] = False
    if not mode.dry_run:
        path = sidecar(take, MATCH_FILENAME)
        if write_json(path, record):
            failed += copy_sidecar(take, path, dest, mode)
        if not failed:
            clear_pending(take)
    return Paired(copied_ok, failed)


def write_pending(
    take: Path, info: TakeInfo, dest: Path, source_root: Path, mode: CopyMode
) -> None:
    """Note an armed take transferred before its 2P data settled (or appeared),
    so a later run's sweep pairs it once it has: nothing else revisits a
    finished take.
    """
    write_json(
        sidecar(take, PENDING_FILENAME),
        {
            "start_time": info.start_time,
            "duration_s": info.duration_s,
            "camera_frame_counts": info.camera_frame_counts,
            "dest": str(dest),
            "source_root": str(source_root.resolve()),
            "verify": mode.verify,
            "by_content": mode.by_content,
        },
    )


def clear_pending(take: Path) -> None:
    try:
        sidecar(take, PENDING_FILENAME).unlink()
    except FileNotFoundError:
        pass
    except OSError as e:
        log.warning("2P transfer: could not clear the pending note of %s: %s", take, e)


def write_source_marker(dest: Path, experiment: str) -> None:
    """Note which share experiment a fly's 2P-only folder came from: a fly
    with no behavior take has no other record of it, and later data of the same
    fly is attributed by it.
    """
    write_json(dest / TWOPHOTON_2P_ONLY_SOURCE_FILENAME, {"experiment": experiment})


def remove_sources(paths: list[Path], root: Path | None) -> None:
    """Delete verified share folders, only ones inside *root* (the user's 2P
    folder on the share; None deletes nothing): another person's data is never
    removed. A refused delete (the share's account has no delete right: a
    `noperm` CIFS mount) is reported once for all.
    """
    denied: list[Path] = []
    for path in paths:
        if not _inside(path, root):
            log.warning(
                "Not removing 2P source %s: it is outside the user's share folder %s",
                path,
                root,
            )
            continue
        try:
            shutil.rmtree(path)
            log.info("Removed 2P source %s (verified at the destination)", path)
        except PermissionError:
            denied.append(path)
        except OSError as e:
            log.error("Could not remove 2P source %s: %s", path, e)
    if denied:
        experiments = sorted({str(p.parent) for p in denied})
        log.warning(
            "No permission to delete %d verified 2P source folder(s) on the share "
            "(%s) \N{EM DASH} they are safe at the destination. Give the share's "
            "account delete rights on the 2P PC, then `octacam twophoton verify "
            "--delete %s` removes them (or delete them by hand there)",
            len(denied),
            ", ".join(p.name for p in denied),
            " ".join(experiments),
        )


def _inside(path: Path, root: Path | None) -> bool:
    """Whether *path* is strictly inside *root* (never *root* itself)."""
    if root is None:
        return False
    try:
        a, b = path.resolve(), root.resolve()
    except OSError:
        return False
    return a != b and a.is_relative_to(b)


def settled(matches: list[TwoPhotonMatch], settings: TwoPhotonSettings) -> bool:
    from octacam.plugins.twophoton.matching import is_settled

    return all(is_settled(m.folder, settings.settle_s) for m in matches)
