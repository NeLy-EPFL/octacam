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
and re-running skips files already present (size match, or full checksum with
``checksum=True``) — resume is at file granularity.
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

from octacam.transform import RECORDING_SUMMARY_FILENAME

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
    """Outcome of transferring one folder to its destination.

    Truthy iff nothing failed *and* at least one file was copied or skipped, so
    callers can keep using ``if not result:`` to detect a hard failure.
    """

    dest: Path
    copied: list[str] = dataclasses.field(default_factory=list)
    skipped: list[str] = dataclasses.field(default_factory=list)
    failed: list[str] = dataclasses.field(default_factory=list)

    def __bool__(self) -> bool:
        return not self.failed and bool(self.copied or self.skipped)


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


def _should_skip(src: Path, final: Path, *, checksum: bool) -> bool:
    """Whether *final* already matches *src* and can be skipped on a rerun.

    Default heuristic is size-only: truncation (the dominant interruption
    failure) changes size, and mtime is unreliable over SMB/CIFS/NFS so it must
    not gate the decision.  With *checksum*, compare full content digests
    instead (repair mode — re-copies any final whose bytes differ).
    """
    if not final.exists():
        return False
    # Size first: a mismatch always means re-copy, and it short-circuits the
    # (expensive, network-bound) double hash in --checksum mode.
    if final.stat().st_size != src.stat().st_size:
        return False
    if checksum:
        return _file_digest(final) == _file_digest(src)
    return True


def transfer_destination(
    folder: Path, dest_root: Path, local_base: Path | None = None
) -> Path:
    """Destination directory *folder* maps to under *dest_root*.

    When *local_base* is given and *folder* lies under it, the path relative to
    *local_base* is reproduced under *dest_root*; otherwise only *folder*'s name
    is used.  Shared with the CLI so its collision check matches the real copy.

    Both paths are resolved before the relative computation so a relative input
    (e.g. ``data/260624``) or one with ``..``/symlinks still mirrors correctly
    against the (resolved) auto-derived base.
    """
    rel = None
    if local_base is not None:
        try:
            rel = folder.resolve().relative_to(local_base.resolve())
        except ValueError:
            rel = None
    return dest_root / rel if rel is not None else dest_root / folder.name


def transfer_folder(
    folder: Path,
    dest: Path,
    files_only: list[Path] | None = None,
    dry_run: bool = False,
    verify: bool = True,
    checksum: bool = False,
    on_progress: TransferCallback | None = None,
) -> TransferResult:
    """Copy mp4s (and recording_summary.json) from *folder* to *dest*.

    Parameters
    ----------
    folder:
        Source recording directory.
    dest:
        The exact destination directory to copy into (the caller resolves it,
        e.g. ``transfer.directory / relative_directory``).
    files_only:
        Explicit list of files to copy; overrides the default (all *.mp4 in
        *folder*).  ``recording_summary.json`` is always appended if present.
    dry_run:
        Log intended operations without touching the filesystem.
    verify:
        Content-verify each freshly-copied file (blake2b of the source vs. the
        written temp) before promoting it to its final name.  Disable for a
        faster size-only check on trusted links.
    checksum:
        When deciding whether an already-present file can be skipped, compare
        full content digests rather than just size (repair mode).
    on_progress:
        Optional callback invoked after each ``_CHUNK_SIZE`` chunk is written.

    Returns a :class:`TransferResult` (truthy on success, falsy on hard failure
    or nothing to copy).
    """
    # --- Decide which files to copy -----------------------------------------
    if files_only is not None:
        candidates = list(files_only)
    else:
        candidates = sorted(folder.glob("*.mp4"))

    summary = folder / RECORDING_SUMMARY_FILENAME
    if summary.exists() and summary not in candidates:
        candidates.append(summary)

    result = TransferResult(dest=dest)

    if not candidates:
        log.warning("Nothing to transfer from %s", folder)
        return result

    # --- Dry run: log intended copies; note files already at the destination -
    if dry_run:
        for f in candidates:
            target = dest / f.name
            if _should_skip(f, target, checksum=checksum):
                # Already present (size/checksum match): a real run would skip
                # it, so the preview must report a skip — not a phantom copy.
                result.skipped.append(f.name)
            else:
                log.info("[dry-run] transfer: %s → %s", f, target)
                result.copied.append(f.name)
        return result

    # --- Real copy ----------------------------------------------------------
    try:
        dest.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        log.error("Could not create destination directory %s: %s", dest, e)
        result.failed.extend(f.name for f in candidates)
        return result

    n = len(candidates)
    for idx, f in enumerate(candidates, 1):
        target = dest / f.name
        try:
            if _should_skip(f, target, checksum=checksum):
                # Present and matching — counted in the run summary rather than
                # logged per file, so a full re-run doesn't spam one line for
                # every already-copied output.
                result.skipped.append(f.name)
                continue
            if _copy_one(f, target, idx, n, verify=verify, on_progress=on_progress):
                log.info("Transfer: %s → %s", f.name, dest)
                result.copied.append(f.name)
            else:
                log.error("Transfer: %s failed verification — not copied", f.name)
                result.failed.append(f.name)
        except OSError as e:
            log.error("Failed to transfer %s: %s", f, e)
            result.failed.append(f.name)

    return result
