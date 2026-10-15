"""`octacam process`'s 2P steps end to end, on synthetic recordings and a fake
share: pairing, the sweep, renumbering, and both recording layouts.
"""

import json
import os
import time

import pytest
from typer.testing import CliRunner

from octacam import session_cache
from octacam.cli import app

runner = CliRunner()


def _make_sync_folder(root, experiment, name, mtime):
    folder = root / experiment / name
    folder.mkdir(parents=True)
    for fname in ("ThorRealTimeDataSettings.xml", "Episode001.h5"):
        p = folder / fname
        p.write_bytes(b"x")
        os.utime(p, (mtime, mtime))
    os.utime(folder, (mtime, mtime))
    return folder


def _make_image_folder(root, experiment, name, *, u_time, mtime):
    """A ThorImage folder with no frames: enough for pairing and filing."""
    folder = root / experiment / name
    folder.mkdir(parents=True)
    xml = folder / "Experiment.xml"
    xml.write_text(
        '<?xml version="1.0"?><ThorImageExperiment>'
        f'<Date date="x" uTime="{u_time}" /></ThorImageExperiment>'
    )
    os.utime(xml, (mtime, mtime))
    os.utime(folder, (mtime, mtime))
    return folder


def _twophoton_toml(dest_root, source_root, **options):
    lines = [
        "[transfer]",
        f'directory = "{dest_root.as_posix()}"',
        "[[plugins]]",
        'name = "twophoton"',
        "[plugins.options]",
        f'source = "{source_root.as_posix()}"',
        "match_window_s = 60",
        "settle_s = 60",
    ]
    lines += [f"{key} = {json.dumps(value)}" for key, value in options.items()]
    return "\n".join(lines) + "\n"


def _make_take(
    folder,
    *,
    toml,
    take_start,
    armed=True,
    nested=False,
    duration_s=10.0,
    frames=10,
):
    """A recording with one finished video: flat (the rig's first layout) or
    nested (develop's: metadata in octacam_recording/, the plugin's facts in
    twophoton.json there).
    """
    folder.mkdir(parents=True, exist_ok=True)
    info = folder / "octacam_recording" if nested else folder
    info.mkdir(exist_ok=True)
    summary = {
        "schema_version": 4,
        "fps_target": 100,
        "relative_directory": folder.name,
        "start_time_ns": int(take_start * 1e9),
        "duration_s": duration_s,
        "cameras": [{"name": "camera_LF", "file": "camera_LF.mkv", "frames": frames}],
    }
    if nested:
        (info / "twophoton.json").write_text(json.dumps({"armed": armed}))
    else:
        summary["plugins"] = {"twophoton": {"armed": armed}}
    (info / "recording_summary.json").write_text(json.dumps(summary))
    (info / "octacam_config.toml").write_text(toml)
    (folder / "camera_LF.mp4").write_bytes(b"finished-transcode")
    return folder


@pytest.fixture(autouse=True)
def _cache(tmp_path, monkeypatch):
    monkeypatch.setenv("OCTACAM_CACHE_DIR", str(tmp_path / "cache"))


@pytest.mark.parametrize("nested", [False, True])
def test_an_armed_take_gets_its_2p_data(tmp_path, nested):
    dest_root = tmp_path / "dest"
    source_root = tmp_path / "share"
    take_start = 1_000_000.0
    _make_sync_folder(source_root, "Exp", "SyncData102", mtime=take_start + 2)
    folder = _make_take(
        tmp_path / "rec",
        toml=_twophoton_toml(dest_root, source_root),
        take_start=take_start,
        nested=nested,
    )
    result = runner.invoke(app, ["process", "--no-transcode", "--no-grid", str(folder)])
    assert result.exit_code == 0, result.output
    dest = dest_root / "rec"
    assert (dest / "2P" / "SyncData102" / "Episode001.h5").is_file()
    info = dest / "octacam_recording" if nested else dest
    record = json.loads((info / "twophoton_match.json").read_text())
    assert record["matched"][0]["path"] == "Exp/SyncData102"


