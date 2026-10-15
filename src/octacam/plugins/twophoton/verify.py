"""Which share folders are verifiably on the destination, so safe to delete by
hand (octacam cannot on a share whose account has no delete right). Read-only
on both sides: every file is re-read and compared, as a pairing record only
says a copy was made, not that it was checked.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from octacam.plugins.twophoton.copying import verify_stack
from octacam.plugins.twophoton.matching import (
    TwoPhotonFolder,
    is_settled,
    twophoton_identity,
)
from octacam.plugins.twophoton.nas import NasIndex
from octacam.transfer import file_digest

log = logging.getLogger("octacam")

# The small files ThorImage/ThorSync write first: a copy of the same
# acquisition has them identical, even when a big data file was damaged.
_METADATA_FILES = ("Experiment.xml", "ThorRealTimeDataSettings.xml")


def _maybe_same_acquisition(source: Path, candidate: Path) -> bool:
    """Whether a same-named *candidate* may be a (damaged) copy of *source*
    rather than another acquisition reusing the name: their metadata files are
    identical. Missing evidence counts as "may be": it is then compared, and
    reported a mismatch, never safe.
    """
    for name in _METADATA_FILES:
        a, b = source / name, candidate / name
        if a.is_file() and b.is_file():
            try:
                if a.read_bytes() != b.read_bytes():
                    return False
            except OSError:
                pass
    return True


def find_copy(index: NasIndex, source: Path) -> Path | None:
    """Where *source* landed on the destination: the same-named folder with its
    acquisition identity, else one that may be a damaged copy of it (so it is
    still compared and flagged), else None.
    """
    same_name = index.twophoton_dirs_named(source.name)
    want = twophoton_identity(source)
    for candidate in same_name:
        if twophoton_identity(candidate) == want:
            return candidate
    return next((c for c in same_name if _maybe_same_acquisition(source, c)), None)


def compare(source: Path, copy: Path) -> list[str]:
    """Every way *copy* differs from *source*; [] when all of it is there. A
    ThorImage channel stacked on the copy is compared page by page with its
    frames.
    """
    from octacam.plugins.twophoton.tiff import plan_assembly

    problems: list[str] = []
    covered: set[Path] = set()
    for channel in plan_assembly(source).ready:
        stack = copy / channel.dest_name
        if not stack.is_file():
            continue  # copied frame by frame: compared below
        covered.update(channel.source_files)
        problem = verify_stack(stack, channel.source_files)
        if problem:
            problems.append(problem)
    for src in sorted(p for p in source.rglob("*") if p.is_file()):
        if src in covered:
            continue
        rel = src.relative_to(source)
        dst = copy / rel
        if not dst.is_file():
            problems.append(f"missing: {rel}")
        elif src.stat().st_size != dst.stat().st_size:
            problems.append(f"size mismatch: {rel}")
        elif file_digest(src) != file_digest(dst):
            problems.append(f"content mismatch: {rel}")
    return problems


@dataclass
class VerifyReport:
    safe: int = 0
    not_transferred: int = 0
    writing: int = 0
    mismatched: int = 0


def verify(
    folders: list[TwoPhotonFolder], index: NasIndex, settle_s: float
) -> VerifyReport:
    """Report, folder by folder, which of *folders* are safe to delete."""
    report = VerifyReport()
    for folder in sorted(folders, key=lambda f: f.path.name):
        if not is_settled(folder, settle_s):
            log.info("2P verify: %s still writing \N{EM DASH} skipped", folder.path)
            report.writing += 1
            continue
        copy = find_copy(index, folder.path)
        if copy is None:
            log.info(
                "2P verify: %s not transferred yet \N{EM DASH} keep it", folder.path
            )
            report.not_transferred += 1
            continue
        problems = compare(folder.path, copy)
        if problems:
            report.mismatched += 1
            log.error(
                "2P verify: %s -> %s MISMATCH \N{EM DASH} do NOT delete (%d "
                "problem(s)): %s",
                folder.path,
                copy,
                len(problems),
                "; ".join(problems[:5]) + ("; ..." if len(problems) > 5 else ""),
            )
        else:
            report.safe += 1
            log.info(
                "2P verify: %s -> %s verified \N{EM DASH} safe to delete",
                folder.path,
                copy,
            )
    log.info(
        "2P verify: %d safe to delete, %d not transferred yet, %d still writing, "
        "%d MISMATCH",
        report.safe,
        report.not_transferred,
        report.writing,
        report.mismatched,
    )
    return report


def within(folders: list[TwoPhotonFolder], paths: list[Path]) -> list[TwoPhotonFolder]:
    """The *folders* at or under one of *paths* (experiment or 2P folders)."""
    roots = [p.resolve() for p in paths]
    return [
        f
        for f in folders
        if any(f.path.resolve() == r or r in f.path.resolve().parents for r in roots)
    ]
