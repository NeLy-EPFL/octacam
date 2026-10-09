"""Files other processes may read mid-write: atomic text writes, partial temps
renamed into place, and flock liveness.

An flock is dropped by the OS when its holder exits or crashes, so a lock file
that can be locked belongs to nobody: liveness without PID bookkeeping.
"""

from __future__ import annotations

import contextlib
import errno
import fcntl
import glob
import os
import uuid
from pathlib import Path

# Tags a temp that is renamed onto its final name once complete; folder scans
# skip such files and each writer sweeps its own orphans.
PARTIAL_INFIX = ".octacam-part"

_LOCK_HELD = (errno.EWOULDBLOCK, errno.EAGAIN, errno.EACCES)


def _unique() -> str:
    """Unique per process and call, so concurrent writers never share a temp."""
    return f"{os.getpid()}.{uuid.uuid4().hex}"


def atomic_write_text(path: str | Path, text: str) -> None:
    """Write *text* to *path* through an fsynced sibling temp and a rename.

    The temp is created like any other file, so the result gets the umask's
    permissions: a file another user or a share must read is never 0600.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{_unique()}.tmp")
    try:
        with open(tmp, "x") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            tmp.unlink(missing_ok=True)
        raise


def partial_path(final: Path, *, extension_last: bool = False) -> Path:
    """A hidden, unique temp beside *final* (the same directory, so the rename
    onto it is atomic). *extension_last* keeps *final*'s suffix at the end, for
    ffmpeg, which picks its muxer from it.
    """
    stem, suffix = (final.stem, final.suffix) if extension_last else (final.name, "")
    return final.with_name(f".{stem}{PARTIAL_INFIX}.{_unique()}{suffix}")


def partial_glob(final: Path, *, extension_last: bool = False) -> str:
    """A glob for every `partial_path` of *final* (with *extension_last*,
    the pid-less `.<stem>.octacam-part<ext>` too). Escaped: unescaped, a
    camera `cam[1]` would match `cam1`'s temps.
    """
    if extension_last:
        return f".{glob.escape(final.stem)}{PARTIAL_INFIX}*{glob.escape(final.suffix)}"
    return f".{glob.escape(final.name)}{PARTIAL_INFIX}.*"


def is_partial(path: Path) -> bool:
    """Whether *path* is a partial temp (a hard kill can leave one behind)."""
    return PARTIAL_INFIX in path.name


def flock_held(path: Path, mode: str = "r") -> bool | None:
    """Whether another process holds an exclusive flock on *path*: True, False
    (the probe's own lock is released at once), or None when the filesystem
    cannot tell. Raises OSError when *path* cannot be opened in *mode*.
    """
    with open(path, mode) as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as e:
            return True if e.errno in _LOCK_HELD else None
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        return False
