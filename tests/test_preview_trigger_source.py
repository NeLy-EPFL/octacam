"""Preview trigger-source resolution and arming on the fake backend.

Covers the two-axis design: the recording ``trigger_source`` (software | managed
| external) and the ``preview_trigger_source`` override (auto | software |
free_running), plus the plugin capability that makes ``managed`` real and the
back-compat promotion of a legacy external+driving-plugin rig to ``managed``.
No hardware/SDK — a fake camera system + a stub driving plugin.
"""

import os
import threading
import time

os.environ.setdefault("OCTACAM_FAKE_CAMERAS", "FAKE-0,FAKE-1")

import pytest

from octacam.cameras import CameraSystem
from octacam.controller import RecordingController, RecordingSettings, StartResult
from octacam.plugins.base import Plugin, PluginManager

FAKE_SERIALS = ["FAKE-0", "FAKE-1"]


class DrivingPlugin(Plugin):
    """A stub trigger-generating plugin (like the triggerbox): it can drive the
    trigger during preview and records the arm/disarm calls it receives."""

    name = "faketrigger"

    def __init__(self):
        self.preview_starts = 0
        self.preview_stops = 0
        self.last_params = None

    def drives_preview_trigger(self) -> bool:
        return True

    def on_preview_start(self, params) -> None:
        self.preview_starts += 1
        self.last_params = params

    def on_preview_stop(self) -> None:
        self.preview_stops += 1

    def default_start_params(self, fps, duration_s):
        return {"fps": int(fps), "duration_ms": max(1, int(duration_s * 1000))}


@pytest.fixture
def make_controller(tmp_path):
    created = []

    def _make(
        trigger_source="software",
        preview_trigger_source="auto",
        driving=False,
        plugin=None,
        auto_preview=False,
    ):
        """``plugin`` injects a specific driving-plugin instance (implies
        ``driving``); ``auto_preview`` mirrors the GUI (preview resumes after a
        recording) rather than the headless default."""
        system = CameraSystem(FAKE_SERIALS, backend="fake")
        system.load_config(tmp_path)
        for camera in system:
            camera.set_geometry(width=64, height=48)
        if plugin is not None:
            driving = True
        plugins = PluginManager([plugin or DrivingPlugin()] if driving else [])
        settings = RecordingSettings(
            fps=50.0,
            trigger_source=trigger_source,
            preview_trigger_source=preview_trigger_source,
            save_dir=str(tmp_path / "rec" / "001"),
        )
        controller = RecordingController(
            system, settings, plugins=plugins, auto_preview=auto_preview
        )
        created.append((controller, system))
        return controller, system

    yield _make
    for controller, _system in created:
        controller.close()


def _driving_plugin(controller):
    return controller.plugins.plugins[0]


# --------------------------------------------------------------- resolution


@pytest.mark.parametrize(
    "trigger_source,preview_pref,driving,expected",
    [
        # auto mirrors the recording trigger source
        ("software", "auto", False, "software"),
        ("external", "auto", False, "free_running"),
        ("managed", "auto", True, "managed"),
        # managed with no driving plugin present degrades to the approximation
        ("managed", "auto", False, "free_running"),
        # explicit overrides win regardless of the recording source
        ("managed", "software", True, "software"),
        ("managed", "free_running", True, "free_running"),
        ("software", "free_running", False, "free_running"),
    ],
)
def test_effective_preview_mode(
    make_controller, trigger_source, preview_pref, driving, expected
):
    controller, _system = make_controller(
        trigger_source=trigger_source,
        preview_trigger_source=preview_pref,
        driving=driving,
    )
    assert controller._effective_preview_mode() == expected


def test_managed_trigger_available_reflects_driving_plugin(make_controller):
    with_plugin, _ = make_controller(driving=True)
    without_plugin, _ = make_controller(driving=False)
    assert with_plugin.managed_trigger_available is True
    assert without_plugin.managed_trigger_available is False


def test_external_plus_driving_plugin_promotes_to_managed(make_controller):
    """A legacy external rig with a driving plugin (the shipped triggerbox config)
    is promoted to managed, so recording is unchanged but auto preview drives it."""
    controller, _system = make_controller(trigger_source="external", driving=True)
    assert controller.get_settings().trigger_source == "managed"
    assert controller._effective_preview_mode() == "managed"


def test_external_without_driving_plugin_stays_external(make_controller):
    controller, _system = make_controller(trigger_source="external", driving=False)
    assert controller.get_settings().trigger_source == "external"


# --------------------------------------------------------------- arming


