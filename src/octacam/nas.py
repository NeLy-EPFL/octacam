"""Copy transcoded recordings to a NAS or any writable destination path.

Typical call after ``octacam transcode --grid --nas-path /mnt/nas/matthias``:

    copy_folder_to_nas(
        folder=Path("/data/octacam/260620-genotype/Fly1/001-bhv"),
        nas_root=Path("/mnt/nas/matthias"),
        local_base=Path("/data/octacam"),  # optional: mirrors full path structure
        files_only=[Path(".../camera_LF.mp4"), ...],  # only the transcoded mp4s
    )

Path mirroring:  when *local_base* is given and *folder* lies under it, the
path relative to *local_base* is reproduced under *nas_root*, so the NAS gets
the same ``260620-genotype/Fly1/001-bhv`` hierarchy.  If *local_base* is
omitted (or *folder* is not under it), only the last component of *folder* is
used, which is safe for flat layouts but may cause collisions for deep ones.
"""

from __future__ import annotations

import logging
import shutil
from pathlib import Path

from octacam.transform import RECORDING_SUMMARY_FILENAME

log = logging.getLogger("octacam")


def copy_folder_to_nas(
    folder: Path,
    nas_root: Path,
    local_base: Path | None = None,
    files_only: list[Path] | None = None,
    dry_run: bool = False,
) -> Path | None:
    """Copy mp4s (and recording_summary.json) from *folder* to *nas_root*.

    Parameters
    ----------
    folder:
        Source recording directory.
    nas_root:
        Root of the NAS destination (e.g. ``/mnt/nas/matthias``).
    local_base:
        Local root to strip when computing the NAS sub-path.  If *folder* is
        ``/data/octacam/exp/Fly1`` and *local_base* is ``/data/octacam``, the
        NAS destination becomes ``nas_root/exp/Fly1``.
    files_only:
        Explicit list of files to copy; overrides the default (all *.mp4 in
        *folder*).  ``recording_summary.json`` is always appended if present.
    dry_run:
        Log intended operations without touching the filesystem.

    Returns the NAS destination directory on success, None on failure.
    """
    # --- Compute destination path -------------------------------------------
    try:
        rel = folder.relative_to(local_base) if local_base else None
    except ValueError:
        rel = None
    dest = nas_root / rel if rel is not None else nas_root / folder.name

    # --- Decide which files to copy -----------------------------------------
    if files_only is not None:
        candidates = list(files_only)
    else:
        candidates = sorted(folder.glob("*.mp4"))

    summary = folder / RECORDING_SUMMARY_FILENAME
    if summary.exists() and summary not in candidates:
        candidates.append(summary)

    if not candidates:
        log.warning("Nothing to copy to NAS from %s", folder)
        return None

    # --- Dry run: just log --------------------------------------------------
    if dry_run:
        for f in candidates:
            log.info("[dry-run] NAS copy: %s → %s", f, dest / f.name)
        return dest

    # --- Real copy ----------------------------------------------------------
    try:
        dest.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        log.error("Could not create NAS directory %s: %s", dest, e)
        return None

    any_ok = False
    for f in candidates:
        target = dest / f.name
        try:
            shutil.copy2(str(f), str(target))
            log.info("NAS: %s → %s", f.name, dest)
            any_ok = True
        except OSError as e:
            log.error("Failed to copy %s to NAS: %s", f, e)

    return dest if any_ok else None
