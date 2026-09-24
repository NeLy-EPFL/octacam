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
from typing import Any

os.environ.setdefault("OCTACAM_FAKE_CAMERAS", "FAKE-0,FAKE-1")

import numpy as np
import pytest

import octacam.cameras._trigger_handoff as handoff
import octacam.controller as controller_module
from octacam.cameras import CameraSystem
from octacam.check import check_recording
from octacam.controller import RecordingController, RecordingSettings
from octacam.plugins.base import Plugin, PluginManager
from octacam.pulses import PulseClock
from octacam.writer import FORMATS

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
    assert summary["completed"] is True
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


def _ignoring_cameras(system, ignored):
    for serial, count in zip(FAKE_SERIALS, ignored, strict=True):
        backend = _backend(system, serial)
        backend.hardware_period_ns = PERIOD_NS
        backend.ignore_first_triggers = count


@pytest.mark.parametrize("source", ["managed", "software"])
def test_priming_repeats_until_every_camera_has_answered(fake_system, tmp_path, source):
    # Two GS3s fresh from a power-up answered none of four priming pulses, and
    # most likely not the train's first pulse either: one round would leave
    # FAKE-1 starting the recording on pulse 1 (a pulse behind FAKE-0, and its
    # last pulse "missed"). A second round gets it past what it ignores.
    _ignoring_cameras(fake_system, [2, 5])
    board = Board(fake_system, count=50)
    _save_dir, summary, _arrays, controller = _record(
        fake_system,
        tmp_path,
        plugins=PluginManager([board]) if source == "managed" else None,
        trigger_source=source,
    )
    if source == "managed":
        assert board.primed == 8
    assert summary["pulse_train"]["primed"] == 8
    assert _cam(summary, "FAKE-0")["primed_frames"] == 6
    assert _cam(summary, "FAKE-1")["primed_frames"] == 3
    for serial in FAKE_SERIALS:
        cam = _cam(summary, serial)
        assert cam["frames"] == 50 and cam["missed_pulses"] == 0, cam
        assert cam["start_offset_pulses"] == 0, cam
    assert summary["sync"]["ok"], summary["sync"]
    assert not [e for e in controller.events if "priming" in e["message"]]


def test_software_priming_absorbs_triggers_a_camera_silently_ignores(
    fake_system, tmp_path
):
    # A real Grasshopper3 ignores its first software triggers after acquisition
    # start without a word (no image, no error), like its hardware ones. Before
    # counting, each ignored trigger must cost only a short answer deadline: on
    # the rig the long one made priming answer nothing, and the stale priming
    # trigger then blocked the train's first 37 pulses (missed 2-38 at 50 fps).
    for serial in FAKE_SERIALS:
        backend = _backend(fake_system, serial)
        backend.ignore_first_triggers = 2
        backend.ignore_first_silently = True
    started = time.monotonic()
    _save_dir, summary, _arrays, controller = _record(
        fake_system, tmp_path, trigger_source="software"
    )
    elapsed = time.monotonic() - started
    for serial in FAKE_SERIALS:
        cam = _cam(summary, serial)
        assert cam["frames"] == 50 and cam["missed_pulses"] == 0, cam
        assert cam["primed_frames"] == 2, cam  # 4 priming triggers, 2 ignored
    assert summary["pulse_train"]["primed"] == 4  # one round was enough
    assert summary["sync"]["ok"], summary["sync"]
    assert not [e for e in controller.events if "priming" in e["message"]]
    # Priming cost two short deadlines, not two seconds: a 1 s take, a few
    # hundred ms of start-up.
    assert elapsed < 3.0, elapsed


def test_software_priming_with_a_real_fetch_leaves_the_first_pulse_intact(
    fake_system, tmp_path, monkeypatch
):
    # As above, but a fetch that finds no image blocks for its whole timeout, as a
    # real SDK's does. The ignored triggers open a drain window (made long here
    # so counting starts inside it); were counting not to end it, the train's
    # first trigger would wait out a drain fetch and be dropped as stale — pulse
    # 0 missed at the start of every software take.
    monkeypatch.setattr(handoff, "DRAIN_WINDOW_S", 2.0)
    for serial in FAKE_SERIALS:
        backend = _backend(fake_system, serial)
        backend.ignore_first_triggers = 2
        backend.ignore_first_silently = True
        backend.fetch_blocks = True
    _save_dir, summary, _arrays, _ = _record(
        fake_system, tmp_path, trigger_source="software"
    )
    for serial in FAKE_SERIALS:
        cam = _cam(summary, serial)
        assert cam["frames"] == 50 and cam["missed_pulses"] == 0, cam
        assert cam["primed_frames"] == 2, cam
    assert summary["sync"]["ok"], summary["sync"]


