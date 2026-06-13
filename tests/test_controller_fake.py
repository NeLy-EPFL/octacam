"""Full RecordingController record cycle on the fake backend (no hardware/SDK).

Proves the shared controller/grab-loop/writer/CSV path works end to end without
PYLON_CAMEMU, driven by the same software-trigger timer as the real rig.
"""

import os

os.environ.setdefault("OCTACAM_FAKE_CAMERAS", "FAKE-0,FAKE-1")

import pytest

from octacam.cameras import CameraSystem
from octacam.controller import RecordingController, RecordingSettings, StartResult

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
        csv_lines = video.with_suffix(".csv").read_text().splitlines()
        assert csv_lines[0] == "frame_index,timestamp,dropped"
        # frames flowed via the software trigger (be lenient on the exact count)
        assert len(csv_lines) - 1 >= 20

    assert controller.get_settings().save_dir.endswith("002-trial")
    snapshot = controller.snapshot()
    assert all(c["frames"] > 0 for c in snapshot["cameras"])


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
