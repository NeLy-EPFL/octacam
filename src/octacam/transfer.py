"""Copy transcoded recordings to any writable destination path.

Typical call from the ``octacam process`` transfer step:

    transfer_folder(
        folder=Path("/data/octacam/260620-genotype/Fly1/001-bhv"),
        dest=Path("/mnt/store/matthias/260620-genotype/Fly1/001-bhv"),
        files_only=[Path(".../camera_LF.mp4"), ...],  # only the transcoded mp4s
    )

Path mirroring:  the caller resolves *dest* — for ``octacam process`` that is
``transfer.directory`` joined with the recording's ``relative_directory`` (the
sub-path resolved at record time and stored in recording_summary.json), so the
destination reproduces the local ``260620-genotype/Fly1/001-bhv`` hierarchy and
distinct trials sharing a name never collide.

The destination is commonly a network share (an SMB/CIFS/NFS mount), but may be
any writable path — a local disk, an external drive, a bind mount, etc. — so
nothing here assumes a particular medium.

Reliability:  each file is streamed to a unique sibling temp in the destination
directory and only ``os.replace``-d onto its final name once whole and (by
default) content-verified, mirroring :func:`octacam.writer._atomic_output`.  So
an interrupted copy never leaves a complete-looking partial at the final name,
and re-running skips files already present (videos by size, the small metadata
files by content) — resume is at file granularity.

Besides the videos, every transfer carries the recording's metadata: the
summary, the timestamps, and the config snapshot with the camera parameter
files beside it, so the destination copy can relaunch the same recording setup.
The copy keeps the source's layout: a recording that keeps its metadata in the
``octacam_recording`` subfolder gets one at the destination, and an older flat
recording stays flat, so every reader finds it there as it did here.
"""

from __future__ import annotations

import dataclasses
import hashlib
import logging
import os
import shutil
import time
import uuid
from collections.abc import Callable
from pathlib import Path

from octacam.transform import (
    CONFIG_SNAPSHOT_FILENAME,
    PARAM_FILE_EXTENSIONS,
    RECORDING_SUMMARY_FILENAME,
    TIMESTAMPS_FILENAME,
    recording_info_dir,
)

log = logging.getLogger("octacam")

_CHUNK_SIZE = 4 * 1024 * 1024  # 4 MB

# Infix tagging an in-progress copy's temp file. Greppable and stable so a
# stale temp a hard kill left behind is recognisable and swept on the next run.
_TEMP_INFIX = ".octacam-part"

# Only orphaned temps older than this are reaped, so a concurrent run's live
# temp (or a slow multi-GB copy still touching its temp) is never deleted.
_STALE_TEMP_AGE_S = 24 * 3600


@dataclasses.dataclass(frozen=True)
class TransferProgress:
    """Progress snapshot for one file-copy chunk."""

    file_index: int  # 1-based index within the current folder
    file_count: int  # total files being copied for this folder
    filename: str  # basename of the file being copied
    bytes_done: int  # bytes written (copy) or read back (verify) so far
    file_size: int  # total file size in bytes
    elapsed_s: float  # elapsed seconds since this phase started
    phase: str = "copy"  # "copy" (writing) or "verify" (reading back to hash)

    @property
    def speed_mbs(self) -> float:
        if self.elapsed_s <= 0 or self.bytes_done <= 0:
            return 0.0
        return self.bytes_done / self.elapsed_s / 1_000_000

    @property
    def done(self) -> bool:
        return self.bytes_done >= self.file_size


TransferCallback = Callable[[TransferProgress], None]


@dataclasses.dataclass
class TransferResult:
    """Outcome of transferring one folder to its destination."""

    dest: Path
    copied: list[str] = dataclasses.field(default_factory=list)
    skipped: list[str] = dataclasses.field(default_factory=list)
    failed: list[str] = dataclasses.field(default_factory=list)


def _temp_path(final: Path) -> Path:
    """A unique sibling temp path in *final*'s own directory.

    Same filesystem as *final* (so the final :func:`os.replace` is atomic) and
    unique per process + call, so two concurrent runs never rename the same
    temp out from under each other (mirrors ``session_cache._write_entries``).
    """
    return final.with_name(
        f".{final.name}{_TEMP_INFIX}.{os.getpid()}.{uuid.uuid4().hex}"
    )


def _file_digest(path: Path, on_chunk: Callable[[int], None] | None = None) -> str:
    """blake2b hex digest of *path*, read in chunks (fast, stdlib, non-crypto).

    *on_chunk* (if given) is called with the cumulative bytes read after each
    chunk, so a long readback can keep a progress bar live.
    """
    h = hashlib.blake2b()
    read = 0
    with open(path, "rb") as f:
        while chunk := f.read(_CHUNK_SIZE):
            h.update(chunk)
            read += len(chunk)
            if on_chunk is not None:
                on_chunk(read)
    return h.hexdigest()


