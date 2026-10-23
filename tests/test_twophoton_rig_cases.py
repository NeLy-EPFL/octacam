"""The rig branch's 2P processing regression tests, ported onto the twophoton
plugin's process steps and `octacam twophoton ...` commands.

Each case pins down a bug found on real data on the rig (the comments say
which); only the paths, commands and APIs are adapted: the 2P options are the
plugin's (`[plugins.options]`), the standalone modes are `octacam twophoton
sweep|manifest|reconcile|verify|reassemble`, and a take's videos and grid sit
flat at its destination (no `Behavior/`, `Renderings/`).
"""

import json
import logging
import os
import shutil
import time
from pathlib import Path

import pytest
from helpers import plugin_doctor_text
from typer.testing import CliRunner

from octacam import session_cache
from octacam.cli import app

runner = CliRunner()


@pytest.fixture(autouse=True)
def _cache(tmp_path, monkeypatch):
    monkeypatch.setenv("OCTACAM_CACHE_DIR", str(tmp_path / "cache"))


def _out(result) -> str:
    """The run's output (logs included) with rich's line wrapping undone."""
    return " ".join(result.output.split())


# --- fixtures ---------------------------------------------------------------


def _toml(dest_root, source_root=None, *, transfer=None, visualization=False, **opts):
    """A config snapshot: `[transfer]` at *dest_root* and, with *source_root*,
    the twophoton plugin with that 2P `source` and *opts* as its options.
    """
    lines = ["[transfer]", f'directory = "{dest_root.as_posix()}"']
    lines += [f"{key} = {json.dumps(value)}" for key, value in (transfer or {}).items()]
    if source_root is not None:
        lines += [
            "[[plugins]]",
            'name = "twophoton"',
            "[plugins.options]",
            f'source = "{source_root.as_posix()}"',
        ]
        lines += [f"{key} = {json.dumps(value)}" for key, value in opts.items()]
    if visualization:
        lines += [
            "[[visualization]]",
            'name = "grid.mp4"',
            'layout = [["camera_LF"]]',
        ]
    return "\n".join(lines) + "\n"


def _armed_toml(dest_root, source_root, **opts):
    return _toml(
        dest_root, source_root, **{"match_window_s": 60, "settle_s": 60, **opts}
    )


def _rig_config(tmp_path, dest_root, source_root=None, **opts):
    """A rig config directory for the `octacam twophoton ...` commands (they
    all need the plugin's `source`; reconcile never reads it).
    """
    config_dir = tmp_path / "rig"
    config_dir.mkdir()
    source_root = tmp_path / "unused-share" if source_root is None else source_root
    (config_dir / "octacam_config.toml").write_text(
        _toml(dest_root, source_root, **opts)
    )
    return config_dir


def _make_recording(folder, *, with_outputs, toml="", summary=None):
    """A flat recording folder (the rig's layout) with one camera's source
    .mkv, its summary and *toml* as its config snapshot; *with_outputs* drops a
    finished camera_LF.mp4 and grid.mp4 too. *summary* is merged into it.
    """
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "camera_LF.mkv").write_bytes(b"source-bytes")
    data = {
        "fps_target": 100,
        "relative_directory": folder.name,
        "cameras": [
            {
                "name": "camera_LF",
                "file": "camera_LF.mkv",
                "width": 64,
                "height": 48,
                "fps": 100,
                "frames": 10,
            }
        ],
    }
    data.update(summary or {})
    (folder / "recording_summary.json").write_text(json.dumps(data))
    if toml:
        (folder / "octacam_config.toml").write_text(toml)
    if with_outputs:
        (folder / "camera_LF.mp4").write_bytes(b"finished-transcode")
        (folder / "grid.mp4").write_bytes(b"finished-grid")
    return folder


def _armed_recording(
    folder, *, source_root, dest_root, take_start, duration_s=10.0, summary=None, **opts
):
    return _make_recording(
        folder,
        with_outputs=True,
        toml=_armed_toml(dest_root, source_root, **opts),
        summary={
            "start_time_ns": int(take_start * 1e9),
            "duration_s": duration_s,
            "plugins": {"twophoton": {"armed": True}},
            **(summary or {}),
        },
    )


def _make_sync_folder_2p(root, experiment, name, mtime):
    folder = root / experiment / name
    folder.mkdir(parents=True)
    for fname in ("ThorRealTimeDataSettings.xml", "Episode001.h5"):
        p = folder / fname
        p.write_bytes(b"x")
        os.utime(p, (mtime, mtime))
    # A ThorSync folder's start is its directory's mtime: creating the files
    # bumped it to now.
    os.utime(folder, (mtime, mtime))
    return folder


def _make_bare_image_folder_2p(root, experiment, name, *, u_time, mtime):
    """An "image" 2P folder with only its Experiment.xml (no frames)."""
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


def _make_verifiable_sync_folder_2p(
    root, experiment, name, *, mtime, camera_frames, frameout_edges=0
):
    """A real (synthetic) Episode001.h5 whose Cameras edge count matches
    *camera_frames* exactly, inside one CaptureOn window spanning the file;
    *frameout_edges* seeds the FrameOut channel too.
    """
    h5py = pytest.importorskip("h5py")
    import numpy as np

    folder = root / experiment / name
    folder.mkdir(parents=True)
    total = max(camera_frames, frameout_edges) * 2 + 100
    capture_on = np.zeros((total, 1), dtype=np.uint32)
    capture_on[10 : total - 10] = 1
    cameras = np.zeros((total, 1), dtype=np.uint32)
    for i in range(camera_frames):
        cameras[20 + i * 2] = 1
    frameout = np.zeros((total, 1), dtype=np.uint32)
    for i in range(frameout_edges):
        frameout[20 + i * 2] = 1
    episode_path = folder / "Episode001.h5"
    with h5py.File(episode_path, "w") as f:
        di = f.create_group("DI")
        di.create_dataset("CaptureOn", data=capture_on)
        di.create_dataset("Cameras", data=cameras)
        di.create_dataset("FrameOut", data=frameout)
    os.utime(episode_path, (mtime, mtime))
    os.utime(folder, (mtime, mtime))
    return folder


def _make_image_folder_2p(root, experiment, name, *, u_time, n_frames, channel="ChanA"):
    """A ThorImage-shaped folder: Experiment.xml plus *n_frames* real
    per-frame TIFFs (frame i filled with i), all dated *u_time*.
    """
    tifffile = pytest.importorskip("tifffile")
    import numpy as np

    folder = root / experiment / name
    folder.mkdir(parents=True)
    (folder / "Experiment.xml").write_text(
        f"""<?xml version="1.0"?>
<ThorImageExperiment>
  <Date date="" uTime="{u_time}" />
  <Wavelengths>
    <Wavelength name="{channel}" exposureTimeMS="0" />
  </Wavelengths>
  <ZStage name="ThorZPiezo" steps="1" enable="0" />
  <Timelapse timepoints="{n_frames}" intervalSec="0" triggerMode="0" />
  <LSM pixelSizeUM="0.089" />
</ThorImageExperiment>
"""
    )
    for i in range(1, n_frames + 1):
        tifffile.imwrite(
            folder / f"{channel}_001_001_001_{i:03d}.tif",
            np.full((16, 16), i, dtype=np.uint16),
        )
    for p in folder.iterdir():
        os.utime(p, (u_time, u_time))
    os.utime(folder, (u_time, u_time))
    return folder


def _make_image_folder_2p_flat(folder, *, n_frames, channel="ChanA"):
    """Like _make_image_folder_2p, written straight into *folder* (already
    on the destination).
    """
    tifffile = pytest.importorskip("tifffile")
    import numpy as np

    folder.mkdir(parents=True)
    (folder / "Experiment.xml").write_text(
        f"""<?xml version="1.0"?>
<ThorImageExperiment>
  <Wavelengths>
    <Wavelength name="{channel}" exposureTimeMS="0" />
  </Wavelengths>
  <ZStage name="ThorZPiezo" steps="1" enable="0" />
  <Timelapse timepoints="{n_frames}" intervalSec="0" triggerMode="0" />
  <LSM pixelSizeUM="0.089" />
</ThorImageExperiment>
"""
    )
    for i in range(1, n_frames + 1):
        tifffile.imwrite(
            folder / f"{channel}_001_001_001_{i:03d}.tif",
            np.full((16, 16), i, dtype=np.uint16),
        )
    return folder


def _make_fastz_folder_2p(
    root, experiment, name, *, u_time, zsteps, timepoints, channel="ChanA"
):
    """A FastZ ThorImage folder as real data has it: ZStage enable="0", Z
    motion signalled by Streaming's zFastEnable="1", one file per (plane,
    timepoint).
    """
    tifffile = pytest.importorskip("tifffile")
    import numpy as np

    folder = root / experiment / name
    folder.mkdir(parents=True)
    (folder / "Experiment.xml").write_text(
        f"""<?xml version="1.0"?>
<ThorImageExperiment>
  <Date date="" uTime="{u_time}" />
  <Wavelengths>
    <Wavelength name="{channel}" exposureTimeMS="0" />
  </Wavelengths>
  <ZStage name="ThorZPiezo" steps="{zsteps}" enable="0" />
  <Streaming enable="1" frames="0" zFastEnable="1" />
  <Timelapse timepoints="{timepoints}" intervalSec="0" triggerMode="0" />
  <LSM pixelSizeUM="0.089" />
</ThorImageExperiment>
"""
    )
    for z in range(1, zsteps + 1):
        for t in range(1, timepoints + 1):
            tifffile.imwrite(
                folder / f"{channel}_001_001_{z:03d}_{t:03d}.tif",
                np.full((16, 16), z * 1000 + t, dtype=np.uint16),
            )
    for p in folder.iterdir():
        os.utime(p, (u_time, u_time))
    os.utime(folder, (u_time, u_time))
    return folder


def _write_take(dest_root, rel, *, start_time_ns, duration_s, matched_paths=None):
    """A take already on the destination: its summary, and its pairing record
    when *matched_paths* (ThorImage folders) is given.
    """
    folder = dest_root / rel
    folder.mkdir(parents=True)
    (folder / "recording_summary.json").write_text(
        json.dumps({"start_time_ns": start_time_ns, "duration_s": duration_s})
    )
    if matched_paths is not None:
        (folder / "twophoton_match.json").write_text(
            json.dumps(
                {"matched": [{"path": p, "kind": "image"} for p in matched_paths]}
            )
        )
    return folder


def _process(*args):
    return runner.invoke(
        app, ["process", *map(str, args), "--no-transcode", "--no-grid"]
    )


# --- pairing a take with its 2P data ---------------------------------------


def test_process_transfers_matched_twophoton_data(tmp_path):
    dest_root = tmp_path / "dest"
    source_root = tmp_path / "windows_share" / "MD"
    take_start = 1_000_000.0
    # 30 s before the take (within match_window_s=60), and long settled.
    _make_sync_folder_2p(
        source_root, "MB247_CI63", "SyncData102", mtime=take_start - 30
    )
    folder = _armed_recording(
        tmp_path / "rec",
        source_root=source_root,
        dest_root=dest_root,
        take_start=take_start,
    )
    result = _process(folder)
    assert result.exit_code == 0, result.output
    dest_2p = dest_root / folder.name / "2P" / "SyncData102"
    assert (dest_2p / "Episode001.h5").exists()
    assert (dest_2p / "ThorRealTimeDataSettings.xml").exists()
    record = json.loads((dest_root / folder.name / "twophoton_match.json").read_text())
    assert record["matched"][0]["path"] == "MB247_CI63/SyncData102"
    # Fake bytes, not HDF5: verification falls back to the timestamp tier.
    assert record["matched"][0]["confidence"] == "timestamp"


def test_process_verifies_twophoton_match_via_real_signal(tmp_path):
    dest_root = tmp_path / "dest"
    source_root = tmp_path / "windows_share" / "MD"
    take_start = 1_000_000.0
    # Far outside match_window_s (within verify_window_s): the edge-count
    # match must still win over the timestamp heuristic.
    _make_verifiable_sync_folder_2p(
        source_root,
        "MB247_CI63",
        "SyncData102",
        mtime=take_start + 500,
        camera_frames=42,
    )
    folder = _armed_recording(
        tmp_path / "rec",
        source_root=source_root,
        dest_root=dest_root,
        take_start=take_start,
        summary={"cameras": [{"name": "camera_LF", "frames": 42}]},
    )
    result = _process(folder)
    assert result.exit_code == 0, result.output
    assert (dest_root / folder.name / "2P" / "SyncData102" / "Episode001.h5").exists()
    record = json.loads((dest_root / folder.name / "twophoton_match.json").read_text())
    assert record["matched"][0]["confidence"] == "verified"


def test_process_skips_twophoton_when_explicitly_not_armed(tmp_path):
    # Schema 4 with the checkbox recorded unchecked skips outright, unlike a
    # pre-feature recording that never had the chance to say "armed".
    dest_root = tmp_path / "dest"
    source_root = tmp_path / "windows_share" / "MD"
    take_start = 1_000_000.0
    _make_sync_folder_2p(
        source_root, "MB247_CI63", "SyncData102", mtime=take_start - 30
    )
    folder = _make_recording(
        tmp_path / "rec",
        with_outputs=True,
        toml=_toml(dest_root, source_root, settle_s=60),
        summary={
            "schema_version": 4,
            "start_time_ns": int(take_start * 1e9),
            "duration_s": 10.0,
            "plugins": {"twophoton": {"armed": False}},
        },
    )
    result = _process(folder)
    assert result.exit_code == 0, result.output
    assert not (dest_root / folder.name / "2P").exists()


