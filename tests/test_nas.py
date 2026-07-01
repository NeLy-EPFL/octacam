"""NAS export: atomic copy, integrity verification, file-granularity resume.

These exercise the reliability guarantees of ``octacam.nas`` and the CLI
discovery helper that drives it — all with plain files in ``tmp_path``; no
ffmpeg or real NAS needed.

``copy_folder_to_nas`` now takes a single precomputed ``dest`` directory (the
exact directory files are copied into); the old ``nas_root`` + ``local_base``
mirroring pair is gone.  Tests compute the expected ``dest`` with the still-
present ``nas_destination(folder, nas_root, local_base)`` helper so the
destination path logic they used to rely on is preserved.
"""

from pathlib import Path

import pytest

from octacam import nas as nas_mod
from octacam.nas import (
    NasCopyResult,
    copy_folder_to_nas,
    nas_destination,
)
from octacam.transform import RECORDING_SUMMARY_FILENAME

TEMP_GLOB = f".*{nas_mod._TEMP_INFIX}*"


def _make_recording(folder: Path, files: dict[str, bytes]) -> Path:
    """Create a recording dir with the given ``name -> bytes`` files + summary."""
    folder.mkdir(parents=True, exist_ok=True)
    for name, data in files.items():
        (folder / name).write_bytes(data)
    (folder / RECORDING_SUMMARY_FILENAME).write_text("{}")
    return folder


def _no_temps(dest: Path) -> bool:
    return not list(dest.glob(TEMP_GLOB))


# --- destination path logic -------------------------------------------------


def test_nas_destination_mirror_and_bare():
    base = Path("/data/octacam")
    folder = Path("/data/octacam/exp/Fly1/001")
    assert nas_destination(folder, Path("/nas"), base) == Path("/nas/exp/Fly1/001")
    # No base → only the last component.
    assert nas_destination(folder, Path("/nas"), None) == Path("/nas/001")
    # Folder not under the base → falls back to the last component.
    assert nas_destination(Path("/other/001"), Path("/nas"), base) == Path("/nas/001")


# --- happy path / atomicity hygiene -----------------------------------------


def test_copy_happy_path_no_temp_left(tmp_path):
    src = _make_recording(
        tmp_path / "rec",
        {"camera_LF.mp4": b"abc" * 1000, "camera_RF.mp4": b"xyz" * 2000},
    )
    nas = tmp_path / "nas"
    dest = nas_destination(src, nas, tmp_path)
    result = copy_folder_to_nas(src, dest=dest)

    assert dest == nas / "rec"
    assert result  # truthy on success
    assert set(result.copied) == {
        "camera_LF.mp4",
        "camera_RF.mp4",
        RECORDING_SUMMARY_FILENAME,
    }
    assert not result.failed
    assert (dest / "camera_LF.mp4").read_bytes() == b"abc" * 1000
    assert (dest / "camera_RF.mp4").read_bytes() == b"xyz" * 2000
    assert (dest / RECORDING_SUMMARY_FILENAME).exists()
    assert _no_temps(dest)  # mirror test_config_writer's no-temp assertion


def test_copy_bare_name_without_base(tmp_path):
    src = _make_recording(tmp_path / "deep" / "001", {"camera_LF.mp4": b"x" * 10})
    nas = tmp_path / "nas"
    dest = nas_destination(src, nas)  # no local_base → bare name
    assert dest == nas / "001"
    copy_folder_to_nas(src, dest=dest)
    assert (nas / "001" / "camera_LF.mp4").read_bytes() == b"x" * 10


# --- resume / skip ----------------------------------------------------------


def test_skip_on_rerun_size(tmp_path):
    src = _make_recording(tmp_path / "rec", {"camera_LF.mp4": b"data" * 500})
    nas = tmp_path / "nas"
    dest = nas_destination(src, nas, tmp_path)
    copy_folder_to_nas(src, dest=dest)
    mtimes = {p.name: p.stat().st_mtime_ns for p in dest.iterdir()}

    result = copy_folder_to_nas(src, dest=dest)
    assert set(result.skipped) == {"camera_LF.mp4", RECORDING_SUMMARY_FILENAME}
    assert not result.copied
    # Skipped files are not rewritten.
    for p in dest.iterdir():
        assert p.stat().st_mtime_ns == mtimes[p.name]


