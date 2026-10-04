"""Transfer step: atomic copy, integrity verification, file-granularity resume.

These exercise the reliability guarantees of ``octacam.transfer`` and the CLI
discovery helper that drives it — all with plain files in ``tmp_path``; no
ffmpeg or real destination needed. ``transfer_folder`` copies into the exact
``dest`` directory its caller resolved.
"""

from pathlib import Path

import pytest

from octacam import transfer as transfer_mod
from octacam.transfer import TransferResult, transfer_folder
from octacam.transform import (
    CONFIG_SNAPSHOT_FILENAME,
    RECORDING_INFO_DIRNAME,
    RECORDING_SUMMARY_FILENAME,
    TIMESTAMPS_FILENAME,
    recording_info_dir,
)

TEMP_GLOB = f".*{transfer_mod._TEMP_INFIX}*"


def _make_recording(
    folder: Path, files: dict[str, bytes], *, nested: bool = False
) -> Path:
    """Create a recording dir with the given ``name -> bytes`` files + summary.

    The summary sits flat beside the videos (a recording made before the
    ``octacam_recording`` subfolder), or in that subfolder with ``nested``."""
    folder.mkdir(parents=True, exist_ok=True)
    for name, data in files.items():
        (folder / name).write_bytes(data)
    info = folder / RECORDING_INFO_DIRNAME if nested else folder
    info.mkdir(exist_ok=True)
    (info / RECORDING_SUMMARY_FILENAME).write_text("{}")
    return folder


SUMMARY_NESTED = f"{RECORDING_INFO_DIRNAME}/{RECORDING_SUMMARY_FILENAME}"


def _transfer(src: Path, dest: Path, **kwargs) -> TransferResult:
    """transfer_folder over every video in *src*, as the CLI passes its outputs."""
    return transfer_folder(src, dest, sorted(src.glob("*.mp4")), **kwargs)


def _no_temps(dest: Path) -> bool:
    return not list(dest.glob(TEMP_GLOB))


# --- happy path / atomicity hygiene -----------------------------------------


def test_copy_happy_path_no_temp_left(tmp_path):
    src = _make_recording(
        tmp_path / "rec",
        {"camera_LF.mp4": b"abc" * 1000, "camera_RF.mp4": b"xyz" * 2000},
    )
    dest_root = tmp_path / "dest"
    dest = dest_root / "rec"
    result = _transfer(src, dest=dest)

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


def test_copy_includes_timestamps_when_present(tmp_path):
    # timestamps.npz is opt-in; when it exists it rides along like the summary.
    src = _make_recording(tmp_path / "rec", {"camera_LF.mp4": b"abc" * 1000})
    (src / TIMESTAMPS_FILENAME).write_bytes(b"\x00npz-bytes")
    dest = tmp_path / "dest" / "rec"
    result = _transfer(src, dest=dest)

    assert TIMESTAMPS_FILENAME in set(result.copied)
    assert (dest / TIMESTAMPS_FILENAME).read_bytes() == b"\x00npz-bytes"


def test_copy_without_timestamps_is_fine(tmp_path):
    # No timestamps.npz (the default) → nothing extra, no error.
    src = _make_recording(tmp_path / "rec", {"camera_LF.mp4": b"abc" * 1000})
    dest = tmp_path / "dest" / "rec"
    result = _transfer(src, dest=dest)

    assert TIMESTAMPS_FILENAME not in set(result.copied)
    assert not (dest / TIMESTAMPS_FILENAME).exists()