def test_an_unarmed_take_is_not_paired(tmp_path):
    dest_root = tmp_path / "dest"
    source_root = tmp_path / "share"
    take_start = 1_000_000.0
    _make_sync_folder(source_root, "Exp", "SyncData102", mtime=take_start + 2)
    folder = _make_take(
        tmp_path / "rec",
        toml=_twophoton_toml(dest_root, source_root, attribute_unclaimed_to_fly=False),
        take_start=take_start,
        armed=False,
        nested=True,
    )
    result = runner.invoke(app, ["process", "--no-transcode", "--no-grid", str(folder)])
    assert result.exit_code == 0, result.output
    assert not (dest_root / "rec" / "2P").exists()
    # The sweep files the unclaimed folder in the generic bucket instead.
    assert list((dest_root / "2p_only" / "Exp").glob("*/SyncData102"))


def test_a_take_without_2p_data_yet_is_paired_by_a_later_run(tmp_path):
    dest_root = tmp_path / "dest"
    source_root = tmp_path / "share"
    (source_root / "Exp").mkdir(parents=True)
    take_start = 1_000_000.0
    folder = _make_take(
        tmp_path / "rec",
        toml=_twophoton_toml(dest_root, source_root),
        take_start=take_start,
        nested=True,
    )
    session_cache.record_recording(folder, "session")  # as a recording is
    result = runner.invoke(app, ["process", "--no-transcode", "--no-grid", str(folder)])
    assert result.exit_code == 0, result.output
    assert (folder / "octacam_recording" / "twophoton_pending.json").is_file()

    # The ThorSync folder appears (and settles) later: the next run's sweep,
    # on any recording, pairs it with the waiting take.
    _make_sync_folder(source_root, "Exp", "SyncData102", mtime=take_start + 2)
    result = runner.invoke(app, ["twophoton", "sweep", "--config", str(folder)])
    assert result.exit_code == 0, result.output
    assert (dest_root / "rec" / "2P" / "SyncData102" / "Episode001.h5").is_file()
    assert not (folder / "octacam_recording" / "twophoton_pending.json").exists()


def test_takes_are_renumbered_and_the_grids_follow(tmp_path, monkeypatch):
    dest_root = tmp_path / "dest"
    source_root = tmp_path / "share"
    take_start = 1_000_000.0
    _make_sync_folder(source_root, "Exp", "SyncData102", mtime=take_start + 2)
    toml = _twophoton_toml(dest_root, source_root)
    toml += '[[visualization]]\nname = "grid.mp4"\nlayout = [["camera_LF"]]\n'
    day = tmp_path / "261014_Exp" / "Fly1"
    first = _make_take(day / "001", toml=toml, take_start=take_start, nested=True)
    second = _make_take(
        day / "002", toml=toml, take_start=take_start + 600, armed=False, nested=True
    )
    for take, rel in ((first, "261014_Exp/Fly1/001"), (second, "261014_Exp/Fly1/002")):
        summary = take / "octacam_recording" / "recording_summary.json"
        data = json.loads(summary.read_text())
        data["relative_directory"] = rel
        summary.write_text(json.dumps(data))

    def build_grid(source, *args, output, **kwargs):
        output.write_bytes(b"grid")
        return output

    monkeypatch.setattr("octacam.process.build_grid_video", build_grid)
    result = runner.invoke(app, ["process", "--no-transcode", str(first), str(second)])
    assert result.exit_code == 0, result.output
    fly = dest_root / "261014_Exp" / "Fly1"
    assert sorted(p.name for p in fly.iterdir() if p.is_dir()) == [
        "Recording1_Synced",
        "Recording2_Beh",
    ]
    assert (fly / "Recording1_Synced" / "grid.mp4").is_file()
    assert (fly / "Recording1_Synced" / "2P" / "SyncData102").is_dir()
    assert (fly / "Recording2_Beh" / "grid.mp4").is_file()
    assert "Recording1_Synced" in (fly / "reconciliation_log.md").read_text()

    # A re-run copies into the renamed folders, never re-creating 001/002.
    result = runner.invoke(app, ["process", "--no-transcode", str(first), str(second)])
    assert result.exit_code == 0, result.output
    assert sorted(p.name for p in fly.iterdir() if p.is_dir()) == [
        "Recording1_Synced",
        "Recording2_Beh",
    ]


