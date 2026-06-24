"""Plugin contract for octacam.

A plugin is any object implementing (a subset of) the ``OctacamPlugin``
Protocol. Hooks are called synchronously and must be thread-safe and fast.
``on_first_frame`` and ``on_recording_stop`` fire from the controller's monitor
thread; ``on_recording_start`` fires on the caller's thread (the web executor or
the CLI thread) just after a recording starts, off the controller lock.
``on_first_frame`` in particular runs at the t0 of the recording countdown, so
it must not block.

Plugins are bundled in-repo under ``octacam.plugins.<name>`` and registered via
the ``@register`` decorator (see :mod:`octacam.plugins`). They are opt-in: the
default launch loads none. A user enables them through the ``[[plugins]]``
section of ``octacam_config.toml`` or the ``--plugin`` CLI flag.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from fastapi import APIRouter

log = logging.getLogger("octacam")


@runtime_checkable
class OctacamPlugin(Protocol):
    """Structural contract for an octacam plugin.

    Implementations typically subclass :class:`Plugin` (which supplies no-op
    defaults) and override only the hooks they need.
    """

    name: str

    # ---- process lifecycle (octacam gui/record startup & shutdown) ----
    def setup(self) -> None: ...
    def teardown(self) -> None: ...
    def is_ready(self) -> bool: ...
    def status(self) -> dict: ...

    # ---- recording lifecycle ----
    # on_first_frame/on_recording_stop run on the controller monitor thread;
    # on_recording_start runs on the caller's thread just after start.
    # params is this plugin's slice of the recording-start request, keyed by
    # plugin name (e.g. {"flywheel": {...}}).
    def on_recording_start(self, params: dict | None) -> None: ...
    def on_first_frame(self, params: dict | None) -> None: ...
    def on_recording_stop(self, aborted: bool) -> None: ...

    # ---- web contribution (optional) ----
    # client_id identifies the WebSocket connection a message/disconnect came
    # from, so a plugin can scope per-connection state (e.g. a hold-to-jog) to
    # the socket that owns it.
    def api_router(self) -> APIRouter | None: ...
    def on_ws_message(
        self, message: dict, client_id: int
    ) -> bool: ...  # True = handled
    def on_ws_disconnect(self, client_id: int) -> None: ...  # a control socket closed


class Plugin:
    """Base class with safe no-op defaults for every hook.

    Subclass it and override only the hooks a given plugin actually needs.
    """

    name: str = "plugin"

    def setup(self) -> None:
        pass

    def teardown(self) -> None:
        pass

    def is_ready(self) -> bool:
        return True

    def status(self) -> dict:
        return {}

    def on_recording_start(self, params: dict | None) -> None:
        pass

    def on_first_frame(self, params: dict | None) -> None:
        pass

    def on_recording_stop(self, aborted: bool) -> None:
        pass

    def api_router(self) -> APIRouter | None:
        return None

    def on_ws_message(self, message: dict, client_id: int) -> bool:
        return False

    def on_ws_disconnect(self, client_id: int) -> None:
        pass


class PluginManager:
    """Holds the active plugins and fans hooks out to them.

    Every call is wrapped so a misbehaving plugin logs and is skipped rather
    than crashing the caller (mirrors ``RecordingController._notify``).
    """

    def __init__(self, plugins: list[OctacamPlugin] | None = None):
        self.plugins: list[OctacamPlugin] = list(plugins or [])

    def _name(self, plugin) -> str:
        return getattr(plugin, "name", repr(plugin))

    def setup_all(self) -> None:
        for plugin in self.plugins:
            try:
                plugin.setup()
            except Exception:
                log.exception("Plugin %s setup failed", self._name(plugin))

    def teardown_all(self) -> None:
        for plugin in reversed(self.plugins):
            try:
                plugin.teardown()
            except Exception:
                log.exception("Plugin %s teardown failed", self._name(plugin))

    def dispatch(self, hook: str, *args) -> None:
        for plugin in self.plugins:
            try:
                getattr(plugin, hook)(*args)
            except Exception:
                log.exception("Plugin %s.%s failed", self._name(plugin), hook)

    def status(self) -> dict:
        result: dict = {}
        for plugin in self.plugins:
            try:
                result[plugin.name] = {"ready": plugin.is_ready(), **plugin.status()}
            except Exception:
                log.exception("Plugin %s status failed", self._name(plugin))
                result[self._name(plugin)] = {"ready": False}
        return result