def test_checksum_repair(tmp_path):
    src = _make_recording(tmp_path / "rec", {"camera_LF.mp4": b"A" * 1000})
    nas = tmp_path / "nas"
    dest = nas_destination(src, nas, tmp_path)
    dest.mkdir(parents=True)
    # Same size, different content — size-only can't tell, checksum can.
    (dest / "camera_LF.mp4").write_bytes(b"B" * 1000)
    (dest / RECORDING_SUMMARY_FILENAME).write_text("{}")

    r1 = copy_folder_to_nas(src, dest=dest, checksum=False)
    assert "camera_LF.mp4" in r1.skipped
    assert (dest / "camera_LF.mp4").read_bytes() == b"B" * 1000  # not repaired

    r2 = copy_folder_to_nas(src, dest=dest, checksum=True)
    assert "camera_LF.mp4" in r2.copied
    assert (dest / "camera_LF.mp4").read_bytes() == b"A" * 1000  # repaired
    assert _no_temps(dest)


# --- integrity / atomicity under failure ------------------------------------


def test_verify_mismatch_fails_and_cleans_up(tmp_path, monkeypatch):
    src = _make_recording(tmp_path / "rec", {"camera_LF.mp4": b"data" * 100})
    nas = tmp_path / "nas"
    # Force every read-back digest to differ from the (real, inline) source digest.
    monkeypatch.setattr(nas_mod, "_file_digest", lambda *a, **k: "deadbeef")

    dest = nas_destination(src, nas, tmp_path)
    result = copy_folder_to_nas(src, dest=dest, verify=True)
    assert "camera_LF.mp4" in result.failed
    assert not (dest / "camera_LF.mp4").exists()  # never promoted
    assert _no_temps(dest)  # temp cleaned
    assert not result  # falsy: a file failed


def test_atomic_interrupt_then_resume(tmp_path, monkeypatch):
    src = _make_recording(tmp_path / "rec", {"camera_LF.mp4": b"x" * 5000})
    nas = tmp_path / "nas"
    dest = nas_destination(src, nas, tmp_path)

    def boom(_a, _b):
        raise OSError("simulated interruption at rename")

    monkeypatch.setattr(nas_mod.os, "replace", boom)
    result = copy_folder_to_nas(src, dest=dest)
    assert result.failed  # the rename never completed
    assert not (dest / "camera_LF.mp4").exists()  # no complete-looking partial
    assert _no_temps(dest)  # temp removed on the exception path

    monkeypatch.undo()  # "next run" with a working filesystem
    result2 = copy_folder_to_nas(src, dest=dest)
    assert result2
    assert (dest / "camera_LF.mp4").read_bytes() == b"x" * 5000


# --- options / edge cases ---------------------------------------------------


def test_copystat_failure_is_best_effort(tmp_path, monkeypatch):
    # Many NAS mounts reject utime; a verified copy must still be promoted.
    src = _make_recording(tmp_path / "rec", {"camera_LF.mp4": b"x" * 200})

    def boom(*_a, **_k):
        raise OSError("utime rejected by the NAS")

    monkeypatch.setattr(nas_mod.shutil, "copystat", boom)
    result = copy_folder_to_nas(src, dest=tmp_path / "nas" / "rec")
    assert result and not result.failed
    assert (tmp_path / "nas" / "rec" / "camera_LF.mp4").read_bytes() == b"x" * 200


