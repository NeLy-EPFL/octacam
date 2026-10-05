"""The rig instance lock: one octacam owns a rig, on any ``--port``.

An flock on a per-rig file in the temp dir, keyed on the resolved config dir so
every spelling of one rig shares it. The OS drops it when its holder exits, even
on SIGKILL, so it never goes stale. The holder writes its pid into the file for
the messages that name it.
"""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import logging
import os
import tempfile
from collections.abc import Iterator
from pathlib import Path

from octacam.files import flock_held

log = logging.getLogger("octacam")


class RigInUse(Exception):
    """Another octacam holds the rig's lock; ``holder`` is its pid ("unknown"
    when unreadable)."""

    def __init__(self, holder: str) -> None:
        super().__init__(f"another octacam (pid {holder}) owns this rig")
        self.holder = holder


def _lock_path(config_dir: Path) -> Path:
    key = hashlib.sha1(str(config_dir.resolve()).encode()).hexdigest()[:16]
    return Path(tempfile.gettempdir()) / f"octacam-{key}.lock"


@contextlib.contextmanager
def instance_lock(config_dir: Path) -> Iterator[None]:
    """Own the rig at *config_dir* for the block, or raise RigInUse.

    A lock file that cannot be opened is no lock: the exclusive camera open is
    then the only guard."""
    path = _lock_path(config_dir)
    try:
        handle = open(path, "a+")
    except OSError as e:
        log.debug("Instance lock %s unavailable (%s); relying on camera lock", path, e)
        yield
        return
    with handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            handle.seek(0)
            raise RigInUse(handle.read().strip() or "unknown") from None
        handle.seek(0)
        handle.truncate()
        handle.write(f"{os.getpid()}\n")
        handle.flush()
        yield


def holder(config_dir: Path) -> str | None:
    """The pid holding the rig's lock ("unknown" when unreadable), or None when
    it is free. The probe never takes or blocks the holder's lock; a lock the
    filesystem cannot probe counts as held."""
    path = _lock_path(config_dir)
    try:
        if flock_held(path) is False:
            return None
        return path.read_text().strip() or "unknown"
    except OSError:
        return None  # never created: nobody has locked this rig
