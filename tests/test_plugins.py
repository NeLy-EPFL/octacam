"""Plugin registry + manager behavior."""

from octacam.config import OctacamConfig, PluginConfig
from octacam.plugins import PluginManager, build_plugins, register
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