def test_a_long_exposure_keeps_every_image_on_its_own_pulse(fake_system, tmp_path):
    # 5 fps, each image ready 150 ms after its trigger (a ~140 ms exposure plus
    # a GS3's transfer): longer than a fixed 0.1 s answer deadline, which gave
    # the first priming trigger up, slipped the pairing by one, and made the
    # last priming image frame 0 of the take. Deadlines are at least two periods.
    for serial in FAKE_SERIALS:
        _backend(fake_system, serial).image_latency_s = 0.15
    save_dir, summary, _arrays, _ = _record(
        fake_system, tmp_path, trigger_source="software", fps=5.0, duration_s=2.0
    )
    for serial in FAKE_SERIALS:
        cam = _cam(summary, serial)
        assert cam["frames"] == 10 and cam["missed_pulses"] == 0, cam
        # A frame shows its trigger's sequence number: frame k is pulse k.
        np.testing.assert_array_equal(_frames(save_dir, serial)[:, 0, 0], np.arange(10))
    assert summary["sync"]["ok"], summary["sync"]


@pytest.mark.parametrize("delay_s", [0.13, 0.3, 0.6])
def test_a_late_first_image_cannot_shift_the_take(fake_system, tmp_path, delay_s):
    # FAKE-1's first image of the record grab is held up (a USB stall) past its
    # trigger's short priming deadline: the next trigger fires and the late image
    # arrives in its place. Its camera timestamp shows it older than that trigger,
    # so it is discarded instead of slipping the pairing — which left the last
    # priming image in the buffer to become pulse 0 of the take (reported clean).
    _backend(fake_system, "FAKE-1").latency_by_fire = {1: delay_s}
    save_dir, summary, _arrays, _ = _record(
        fake_system, tmp_path, trigger_source="software"
    )
    for serial in FAKE_SERIALS:
        cam = _cam(summary, serial)
        assert cam["frames"] == 50 and cam["missed_pulses"] == 0, cam
        np.testing.assert_array_equal(_frames(save_dir, serial)[:, 0, 0], np.arange(50))
    assert summary["sync"]["ok"], summary["sync"]


def test_an_image_later_than_its_deadline_cannot_answer_a_later_trigger(
    fake_system, tmp_path, monkeypatch
):
    # Mid-take, FAKE-1's image for one trigger arrives after the answer deadline
    # gave up on it. It must not answer the trigger fired next (every later frame
    # a pulse late): its timestamp marks it stale, it is discarded, its pulse is
    # a filled miss, and every other frame is its own pulse.
    monkeypatch.setattr(handoff, "ANSWER_TIMEOUT_S", 0.2)
    _backend(fake_system, "FAKE-1").latency_by_fire = {20: 0.35}
    save_dir, summary, arrays, _ = _record(
        fake_system, tmp_path, trigger_source="software"
    )
    bad = _cam(summary, "FAKE-1")
    assert bad["frames"] == 50 and bad["missed_pulses"] > 0, bad
    video = _frames(save_dir, "FAKE-1")[:, 0, 0]
    dropped = arrays["FAKE-1/dropped"]
    for row in range(50):
        expected = video[row - 1] if dropped[row] else row % 256
        assert video[row] == expected, (row, video[max(0, row - 3) : row + 3])
    backend = _backend(fake_system, "FAKE-1")
    assert backend.stale_images >= 1


def test_triggers_left_pending_through_a_wait_are_dropped_not_fired_late(
    fake_system, tmp_path
):
    # FAKE-1 never delivers trigger 30's image: it waits out the answer deadline
    # while the timer keeps offering triggers. Those left pending must be dropped
    # (missed, filled), not fired ~a second late under their own pulse numbers:
    # every frame FAKE-1 did take must be exposed when the others took theirs.
    _backend(fake_system, "FAKE-1").lost_triggers = {30}
    _save_dir, summary, arrays, _ = _record(
        fake_system, tmp_path, trigger_source="software", duration_s=2.0
    )
    bad = _cam(summary, "FAKE-1")
    assert 30 in bad["missed_pulse_indices"]
    real = ~arrays["FAKE-1/dropped"]
    pulses = arrays["FAKE-1/pulse_index"][real]
    arrival1 = arrays["FAKE-1/arrival_ns"][real]
    arrival0 = dict(zip(arrays["FAKE-0/pulse_index"].tolist(),
                        arrays["FAKE-0/arrival_ns"].tolist(), strict=True))
    late = [int(p) for p, a in zip(pulses, arrival1, strict=True)
            if abs(int(a) - arrival0[int(p)]) > 3 * PERIOD_NS]
    assert not late, late


