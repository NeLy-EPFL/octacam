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


def test_fake_external_trigger_waits_indefinitely(fake_system, tmp_path, monkeypatch):
    """External trigger must never auto-start: it waits for the first frame.

    With the software trigger a stalled camera is given up on after
    STARTED_FAIL_AFTER_S and the countdown starts anyway. With an external
    trigger no frame arrives until the external source fires, so the monitor
    must wait indefinitely (here: stay in "waiting" well past a shrunk fail
    threshold) and only start once a frame is actually delivered.
    """
    import time

    import octacam.controller as controller_module

    # Shrink the thresholds so the *old* auto-start behaviour would trigger
    # almost immediately; if the fix works the controller still won't start.
    monkeypatch.setattr(controller_module, "STARTED_WARN_AFTER_S", 0.1)
    monkeypatch.setattr(controller_module, "STARTED_FAIL_AFTER_S", 0.3)

    save_dir = tmp_path / "ext" / "001"
    settings = RecordingSettings(
        fps=50.0, duration_s=30.0, save_dir=str(save_dir), trigger_source="external"
    )
    controller = RecordingController(fake_system, settings, auto_preview=False)

    assert controller.start_recording().ok
    # The fake backend only yields a frame when trigger_once() is called, and
    # external mode never starts the software trigger, so no frame arrives.
    # Wait well past STARTED_FAIL_AFTER_S; the recording must not have started.
    time.sleep(1.0)
    assert controller.state == "waiting"
    messages = [e["message"] for e in controller.events]
    assert not any("starting the countdown anyway" in m for m in messages)
    assert any("Waiting for the external trigger" in m for m in messages)

    # Now the external source "fires": frames arrive and recording begins.
    deadline = time.monotonic() + 10
    while controller.state == "waiting" and time.monotonic() < deadline:
        for camera in fake_system:
            camera.trigger_once()
        time.sleep(0.05)
    assert controller.state == "recording"

    controller.stop_recording(abort=True)
    controller.join(timeout=20)
    assert controller.state == "idle"


def test_fake_zero_frame_recording_is_flagged(fake_system, tmp_path):
    """A capture that yields no frames (external trigger that never fired) must
    be flagged loudly, not reported as a silent success.

    Reproduces the failure mode where the first external-trigger recording ran
    its window with no pulses: every camera wrote a header-only file with 0
    frames, yet aborted/writer_failed stayed False so it looked successful.
    """
    import time

    save_dir = tmp_path / "ext" / "001"
    settings = RecordingSettings(
        fps=50.0, duration_s=30.0, save_dir=str(save_dir), trigger_source="external"
    )
    controller = RecordingController(fake_system, settings, auto_preview=False)

    assert controller.start_recording().ok
    # No frame is ever delivered (external mode + no trigger_once), so the
    # monitor stays in "waiting"; stop it normally (not an abort), as an
    # operator who gave up waiting would.
    time.sleep(0.5)
    assert controller.state == "waiting"
    controller.stop_recording(abort=False)
    controller.join(timeout=20)

    # The summary still records the empty capture (not an abort, 0 frames)...
    summary = json.loads((save_dir / "recording_summary.json").read_text())
    assert summary["aborted"] is False
    assert all(c["frames"] == 0 for c in summary["cameras"])
    # ...but the controller now emits a loud zero-frame error, naming the
    # likely cause, so it is no longer a silent success.
    errors = [e["message"] for e in controller.events if e["level"] == "error"]
    assert any("captured 0 frames" in m for m in errors)
    assert any("external trigger" in m.lower() for m in errors)


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
