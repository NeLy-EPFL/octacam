"""The recording folder layout: metadata in the ``octacam_recording`` subfolder.

A recording folder shows only its videos; the summary, the timestamps, the
config snapshot and the camera parameter files sit in its
``octacam_recording`` subfolder. Recordings made before that keep them flat
beside the videos, and every reader must accept both. These pin the shared
helpers in :mod:`octacam.transform` and :func:`octacam.config.resolve_config_dir`
that every reader goes through.
"""

from pathlib import Path

from octacam.config import find_config_file, resolve_config_dir
from octacam.transform import (
    CONFIG_SNAPSHOT_FILENAME,
    RECORDING_INFO_DIRNAME,
    RECORDING_SUMMARY_FILENAME,
    find_recording_dirs,
    is_recording_dir,
    recording_folder_of,
    recording_info_dir,
    recording_summary_path,
)


def _flat(folder: Path, *, config: bool = False) -> Path:
    """A recording made before the subfolder: metadata beside the videos."""
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "camera_0.mp4").write_bytes(b"v")
    (folder / RECORDING_SUMMARY_FILENAME).write_text('{"take": "flat"}')
    if config:
        (folder / CONFIG_SNAPSHOT_FILENAME).write_text("# flat take\n")
    return folder


def _nested(folder: Path, *, config: bool = False, summary: bool = True) -> Path:
    """A recording in the current layout: metadata in the subfolder."""
    info = folder / RECORDING_INFO_DIRNAME
    info.mkdir(parents=True, exist_ok=True)
    (folder / "camera_0.mp4").write_bytes(b"v")
    if summary:
        (info / RECORDING_SUMMARY_FILENAME).write_text('{"take": "nested"}')
    if config:
        (info / CONFIG_SNAPSHOT_FILENAME).write_text("# nested take\n")
    return folder


# ------------------------------------------------------- recording_info_dir


def test_info_dir_is_the_subfolder_when_it_has_the_summary(tmp_path):
    rec = _nested(tmp_path / "rec")
    assert recording_info_dir(rec) == rec / RECORDING_INFO_DIRNAME
    assert recording_summary_path(rec) == (
        rec / RECORDING_INFO_DIRNAME / RECORDING_SUMMARY_FILENAME
    )


def test_info_dir_is_the_folder_for_a_flat_recording(tmp_path):
    rec = _flat(tmp_path / "rec")
    assert recording_info_dir(rec) == rec
    assert recording_summary_path(rec) == rec / RECORDING_SUMMARY_FILENAME


def test_the_newer_nested_take_wins_over_an_older_flat_one(tmp_path):
    # A folder recorded into again keeps the older flat take's files; the
    # subfolder's summary is the newer take's.
    rec = _nested(_flat(tmp_path / "rec"))
    assert recording_info_dir(rec) == rec / RECORDING_INFO_DIRNAME
    assert '"nested"' in recording_summary_path(rec).read_text()


def test_info_dir_without_any_summary(tmp_path):
    # A take killed before its summary: the subfolder if it exists...
    killed = _nested(tmp_path / "killed", summary=False)
    assert recording_info_dir(killed) == killed / RECORDING_INFO_DIRNAME
    # ...else the folder itself (also for a path that does not exist).
    empty = tmp_path / "empty"
    empty.mkdir()
    assert recording_info_dir(empty) == empty
    assert recording_info_dir(tmp_path / "missing") == tmp_path / "missing"
    assert recording_info_dir(str(empty)) == empty  # a str is accepted


# ------------------------------------------------ is_recording_dir / folder_of


def test_is_recording_dir_in_either_layout(tmp_path):
    flat = _flat(tmp_path / "flat")
    nested = _nested(tmp_path / "nested")
    plain = tmp_path / "plain"
    plain.mkdir()
    assert is_recording_dir(flat)
    assert is_recording_dir(nested)
    assert not is_recording_dir(plain)
    assert not is_recording_dir(tmp_path / "missing")
    assert not is_recording_dir(_nested(tmp_path / "killed", summary=False))


