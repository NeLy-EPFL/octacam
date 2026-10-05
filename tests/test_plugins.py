"""Plugin registry + manager behavior."""

import pytest

import octacam.plugins as plugins_mod
from octacam.config import OctacamConfig, PluginConfig
from octacam.plugins import available_plugins, build_plugins
from octacam.plugins.base import Plugin, PluginManager


class SpyPlugin(Plugin):
    """A registrable plugin double that keeps the options it was built with."""

    name = "spy"

    def __init__(self, options=None):
        self.options = options

    @classmethod
    def from_options(cls, options):
        return cls(options)


@pytest.fixture
def spy_registered(monkeypatch):
    monkeypatch.setitem(plugins_mod._PLUGINS, "spy", f"{__name__}:SpyPlugin")


def test_build_plugins_default_is_empty():
    assert build_plugins(OctacamConfig()).plugins == []


def test_unknown_plugin_is_skipped():
    config = OctacamConfig(plugins=[PluginConfig(name="does-not-exist")])
    assert build_plugins(config).plugins == []


def test_no_plugins_override_disables_config():
    config = OctacamConfig(plugins=[PluginConfig(name="flywheel")])
    assert build_plugins(config, enabled=[]).plugins == []


def test_legacy_arduino_name_resolves_to_flywheel():
    # The stepper plugin was renamed arduino -> flywheel; an existing rig config
    # (or --plugin arduino) must still load it rather than silently dropping it.
    config = OctacamConfig(plugins=[PluginConfig(name="arduino")])
    manager = build_plugins(config)
    assert [p.name for p in manager.plugins] == ["flywheel"]


def test_legacy_alias_and_new_name_do_not_double_load():
    # arduino aliases to flywheel, so config flywheel + --plugin arduino must
    # resolve to a single flywheel instance, not two.
    config = OctacamConfig(plugins=[PluginConfig(name="flywheel")])
    manager = build_plugins(config, enabled=["arduino"])
    assert [p.name for p in manager.plugins] == ["flywheel"]


def test_build_passes_the_options_to_from_options(spy_registered):
    config = OctacamConfig(plugins=[PluginConfig(name="spy", options={"a": 1})])
    manager = build_plugins(config)
    assert len(manager.plugins) == 1
    assert manager.plugins[0].options == {"a": 1}


def test_cli_plugin_flag_adds_to_config(spy_registered):
    # config has none; --plugin spy adds it
    manager = build_plugins(OctacamConfig(), enabled=["spy"])
    assert [p.name for p in manager.plugins] == ["spy"]
    assert manager.plugins[0].options == {}


def test_plugin_that_fails_to_build_is_skipped(monkeypatch, spy_registered, caplog):
    def boom(cls, options):
        raise ValueError("bad options")

    monkeypatch.setattr(SpyPlugin, "from_options", classmethod(boom))
    config = OctacamConfig(plugins=[PluginConfig(name="spy")])
    assert build_plugins(config).plugins == []
    assert "Plugin 'spy' failed to load (bad options)" in caplog.text


def test_available_plugins_describes_bundled_flywheel():
    infos = {info.name: info for info in available_plugins()}
    # Only in-repo builtins are discoverable; flywheel is one of the bundled plugins.
    assert "flywheel" in infos
    info = infos["flywheel"]
    assert info.available is True
    assert info.summary  # first line of the module docstring


def test_dispatch_swallows_plugin_exceptions():
    class Boom(Plugin):
        name = "boom"

        def on_first_frame(self, params):
            raise RuntimeError("boom")

    PluginManager([Boom()]).dispatch("on_first_frame", None)  # must not raise


def test_snapshot_options_lists_every_plugin():
    class Live(Plugin):
        name = "live"

        def snapshot_options(self, params):
            return {"lights": params["live"]}

    class Unchanged(Plugin):
        name = "unchanged"  # the base hook: nothing differs from the config

    class Legacy:
        name = "legacy"  # predates the hook and doesn't subclass Plugin

    class Boom(Plugin):
        name = "boom"

        def snapshot_options(self, params):
            raise RuntimeError("boom")

    manager = PluginManager([Live(), Unchanged(), Legacy(), Boom()])
    # Every loaded plugin is keyed (so the snapshot lists it); only a plugin with
    # live changes contributes options, and a failing hook never raises.
    assert manager.snapshot_options({"live": [1]}) == {
        "live": {"lights": [1]},
        "unchanged": {},
        "legacy": {},
        "boom": {},
    }