def test_process_legacy_fallback_matches_predates_feature_recording(tmp_path):
    # No "plugins" key and no schema_version: every recording made before the
    # feature (real production data). It must still be paired.
    dest_root = tmp_path / "dest"
    source_root = tmp_path / "windows_share" / "MD"
    take_start = 1_000_000.0
    _make_sync_folder_2p(source_root, "MB247_CI63", "SyncData102", mtime=take_start - 2)
    folder = _make_recording(
        tmp_path / "rec",
        with_outputs=True,
        toml=_toml(dest_root, source_root, settle_s=60),
        summary={"start_time_ns": int(take_start * 1e9), "duration_s": 10.0},
    )
    result = _process(folder)
    assert result.exit_code == 0, result.output
    assert (dest_root / folder.name / "2P" / "SyncData102" / "Episode001.h5").exists()


def test_process_legacy_fallback_requires_twophoton_config(tmp_path):
    # A rig that never uses 2P must not start scanning a share.
    dest_root = tmp_path / "dest"
    folder = _make_recording(
        tmp_path / "rec",
        with_outputs=True,
        toml=_toml(dest_root),
        summary={"start_time_ns": 1_000_000_000_000_000, "duration_s": 10.0},
    )
    result = _process(folder)
    assert result.exit_code == 0, result.output
    assert not (dest_root / folder.name / "2P").exists()


def test_process_leaves_unsettled_twophoton_folder_for_next_run(tmp_path):
    dest_root = tmp_path / "dest"
    source_root = tmp_path / "windows_share" / "MD"
    take_start = time.time() - 5  # still fresh
    _make_sync_folder_2p(source_root, "MB247_CI63", "SyncData102", mtime=take_start)
    folder = _armed_recording(
        tmp_path / "rec",
        source_root=source_root,
        dest_root=dest_root,
        take_start=take_start,
        settle_s=3600,
    )
    result = _process(folder)
    assert result.exit_code == 0, result.output
    assert not (dest_root / folder.name / "2P").exists()
    # The take is done but not paired yet: a later sweep retries it.
    assert (folder / "twophoton_pending.json").exists()


def test_process_retroactively_pairs_pending_take_via_later_sweep(tmp_path):
    # TWOPHOTON_MATCHING_BUG_REPORT.md: a take finalized before its 2P data
    # appeared was never revisited. The standalone sweep, in a separate run
    # that does not know the take's folder, must pair it once the data
    # settles, not file the data as 2P-only.
    dest_root = tmp_path / "dest"
    source_root = tmp_path / "windows_share" / "MD"
    take_start = 1_000_000.0
    folder = _armed_recording(
        tmp_path / "rec",
        source_root=source_root,
        dest_root=dest_root,
        take_start=take_start,
    )
    session_cache.record_recording(folder, session_id="s1")

    result = _process(folder)
    assert result.exit_code == 0, result.output
    assert not (dest_root / folder.name / "2P").exists()
    assert (folder / "twophoton_pending.json").exists()
    assert not (folder / "twophoton_match.json").exists()

    _make_sync_folder_2p(
        source_root, "MB247_CI63", "SyncData102", mtime=take_start - 30
    )
    config_dir = _rig_config(
        tmp_path, dest_root, source_root, match_window_s=60, settle_s=60
    )
    result = runner.invoke(app, ["twophoton", "sweep", "--config", str(config_dir)])
    assert result.exit_code == 0, result.output
    assert (dest_root / folder.name / "2P" / "SyncData102" / "Episode001.h5").exists()
    record = json.loads((dest_root / folder.name / "twophoton_match.json").read_text())
    assert record["matched"][0]["path"] == "MB247_CI63/SyncData102"
    assert not (folder / "twophoton_pending.json").exists()
    assert not (dest_root / "2p_only").exists()


def test_twophoton_pairs_on_first_frame_not_arm_time(tmp_path):
    # Real 2026-10-14 Fly1/002: armed 23 s before ThorImage started, so
    # pairing on the arm time missed the 5 s window.
    dest_root = tmp_path / "dest"
    source_root = tmp_path / "windows_share" / "MD"
    arm = 1_000_000.0
    _make_sync_folder_2p(source_root, "PAM7xCI80", "SyncData167", mtime=arm + 23)
    folder = _armed_recording(
        tmp_path / "rec",
        source_root=source_root,
        dest_root=dest_root,
        take_start=arm,
        match_window_s=5,
        summary={"first_frame_time_ns": int((arm + 23.5) * 1e9)},
    )
    result = _process(folder)
    assert result.exit_code == 0, result.output
    assert (dest_root / folder.name / "2P" / "SyncData167" / "Episode001.h5").exists()


def test_take_match_start_estimates_first_frame_for_older_synced_takes(tmp_path):
    # Before first_frame_time_ns: a take that stopped with ThorImage has its
    # summary written right after the stop, so its write time minus the
    # board's run length is the first frame (real: 1.4-3.9 s off).
    from octacam.plugins.twophoton.takes import match_start_s

    folder = tmp_path / "take"
    folder.mkdir()
    path = folder / "recording_summary.json"
    summary = {
        "start_time_ns": int(1000e9),
        "plugins": {"twophoton": {"stop": {"reason": "thorimage", "run_ms": 394_000}}},
    }
    path.write_text(json.dumps(summary))
    os.utime(path, (1000 + 23 + 394 + 3, 1000 + 23 + 394 + 3))
    assert match_start_s(folder, summary) == pytest.approx(1026.0)
    exact = {**summary, "first_frame_time_ns": int(1023.4e9)}
    assert match_start_s(folder, exact) == pytest.approx(1023.4)
    assert match_start_s(folder, {"start_time_ns": int(1000e9)}) == pytest.approx(
        1000.0
    )
    os.utime(path, (1000, 1000))  # never earlier than the arm time
    assert match_start_s(folder, summary) == pytest.approx(1000.0)


def test_a_take_still_waiting_for_its_2p_data_keeps_its_local_folder(tmp_path):
    # Tommy's review: the delete after transfer removed the local folder and
    # its pending note with it, so the take was never paired. #24 keeps a
    # folder with a file the destination does not hold.
    pytest.importorskip("tifffile")
    dest_root = tmp_path / "dest"
    source_root = tmp_path / "windows_share" / "MD"
    take_start = 1_000_000.0
    _make_sync_folder_2p(source_root, "MB247_CI63", "SyncData102", mtime=take_start + 1)
    image = _make_image_folder_2p(
        source_root, "MB247_CI63", "Fly1", u_time=take_start + 1, n_frames=4
    )
    now = time.time()
    for p in image.iterdir():
        os.utime(p, (now, now))  # ThorImage still writing
    folder = _armed_recording(
        tmp_path / "rec",
        source_root=source_root,
        dest_root=dest_root,
        take_start=take_start,
        delete_share_data=True,
    )
    result = _process(folder, "--delete-after-transfer")
    assert result.exit_code == 0, result.output
    assert "still being written" in _out(result)
    assert folder.is_dir()  # kept: its pending note is not at the destination
    assert any(folder.rglob("twophoton_pending.json"))
    assert image.is_dir() and (source_root / "MB247_CI63" / "SyncData102").is_dir()


def test_take_waits_whole_while_one_2p_folder_is_still_written(tmp_path):
    # Audit 2026-10-15: the settled ThorSync folder alone was paired while
    # ThorImage still wrote its TIFFs; the image folder was later swept as
    # 2P-only, the pair lost for good. The take waits whole.
    pytest.importorskip("tifffile")
    dest_root = tmp_path / "dest"
    source_root = tmp_path / "windows_share" / "MD"
    take_start = 1_000_000.0
    _make_sync_folder_2p(source_root, "MB247_CI63", "SyncData102", mtime=take_start + 1)
    image = _make_image_folder_2p(
        source_root, "MB247_CI63", "Fly1", u_time=take_start + 1, n_frames=4
    )
    now = time.time()
    for p in image.iterdir():
        os.utime(p, (now, now))
    folder = _armed_recording(
        tmp_path / "rec",
        source_root=source_root,
        dest_root=dest_root,
        take_start=take_start,
    )
    result = _process(folder)
    assert result.exit_code == 0, result.output
    assert not (dest_root / folder.name / "2P").exists()
    assert (folder / "twophoton_pending.json").is_file()
    assert not (dest_root / "2p_only").exists()
    assert "still being written" in _out(result)

    for p in image.iterdir():
        os.utime(p, (take_start + 30, take_start + 30))
    result = _process(folder)
    assert result.exit_code == 0, result.output
    assert (dest_root / folder.name / "2P" / "SyncData102" / "Episode001.h5").exists()
    assert (dest_root / folder.name / "2P" / "Fly1" / "ChanA.tif").exists()
    assert not (folder / "twophoton_pending.json").exists()


def test_process_flags_a_cut_off_thorsync_recording(tmp_path):
    # Real 2026-10-14: ThorSync's 320 s limit cut every ~394 s take's episode,
    # silently. Now a WARNING, recorded in the take's twophoton_match.json.
    h5py = pytest.importorskip("h5py")
    import numpy as np

    dest_root = tmp_path / "dest"
    source_root = tmp_path / "windows_share" / "MD"
    take_start = 1_000_000.0
    sync = source_root / "PAM7xCI80" / "SyncData167"
    sync.mkdir(parents=True)
    with h5py.File(sync / "Episode001.h5", "w") as f:
        capture = np.ones((3200, 1), np.uint32)
        capture[:10] = 0
        f.create_group("DI").create_dataset("CaptureOn", data=capture)
    (sync / "ThorRealTimeDataSettings.xml").write_text(
        '<?xml version="1.0"?><ThorData><AcquireBoard active="1" duration="320">'
        '<DataChannel alias="CaptureOn" enable="1" /><SampleRate enable="1" '
        'rate="10" /></AcquireBoard></ThorData>'
    )
    for p in (*sync.iterdir(), sync):
        os.utime(p, (take_start + 2, take_start + 2))
    folder = _armed_recording(
        tmp_path / "rec",
        source_root=source_root,
        dest_root=dest_root,
        take_start=take_start,
    )
    result = _process(folder)
    assert result.exit_code == 0, result.output
    out = _out(result)
    assert "WARNING" in out
    assert "SyncData167 stopped at its 320 s duration limit" in out
    record = json.loads((dest_root / folder.name / "twophoton_match.json").read_text())
    (entry,) = record["matched"]
    assert entry["thorsync_cut_off"]["limit_s"] == 320.0
    assert "AirFlow" in entry["thorsync_cut_off"]["message"]


def test_denied_2p_source_deletes_become_one_explicit_warning(
    tmp_path, monkeypatch, caplog
):
    # The rig's share has no delete right: one error per folder buried every
    # real warning. Now: one line saying what to do instead.
    from octacam.plugins.twophoton.copying import remove_sources

    def deny(path, *a, **kw):
        raise PermissionError(13, "Permission denied", str(path))

    monkeypatch.setattr(shutil, "rmtree", deny)
    srcs = [tmp_path / "PAM7xCI80" / n for n in ("SyncData167", "Fly1_Streaming_001")]
    with caplog.at_level(logging.INFO, logger="octacam"):
        remove_sources(srcs, tmp_path)
    errors = [r for r in caplog.records if r.levelname == "ERROR"]
    warnings = [r.getMessage() for r in caplog.records if r.levelname == "WARNING"]
    assert errors == []
    assert len(warnings) == 1
    assert "No permission to delete 2 verified 2P source folder(s)" in warnings[0]
    assert "SyncData167, Fly1_Streaming_001" in warnings[0]
    assert "octacam twophoton verify" in warnings[0]


# --- TIFF stacks --------------------------------------------------------------


def test_process_assembles_multipage_tiff_stack_on_transfer(tmp_path):
    tifffile = pytest.importorskip("tifffile")
    dest_root = tmp_path / "dest"
    source_root = tmp_path / "windows_share" / "MD"
    take_start = 1_000_000.0
    _make_image_folder_2p(
        source_root, "MB247_CI63", "Fly1", u_time=take_start - 5, n_frames=6
    )
    folder = _armed_recording(
        tmp_path / "rec",
        source_root=source_root,
        dest_root=dest_root,
        take_start=take_start,
    )
    result = _process(folder)
    assert result.exit_code == 0, result.output
    dest_2p = dest_root / folder.name / "2P" / "Fly1"
    assert (dest_2p / "ChanA.tif").is_file()
    assert not list(dest_2p.glob("ChanA_*.tif"))
    with tifffile.TiffFile(dest_2p / "ChanA.tif") as tf:
        assert len(tf.pages) == 6
        assert tf.series[0].axes == "TYX"
        for i in range(6):
            assert (tf.pages[i].asarray() == i + 1).all()


@pytest.mark.parametrize("checksum", [True, False])
def test_process_pixel_verifies_assembled_stack_when_checksum_on(
    tmp_path, monkeypatch, checksum
):
    # A delete trusts a "verified" stack: with [transfer].checksum on, that
    # must mean every page compared to its frame, not just a page count.
    pytest.importorskip("tifffile")
    from octacam.plugins.twophoton import tiff

    dest_root = tmp_path / "dest"
    source_root = tmp_path / "windows_share" / "MD"
    take_start = 1_000_000.0
    _make_image_folder_2p(
        source_root, "MB247_CI63", "Fly1", u_time=take_start - 5, n_frames=4
    )
    folder = _make_recording(
        tmp_path / "rec",
        with_outputs=True,
        toml=_toml(
            dest_root,
            source_root,
            transfer={"checksum": checksum},
            match_window_s=60,
            settle_s=60,
        ),
        summary={
            "start_time_ns": int(take_start * 1e9),
            "duration_s": 10.0,
            "plugins": {"twophoton": {"armed": True}},
        },
    )
    seen = []
    real = tiff.assemble_channel_stack

    def spy(*args, **kwargs):
        seen.append(kwargs.get("full_verify"))
        return real(*args, **kwargs)

    monkeypatch.setattr(tiff, "assemble_channel_stack", spy)
    result = _process(folder)
    assert result.exit_code == 0, result.output
    assert seen == [checksum]
    assert (dest_root / folder.name / "2P" / "Fly1" / "ChanA.tif").is_file()
    assert ("every frame verified" in _out(result)) is checksum


