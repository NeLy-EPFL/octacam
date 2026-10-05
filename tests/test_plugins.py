"""Plugin registry + manager behavior."""

import functools

import pytest

import octacam.plugins as plugins_mod
from octacam.config import OctacamConfig, PluginConfig
from octacam.plugins import available_plugins, build_plugins, plugin_class
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


_BUNDLED = [
    # name, generates_trigger, default_device, banner_prefix
    ("flywheel", False, "/dev/ttyACM0", "FLYWHEEL"),
    ("twophoton", False, "/dev/arduinoCams", "2PHOTON"),
    ("triggerbox", True, "/dev/ttyACM0", "TRIGGERBOX"),
]


def test_every_bundled_plugin_is_listed():
    assert [row[0] for row in _BUNDLED] == list(plugins_mod._PLUGINS)


@pytest.mark.parametrize(("name", "trigger", "device", "banner"), _BUNDLED)
def test_a_bundled_plugin_declares_its_facts(name, trigger, device, banner):
    cls = plugin_class(name)
    assert cls.name == name
    assert cls.generates_trigger is trigger
    assert cls.web_dir is not None and (cls.web_dir / f"{name}.js").is_file()
    assert cls.default_device == device
    assert cls.firmware is not None and cls.firmware.banner_prefix == banner
    # The spec the CLI and doctor read is the one the plugin provisions with.
    assert cls.from_options({})._fw.spec == cls.firmware


@pytest.mark.parametrize(
    ("method", "hook", "args"),
    [
        ("setup_all", "setup", ()),
        ("teardown_all", "teardown", ()),
        ("on_preview_start", "on_preview_start", ({},)),
        ("on_preview_stop", "on_preview_stop", ()),
        ("on_recording_start", "on_recording_start", ({},)),
        ("on_first_frame", "on_first_frame", ({},)),
        ("on_recording_stop", "on_recording_stop", (False,)),
        ("on_ws_disconnect", "on_ws_disconnect", (1,)),
    ],
)
def test_a_raising_hook_does_not_stop_the_next_plugin(method, hook, args):
    calls = []

    def boom(self, *args):
        raise RuntimeError("boom")

    def record(self, *args):
        calls.append(args)

    plugins = [
        type("Boom", (Plugin,), {"name": "boom", hook: boom})(),
        type("Recorder", (Plugin,), {"name": "recorder", hook: record})(),
    ]
    if method == "teardown_all":
        plugins.reverse()  # teardown runs in reverse, so Boom still goes first
    getattr(PluginManager(plugins), method)(*args)  # must not raise
    assert len(calls) == 1


def test_a_failing_hook_without_a_name_is_still_isolated():
    class Partial(Plugin):
        name = "partial"

        def _boom(self, *args):
            raise RuntimeError("boom")

        on_first_frame = functools.partialmethod(_boom)

    PluginManager([Partial()]).on_first_frame(None)  # must not raise


def test_attach_gives_every_plugin_the_controller_and_the_broadcast():
    a, b = Plugin(), Plugin()
    manager = PluginManager([a, b])
    a.broadcast("topic", {})  # a no-op until attached
    controller, sent = object(), []
    manager.attach(controller=controller)
    manager.attach(broadcast=lambda topic, payload: sent.append(topic))
    assert a.controller is b.controller is controller  # a later attach keeps it
    a.broadcast("a_state", {})
    b.broadcast("b_state", {})
    assert sent == ["a_state", "b_state"]


def test_only_the_trigger_plugin_is_asked_for_the_train_and_priming():
    class Other(Plugin):
        name = "other"

        def trigger_train(self, params):
            raise AssertionError("asked a plugin that does not generate the trigger")

        def prime_trigger(self, params, pulses):
            raise AssertionError("asked a plugin that does not generate the trigger")

    class Board(Plugin):
        name = "board"
        generates_trigger = True

        def trigger_train(self, params):
            return {"period_ns": 10_000_000, "count": params["count"]}

        def prime_trigger(self, params, pulses):
            return pulses == 4

    assert PluginManager([Other()]).trigger_plugin() is None
    assert PluginManager([Other()]).trigger_train({}) is None
    assert PluginManager([Other()]).prime_trigger({}, 4) is False
    board = Board()
    manager = PluginManager([Other(), board])
    assert manager.trigger_plugin() is board
    assert manager.trigger_train({"board": {"count": 7}})["count"] == 7
    assert manager.prime_trigger({}, 4) is True


def test_a_ws_message_goes_to_the_first_plugin_that_claims_it():
    seen = []

    class Claims(Plugin):
        def __init__(self, name, claims):
            self.name, self.claims = name, claims

        def on_ws_message(self, message, client_id):
            seen.append(self.name)
            if self.claims == "raise":
                raise ValueError("bad message")
            return self.claims

    PluginManager([Claims("a", False), Claims("b", True), Claims("c", True)]).on_ws_message({}, 1)
    assert seen == ["a", "b"]
    seen.clear()
    # A raising hook is logged and counts as handled: the message goes no further.
    PluginManager([Claims("a", "raise"), Claims("b", True)]).on_ws_message({}, 1)
    assert seen == ["a"]


def test_each_hook_gets_its_own_slice():
    seen = []

    class Slice(Plugin):
        def __init__(self, name):
            self.name = name

        def on_recording_start(self, params):
            seen.append((self.name, params))

    manager = PluginManager([Slice("a"), Slice("b"), Slice("c"), Slice("d")])
    manager.on_recording_start({"a": {"x": 1}, "b": True, "d": {}})
    # b's slice is not a table, and c has none: both get None. d's empty table
    # is a slice (armed with defaults), not None.
    assert seen == [("a", {"x": 1}), ("b", None), ("c", None), ("d", {})]
    seen.clear()
    manager.on_recording_start(None)
    assert seen == [("a", None), ("b", None), ("c", None), ("d", None)]


def test_snapshot_options_lists_every_plugin():
    class Live(Plugin):
        name = "live"

        def snapshot_options(self, params):
            return {"lights": params["lights"]}

    class Unchanged(Plugin):
        name = "unchanged"  # the base hook: nothing differs from the config

    class Boom(Plugin):
        name = "boom"

        def snapshot_options(self, params):
            raise RuntimeError("boom")

    manager = PluginManager([Live(), Unchanged(), Boom()])
    # Every loaded plugin is keyed (so the snapshot lists it); only a plugin with
    # live changes contributes options, and a failing hook never raises.
    assert manager.snapshot_options({"live": {"lights": [1]}}) == {
        "live": {"lights": [1]},
        "unchanged": {},
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