def test_a_camera_still_silent_after_priming_is_warned_about(
    fake_system, tmp_path, monkeypatch
):
    # Out of budget after one round: FAKE-1 is still ignoring triggers, so it
    # starts on the train's second pulse. Priming says so, and the start check
    # catches the offset against FAKE-0.
    monkeypatch.setattr(controller_module, "PRIME_BUDGET_S", 0.0)
    _ignoring_cameras(fake_system, [0, 5])
    board = Board(fake_system, count=50)
    _save_dir, summary, _arrays, controller = _record(
        fake_system, tmp_path, plugins=PluginManager([board]), trigger_source="managed"
    )
    assert board.primed == 4
    warnings = [
        e["message"]
        for e in controller.events
        if e["level"] == "warning" and "priming" in e["message"]
    ]
    assert len(warnings) == 1 and "FAKE-1" in warnings[0], warnings
    assert "FAKE-0" not in warnings[0]
    assert _cam(summary, "FAKE-1")["primed_frames"] == 0
    assert _cam(summary, "FAKE-1")["start_offset_pulses"] == 1
    assert not summary["sync"]["ok"]


def test_priming_does_not_wait_on_a_camera_that_failed_to_start(fake_system, tmp_path):
    # FAKE-1's record grab never starts (the rig carries on with the rest): it
    # can answer no priming pulse, so it must neither hold priming to its
    # budget nor be blamed on the trigger.
    _ignoring_cameras(fake_system, [0, 0])
    _backend(fake_system, "FAKE-1").start_grab_record = lambda: False
    board = Board(fake_system, count=25)
    _save_dir, summary, _arrays, controller = _record(
        fake_system, tmp_path, plugins=PluginManager([board]), trigger_source="managed"
    )
    assert board.primed == 4
    assert summary["pulse_train"]["primed"] == 4
    assert not [e for e in controller.events if "priming" in e["message"]]
    assert _cam(summary, "FAKE-0")["frames"] == 25


def test_a_camera_with_no_timestamps_at_all_is_not_called_a_stray(
    fake_system, tmp_path
):
    # Every FAKE-1 frame lacks a timestamp; fills at the train's end make its
    # frame count exceed its real frames. The warning must still say misses
    # cannot be detected at all, not that only a stray frame was placed.
    for serial in FAKE_SERIALS:
        _backend(fake_system, serial).hardware_period_ns = PERIOD_NS
    bad = _backend(fake_system, "FAKE-1")
    bad.zero_timestamp_triggers = set(range(10_000))
    bad.miss_triggers = {48, 49}
    board = Board(fake_system, count=50)
    _save_dir, summary, _arrays, _ = _record(
        fake_system, tmp_path, plugins=PluginManager([board]), trigger_source="managed"
    )
    (warning,) = [w for w in summary["sync"]["warnings"] if "no hardware timestamp" in w]
    assert "so missed pulses cannot be detected" in warning, warning
    assert not summary["sync"]["ok"]


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


class SlowArmBoard(Board):
    """A board whose recording arm lands late (the triggerbox's USB-reset
    recovery and re-arm take seconds) before its train starts."""

    def on_recording_start(self, params):
        time.sleep(1.0)
        super().on_recording_start(params)


def test_a_slow_arm_does_not_cut_the_train_short(fake_system, tmp_path):
    # The train's end used to be counted from before the arm, so the recording
    # stopped a second early and the rest of the train was filled with repeats.
    for serial in FAKE_SERIALS:
        _backend(fake_system, serial).hardware_period_ns = PERIOD_NS
    board = SlowArmBoard(fake_system, count=50)
    _save_dir, summary, arrays, _ = _record(
        fake_system, tmp_path, plugins=PluginManager([board]), trigger_source="managed"
    )
    for serial in FAKE_SERIALS:
        cam = _cam(summary, serial)
        assert cam["frames"] == 50, cam
        assert cam["missed_pulses"] == 0 and cam["dropped"] == 0, cam
        assert not arrays[f"{serial}/dropped"].any()
    assert summary["sync"]["ok"], summary["sync"]


class HookLog(Board):
    """A board that logs when each lifecycle hook ran and when priming ended."""

    def __init__(self, system, count, period_ns):
        super().__init__(system, count, period_ns)
        self.log: list[tuple[str, float]] = []

    def prime_trigger(self, params, pulses):
        primed = super().prime_trigger(params, pulses)
        self.log.append(("primed", time.monotonic()))
        return primed

    def on_recording_start(self, params):
        self.log.append(("start", time.monotonic()))
        super().on_recording_start(params)

    def on_first_frame(self, params):
        self.log.append(("first_frame", time.monotonic()))

    def on_recording_stop(self, aborted):
        super().on_recording_stop(aborted)
        self.log.append(("stop", time.monotonic()))


