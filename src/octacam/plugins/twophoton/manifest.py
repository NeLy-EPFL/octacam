"""Each day's `2p_reconciliation.md` on the destination: every take with what
it was paired with, and the share's 2P folders of that day nobody claims (with
where the sweep files them). Rebuilt from scratch by every run, from what is
on the destination and the share, so it never drifts.
"""

from __future__ import annotations

import logging
from pathlib import Path

from octacam.plugins.twophoton.layout import MANIFEST_FILENAME
from octacam.plugins.twophoton.matching import (
    TwoPhotonFolder,
    render_twophoton_manifest,
)
from octacam.plugins.twophoton.nas import NasIndex
from octacam.plugins.twophoton.sweep import FlyNames, attribute
from octacam.plugins.twophoton.takes import start_s

log = logging.getLogger("octacam")

# A share folder this far outside a day's takes is another experiment's (two
# experiments a day share the share): beyond ThorSync/ThorImage's start and
# write-out lag, well short of hours between two sessions.
UNCLAIMED_WINDOW_MARGIN_S = 1800.0


def _takes_by_day(index: NasIndex) -> dict[Path, list[dict]]:
    by_day: dict[Path, list[dict]] = {}
    for take in index.takes:
        rel = take.path.relative_to(index.root)
        start = start_s(take.summary)
        duration_s = take.summary.get("duration_s")
        if not rel.parts or start is None or duration_s is None:
            continue
        by_day.setdefault(index.root / rel.parts[0], []).append(
            {
                "name": str(rel),
                "start_time": start,
                "duration_s": duration_s,
                "armed": bool(take.info.get("armed", False)),
                "matches": take.match.get("matched", []),
            }
        )
    return by_day


def rebuild_manifests(
    index: NasIndex,
    source_root: Path,
    folders: list[TwoPhotonFolder] | None,
    names: FlyNames,
    *,
    dry_run: bool,
    days: set[Path] | None = None,
) -> int:
    """Rewrite the manifest of every day on the destination with a take (of
    *days* only, when given: the ones a run touched). *folders* is the run's
    scan of *source_root* (None: the share was not reachable, which the
    manifests say). Returns how many were written.
    """
    from octacam.plugins.twophoton.signals import describe_cut_off, episode_cut_off

    by_day = _takes_by_day(index)
    if days is not None:
        by_day = {day: takes for day, takes in by_day.items() if day in days}
    if not by_day:
        return 0
    reachable = folders is not None
    claimed = index.matched_sources(source_root) if reachable else set()
    unclaimed_all = [f for f in folders or [] if f.path.resolve() not in claimed]
    # Attribution is by name, not time: a fly's Z-stack can sit far outside the
    # window and still be filed under it.
    filed: dict[Path, Path] = {}
    for f in unclaimed_all if names else []:
        experiment = f.path.parent.name
        images = [
            c
            for c in folders or []
            if c.kind == "image" and c.path.parent.name == experiment
        ]
        fly_dir = attribute(f, experiment, names, images)
        if fly_dir is not None:
            filed[f.path] = fly_dir

    written = 0
    for day_dir, takes in by_day.items():
        first = min(t["start_time"] for t in takes) - UNCLAIMED_WINDOW_MARGIN_S
        last = (
            max(t["start_time"] + t["duration_s"] for t in takes)
            + UNCLAIMED_WINDOW_MARGIN_S
        )
        unclaimed = [
            f
            for f in unclaimed_all
            if first <= f.start_time <= last
            or (f.path in filed and filed[f.path].parent == day_dir)
        ]
        attributed = {f.path: filed[f.path].name for f in unclaimed if f.path in filed}
        cut_offs = {
            f.path.name: describe_cut_off(f.path.name, cut)
            for f in unclaimed
            if f.kind == "sync" and (cut := episode_cut_off(f.path)) is not None
        }
        text = render_twophoton_manifest(
            day_dir.name, takes, unclaimed, reachable, attributed, cut_offs
        )
        path = day_dir / MANIFEST_FILENAME
        if dry_run:
            log.info("[dry-run] 2P manifest: would write %s", path)
        else:
            try:
                path.write_text(text)
            except OSError as e:
                log.warning("2P manifest: could not write %s: %s", path, e)
                continue
        written += 1
    return written