def test_free_running_preview_caps_the_backend_and_flows_frames(make_controller):
    controller, system = make_controller(preview_trigger_source="free_running")
    controller.start_preview()
    assert controller.state == "preview"
    # begin_freerun(fps) reached every backend with the target rate.
    for camera in system:
        assert getattr(camera.backend, "_freerun_fps", None) == 50.0
    # retrieve_freerun actually delivers frames — timestamps accumulate even with
    # no display consumer popping the single-slot handoff.
    deadline = time.monotonic() + 3.0
    while time.monotonic() < deadline and not any(
        c.frames_recorded > 1 for c in system
    ):
        time.sleep(0.05)
    assert any(c.frames_recorded > 1 for c in system)


def test_software_preview_does_not_freerun(make_controller):
    controller, system = make_controller(preview_trigger_source="software")
    controller.start_preview()
    assert controller.state == "preview"
    for camera in system:
        assert getattr(camera.backend, "_freerun_fps", None) is None


def test_managed_preview_arms_driving_plugin(make_controller):
    controller, _system = make_controller(trigger_source="managed", driving=True)
    plugin = _driving_plugin(controller)
    controller.start_preview()
    assert controller._effective_preview_mode() == "managed"
    assert plugin.preview_starts == 1
    # The controller hands the plugin its recording arm slice to reuse.
    assert plugin.last_params == {"faketrigger": {"fps": 50, "duration_ms": 20000}}


def test_non_managed_preview_disarms_driving_plugin(make_controller):
    # A driving plugin is present, but the operator forces a software preview:
    # the plugin must be cancelled, not armed.
    controller, _system = make_controller(
        trigger_source="managed", preview_trigger_source="software", driving=True
    )
    plugin = _driving_plugin(controller)
    controller.start_preview()
    assert plugin.preview_starts == 0
    assert plugin.preview_stops == 1


def test_switching_preview_source_live_rearms(make_controller):
    controller, system = make_controller(trigger_source="managed", driving=True)
    plugin = _driving_plugin(controller)
    controller.start_preview()
    assert plugin.preview_starts == 1  # managed
    # Switch preview to free-run: cameras re-arm to free-run, plugin is disarmed.
    controller.update_settings(preview_trigger_source="free_running")
    assert plugin.preview_stops >= 1
    for camera in system:
        assert getattr(camera.backend, "_freerun_fps", None) == 50.0


# --------------------------------------------------------------- validation


def test_update_settings_accepts_managed_and_rejects_bad_preview(make_controller):
    controller, _system = make_controller()
    controller.update_settings(trigger_source="managed")
    assert controller.get_settings().trigger_source == "managed"
    controller.update_settings(preview_trigger_source="free_running")
    assert controller.get_settings().preview_trigger_source == "free_running"
    with pytest.raises(ValueError):
        controller.update_settings(preview_trigger_source="bogus")
    with pytest.raises(ValueError):
        controller.update_settings(trigger_source="nope")


# ------------------------------------------------ preview -> recording hand-off


class HookLog(DrivingPlugin):
    """DrivingPlugin that also journals every arm/disarm/recording hook, in
    order, into a shared list — so a test can check *when* the board was
    canceled relative to the cameras' record grab. ``release_preview_stop``
    can be cleared to hold ``on_preview_stop`` open (it is set by default)."""

    def __init__(self, order):
        super().__init__()
        self.order = order
        self.preview_stop_entered = threading.Event()
        self.release_preview_stop = threading.Event()
        self.release_preview_stop.set()

    def on_preview_start(self, params) -> None:
        super().on_preview_start(params)
        self.order.append("preview_start")

    def on_preview_stop(self) -> None:
        super().on_preview_stop()
        self.order.append("preview_stop")
        self.preview_stop_entered.set()
        self.release_preview_stop.wait(5)

    def on_recording_start(self, params) -> None:
        self.order.append("recording_start")

    def on_recording_stop(self, aborted) -> None:
        self.order.append("recording_stop")


def _spy_record_grab(system, order):
    """Journal the moment each camera's backend actually begins its record grab."""
    for camera in system:
        backend = camera.backend
        original = backend.start_grab_record

        def spy(_original=original, _name=camera.name):
            order.append(f"record_grab:{_name}")
            return _original()

        backend.start_grab_record = spy


