"""Preview trigger-source resolution and arming on the fake backend.

Covers the two-axis design: the recording ``trigger_source`` (software | managed
| external) and the ``preview_trigger_source`` override (auto | software |
free_running), plus the plugin capability that makes ``managed`` real and the
back-compat promotion of a legacy external+driving-plugin rig to ``managed``.
No hardware/SDK — a fake camera system + a stub driving plugin.
"""

import os
import time

os.environ.setdefault("OCTACAM_FAKE_CAMERAS", "FAKE-0,FAKE-1")

import pytest

from octacam.cameras import CameraSystem
from octacam.controller import RecordingController, RecordingSettings
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

    def _make(trigger_source="software", preview_trigger_source="auto", driving=False):
        system = CameraSystem(FAKE_SERIALS, backend="fake")
        system.load_config(tmp_path)
        for camera in system:
            camera.set_geometry(width=64, height=48)
        plugins = PluginManager([DrivingPlugin()] if driving else [])
        settings = RecordingSettings(
            fps=50.0,
            trigger_source=trigger_source,
            preview_trigger_source=preview_trigger_source,
        )
        controller = RecordingController(
            system, settings, plugins=plugins, auto_preview=False
        )
        created.append((controller, system))
        return controller, system

    yield _make
    for controller, system in created:
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
