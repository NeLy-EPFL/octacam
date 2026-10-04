"""The opt-in plugins: a name -> factory registry and the loader.

A rig selects plugins in ``[[plugins]]`` (options in ``[plugins.options]``) or
with ``--plugin``; the default launch loads none. Another package can add one
through the ``octacam.plugins`` entry-point group, but a bundled name wins.
"""

from __future__ import annotations

import importlib
import logging
import sys
from collections.abc import Callable
from dataclasses import dataclass

from octacam.plugins.base import OctacamPlugin, Plugin, PluginManager

log = logging.getLogger("octacam")

__all__ = [
    "OctacamPlugin",
    "Plugin",
    "PluginManager",
    "PluginInfo",
    "register",
    "build_plugins",
    "available_plugins",
]

# Filled by @register as each plugin module is imported.
_REGISTRY: dict[str, Callable[[dict], OctacamPlugin]] = {}

# octacam.plugins.<name>, imported on demand.
_BUILTINS = ("flywheel", "twophoton", "triggerbox")

# Old plugin names, still loaded (with a warning) so no rig loses its plugin.
_ALIASES = {"arduino": "flywheel"}


def canonical_name(name: str) -> str:
    """The current name for a configured plugin name (``arduino`` -> ``flywheel``)."""
    return _ALIASES.get(name, name)


def _discover_entry_points() -> None:
    """Import the plugins other packages register, e.g.::

        [project.entry-points."octacam.plugins"]
        mydevice = "octacam_mydevice.plugin:_build"

    One that fails is logged at debug level: it must not stop octacam."""
    try:
        from importlib.metadata import entry_points

        for ep in entry_points(group="octacam.plugins"):
            if ep.name in _BUILTINS or ep.name in _REGISTRY:
                continue
            try:
                ep.load()
                log.debug("Loaded entry-point plugin %r from %s", ep.name, ep.value)
            except Exception as e:
                log.debug("Entry-point plugin %r failed to load: %s", ep.name, e)
    except Exception as e:
        log.debug("Entry-point plugin discovery failed: %s", e)


def register(name: str):
    """Decorator registering a plugin factory under *name*."""

    def decorator(factory: Callable[[dict], OctacamPlugin]):
        _REGISTRY[name] = factory
        return factory

    return decorator


def _import_builtin(name: str) -> None:
    """Import a bundled plugin so its @register runs. A failure is a real fault,
    not a missing optional plugin, so its cause is logged at warning level."""
    if name in _REGISTRY or name not in _BUILTINS:
        return
    try:
        importlib.import_module(f"octacam.plugins.{name}")
    except Exception as e:
        log.warning("Builtin plugin module %r could not be imported: %s", name, e)


def _resolve_selection(config_plugins, enabled) -> list[tuple[str, dict]]:
    """``[(name, options)]`` from the config and the CLI's ``enabled``: None
    keeps the config, ``[]`` is ``--no-plugins``, and names from ``--plugin``
    are added to the config's."""
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
    """The configured and enabled plugins; one that is unknown or fails to
    build is logged and skipped."""
    _discover_entry_points()
    selection = _resolve_selection(config.plugins, enabled)
    plugins: list[OctacamPlugin] = []
    seen: set[str] = set()
    for name, options in selection:
        canonical = _ALIASES.get(name)
        if canonical is not None:
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
        _import_builtin(name)
        factory = _REGISTRY.get(name)
        if factory is None:
            if name in _BUILTINS:
                log.warning(
                    "Builtin plugin %r failed to import (cause logged above); skipping",
                    name,
                )
            else:
                log.warning("Unknown plugin %r; skipping", name)
            continue
        try:
            plugins.append(factory(options))
        except Exception as e:
            log.warning("Plugin %r failed to load (%s); skipping", name, e)
    if plugins:
        log.info("Loaded plugin(s): %s", ", ".join(p.name for p in plugins))
    return PluginManager(plugins)


@dataclass(frozen=True)
class PluginInfo:
    """A plugin and whether it builds right now; ``detail`` says why not."""

    name: str
    summary: str
    available: bool
    detail: str = ""


def _plugin_summary(name: str) -> str:
    """First line of a plugin's module docstring, else of its factory's module
    (a third-party plugin has no ``octacam.plugins.<name>``)."""
    # Not getattr(module, "__doc__"): for a missing module that is NoneType's
    # docstring, which would mask the fallback.
    mod = sys.modules.get(f"octacam.plugins.{name}")
    doc = (mod.__doc__ or "").strip() if mod is not None else ""
    if not doc:
        factory = _REGISTRY.get(name)
        factory_module = (
            sys.modules.get(getattr(factory, "__module__", "")) if factory else None
        )
        doc = (factory_module.__doc__ or "").strip() if factory_module is not None else ""
    return doc.splitlines()[0] if doc else ""


def available_plugins() -> list[PluginInfo]:
    """Every plugin build_plugins could load, bundled ones first, each built
    with no options to see whether it can be."""
    _discover_entry_points()
    infos: list[PluginInfo] = []
    names = list(_BUILTINS) + [n for n in _REGISTRY if n not in _BUILTINS]
    for name in names:
        if name in _BUILTINS:
            _import_builtin(name)
        summary = _plugin_summary(name)
        factory = _REGISTRY.get(name)
        if factory is None:
            infos.append(
                PluginInfo(
                    name, summary, available=False, detail="module failed to import"
                )
            )
            continue
        try:
            factory({})
            infos.append(PluginInfo(name, summary, available=True))
        except Exception as e:
            infos.append(PluginInfo(name, summary, available=False, detail=str(e)))
    return infos
