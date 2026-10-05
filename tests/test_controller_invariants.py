"""The controller's lock discipline, pinned through its public calls.

``snapshot()`` and ``stop_recording()`` take the controller lock, so nothing that
can stall may run under it: not the device reads a recording makes as it starts
(the parameter export walks each camera's node map over USB with no timeout),
and not a plugin hook (a serial write that may wait for an ack). And however
long a plugin's arm takes, a stop issued meanwhile never overtakes it.
"""

import threading

import pytest
from helpers import wait_until

from octacam.cameras import CameraSystem
from octacam.cameras.fake import FakeBackend
from octacam.config import RecordingSettings
from octacam.controller import RecordingController
from octacam.plugins.base import Plugin, PluginManager

SERIALS = ["FAKE-0", "FAKE-1"]
# A call that only takes the lock returns long before this; one stuck behind a
# stalled device read or plugin hook would wait out the test's 30 s release.
PROMPT_S = 2.0


@pytest.fixture
def system(monkeypatch):
    monkeypatch.setenv("OCTACAM_FAKE_CAMERAS", ",".join(SERIALS))
    system = CameraSystem(SERIALS, backend="fake")
    yield system
    system.close()


def _settings(tmp_path) -> RecordingSettings:
    # A take that runs until it is stopped, within the test's timeouts.
    return RecordingSettings(
        fps=50.0,
        duration_s=60.0,
        save_method="raw",
        save_dir=str(tmp_path / "rec" / "001"),
    )


def _call_promptly(call):
    """``call()``'s result, on a helper thread so a call stuck behind the lock
    fails the test instead of hanging it."""
    result = []
    worker = threading.Thread(target=lambda: result.append(call()), daemon=True)
    worker.start()
    worker.join(PROMPT_S)
    assert not worker.is_alive(), f"{call} blocked for over {PROMPT_S} s"
    return result[0]


@pytest.mark.parametrize(
    "reads",
    [("save_params",), ("read_node", "read_feature")],
    ids=["parameter-export", "delivery-profile"],
)
def test_a_camera_stalled_in_a_start_read_leaves_snapshot_and_stop_responsive(
    system, tmp_path, monkeypatch, reads
):
    # The start reads every camera's parameters (for the recording's config
    # snapshot) and its delivery profile (for the sync check); FAKE-1 stalls in
    # one of them, as a camera stuck on a USB control transfer does.
    stalled = threading.Event()
    release = threading.Event()
    for name in reads:
        read = getattr(FakeBackend, name)

        def stalling_read(self, *args, _read=read, **kwargs):
            if self.serial_number == "FAKE-1":
                stalled.set()
                release.wait(30)
            return _read(self, *args, **kwargs)

        monkeypatch.setattr(FakeBackend, name, stalling_read)
    config_dir = tmp_path / "rig"
    config_dir.mkdir()
    (config_dir / "octacam_config.toml").write_text('backend = "fake"\n')
    controller = RecordingController(
        system, _settings(tmp_path), auto_preview=False, config_dir=config_dir
    )
    starter = threading.Thread(target=controller.start_recording, daemon=True)
    try:
        starter.start()
        assert stalled.wait(10), f"the start never called {' or '.join(reads)}"

        _call_promptly(controller.snapshot)
        _call_promptly(controller.stop_recording)
    finally:
        release.set()
        starter.join(10)
        controller.close()


class _BlockingArm(Plugin):
    """A plugin whose recording arm blocks until released, like a serial write
    waiting for its ack."""

    name = "arm"

    def __init__(self):
        self.calls: list[str] = []
        self.arming = threading.Event()
        self.release = threading.Event()

    def on_recording_start(self, params):
        self.calls.append("start")
        self.arming.set()
        self.release.wait(30)
        self.calls.append("armed")

    def on_recording_stop(self, aborted):
        self.calls.append(f"stop(aborted={aborted})")


@pytest.mark.parametrize("abort", [False, True], ids=["stop", "abort"])
def test_a_stop_during_a_blocking_arm_waits_for_the_arm(system, tmp_path, abort):
    plugin = _BlockingArm()
    controller = RecordingController(
        system, _settings(tmp_path), PluginManager([plugin]), auto_preview=False
    )
    starter = threading.Thread(target=controller.start_recording, daemon=True)
    try:
        starter.start()
        assert plugin.arming.wait(10)

        assert _call_promptly(controller.snapshot)["state"] == "waiting"
        _call_promptly(lambda: controller.stop_recording(abort=abort))
        # A teardown that did not wait for the arm would reach the stop hook
        # within a few grab timeouts (0.1 s each).
        assert not wait_until(lambda: len(plugin.calls) > 1, timeout=1.0)
        plugin.release.set()
        # The stop took effect: the 60 s take ends as soon as the arm returns.
        assert wait_until(lambda: len(plugin.calls) == 3, timeout=10)
    finally:
        plugin.release.set()
        starter.join(10)
        controller.close()

    assert plugin.calls == ["start", "armed", f"stop(aborted={abort})"]