def test_copy_carries_the_config_snapshot_and_camera_files(tmp_path):
    # With each camera's parameter file beside it, the config snapshot makes the
    # copy a config dir a new session can launch from, so all of it travels.
    src = _make_recording(tmp_path / "rec", {"camera_LF.mp4": b"v" * 100})
    (src / CONFIG_SNAPSHOT_FILENAME).write_text("[record]\nfps = 80.0\n")
    (src / "40018631.pfs").write_text("basler")
    (src / "17475185.txt").write_text("flir")
    (src / "FAKE-0.fake").write_text("fake")
    # Not metadata: an untranscoded source, and a hidden macOS fork.
    (src / "camera_LF.mkv").write_bytes(b"raw")
    (src / "._17475185.txt").write_text("fork")
    dest = tmp_path / "dest" / "rec"

    result = _transfer(src, dest=dest)

    assert result
    assert sorted(p.name for p in dest.iterdir()) == sorted(
        [
            "camera_LF.mp4",
            RECORDING_SUMMARY_FILENAME,
            CONFIG_SNAPSHOT_FILENAME,
            "40018631.pfs",
            "17475185.txt",
            "FAKE-0.fake",
        ]
    )
    assert (dest / "17475185.txt").read_text() == "flir"


def test_metadata_changed_at_the_same_size_is_recopied(tmp_path):
    # An edited config is often exactly as long as the copy already there
    # (fps = 80.0 -> 90.0), so metadata is compared by content; a video keeps
    # the cheap size-only check.
    src = _make_recording(tmp_path / "rec", {"camera_LF.mp4": b"A" * 100})
    (src / CONFIG_SNAPSHOT_FILENAME).write_text("[record]\nfps = 80.0\n")
    dest = tmp_path / "dest" / "rec"
    _transfer(src, dest=dest)
    (src / CONFIG_SNAPSHOT_FILENAME).write_text("[record]\nfps = 90.0\n")
    (src / "camera_LF.mp4").write_bytes(b"B" * 100)

    planned = _transfer(src, dest=dest, dry_run=True)
    result = _transfer(src, dest=dest)

    assert planned.copied == [CONFIG_SNAPSHOT_FILENAME]
    assert result.copied == [CONFIG_SNAPSHOT_FILENAME]
    assert set(result.skipped) == {"camera_LF.mp4", RECORDING_SUMMARY_FILENAME}
    assert (dest / CONFIG_SNAPSHOT_FILENAME).read_text() == "[record]\nfps = 90.0\n"
    assert (dest / "camera_LF.mp4").read_bytes() == b"A" * 100


# --- the recording layout: metadata subfolder, or flat ------------------------


def test_nested_metadata_lands_in_the_destinations_subfolder(tmp_path):
    src = _make_recording(tmp_path / "rec", {"camera_LF.mp4": b"v" * 100}, nested=True)
    info = src / RECORDING_INFO_DIRNAME
    (info / TIMESTAMPS_FILENAME).write_bytes(b"npz")
    (info / CONFIG_SNAPSHOT_FILENAME).write_text("[record]\nfps = 80.0\n")
    (info / "40018631.pfs").write_text("basler")
    (info / "17475185.txt").write_text("flir")
    (info / "._17475185.txt").write_text("fork")  # hidden: not metadata
    dest = tmp_path / "dest" / "rec"

    result = _transfer(src, dest=dest)

    assert result
    meta = [
        RECORDING_SUMMARY_FILENAME,
        TIMESTAMPS_FILENAME,
        CONFIG_SNAPSHOT_FILENAME,
        "40018631.pfs",
        "17475185.txt",
    ]
    assert result.copied == ["camera_LF.mp4"] + [
        f"{RECORDING_INFO_DIRNAME}/{name}" for name in meta
    ]
    # The destination is laid out as the source: only the video at the top...
    assert sorted(p.name for p in dest.iterdir()) == [
        "camera_LF.mp4",
        RECORDING_INFO_DIRNAME,
    ]
    # ...and the camera files beside the snapshot, so it is a config dir.
    dest_info = dest / RECORDING_INFO_DIRNAME
    assert sorted(p.name for p in dest_info.iterdir()) == sorted(meta)
    assert (dest_info / "17475185.txt").read_text() == "flir"
    assert recording_info_dir(dest) == dest_info
    assert _no_temps(dest) and _no_temps(dest_info)


