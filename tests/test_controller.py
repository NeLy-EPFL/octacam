"""RecordingController tests: pure-unit + emulator integration."""

import os
import time

os.environ.setdefault("PYLON_CAMEMU", "2")

import pytest

from octacam.controller import (
    RecordingController,
    RecordingSettings,
    StartResult,
    increment_trailing_number,
    normalize_save_dir,
)

EMULATED_SERIALS = ["0815-0000", "0815-0001"]


# ------------------------------------------------------------------- units


def test_increment_trailing_number():
    assert increment_trailing_number("/data/001-bhv") == "/data/002-bhv"
    assert increment_trailing_number("/d/240101_/Fly1/009") == "/d/240101_/Fly1/010"
    assert increment_trailing_number("/data/run007/trial003") == "/data/run007/trial004"
    assert increment_trailing_number("/data/999") == "/data/1000"
    assert increment_trailing_number("/data/no-number") == "/data/no-number"


def test_normalize_save_dir():
    home = os.path.expanduser("~")
    assert normalize_save_dir(" ~/data ") == f"{home}/data"
    assert normalize_save_dir("/a/b").startswith("/a/b")


def test_update_settings_validation():
    controller = RecordingController.__new__(RecordingController)
    controller._settings = RecordingSettings()
    controller._state = "preview"
    controller._lock = __import__("threading").RLock()

    class _SystemStub:
        def set_software_trigger_frequency(self, hz):
            self.hz = hz

    controller.camera_system = _SystemStub()
    with pytest.raises(ValueError):
        controller.update_settings(codec="vp9")
    with pytest.raises(ValueError):
        controller.update_settings(fps=0)
    with pytest.raises(ValueError):
        controller.update_settings(trigger_source="quantum")
    with pytest.raises(ValueError):
        controller.update_settings(no_such_field=1)
    controller.update_settings(fps=42.0)
    assert controller.camera_system.hz == 42.0

    controller._state = "recording"
    with pytest.raises(RuntimeError):
        controller.update_settings(fps=10.0)


def test_video_format_carries_x264_options():
    settings = RecordingSettings(codec="x264", crf=20, preset="superfast")
    video_format = settings.video_format()
    assert (video_format.crf, video_format.preset) == (20, "superfast")
    assert RecordingSettings(codec="raw").video_format().extension == "raw"


# ------------------------------------------------- emulator integration


@pytest.fixture
def camera_system(tmp_path):
    from octacam.camera import CameraSystem

    system = CameraSystem(EMULATED_SERIALS)
    assert len(system) == 2, "PYLON_CAMEMU=2 expected"
    system.load_config(tmp_path)  # no .pfs files: emulator defaults
    yield system
    system.close()


def collect_states(controller):
    states = []
    controller.add_listener(
        lambda kind, payload: states.append(payload["state"])
        if kind == "state"
        else None
    )
    return states


def test_full_recording_cycle(camera_system, tmp_path):
    save_dir = tmp_path / "rec" / "001-trial"
    settings = RecordingSettings(
        fps=50.0, duration_s=1.5, save_dir=str(save_dir)
    )
    controller = RecordingController(
        camera_system, settings, auto_preview=False
    )
    states = collect_states(controller)

    result = controller.start_recording()
    assert result.ok, result.message
    busy = controller.start_recording()
    assert busy.status == StartResult.BUSY

    controller.join(timeout=20)
    assert controller.state == "idle"
    assert states[0] == "waiting"
    assert "recording" in states and states[-1] == "idle"

    videos = sorted(save_dir.glob("*.mkv"))
    assert len(videos) == 2
    for video in videos:
        assert video.stat().st_size > 0
        csv_lines = video.with_suffix(".csv").read_text().splitlines()
        assert csv_lines[0] == "frame_index,timestamp,dropped"
        assert 50 <= len(csv_lines) - 1 <= 100  # ~75 frames at 50 fps x 1.5 s

    # the save dir auto-incremented for the next trial
    assert controller.get_settings().save_dir.endswith("002-trial")

    snapshot = controller.snapshot()
    assert snapshot["state"] == "idle"
    assert len(snapshot["cameras"]) == 2
    assert all(c["frames"] > 0 for c in snapshot["cameras"])


def test_abort_recording(camera_system, tmp_path):
    save_dir = tmp_path / "abort" / "001"
    settings = RecordingSettings(
        fps=50.0, duration_s=60.0, save_dir=str(save_dir)
    )
    controller = RecordingController(
        camera_system, settings, auto_preview=False
    )
    assert controller.start_recording().ok

    deadline = time.monotonic() + 10
    while controller.state != "recording" and time.monotonic() < deadline:
        time.sleep(0.05)
    assert controller.state == "recording"

    controller.stop_recording(abort=True)
    controller.join(timeout=20)
    assert controller.state == "idle"
    # an aborted recording does not increment the save dir
    assert controller.get_settings().save_dir.endswith("001")
    assert any(e["message"] == "Recording aborted" for e in controller.events)


def test_plugin_hooks_fire_during_recording(camera_system, tmp_path):
    from octacam.plugins.base import Plugin, PluginManager

    calls = []

    class Spy(Plugin):
        name = "spy"

        def on_recording_start(self, params):
            calls.append(("start", params))

        def on_first_frame(self, params):
            calls.append(("first_frame", params))

        def on_recording_stop(self, aborted):
            calls.append(("stop", aborted))

    settings = RecordingSettings(
        fps=50.0, duration_s=1.0, save_dir=str(tmp_path / "rec" / "001")
    )
    controller = RecordingController(
        camera_system, settings, PluginManager([Spy()]), auto_preview=False
    )
    params = {"spy": {"value": 1}}
    assert controller.start_recording(plugin_params=params).ok
    controller.join(timeout=20)

    first_frames = [c for c in calls if c[0] == "first_frame"]
    assert len(first_frames) == 1, "on_first_frame must fire exactly once"
    assert first_frames[0][1] == params  # plugin slice threaded through
    assert ("start", params) in calls
    assert ("stop", False) in calls  # completed, not aborted


def test_needs_confirm_on_existing_dir(camera_system, tmp_path):
    save_dir = tmp_path / "exists"
    save_dir.mkdir()
    controller = RecordingController(
        camera_system,
        RecordingSettings(fps=50.0, duration_s=1.0, save_dir=str(save_dir)),
        auto_preview=False,
    )
    result = controller.start_recording()
    assert result.status == StartResult.NEEDS_CONFIRM
    assert controller.state == "idle"

    validation = controller.validate_save_dir(str(save_dir))
    assert validation["exists"] and validation["creatable"]
    assert validation["free_bytes"] > 0