def test_sweep_only_reaps_old_temps(tmp_path):
    import os as _os
    import time as _time

    src = _make_recording(tmp_path / "rec", {"camera_LF.mp4": b"x" * 100})
    dest = tmp_path / "nas" / "rec"
    dest.mkdir(parents=True)
    fresh = dest / f".camera_LF.mp4{nas_mod._TEMP_INFIX}.999.fresh"
    old = dest / f".camera_LF.mp4{nas_mod._TEMP_INFIX}.999.old"
    fresh.write_bytes(b"a concurrent run's live temp")
    old.write_bytes(b"a crash orphan")
    old_t = _time.time() - nas_mod._STALE_TEMP_AGE_S - 100
    _os.utime(old, (old_t, old_t))

    copy_folder_to_nas(src, dest=dest)
    assert fresh.exists()  # a live/concurrent temp is never deleted
    assert not old.exists()  # a genuine orphan is reaped


def test_mixed_roots_skip_non_recording(tmp_path):
    # A stray non-recording dir mixed with a valid recording must NOT abort.
    from octacam.cli import _find_recording_dirs

    rec = tmp_path / "recA"
    rec.mkdir()
    (rec / RECORDING_SUMMARY_FILENAME).write_text("{}")
    stray = tmp_path / "notes"
    stray.mkdir()

    found = _find_recording_dirs([rec, stray], recursive=False)
    assert found == [rec]  # valid kept, stray skipped, no SystemExit


def test_no_verify_copies(tmp_path):
    src = _make_recording(tmp_path / "rec", {"camera_LF.mp4": b"hello" * 100})
    nas = tmp_path / "nas"
    result = copy_folder_to_nas(src, dest=nas / "rec", verify=False)
    assert result
    assert (nas / "rec" / "camera_LF.mp4").read_bytes() == b"hello" * 100


def test_zero_byte_source(tmp_path):
    src = _make_recording(tmp_path / "rec", {"camera_LF.mp4": b""})
    nas = tmp_path / "nas"
    result = copy_folder_to_nas(src, dest=nas / "rec")
    target = nas / "rec" / "camera_LF.mp4"
    assert result
    assert target.exists() and target.stat().st_size == 0


def test_dry_run_touches_nothing(tmp_path):
    src = _make_recording(tmp_path / "rec", {"camera_LF.mp4": b"x" * 10})
    nas = tmp_path / "nas"
    result = copy_folder_to_nas(src, dest=nas / "rec", dry_run=True)
    assert result  # lists intended copies
    assert not nas.exists()


def test_nothing_to_copy_is_falsy(tmp_path):
    folder = tmp_path / "empty"
    folder.mkdir()
    result = copy_folder_to_nas(folder, dest=tmp_path / "nas" / "empty")
    assert isinstance(result, NasCopyResult)
    assert not result
    assert not result.copied and not result.failed


def test_progress_phases(tmp_path):
    src = _make_recording(tmp_path / "rec", {"camera_LF.mp4": b"z" * 4096})
    events = []
    copy_folder_to_nas(src, dest=tmp_path / "nas" / "rec", on_progress=events.append)
    phases = {e.phase for e in events}
    assert "copy" in phases and "verify" in phases

    events_off = []
    src2 = _make_recording(tmp_path / "rec2", {"camera_LF.mp4": b"z" * 4096})
    copy_folder_to_nas(
        src2,
        dest=tmp_path / "nas2" / "rec2",
        verify=False,
        on_progress=events_off.append,
    )
    assert {e.phase for e in events_off} == {"copy"}


# --- CLI driver helpers -----------------------------------------------------


def test_discovery_hint(tmp_path):
    from octacam.cli import _find_recording_dirs

    rec = tmp_path / "parent" / "001"
    rec.mkdir(parents=True)
    (rec / RECORDING_SUMMARY_FILENAME).write_text("{}")

    # Non-recursive at a non-recording parent → exit with a hint.
    with pytest.raises(SystemExit):
        _find_recording_dirs([tmp_path / "parent"], recursive=False)
    # Recursive discovers the nested recording.
    found = {p.resolve() for p in _find_recording_dirs([tmp_path / "parent"], True)}
    assert rec.resolve() in found
    # Pointing directly at a recording works non-recursively.
    assert _find_recording_dirs([rec], recursive=False) == [rec]