class HookSpy(Plugin):
    """Logs the lifecycle hooks of a software-triggered recording."""

    name = "board"

    def __init__(self):
        self.log: list[tuple[str, float]] = []

    def on_recording_start(self, params):
        self.log.append(("start", time.monotonic()))

    def on_first_frame(self, params):
        self.log.append(("first_frame", time.monotonic()))

    def on_recording_stop(self, aborted):
        self.log.append(("stop", time.monotonic()))


@pytest.mark.parametrize("source", ["managed", "software"])
def test_a_low_fps_start_sequence_is_waited_out(
    fake_system, tmp_path, monkeypatch, source
):
    # At a low fps priming takes several periods — at 1 fps a round alone is 8 s —
    # which outlasted the monitor's fixed waits: on_first_frame (and a teardown)
    # ran before the arm. Scaled down here: 5 fps, and waits that a round of
    # priming outlasts unless they allow for it.
    monkeypatch.setattr(controller_module, "PRIME_BUDGET_S", 0.0)  # one round
    monkeypatch.setattr(controller_module, "START_HOOKS_TIMEOUT_S", 0.2)
    monkeypatch.setattr(controller_module, "STARTED_FAIL_AFTER_S", 0.4)
    fps = 5.0
    period_ns = int(1e9 / fps)
    for serial in FAKE_SERIALS:
        _backend(fake_system, serial).hardware_period_ns = period_ns
    if source == "managed":
        plugin = HookLog(fake_system, count=5, period_ns=period_ns)
    else:
        plugin = HookSpy()
        prime = fake_system.prime_software_trigger

        def logged_prime(pulses, rate):
            prime(pulses, rate)
            plugin.log.append(("primed", time.monotonic()))

        monkeypatch.setattr(fake_system, "prime_software_trigger", logged_prime)
    _save_dir, summary, _arrays, controller = _record(
        fake_system,
        tmp_path,
        plugins=PluginManager([plugin]),
        trigger_source=source,
        fps=fps,
    )
    order = [hook for hook, _t in plugin.log]
    assert order == ["primed", "start", "first_frame", "stop"], order
    at = dict(plugin.log)
    # The priming frames of a slow train get a few periods to land.
    assert at["start"] - at["primed"] >= 4 * period_ns / 1e9 - 0.02
    assert not [e for e in controller.events if "countdown anyway" in e["message"]]
    for serial in FAKE_SERIALS:
        cam = _cam(summary, serial)
        assert cam["frames"] == 5 and cam["missed_pulses"] == 0, cam
    assert summary["sync"]["ok"], summary["sync"]


def test_a_stop_during_low_fps_priming_waits_for_it(fake_system, tmp_path, monkeypatch):
    # Stopped while a slow train's priming burst is still going out: the
    # teardown (and the board's disarm) must wait for it, not time out first.
    monkeypatch.setattr(controller_module, "PRIME_BUDGET_S", 0.0)
    monkeypatch.setattr(controller_module, "START_HOOKS_TIMEOUT_S", 0.2)
    period_ns = int(1e9 / 5.0)
    board = HookLog(fake_system, count=5, period_ns=period_ns)
    settings = RecordingSettings(
        fps=5.0,
        duration_s=1.0,
        save_dir=str(tmp_path / "rec" / "001"),
        save_method="raw",
        trigger_source="managed",
    )
    controller = RecordingController(
        fake_system, settings, plugins=PluginManager([board]), auto_preview=False
    )
    starter = threading.Thread(
        target=controller.start_recording, kwargs={"plugin_params": {"board": {}}}
    )
    starter.start()
    deadline = time.monotonic() + 5
    while controller.state != "waiting" and time.monotonic() < deadline:
        time.sleep(0.01)
    time.sleep(0.05)  # into the 0.8 s priming burst
    controller.stop_recording(abort=True)
    starter.join(timeout=10)
    controller.join(timeout=10)
    order = [hook for hook, _t in board.log]
    assert order == ["primed", "stop"], order  # the stop skipped the arm


def test_a_camera_that_records_nothing_breaks_sync(fake_system, tmp_path, monkeypatch):
    # FAKE-1's record grab starts but it never answers a trigger: there is no
    # video of it to align, whatever the other camera did.
    monkeypatch.setattr(controller_module, "PRIME_BUDGET_S", 0.0)
    monkeypatch.setattr(controller_module, "STARTED_FAIL_AFTER_S", 0.3)
    _backend(fake_system, "FAKE-1").ignore_first_triggers = 10**9
    _save_dir, summary, _arrays, _ = _record(
        fake_system, tmp_path, trigger_source="software"
    )
    assert _cam(summary, "FAKE-0")["frames"] == 50
    assert _cam(summary, "FAKE-1")["frames"] == 0
    assert not summary["sync"]["ok"]
    assert any("FAKE-1 recorded no frame" in w for w in summary["sync"]["warnings"])