def test_status_shape():
    class Demo(Plugin):
        name = "demo"

        def status(self):
            return {"foo": 1}

    assert PluginManager([Demo()]).status() == {"demo": {"ready": True, "foo": 1}}


def test_status_is_ready_wins_over_status_ready_key():
    # A plugin-supplied "ready" in status() must not shadow the authoritative
    # is_ready() value.
    class Sneaky(Plugin):
        name = "sneaky"

        def is_ready(self):
            return True

        def status(self):
            return {"ready": False, "foo": 1}

    assert PluginManager([Sneaky()]).status() == {"sneaky": {"ready": True, "foo": 1}}


def test_status_detail_failure_preserves_ready():
    # A status() that raises after is_ready() already succeeded must not flip the
    # plugin to not-ready — only the details are dropped.
    class Half(Plugin):
        name = "half"

        def is_ready(self):
            return True

        def status(self):
            raise RuntimeError("detail boom")

    assert PluginManager([Half()]).status() == {"half": {"ready": True}}


def test_status_is_ready_failure_reports_not_ready():
    class Broken(Plugin):
        name = "broken"

        def is_ready(self):
            raise RuntimeError("boom")

    assert PluginManager([Broken()]).status() == {"broken": {"ready": False}}


def test_available_plugins_summarizes_each_from_its_module_docstring(spy_registered):
    infos = {info.name: info for info in available_plugins()}
    assert infos["spy"].summary == "Plugin registry + manager behavior."
    assert infos["spy"].available is True


def test_builtin_import_failure_reports_distinct_warning(monkeypatch, caplog):
    # A known builtin whose module fails to import must NOT be reported as an
    # "Unknown plugin" (which is indistinguishable from a typo); it gets a
    # builtin-specific warning instead.
    monkeypatch.setitem(
        plugins_mod._PLUGINS, "flywheel", "octacam.plugins.no_such_module:Flywheel"
    )
    config = OctacamConfig(plugins=[PluginConfig(name="flywheel")])
    manager = build_plugins(config)
    assert manager.plugins == []
    assert any(
        "Builtin plugin 'flywheel' failed to import" in m for m in caplog.messages
    )
    assert not any("Unknown plugin" in m for m in caplog.messages)
    info = {i.name: i for i in available_plugins()}["flywheel"]
    assert (info.available, info.detail) == (False, "module failed to import")


class _FakeLink:
    """Stand-in for flywheel.SerialLink so tests need no real serial device."""

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

    def identify(self, banner_prefix, timeout=0.5):
        return None  # no board / no banner in these open-path tests


def test_flywheel_open_reports_success_and_failure():
    """_open never raises; it returns None on success, the message on failure."""
    from octacam.plugins.flywheel import FlywheelPlugin

    plugin = FlywheelPlugin(device="/dev/test")
    plugin._link = link = _FakeLink()

    assert plugin.is_ready() is False
    assert plugin._open() is None
    assert plugin.is_ready() is True

    link.fail = OSError("no such device")
    # The message is enriched with detected-port hints (env-dependent), but it
    # always preserves the underlying open error.
    assert plugin._open().startswith("failed to open /dev/test: no such device")
    assert plugin.is_ready() is False  # a failed open leaves the port closed


def test_flywheel_reconnect_endpoint_surfaces_ready_state():
    """POST /api/serial/reconnect re-opens the port and reports the outcome."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from octacam.plugins.flywheel import FlywheelPlugin

    plugin = FlywheelPlugin(device="/dev/test")
    plugin._link = link = _FakeLink()

    app = FastAPI()
    app.include_router(plugin.api_router())
    client = TestClient(app)

    # Board absent: reconnect fails, ready stays false and the reason is surfaced.
    link.fail = OSError("no such device")
    r = client.post("/api/serial/reconnect")
    assert r.status_code == 200
    body = r.json()
    assert body["ready"] is False
    assert body["device"] == "/dev/test"
    # Error is enriched with detected-port hints but preserves the base message.
    assert body["error"].startswith("failed to open /dev/test: no such device")

    # Board now present: reconnect succeeds (response also carries firmware fields).
    link.fail = None
    body = client.post("/api/serial/reconnect").json()
    assert body["ready"] is True
    assert body["device"] == "/dev/test"
    assert body["error"] is None


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
