"""Atomic text writes, partial-temp names and the flock liveness probe."""

import fcntl
import os

from octacam.files import (
    PARTIAL_INFIX,
    atomic_write_text,
    flock_held,
    is_partial,
    partial_glob,
    partial_path,
)


def test_atomic_write_leaves_no_temp(tmp_path):
    atomic_write_text(tmp_path / "sub" / "octacam_config.toml", "[gui]\n")
    assert (tmp_path / "sub" / "octacam_config.toml").read_text() == "[gui]\n"
    assert [p.name for p in (tmp_path / "sub").iterdir()] == ["octacam_config.toml"]


def test_atomic_write_gets_the_umask_permissions(tmp_path):
    old = os.umask(0o022)
    try:
        atomic_write_text(tmp_path / "octacam_config.toml", "[gui]\n")
    finally:
        os.umask(old)
    assert (tmp_path / "octacam_config.toml").stat().st_mode & 0o777 == 0o644


def test_partial_names_are_unique_hidden_and_globbed(tmp_path):
    final = tmp_path / "cam[1].mp4"
    for extension_last in (False, True):
        a = partial_path(final, extension_last=extension_last)
        b = partial_path(final, extension_last=extension_last)
        assert a != b and a.parent == tmp_path and a.name.startswith(".")
        assert is_partial(a) and PARTIAL_INFIX in a.name
        assert (a.suffix == ".mp4") == extension_last
        a.write_bytes(b"")
        pattern = partial_glob(final, extension_last=extension_last)
        assert list(tmp_path.glob(pattern)) == [a]
        # Escaped: cam[1]'s pattern never matches cam1's temps.
        other = partial_path(tmp_path / "cam1.mp4", extension_last=extension_last)
        other.write_bytes(b"")
        assert list(tmp_path.glob(pattern)) == [a]
        a.unlink()
        other.unlink()


def test_flock_held(tmp_path):
    path = tmp_path / "job.lock"
    path.write_text("")
    assert flock_held(path) is False
    with open(path) as holder:
        fcntl.flock(holder.fileno(), fcntl.LOCK_EX)
        assert flock_held(path) is True
        assert flock_held(path, "r+") is True
    assert flock_held(path) is False  # the probe left no lock behind
    try:
        flock_held(tmp_path / "missing")
    except OSError:
        pass
    else:
        raise AssertionError("an unopenable path must raise")