@pytest.mark.parametrize("unlike", [None, "exposure", "geometry"])
def test_the_start_check_compares_only_like_cameras(fake_system, tmp_path, unlike):
    # FAKE-1's frames reach the host 0.8 of a period after FAKE-0's, as a slower
    # model's, a larger frame's or a longer exposure's do. Cameras alike in all
    # of those deliver a pulse equally fast, so between them that is a camera a
    # pulse late (or unverifiable); between unlike cameras it says nothing.
    slow = next(c for c in fake_system if c.serial_number == "FAKE-1")
    if unlike == "exposure":
        slow.set_live_param("exposure", 12000)
    elif unlike == "geometry":
        slow.set_geometry(width=2 * W, height=2 * H)
    _ignoring_cameras(fake_system, [0, 0])  # hardware-clocked, trigger line
    backend = slow.backend
    original = backend.retrieve_external

    def late(timeout_ms, wants_array):
        frame = original(timeout_ms, wants_array)
        if frame is not None:
            time.sleep(0.8 * PERIOD_NS / 1e9)
        return frame

    backend.retrieve_external = late
    board = Board(fake_system, count=50)
    _save_dir, summary, _arrays, _ = _record(
        fake_system, tmp_path, plugins=PluginManager([board]), trigger_source="managed"
    )
    sync = summary["sync"]
    if unlike is None:
        assert not sync["ok"], sync
        assert not sync["notes"], sync
        return
    assert sync["ok"], sync
    for serial in FAKE_SERIALS:
        assert _cam(summary, serial)["start_offset_pulses"] is None
    (note,) = sync["notes"]
    assert "[FAKE-0]" in note and "[FAKE-1]" in note, note


def test_the_start_check_trusts_the_software_trigger_sequence(fake_system, tmp_path):
    # Under the software trigger a frame's pulse is its own trigger's sequence
    # number, so frame 0 is trigger 0 in every camera by construction. A like
    # camera that merely delivers later (a busier USB lane) must not read as
    # having started a pulse late.
    slow = next(c for c in fake_system if c.serial_number == "FAKE-1")
    original = slow.backend.retrieve

    def late(timeout_ms, wants_array):
        frame = original(timeout_ms, wants_array)
        if frame is not None:
            time.sleep(0.8 * PERIOD_NS / 1e9)
        return frame

    slow.backend.retrieve = late
    save_dir, summary, _arrays, _ = _record(
        fake_system, tmp_path, trigger_source="software"
    )
    assert summary["sync"]["ok"], summary["sync"]
    for serial in FAKE_SERIALS:
        assert _cam(summary, serial)["start_offset_pulses"] == 0
    assert check_recording(save_dir).ok


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


# --- software-trigger pairing: an image answers the trigger that exposed it --- #


def test_a_late_image_is_credited_to_its_own_trigger(fake_system, tmp_path):
    # Trigger 10's image arrives a fetch after the one that fired it (30's three
    # fetches after). Firing the next trigger regardless credited that image to
    # trigger 11: pulse 10 "missed" and every later frame of FAKE-1 a pulse late,
    # while the summary said aligned.
    _backend(fake_system, "FAKE-1").late_triggers = {10: 1, 30: 3}
    save_dir, summary, arrays, _ = _record(
        fake_system, tmp_path, trigger_source="software"
    )
    for serial in FAKE_SERIALS:
        cam = _cam(summary, serial)
        assert cam["frames"] == 50 and cam["missed_pulses"] == 0, cam
    np.testing.assert_array_equal(arrays["FAKE-1/pulse_index"], np.arange(50))
    video = _frames(save_dir, "FAKE-1")
    np.testing.assert_array_equal(video, _frames(save_dir, "FAKE-0"))
    # The fake draws each frame from its trigger's number: frame k is pulse k.
    np.testing.assert_array_equal(video[:, 0, 0], np.arange(50))


