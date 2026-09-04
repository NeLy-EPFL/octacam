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


def test_available_plugins_describes_bundled_flywheel():
    infos = {info.name: info for info in available_plugins()}
    # Only in-repo builtins are discoverable; flywheel is one of the bundled plugins.
    assert "flywheel" in infos
    info = infos["flywheel"]
    assert isinstance(info.available, bool)
    assert info.summary  # first line of the module docstring
    # When pyserial is missing (a broken env), the reason is surfaced.
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


def test_collect_recording_metadata_omits_none():
    class Quiet(Plugin):
        name = "quiet"

    class Chatty(Plugin):
        name = "chatty"

        def recording_metadata(self):
            return {"armed": True}

    manager = PluginManager([Quiet(), Chatty()])
    assert manager.collect_recording_metadata() == {"chatty": {"armed": True}}


def test_collect_recording_metadata_swallows_plugin_exceptions():
    class Boom(Plugin):
        name = "boom"

        def recording_metadata(self):
            raise RuntimeError("boom")

    # A misbehaving plugin is skipped, not allowed to abort the summary write.
    assert PluginManager([Boom()]).collect_recording_metadata() == {}


def test_plugin_summary_falls_back_to_factory_module_doc():
    # A third-party entry-point plugin has no octacam.plugins.<name> module, so
    # sys.modules.get(...) is None. The summary must fall back to the factory
    # module's docstring, not the truthy NoneType class docstring.
    from octacam.plugins import _plugin_summary

    @register("spy_summary")
    def _factory(options):
        p = Plugin()
        p.name = "spy_summary"
        return p

    summary = _plugin_summary("spy_summary")
    # This module's docstring first line.
    assert summary == "Plugin registry + manager behavior."
    assert "NoneType" not in summary


def test_builtin_import_failure_reports_distinct_warning(monkeypatch):
    # A known builtin whose module fails to import must NOT be reported as an
    # "Unknown plugin" (which is indistinguishable from a typo); it gets a
    # builtin-specific warning instead.
    import logging

    import octacam.plugins as plugins_mod

    # Simulate the module never importing: neutralize the import and drop any
    # already-registered factory so build_plugins sees factory is None.
    monkeypatch.setattr(plugins_mod, "_import_builtin", lambda name: None)
    monkeypatch.delitem(plugins_mod._REGISTRY, "flywheel", raising=False)

    # Capture on the octacam logger directly rather than via caplog: another test
    # (e.g. the CLI's _setup_logging) may leave propagate=False, which would empty
    # caplog's root-level capture.
    msgs: list[str] = []
    handler = logging.Handler()
    handler.emit = lambda record: msgs.append(record.getMessage())
    logger = logging.getLogger("octacam")
    logger.addHandler(handler)
    try:
        config = OctacamConfig(plugins=[PluginConfig(name="flywheel")])
        manager = build_plugins(config)
    finally:
        logger.removeHandler(handler)
    assert manager.plugins == []
    assert any("Builtin plugin 'flywheel' failed to import" in m for m in msgs)
    assert not any("Unknown plugin" in m for m in msgs)


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
