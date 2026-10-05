"""The plugin contract and the manager that fans hooks out to the plugins.

Hooks run synchronously and must be thread-safe. ``on_first_frame`` and
``on_recording_stop`` run on the controller's monitor thread, ``on_first_frame``
at the countdown's t0, so it must not block. The start and preview hooks run on
the caller's thread, off the controller lock.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from pathlib import Path

    from fastapi import APIRouter

log = logging.getLogger("octacam")


@runtime_checkable
class OctacamPlugin(Protocol):
    """Structural contract for a plugin; :class:`Plugin` gives each hook a no-op."""

    name: str

    # ---- process lifecycle ----
    def setup(self) -> None: ...
    def teardown(self) -> None: ...
    def is_ready(self) -> bool: ...
    def status(self) -> dict: ...

    # ---- recording lifecycle ----
    # params is the start request's {plugin name: slice} dict.
    def on_recording_start(self, params: dict | None) -> None: ...
    def on_first_frame(self, params: dict | None) -> None: ...
    def on_recording_stop(self, aborted: bool) -> None: ...

    # The slice headless `octacam record` starts with (the GUI sends its tab's);
    # a plugin that must act at record start, such as arming a board, needs it.
    # None = nothing to contribute.
    def default_start_params(self, fps: float, duration_s: float) -> dict | None: ...

    # The [[plugins]] options that reproduce this plugin's live state in the
    # recording's config snapshot; None = the configured ones already do.
    # Called under the controller lock: no I/O.
    def snapshot_options(self, params: dict | None) -> dict | None: ...

    # ---- preview (a plugin that generates the trigger) ----
    # drives_preview_trigger() -> True: the preview runs on this plugin's trigger,
    # armed indefinitely by on_preview_start and disarmed by on_preview_stop, so
    # it looks like the recording. on_preview_stop also runs before a recording's
    # record grab starts, so on_recording_start arms against idle cameras (see
    # RecordingController.start_recording).
    def drives_preview_trigger(self) -> bool: ...
    def on_preview_start(self, params: dict | None) -> None: ...
    def on_preview_stop(self) -> None: ...

    # ---- trigger train (a plugin that generates the trigger) ----
    # trigger_train: the exact {"period_ns", "count"} on_recording_start emits
    # for these params, which the recording counts frames against (pure; called
    # under the controller lock). prime_trigger: emit `pulses` sacrificial pulses
    # on the camera lines, lights dark, and return once they are out; True if it
    # did (called off the lock, before on_recording_start; see CLAUDE.md).
    def trigger_train(self, params: dict | None) -> dict | None: ...
    def prime_trigger(self, params: dict | None, pulses: int) -> bool: ...

    # ---- web ----
    # client_id names the WebSocket a message or disconnect came from, so
    # per-connection state (a hold-to-jog) stays with its socket.
    def api_router(self) -> APIRouter | None: ...
    def on_ws_message(
        self, message: dict, client_id: int
    ) -> bool: ...  # True = handled
    def on_ws_disconnect(self, client_id: int) -> None: ...  # a control socket closed

    # JS/CSS served under /plugins/<name>/ (<name>.js, optional <name>.css);
    # None = no UI.
    def web_assets(self) -> Path | None: ...


class Plugin:
    """No-op defaults for every hook."""

    name: str = "plugin"

    @classmethod
    def from_options(cls, options: dict) -> Plugin:
        """The plugin its ``[plugins.options]`` table configures; raises when it
        cannot be built."""
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
        return None

    def snapshot_options(self, params: dict | None) -> dict | None:
        return None

    def drives_preview_trigger(self) -> bool:
        return False

    def on_preview_start(self, params: dict | None) -> None:
        pass

    def on_preview_stop(self) -> None:
        pass

    def trigger_train(self, params: dict | None) -> dict | None:
        return None

    def prime_trigger(self, params: dict | None, pulses: int) -> bool:
        return False

    def api_router(self) -> APIRouter | None:
        return None

    def on_ws_message(self, message: dict, client_id: int) -> bool:
        return False

    def on_ws_disconnect(self, client_id: int) -> None:
        pass

    def web_assets(self) -> Path | None:
        return None


class PluginManager:
    """The active plugins; a hook that raises is logged and skipped, never fatal."""

    def __init__(self, plugins: list[Plugin] | None = None):
        self.plugins: list[Plugin] = list(plugins or [])

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

    def default_start_params(self, fps: float, duration_s: float) -> dict:
        """Each plugin's headless start slice, keyed by name as the GUI sends
        them; plugins returning None are left out."""
        params: dict = {}
        for plugin in self.plugins:
            try:
                slice_ = plugin.default_start_params(fps, duration_s)
            except Exception:
                log.exception(
                    "Plugin %s.default_start_params failed", self._name(plugin)
                )
                slice_ = None
            if slice_ is not None:
                params[self._name(plugin)] = slice_
        return params

    def trigger_train(self, params: dict | None) -> dict | None:
        """The train of the first plugin that generates one, else None."""
        for plugin in self.plugins:
            hook = getattr(plugin, "trigger_train", None)
            if hook is None:
                continue
            try:
                train = hook(params)
            except Exception:
                log.exception("Plugin %s.trigger_train failed", self._name(plugin))
                continue
            if train:
                return train
        return None

    def prime_trigger(self, params: dict | None, pulses: int) -> bool:
        """Have the trigger-generating plugin emit *pulses* priming pulses; True
        if one did."""
        for plugin in self.plugins:
            hook = getattr(plugin, "prime_trigger", None)
            if hook is None:
                continue
            try:
                if hook(params, pulses):
                    return True
            except Exception:
                log.exception("Plugin %s.prime_trigger failed", self._name(plugin))
        return False

    def snapshot_options(self, params: dict | None) -> dict[str, dict]:
        """Each plugin's snapshot options, keyed by name for every plugin (empty
        when its config already reproduces it) so one enabled with ``--plugin``
        is listed too. A failing hook contributes nothing."""
        result: dict[str, dict] = {}
        for plugin in self.plugins:
            name = self._name(plugin)
            hook = getattr(plugin, "snapshot_options", None)
            try:
                options = hook(params) if hook is not None else None
            except Exception:
                log.exception("Plugin %s.snapshot_options failed", name)
                options = None
            result[name] = dict(options or {})
        return result

    def status(self) -> dict:
        result: dict = {}
        for plugin in self.plugins:
            name = self._name(plugin)
            try:
                ready = plugin.is_ready()
            except Exception:
                log.exception("Plugin %s is_ready failed", name)
                result[name] = {"ready": False}
                continue
            # A failing status() must not flip a ready plugin, and is_ready()
            # wins over a "ready" key in status().
            try:
                extra = plugin.status()
            except Exception:
                log.exception("Plugin %s status failed", name)
                extra = {}
            result[name] = {**extra, "ready": ready}
        return result
