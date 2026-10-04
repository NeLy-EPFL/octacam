"""Copy processed recordings to a destination directory, often a network share.

The caller resolves the destination (``octacam process``: ``transfer.directory``
joined with the summary's ``relative_directory``). Each file is copied to a
unique sibling temp, checked (by digest, or by size) and only then renamed onto
its name, so an interrupted copy never leaves a complete-looking partial, and a
rerun skips what is already there (videos by size, metadata by content). The
recording's metadata always goes along, in the layout it has here (the
``octacam_recording`` subfolder, or flat), so the copy can relaunch the same
setup.
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

# Tags an in-progress copy's temp, so one a hard kill left is swept later.
_TEMP_INFIX = ".octacam-part"

# Only a temp this old is an orphan: a concurrent run's live copy keeps its
# temp's mtime fresh.
_STALE_TEMP_AGE_S = 24 * 3600


@dataclasses.dataclass(frozen=True)
class TransferProgress:
    """Progress snapshot for one file-copy chunk."""

    file_index: int  # 1-based, within this folder's files
    file_count: int
    filename: str
    bytes_done: int  # written ("copy") or read back to hash ("verify")
    file_size: int
    elapsed_s: float  # since this phase started
    phase: str = "copy"

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
    """A sibling temp of *final*: same filesystem, so the rename is atomic, and
    unique per process and call, so concurrent runs never share one."""
    return final.with_name(
        f".{final.name}{_TEMP_INFIX}.{os.getpid()}.{uuid.uuid4().hex}"
    )


def _file_digest(path: Path, on_chunk: Callable[[int], None] | None = None) -> str:
    """blake2b hex digest of *path*; *on_chunk* gets the bytes read so far."""
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
    """Stream *src* into *tmp* and fsync it (durable before the rename);
    returns the source digest when *hash_src*."""
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
    # An empty file reads no chunk; one tick keeps it on the progress bar.
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
    """Remove *final*'s orphaned temps (see :data:`_STALE_TEMP_AGE_S`)."""
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
    """Copy *src* onto *final* through a temp that is verified (or size-checked)
    before the rename; False on a mismatch. Any exception, Ctrl-C included,
    removes the temp and leaves *final* untouched."""
    size = src.stat().st_size
    _sweep_stale_temps(final)
    tmp = _temp_path(final)
    try:
        if verify or on_progress is not None:
            src_digest = _stream_copy(
                src, tmp, size, file_index, file_count, on_progress, hash_src=verify
            )
        else:
            # Bytes only: copy2's copystat raises on a share that rejects utime.
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

        # Best effort: SMB/CIFS/NFS often reject utime, and no skip reads mtime.
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
    """Whether *final* already matches *src*: by size (an interrupted copy is
    short; mtime is unreliable on network shares), and with *by_content* by
    digest too (the metadata: an edited config is often the same size)."""
    if not final.exists():
        return False
    if final.stat().st_size != src.stat().st_size:
        return False
    if by_content:
        return _file_digest(final) == _file_digest(src)
    return True


def _metadata_files(folder: Path) -> list[Path]:
    """The summary, timestamps, config snapshot and camera parameter files, from
    :func:`~octacam.transform.recording_info_dir` only: a folder recorded into
    again carries the new take's metadata, not an older flat take's leftovers."""
    info = recording_info_dir(folder)
    names = [RECORDING_SUMMARY_FILENAME, TIMESTAMPS_FILENAME, CONFIG_SNAPSHOT_FILENAME]
    files = [info / name for name in names]
    for ext in PARAM_FILE_EXTENSIONS:
        files.extend(sorted(info.glob(f"*.{ext}")))
    # Hidden files (the "._name" forks a Mac leaves on a share) are not metadata.
    return [f for f in files if f.is_file() and not f.name.startswith(".")]


def _target(
    folder: Path, dest: Path, src: Path, *, metadata: bool
) -> tuple[Path, str]:
    """Where *src* lands under *dest*, and the name the result reports it by:
    metadata keeps its path relative to *folder*, videos land in *dest*."""
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
    """Copy *files_only* (the processed videos) and the recording's metadata
    from *folder* into *dest*.

    *dry_run* touches nothing and reports a file already matching at *dest* as
    skipped, any other (even an output not produced yet) as copied. *verify*
    compares each copy's digest with the source's before the rename, else only
    its size. *on_progress* gets a :class:`TransferProgress` per chunk.
    """
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

    if dry_run:
        for f, target, label in plan:
            if f.exists() and _should_skip(f, target, by_content=f in by_content):
                result.skipped.append(label)
            else:
                log.info("[dry-run] transfer: %s → %s", f, target)
                result.copied.append(label)
        return result

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
                result.skipped.append(label)  # counted, not logged per file
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