def test_a_fly_s_unclaimed_zstack_is_filed_under_it(tmp_path):
    dest_root = tmp_path / "dest"
    source_root = tmp_path / "share"
    take_start = 1_000_000_000.0  # a real-looking day for the same-day check
    _make_image_folder(
        source_root,
        "Exp",
        "Fly1_001",
        u_time=int(take_start) + 1,
        mtime=take_start + 30,
    )
    _make_image_folder(
        source_root,
        "Exp",
        "Fly1_Zstack",
        u_time=int(take_start) - 600,
        mtime=take_start - 500,
    )
    day = time_day(take_start)
    folder = _make_take(
        tmp_path / day / "Fly1" / "001",
        toml=_twophoton_toml(dest_root, source_root, reconcile_recordings=False),
        take_start=take_start,
        nested=True,
    )
    summary = folder / "octacam_recording" / "recording_summary.json"
    data = json.loads(summary.read_text())
    data["relative_directory"] = f"{day}/Fly1/001"
    summary.write_text(json.dumps(data))
    result = runner.invoke(app, ["process", "--no-transcode", "--no-grid", str(folder)])
    assert result.exit_code == 0, result.output
    fly = dest_root / day / "Fly1"
    assert (fly / "001" / "2P" / "Fly1_001").is_dir()
    assert (fly / "2P_only" / "Fly1_Zstack").is_dir()
    assert (fly / "2P_only" / "Fly1_Zstack" / ".octacam_2p_source.json").is_file()


def time_day(epoch: float) -> str:
    return time.strftime("%y%m%d", time.localtime(epoch)) + "_Exp"


def test_doctor_checks_the_2p_share(tmp_path):
    config_dir = tmp_path / "rig"
    config_dir.mkdir()
    (config_dir / "octacam_config.toml").write_text(
        _twophoton_toml(tmp_path / "dest", tmp_path / "missing-share")
    )
    result = runner.invoke(app, ["doctor", str(config_dir), "--json"])
    assert "the 2P share" in result.output
    assert "not reachable" in result.output


def test_the_twophoton_commands_are_mounted():
    result = runner.invoke(app, ["twophoton", "--help"])
    assert result.exit_code == 0, result.output
    for name in ("verify", "sweep", "manifest", "reconcile", "reassemble", "render"):
        assert name in result.output


def test_a_legacy_transfer_twophoton_table_is_still_read(tmp_path):
    # A rig recording made before the move keeps [transfer.twophoton].
    dest_root = tmp_path / "dest"
    source_root = tmp_path / "share"
    take_start = 1_000_000.0
    _make_sync_folder(source_root, "Exp", "SyncData102", mtime=take_start + 2)
    toml = (
        f'[transfer]\ndirectory = "{dest_root.as_posix()}"\n'
        '[[plugins]]\nname = "twophoton"\n'
        f'[transfer.twophoton]\nsource = "{source_root.as_posix()}"\n'
        "match_window_s = 60\nsettle_s = 60\n"
    )
    folder = _make_take(tmp_path / "rec", toml=toml, take_start=take_start)
    result = runner.invoke(app, ["process", "--no-transcode", "--no-grid", str(folder)])
    assert result.exit_code == 0, result.output
    assert (dest_root / "rec" / "2P" / "SyncData102").is_dir()