def test_recording_start_disarms_the_managed_preview_before_the_record_grab(
    make_controller,
):
    """Regression for the hexaview "IR lights flash once at record start".

    Under a managed preview the trigger board is pulsing the cameras from its
    indefinite preview arm. A recording start used to switch the cameras into the
    record grab while those pulses kept coming and only then re-arm the board for
    the recording, restarting its frame clock at an arbitrary phase; a camera in
    overlapped readout (TriggerOverlap=ReadOut) then delayed its exposure out from
    under the strobe for the next several frames — a dark ramp opening every GUI
    recording. The preview arm must be canceled before any camera begins the
    record grab, and the recording arm sent only after."""
    order: list[str] = []
    controller, system = make_controller(
        trigger_source="managed", plugin=HookLog(order), auto_preview=True
    )
    plugin = _driving_plugin(controller)
    controller.start_preview()
    assert order == ["preview_start"]
    _spy_record_grab(system, order)

    assert controller.start_recording(plugin_params={"faketrigger": {}}).ok
    grabs = [i for i, e in enumerate(order) if e.startswith("record_grab:")]
    assert len(grabs) == len(FAKE_SERIALS), order
    assert order.index("preview_stop") < min(grabs), order
    assert order.index("recording_start") > max(grabs), order
    assert (plugin.preview_starts, plugin.preview_stops) == (1, 1)
    assert controller.recording_active

    # No pulses reach the fake's external record fetch, so end it by hand; the
    # teardown then cancels the recording arm and re-arms the managed preview.
    controller.stop_recording(abort=True)
    controller.join(timeout=20)
    assert order[-2:] == ["recording_stop", "preview_start"], order
    assert controller.state == "preview"


def test_recording_start_leaves_a_non_managed_preview_alone(make_controller):
    """A software preview never armed the board (the driving plugin was disarmed
    when preview started), so the recording start sends no further cancel: the
    recording arm brings the board up from idle, as it always did."""
    order: list[str] = []
    controller, system = make_controller(
        trigger_source="managed",
        preview_trigger_source="software",
        plugin=HookLog(order),
        auto_preview=True,
    )
    controller.start_preview()
    assert order == ["preview_stop"]
    _spy_record_grab(system, order)
    assert controller.start_recording(plugin_params={"faketrigger": {}}).ok
    assert order.count("preview_stop") == 1, order
    assert order[-1] == "recording_start", order
    controller.stop_recording(abort=True)
    controller.join(timeout=20)


def test_camera_ops_are_refused_while_the_preview_arm_is_being_canceled(
    make_controller,
):
    """While start_recording is canceling the preview arm off the lock, the
    cameras are already stopped and claimed: a second start, a benchmark, a
    settings change and a preview restart must all be refused instead of handing
    the cameras a fresh trigger clock under the recording's feet — and the gate
    must lift once the recording is running."""
    order: list[str] = []
    plugin = HookLog(order)
    plugin.release_preview_stop.clear()  # hold on_preview_stop open
    controller, _system = make_controller(
        trigger_source="managed", plugin=plugin, auto_preview=True
    )
    controller.start_preview()

    results: list[StartResult] = []
    starter = threading.Thread(
        target=lambda: results.append(
            controller.start_recording(plugin_params={"faketrigger": {}})
        )
    )
    starter.start()
    assert plugin.preview_stop_entered.wait(5), "on_preview_stop never dispatched"
    try:
        second = controller.start_recording()
        assert second.status == StartResult.BUSY
        assert "starting" in second.message
        assert controller.run_diagnostic().status == StartResult.BUSY
        with pytest.raises(RuntimeError):
            controller.update_settings(fps=60.0)
        with pytest.raises(RuntimeError):
            controller.start_preview()
        assert controller.get_settings().fps == 50.0
    finally:
        plugin.release_preview_stop.set()
    starter.join(timeout=10)
    assert results and results[0].ok
    assert controller.recording_active

    controller.stop_recording(abort=True)
    controller.join(timeout=20)
    # The gate has lifted: a new start is admitted again (the aborted take left
    # its folder in place, so it asks to confirm rather than reporting busy).
    assert controller.start_recording().status == StartResult.NEEDS_CONFIRM


def test_failed_start_rearms_the_managed_preview(make_controller, monkeypatch):
    """If no camera can begin the record grab after the preview arm was
    canceled, the preview — cameras and board — is re-armed and the start gate
    released, so the operator is back on a live, strobe-lit preview."""
    order: list[str] = []
    controller, system = make_controller(
        trigger_source="managed", plugin=HookLog(order), auto_preview=True
    )
    controller.start_preview()
    monkeypatch.setattr(system, "start_record", lambda *args, **kwargs: [])

    result = controller.start_recording(plugin_params={"faketrigger": {}})
    assert result.status == StartResult.ERROR
    assert order == ["preview_start", "preview_stop", "preview_start"], order
    assert controller.state == "preview"
    assert all(camera.backend.is_grabbing() for camera in system)
    # and the gate lifted: the next start is admitted (folder exists -> confirm)
    assert controller.start_recording().status == StartResult.NEEDS_CONFIRM
