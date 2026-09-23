"""End-to-end pulse accounting on the fake backend: missed pulses are detected,
filled and reported; every camera's video frame k is trigger pulse k.

The fake models the hardware failure modes the hexaview rig showed (see
octacam.cameras.fake): a camera that misses given triggers, a camera that ignores
its first triggers after acquisition start (a FLIR Grasshopper3 ignores two), and
a camera whose frames carry a hardware-clock timestamp instead of the trigger's
sequence number — so both the sequence and the timestamp paths are exercised.
"""

import json
import os
import threading
import time

os.environ.setdefault("OCTACAM_FAKE_CAMERAS", "FAKE-0,FAKE-1")

import numpy as np
import pytest

from octacam.cameras import CameraSystem
from octacam.controller import RecordingController, RecordingSettings
from octacam.plugins.base import Plugin, PluginManager

FAKE_SERIALS = ["FAKE-0", "FAKE-1"]
W, H = 64, 48
FPS = 50.0
PERIOD_NS = int(1e9 / FPS)


@pytest.fixture
def fake_system(tmp_path):
    system = CameraSystem(FAKE_SERIALS, backend="fake")
    system.load_config(tmp_path)
    for camera in system:
        camera.set_geometry(width=W, height=H)
    yield system
    system.close()


def _backend(system, serial):
    return next(c for c in system if c.serial_number == serial).backend


def _record(system, tmp_path, *, plugins=None, **overrides):
    save_dir = tmp_path / "rec" / "001"
    fields = {
        "fps": FPS,
        "duration_s": 1.0,
        "save_dir": str(save_dir),
        "save_method": "raw",  # exact pixels: a filled frame must equal its neighbor
        "save_frame_timestamps": True,
        **overrides,
    }
    settings = RecordingSettings(**fields)
    controller = RecordingController(
        system, settings, plugins=plugins, auto_preview=False
    )
    result = controller.start_recording(
        plugin_params={"board": {}} if plugins is not None else None
    )
    assert result.ok, result.message
    controller.join(timeout=30)
    summary = json.loads((save_dir / "recording_summary.json").read_text())
    with np.load(save_dir / "timestamps.npz") as data:
        arrays = {k: data[k] for k in data.files}
    return save_dir, summary, arrays, controller


def _frames(save_dir, name):
    raw = np.fromfile(save_dir / f"{name}.raw", dtype=np.uint8)
    return raw.reshape(-1, H, W)


def _cam(summary, name):
    return next(c for c in summary["cameras"] if c["name"] == name)


class Board(Plugin):
    """A stand-in trigger board: emits a counted train by firing every camera's
    trigger at the period, primes with sacrificial pulses, like the triggerbox."""

    name = "board"

    def __init__(self, system, count, period_ns=PERIOD_NS):
        self.system = system
        self.count = count
        self.period_s = period_ns / 1e9
        self.period_ns = period_ns
        self.primed = 0
        self._thread = None

    def trigger_train(self, params):
        return {"period_ns": self.period_ns, "count": self.count}

    def prime_trigger(self, params, pulses):
        self._pulse(pulses)
        self.primed += pulses
        return True

    def on_recording_start(self, params):
        self._thread = threading.Thread(target=self._pulse, args=(self.count,))
        self._thread.start()

    def on_recording_stop(self, aborted):
        if self._thread is not None:
            self._thread.join()

    def _pulse(self, n):
        for _ in range(n):
            for camera in self.system:
                camera.trigger_once()
            time.sleep(self.period_s)


