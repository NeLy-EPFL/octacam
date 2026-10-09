"""The opt-in plugins: a name -> class table and the loader.

A rig selects plugins in `[[plugins]]` (options in `[plugins.options]`) or
with `--plugin`; the default launch loads none.
"""

from __future__ import annotations

import importlib
import logging
import sys
from dataclasses import dataclass

from octacam.plugins.base import Plugin, PluginManager

log = logging.getLogger("octacam")

# name -> "module:Class", imported on first use.
_PLUGINS = {
    "flywheel": "octacam.plugins.flywheel:FlywheelPlugin",
    "twophoton": "octacam.plugins.twophoton:TwoPhotonPlugin",
    "triggerbox": "octacam.plugins.triggerbox.plugin:TriggerboxPlugin",
}

# Old plugin names, still loaded (with a warning) so no rig loses its plugin.
_ALIASES = {"arduino": "flywheel"}


def canonical_name(name: str) -> str:
    """The current name for a configured plugin name (`arduino` -> `flywheel`)."""
    return _ALIASES.get(name, name)


def plugin_class(name: str) -> type[Plugin]:
    """The plugin class named *name*, imported on first use. Raises KeyError for
    an unknown name, and whatever its module raises when it fails to import.
    """
    module, _, cls = _PLUGINS[name].partition(":")
    return getattr(importlib.import_module(module), cls)


def _resolve_selection(config_plugins, enabled) -> list[tuple[str, dict]]:
    """`[(name, options)]` from the config and the CLI's `enabled`: None
    keeps the config, `[]` is `--no-plugins`, and names from `--plugin`
    are added to the config's.
    """
    selection = [(p.name, dict(p.options)) for p in config_plugins]
    if enabled is None:
        return selection
    if not enabled:  # --no-plugins
        return []
    known = {name for name, _ in selection}
    for name in enabled:
        if name not in known:
            selection.append((name, {}))
            known.add(name)
    return selection


def build_plugins(config, enabled: list[str] | None = None) -> PluginManager:
    """The configured and enabled plugins; one that is unknown, or fails to
    import or to build, is logged and skipped.
    """
    plugins: list[Plugin] = []
    seen: set[str] = set()
    for name, options in _resolve_selection(config.plugins, enabled):
        canonical = canonical_name(name)
        if canonical != name:
            log.warning(
                "Plugin %r was renamed to %r; please update your config / "
                "--plugin flag (loading %r for now).",
                name,
                canonical,
                canonical,
            )
            name = canonical
        if name in seen:
            continue  # after aliasing, so arduino + flywheel load once
        seen.add(name)
        if name not in _PLUGINS:
            log.warning("Unknown plugin %r; skipping", name)
            continue
        # A bundled module that fails to import is a real fault, not a typo.
        try:
            cls = plugin_class(name)
        except Exception as e:
            log.warning("Builtin plugin %r failed to import (%s); skipping", name, e)
            continue
        try:
            plugins.append(cls.from_options(options))
        except Exception as e:
            log.warning("Plugin %r failed to load (%s); skipping", name, e)
    if plugins:
        log.info("Loaded plugin(s): %s", ", ".join(p.name for p in plugins))
    return PluginManager(plugins)


@dataclass(frozen=True)
class PluginInfo:
    """A plugin and whether it builds right now; `detail` says why not."""

    name: str
    summary: str
    available: bool
    detail: str = ""


def available_plugins() -> list[PluginInfo]:
    """Every plugin build_plugins could load, each built with no options to see
    whether it can be; the summary is its module docstring's first line.
    """
    infos: list[PluginInfo] = []
    for name in _PLUGINS:
        try:
            cls = plugin_class(name)
        except Exception:
            infos.append(
                PluginInfo(name, "", available=False, detail="module failed to import")
            )
            continue
        doc = (sys.modules[cls.__module__].__doc__ or "").strip()
        summary = doc.splitlines()[0] if doc else ""
        try:
            cls.from_options({})
            infos.append(PluginInfo(name, summary, available=True))
        except Exception as e:
            infos.append(PluginInfo(name, summary, available=False, detail=str(e)))
    return infos