def test_nested_metadata_skips_and_recopies_by_content_on_a_rerun(tmp_path):
    src = _make_recording(tmp_path / "rec", {"camera_LF.mp4": b"A" * 100}, nested=True)
    snapshot = src / RECORDING_INFO_DIRNAME / CONFIG_SNAPSHOT_FILENAME
    snapshot.write_text("[record]\nfps = 80.0\n")
    dest = tmp_path / "dest" / "rec"
    _transfer(src, dest=dest)

    again = _transfer(src, dest=dest, dry_run=True)
    assert set(again.skipped) == {
        "camera_LF.mp4",
        SUMMARY_NESTED,
        f"{RECORDING_INFO_DIRNAME}/{CONFIG_SNAPSHOT_FILENAME}",
    }
    assert not again.copied

    snapshot.write_text("[record]\nfps = 90.0\n")  # same size, new content
    planned = _transfer(src, dest=dest, dry_run=True)
    result = _transfer(src, dest=dest)
    edited = f"{RECORDING_INFO_DIRNAME}/{CONFIG_SNAPSHOT_FILENAME}"
    assert planned.copied == [edited] and result.copied == [edited]
    assert (dest / edited).read_text() == "[record]\nfps = 90.0\n"


def test_nested_dry_run_plans_the_subfolder_and_touches_nothing(tmp_path):
    src = _make_recording(tmp_path / "rec", {"camera_LF.mp4": b"x" * 10}, nested=True)
    dest_root = tmp_path / "dest"
    result = _transfer(src, dest=dest_root / "rec", dry_run=True)
    assert result.copied == ["camera_LF.mp4", SUMMARY_NESTED]
    assert not dest_root.exists()


def test_flat_metadata_stays_flat_at_the_destination(tmp_path):
    # An archive made before the subfolder is copied as it is: no subfolder
    # appears at the destination, so every reader finds it there as it did here.
    src = _make_recording(tmp_path / "rec", {"camera_LF.mp4": b"v" * 10})
    (src / CONFIG_SNAPSHOT_FILENAME).write_text("[gui]\n")
    dest = tmp_path / "dest" / "rec"
    result = _transfer(src, dest=dest)
    assert result.copied == [
        "camera_LF.mp4",
        RECORDING_SUMMARY_FILENAME,
        CONFIG_SNAPSHOT_FILENAME,
    ]
    assert not (dest / RECORDING_INFO_DIRNAME).exists()
    assert recording_info_dir(dest) == dest


def test_a_newer_nested_take_carries_its_own_metadata_only(tmp_path):
    # Recorded into again: the older flat take's metadata stays at the source
    # (never deleted), but only the take the folder's readers see is carried,
    # so the destination never holds a second, stale config.
    src = _make_recording(tmp_path / "rec", {"camera_LF.mp4": b"v" * 10})
    (src / CONFIG_SNAPSHOT_FILENAME).write_text("# older take\n")
    (src / "40018631.pfs").write_text("older")
    _make_recording(src, {}, nested=True)
    (src / RECORDING_INFO_DIRNAME / CONFIG_SNAPSHOT_FILENAME).write_text("# newer\n")
    dest = tmp_path / "dest" / "rec"

    result = _transfer(src, dest=dest)

    assert result.copied == [
        "camera_LF.mp4",
        SUMMARY_NESTED,
        f"{RECORDING_INFO_DIRNAME}/{CONFIG_SNAPSHOT_FILENAME}",
    ]
    assert sorted(p.name for p in dest.iterdir()) == [
        "camera_LF.mp4",
        RECORDING_INFO_DIRNAME,
    ]
    assert (src / CONFIG_SNAPSHOT_FILENAME).read_text() == "# older take\n"


def test_an_unwritable_nested_destination_fails_the_metadata(tmp_path, monkeypatch):
    # A subfolder the destination refuses fails its files, not the whole run.
    src = _make_recording(tmp_path / "rec", {"camera_LF.mp4": b"v" * 10}, nested=True)
    dest = tmp_path / "dest" / "rec"
    real_mkdir = Path.mkdir

    def mkdir(self, *args, **kwargs):
        if self.name == RECORDING_INFO_DIRNAME:
            raise PermissionError(13, "Permission denied", str(self))
        return real_mkdir(self, *args, **kwargs)

    monkeypatch.setattr(Path, "mkdir", mkdir)
    result = _transfer(src, dest=dest)
    assert not result
    assert result.copied == ["camera_LF.mp4"]
    assert result.failed == [SUMMARY_NESTED]