def test_software_missed_pulses_are_filled_and_reported(fake_system, tmp_path):
    _backend(fake_system, "FAKE-1").miss_triggers = {10, 11, 30}
    save_dir, summary, arrays, _ = _record(
        fake_system, tmp_path, trigger_source="software"
    )
    ok, bad = _cam(summary, "FAKE-0"), _cam(summary, "FAKE-1")
    assert ok["frames"] == bad["frames"] == 50  # both end on the train's last pulse
    assert ok["missed_pulses"] == 0 and ok["dropped"] == 0
    assert bad["missed_pulse_indices"] == [10, 11, 30]
    assert bad["dropped"] == 3 and bad["dropped_indices"] == [10, 11, 30]
    assert summary["pulse_train"]["count"] == 50
    assert summary["pulse_train"]["primed"] > 0
    assert summary["schema_version"] == 4
    np.testing.assert_array_equal(arrays["FAKE-1/pulse_index"], np.arange(50))
    assert np.flatnonzero(arrays["FAKE-1/missed"]).tolist() == [10, 11, 30]
    assert np.flatnonzero(arrays["FAKE-1/dropped"]).tolist() == [10, 11, 30]
    # The video holds one frame per pulse; a missed pulse repeats its predecessor.
    video = _frames(save_dir, "FAKE-1")
    assert len(video) == 50
    assert (video[10] == video[9]).all() and (video[11] == video[9]).all()
    assert (video[30] == video[29]).all()
    assert not (video[12] == video[9]).all()
    assert len(_frames(save_dir, "FAKE-0")) == 50


def test_a_missed_pulse_is_found_from_hardware_timestamps(fake_system, tmp_path):
    # Frames carry a hardware-clock timestamp, not the trigger sequence: the
    # interval across the missed pulse is two periods.
    for serial in FAKE_SERIALS:
        _backend(fake_system, serial).hardware_period_ns = PERIOD_NS
    _backend(fake_system, "FAKE-0").miss_triggers = {7}
    save_dir, summary, arrays, _ = _record(
        fake_system, tmp_path, trigger_source="software"
    )
    assert _cam(summary, "FAKE-0")["missed_pulse_indices"] == [7]
    assert _cam(summary, "FAKE-1")["missed_pulses"] == 0
    assert _cam(summary, "FAKE-0")["frames"] == _cam(summary, "FAKE-1")["frames"] == 50
    # The filled frame is time-stamped when its pulse was due.
    ts = arrays["FAKE-0/timestamp_ns"]
    assert np.all(np.diff(ts) == PERIOD_NS)


def test_priming_absorbs_the_triggers_a_camera_ignores_after_start(
    fake_system, tmp_path
):
    # A GS3 ignores its first two triggers after acquisition start; without
    # priming, frame 0 would be pulse 2 and the video two frames short.
    for serial in FAKE_SERIALS:
        backend = _backend(fake_system, serial)
        backend.hardware_period_ns = PERIOD_NS
        backend.ignore_first_triggers = 2
    board = Board(fake_system, count=50)
    _save_dir, summary, arrays, _ = _record(
        fake_system, tmp_path, plugins=PluginManager([board]), trigger_source="managed"
    )
    assert board.primed == 4
    for serial in FAKE_SERIALS:
        cam = _cam(summary, serial)
        assert cam["frames"] == 50 and cam["missed_pulses"] == 0, cam
        assert cam["primed_frames"] == 2  # the 4 priming pulses minus the 2 ignored
    assert summary["sync"]["ok"], summary["sync"]
    assert summary["pulse_train"]["source"] == "managed"


def test_managed_train_fills_a_miss_and_stops_on_the_pulse_count(
    fake_system, tmp_path
):
    for serial in FAKE_SERIALS:
        _backend(fake_system, serial).hardware_period_ns = PERIOD_NS
    _backend(fake_system, "FAKE-1").miss_triggers = {3, 24}
    # A 25-pulse train in a recording nominally 5 s long: it ends with the train.
    board = Board(fake_system, count=25)
    started = time.monotonic()
    save_dir, summary, _arrays, _ = _record(
        fake_system,
        tmp_path,
        plugins=PluginManager([board]),
        trigger_source="managed",
        duration_s=5.0,
    )
    assert time.monotonic() - started < 4.0
    bad = _cam(summary, "FAKE-1")
    # 24 is the train's last pulse: filled at the end so both cameras end on it.
    assert bad["missed_pulse_indices"] == [3, 24]
    assert bad["frames"] == _cam(summary, "FAKE-0")["frames"] == 25
    assert len(_frames(save_dir, "FAKE-1")) == 25
    assert any("FAKE-1 missed 2 trigger pulse" in w for w in summary["sync"]["warnings"])
    assert summary["sync"]["ok"]  # filled: still aligned


