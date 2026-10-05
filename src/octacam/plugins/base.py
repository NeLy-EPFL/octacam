"""The plugin contract and the manager that calls it. Hooks must be thread-safe:
on_first_frame (at the countdown's t0: never block) and on_recording_stop run on
the monitor thread, the start and preview hooks off the controller lock."""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, ClassVar

if TYPE_CHECKING:
    from pathlib import Path

    from fastapi import APIRouter

    from octacam.controller import RecordingController

log = logging.getLogger("octacam")


class Plugin:
    """The hooks the core calls, all no-ops by default. A hook's ``params`` is
    this plugin's slice of the start request, or None."""

    name: str = "plugin"
    # Drives the managed trigger. A recording counts frames against its
    # trigger_train; on_preview_stop runs before the record grab starts, so
    # on_recording_start arms idle cameras.
    generates_trigger: ClassVar[bool] = False
    web_dir: ClassVar[Path | None] = None  # serves <name>.js (+ <name>.css)
    controller: RecordingController | None = None  # set by PluginManager.attach

    def broadcast(self, topic: str, payload: dict, /) -> None:
        """Push ``payload`` to every GUI client as ``topic``; a no-op until attached."""

    @classmethod
    def from_options(cls, options: dict) -> Plugin:
        """The plugin a ``[plugins.options]`` table configures; raises if it can't."""
        return cls()

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

    def default_start_params(self, fps: float, duration_s: float) -> dict | None:
        """The slice headless ``octacam record`` starts with, as the GUI sends its
        tab's: a plugin that arms a board at record start needs one."""
        return None

    def snapshot_options(self, params: dict | None) -> dict | None:
        """The ``[[plugins]]`` options reproducing this plugin's live state in the
        config snapshot, None when the configured ones do. No I/O: under the lock."""
        return None

    def on_preview_start(self, params: dict | None) -> None:
        pass

    def on_preview_stop(self) -> None:
        pass

    def trigger_train(self, params: dict | None) -> dict | None:
        """The exact ``{"period_ns", "count"}`` on_recording_start emits for
        these params. Pure: called under the controller lock."""
        return None

    def prime_trigger(self, params: dict | None, pulses: int) -> bool:
        """Emit ``pulses`` sacrificial camera pulses, lights dark, and return once
        they are out; True if it did. Off the lock, before on_recording_start."""
        return False

    def api_router(self) -> APIRouter | None:
        return None

    def on_ws_message(self, message: dict, client_id: int) -> bool:
        """True if the message was this plugin's; ``client_id`` names the socket."""
        return False

    def on_ws_disconnect(self, client_id: int) -> None:
        pass


class PluginManager:
    """The active plugins, called in load order (teardown in reverse); a hook
    that raises is logged and skipped, never fatal."""

    def __init__(self, plugins: list[Plugin] | None = None):
        self.plugins: list[Plugin] = list(plugins or [])

    def attach(
        self,
        *,
        controller: RecordingController | None = None,
        broadcast: Callable[[str, dict], None] | None = None,
    ) -> None:
        for plugin in self.plugins:
            if controller is not None:
                plugin.controller = controller
            if broadcast is not None:
                plugin.broadcast = broadcast

    @staticmethod
    def _slice(plugin: Plugin, params: dict | None) -> dict | None:
        slice_ = (params or {}).get(plugin.name)
        return slice_ if isinstance(slice_, dict) else None

    @staticmethod
    def _call(plugin: Plugin, hook: Callable[..., Any], *args, default=None) -> Any:
        try:
            return hook(*args)
        except Exception:
            log.exception("Plugin %s.%s failed", plugin.name, hook.__name__)
            return default

    def setup_all(self) -> None:
        for plugin in self.plugins:
            self._call(plugin, plugin.setup)

    def teardown_all(self) -> None:
        for plugin in reversed(self.plugins):
            self._call(plugin, plugin.teardown)

    def on_preview_start(self, params: dict | None) -> None:
        for plugin in self.plugins:
            self._call(plugin, plugin.on_preview_start, self._slice(plugin, params))

    def on_preview_stop(self) -> None:
        for plugin in self.plugins:
            self._call(plugin, plugin.on_preview_stop)

    def on_recording_start(self, params: dict | None) -> None:
        for plugin in self.plugins:
            self._call(plugin, plugin.on_recording_start, self._slice(plugin, params))

    def on_first_frame(self, params: dict | None) -> None:
        for plugin in self.plugins:
            self._call(plugin, plugin.on_first_frame, self._slice(plugin, params))

    def on_recording_stop(self, aborted: bool) -> None:
        for plugin in self.plugins:
            self._call(plugin, plugin.on_recording_stop, aborted)

    def on_ws_message(self, message: dict, client_id: int) -> None:
        """Offer a message to each plugin until one claims it; a raising hook
        claims it, so a bad message reaches no further plugin."""
        for plugin in self.plugins:
            if self._call(plugin, plugin.on_ws_message, message, client_id, default=True):
                return

    def on_ws_disconnect(self, client_id: int) -> None:
        for plugin in self.plugins:
            self._call(plugin, plugin.on_ws_disconnect, client_id)

    def trigger_plugin(self) -> Plugin | None:
        return next((p for p in self.plugins if p.generates_trigger), None)

    def trigger_train(self, params: dict | None) -> dict | None:
        plugin = self.trigger_plugin()
        if plugin is None:
            return None
        return self._call(plugin, plugin.trigger_train, self._slice(plugin, params))

    def prime_trigger(self, params: dict | None, pulses: int) -> bool:
        plugin = self.trigger_plugin()
        if plugin is None:
            return False
        slice_ = self._slice(plugin, params)
        return bool(self._call(plugin, plugin.prime_trigger, slice_, pulses))

    def default_start_params(self, fps: float, duration_s: float) -> dict:
        """Each plugin's non-None headless start slice, keyed by name as the GUI
        sends them."""
        params: dict = {}
        for plugin in self.plugins:
            slice_ = self._call(plugin, plugin.default_start_params, fps, duration_s)
            if slice_ is not None:
                params[plugin.name] = slice_
        return params

    def snapshot_options(self, params: dict | None) -> dict[str, dict]:
        """Every plugin's snapshot options by name, ``{}`` when its config
        reproduces it."""
        result: dict[str, dict] = {}
        for plugin in self.plugins:
            slice_ = self._slice(plugin, params)
            result[plugin.name] = dict(
                self._call(plugin, plugin.snapshot_options, slice_) or {}
            )
        return result

    def status(self) -> dict:
        """Each plugin's status() with its is_ready() as ``ready``, which wins
        over a "ready" key; a failing status() drops only the details."""
        result: dict = {}
        for plugin in self.plugins:
            ready = self._call(plugin, plugin.is_ready)
            if ready is None:  # is_ready raised
                result[plugin.name] = {"ready": False}
                continue
            extra = self._call(plugin, plugin.status, default={})
            result[plugin.name] = {**extra, "ready": ready}
        return result
