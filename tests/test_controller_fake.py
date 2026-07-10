"""Full RecordingController record cycle on the fake backend (no hardware/SDK).

Proves the shared controller/grab-loop/writer/timestamps path works end to end
without PYLON_CAMEMU, driven by the same software-trigger timer as the real rig.
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
    assert not (save_dir / "timestamps.npz").exists()  # timestamps are opt-in

    summary = json.loads((save_dir / "recording_summary.json").read_text())
    assert len(summary["cameras"]) == 2
    # frames flowed via the software trigger (be lenient on the exact count)
    assert all(c["frames"] >= 20 for c in summary["cameras"])

    assert controller.get_settings().save_dir.endswith("002-trial")
    snapshot = controller.snapshot()
    assert all(c["frames"] > 0 for c in snapshot["cameras"])


def test_writer_queue_size_reaches_each_writer(fake_system, tmp_path):
    # The config knob must flow settings -> CameraSystem -> Camera -> writer so a
    # deeper queue actually absorbs transient encoder stalls at record time.
    save_dir = tmp_path / "rec" / "001-trial"
    settings = RecordingSettings(
        fps=50.0, duration_s=1.0, save_dir=str(save_dir), writer_queue_size=7
    )
    controller = RecordingController(fake_system, settings, auto_preview=False)
    assert controller.start_recording().ok
    # Writers are open the moment start_recording() returns ok (created under the
    # controller lock), before the countdown ends — inspect them now.
    for camera in fake_system:
        assert camera._video_writer is not None
        assert camera._video_writer._max_queue_size == 7
    controller.join(timeout=20)


def test_fake_recording_bakes_process_params_into_snapshot(fake_system, tmp_path):
    from octacam._compat import tomllib
    from octacam.config_writer import write_config

    # A rig config the GUI has *not* edited on disk, but whose transcode/transfer
    # the operator overrode live in the Process section.
    config_dir = tmp_path / "cfg"
    write_config(
        config_dir,
        {
            "record": {"fps": 50.0, "directory": "~/data/%y%m%d"},
            "transcode": {"ffmpeg_params": "-c:v libx264 -crf 20"},
            "visualization": [{"name": "grid.mp4", "layout": [["FAKE-0", "FAKE-1"]]}],
            "transfer": {"directory": "~/store", "checksum": True},
        },
    )

    save_dir = tmp_path / "rec" / "001"
    settings = RecordingSettings(
        fps=50.0,
        duration_s=1.0,
        save_dir=str(save_dir),
        transcode_ffmpeg_params="-c:v ffv1 -level 3",
        transfer_directory="~/other-store",
        transfer_checksum=False,
    )
    controller = RecordingController(
        fake_system, settings, auto_preview=False, config_dir=config_dir
    )
    assert controller.start_recording().ok
    controller.join(timeout=20)

    # The live Process values land in the recording folder's config snapshot...
    snap = tomllib.loads((save_dir / "octacam_config.toml").read_text())
    assert snap["transcode"]["ffmpeg_params"] == "-c:v ffv1 -level 3"
    assert snap["transfer"] == {"directory": "~/other-store", "checksum": False}
    # ...while the untouched sections survive the patched re-emit and the
    # directory template stays unexpanded (a `~`/strftime path, not a date).
    assert snap["visualization"] == [
        {"name": "grid.mp4", "layout": [["FAKE-0", "FAKE-1"]]}
    ]
    assert snap["record"]["directory"] == "~/data/%y%m%d"


def test_fake_recording_writes_timestamps_when_enabled(fake_system, tmp_path):
    import numpy as np

    save_dir = tmp_path / "rec" / "001"
    settings = RecordingSettings(
        fps=50.0, duration_s=1.0, save_dir=str(save_dir), save_frame_timestamps=True
    )
    controller = RecordingController(fake_system, settings, auto_preview=False)
    assert controller.start_recording().ok
    controller.join(timeout=20)

    # One compressed file for the whole recording (no per-camera CSVs).
    assert not any(save_dir.glob("*.csv"))
    with np.load(save_dir / "timestamps.npz") as data:
        for serial in FAKE_SERIALS:
            timestamps = data[f"{serial}/timestamp_ns"]
            dropped = data[f"{serial}/dropped"]
            assert timestamps.dtype == np.int64
            assert dropped.dtype == np.bool_
            assert len(timestamps) == len(dropped) >= 20
            # Software-trigger cadence is monotonic non-decreasing.
            assert np.all(np.diff(timestamps) >= 0)

    # The fake backend supplies a (nonzero) timestamp for every frame, so nothing
    # falls back to host time — the summary records that provenance. The 0 -> host
    # fallback path (pycameleon) is covered by test_timestamp_source_derivation.
    summary = json.loads((save_dir / "recording_summary.json").read_text())
    for cam in summary["cameras"]:
        assert cam["timestamp_source"] == "hardware"
        assert cam["host_fallback_count"] == 0


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
    # so `octacam process --last session` can rediscover the batch.
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


def test_teardown_preview_rearm_failure_goes_idle(fake_system, tmp_path, monkeypatch):
    """A camera dropping out mid-recording can make the teardown preview re-arm
    raise; the controller must still fire on_recording_stop and reach a terminal,
    non-active state (idle) rather than wedging in "finishing" forever."""
    from octacam.plugins.base import Plugin, PluginManager

    stopped: list[bool] = []

    class Spy(Plugin):
        name = "spy"

        def on_recording_stop(self, aborted):
            stopped.append(aborted)

    settings = RecordingSettings(
        fps=50.0, duration_s=0.5, save_dir=str(tmp_path / "rec" / "001")
    )
    # auto_preview=True so teardown re-arms preview; make that arm raise.
    controller = RecordingController(
        fake_system, settings, PluginManager([Spy()]), auto_preview=True
    )

    def boom(*a, **k):
        raise RuntimeError("camera dropped during re-arm")

    monkeypatch.setattr(fake_system, "start_preview", boom)

    assert controller.start_recording().ok
    controller.join(timeout=20)

    assert controller.state == "idle"  # not stuck in "finishing"
    assert not controller.recording_active
    assert stopped == [False]  # on_recording_stop still fired (plugin disarmed)


def test_start_recording_errors_when_start_record_raises(
    fake_system, tmp_path, monkeypatch
):
    """A non-BackendError escaping camera_system.start_record must not orphan
    partially-started cameras: start_recording tears them down, re-arms preview,
    and returns ERROR instead of letting the exception escape."""
    settings = RecordingSettings(
        fps=50.0, duration_s=1.0, save_dir=str(tmp_path / "rec" / "001")
    )
    controller = RecordingController(fake_system, settings, auto_preview=False)

    def boom(*a, **k):
        raise RuntimeError("bus dropped mid-start")

    monkeypatch.setattr(fake_system, "start_record", boom)

    result = controller.start_recording()
    assert result.status == StartResult.ERROR
    assert "bus dropped mid-start" in result.message
    assert controller.state == "idle"
    assert not controller.recording_active
    # No orphaned recording cameras left grabbing.
    for camera in fake_system:
        assert not camera.backend.is_grabbing()


def test_teardown_gate_blocks_a_racing_start(fake_system, tmp_path):
    """While a finished recording's off-lock teardown tail is still running
    (on_recording_stop + preview re-arm), the state has already left the active
    set — a new start must still be refused (BUSY) so it cannot race the previous
    recording's disarm over the shared trigger plugin."""
    import threading as _t

    from octacam.plugins.base import Plugin, PluginManager

    in_stop = _t.Event()
    release = _t.Event()

    class Gate(Plugin):
        name = "gate"

        def on_recording_stop(self, aborted):
            in_stop.set()
            release.wait(10)  # hold the teardown tail open

    settings = RecordingSettings(
        fps=50.0, duration_s=0.3, save_dir=str(tmp_path / "rec" / "001")
    )
    controller = RecordingController(
        fake_system, settings, PluginManager([Gate()]), auto_preview=False
    )
    assert controller.start_recording().ok
    # Teardown reaches on_recording_stop only after the state left the active set.
    assert in_stop.wait(20)
    assert not controller.recording_active  # state already flipped to idle...
    # ...yet a racing start is still refused while the tail runs.
    assert controller.start_recording().status == StartResult.BUSY

    release.set()
    controller.join(timeout=20)
    assert controller.state == "idle"
    # Once the tail finishes the gate clears and a start is accepted again.
    assert controller.start_recording(confirm_overwrite=True).ok
    controller.stop_recording(abort=True)
    controller.join(timeout=20)


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
