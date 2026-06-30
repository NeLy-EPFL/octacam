"""NAS export: atomic copy, integrity verification, file-granularity resume.

These exercise the reliability guarantees of ``octacam.nas`` and the CLI
helpers that drive it (auto common-parent base, collision guard, discovery
hint) — all with plain files in ``tmp_path``; no ffmpeg or real NAS needed.
"""

import os
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
    result = copy_folder_to_nas(src, nas_root=nas, local_base=tmp_path)

    dest = nas / "rec"
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
    copy_folder_to_nas(src, nas_root=nas)  # no local_base, single folder
    assert (nas / "001" / "camera_LF.mp4").read_bytes() == b"x" * 10


# --- resume / skip ----------------------------------------------------------


def test_skip_on_rerun_size(tmp_path):
    src = _make_recording(tmp_path / "rec", {"camera_LF.mp4": b"data" * 500})
    nas = tmp_path / "nas"
    copy_folder_to_nas(src, nas_root=nas, local_base=tmp_path)
    dest = nas / "rec"
    mtimes = {p.name: p.stat().st_mtime_ns for p in dest.iterdir()}

    result = copy_folder_to_nas(src, nas_root=nas, local_base=tmp_path)
    assert set(result.skipped) == {"camera_LF.mp4", RECORDING_SUMMARY_FILENAME}
    assert not result.copied
    # Skipped files are not rewritten.
    for p in dest.iterdir():
        assert p.stat().st_mtime_ns == mtimes[p.name]


def test_checksum_repair(tmp_path):
    src = _make_recording(tmp_path / "rec", {"camera_LF.mp4": b"A" * 1000})
    nas = tmp_path / "nas"
    dest = nas / "rec"
    dest.mkdir(parents=True)
    # Same size, different content — size-only can't tell, checksum can.
    (dest / "camera_LF.mp4").write_bytes(b"B" * 1000)
    (dest / RECORDING_SUMMARY_FILENAME).write_text("{}")

    r1 = copy_folder_to_nas(src, nas_root=nas, local_base=tmp_path, checksum=False)
    assert "camera_LF.mp4" in r1.skipped
    assert (dest / "camera_LF.mp4").read_bytes() == b"B" * 1000  # not repaired

    r2 = copy_folder_to_nas(src, nas_root=nas, local_base=tmp_path, checksum=True)
    assert "camera_LF.mp4" in r2.copied
    assert (dest / "camera_LF.mp4").read_bytes() == b"A" * 1000  # repaired
    assert _no_temps(dest)


# --- integrity / atomicity under failure ------------------------------------


def test_verify_mismatch_fails_and_cleans_up(tmp_path, monkeypatch):
    src = _make_recording(tmp_path / "rec", {"camera_LF.mp4": b"data" * 100})
    nas = tmp_path / "nas"
    # Force every read-back digest to differ from the (real, inline) source digest.
    monkeypatch.setattr(nas_mod, "_file_digest", lambda *a, **k: "deadbeef")

    result = copy_folder_to_nas(src, nas_root=nas, local_base=tmp_path, verify=True)
    dest = nas / "rec"
    assert "camera_LF.mp4" in result.failed
    assert not (dest / "camera_LF.mp4").exists()  # never promoted
    assert _no_temps(dest)  # temp cleaned
    assert not result  # falsy: a file failed


def test_atomic_interrupt_then_resume(tmp_path, monkeypatch):
    src = _make_recording(tmp_path / "rec", {"camera_LF.mp4": b"x" * 5000})
    nas = tmp_path / "nas"

    def boom(_a, _b):
        raise OSError("simulated interruption at rename")

    monkeypatch.setattr(nas_mod.os, "replace", boom)
    result = copy_folder_to_nas(src, nas_root=nas, local_base=tmp_path)
    dest = nas / "rec"
    assert result.failed  # the rename never completed
    assert not (dest / "camera_LF.mp4").exists()  # no complete-looking partial
    assert _no_temps(dest)  # temp removed on the exception path

    monkeypatch.undo()  # "next run" with a working filesystem
    result2 = copy_folder_to_nas(src, nas_root=nas, local_base=tmp_path)
    assert result2
    assert (dest / "camera_LF.mp4").read_bytes() == b"x" * 5000