# --- resume / skip ----------------------------------------------------------


def test_skip_on_rerun_size(tmp_path):
    src = _make_recording(tmp_path / "rec", {"camera_LF.mp4": b"data" * 500})
    dest_root = tmp_path / "dest"
    dest = dest_root / "rec"
    _transfer(src, dest=dest)
    mtimes = {p.name: p.stat().st_mtime_ns for p in dest.iterdir()}

    result = _transfer(src, dest=dest)
    assert set(result.skipped) == {"camera_LF.mp4", RECORDING_SUMMARY_FILENAME}
    assert not result.copied
    # Skipped files are not rewritten.
    for p in dest.iterdir():
        assert p.stat().st_mtime_ns == mtimes[p.name]


# --- integrity / atomicity under failure ------------------------------------


def test_verify_mismatch_fails_and_cleans_up(tmp_path, monkeypatch):
    src = _make_recording(tmp_path / "rec", {"camera_LF.mp4": b"data" * 100})
    dest_root = tmp_path / "dest"
    # Force every read-back digest to differ from the (real, inline) source digest.
    monkeypatch.setattr(transfer_mod, "_file_digest", lambda *a, **k: "deadbeef")

    dest = dest_root / "rec"
    result = _transfer(src, dest=dest, verify=True)
    assert "camera_LF.mp4" in result.failed
    assert not (dest / "camera_LF.mp4").exists()  # never promoted
    assert _no_temps(dest)  # temp cleaned
    assert not result  # falsy: a file failed


def test_atomic_interrupt_then_resume(tmp_path, monkeypatch):
    src = _make_recording(tmp_path / "rec", {"camera_LF.mp4": b"x" * 5000})
    dest_root = tmp_path / "dest"
    dest = dest_root / "rec"

    def boom(_a, _b):
        raise OSError("simulated interruption at rename")

    monkeypatch.setattr(transfer_mod.os, "replace", boom)
    result = _transfer(src, dest=dest)
    assert result.failed  # the rename never completed
    assert not (dest / "camera_LF.mp4").exists()  # no complete-looking partial
    assert _no_temps(dest)  # temp removed on the exception path

    monkeypatch.undo()  # "next run" with a working filesystem
    result2 = _transfer(src, dest=dest)
    assert result2
    assert (dest / "camera_LF.mp4").read_bytes() == b"x" * 5000


# --- options / edge cases ---------------------------------------------------


def test_copystat_failure_is_best_effort(tmp_path, monkeypatch):
    # Many network mounts reject utime; a verified copy must still be promoted.
    src = _make_recording(tmp_path / "rec", {"camera_LF.mp4": b"x" * 200})

    def boom(*_a, **_k):
        raise OSError("utime rejected by the share")

    monkeypatch.setattr(transfer_mod.shutil, "copystat", boom)
    result = _transfer(src, dest=tmp_path / "dest" / "rec")
    assert result and not result.failed
    assert (tmp_path / "dest" / "rec" / "camera_LF.mp4").read_bytes() == b"x" * 200


def test_sweep_only_reaps_old_temps(tmp_path):
    import os as _os
    import time as _time

    src = _make_recording(tmp_path / "rec", {"camera_LF.mp4": b"x" * 100})
    dest = tmp_path / "dest" / "rec"
    dest.mkdir(parents=True)
    fresh = dest / f".camera_LF.mp4{transfer_mod._TEMP_INFIX}.999.fresh"
    old = dest / f".camera_LF.mp4{transfer_mod._TEMP_INFIX}.999.old"
    fresh.write_bytes(b"a concurrent run's live temp")
    old.write_bytes(b"a crash orphan")
    old_t = _time.time() - transfer_mod._STALE_TEMP_AGE_S - 100
    _os.utime(old, (old_t, old_t))

    _transfer(src, dest=dest)
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
    dest_root = tmp_path / "dest"
    result = _transfer(src, dest=dest_root / "rec", verify=False)
    assert result
    assert (dest_root / "rec" / "camera_LF.mp4").read_bytes() == b"hello" * 100


