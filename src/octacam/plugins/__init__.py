"""Bundled, opt-in plugin registry for octacam.

Plugins are selected by name from the ``[[plugins]]`` section of
``octacam_config.toml`` (each entry may carry an ``[plugins.options]`` table)
and/or the ``--plugin`` CLI flag. The default launch loads none. Selection
resolves to instantiated
plugins via a name -> factory registry, mirroring ``writer.FORMATS``.

Bundled plugins under ``octacam.plugins.<name>`` are always preferred; third-
party plugins may register additional names via the ``octacam.plugins``
entry-point group.
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

# name -> factory(options: dict) -> OctacamPlugin. Populated by @register when a
# plugin module is imported (lazily, in build_plugins).
_REGISTRY: dict[str, Callable[[dict], OctacamPlugin]] = {}

# Bundled plugins live at octacam.plugins.<name>; importing the module runs its
# @register call. Listed here so build_plugins knows what it may import.
_BUILTINS = ("flywheel", "twophoton")

# Legacy plugin names → current name. The stepper plugin was renamed
# arduino → flywheel; existing rig configs (name = "arduino") and
# `--plugin arduino` still resolve, with a deprecation warning, so upgrading
# does not silently drop a configured plugin.
_ALIASES = {"arduino": "flywheel"}


def _discover_entry_points() -> None:
    """Load third-party plugins registered under the ``octacam.plugins`` group.

    A separate package (e.g. ``octacam-twophoton``) can register plugins via::

        [project.entry-points."octacam.plugins"]
        twophoton = "octacam_twophoton.plugin:_build"

    Importing the entry point runs its ``@register`` call, making the plugin
    available to :func:`build_plugins` without any changes to octacam core.
    Failures are silently downgraded to debug logs; a broken third-party plugin
    must not prevent the core from starting.
    """
    try:
        from importlib.metadata import entry_points

        for ep in entry_points(group="octacam.plugins"):
            if ep.name in _BUILTINS or ep.name in _REGISTRY:
                continue  # builtins always win; skip already-loaded names
            try:
                ep.load()
                log.debug("Loaded entry-point plugin %r from %s", ep.name, ep.value)
            except Exception as e:
                log.debug("Entry-point plugin %r failed to load: %s", ep.name, e)
    except Exception as e:
        log.debug("Entry-point plugin discovery failed: %s", e)


def register(name: str):
    """Decorator registering a factory under ``name`` (mirrors ``FORMATS``)."""

    def decorator(factory: Callable[[dict], OctacamPlugin]):
        _REGISTRY[name] = factory
        return factory

    return decorator


def _import_builtin(name: str) -> None:
    """Lazily import a bundled plugin module so its @register call runs.

    A missing optional dependency (or any import error) is downgraded to a
    debug log; ``build_plugins`` then reports it as a skipped plugin.
    """
    if name in _REGISTRY or name not in _BUILTINS:
        return
    try:
        importlib.import_module(f"octacam.plugins.{name}")
    except Exception as e:
        log.debug("Plugin module %r could not be imported: %s", name, e)


def _resolve_selection(config_plugins, enabled) -> list[tuple[str, dict]]:
    """Merge config plugins with the CLI override into ``[(name, options)]``.

    ``enabled`` is ``None`` for no override (use the config as-is), an empty
    list for ``--no-plugins`` (disable everything), or a list of names from
    ``--plugin`` that are *added* to the config selection.
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
    """Resolve the configured/enabled plugins into a :class:`PluginManager`.

    Unknown names and plugins whose optional dependency is missing are logged
    and skipped — core always keeps running.
    """
    _discover_entry_points()
    selection = _resolve_selection(getattr(config, "plugins", []), enabled)
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
            continue  # dedup after aliasing so arduino + flywheel don't double-load
        seen.add(name)
        _import_builtin(name)
        factory = _REGISTRY.get(name)
        if factory is None:
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
    """A bundled plugin and whether it can currently be loaded.

    ``available`` is False when the plugin's optional dependency is missing; in
    that case ``detail`` carries the reason (typically the install hint).
    """

    name: str
    summary: str
    available: bool
    detail: str = ""


def _plugin_summary(name: str) -> str:
    """First line of a plugin's module docstring (best-effort).

    Bundled plugins live at ``octacam.plugins.<name>``; a third-party plugin
    registered through the entry-point group lives in its own module, so fall
    back to the registered factory's module when the bundled path misses."""
    module = sys.modules.get(f"octacam.plugins.{name}")
    if module is None:
        factory = _REGISTRY.get(name)
        module = (
            sys.modules.get(getattr(factory, "__module__", "")) if factory else None
        )
    doc = (getattr(module, "__doc__", None) or "").strip()
    return doc.splitlines()[0] if doc else ""


def available_plugins() -> list[PluginInfo]:
    """Describe every loadable plugin and whether it can load right now.

    Mirrors :func:`build_plugins`: each bundled builtin (and any third-party
    plugin discovered via the ``octacam.plugins`` entry-point group) is dry-run
    built with no options. A plugin whose dependency is missing raises during
    that build; it is reported as ``available=False`` with the error as
    ``detail`` rather than propagating.
    """
    _discover_entry_points()
    infos: list[PluginInfo] = []
    # Bundled builtins first, then any third-party names discovered via entry
    # points, so `list-plugins` reflects everything build_plugins could load
    # rather than only the builtins.
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