def test_process_assembles_fastz_one_stack_per_z_plane_on_transfer(tmp_path):
    # Real "Fly1_FastZ_Test" (2026-09-16/17): one TIFF per (channel, plane),
    # not loose frames and not the single-axis case.
    tifffile = pytest.importorskip("tifffile")
    dest_root = tmp_path / "dest"
    source_root = tmp_path / "windows_share" / "MD"
    take_start = 1_000_000.0
    _make_fastz_folder_2p(
        source_root,
        "MB247_CI63",
        "Fly1",
        u_time=take_start - 5,
        zsteps=3,
        timepoints=5,
    )
    folder = _armed_recording(
        tmp_path / "rec",
        source_root=source_root,
        dest_root=dest_root,
        take_start=take_start,
    )
    result = _process(folder)
    assert result.exit_code == 0, result.output
    dest_2p = dest_root / folder.name / "2P" / "Fly1"
    for z in ("Z01", "Z02", "Z03"):
        assert (dest_2p / f"ChanA_{z}.tif").is_file()
    assert not list(dest_2p.glob("ChanA_*_*.tif"))
    with tifffile.TiffFile(dest_2p / "ChanA_Z02.tif") as tf:
        assert len(tf.pages) == 5
        for t in range(5):
            assert (tf.pages[t].asarray() == 2000 + t + 1).all()


def test_process_assembly_disabled_falls_back_to_per_file_copy(tmp_path):
    pytest.importorskip("tifffile")
    dest_root = tmp_path / "dest"
    source_root = tmp_path / "windows_share" / "MD"
    take_start = 1_000_000.0
    _make_image_folder_2p(
        source_root, "MB247_CI63", "Fly1", u_time=take_start - 5, n_frames=6
    )
    folder = _armed_recording(
        tmp_path / "rec",
        source_root=source_root,
        dest_root=dest_root,
        take_start=take_start,
        assemble_tiff_stacks=False,
    )
    result = _process(folder)
    assert result.exit_code == 0, result.output
    dest_2p = dest_root / folder.name / "2P" / "Fly1"
    assert not (dest_2p / "ChanA.tif").is_file()
    assert len(list(dest_2p.glob("ChanA_*.tif"))) == 6


def test_process_dry_run_previews_assembly_without_writing(tmp_path):
    pytest.importorskip("tifffile")
    dest_root = tmp_path / "dest"
    source_root = tmp_path / "windows_share" / "MD"
    take_start = 1_000_000.0
    _make_image_folder_2p(
        source_root, "MB247_CI63", "Fly1", u_time=take_start - 5, n_frames=6
    )
    folder = _armed_recording(
        tmp_path / "rec",
        source_root=source_root,
        dest_root=dest_root,
        take_start=take_start,
    )
    result = runner.invoke(
        app, ["process", str(folder), "--no-transcode", "--no-grid", "--dry-run"]
    )
    assert result.exit_code == 0, result.output
    assert "would assemble" in result.output
    assert not dest_root.exists()


def test_rerun_keeps_complete_stacks_and_rebuilds_a_bad_one(tmp_path):
    pytest.importorskip("tifffile")
    import numpy as np
    import tifffile

    from octacam.plugins.twophoton.copying import _stack_is_complete
    from octacam.plugins.twophoton.tiff import plan_assembly

    src = _make_image_folder_2p(
        tmp_path / "share", "exp", "Fly1_Streaming", u_time=1_000_000, n_frames=4
    )
    (ch,) = plan_assembly(src).ready
    stack = tmp_path / "ChanA.tif"
    assert not _stack_is_complete(stack, ch.source_files, recheck=False)
    frames = [np.full((16, 16), i, dtype=np.uint16) for i in range(1, 5)]

    def write(pages):
        with tifffile.TiffWriter(stack) as tw:
            for page in pages:
                tw.write(page, contiguous=False)

    write(frames)
    assert _stack_is_complete(stack, ch.source_files, recheck=False)
    assert _stack_is_complete(stack, ch.source_files, recheck=True)
    frames[2] = frames[2] + 1
    write(frames)
    assert _stack_is_complete(stack, ch.source_files, recheck=False)  # count only
    assert not _stack_is_complete(stack, ch.source_files, recheck=True)
    write(frames[:3])
    assert not _stack_is_complete(stack, ch.source_files, recheck=False)


def test_the_deleting_commands_need_paths_or_all(tmp_path):
    # Tommy's review: no paths walked the whole destination (reassemble deletes
    # the frames it stacks) or the whole share (verify --delete).
    dest_root = tmp_path / "dest"
    source_root = tmp_path / "share"
    source_root.mkdir()
    config_dir = _rig_config(tmp_path, dest_root, source_root)
    result = runner.invoke(
        app, ["twophoton", "reassemble", "--config", str(config_dir)]
    )
    assert result.exit_code != 0 and "--all" in result.output
    result = runner.invoke(
        app, ["twophoton", "verify", "--config", str(config_dir), "--delete"]
    )
    assert result.exit_code != 0 and "--all" in result.output


def test_reassemble_tiffs_requires_config():
    result = runner.invoke(app, ["twophoton", "reassemble"])
    assert result.exit_code != 0


def test_reassemble_tiffs_assembles_and_deletes_originals(tmp_path):
    pytest.importorskip("tifffile")
    dest_root = tmp_path / "dest"
    take_folder = dest_root / "260813_" / "Fly2" / "001"
    _make_image_folder_2p_flat(take_folder / "2P" / "Fly1", n_frames=5)
    config_dir = _rig_config(tmp_path, dest_root)
    result = runner.invoke(
        app, ["twophoton", "reassemble", "--config", str(config_dir), "--all"]
    )
    assert result.exit_code == 0, result.output
    dest_2p = take_folder / "2P" / "Fly1"
    assert (dest_2p / "ChanA.tif").is_file()
    assert not list(dest_2p.glob("ChanA_*.tif"))
    # Idempotent: an already-stacked folder is a no-op.
    result = runner.invoke(
        app, ["twophoton", "reassemble", "--config", str(config_dir), "--all"]
    )
    assert result.exit_code == 0, result.output


def test_reassemble_tiffs_dry_run_touches_nothing(tmp_path):
    pytest.importorskip("tifffile")
    dest_root = tmp_path / "dest"
    take_folder = dest_root / "260813_" / "Fly2" / "001"
    _make_image_folder_2p_flat(take_folder / "2P" / "Fly1", n_frames=5)
    config_dir = _rig_config(tmp_path, dest_root)
    result = runner.invoke(
        app,
        ["twophoton", "reassemble", "--config", str(config_dir), "--all", "--dry-run"],
    )
    assert result.exit_code == 0, result.output
    dest_2p = take_folder / "2P" / "Fly1"
    assert not (dest_2p / "ChanA.tif").is_file()
    assert len(list(dest_2p.glob("ChanA_*.tif"))) == 5


# --- order: data before renders; re-runs follow a renamed take ----------------


def _ordered_take(tmp_path, dest_root, source_root, take_start):
    _make_image_folder_2p(
        source_root, "MB247_CI63", "Fly1", u_time=take_start - 5, n_frames=4
    )
    folder = _make_recording(
        tmp_path / "data" / "day1" / "Fly1" / "001",
        with_outputs=True,
        toml=_toml(
            dest_root, source_root, visualization=True, match_window_s=60, settle_s=60
        ),
        summary={
            "relative_directory": "day1/Fly1/001",
            "start_time_ns": int(take_start * 1e9),
            "duration_s": 10.0,
            "plugins": {"twophoton": {"armed": True}},
        },
    )
    (folder / "grid.mp4").unlink()
    return folder


def test_process_transfers_data_before_rendering_and_reruns_into_renamed_take(
    tmp_path, monkeypatch
):
    # Matthias, 2026-10-15: a day's grids took ~8 h and held the data's copy
    # behind them. Order: behavior + 2P data, the RecordingN rename, the
    # grids, then the synced render; a re-run writes into the renamed take.
    pytest.importorskip("tifffile")
    from octacam.plugins.twophoton import copying as tp_copying
    from octacam.plugins.twophoton import render as tp_render

    dest_root = tmp_path / "dest"
    source_root = tmp_path / "windows_share" / "MD"
    folder = _ordered_take(tmp_path, dest_root, source_root, 1_000_000.0)
    old_take = dest_root / "day1" / "Fly1" / "001"
    events = []
    real_finish = tp_copying.finish_pairing

    def finish(*args, **kwargs):
        events.append("2p")
        return real_finish(*args, **kwargs)

    def fake_grid(folder, *, output, **_kwargs):
        events.append(("grid", old_take.exists()))
        output.write_bytes(b"grid")
        return output

    def fake_render(take_dir, options=None, **_kwargs):
        events.append(("render", take_dir.name))

    # The process step imports it when it runs (the plugin stays light).
    monkeypatch.setattr(tp_copying, "finish_pairing", finish)
    monkeypatch.setattr("octacam.process.build_grid_video", fake_grid)
    monkeypatch.setattr(tp_render, "render_synced_video", fake_render)

    result = runner.invoke(app, ["process", str(folder), "--no-transcode"])
    assert result.exit_code == 0, result.output
    assert events == ["2p", ("grid", False), ("render", "Recording1_Synced")]
    renamed = dest_root / "day1" / "Fly1" / "Recording1_Synced"
    assert (renamed / "2P" / "Fly1" / "ChanA.tif").is_file()
    assert (renamed / "camera_LF.mp4").is_file()
    assert (renamed / "grid.mp4").is_file()
    assert not old_take.exists()

    result = runner.invoke(app, ["process", str(folder), "--no-transcode"])
    assert result.exit_code == 0, result.output
    assert not old_take.exists()
    fly = dest_root / "day1" / "Fly1"
    assert sorted(p.name for p in fly.iterdir() if p.is_dir()) == ["Recording1_Synced"]


def test_detached_job_reports_transfer_grid_render_phases(tmp_path, monkeypatch):
    # The GUI's background-processing notice reads these: the data phases
    # first, the synced render as its own "render" phase last, and the grid
    # encode reporting its own progress even with no terminal bar.
    pytest.importorskip("tifffile")
    from types import SimpleNamespace

    from octacam import process as process_mod
    from octacam.plugins.twophoton import render as tp_render

    dest_root = tmp_path / "dest"
    source_root = tmp_path / "windows_share" / "MD"
    folder = _ordered_take(tmp_path, dest_root, source_root, 1_000_000.0)
    events = []

    class Reporter(process_mod.process_jobs.NullReporter):
        def begin_phase(self, phase, total):
            events.append(("phase", phase, total))

        def item_started(self, index, total, path):
            events.append(("item", Path(path).name))

        def transcode_progress(self, index, total):
            events.append(("grid-progress", index))
            return lambda prog: None

    def fake_grid(folder, *, output, on_progress=None, **_kwargs):
        assert on_progress is not None
        on_progress(SimpleNamespace(frame=1, total_frames=2))
        output.write_bytes(b"grid")
        return output

    monkeypatch.setattr(process_mod, "build_grid_video", fake_grid)
    monkeypatch.setattr(tp_render, "render_synced_video", lambda take, *a, **kw: None)
    process_mod.run([folder], process_mod.ProcessOptions(transcode=False), Reporter())
    phases = [e[1] for e in events if e[0] == "phase"]
    # develop transfers the grids in a second "transfer" phase of their own.
    assert phases == ["transfer", "grid", "transfer", "render"]
    assert ("grid-progress", 1) in events
    assert events[-1] == ("item", "Recording1_Synced")  # in its final folder


def test_current_take_dest_finds_a_renamed_take(tmp_path):
    from octacam.plugins.twophoton.takes import current_take_dest

    fly = tmp_path / "day1" / "Fly1"
    renamed = fly / "Recording3_Synced_Streaming"
    renamed.mkdir(parents=True)
    (renamed / "recording_summary.json").write_text(
        json.dumps({"start_time_ns": 1_500_000_000_000})
    )
    other = fly / "Recording2_Beh"
    other.mkdir()
    (other / "recording_summary.json").write_text(
        json.dumps({"start_time_ns": 1_400_000_000_000})
    )
    assert current_take_dest(fly / "003", 1500.0) == renamed
    assert current_take_dest(fly / "004", 1600.0) == fly / "004"
    assert current_take_dest(fly / "003", None) == fly / "003"
    (fly / "003").mkdir()
    assert current_take_dest(fly / "003", 1500.0) == fly / "003"


# --- delete-after-transfer ----------------------------------------------------


def test_process_delete_after_transfer_removes_local_once_verified(tmp_path):
    dest_root = tmp_path / "dest"
    folder = _make_recording(tmp_path / "rec", with_outputs=True, toml=_toml(dest_root))
    result = _process(folder, "--delete-after-transfer")
    assert result.exit_code == 0, result.output
    assert (dest_root / folder.name / "camera_LF.mp4").exists()
    assert not folder.exists()


def test_process_delete_after_transfer_dry_run_previews_without_deleting(tmp_path):
    dest_root = tmp_path / "dest"
    folder = _make_recording(tmp_path / "rec", with_outputs=True, toml=_toml(dest_root))
    result = _process(folder, "--delete-after-transfer", "--dry-run")
    assert result.exit_code == 0, result.output
    assert "would remove local" in result.output
    assert folder.exists()
    assert not dest_root.exists()