def test_an_image_that_never_arrives_is_given_up_on(
    fake_system, tmp_path, monkeypatch
):
    # No image ever answers trigger 10 (and the SDK says nothing). The hand-off
    # fires nothing until it gives up on it at its deadline; the pulses offered
    # meanwhile are missed and filled, and every later frame is still its pulse.
    monkeypatch.setattr(handoff, "ANSWER_TIMEOUT_S", 0.1)
    bad_backend = _backend(fake_system, "FAKE-1")
    bad_backend.lost_triggers = {10}
    save_dir, summary, arrays, _ = _record(
        fake_system, tmp_path, trigger_source="software", duration_s=2.0
    )
    count = summary["pulse_train"]["count"]
    bad = _cam(summary, "FAKE-1")
    assert bad["frames"] == _cam(summary, "FAKE-0")["frames"] == count
    assert _cam(summary, "FAKE-0")["missed_pulses"] == 0
    missed = bad["missed_pulse_indices"]
    # 10 and the pulses within the 0.1 s deadline after it (5 periods), no more.
    assert missed[0] == 10 and missed[-1] <= 16, missed
    assert bad_backend.unanswered_triggers == 1
    np.testing.assert_array_equal(arrays["FAKE-1/pulse_index"], np.arange(count))
    good, video = _frames(save_dir, "FAKE-0"), _frames(save_dir, "FAKE-1")
    for k in range(count):
        expected = video[k - 1] if k in missed else good[k]
        assert (video[k] == expected).all(), k


def _direct_camera(serial):
    from octacam.cameras.base import Camera
    from octacam.cameras.fake import FakeBackend

    backend: Any = FakeBackend(serial)
    backend.open()
    backend.write_node("width", W)
    backend.write_node("height", H)
    return backend, Camera(backend)