def test_the_subfolder_itself_is_not_a_recording(tmp_path):
    rec = _nested(tmp_path / "rec")
    assert not is_recording_dir(rec / RECORDING_INFO_DIRNAME)


def test_recording_folder_of_maps_a_summary_back(tmp_path):
    flat = _flat(tmp_path / "flat")
    nested = _nested(tmp_path / "nested")
    assert recording_folder_of(flat / RECORDING_SUMMARY_FILENAME) == flat
    assert (
        recording_folder_of(nested / RECORDING_INFO_DIRNAME / RECORDING_SUMMARY_FILENAME)
        == nested
    )
    # Pure path arithmetic: the file need not exist.
    assert recording_folder_of("/a/b/octacam_recording/x.json") == Path("/a/b")


# ------------------------------------------------------ find_recording_dirs


def test_find_recording_dirs_on_a_mixed_tree(tmp_path):
    _flat(tmp_path / "exp" / "Fly1" / "001")
    _nested(tmp_path / "exp" / "Fly1" / "002")
    _nested(_flat(tmp_path / "exp" / "Fly2" / "001"))  # both layouts: listed once
    _nested(tmp_path / "exp" / "Fly2" / "002", summary=False)  # no summary
    (tmp_path / "exp" / "notes").mkdir()

    found = [p.relative_to(tmp_path).as_posix() for p in find_recording_dirs(tmp_path)]

    assert found == ["exp/Fly1/001", "exp/Fly1/002", "exp/Fly2/001"]
    assert not any(p.name == RECORDING_INFO_DIRNAME for p in find_recording_dirs(tmp_path))


def test_find_recording_dirs_at_a_recording_and_off_the_tree(tmp_path):
    rec = _nested(tmp_path / "rec")
    assert find_recording_dirs(rec) == [rec]
    assert find_recording_dirs(tmp_path / "missing") == []
    summary = rec / RECORDING_INFO_DIRNAME / RECORDING_SUMMARY_FILENAME
    assert find_recording_dirs(summary) == []  # a file is not a tree


# ------------------------------------------------------- resolve_config_dir


def test_resolve_config_dir_relaunches_a_nested_recording(tmp_path):
    rec = _nested(tmp_path / "rec", config=True)
    assert resolve_config_dir(rec) == rec / RECORDING_INFO_DIRNAME
    assert resolve_config_dir(str(rec)) == rec / RECORDING_INFO_DIRNAME
    assert find_config_file(resolve_config_dir(rec)).read_text() == "# nested take\n"


def test_resolve_config_dir_keeps_a_flat_recording_and_a_plain_dir(tmp_path):
    flat = _flat(tmp_path / "flat", config=True)
    assert resolve_config_dir(flat) == flat
    rig = tmp_path / "rig"
    rig.mkdir()
    (rig / CONFIG_SNAPSHOT_FILENAME).write_text("# rig\n")
    assert resolve_config_dir(rig) == rig
    # No config anywhere: unchanged, so the caller reports the missing file.
    empty = tmp_path / "empty"
    empty.mkdir()
    assert resolve_config_dir(empty) == empty
    # The subfolder named directly is a config dir of its own.
    nested = _nested(tmp_path / "nested", config=True) / RECORDING_INFO_DIRNAME
    assert resolve_config_dir(nested) == nested


def test_resolve_config_dir_follows_the_newer_take(tmp_path):
    # Recorded into again: the flat config is the older take's snapshot, and
    # the relaunch must reproduce the newer take, as every other reader does.
    rec = _nested(_flat(tmp_path / "rec", config=True), config=True)
    assert resolve_config_dir(rec) == rec / RECORDING_INFO_DIRNAME


def test_resolve_config_dir_keeps_a_rig_config_that_was_recorded_into(tmp_path):
    # A rig config dir used as the save directory gains a recording subfolder,
    # but its own config is the rig's (no flat summary beside it), not a take's.
    rig = _nested(tmp_path / "rig", config=True)
    (rig / CONFIG_SNAPSHOT_FILENAME).write_text("# rig\n")
    assert resolve_config_dir(rig) == rig