def test_process_delete_after_transfer_skipped_on_transcode_failure(
    tmp_path, monkeypatch
):
    # A camera whose transcode failed was never archived: never deleted.
    dest_root = tmp_path / "dest"
    folder = _make_recording(
        tmp_path / "rec", with_outputs=False, toml=_toml(dest_root)
    )

    def fake_transcode(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr("octacam.process.transcode_file", fake_transcode)
    result = runner.invoke(
        app, ["process", str(folder), "--no-grid", "--delete-after-transfer"]
    )
    assert result.exit_code != 0
    assert folder.exists()


def test_process_config_default_delete_after_transfer(tmp_path):
    dest_root = tmp_path / "dest"
    folder = _make_recording(
        tmp_path / "rec",
        with_outputs=True,
        toml=_toml(dest_root, transfer={"delete_after_transfer": True}),
    )
    result = _process(folder)
    assert result.exit_code == 0, result.output
    assert not folder.exists()


def test_process_no_delete_after_transfer_overrides_config(tmp_path):
    dest_root = tmp_path / "dest"
    folder = _make_recording(
        tmp_path / "rec",
        with_outputs=True,
        toml=_toml(dest_root, transfer={"delete_after_transfer": True}),
    )
    result = _process(folder, "--no-delete-after-transfer")
    assert result.exit_code == 0, result.output
    assert folder.exists()


# --- the sweep: 2P data no take claims ----------------------------------------


def test_twophoton_sweep_requires_config():
    result = runner.invoke(app, ["twophoton", "sweep"])
    assert result.exit_code != 0
    assert "--config" in result.output


def test_twophoton_sweep_requires_twophoton_config(tmp_path):
    config_dir = tmp_path / "rig"
    config_dir.mkdir()
    (config_dir / "octacam_config.toml").write_text(_toml(tmp_path / "dest"))
    result = runner.invoke(app, ["twophoton", "sweep", "--config", str(config_dir)])
    assert result.exit_code != 0
    assert "twophoton plugin has no `source`" in _out(result)


def test_twophoton_sweep_transfers_unmatched_folder(tmp_path):
    dest_root = tmp_path / "dest"
    source_root = tmp_path / "windows_share" / "MD"
    _make_sync_folder_2p(
        source_root, "Tests", "SyncData001", mtime=time.time() - 10_000
    )
    config_dir = _rig_config(tmp_path, dest_root, source_root, settle_s=60)
    result = runner.invoke(app, ["twophoton", "sweep", "--config", str(config_dir)])
    assert result.exit_code == 0, result.output
    (date_dir,) = list((dest_root / "2p_only" / "Tests").iterdir())
    assert (date_dir / "SyncData001" / "Episode001.h5").exists()


def test_twophoton_sweep_deletes_verified_sources_when_rig_deletes(tmp_path):
    # A rig that deletes after transfer clears the share of 2P-only data too,
    # once its copy is verified, when the data's owner chose it.
    dest_root = tmp_path / "dest"
    source_root = tmp_path / "windows_share" / "MD"
    folder = _make_sync_folder_2p(
        source_root, "Tests", "SyncData001", mtime=time.time() - 10_000
    )
    config_dir = _rig_config(
        tmp_path,
        dest_root,
        source_root,
        settle_s=60,
        delete_share_data=True,
        transfer={"delete_after_transfer": True},
    )
    result = runner.invoke(app, ["twophoton", "sweep", "--config", str(config_dir)])
    assert result.exit_code == 0, result.output
    (date_dir,) = list((dest_root / "2p_only" / "Tests").iterdir())
    assert (date_dir / "SyncData001" / "Episode001.h5").exists()
    assert not folder.exists()


def test_share_data_is_kept_unless_its_owner_chose_to_delete_it(tmp_path):
    # Matthias, 2026-10-21: another person's imaging data on the share is
    # theirs to decide about; the rig's delete_after_transfer alone keeps it.
    dest_root = tmp_path / "dest"
    source_root = tmp_path / "windows_share" / "XY"
    folder = _make_sync_folder_2p(
        source_root, "Tests", "SyncData001", mtime=time.time() - 10_000
    )
    config_dir = _rig_config(
        tmp_path,
        dest_root,
        source_root,
        settle_s=60,
        transfer={"delete_after_transfer": True},
    )
    result = runner.invoke(app, ["twophoton", "sweep", "--config", str(config_dir)])
    assert result.exit_code == 0, result.output
    (date_dir,) = list((dest_root / "2p_only" / "Tests").iterdir())
    assert (date_dir / "SyncData001" / "Episode001.h5").exists()  # copied
    assert folder.exists()  # but kept


def test_a_user_s_own_table_decides_for_their_share_data(tmp_path):
    # `--user MD`: their share folder, destination and delete_share_data.
    dest_root = tmp_path / "dest"
    mine = tmp_path / "windows_share" / "MD"
    folder = _make_sync_folder_2p(
        mine, "Tests", "SyncData001", mtime=time.time() - 10_000
    )
    config_dir = _rig_config(
        tmp_path,
        tmp_path / "rig-dest",
        tmp_path / "windows_share" / "rig",
        settle_s=60,
        transfer={"delete_after_transfer": True},
    )
    toml = config_dir / "octacam_config.toml"
    toml.write_text(
        toml.read_text()
        + f'[users.MD]\ntransfer_directory = "{dest_root.as_posix()}"\n'
        + f'[users.MD.twophoton]\nsource = "{mine.as_posix()}"\n'
        + "delete_share_data = true\n"
    )
    result = runner.invoke(
        app, ["twophoton", "sweep", "--config", str(config_dir), "--user", "md"]
    )
    assert result.exit_code == 0, result.output
    assert list((dest_root / "2p_only" / "Tests").iterdir())
    assert not folder.exists()


def test_a_share_delete_never_leaves_the_user_s_folder(tmp_path, caplog):
    from octacam.plugins.twophoton.copying import remove_sources

    root = tmp_path / "windows_share" / "MD"
    inside = root / "Exp" / "SyncData001"
    outside = tmp_path / "windows_share" / "XY" / "Exp" / "SyncData002"
    for d in (inside, outside):
        d.mkdir(parents=True)
    with caplog.at_level(logging.INFO, logger="octacam"):
        remove_sources([inside, outside, root], root)
        remove_sources([outside], None)
    assert not inside.exists()
    assert outside.exists() and root.exists()
    refused = [r.getMessage() for r in caplog.records if r.levelname == "WARNING"]
    assert len(refused) == 3
    assert all("outside the user's share folder" in m for m in refused)


def test_twophoton_sweep_keeps_sources_without_delete_after_transfer(tmp_path):
    dest_root = tmp_path / "dest"
    source_root = tmp_path / "windows_share" / "MD"
    folder = _make_sync_folder_2p(
        source_root, "Tests", "SyncData001", mtime=time.time() - 10_000
    )
    config_dir = _rig_config(tmp_path, dest_root, source_root, settle_s=60)
    result = runner.invoke(app, ["twophoton", "sweep", "--config", str(config_dir)])
    assert result.exit_code == 0, result.output
    assert folder.exists()


def test_twophoton_sweep_skips_folder_still_settling(tmp_path):
    dest_root = tmp_path / "dest"
    source_root = tmp_path / "windows_share" / "MD"
    _make_sync_folder_2p(source_root, "Tests", "SyncData001", mtime=time.time() - 5)
    config_dir = _rig_config(tmp_path, dest_root, source_root, settle_s=3600)
    result = runner.invoke(app, ["twophoton", "sweep", "--config", str(config_dir)])
    assert result.exit_code == 0, result.output
    assert not (dest_root / "2p_only").exists()


def test_twophoton_sweep_excludes_already_matched_folder(tmp_path):
    # A folder already paired with a (still cached) take is not 2P-only.
    dest_root = tmp_path / "dest"
    source_root = tmp_path / "windows_share" / "MD"
    _make_sync_folder_2p(
        source_root, "MB247_CI63", "SyncData102", mtime=time.time() - 10_000
    )
    matched = tmp_path / "already_processed_take"
    matched.mkdir()
    (matched / "twophoton_match.json").write_text(
        json.dumps({"matched": [{"path": "MB247_CI63/SyncData102", "kind": "sync"}]})
    )
    session_cache.record_recording(matched, session_id="s1")
    config_dir = _rig_config(tmp_path, dest_root, source_root, settle_s=60)
    result = runner.invoke(app, ["twophoton", "sweep", "--config", str(config_dir)])
    assert result.exit_code == 0, result.output
    assert not (dest_root / "2p_only").exists()


def test_twophoton_sweep_excludes_already_matched_folder_with_no_local_cache(
    tmp_path,
):
    # A take whose local folder is gone (cleaned up, or past the cache's
    # retention): its pairing on the destination alone must keep the folder
    # from being copied a second time.
    dest_root = tmp_path / "dest"
    source_root = tmp_path / "windows_share" / "MD"
    _make_sync_folder_2p(
        source_root, "MB247_CI63", "SyncData102", mtime=time.time() - 10_000
    )
    # A transferred take: its summary and pairing record, nothing local.
    take_dir = _write_take(
        dest_root, "day1/Fly1/001", start_time_ns=10**18, duration_s=10.0
    )
    (take_dir / "twophoton_match.json").write_text(
        json.dumps({"matched": [{"path": "MB247_CI63/SyncData102", "kind": "sync"}]})
    )
    config_dir = _rig_config(tmp_path, dest_root, source_root, settle_s=60)
    result = runner.invoke(app, ["twophoton", "sweep", "--config", str(config_dir)])
    assert result.exit_code == 0, result.output
    assert not (dest_root / "2p_only").exists()


def _sweep(source_root, dest_root, *, claimed=frozenset(), **opts):
    """Run the sweep over *source_root* into *dest_root* (verified copies)."""
    from octacam.plugins.twophoton.copying import CopyMode
    from octacam.plugins.twophoton.matching import discover_twophoton_folders
    from octacam.plugins.twophoton.nas import NasIndex
    from octacam.plugins.twophoton.settings import TwoPhotonSettings
    from octacam.plugins.twophoton.sweep import sweep

    settings = TwoPhotonSettings(source=str(source_root), **{"settle_s": 60, **opts})
    return sweep(
        source_root,
        discover_twophoton_folders(source_root),
        NasIndex.build(dest_root),
        settings,
        CopyMode(
            verify=True,
            by_content=False,
            dry_run=False,
            assemble=settings.assemble_tiff_stacks,
        ),
        claimed_this_run=claimed,
    )


def test_sweep_also_exclude_covers_a_same_run_match_not_yet_on_disk(tmp_path):
    # A dry run never writes twophoton_match.json, so the sweep must also
    # skip what this run's pairing just claimed.
    source_root = tmp_path / "windows_share" / "MD"
    matched_a = _make_sync_folder_2p(
        source_root, "expA", "SyncData102", mtime=time.time() - 10_000
    )
    dest_a = tmp_path / "dest_a"
    result = _sweep(source_root, dest_a)
    assert result.failed == 0
    assert result.copied > 0
    assert (dest_a / "2p_only").exists()

    matched_b = _make_sync_folder_2p(
        source_root, "expB", "SyncData102", mtime=time.time() - 10_000
    )
    dest_b = tmp_path / "dest_b"
    result = _sweep(
        source_root,
        dest_b,
        claimed=frozenset({matched_a.resolve(), matched_b.resolve()}),
    )
    assert (result.copied, result.failed) == (0, 0)
    assert not (dest_b / "2p_only").exists()


def test_sweep_skips_folder_already_present_in_generic_bucket_by_name(tmp_path):
    # Real bug on production data: a folder an older sweep already filed in
    # the generic bucket, never claimed by a take, was copied again on every
    # sweep (10 duplicates, ~10.8 GB on the shared NAS).
    source_root = tmp_path / "windows_share" / "MD"
    dest_root = tmp_path / "dest"
    two_p_mtime = time.time() - 10_000
    folder = _make_sync_folder_2p(source_root, "expA", "SyncData102", mtime=two_p_mtime)
    date_str = time.strftime("%y%m%d", time.localtime(two_p_mtime))
    already_there = dest_root / "2p_only" / "expA" / date_str / folder.name
    already_there.mkdir(parents=True)
    shutil.copy(folder / "Episode001.h5", already_there / "Episode001.h5")

    result = _sweep(source_root, dest_root)
    assert (result.copied, result.failed) == (0, 0)
    assert list(dest_root.glob("2p_only/*/*/SyncData102")) == [already_there]


def test_sweep_does_not_skip_a_new_folder_sharing_an_old_folders_name(tmp_path):
    # Real bug (2026-10-07): ThorImage reuses names, so a new day's
    # Fly1_Zstack was never transferred because a September experiment had one
    # on the NAS. Dedup compares the acquisition (uTime), not the name.
    source_root = tmp_path / "windows_share" / "MD"
    dest_root = tmp_path / "dest"
    new = source_root / "expNew" / "Fly1_Zstack"
    new.mkdir(parents=True)
    (new / "Experiment.xml").write_text(
        '<?xml version="1.0"?><ThorImageExperiment><Date date="x" uTime="2000000" />'
        "</ThorImageExperiment>"
    )
    old_mtime = time.time() - 10_000
    os.utime(new / "Experiment.xml", (old_mtime, old_mtime))
    old_copy = (
        dest_root
        / "260903_Old"
        / "Fly1"
        / "Recording11_2P_Zstack"
        / "2P"
        / "Fly1_Zstack"
    )
    old_copy.mkdir(parents=True)
    (old_copy / "Experiment.xml").write_text(
        '<?xml version="1.0"?><ThorImageExperiment><Date date="x" uTime="1000000" />'
        "</ThorImageExperiment>"
    )
    result = _sweep(
        source_root,
        dest_root,
        attribute_unclaimed_to_fly=False,
        assemble_tiff_stacks=False,
    )
    assert result.failed == 0 and result.copied >= 1
    assert list(dest_root.glob("2p_only/expNew/*/Fly1_Zstack/Experiment.xml"))


def test_process_automatically_sweeps_unmatched_twophoton(tmp_path):
    # A normal process run also sweeps the share it paired from, in the same
    # command.
    dest_root = tmp_path / "dest"
    source_root = tmp_path / "windows_share" / "MD"
    take_start = 1_000_000.0
    _make_sync_folder_2p(
        source_root, "MB247_CI63", "SyncData102", mtime=take_start - 30
    )
    _make_sync_folder_2p(
        source_root, "Tests", "SyncData999", mtime=time.time() - 10_000
    )
    folder = _armed_recording(
        tmp_path / "rec",
        source_root=source_root,
        dest_root=dest_root,
        take_start=take_start,
    )
    result = _process(folder)
    assert result.exit_code == 0, result.output
    assert (dest_root / folder.name / "2P" / "SyncData102" / "Episode001.h5").exists()
    (date_dir,) = list((dest_root / "2p_only" / "Tests").iterdir())
    assert (date_dir / "SyncData999" / "Episode001.h5").exists()


def test_process_automatic_sweep_retroactively_pairs_prior_pending_take(tmp_path):
    # The automatic sweep of an unrelated later run also pairs a take left
    # pending by an earlier run.
    dest_root = tmp_path / "dest"
    source_root = tmp_path / "windows_share" / "MD"
    take_start = 1_000_000.0
    folder_a = _armed_recording(
        tmp_path / "rec_a",
        source_root=source_root,
        dest_root=dest_root,
        take_start=take_start,
    )
    session_cache.record_recording(folder_a, session_id="s1")
    result = _process(folder_a)
    assert result.exit_code == 0, result.output
    assert (folder_a / "twophoton_pending.json").exists()

    _make_sync_folder_2p(
        source_root, "MB247_CI63", "SyncData102", mtime=take_start - 30
    )
    folder_b = _armed_recording(
        tmp_path / "rec_b",
        source_root=source_root,
        dest_root=dest_root,
        take_start=take_start + 10_000,
    )
    result = _process(folder_b)
    assert result.exit_code == 0, result.output
    assert not (dest_root / folder_b.name / "2P").exists()
    dest_2p = dest_root / folder_a.name / "2P" / "SyncData102"
    assert (dest_2p / "Episode001.h5").exists()
    assert not (folder_a / "twophoton_pending.json").exists()
    assert not (dest_root / "2p_only").exists()


def test_process_automatic_sweep_dry_run_touches_nothing(tmp_path):
    dest_root = tmp_path / "dest"
    source_root = tmp_path / "windows_share" / "MD"
    take_start = 1_000_000.0
    _make_sync_folder_2p(
        source_root, "MB247_CI63", "SyncData102", mtime=take_start - 30
    )
    _make_sync_folder_2p(
        source_root, "Tests", "SyncData999", mtime=time.time() - 10_000
    )
    folder = _armed_recording(
        tmp_path / "rec",
        source_root=source_root,
        dest_root=dest_root,
        take_start=take_start,
    )
    result = _process(folder, "--dry-run")
    assert result.exit_code == 0, result.output
    assert not (dest_root / "2p_only").exists()
    assert "[dry-run] 2P sweep:" in result.output


# --- fly attribution of unclaimed 2P data ---------------------------------------


def _anchored_take(tmp_path, dest_root, source_root, take_start, rel, **opts):
    return _make_recording(
        tmp_path / ("rec_" + rel.replace("/", "_")),
        with_outputs=True,
        toml=_toml(dest_root, source_root, **{"settle_s": 60, **opts}),
        summary={
            "start_time_ns": int(take_start * 1e9),
            "duration_s": 10.0,
            "relative_directory": rel,
            "plugins": {"twophoton": {"armed": True}},
        },
    )


def test_sweep_attributes_unclaimed_image_folder_to_matching_fly(tmp_path):
    # A Z-stack with no behavior counterpart lands next to its fly's takes,
    # found by its ThorImage name sharing the claimed take's "Fly1" prefix.
    pytest.importorskip("tifffile")
    dest_root = tmp_path / "dest"
    source_root = tmp_path / "windows_share" / "MD"
    take_start = 1_000_000.0
    _make_image_folder_2p(
        source_root, "exp", "Fly1_001", u_time=take_start + 2, n_frames=2
    )
    _make_image_folder_2p(
        source_root, "exp", "Fly1_Zstack", u_time=take_start - 5000, n_frames=2
    )
    folder = _anchored_take(
        tmp_path,
        dest_root,
        source_root,
        take_start,
        "day1/Fly1/001",
        reconcile_recordings=False,
    )
    result = _process(folder)
    assert result.exit_code == 0, result.output
    assert (dest_root / "day1" / "Fly1" / "001" / "2P" / "Fly1_001").exists()
    assert (dest_root / "day1" / "Fly1" / "2P_only" / "Fly1_Zstack").exists()
    assert not (dest_root / "2p_only").exists()


def test_sweep_attributes_unclaimed_sync_folder_via_frameout_correlation(tmp_path):
    # A SyncData folder carries no fly name: it goes with the ThorImage
    # folder its FrameOut edges match, then by that folder's name.
    pytest.importorskip("tifffile")
    dest_root = tmp_path / "dest"
    source_root = tmp_path / "windows_share" / "MD"
    take_start = 1_000_000.0
    _make_image_folder_2p(
        source_root, "exp", "Fly1_001", u_time=take_start + 2, n_frames=2
    )
    # Started with Fly1_001's capture (4 s after it), but over the 5 s
    # match window from the take, which leaves it unclaimed.
    _make_verifiable_sync_folder_2p(
        source_root,
        "exp",
        "SyncData999",
        mtime=take_start + 6,
        camera_frames=0,
        frameout_edges=2,
    )
    folder = _anchored_take(
        tmp_path,
        dest_root,
        source_root,
        take_start,
        "day1/Fly1/001",
        reconcile_recordings=False,
    )
    result = _process(folder)
    assert result.exit_code == 0, result.output
    fly = dest_root / "day1" / "Fly1"
    assert (fly / "2P_only" / "SyncData999" / "Episode001.h5").exists()
    assert not (dest_root / "2p_only").exists()


def test_sweep_attributes_unclaimed_folder_with_different_modality_word(tmp_path):
    # Real "AllPAM_G151xCI80" (2026-09-16): the anchor take paired with
    # "Fly1_Streaming_000" shares no base name with "Fly1_Zstack": the
    # leading Fly1 token must still attribute it.
    pytest.importorskip("tifffile")
    dest_root = tmp_path / "dest"
    source_root = tmp_path / "windows_share" / "MD"
    take_start = 1_000_000.0
    _make_image_folder_2p(
        source_root, "exp", "Fly1_Streaming_000", u_time=take_start + 2, n_frames=2
    )
    _make_image_folder_2p(
        source_root, "exp", "Fly1_Zstack", u_time=take_start - 5000, n_frames=2
    )
    folder = _anchored_take(
        tmp_path,
        dest_root,
        source_root,
        take_start,
        "day1/Fly1/001",
        reconcile_recordings=False,
    )
    result = _process(folder)
    assert result.exit_code == 0, result.output
    fly = dest_root / "day1" / "Fly1"
    assert (fly / "001" / "2P" / "Fly1_Streaming_000").exists()
    assert (fly / "2P_only" / "Fly1_Zstack").exists()
    assert not (dest_root / "2p_only").exists()


def test_sweep_ambiguous_prefix_stays_in_generic_bucket(tmp_path):
    # Two flies both claimed a "Fly1" ThorImage folder in one experiment
    # (ThorImage's numbering is offset from octacam's): an unclaimed
    # "Fly1_Zstack" is never guessed between them. Both claims must be on the
    # destination before the sweep: both takes go in one run, whose sweep
    # follows their pairing.
    pytest.importorskip("tifffile")
    dest_root = tmp_path / "dest"
    source_root = tmp_path / "windows_share" / "MD"
    take_a_start = 1_000_000.0
    take_b_start = 2_000_000.0
    _make_image_folder_2p(
        source_root, "exp", "Fly1_001", u_time=take_a_start + 2, n_frames=2
    )
    _make_image_folder_2p(
        source_root, "exp", "Fly1_002", u_time=take_b_start + 2, n_frames=2
    )
    _make_image_folder_2p(
        source_root, "exp", "Fly1_Zstack", u_time=take_a_start - 5000, n_frames=2
    )
    folders = [
        _anchored_take(tmp_path, dest_root, source_root, start, f"{day}/Fly1/001")
        for day, start in (("day1", take_a_start), ("day2", take_b_start))
    ]
    result = _process(*folders)
    assert result.exit_code == 0, result.output
    assert not (dest_root / "day1" / "Fly1" / "2P_only").exists()
    assert not (dest_root / "day2" / "Fly1" / "2P_only").exists()
    assert (dest_root / "2p_only" / "exp").exists()


def test_sweep_attribution_disabled_by_config_stays_generic(tmp_path):
    pytest.importorskip("tifffile")
    dest_root = tmp_path / "dest"
    source_root = tmp_path / "windows_share" / "MD"
    take_start = 1_000_000.0
    _make_image_folder_2p(
        source_root, "exp", "Fly1_001", u_time=take_start + 2, n_frames=2
    )
    _make_image_folder_2p(
        source_root, "exp", "Fly1_Zstack", u_time=take_start - 5000, n_frames=2
    )
    folder = _anchored_take(
        tmp_path,
        dest_root,
        source_root,
        take_start,
        "day1/Fly1/001",
        attribute_unclaimed_to_fly=False,
    )
    result = _process(folder)
    assert result.exit_code == 0, result.output
    assert not list((dest_root / "day1" / "Fly1").glob("*/2P/Fly1_Zstack"))
    assert not (dest_root / "day1" / "Fly1" / "2P_only").exists()
    assert (dest_root / "2p_only" / "exp").exists()


def test_gather_fly_image_basenames_groups_by_experiment_and_base(tmp_path):
    from octacam.plugins.twophoton.nas import NasIndex
    from octacam.plugins.twophoton.sweep import fly_names

    dest_root = tmp_path / "dest"
    fly1 = dest_root / "day1" / "Fly1"
    fly2 = dest_root / "day2" / "Fly2"
    for fly_dir, matched_path in ((fly1, "exp/Fly1_004"), (fly2, "exp/Fly7_002")):
        rel = fly_dir.relative_to(dest_root) / "001"
        take_dir = _write_take(
            dest_root, str(rel), start_time_ns=10**18, duration_s=10.0
        )
        (take_dir / "twophoton_match.json").write_text(
            json.dumps(
                {"matched": [{"path": matched_path, "kind": "image", "gap_s": 1.0}]}
            )
        )
    by_key = fly_names(NasIndex.build(dest_root))
    assert by_key[("exp", "Fly1")] == {fly1}
    assert by_key[("exp", "Fly7")] == {fly2}
    assert by_key[("exp", "fly:Fly1")] == {fly1}
    assert by_key[("exp", "fly:Fly7")] == {fly2}


def test_unclaimed_2p_folder_is_only_attributed_to_a_fly_of_its_own_day(tmp_path):
    # Real 2026-10-14: today's Fly1_Zstack was filed under September's Fly1,
    # the only fly with a Fly1_Zstack on file. Only the same day counts.
    from octacam.plugins.twophoton.matching import TwoPhotonFolder
    from octacam.plugins.twophoton.sweep import attribute

    dest = tmp_path / "dest"
    september = dest / "260903_PAM07xCI80" / "Fly1"
    today = dest / "261014_PAM7xCI80" / "Fly1"
    names = {
        ("PAM7xCI80", "Fly1_Zstack"): {september},
        ("PAM7xCI80", "fly:Fly1"): {september, today},
        ("PAM7xCI80", "Fly1_Streaming"): {today},
    }
    start = time.mktime((2026, 10, 14, 11, 31, 0, 0, 0, -1))
    zstack = TwoPhotonFolder(
        path=tmp_path / "share" / "PAM7xCI80" / "Fly1_Zstack",
        kind="image",
        start_time=start,
        last_mtime=start,
    )
    assert attribute(zstack, "PAM7xCI80", names, []) == today
    later = TwoPhotonFolder(
        path=zstack.path, kind="image", start_time=start + 3 * 86400, last_mtime=start
    )
    assert attribute(later, "PAM7xCI80", names, []) is None


# --- the day's reconciliation manifest ------------------------------------------


def test_manifest_shows_fly_attribution_preview_for_unclaimed_folder(tmp_path):
    pytest.importorskip("tifffile")
    dest_root = tmp_path / "dest"
    source_root = tmp_path / "windows_share" / "MD"
    take_start = 1_000_000.0
    _make_image_folder_2p(
        source_root, "exp", "Fly1_001", u_time=take_start + 2, n_frames=2
    )
    _make_image_folder_2p(
        source_root, "exp", "Fly1_Zstack", u_time=take_start - 5000, n_frames=2
    )
    folder = _anchored_take(
        tmp_path, dest_root, source_root, take_start, "day1/Fly1/001"
    )
    result = _process(folder)
    assert result.exit_code == 0, result.output
    manifest = (dest_root / "day1" / "2p_reconciliation.md").read_text()
    assert "Fly1_Zstack" in manifest
    assert "Fly1/2P_only" in manifest


def test_twophoton_manifest_requires_config():
    result = runner.invoke(app, ["twophoton", "manifest"])
    assert result.exit_code != 0
    assert "--config" in result.output


def test_twophoton_manifest_requires_twophoton_config(tmp_path):
    config_dir = tmp_path / "rig"
    config_dir.mkdir()
    (config_dir / "octacam_config.toml").write_text(_toml(tmp_path / "dest"))
    result = runner.invoke(app, ["twophoton", "manifest", "--config", str(config_dir)])
    assert result.exit_code != 0
    assert "twophoton plugin has no `source`" in _out(result)


def test_twophoton_manifest_reports_matched_and_unmatched_takes(tmp_path):
    dest_root = tmp_path / "dest"
    source_root = tmp_path / "windows_share" / "MD"
    take_start = 1_000_000.0
    _make_sync_folder_2p(
        source_root, "MB247_CI63", "SyncData102", mtime=take_start - 30
    )
    matched = _armed_recording(
        tmp_path / "rec_matched",
        source_root=source_root,
        dest_root=dest_root,
        take_start=take_start,
    )
    unmatched = _armed_recording(
        tmp_path / "rec_unmatched",
        source_root=source_root,
        dest_root=dest_root,
        take_start=take_start + 10_000,
    )
    result = _process(matched, unmatched)
    assert result.exit_code == 0, result.output
    manifest = (dest_root / "rec_matched" / "2p_reconciliation.md").read_text()
    assert "SyncData102" in manifest
    manifest = (dest_root / "rec_unmatched" / "2p_reconciliation.md").read_text()
    assert "**unmatched**" in manifest


def test_twophoton_manifest_excludes_folder_outside_days_take_window(tmp_path):
    # Real-data regression: one share serves several experiments a day; a
    # same-date folder hours away is another experiment's, not this day's.
    from octacam.plugins.twophoton.manifest import rebuild_manifests
    from octacam.plugins.twophoton.matching import discover_twophoton_folders
    from octacam.plugins.twophoton.nas import NasIndex
    from octacam.plugins.twophoton.sweep import fly_names

    dest_root = tmp_path / "dest"
    source_root = tmp_path / "windows_share" / "MD"
    source_root.mkdir(parents=True)
    take_start = 1_000_000.0
    folder = _armed_recording(
        tmp_path / "rec",
        source_root=source_root,
        dest_root=dest_root,
        take_start=take_start,
    )
    result = _process(folder)
    assert result.exit_code == 0, result.output

    _make_sync_folder_2p(
        source_root, "OtherExperiment", "SyncData001", mtime=take_start + 18_000
    )
    index = NasIndex.build(dest_root)
    written = rebuild_manifests(
        index,
        source_root,
        discover_twophoton_folders(source_root),
        fly_names(index),
        dry_run=False,
    )
    assert written == 1
    manifest = (dest_root / "rec" / "2p_reconciliation.md").read_text()
    assert "SyncData001" not in manifest
    assert "None." in manifest


def test_twophoton_manifest_dry_run_touches_nothing(tmp_path):
    dest_root = tmp_path / "dest"
    source_root = tmp_path / "windows_share" / "MD"
    folder = _armed_recording(
        tmp_path / "rec",
        source_root=source_root,
        dest_root=dest_root,
        take_start=1_000_000.0,
    )
    result = _process(folder, "--dry-run")
    assert result.exit_code == 0, result.output
    assert not (dest_root / "rec" / "2p_reconciliation.md").exists()


def test_twophoton_manifest_config_toggle_skips_automatic_rebuild(tmp_path):
    dest_root = tmp_path / "dest"
    source_root = tmp_path / "windows_share" / "MD"
    folder = _armed_recording(
        tmp_path / "rec",
        source_root=source_root,
        dest_root=dest_root,
        take_start=1_000_000.0,
        write_manifest=False,
    )
    result = _process(folder)
    assert result.exit_code == 0, result.output
    assert not (dest_root / "rec" / "2p_reconciliation.md").exists()


def test_twophoton_manifest_standalone_rebuilds_from_already_transferred_data(
    tmp_path,
):
    # Self-contained: works from what is on the destination, no local take.
    dest_root = tmp_path / "dest"
    source_root = tmp_path / "windows_share" / "MD"
    take_start = 1_000_000.0
    _make_sync_folder_2p(source_root, "exp", "SyncData001", mtime=take_start - 30)
    folder = _armed_recording(
        tmp_path / "rec",
        source_root=source_root,
        dest_root=dest_root,
        take_start=take_start,
    )
    result = _process(folder)
    assert result.exit_code == 0, result.output
    manifest_path = dest_root / "rec" / "2p_reconciliation.md"
    manifest_path.unlink()  # an older transfer, from before the manifest

    config_dir = _rig_config(tmp_path, dest_root, source_root)
    result = runner.invoke(app, ["twophoton", "manifest", "--config", str(config_dir)])
    assert result.exit_code == 0, result.output
    assert manifest_path.exists()
    assert "SyncData001" in manifest_path.read_text()


# --- verify: which share folders are safe to delete by hand ----------------------


def _run_verify(tmp_path, dest_root, source_root, *extra, settle_s=60):
    config_dir = tmp_path / "rig"
    if not config_dir.exists():
        _rig_config(tmp_path, dest_root, source_root, settle_s=settle_s)
    return runner.invoke(
        app, ["twophoton", "verify", "--config", str(config_dir), *extra]
    )


def _nas_copy_with_assembled_stack(folder, dest_2p, *, tamper=None):
    """Mirror *folder* as a transfer does: each channel's frames as one stack,
    the rest copied; *tamper(frames)* may edit the frames first.
    """
    tifffile = pytest.importorskip("tifffile")
    from octacam.plugins.twophoton.tiff import _read_single_page, plan_assembly

    dest_2p.mkdir(parents=True)
    covered = set()
    for ch in plan_assembly(folder).ready:
        covered.update(ch.source_files)
        frames = [_read_single_page(f) for f in ch.source_files]
        if tamper is not None:
            frames = tamper(frames)
        with tifffile.TiffWriter(dest_2p / ch.dest_name) as tw:
            for frame in frames:
                tw.write(frame, contiguous=False)
    for f in folder.iterdir():
        if f not in covered:
            (dest_2p / f.name).write_bytes(f.read_bytes())


def test_twophoton_verify_requires_config():
    result = runner.invoke(app, ["twophoton", "verify"])
    assert result.exit_code != 0


def test_twophoton_verify_reports_safe_to_delete_for_verified_folder(tmp_path):
    # Re-verified live, not trusted because a pairing record exists.
    dest_root = tmp_path / "dest"
    source_root = tmp_path / "windows_share" / "MD"
    folder = _make_sync_folder_2p(
        source_root, "exp", "SyncData102", mtime=time.time() - 10_000
    )
    dest_2p = dest_root / "day1" / "Fly1" / "001" / "2P" / "SyncData102"
    dest_2p.mkdir(parents=True)
    for f in folder.iterdir():
        (dest_2p / f.name).write_bytes(f.read_bytes())
    result = _run_verify(tmp_path, dest_root, source_root)
    assert result.exit_code == 0, result.output
    assert "safe to delete" in result.output
    assert "1 safe to delete" in _out(result)
    assert folder.exists()
    assert dest_2p.exists()


def test_twophoton_verify_delete_removes_only_verified_folders(tmp_path):
    dest_root = tmp_path / "dest"
    source_root = tmp_path / "windows_share" / "MD"
    good = _make_sync_folder_2p(
        source_root, "exp", "SyncData102", mtime=time.time() - 10_000
    )
    bad = _make_sync_folder_2p(
        source_root, "exp", "SyncData103", mtime=time.time() - 10_000
    )
    missing = _make_sync_folder_2p(
        source_root, "exp", "SyncData104", mtime=time.time() - 10_000
    )
    for folder, corrupt in ((good, False), (bad, True)):
        dest_2p = dest_root / "day1" / "Fly1" / "001" / "2P" / folder.name
        dest_2p.mkdir(parents=True)
        for f in folder.iterdir():
            data = f.read_bytes()
            if corrupt and f.suffix == ".h5":
                data += b"corrupted"
            (dest_2p / f.name).write_bytes(data)
    result = _run_verify(tmp_path, dest_root, source_root, "--delete", "--all")
    assert result.exit_code != 0  # the mismatch still fails the run
    assert not good.exists()
    assert bad.exists()
    assert missing.exists()
    assert "Removed 2P source" in _out(result)


def test_twophoton_verify_flags_content_mismatch(tmp_path):
    dest_root = tmp_path / "dest"
    source_root = tmp_path / "windows_share" / "MD"
    folder = _make_sync_folder_2p(
        source_root, "exp", "SyncData102", mtime=time.time() - 10_000
    )
    dest_2p = dest_root / "day1" / "Fly1" / "001" / "2P" / "SyncData102"
    dest_2p.mkdir(parents=True)
    for f in folder.iterdir():
        data = f.read_bytes()
        if f.suffix == ".h5":
            data += b"corrupted"
        (dest_2p / f.name).write_bytes(data)
    result = _run_verify(tmp_path, dest_root, source_root)
    assert result.exit_code != 0
    assert "MISMATCH" in result.output
    assert "verified \N{EM DASH} safe to delete" not in _out(result)


def test_twophoton_verify_reports_not_yet_transferred(tmp_path):
    dest_root = tmp_path / "dest"
    source_root = tmp_path / "windows_share" / "MD"
    _make_sync_folder_2p(source_root, "exp", "SyncData102", mtime=time.time() - 10_000)
    result = _run_verify(tmp_path, dest_root, source_root)
    assert result.exit_code == 0, result.output
    assert "not transferred yet" in _out(result)


def test_twophoton_verify_skips_unsettled_folder(tmp_path):
    dest_root = tmp_path / "dest"
    source_root = tmp_path / "windows_share" / "MD"
    _make_sync_folder_2p(source_root, "exp", "SyncData102", mtime=time.time())
    result = _run_verify(tmp_path, dest_root, source_root, settle_s=3600)
    assert result.exit_code == 0, result.output
    assert "still writing" in _out(result)


def test_twophoton_verify_accepts_assembled_tiff_stack(tmp_path):
    # The loose frames are never on the NAS (stacked): they are compared with
    # the stack's pages (the real 2026-10-13 false alarm flagged every
    # imaging folder MISMATCH).
    dest_root = tmp_path / "dest"
    source_root = tmp_path / "windows_share" / "MD"
    folder = _make_image_folder_2p(
        source_root, "exp", "Fly1_Streaming", u_time=1_000_000, n_frames=5
    )
    _nas_copy_with_assembled_stack(
        folder,
        dest_root / "day1" / "Fly1" / "Recording1_Synced" / "2P" / "Fly1_Streaming",
    )
    result = _run_verify(tmp_path, dest_root, source_root)
    assert result.exit_code == 0, result.output
    assert "1 safe to delete" in _out(result)
    assert "0 MISMATCH" in _out(result)


def test_twophoton_verify_flags_assembled_stack_with_a_wrong_frame(tmp_path):
    dest_root = tmp_path / "dest"
    source_root = tmp_path / "windows_share" / "MD"
    folder = _make_image_folder_2p(
        source_root, "exp", "Fly1_Streaming", u_time=1_000_000, n_frames=5
    )

    def corrupt_one(frames):
        frames[2] = frames[2] + 1
        return frames

    _nas_copy_with_assembled_stack(
        folder,
        dest_root / "day1" / "Fly1" / "001" / "2P" / "Fly1_Streaming",
        tamper=corrupt_one,
    )
    result = _run_verify(tmp_path, dest_root, source_root)
    out = _out(result)
    assert result.exit_code != 0
    assert "MISMATCH" in out
    assert "1 of 5 frame(s) differ" in out
    assert "missing:" not in out


def test_twophoton_verify_flags_truncated_assembled_stack(tmp_path):
    dest_root = tmp_path / "dest"
    source_root = tmp_path / "windows_share" / "MD"
    folder = _make_image_folder_2p(
        source_root, "exp", "Fly1_Streaming", u_time=1_000_000, n_frames=5
    )
    _nas_copy_with_assembled_stack(
        folder,
        dest_root / "day1" / "Fly1" / "001" / "2P" / "Fly1_Streaming",
        tamper=lambda frames: frames[:4],
    )
    result = _run_verify(tmp_path, dest_root, source_root)
    assert result.exit_code != 0
    assert "4 page(s) for 5 source frame(s)" in _out(result)


def test_twophoton_verify_still_checks_loose_frames_when_not_assembled(tmp_path):
    # A channel copied frame by frame has no stack: its frames are compared
    # file by file, and a missing one is caught.
    dest_root = tmp_path / "dest"
    source_root = tmp_path / "windows_share" / "MD"
    folder = _make_image_folder_2p(
        source_root, "exp", "Fly1_Streaming", u_time=1_000_000, n_frames=5
    )
    dest_2p = dest_root / "day1" / "Fly1" / "001" / "2P" / "Fly1_Streaming"
    dest_2p.mkdir(parents=True)
    for f in folder.iterdir():
        (dest_2p / f.name).write_bytes(f.read_bytes())
    result = _run_verify(tmp_path, dest_root, source_root)
    assert result.exit_code == 0, result.output
    assert "1 safe to delete" in _out(result)

    (dest_2p / "ChanA_001_001_001_003.tif").unlink()
    result = _run_verify(tmp_path, dest_root, source_root)
    assert result.exit_code != 0
    assert "missing: ChanA_001_001_001_003.tif" in _out(result)


def test_twophoton_verify_ignores_a_different_acquisition_with_the_same_name(
    tmp_path,
):
    # Real share 2026-10-14: SyncData165 in two experiments a day apart. A
    # folder not transferred yet is "not transferred", not a MISMATCH against
    # someone else's namesake.
    dest_root = tmp_path / "dest"
    source_root = tmp_path / "windows_share" / "MD"
    old = time.time() - 10_000
    folder = _make_sync_folder_2p(source_root, "Test", "SyncData165", mtime=old)
    (folder / "ThorRealTimeDataSettings.xml").write_bytes(b"<today's settings/>")
    os.utime(folder / "ThorRealTimeDataSettings.xml", (old, old))
    os.utime(folder, (old, old))
    other = dest_root / "day0" / "Fly1" / "Recording7_Synced" / "2P" / "SyncData165"
    other.mkdir(parents=True)
    (other / "ThorRealTimeDataSettings.xml").write_bytes(b"<yesterday's settings/>")
    (other / "Episode001.h5").write_bytes(b"a much bigger, unrelated episode")
    result = _run_verify(tmp_path, dest_root, source_root)
    out = _out(result)
    assert result.exit_code == 0, result.output
    assert "0 safe to delete, 1 not transferred yet" in out
    assert "MISMATCH \N{EM DASH}" not in out


def test_twophoton_verify_paths_limit_the_check_to_one_experiment(tmp_path):
    dest_root = tmp_path / "dest"
    source_root = tmp_path / "windows_share" / "MD"
    folder = _make_image_folder_2p(
        source_root, "THxCI80", "Fly1_Streaming", u_time=1_000_000, n_frames=3
    )
    _nas_copy_with_assembled_stack(
        folder, dest_root / "day1" / "Fly1" / "001" / "2P" / "Fly1_Streaming"
    )
    # An older experiment never transferred: outside the scope, not reported.
    _make_image_folder_2p(
        source_root, "PAM7xCI80", "Fly2_Streaming", u_time=900_000, n_frames=3
    )
    result = _run_verify(tmp_path, dest_root, source_root, str(source_root / "THxCI80"))
    out = _out(result)
    assert result.exit_code == 0, result.output
    assert "1 safe to delete, 0 not transferred yet" in out
    assert "Fly2_Streaming" not in out

    result = _run_verify(tmp_path, dest_root, source_root)
    assert "1 not transferred yet" in _out(result)


# --- renumbering: one RecordingN sequence per fly ---------------------------------


def _plain_take(tmp_path, name, dest_root, source_root, start_time_ns, rel, **opts):
    return _make_recording(
        tmp_path / name,
        with_outputs=True,
        toml=_toml(dest_root, source_root, **opts),
        summary={"start_time_ns": start_time_ns, "relative_directory": rel},
    )


def test_process_auto_reconciles_touched_fly_by_default(tmp_path):
    dest_root = tmp_path / "dest"
    source_root = tmp_path / "windows_share" / "MD"  # no 2P data: a Beh take
    folder = _plain_take(
        tmp_path, "rec", dest_root, source_root, 1_000_000_000, "day1/Fly1/001"
    )
    result = _process(folder)
    assert result.exit_code == 0, result.output
    assert (dest_root / "day1" / "Fly1" / "Recording1_Beh").exists()
    assert not (dest_root / "day1" / "Fly1" / "001").exists()


def test_process_auto_reconcile_disabled_by_config(tmp_path):
    dest_root = tmp_path / "dest"
    source_root = tmp_path / "windows_share" / "MD"
    folder = _plain_take(
        tmp_path,
        "rec",
        dest_root,
        source_root,
        1_000_000_000,
        "day1/Fly1/001",
        reconcile_recordings=False,
    )
    result = _process(folder)
    assert result.exit_code == 0, result.output
    assert (dest_root / "day1" / "Fly1" / "001").exists()
    assert not (dest_root / "day1" / "Fly1" / "Recording1_Beh").exists()


def test_process_auto_reconcile_renumbers_second_take_without_disturbing_first(
    tmp_path,
):
    dest_root = tmp_path / "dest"
    source_root = tmp_path / "windows_share" / "MD"
    folder1 = _plain_take(
        tmp_path, "rec1", dest_root, source_root, 1_000_000_000, "day1/Fly1/001"
    )
    result = _process(folder1)
    assert result.exit_code == 0, result.output
    assert (dest_root / "day1" / "Fly1" / "Recording1_Beh").exists()

    folder2 = _plain_take(
        tmp_path, "rec2", dest_root, source_root, 2_000_000_000, "day1/Fly1/002"
    )
    result = _process(folder2)
    assert result.exit_code == 0, result.output
    assert (dest_root / "day1" / "Fly1" / "Recording1_Beh").exists()
    assert (dest_root / "day1" / "Fly1" / "Recording2_Beh").exists()


def _reconcile(config_dir, *args):
    return runner.invoke(
        app, ["twophoton", "reconcile", "--config", str(config_dir), *map(str, args)]
    )


def test_reconcile_recordings_requires_config():
    result = runner.invoke(app, ["twophoton", "reconcile"])
    assert result.exit_code != 0
    assert "--config" in result.output


def test_reconcile_recordings_requires_paths_or_all(tmp_path):
    # Real request (2026-09-17): never scan the whole NAS implicitly.
    dest_root = tmp_path / "dest"
    _write_take(
        dest_root, "day1/Fly1/001", start_time_ns=1_000_000_000_000, duration_s=10.0
    )
    result = _reconcile(_rig_config(tmp_path, dest_root))
    assert result.exit_code != 0
    assert "--all" in result.output
    assert not (dest_root / "day1" / "Fly1" / "Recording1_Beh").exists()


def test_reconcile_recordings_scoped_to_day_folder_path(tmp_path):
    # Only the named day's flies; a sibling day is never touched.
    dest_root = tmp_path / "dest"
    _write_take(
        dest_root, "day1/Fly1/001", start_time_ns=1_000_000_000_000, duration_s=10.0
    )
    other = _write_take(
        dest_root, "day2/Fly1/001", start_time_ns=1_000_000_000_000, duration_s=10.0
    )
    result = _reconcile(_rig_config(tmp_path, dest_root), dest_root / "day1")
    assert result.exit_code == 0, result.output
    assert (dest_root / "day1" / "Fly1" / "Recording1_Beh").exists()
    assert other.exists()
    assert not (dest_root / "day2" / "Fly1" / "Recording1_Beh").exists()


def test_reconcile_recordings_scoped_to_fly_folder_path(tmp_path):
    dest_root = tmp_path / "dest"
    _write_take(
        dest_root, "day1/Fly1/001", start_time_ns=1_000_000_000_000, duration_s=10.0
    )
    other = _write_take(
        dest_root, "day1/Fly2/001", start_time_ns=1_000_000_000_000, duration_s=10.0
    )
    result = _reconcile(_rig_config(tmp_path, dest_root), dest_root / "day1" / "Fly1")
    assert result.exit_code == 0, result.output
    assert (dest_root / "day1" / "Fly1" / "Recording1_Beh").exists()
    assert other.exists()
    assert not (dest_root / "day1" / "Fly2" / "Recording1_Beh").exists()


def test_reconcile_recordings_scoped_skips_generic_bucket_promotion(tmp_path):
    # The generic bucket has no anchor short of looking at everything: --all.
    dest_root = tmp_path / "dest"
    _write_take(
        dest_root, "day1/Fly1/001", start_time_ns=1_000_000_000_000, duration_s=10.0
    )
    generic = dest_root / "2p_only" / "exp" / "260101"
    (generic / "SomeFly").mkdir(parents=True)
    result = _reconcile(_rig_config(tmp_path, dest_root), dest_root / "day1")
    assert result.exit_code == 0, result.output
    assert (generic / "SomeFly").exists()


def test_reconcile_recordings_renumbers_chronologically(tmp_path):
    dest_root = tmp_path / "dest"
    _write_take(
        dest_root, "day1/Fly1/001", start_time_ns=1_000_000_000_000, duration_s=10.0
    )
    _write_take(
        dest_root,
        "day1/Fly1/002",
        start_time_ns=2_000_000_000_000,
        duration_s=10.0,
        matched_paths=["exp/Fly1_001"],
    )
    result = _reconcile(_rig_config(tmp_path, dest_root), "--all")
    assert result.exit_code == 0, result.output
    fly_dir = dest_root / "day1" / "Fly1"
    assert (fly_dir / "Recording1_Beh" / "recording_summary.json").exists()
    assert (fly_dir / "Recording2_Synced" / "recording_summary.json").exists()
    assert not (fly_dir / "001").exists()
    assert not (fly_dir / "002").exists()


def test_reconcile_recordings_synced_take_folds_in_2p_detail(tmp_path):
    dest_root = tmp_path / "dest"
    _write_take(
        dest_root,
        "day1/Fly1/001",
        start_time_ns=1_000_000_000_000,
        duration_s=10.0,
        matched_paths=["exp/Fly1_Zstack"],
    )
    result = _reconcile(_rig_config(tmp_path, dest_root), "--all")
    assert result.exit_code == 0, result.output
    fly_dir = dest_root / "day1" / "Fly1"
    assert (fly_dir / "Recording1_Synced_Zstack" / "recording_summary.json").exists()


def test_reconcile_recordings_folds_2p_only_into_sequence(tmp_path):
    dest_root = tmp_path / "dest"
    fly_dir = dest_root / "day1" / "Fly1"
    _write_take(
        dest_root, "day1/Fly1/001", start_time_ns=3_000_000_000_000, duration_s=10.0
    )
    two_p = fly_dir / "2P_only" / "Fly1_Zstack"
    two_p.mkdir(parents=True)
    (two_p / "Experiment.xml").write_text(
        '<?xml version="1.0"?><ThorImageExperiment><Date date="x" uTime="1000" />'
        "</ThorImageExperiment>"
    )
    result = _reconcile(_rig_config(tmp_path, dest_root), "--all")
    assert result.exit_code == 0, result.output
    wrapper = fly_dir / "Recording1_2P_Zstack"
    assert (wrapper / "2P" / "Fly1_Zstack" / "Experiment.xml").exists()
    assert (fly_dir / "Recording2_Beh" / "recording_summary.json").exists()
    assert not (fly_dir / "2P_only").exists()


def test_reconcile_recordings_rerun_moves_whole_wrapper_not_just_leaf(tmp_path):
    # Real NAS bug (2026-09-18): adding the detail suffix to an already
    # renumbered RecordingN_2P moved only its 2P leaf and stranded the
    # wrapper's Renderings/ (a Z-stack's MIPs) under the stale name.
    dest_root = tmp_path / "dest"
    fly_dir = dest_root / "day1" / "Fly1"
    _write_take(
        dest_root,
        "day1/Fly1/Recording2_Beh",
        start_time_ns=3_000_000_000_000,
        duration_s=10.0,
    )
    wrapper = fly_dir / "Recording1_2P"
    two_p = wrapper / "2P" / "Fly1_Zstack"
    two_p.mkdir(parents=True)
    (two_p / "Experiment.xml").write_text(
        '<?xml version="1.0"?><ThorImageExperiment><Date date="x" uTime="1000" />'
        "</ThorImageExperiment>"
    )
    renderings = wrapper / "Renderings"
    renderings.mkdir()
    (renderings / "zstack_mip_green.png").write_bytes(b"fake-png")
    result = _reconcile(_rig_config(tmp_path, dest_root), "--all")
    assert result.exit_code == 0, result.output
    new_wrapper = fly_dir / "Recording1_2P_Zstack"
    assert (new_wrapper / "2P" / "Fly1_Zstack" / "Experiment.xml").exists()
    assert (new_wrapper / "Renderings" / "zstack_mip_green.png").exists()
    assert not wrapper.exists()


def test_reconcile_recordings_shifting_every_number_up_does_not_collide(tmp_path):
    # Real NAS run (2026-10-08): a late Z-stack shifts a fly's sequence up; an
    # in-order rename hit Recording2 while it still existed.
    dest_root = tmp_path / "dest"
    fly_dir = dest_root / "day1" / "Fly1"
    _write_take(
        dest_root,
        "day1/Fly1/Recording1_Beh",
        start_time_ns=3_000_000_000_000,
        duration_s=10.0,
    )
    _write_take(
        dest_root,
        "day1/Fly1/Recording2_Beh",
        start_time_ns=4_000_000_000_000,
        duration_s=10.0,
    )
    (fly_dir / "Recording2_Beh" / "marker").write_text("second")
    zstack = fly_dir / "2P_only" / "Fly1_Zstack"
    zstack.mkdir(parents=True)
    (zstack / "Experiment.xml").write_text(
        '<?xml version="1.0"?><ThorImageExperiment><Date date="x" uTime="1000" />'
        "</ThorImageExperiment>"
    )
    result = _reconcile(_rig_config(tmp_path, dest_root), "--all")
    assert result.exit_code == 0, result.output
    assert sorted(p.name for p in fly_dir.iterdir() if p.is_dir()) == [
        "Recording1_2P_Zstack",
        "Recording2_Beh",
        "Recording3_Beh",
    ]
    assert (fly_dir / "Recording3_Beh" / "marker").read_text() == "second"


def test_reconcile_recordings_rerun_with_no_changes_is_noop(tmp_path):
    dest_root = tmp_path / "dest"
    _write_take(
        dest_root, "day1/Fly1/001", start_time_ns=1_000_000_000_000, duration_s=10.0
    )
    config_dir = _rig_config(tmp_path, dest_root)
    result = _reconcile(config_dir, "--all")
    assert result.exit_code == 0, result.output
    fly_dir = dest_root / "day1" / "Fly1"
    mtime_before = (fly_dir / "Recording1_Beh").stat().st_mtime_ns
    result = _reconcile(config_dir, "--all")
    assert result.exit_code == 0, result.output
    assert (fly_dir / "Recording1_Beh").stat().st_mtime_ns == mtime_before


def test_reconcile_recordings_dry_run_touches_nothing(tmp_path):
    dest_root = tmp_path / "dest"
    _write_take(
        dest_root, "day1/Fly1/001", start_time_ns=1_000_000_000_000, duration_s=10.0
    )
    result = _reconcile(_rig_config(tmp_path, dest_root), "--dry-run", "--all")
    assert result.exit_code == 0, result.output
    assert (dest_root / "day1" / "Fly1" / "001").exists()
    assert not (dest_root / "day1" / "Fly1" / "Recording1_Beh").exists()


def test_reconcile_recordings_promotes_generic_bucket_to_new_fly(tmp_path):
    dest_root = tmp_path / "dest"
    _write_take(
        dest_root,
        "260903_PAM7xCI63/Fly1/001",
        start_time_ns=1_000_000_000_000,
        duration_s=10.0,
        matched_paths=["PAM7xCI63/Fly1_001"],
    )
    # Under a different prefix ("Fly9"): nothing claims it.
    orphan = dest_root / "2p_only" / "PAM7xCI63" / "260904" / "Fly9_Zstack"
    orphan.mkdir(parents=True)
    (orphan / "Experiment.xml").write_text(
        '<?xml version="1.0"?><ThorImageExperiment><Date date="x" uTime="2000" />'
        "</ThorImageExperiment>"
    )
    result = _reconcile(_rig_config(tmp_path, dest_root), "--all")
    assert result.exit_code == 0, result.output
    new_fly = dest_root / "260903_PAM7xCI63" / "Fly2"
    wrapper = new_fly / "Recording1_2P_Zstack"
    assert (wrapper / "2P" / "Fly9_Zstack" / "Experiment.xml").exists()
    assert not orphan.exists()


def test_reconcile_recordings_promotes_new_fly_first_batch_unifies_modalities(
    tmp_path,
):
    # A brand-new fly's first batch was grouped by base name, which cannot
    # unify "Fly9_Zstack" and "Fly9_Streaming": two new flies for one.
    dest_root = tmp_path / "dest"
    for name, u_time in (("Fly9_Zstack", "1000"), ("Fly9_Streaming", "2000")):
        orphan = dest_root / "2p_only" / "PAM7xCI63" / "260904" / name
        orphan.mkdir(parents=True)
        (orphan / "Experiment.xml").write_text(
            '<?xml version="1.0"?><ThorImageExperiment>'
            f'<Date date="x" uTime="{u_time}" /></ThorImageExperiment>'
        )
    result = _reconcile(_rig_config(tmp_path, dest_root), "--all")
    assert result.exit_code == 0, result.output
    (day_dir,) = [d for d in dest_root.iterdir() if d.is_dir() and d.name != "2p_only"]
    fly_dirs = [d for d in day_dir.iterdir() if d.is_dir()]
    assert len(fly_dirs) == 1, f"expected one unified fly, got {fly_dirs}"
    (fly_dir,) = fly_dirs
    assert (fly_dir / "Recording1_2P_Zstack" / "2P" / "Fly9_Zstack").is_dir()
    assert (fly_dir / "Recording2_2P_Streaming" / "2P" / "Fly9_Streaming").is_dir()


def test_sweep_reattributes_later_data_to_a_pure_2p_only_fly(tmp_path):
    # A fly with no behavior take ever, once promoted to its own Fly folder,
    # had no pairing record to be recognized by: later data of it went back to
    # the generic bucket or into a duplicate Fly.
    dest_root = tmp_path / "dest"
    source_root = tmp_path / "windows_share" / "MD"
    _make_bare_image_folder_2p(
        source_root, "exp1", "Fly1_Zstack", u_time="1000", mtime=1000
    )
    _make_bare_image_folder_2p(
        source_root, "exp1", "Fly1_004", u_time="2000", mtime=2000
    )
    config_dir = _rig_config(tmp_path, dest_root, source_root, settle_s=0)

    result = runner.invoke(app, ["twophoton", "sweep", "--config", str(config_dir)])
    assert result.exit_code == 0, result.output
    assert (dest_root / "2p_only" / "exp1").is_dir()

    result = _reconcile(config_dir, "--all")
    assert result.exit_code == 0, result.output
    (day_dir,) = [d for d in dest_root.iterdir() if d.is_dir() and d.name != "2p_only"]
    (fly_dir,) = [d for d in day_dir.iterdir() if d.is_dir()]
    assert not (dest_root / "2p_only").exists()

    # Later that day, a third folder of the same fly.
    _make_bare_image_folder_2p(
        source_root, "exp1", "Fly1_005", u_time="9000", mtime=9000
    )
    result = runner.invoke(app, ["twophoton", "sweep", "--config", str(config_dir)])
    assert result.exit_code == 0, result.output
    assert (fly_dir / "2P_only" / "Fly1_005").is_dir()
    assert not (dest_root / "2p_only").exists()
    assert [d for d in day_dir.iterdir() if d.is_dir() and d != fly_dir] == []

    # And a later --all pass still finds and renumbers this take-less fly.
    result = _reconcile(config_dir, "--all")
    assert result.exit_code == 0, result.output
    assert (fly_dir / "Recording3_2P" / "2P" / "Fly1_005").is_dir()


def test_reconcile_recordings_attributes_prefix_match_not_a_new_fly(tmp_path):
    # Real dry run: "Fly1_Zstack" extends the fly's known "Fly1": it joins
    # that fly, never becomes a new one.
    dest_root = tmp_path / "dest"
    # The day folder is dated like the synthetic epoch (same-day attribution).
    _write_take(
        dest_root,
        "700101_PAM7xCI63/Fly1/001",
        start_time_ns=1_000_000_000_000,
        duration_s=10.0,
        matched_paths=["PAM7xCI63/Fly1_001"],
    )
    orphan = dest_root / "2p_only" / "PAM7xCI63" / "260903" / "Fly1_Zstack"
    orphan.mkdir(parents=True)
    (orphan / "Experiment.xml").write_text(
        '<?xml version="1.0"?><ThorImageExperiment><Date date="x" uTime="500" />'
        "</ThorImageExperiment>"
    )
    result = _reconcile(_rig_config(tmp_path, dest_root), "--all")
    assert result.exit_code == 0, result.output
    fly1 = dest_root / "700101_PAM7xCI63" / "Fly1"
    wrapper = fly1 / "Recording1_2P_Zstack"
    assert (wrapper / "2P" / "Fly1_Zstack" / "Experiment.xml").exists()
    assert (fly1 / "Recording2_Synced" / "recording_summary.json").exists()
    assert not (dest_root / "700101_PAM7xCI63" / "Fly2").exists()


def test_reconcile_recordings_reuses_day_folder_despite_digit_mismatch(tmp_path):
    # Real dry run: the share's "PAM7xCI80" is octacam's "260903_PAM07xCI80"
    # (an extra zero); a substring check made a near-duplicate day folder.
    dest_root = tmp_path / "dest"
    existing = _write_take(
        dest_root,
        "260903_PAM07xCI80/Fly1/001",
        start_time_ns=1_000_000_000_000,
        duration_s=10.0,
    )
    orphan = dest_root / "2p_only" / "PAM7xCI80" / "260903" / "Fly2_Zstack"
    orphan.mkdir(parents=True)
    (orphan / "Experiment.xml").write_text(
        '<?xml version="1.0"?><ThorImageExperiment><Date date="x" uTime="2000" />'
        "</ThorImageExperiment>"
    )
    result = _reconcile(_rig_config(tmp_path, dest_root), "--all")
    assert result.exit_code == 0, result.output
    assert existing.parent.is_dir()
    new_fly = dest_root / "260903_PAM07xCI80" / "Fly2"
    wrapper = new_fly / "Recording1_2P_Zstack"
    assert (wrapper / "2P" / "Fly2_Zstack" / "Experiment.xml").exists()
    assert not (dest_root / "260903_PAM7xCI80").exists()


def test_reconcile_recordings_writes_append_only_log(tmp_path):
    dest_root = tmp_path / "dest"
    _write_take(
        dest_root, "day1/Fly1/001", start_time_ns=1_000_000_000_000, duration_s=10.0
    )
    config_dir = _rig_config(tmp_path, dest_root)
    result = _reconcile(config_dir, "--all")
    assert result.exit_code == 0, result.output
    log_path = dest_root / "day1" / "Fly1" / "reconciliation_log.md"
    first_text = log_path.read_text()
    assert "Recording1_Beh" in first_text
    assert "day1/Fly1/001" in first_text

    _write_take(
        dest_root, "day1/Fly1/002", start_time_ns=500_000_000_000, duration_s=10.0
    )
    result = _reconcile(config_dir, "--all")
    assert result.exit_code == 0, result.output
    second_text = log_path.read_text()
    assert second_text.startswith(first_text.split("\n\n", 1)[0])
    assert second_text.count("# Reconciliation log") == 1
    assert "day1/Fly1/001" in second_text
    assert "day1/Fly1/002" in second_text or "Recording1_Beh" in second_text


def test_reconcile_recordings_dry_run_writes_no_log(tmp_path):
    dest_root = tmp_path / "dest"
    _write_take(
        dest_root, "day1/Fly1/001", start_time_ns=1_000_000_000_000, duration_s=10.0
    )
    result = _reconcile(_rig_config(tmp_path, dest_root), "--dry-run", "--all")
    assert result.exit_code == 0, result.output
    assert not (dest_root / "day1" / "Fly1" / "reconciliation_log.md").exists()


def test_reconcile_puts_a_zstack_and_its_thorsync_in_one_session(tmp_path):
    # ThorSync is per session: a Z-stack's SyncData (one CaptureOn segment per
    # slice) belongs in the Z-stack's RecordingN (real 2026-10-14: Fly2's
    # SyncData171 became a Recording3_2P of its own).
    h5py = pytest.importorskip("h5py")
    import numpy as np

    dest_root = tmp_path / "dest"
    fly = dest_root / "261014_PAM7xCI80" / "Fly2"
    t0 = 1_791_000_000.0
    zstack = fly / "Recording1_2P_Zstack" / "2P" / "Fly2_Zstack"
    zstack.mkdir(parents=True)
    (zstack / "Experiment.xml").write_text(
        f'<?xml version="1.0"?><ThorImageExperiment><Date uTime="{int(t0)}" />'
        '<ZStage steps="3" enable="1" /><Streaming enable="0" />'
        '<Timelapse timepoints="1" /><LSM averageMode="0" /></ThorImageExperiment>'
    )
    _write_take(
        dest_root,
        "261014_PAM7xCI80/Fly2/Recording2_Synced_Streaming",
        start_time_ns=int((t0 + 300) * 1e9),
        duration_s=10.0,
        matched_paths=["PAM7xCI80/Fly2_Streaming"],
    )
    sync = fly / "Recording3_2P" / "2P" / "SyncData171"
    sync.mkdir(parents=True)
    capture = np.zeros((300, 1), np.uint32)
    frameout = np.zeros((300, 1), np.uint32)
    for k in range(3):  # 3 slices, 1 frame each
        capture[10 + 90 * k : 60 + 90 * k] = 1
        frameout[20 + 90 * k] = 1
    with h5py.File(sync / "Episode001.h5", "w") as f:
        di = f.create_group("DI")
        di.create_dataset("CaptureOn", data=capture)
        di.create_dataset("FrameOut", data=frameout)
        di.create_dataset("Cameras", data=np.zeros((300, 1), np.uint32))
    (sync / "ThorRealTimeDataSettings.xml").write_text("<ThorData/>")
    os.utime(sync / "ThorRealTimeDataSettings.xml", (t0 + 2, t0 + 2))

    result = _reconcile(
        _rig_config(tmp_path, dest_root), dest_root / "261014_PAM7xCI80"
    )
    assert result.exit_code == 0, result.output
    assert sorted(p.name for p in fly.iterdir() if p.is_dir()) == [
        "Recording1_2P_Zstack",
        "Recording2_Synced_Streaming",
    ]
    assert sorted(p.name for p in (fly / "Recording1_2P_Zstack" / "2P").iterdir()) == [
        "Fly2_Zstack",
        "SyncData171",
    ]
    assert "SyncData171" in (fly / "reconciliation_log.md").read_text()


# --- doctor -------------------------------------------------------------------


def _thorsync_settings_folder(root, name, duration):
    folder = root / name
    folder.mkdir(parents=True)
    (folder / "ThorRealTimeDataSettings.xml").write_text(
        '<?xml version="1.0"?><ThorData><AcquireBoard active="1" '
        f'duration="{duration}"><SampleRate enable="1" rate="100000" />'
        "</AcquireBoard></ThorData>"
    )
    return folder


@pytest.mark.parametrize(
    ("duration", "options", "warns"),
    [
        (320, {}, True),  # synced takes can run up to the 3600 s safety limit
        (7200, {}, False),
        (320, {"match_thorimage": False}, False),  # fixed 5 s takes
    ],
)
def test_doctor_checks_thorsync_duration_against_the_longest_take(
    tmp_path, duration, options, warns
):
    source = tmp_path / "share"
    _thorsync_settings_folder(source / "PAM7xCI80", "SyncData184", duration)
    config_dir = _rig_config(tmp_path, tmp_path / "nas", source, **options)
    # The plugins' checks alone: the whole command probes USB and serial.
    out = " ".join(plugin_doctor_text(config_dir).split())
    if warns:
        assert "ThorSync is set to stop after 320 s" in out
        assert "PAM7xCI80/SyncData184" in out and "60 min" in out
    else:
        assert "ThorSync is set to stop" not in out
        assert "covers the longest take" in out


def test_process_warns_about_a_take_thorsync_did_not_record(tmp_path):
    # Real 2026-10-20: ThorImage's ThorSync box unticked, a whole day of takes
    # paired with their images alone: no AirFlow, no signal check, no word.
    pytest.importorskip("tifffile")
    dest_root = tmp_path / "dest"
    source_root = tmp_path / "windows_share" / "MD"
    take_start = 1_000_000.0
    _make_image_folder_2p(
        source_root, "AllPAMxCI80", "Fly3_Streaming", u_time=take_start + 1, n_frames=4
    )
    folder = _armed_recording(
        tmp_path / "rec",
        source_root=source_root,
        dest_root=dest_root,
        take_start=take_start,
    )
    result = _process(folder)
    assert result.exit_code == 0, result.output
    out = _out(result)
    assert "WARNING" in out
    assert "no ThorSync recording for" in out
    assert "(ThorImage Fly3_Streaming)" in out
