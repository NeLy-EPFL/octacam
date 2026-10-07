"""Synced behavior + 2P quick-look render — a fully synthetic transferred
take (tiny ffmpeg-generated grid, tifffile stacks, h5py ThorSync episode)."""

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

h5py = pytest.importorskip("h5py")
tifffile = pytest.importorskip("tifffile")
import numpy as np  # noqa: E402

from octacam.synced_render import (  # noqa: E402
    SYNCED_VIDEO_FILENAME,
    SyncedRenderOptions,
    channel_stacks,
    find_synced_inputs,
    options_from_config,
    render_synced_video,
)
from octacam.writer import find_ffmpeg  # noqa: E402

RATE = 1000  # DAQ samples/s, written into the settings XML


def _edges(start: int, n: int, step: int) -> list[int]:
    return [start + i * step for i in range(n)]


def _make_take(
    tmp_path: Path, *, planes: int, timepoints: int, behavior_frames: int = 40
) -> Path:
    take = tmp_path / "Fly1" / "001"
    (take / "Renderings").mkdir(parents=True)
    subprocess.run(
        [find_ffmpeg(), "-v", "error", "-y", "-f", "lavfi",
         "-i", f"testsrc=size=96x64:rate=20:duration={behavior_frames / 20}",
         "-pix_fmt", "yuv420p", str(take / "Renderings" / "grid.mp4")],
        check=True,
    )
    (take / "recording_summary.json").write_text(
        json.dumps({"cameras": [{"name": "camera_F", "frames": behavior_frames}]})
    )
    (take / "twophoton_match.json").write_text(
        json.dumps({"matched": [
            {"path": "exp/SyncData001", "kind": "sync"},
            {"path": "exp/Fly1_Streaming", "kind": "image"},
        ]})
    )

    image = take / "2P" / "Fly1_Streaming"
    image.mkdir(parents=True)
    fastz = planes > 1
    (image / "Experiment.xml").write_text(
        '<?xml version="1.0"?><ThorImageExperiment><Date date="x" uTime="1" />'
        '<LSM pixelX="32" pixelY="16" widthUM="64" heightUM="32" />'
        f'<ZStage steps="{planes}" enable="{int(fastz)}" />'
        f'<Streaming enable="1" zFastEnable="{int(fastz)}" flybackFrames="1" />'
        f'<Timelapse timepoints="{timepoints}" /></ThorImageExperiment>'
    )
    rng = np.random.default_rng(0)
    for chan in ("ChanA", "ChanB"):
        for z in range(1, planes + 1):
            name = f"{chan}_Z{z:02d}.tif" if fastz else f"{chan}.tif"
            tifffile.imwrite(
                image / name, rng.integers(0, 4000, (timepoints, 16, 32), dtype=np.uint16)
            )
    (image / "ChanA_Preview.tif").write_bytes(b"ignored")

    sync = take / "2P" / "SyncData001"
    sync.mkdir()
    per_volume = planes + 1 if fastz else 1
    total = 3000
    capture = np.zeros((total, 1), np.uint32)
    capture[100:2900] = 1
    cameras = np.zeros((total, 1), np.uint32)
    frameout = np.zeros((total, 1), np.uint32)
    for e in _edges(150, behavior_frames, 50):  # 20 Hz behavior
        cameras[e] = 1
    for e in _edges(150, timepoints * per_volume, 2000 // (timepoints * per_volume)):
        frameout[e] = 1
    with h5py.File(sync / "Episode001.h5", "w") as f:
        di = f.create_group("DI")
        di.create_dataset("CaptureOn", data=capture)
        di.create_dataset("Cameras", data=cameras)
        di.create_dataset("FrameOut", data=frameout)
    (sync / "ThorRealTimeDataSettings.xml").write_text(
        '<?xml version="1.0"?><ThorData><AcquireBoard>'
        '<DataChannel alias="CaptureOn" enable="1" />'
        f'<SampleRate enable="1" rate="{RATE}" /></AcquireBoard></ThorData>'
    )
    return take


def _frame_count(video: Path) -> int:
    out = subprocess.run(
        [find_ffmpeg(), "-i", str(video), "-map", "0:v", "-f", "null", "-"],
        capture_output=True, text=True,
    ).stderr
    return int(out.rsplit("frame=", 1)[1].split()[0])


@pytest.mark.parametrize("planes", [1, 3])
def test_renders_plain_and_fastz_takes(tmp_path, planes):
    take = _make_take(tmp_path, planes=planes, timepoints=10)
    if planes > 1:
        assert len(channel_stacks(take / "2P" / "Fly1_Streaming")["ChanA"]) == planes
    out = render_synced_video(take, SyncedRenderOptions(fps=10, height=64))
    assert out == take / "Renderings" / SYNCED_VIDEO_FILENAME
    # 40 behavior edges, 50 samples apart at 1 kHz = 1.95 s first-to-last.
    assert _frame_count(out) == 20
    assert not list((take / "Renderings").glob("*.partial.mp4"))


def test_existing_output_is_kept_unless_forced(tmp_path):
    take = _make_take(tmp_path, planes=1, timepoints=10)
    out = take / "Renderings" / SYNCED_VIDEO_FILENAME
    out.write_bytes(b"old")
    assert render_synced_video(take) == out
    assert out.read_bytes() == b"old"
    assert render_synced_video(take, SyncedRenderOptions(fps=10, height=64), force=True) == out
    assert out.read_bytes() != b"old"


def test_take_without_image_match_is_skipped(tmp_path):
    take = _make_take(tmp_path, planes=1, timepoints=10)
    (take / "twophoton_match.json").write_text(
        json.dumps({"matched": [{"path": "exp/SyncData001", "kind": "sync"}]})
    )
    assert isinstance(find_synced_inputs(take), str)
    assert render_synced_video(take) is None
    assert not (take / "Renderings" / SYNCED_VIDEO_FILENAME).exists()


def test_options_from_config():
    assert options_from_config(None) is None
    assert options_from_config(SimpleNamespace(render_synced_video=False)) is None
    opts = options_from_config(
        SimpleNamespace(
            render_synced_video=True,
            synced_green_channel="ChanB",
            synced_red_channel="ChanA",
            synced_fastz_projection="max",
        )
    )
    assert opts is not None
    assert (opts.green_channel, opts.red_channel, opts.fastz_projection) == ("ChanB", "ChanA", "max")