def _stream_copy(
    src: Path,
    tmp: Path,
    size: int,
    file_index: int,
    file_count: int,
    on_progress: TransferCallback | None,
    *,
    hash_src: bool,
) -> str | None:
    """Stream *src* → *tmp* in chunks, fsync, return the source digest if asked.

    Hashing the source here is free: it rides the read we already do.  The
    write is flushed and ``fsync``-ed before returning so the bytes are durable
    on the destination before the caller renames the temp onto its final name.
    """
    h = hashlib.blake2b() if hash_src else None
    bytes_done = 0
    start = time.monotonic()
    with open(src, "rb") as fsrc, open(tmp, "wb") as fdst:
        while chunk := fsrc.read(_CHUNK_SIZE):
            fdst.write(chunk)
            if h is not None:
                h.update(chunk)
            bytes_done += len(chunk)
            if on_progress is not None:
                on_progress(
                    TransferProgress(
                        file_index=file_index,
                        file_count=file_count,
                        filename=src.name,
                        bytes_done=bytes_done,
                        file_size=size,
                        elapsed_s=time.monotonic() - start,
                    )
                )
        fdst.flush()
        os.fsync(fdst.fileno())
    # A zero-byte file produces no chunk iterations; emit one tick so the bar
    # registers it instead of showing nothing.
    if size == 0 and on_progress is not None:
        on_progress(
            TransferProgress(
                file_index=file_index,
                file_count=file_count,
                filename=src.name,
                bytes_done=0,
                file_size=0,
                elapsed_s=time.monotonic() - start,
            )
        )
    return h.hexdigest() if h is not None else None


def _sweep_stale_temps(final: Path) -> None:
    """Remove *final*'s own ORPHANED temps left by a hard kill / power loss.

    Only temps older than :data:`_STALE_TEMP_AGE_S` are reaped, so a concurrent
    run's in-flight temp — or a slow multi-GB copy still actively touching its
    temp — is never deleted (it keeps a fresh mtime).  Genuine orphans are
    cleaned on a later run.  The leading ``.`` in the pattern means a promoted
    final file can never match.
    """
    cutoff = time.time() - _STALE_TEMP_AGE_S
    for stale in final.parent.glob(f".{final.name}{_TEMP_INFIX}.*"):
        try:
            if stale.stat().st_mtime < cutoff:
                stale.unlink()
        except OSError:
            pass


def _copy_one(
    src: Path,
    final: Path,
    file_index: int,
    file_count: int,
    *,
    verify: bool,
    on_progress: TransferCallback | None,
) -> bool:
    """Copy *src* → *final* atomically; return False on a verification mismatch.

    Writes to a unique temp, optionally content-verifies it against the source
    *before* the rename, then ``os.replace``-s it onto *final* — so a corrupt or
    partial copy never appears at the final name.  Any ``BaseException`` (an
    ``OSError`` such as ENOSPC, or a Ctrl-C / kill) unlinks the temp and
    re-raises, leaving the final name untouched.
    """
    size = src.stat().st_size
    _sweep_stale_temps(final)
    tmp = _temp_path(final)
    try:
        if verify or on_progress is not None:
            src_digest = _stream_copy(
                src, tmp, size, file_index, file_count, on_progress, hash_src=verify
            )
        else:
            # Fast path: no progress bar and no verify — copy bytes only (not
            # metadata; shutil.copy2's copystat can raise on a network share
            # that rejects utime, which must not discard the copy) into the temp
            # for the atomic rename.
            shutil.copyfile(str(src), str(tmp))
            src_digest = None

        if verify:
            on_chunk: Callable[[int], None] | None = None
            if on_progress is not None:
                emit = on_progress
                start = time.monotonic()

                def _emit_verify(read: int) -> None:
                    emit(
                        TransferProgress(
                            file_index=file_index,
                            file_count=file_count,
                            filename=src.name,
                            bytes_done=read,
                            file_size=size,
                            elapsed_s=time.monotonic() - start,
                            phase="verify",
                        )
                    )

                on_chunk = _emit_verify
            if _file_digest(tmp, on_chunk) != src_digest:
                tmp.unlink(missing_ok=True)
                return False
        elif tmp.stat().st_size != size:
            tmp.unlink(missing_ok=True)
            return False

        # Metadata is best-effort: many network mounts (SMB/CIFS/NFS) reject
        # utime, and a verified copy must still be promoted even if its mtime
        # can't be set.  Skip mode never relies on mtime (size/checksum only),
        # so this is safe.
        try:
            shutil.copystat(str(src), str(tmp))
        except OSError:
            pass
        os.replace(tmp, final)
        return True
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def _should_skip(src: Path, final: Path, *, by_content: bool) -> bool:
    """Whether *final* already matches *src* and can be skipped on a rerun.

    Default heuristic is size-only: truncation (the dominant interruption
    failure) changes size, and mtime is unreliable over SMB/CIFS/NFS so it must
    not gate the decision.  With *by_content*, compare full content digests
    too (the small metadata files: an edited config is often the same size).
    """
    if not final.exists():
        return False
    # Size first: a mismatch always means re-copy, and it short-circuits the
    # (network-bound) double hash.
    if final.stat().st_size != src.stat().st_size:
        return False
    if by_content:
        return _file_digest(final) == _file_digest(src)
    return True