def _wait_for(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline, "timed out"
        time.sleep(0.001)


def test_a_priming_image_arriving_after_counting_starts_is_discarded(tmp_path):
    # The last priming trigger's image is still on its way when counting starts.
    # It must be discarded as a priming answer — not recorded as pulse 0, which
    # put every frame of the take a pulse late.
    backend, camera = _direct_camera("FAKE-P")
    path = tmp_path / "cam.raw"
    clock = PulseClock(PERIOD_NS, count=10, source="software")
    assert camera.start_record(
        str(path), FPS, FORMATS["raw"], pulse_clock=clock, hold=True
    )
    try:
        backend.late_triggers = {3: 100}  # ~100 fetches late: well after the arm
        for _ in range(4):
            camera.trigger_once()
            time.sleep(PERIOD_NS / 1e9)
        _wait_for(lambda: backend._pending == 0 and bool(backend._outstanding))
        assert camera.primed_frames == 3
        backend.late_triggers = {}
        camera.arm_counting()
        _wait_for(lambda: camera.primed_frames == 4)
        for _ in range(10):
            camera.trigger_once()
            time.sleep(PERIOD_NS / 1e9)
        _wait_for(lambda: camera.pulses_complete)
    finally:
        camera.stop(fill_to=10)
        camera.join()
    assert camera.primed_frames == 4 and camera.missed_pulses == []
    assert camera.frame_pulse_index == list(range(10))
    video = np.fromfile(path, dtype=np.uint8).reshape(-1, H, W)
    np.testing.assert_array_equal(video[:, 0, 0], np.arange(10))


def test_a_buffered_priming_frame_is_a_straggler_at_low_fps(tmp_path):
    # At 10 fps a priming frame still buffered when counting starts arrives a
    # whole period (100 ms) after the previous priming frame — past the fixed
    # 50 ms straggler window — and became pulse 0 of the recording.
    period = 100_000_000
    backend, camera = _direct_camera("FAKE-S")
    backend.hardware_period_ns = period
    buffered: list = []
    real = backend.retrieve_external

    def retrieve_external(timeout_ms, wants_array):
        return buffered.pop() if buffered else real(timeout_ms, wants_array)

    backend.retrieve_external = retrieve_external
    path = tmp_path / "cam.raw"
    clock = PulseClock(period, count=5, source="managed")
    assert camera.start_record(
        str(path),
        10.0,
        FORMATS["raw"],
        software_trigger=False,
        pulse_clock=clock,
        hold=True,
    )
    try:
        for _ in range(3):
            camera.trigger_once()
            time.sleep(0.01)
        _wait_for(lambda: camera.primed_frames == 3)
        # The fourth priming pulse's frame: exposed a period after the third,
        # still in the SDK's buffer when counting starts.
        stamp = backend._clock_t0 + 3 * period
        camera.arm_counting()
        buffered.append((np.full((H, W), 3, dtype=np.uint8), stamp))
        _wait_for(lambda: camera.primed_frames == 4)
        for _ in range(5):
            camera.trigger_once()
            time.sleep(0.01)
        _wait_for(lambda: camera.pulses_complete)
    finally:
        camera.stop(fill_to=5)
        camera.join()
    assert camera.primed_frames == 4 and camera.missed_pulses == []
    assert camera.frame_pulse_index == list(range(5))
    assert camera.extra_frames == 0


# --- stray zero timestamps and fill timestamps -------------------------------- #


def test_a_stray_zero_timestamp_does_not_shift_later_frames(fake_system, tmp_path):
    # One frame of a hardware-clocked camera comes without a timestamp. Placing
    # it as the next pulse but leaving the clock anchor on the frame before made
    # the next interval two periods: an invented miss, and every later frame a
    # pulse late (the train's last real frame thrown away as extra).
    for serial in FAKE_SERIALS:
        _backend(fake_system, serial).hardware_period_ns = PERIOD_NS
    _backend(fake_system, "FAKE-1").zero_timestamp_triggers = {4}
    save_dir, summary, arrays, _ = _record(
        fake_system, tmp_path, trigger_source="software"
    )
    bad = _cam(summary, "FAKE-1")
    assert bad["frames"] == 50, bad
    assert bad["missed_pulses"] == 0 and bad["extra_frames"] == 0, bad
    assert bad["unclocked_frames"] == 1 and bad["host_fallback_count"] == 1
    np.testing.assert_array_equal(arrays["FAKE-1/pulse_index"], np.arange(50))
    np.testing.assert_array_equal(
        _frames(save_dir, "FAKE-1"), _frames(save_dir, "FAKE-0")
    )
    # Stamped when its pulse was due, on the camera's own clock.
    assert np.all(np.diff(arrays["FAKE-1/timestamp_ns"]) == PERIOD_NS)


def test_zero_timestamps_before_the_first_timed_frame_do_not_restart_the_count(
    fake_system, tmp_path
):
    # The tracker anchors the camera clock at its first timed frame; after two
    # untimed ones that frame is pulse 2, not the train's first pulse again
    # (which put two frames on pulse 0 and every later one a pulse early).
    for serial in FAKE_SERIALS:
        _backend(fake_system, serial).hardware_period_ns = PERIOD_NS
    _backend(fake_system, "FAKE-1").zero_timestamp_triggers = {0, 1}
    save_dir, summary, arrays, _ = _record(
        fake_system, tmp_path, trigger_source="software"
    )
    bad = _cam(summary, "FAKE-1")
    assert bad["frames"] == 50 and bad["missed_pulses"] == 0, bad
    assert bad["unclocked_frames"] == 2
    np.testing.assert_array_equal(arrays["FAKE-1/pulse_index"], np.arange(50))
    np.testing.assert_array_equal(
        _frames(save_dir, "FAKE-1"), _frames(save_dir, "FAKE-0")
    )


def test_fills_are_time_stamped_when_their_pulse_was_due(fake_system, tmp_path):
    # Under the software trigger the pulse comes from the trigger sequence, so
    # the tracker has no clock to place a fill on: the trailing fills were
    # stamped 0, which made the camera's summary fps negative (and a raw
    # transcode then ran at -framerate).
    _backend(fake_system, "FAKE-1").miss_triggers = {0, 25, 48, 49}
    _save_dir, summary, arrays, _ = _record(
        fake_system, tmp_path, trigger_source="software"
    )
    bad = _cam(summary, "FAKE-1")
    assert bad["missed_pulse_indices"] == [0, 25, 48, 49]
    ts = arrays["FAKE-1/timestamp_ns"].astype(np.int64)
    assert (ts > 0).all()
    assert ts[0] == ts[1] - PERIOD_NS  # a leading miss counts back
    assert ts[25] == ts[24] + PERIOD_NS
    assert ts[48] == ts[47] + PERIOD_NS and ts[49] == ts[47] + 2 * PERIOD_NS
    assert 0.5 * FPS < bad["fps"] < 2 * FPS
    assert bad["timestamp_source"] == "hardware"


def test_fills_of_a_host_clocked_camera_are_host_time(fake_system, tmp_path):
    # A backend with no hardware timestamp (pycameleon) is stamped with host
    # time; its fills must be too — not "0 minus a few periods" — and count as
    # host-clocked rows, so the summary says "host", not "mixed".
    backend = _backend(fake_system, "FAKE-1")
    backend.miss_triggers = {0, 25, 49}
    retrieve = backend.retrieve

    def unclocked(timeout_ms, wants_array):
        frame = retrieve(timeout_ms, wants_array)
        return None if frame is None else (frame[0], 0)

    backend.retrieve = unclocked
    _save_dir, summary, arrays, _ = _record(
        fake_system, tmp_path, trigger_source="software"
    )
    bad = _cam(summary, "FAKE-1")
    assert bad["missed_pulse_indices"] == [0, 25, 49]
    ts = arrays["FAKE-1/timestamp_ns"].astype(np.int64)
    assert (ts > 1_500_000_000 * 10**9).all()  # all host wall-clock time
    assert bad["start_timestamp_ns"] > 0
    assert bad["host_fallback_count"] == bad["frames"] == 50
    assert bad["timestamp_source"] == "host"


def test_mean_fps_of_a_series_that_runs_backward_is_zero():
    from octacam.cameras.base import Camera
    from octacam.cameras.fake import FakeBackend

    backend: Any = FakeBackend("FAKE-T")
    camera = Camera(backend)
    camera._timestamps[:] = [3_600_000_000_000, 0]
    assert camera.mean_fps == 0.0


# --- writer overload: a sustained shortfall is skipped, not filled ------------- #


def test_a_writer_that_cannot_keep_up_skips_instead_of_snowballing(
    fake_system, tmp_path
):
    # FAKE-0's sink writes ~33 frames/s against a 50 fps train. Filling every
    # refused frame made each queued frame carry more fills, each costing a
    # real write: the real frames the writer accepted decayed toward zero and
    # close() had the whole backlog to write. Past a queue's worth of refusals
    # the camera now skips refused frames.
    queue_size = 8
    camera = next(c for c in fake_system if c.serial_number == "FAKE-0")
    original = camera.start_record

    def start_record(*args, **kwargs):
        ok = original(*args, **kwargs)
        writer = camera._video_writer
        write_frame = writer._write_frame

        def slow(frame):
            time.sleep(0.03)
            write_frame(frame)

        writer._write_frame = slow
        return ok

    camera.start_record = start_record
    save_dir, summary, arrays, _ = _record(
        fake_system,
        tmp_path,
        trigger_source="software",
        duration_s=2.0,
        writer_queue_size=queue_size,
    )
    count = summary["pulse_train"]["count"]
    cam = _cam(summary, "FAKE-0")
    skipped = camera.writer_skipped_pulses
    assert camera.writer_skipped == len(skipped) > 0
    # The summary carries them, and the recording is no longer frame k = pulse k.
    assert cam["writer_skipped"] == len(skipped)
    assert cam["writer_skipped_pulse_indices"] == skipped[:1000]
    assert not summary["sync"]["ok"]
    # At most a queue's worth of refused frames was filled before the switch.
    assert 0 < cam["writer_dropped"] <= queue_size
    assert cam["missed_pulses"] == 0
    # Rows are video frames: a skipped pulse has neither.
    pulses = arrays["FAKE-0/pulse_index"].tolist()
    video = _frames(save_dir, "FAKE-0")
    assert len(video) == len(pulses) == cam["frames"] == count - len(skipped)
    assert sorted(pulses + skipped) == list(range(count))
    dropped = arrays["FAKE-0/dropped"]
    for row, pulse in enumerate(pulses):
        expected = video[row - 1, 0, 0] if dropped[row] else pulse % 256
        assert video[row, 0, 0] == expected, (row, pulse)
    # The other camera is untouched.
    assert _cam(summary, "FAKE-1")["frames"] == count
    other = next(c for c in fake_system if c.serial_number == "FAKE-1")
    assert other.writer_skipped == 0


def test_transient_writer_stalls_are_filled_however_many(fake_system, tmp_path):
    # Three separate 0.26 s encoder stalls, each refusing ~5 frames against a
    # queue of 8: every one is caught up on before the next, so the writer is
    # never judged overloaded, although more than a queue's worth is refused in
    # all — every refused frame is filled and the video keeps every pulse.
    queue_size = 8
    camera = next(c for c in fake_system if c.serial_number == "FAKE-0")
    original = camera.start_record

    def start_record(*args, **kwargs):
        ok = original(*args, **kwargs)
        writer = camera._video_writer
        write_frame = writer._write_frame
        writes = {"n": 0}

        def stalling(frame):
            writes["n"] += 1
            if writes["n"] in (10, 40, 70):
                time.sleep(0.26)
            write_frame(frame)

        writer._write_frame = stalling
        return ok

    camera.start_record = start_record
    save_dir, summary, arrays, _ = _record(
        fake_system,
        tmp_path,
        trigger_source="software",
        duration_s=2.0,
        writer_queue_size=queue_size,
    )
    count = summary["pulse_train"]["count"]
    cam = _cam(summary, "FAKE-0")
    assert camera.writer_skipped == 0
    assert cam["writer_dropped"] > queue_size
    assert cam["frames"] == count and len(_frames(save_dir, "FAKE-0")) == count
    np.testing.assert_array_equal(arrays["FAKE-0/pulse_index"], np.arange(count))
