"""The plugin contract and the manager that fans hooks out to the plugins.

Hooks run synchronously and must be thread-safe. ``on_first_frame`` and
``on_recording_stop`` run on the controller's monitor thread, ``on_first_frame``
at the countdown's t0, so it must not block. The start and preview hooks run on
the caller's thread, off the controller lock.
"""

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
    """Every hook the core calls, each a no-op by default.

    A hook's ``params`` is the start request's ``{plugin name: slice}`` dict.
    """

    name: str = "plugin"
    # The plugin that generates the trigger (triggerbox). A managed recording
    # counts frames against its trigger_train and primes with prime_trigger, and
    # a managed preview runs on its trigger: armed indefinitely by
    # on_preview_start, disarmed by on_preview_stop. on_preview_stop also runs
    # before a recording's record grab starts, so on_recording_start arms
    # against idle cameras (see RecordingController.start_recording).
    generates_trigger: ClassVar[bool] = False
    # JS/CSS served under /plugins/<name>/ (<name>.js, optional <name>.css);
    # None = no UI.
    web_dir: ClassVar[Path | None] = None
    # Set by PluginManager.attach.
    controller: RecordingController | None = None

    def broadcast(self, topic: str, payload: dict, /) -> None:
        """Push ``payload`` to every GUI client as a ``topic`` message; a no-op
        until PluginManager.attach gives the web app's."""

    @classmethod
    def from_options(cls, options: dict) -> Plugin:
        """The plugin its ``[plugins.options]`` table configures; raises when it
        cannot be built."""
        return cls()

    # ---- process lifecycle ----

    def setup(self) -> None:
        pass

    def teardown(self) -> None:
        pass

    def is_ready(self) -> bool:
        return True

    def status(self) -> dict:
        return {}

    # ---- recording lifecycle ----

    def on_recording_start(self, params: dict | None) -> None:
        pass

    def on_first_frame(self, params: dict | None) -> None:
        pass

    def on_recording_stop(self, aborted: bool) -> None:
        pass

    def default_start_params(self, fps: float, duration_s: float) -> dict | None:
        """The slice headless ``octacam record`` starts with (the GUI sends its
        tab's); a plugin that acts at record start, such as arming a board,
        needs one. None: nothing to contribute."""
        return None

    def snapshot_options(self, params: dict | None) -> dict | None:
        """The ``[[plugins]]`` options that reproduce this plugin's live state in
        the recording's config snapshot; None when the configured ones do.
        Called under the controller lock: no I/O."""
        return None

    # ---- the trigger (generates_trigger) ----

    def on_preview_start(self, params: dict | None) -> None:
        pass

    def on_preview_stop(self) -> None:
        pass

    def trigger_train(self, params: dict | None) -> dict | None:
        """The exact ``{"period_ns", "count"}`` on_recording_start emits for
        these params, which the recording counts frames against. Pure: called
        under the controller lock."""
        return None

    def prime_trigger(self, params: dict | None, pulses: int) -> bool:
        """Emit ``pulses`` sacrificial pulses on the camera lines, lights dark,
        and return once they are out; True if it did. Called off the lock,
        before on_recording_start (see "Priming" in CLAUDE.md)."""
        return False

    # ---- web ----

    def api_router(self) -> APIRouter | None:
        return None

    def on_ws_message(self, message: dict, client_id: int) -> bool:
        """Handle a GUI WebSocket message; True if it was this plugin's.
        ``client_id`` names the socket, so per-connection state (a hold-to-jog)
        stays with it."""
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
        """Give every plugin the controller (RecordingController.__init__) or
        the web app's broadcast (create_app)."""
        for plugin in self.plugins:
            if controller is not None:
                plugin.controller = controller
            if broadcast is not None:
                plugin.broadcast = broadcast

    @staticmethod
    def _call(plugin: Plugin, hook: Callable[..., Any], *args, default=None) -> Any:
        """``hook(*args)``, a bound method of ``plugin``; ``default`` when it raises."""
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
            self._call(plugin, plugin.on_preview_start, params)

    def on_preview_stop(self) -> None:
        for plugin in self.plugins:
            self._call(plugin, plugin.on_preview_stop)

    def on_recording_start(self, params: dict | None) -> None:
        for plugin in self.plugins:
            self._call(plugin, plugin.on_recording_start, params)

    def on_first_frame(self, params: dict | None) -> None:
        for plugin in self.plugins:
            self._call(plugin, plugin.on_first_frame, params)

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
        """The first plugin that generates the trigger, else None (a managed
        preview then free-runs)."""
        return next((p for p in self.plugins if p.generates_trigger), None)

    def trigger_train(self, params: dict | None) -> dict | None:
        """The train the trigger plugin's recording arm emits, else None."""
        plugin = self.trigger_plugin()
        if plugin is None:
            return None
        return self._call(plugin, plugin.trigger_train, params)

    def prime_trigger(self, params: dict | None, pulses: int) -> bool:
        """Have the trigger plugin emit ``pulses`` priming pulses; True if it did."""
        plugin = self.trigger_plugin()
        if plugin is None:
            return False
        return bool(self._call(plugin, plugin.prime_trigger, params, pulses))

    def default_start_params(self, fps: float, duration_s: float) -> dict:
        """Each plugin's headless start slice, keyed by name as the GUI sends
        them; plugins returning None are left out."""
        params: dict = {}
        for plugin in self.plugins:
            slice_ = self._call(plugin, plugin.default_start_params, fps, duration_s)
            if slice_ is not None:
                params[plugin.name] = slice_
        return params

    def snapshot_options(self, params: dict | None) -> dict[str, dict]:
        """Each plugin's snapshot options, keyed by name for every plugin (empty
        when its config already reproduces it) so one enabled with ``--plugin``
        is listed too."""
        return {
            plugin.name: dict(self._call(plugin, plugin.snapshot_options, params) or {})
            for plugin in self.plugins
        }

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
