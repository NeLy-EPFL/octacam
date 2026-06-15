"""Plugin registry + manager behavior."""

from octacam.config import OctacamConfig, PluginConfig
from octacam.plugins import PluginManager, available_plugins, build_plugins, register
from octacam.plugins.base import Plugin


def test_build_plugins_default_is_empty():
    assert build_plugins(OctacamConfig()).plugins == []


def test_unknown_plugin_is_skipped():
    config = OctacamConfig(plugins=[PluginConfig(name="does-not-exist")])
    assert build_plugins(config).plugins == []


def test_no_plugins_override_disables_config():
    config = OctacamConfig(plugins=[PluginConfig(name="arduino")])
    assert build_plugins(config, enabled=[]).plugins == []


def test_register_and_build_with_options():
    @register("spy_demo")
    def _factory(options):
        plugin = Plugin()
        plugin.name = "spy_demo"
        plugin.options = options
        return plugin

    config = OctacamConfig(plugins=[PluginConfig(name="spy_demo", options={"a": 1})])
    manager = build_plugins(config)
    assert len(manager.plugins) == 1
    assert manager.plugins[0].options == {"a": 1}


def test_cli_plugin_flag_adds_to_config():
    @register("spy_added")
    def _factory(options):
        plugin = Plugin()
        plugin.name = "spy_added"
        return plugin

    # config has none; --plugin spy_added adds it
    manager = build_plugins(OctacamConfig(), enabled=["spy_added"])
    assert [p.name for p in manager.plugins] == ["spy_added"]


def test_available_plugins_describes_bundled_arduino():
    infos = {info.name: info for info in available_plugins()}
    # Only in-repo builtins are discoverable; arduino is the one bundled plugin.
    assert "arduino" in infos
    info = infos["arduino"]
    assert isinstance(info.available, bool)
    assert info.summary  # first line of the module docstring
    # When the optional dependency is missing, the reason is surfaced.
    if not info.available:
        assert info.detail


def test_dispatch_swallows_plugin_exceptions():
    class Boom(Plugin):
        name = "boom"

        def on_first_frame(self, params):
            raise RuntimeError("boom")

    PluginManager([Boom()]).dispatch("on_first_frame", None)  # must not raise


def test_status_shape():
    class Demo(Plugin):
        name = "demo"

        def status(self):
            return {"foo": 1}

    assert PluginManager([Demo()]).status() == {"demo": {"ready": True, "foo": 1}}


class _FakeLink:
    """Stand-in for arduino.SerialLink so tests need no real serial device."""

    def __init__(self):
        self._open = False
        self.fail: Exception | None = None

    def open(self, device, baud):
        self._open = False  # the real open() closes any prior link first
        if self.fail is not None:
            raise self.fail
        self._open = True

    def close(self):
        self._open = False

    @property
    def is_open(self):
        return self._open


def test_arduino_open_reports_success_and_failure():
    """_open never raises; it returns None on success, the message on failure."""
    from octacam.plugins.arduino import ArduinoPlugin

    plugin = ArduinoPlugin(device="/dev/test")
    plugin._link = link = _FakeLink()

    assert plugin.is_ready() is False
    assert plugin._open() is None
    assert plugin.is_ready() is True

    link.fail = OSError("no such device")
    assert plugin._open() == "no such device"
    assert plugin.is_ready() is False  # a failed open leaves the port closed


def test_arduino_reconnect_endpoint_surfaces_ready_state():
    """POST /api/serial/reconnect re-opens the port and reports the outcome."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from octacam.plugins.arduino import ArduinoPlugin

    plugin = ArduinoPlugin(device="/dev/test")
    plugin._link = link = _FakeLink()

    app = FastAPI()
    app.include_router(plugin.api_router())
    client = TestClient(app)

    # Board absent: reconnect fails, ready stays false and the reason is surfaced.
    link.fail = OSError("no such device")
    r = client.post("/api/serial/reconnect")
    assert r.status_code == 200
    assert r.json() == {
        "ready": False,
        "device": "/dev/test",
        "error": "no such device",
    }

    # Board now present: reconnect succeeds.
    link.fail = None
    assert client.post("/api/serial/reconnect").json() == {
        "ready": True,
        "device": "/dev/test",
        "error": None,
    }


def test_setup_teardown_order():
    calls = []

    class Recorder(Plugin):
        def __init__(self, name):
            self.name = name

        def setup(self):
            calls.append(("setup", self.name))

        def teardown(self):
            calls.append(("teardown", self.name))

    manager = PluginManager([Recorder("a"), Recorder("b")])
    manager.setup_all()
    manager.teardown_all()
    assert calls == [
        ("setup", "a"),
        ("setup", "b"),
        ("teardown", "b"),  # reverse order on teardown
        ("teardown", "a"),
    ]