def test_external_trigger_misses_are_reported_not_filled(fake_system, tmp_path):
    for serial in FAKE_SERIALS:
        _backend(fake_system, serial).hardware_period_ns = PERIOD_NS
    _backend(fake_system, "FAKE-0").miss_triggers = {4}
    save_dir = tmp_path / "rec" / "001"
    settings = RecordingSettings(
        fps=FPS,
        duration_s=30.0,
        save_dir=str(save_dir),
        save_method="raw",
        save_frame_timestamps=True,
        trigger_source="external",
    )
    controller = RecordingController(fake_system, settings, auto_preview=False)
    assert controller.start_recording().ok
    time.sleep(0.3)
    for _ in range(20):  # the external source: 20 pulses
        for camera in fake_system:
            camera.trigger_once()
        time.sleep(PERIOD_NS / 1e9)
    time.sleep(0.3)
    controller.stop_recording()
    controller.join(timeout=30)
    summary = json.loads((save_dir / "recording_summary.json").read_text())
    assert _cam(summary, "FAKE-0")["frames"] == 19  # not filled
    assert _cam(summary, "FAKE-0")["missed_pulse_indices"] == [4]
    assert not summary["sync"]["ok"]
    with np.load(save_dir / "timestamps.npz") as data:
        pulses = data["FAKE-0/pulse_index"].tolist()
    assert pulses == [p for p in range(20) if p != 4]


def test_a_frame_the_writer_refuses_is_filled(fake_system, tmp_path):
    camera = next(c for c in fake_system if c.serial_number == "FAKE-0")
    original = camera.start_record

    def start_record(*args, **kwargs):
        ok = original(*args, **kwargs)
        writer = camera._video_writer
        write = writer.write
        calls = {"n": 0}

        def refusing(frame, fill_before=0):
            calls["n"] += 1
            return False if calls["n"] == 20 else write(frame, fill_before)

        writer.write = refusing
        return ok

    camera.start_record = start_record
    save_dir, summary, arrays, _ = _record(
        fake_system, tmp_path, trigger_source="software"
    )
    cam = _cam(summary, "FAKE-0")
    assert cam["writer_dropped"] == 1 and cam["missed_pulses"] == 0
    assert cam["dropped_indices"] == [19] and cam["frames"] == 50
    video = _frames(save_dir, "FAKE-0")
    assert len(video) == 50 and (video[19] == video[18]).all()
    # delivered by the camera, refused by the writer: dropped but not missed
    assert arrays["FAKE-0/dropped"][19] and not arrays["FAKE-0/missed"][19]


def test_live_misses_reach_the_operator_as_events(fake_system, tmp_path):
    _backend(fake_system, "FAKE-1").miss_triggers = {5}
    _save_dir, _summary, _arrays, controller = _record(
        fake_system, tmp_path, trigger_source="software"
    )
    messages = [e["message"] for e in controller.events if e["level"] == "warning"]
    assert any("FAKE-1 missed" in m for m in messages), messages


def test_transport_counters_cover_only_the_recording(fake_system, tmp_path):
    # Model an SDK with both kinds of counter: one that restarts with every
    # acquisition (Spinnaker's delivered count) and one that runs on for the
    # life of the stream (its lost count). A preview before the recording must
    # not leak into either.
    backend = _backend(fake_system, "FAKE-0")
    counters = {"per_acquisition": 0, "cumulative": 7}
    original_start = backend.start_grab_record
    original_retrieve = backend.retrieve

    def start_grab_record():
        counters["per_acquisition"] = 0
        return original_start()

    def retrieve(timeout_ms, wants_array):
        frame = original_retrieve(timeout_ms, wants_array)
        if frame is not None:
            counters["per_acquisition"] += 1
        return frame

    backend.start_grab_record = start_grab_record
    backend.retrieve = retrieve
    backend.stream_statistics = lambda: {
        "Delivered": counters["per_acquisition"],
        "Lost": counters["cumulative"],
    }
    counters["per_acquisition"] = 500  # left over from a preview acquisition
    _save_dir, summary, _arrays, _ = _record(
        fake_system, tmp_path, trigger_source="software"
    )
    stream = _cam(summary, "FAKE-0")["stream"]
    primed = _cam(summary, "FAKE-0")["primed_frames"]
    assert stream == {"Delivered": 50 + primed, "Lost": 0}