def _metadata_files(folder: Path) -> list[Path]:
    """The recording's metadata files in *folder*, always transferred.

    The summary and per-frame timestamps, plus the config snapshot and the
    camera parameter files written beside it: together they make a config
    directory, so the copy at the destination can relaunch the same recording
    setup (``octacam gui <folder>``). They are read from wherever the recording
    keeps them (:func:`octacam.transform.recording_info_dir`): its
    ``octacam_recording`` subfolder, or *folder* itself for a flat recording.
    Only that one directory counts, so a folder recorded into again carries the
    new take's metadata and not the older flat take's leftovers beside it.
    """
    info = recording_info_dir(folder)
    names = [RECORDING_SUMMARY_FILENAME, TIMESTAMPS_FILENAME, CONFIG_SNAPSHOT_FILENAME]
    files = [info / name for name in names]
    for ext in PARAM_FILE_EXTENSIONS:
        files.extend(sorted(info.glob(f"*.{ext}")))
    # Hidden files (e.g. the macOS "._name" forks a Mac leaves on a share) are
    # not recording metadata.
    return [f for f in files if f.is_file() and not f.name.startswith(".")]


def _target(
    folder: Path, dest: Path, src: Path, *, metadata: bool
) -> tuple[Path, str]:
    """Where *src* lands under *dest*, and the name the result reports it by.

    A metadata file keeps its path relative to *folder*, so one in the
    ``octacam_recording`` subfolder lands in the destination's own subfolder
    (reported as ``octacam_recording/<name>``) and a flat one stays flat. Every
    other file (the videos) lands directly in *dest* under its own name, as it
    always has."""
    rel = Path(src.name)
    if metadata:
        try:
            rel = src.relative_to(folder)
        except ValueError:
            pass
    return dest / rel, rel.as_posix()


def transfer_folder(
    folder: Path,
    dest: Path,
    files_only: list[Path],
    dry_run: bool = False,
    verify: bool = True,
    on_progress: TransferCallback | None = None,
) -> TransferResult:
    """Copy *files_only* plus the recording's metadata from *folder* to *dest*.

    The metadata (see :func:`_metadata_files`) is recording_summary.json,
    timestamps.npz, the octacam_config.toml snapshot and the camera parameter
    files beside it, each when present, copied into the same layout it has in
    *folder* (the ``octacam_recording`` subfolder, or flat).

    Parameters
    ----------
    folder:
        Source recording directory.
    dest:
        The exact destination directory to copy into (the caller resolves it,
        e.g. ``transfer.directory / relative_directory``).
    files_only:
        The files to copy (the transcoded videos).  The metadata files are
        always appended.
    dry_run:
        Log intended operations without touching the filesystem.  A file in
        *files_only* that doesn't exist yet (an output an earlier dry-run step
        only planned) is reported as a copy.
    verify:
        Content-verify each freshly-copied file (blake2b of the source vs. the
        written temp) before promoting it to its final name.  Disable for a
        faster size-only check on trusted links.
    on_progress:
        Optional callback invoked after each ``_CHUNK_SIZE`` chunk is written.

    Returns a :class:`TransferResult` naming each file copied, skipped or failed.
    """
    # --- Decide which files to copy -----------------------------------------
    candidates = list(files_only)
    metadata = _metadata_files(folder)
    candidates += [f for f in metadata if f not in candidates]
    by_content = set(metadata)

    result = TransferResult(dest=dest)

    if not candidates:
        log.warning("Nothing to transfer from %s", folder)
        return result
    # (source, destination path, the name the result reports it by)
    plan = [
        (f, *_target(folder, dest, f, metadata=f in by_content)) for f in candidates
    ]

    # --- Dry run: log intended copies; note files already at the destination -
    if dry_run:
        for f, target, label in plan:
            # A planned output has no bytes to compare yet; a real run would
            # produce it first and then copy it.
            if f.exists() and _should_skip(f, target, by_content=f in by_content):
                # Already present (size/checksum match): a real run would skip
                # it, so the preview must report a skip — not a phantom copy.
                result.skipped.append(label)
            else:
                log.info("[dry-run] transfer: %s → %s", f, target)
                result.copied.append(label)
        return result

    # --- Real copy ----------------------------------------------------------
    try:
        dest.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        log.error("Could not create destination directory %s: %s", dest, e)
        result.failed.extend(label for _f, _target_path, label in plan)
        return result

    n = len(plan)
    for idx, (f, target, label) in enumerate(plan, 1):
        try:
            if _should_skip(f, target, by_content=f in by_content):
                # Present and matching — counted in the run summary rather than
                # logged per file, so a full re-run doesn't spam one line for
                # every already-copied output.
                result.skipped.append(label)
                continue
            # The metadata subfolder, when the recording has one.
            target.parent.mkdir(parents=True, exist_ok=True)
            if _copy_one(f, target, idx, n, verify=verify, on_progress=on_progress):
                log.info("Transfer: %s → %s", label, dest)
                result.copied.append(label)
            else:
                log.error("Transfer: %s failed verification — not copied", label)
                result.failed.append(label)
        except OSError as e:
            log.error("Failed to transfer %s: %s", f, e)
            result.failed.append(label)

    return result
