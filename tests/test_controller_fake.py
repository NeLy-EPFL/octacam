"""Full RecordingController record cycle on the fake backend (no hardware/SDK).

Proves the shared controller/grab-loop/writer/CSV path works end to end without
PYLON_CAMEMU, driven by the same software-trigger timer as the real rig.
"""

import json
import os

os.environ.setdefault("OCTACAM_FAKE_CAMERAS", "FAKE-0,FAKE-1")

import pytest

from octacam.cameras import CameraSystem
from octacam.controller import RecordingController, RecordingSettings, StartResult
from octacam.transform import DisplayTransform

FAKE_SERIALS = ["FAKE-0", "FAKE-1"]


@pytest.fixture
def fake_system(tmp_path):
    system = CameraSystem(FAKE_SERIALS, backend="fake")
    assert len(system) == 2
    system.load_config(tmp_path)
    # Shrink the sensor so x264 encodes trivially fast in CI.
    for camera in system:
        camera.set_geometry(width=320, height=240)
    yield system
    system.close()


def test_fake_full_recording_cycle(fake_system, tmp_path):
    save_dir = tmp_path / "rec" / "001-trial"
    settings = RecordingSettings(fps=50.0, duration_s=1.0, save_dir=str(save_dir))
    controller = RecordingController(fake_system, settings, auto_preview=False)

    result = controller.start_recording()
    assert result.ok, result.message
    assert controller.start_recording().status == StartResult.BUSY

    controller.join(timeout=20)
    assert controller.state == "idle"

    videos = sorted(save_dir.glob("*.mkv"))
    assert len(videos) == 2
    for video in videos:
        assert video.stat().st_size > 0
        assert not video.with_suffix(".csv").exists()  # CSV is opt-in now

    summary = json.loads((save_dir / "recording_summary.json").read_text())
    assert len(summary["cameras"]) == 2
    # frames flowed via the software trigger (be lenient on the exact count)
    assert all(c["frames"] >= 20 for c in summary["cameras"])

    assert controller.get_settings().save_dir.endswith("002-trial")
    snapshot = controller.snapshot()
    assert all(c["frames"] > 0 for c in snapshot["cameras"])


def test_fake_recording_writes_csv_when_enabled(fake_system, tmp_path):
    save_dir = tmp_path / "rec" / "001"
    settings = RecordingSettings(
        fps=50.0, duration_s=1.0, save_dir=str(save_dir), save_frame_timestamps=True
    )
    controller = RecordingController(fake_system, settings, auto_preview=False)
    assert controller.start_recording().ok
    controller.join(timeout=20)

    for video in sorted(save_dir.glob("*.mkv")):
        csv_lines = video.with_suffix(".csv").read_text().splitlines()
        assert csv_lines[0] == "frame_index,timestamp,dropped"
        assert len(csv_lines) - 1 >= 20


def test_fake_recording_bakes_display_transform(fake_system, tmp_path):
    import cv2

    # A 90° rotation must swap the recorded video's width/height and be flagged
    # in the summary so transcode never re-applies it.
    for camera in fake_system:
        camera.display_transform = DisplayTransform(rotation_deg=90)

    save_dir = tmp_path / "rec" / "001"
    settings = RecordingSettings(
        fps=50.0, duration_s=1.0, save_dir=str(save_dir), record_form="display"
    )
    controller = RecordingController(fake_system, settings, auto_preview=False)
    assert controller.start_recording().ok
    controller.join(timeout=20)

    summary = json.loads((save_dir / "recording_summary.json").read_text())
    for cam in summary["cameras"]:
        assert cam["transform_applied"] is True
        assert cam["transform"]["rotation_deg"] == 90
        # sensor was 320x240; a 90° rotation records 240x320.
        assert (cam["width"], cam["height"]) == (240, 320)

    for video in sorted(save_dir.glob("*.mkv")):
        cap = cv2.VideoCapture(str(video))
        ok, frame = cap.read()
        cap.release()
        assert ok
        assert frame.shape[:2] == (320, 240)  # (height, width) after rotation


def test_fake_recording_notes_folder_in_session_cache(
    fake_system, tmp_path, monkeypatch
):
    # With a session id, each finished recording's folder is noted in the cache
    # so `octacam transcode --session` can rediscover the batch.
    from octacam import session_cache

    monkeypatch.setenv("OCTACAM_CACHE_DIR", str(tmp_path / "cache"))
    save_dir = tmp_path / "rec" / "001-bhv"
    settings = RecordingSettings(fps=50.0, duration_s=1.0, save_dir=str(save_dir))
    controller = RecordingController(
        fake_system, settings, auto_preview=False, session_id="sess-test"
    )
    assert controller.start_recording().ok
    controller.join(timeout=20)
    assert session_cache.session_folders("sess-test") == [save_dir.resolve()]


def test_fake_recording_without_session_id_skips_cache(
    fake_system, tmp_path, monkeypatch
):
    # No session id (the default for a directly-built controller) -> the cache
    # is untouched, so unit tests never write to the user cache dir.
    from octacam import session_cache

    monkeypatch.setenv("OCTACAM_CACHE_DIR", str(tmp_path / "cache"))
    save_dir = tmp_path / "rec" / "001"
    settings = RecordingSettings(fps=50.0, duration_s=1.0, save_dir=str(save_dir))
    controller = RecordingController(fake_system, settings, auto_preview=False)
    assert controller.start_recording().ok
    controller.join(timeout=20)
    assert session_cache.last_folder() is None


def test_fake_abort_recording(fake_system, tmp_path):
    import time

    save_dir = tmp_path / "abort" / "001"
    settings = RecordingSettings(fps=50.0, duration_s=60.0, save_dir=str(save_dir))
    controller = RecordingController(fake_system, settings, auto_preview=False)
    assert controller.start_recording().ok

    deadline = time.monotonic() + 10
    while controller.state != "recording" and time.monotonic() < deadline:
        time.sleep(0.05)
    assert controller.state == "recording"

    controller.stop_recording(abort=True)
    controller.join(timeout=20)
    assert controller.state == "idle"
    assert controller.get_settings().save_dir.endswith("001")  # not incremented