def test_zero_byte_source(tmp_path):
    src = _make_recording(tmp_path / "rec", {"camera_LF.mp4": b""})
    dest_root = tmp_path / "dest"
    result = _transfer(src, dest=dest_root / "rec")
    target = dest_root / "rec" / "camera_LF.mp4"
    assert result
    assert target.exists() and target.stat().st_size == 0


def test_dry_run_touches_nothing(tmp_path):
    src = _make_recording(tmp_path / "rec", {"camera_LF.mp4": b"x" * 10})
    dest_root = tmp_path / "dest"
    result = _transfer(src, dest=dest_root / "rec", dry_run=True)
    assert result  # lists intended copies
    assert not dest_root.exists()


def test_dry_run_reports_already_transferred_as_skipped(tmp_path):
    # A dry run over an already-transferred recording must show the files as
    # skipped, not claim it would (re-)copy them.
    src = _make_recording(tmp_path / "rec", {"camera_LF.mp4": b"data" * 500})
    dest = tmp_path / "dest" / "rec"
    _transfer(src, dest=dest)  # first, real transfer

    result = _transfer(src, dest=dest, dry_run=True)
    assert set(result.skipped) == {"camera_LF.mp4", RECORDING_SUMMARY_FILENAME}
    assert not result.copied


def test_dry_run_plans_a_file_an_earlier_step_has_not_produced(tmp_path):
    # `octacam process --dry-run` passes along outputs its transcode step only
    # planned. There is no source to compare against a same-named file already
    # at the destination, so the copy is planned instead of crashing on the stat.
    src = _make_recording(tmp_path / "rec", {})
    dest = tmp_path / "dest" / "rec"
    dest.mkdir(parents=True)
    (dest / "camera_LF.mp4").write_bytes(b"older")
    planned = src / "camera_LF.mp4"

    result = transfer_folder(src, dest, [planned], dry_run=True)

    assert result.copied == ["camera_LF.mp4", RECORDING_SUMMARY_FILENAME]
    assert not planned.exists()
    assert (dest / "camera_LF.mp4").read_bytes() == b"older"


def test_nothing_to_copy_is_falsy(tmp_path):
    folder = tmp_path / "empty"
    folder.mkdir()
    result = _transfer(folder, dest=tmp_path / "dest" / "empty")
    assert isinstance(result, TransferResult)
    assert not result
    assert not result.copied and not result.failed


def test_progress_phases(tmp_path):
    src = _make_recording(tmp_path / "rec", {"camera_LF.mp4": b"z" * 4096})
    events = []
    _transfer(src, dest=tmp_path / "dest" / "rec", on_progress=events.append)
    phases = {e.phase for e in events}
    assert "copy" in phases and "verify" in phases

    events_off = []
    src2 = _make_recording(tmp_path / "rec2", {"camera_LF.mp4": b"z" * 4096})
    _transfer(
        src2,
        dest=tmp_path / "dest2" / "rec2",
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


def test_discovery_finds_nested_recordings_never_their_subfolder(tmp_path):
    from octacam.cli import _find_recording_dirs

    flat = _make_recording(tmp_path / "parent" / "001", {})
    nested = _make_recording(tmp_path / "parent" / "002", {}, nested=True)

    found = _find_recording_dirs([tmp_path / "parent"], True)
    assert sorted(p.resolve() for p in found) == [flat.resolve(), nested.resolve()]
    assert _find_recording_dirs([nested], recursive=False) == [nested]
    # Naming the subfolder means the recording around it.
    info = nested / RECORDING_INFO_DIRNAME
    assert _find_recording_dirs([info], recursive=False) == [nested]