# --- options / edge cases ---------------------------------------------------


def test_no_verify_copies(tmp_path):
    src = _make_recording(tmp_path / "rec", {"camera_LF.mp4": b"hello" * 100})
    nas = tmp_path / "nas"
    result = copy_folder_to_nas(src, nas_root=nas, local_base=tmp_path, verify=False)
    assert result
    assert (nas / "rec" / "camera_LF.mp4").read_bytes() == b"hello" * 100


def test_zero_byte_source(tmp_path):
    src = _make_recording(tmp_path / "rec", {"camera_LF.mp4": b""})
    nas = tmp_path / "nas"
    result = copy_folder_to_nas(src, nas_root=nas, local_base=tmp_path)
    target = nas / "rec" / "camera_LF.mp4"
    assert result
    assert target.exists() and target.stat().st_size == 0


def test_dry_run_touches_nothing(tmp_path):
    src = _make_recording(tmp_path / "rec", {"camera_LF.mp4": b"x" * 10})
    nas = tmp_path / "nas"
    result = copy_folder_to_nas(src, nas_root=nas, local_base=tmp_path, dry_run=True)
    assert result  # lists intended copies
    assert not nas.exists()


def test_nothing_to_copy_is_falsy(tmp_path):
    folder = tmp_path / "empty"
    folder.mkdir()
    result = copy_folder_to_nas(folder, nas_root=tmp_path / "nas")
    assert isinstance(result, NasCopyResult)
    assert not result
    assert not result.copied and not result.failed


def test_progress_phases(tmp_path):
    src = _make_recording(tmp_path / "rec", {"camera_LF.mp4": b"z" * 4096})
    events = []
    copy_folder_to_nas(
        src, nas_root=tmp_path / "nas", local_base=tmp_path, on_progress=events.append
    )
    phases = {e.phase for e in events}
    assert "copy" in phases and "verify" in phases

    events_off = []
    src2 = _make_recording(tmp_path / "rec2", {"camera_LF.mp4": b"z" * 4096})
    copy_folder_to_nas(
        src2,
        nas_root=tmp_path / "nas2",
        local_base=tmp_path,
        verify=False,
        on_progress=events_off.append,
    )
    assert {e.phase for e in events_off} == {"copy"}


# --- CLI driver helpers -----------------------------------------------------


def test_effective_local_base(tmp_path):
    from octacam.cli import _effective_nas_local_base

    a = tmp_path / "exp" / "Fly1" / "001"
    b = tmp_path / "exp" / "Fly2" / "001"
    a.mkdir(parents=True)
    b.mkdir(parents=True)

    # One level above the common ancestor (tmp/exp), i.e. tmp — so "exp" itself
    # is preserved on the NAS and successive experiments stay distinct.
    common = Path(os.path.commonpath([str(a.resolve()), str(b.resolve())]))
    base = _effective_nas_local_base([a, b], None)
    assert base == common.parent
    assert a.resolve().relative_to(base) == Path("exp/Fly1/001")
    assert b.resolve().relative_to(base) == Path("exp/Fly2/001")
    # A single folder keeps the bare-name behaviour.
    assert _effective_nas_local_base([a], None) is None
    # An explicit base always wins.
    assert _effective_nas_local_base([a, b], tmp_path) == tmp_path


def test_collision_guard(tmp_path):
    from octacam.cli import _check_nas_collisions

    a = tmp_path / "x" / "001"
    b = tmp_path / "y" / "001"
    a.mkdir(parents=True)
    b.mkdir(parents=True)
    nas = tmp_path / "nas"

    with pytest.raises(SystemExit):
        _check_nas_collisions([a, b], nas, None)  # both → nas/001
    # Mirroring from a common base makes them distinct — no exit.
    _check_nas_collisions([a, b], nas, tmp_path)


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
