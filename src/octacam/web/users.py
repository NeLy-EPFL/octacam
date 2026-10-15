"""Users and readiness routes: the rig's users (pick, add, suggested paths)
and the Record tab's "Ready?" checks.
"""

from __future__ import annotations

import dataclasses
import shutil
import time
from pathlib import Path

from fastapi import APIRouter, HTTPException
from pydantic import Field, field_validator

from octacam import users as users_mod
from octacam.config import USER_INITIALS_RE
from octacam.web.state import AppState, StrictModel


class SelectUserRequest(StrictModel):
    initials: str = ""  # "" for no user


class NewUserRequest(StrictModel):
    initials: str
    name: str = ""
    transfer_directory: str = ""
    plugins: dict[str, dict[str, str]] = Field(default_factory=dict)

    @field_validator("initials")
    @classmethod
    def _initials(cls, value: str) -> str:
        value = value.strip().upper()
        if not USER_INITIALS_RE.match(value):
            raise ValueError("initials are 2 to 4 capital letters, like MD")
        return value


# Checks touching the share or the NAS are cached briefly: the strip refreshes
# often, and a hung mount must not hold every request.
READY_CACHE_S = 20.0


def router(state: AppState) -> APIRouter:
    api = APIRouter()
    controller = state.controller
    ready_cache: dict[str, tuple[float, list[dict]]] = {}

    def user_options() -> dict[str, dict[str, str]]:
        return {
            p.name: dict(p.user_options)
            for p in controller.plugins.plugins
            if p.user_options
        }

    def listing() -> dict:
        settings = controller.get_settings()
        return {
            "users": [
                {
                    "initials": initials,
                    "name": user.name,
                    "transfer_directory": user.transfer_directory,
                    "plugins": user.plugins,
                }
                for initials, user in sorted(state.config.users.items())
            ],
            "active": settings.user,
            "discovered": users_mod.discover_initials(state.config.users),
            "user_options": user_options(),
        }

    def choose(initials: str) -> dict:
        if not state.config_dir:
            raise HTTPException(400, "No config directory is set for this session")
        memory = users_mod.load_memory(state.config_dir)
        current = controller.get_settings()
        if current.user:  # their folder name comes back when they return
            memory.relative_by_user[current.user] = current.relative_directory
        try:
            changes = users_mod.settings_for_user(
                state.config, initials, memory.relative_by_user.get(initials)
            )
            updated = controller.update_settings(**changes)
        except RuntimeError as e:
            raise HTTPException(409, str(e)) from None
        except ValueError as e:
            raise HTTPException(422, str(e)) from None
        user = state.config.users.get(initials)
        controller.plugins.on_user(user.plugins if user else {})
        memory.last_user = initials
        users_mod.save_memory(state.config_dir, memory)
        ready_cache.clear()
        settings = dataclasses.asdict(updated)
        state.hub.publish("settings", settings)
        return settings

    @api.get("/api/users")
    def get_users():
        return listing()

    @api.post("/api/users/select")
    def select_user(payload: SelectUserRequest):
        settings = choose(payload.initials)
        return {**listing(), "settings": settings}

    @api.get("/api/users/suggest")
    def suggest(initials: str):
        """Probable folders for a new user, each with whether it exists."""
        initials = initials.strip().upper()
        guess = users_mod.suggest_paths(state.config.users, initials)
        return {
            "transfer_directory": guess["transfer_directory"],
            "transfer_status": users_mod.path_status(guess["transfer_directory"]),
            "plugins": {
                plugin: {
                    option: {"path": path, "status": users_mod.path_status(path)}
                    for option, path in options.items()
                }
                for plugin, options in guess["plugins"].items()
            },
        }

    @api.get("/api/paths/status")
    def path_status(path: str):
        return {"path": path, "status": users_mod.path_status(path)}

    @api.post("/api/users")
    def add_user(payload: NewUserRequest):
        """Add a user to the rig config (appended; nothing else in the file is
        rewritten), then choose them.
        """
        if not state.config_dir:
            raise HTTPException(400, "No config directory is set for this session")
        known = user_options()
        plugins = {
            plugin: {k: v for k, v in options.items() if k in known.get(plugin, {})}
            for plugin, options in payload.plugins.items()
        }
        try:
            state.config = users_mod.add_user(
                state.config_dir,
                payload.initials,
                name=payload.name.strip(),
                transfer_directory=payload.transfer_directory.strip(),
                plugins=plugins,
            )
        except ValueError as e:
            raise HTTPException(422, str(e)) from None
        # A later Save of the camera settings rewrites the file from this
        # copy: it must hold the new user too.
        from octacam.config_writer import load_raw_config

        state.raw_config = load_raw_config(state.config_dir)
        settings = choose(payload.initials)
        return {**listing(), "settings": settings}

    @api.get("/api/ready")
    def ready(refresh: bool = False):
        """Whether the rig is ready to record: `[{status, label, detail}]`,
        status "ok", "info", "warn" or "error". Never touches the hardware.
        """
        key = controller.get_settings().user
        hit = ready_cache.get(key)
        if hit and not refresh and time.monotonic() - hit[0] < READY_CACHE_S:
            return {"checks": hit[1]}
        checks = _ready_checks(state)
        ready_cache[key] = (time.monotonic(), checks)
        return {"checks": checks}

    return api


def choose_initial_user(state: AppState, user: str | None) -> None:
    """At startup: *user* (`--user`), else the last user on this machine when
    the rig still has them. An unknown *user* is a ValueError.
    """
    if not state.config_dir:
        return
    memory = users_mod.load_memory(state.config_dir)
    initials = user or (
        memory.last_user if memory.last_user in state.config.users else ""
    )
    if not initials:
        return
    changes = users_mod.settings_for_user(
        state.config, initials, memory.relative_by_user.get(initials)
    )
    state.controller.update_settings(**changes)
    state.controller.plugins.on_user(state.config.users[initials].plugins)
    memory.last_user = initials
    users_mod.save_memory(state.config_dir, memory)


def _check(status: str, label: str, detail: str = "") -> dict:
    return {"status": status, "label": label, "detail": detail}


def _ready_checks(state: AppState) -> list[dict]:
    from octacam import plugins as plugins_mod

    controller = state.controller
    settings = controller.get_settings()
    checks: list[dict] = []

    if controller.init_error:
        checks.append(_check("error", "Cameras", controller.init_error))
    elif not controller.ready:
        checks.append(_check("info", "Cameras", "opening\N{HORIZONTAL ELLIPSIS}"))
    else:
        n = len(controller.camera_system.cameras)
        wanted = len(state.config.cameras)
        checks.append(
            _check(
                "ok" if not wanted or n >= wanted else "warn",
                "Cameras",
                f"{n} open" + (f" of {wanted} configured" if wanted else ""),
            )
        )

    for plugin in controller.plugins.plugins:
        try:
            ok = plugin.is_ready()
        except Exception:
            ok = False
        checks.append(
            _check("ok" if ok else "error", plugin.name, "ready" if ok else "not ready")
        )

    record_dir = Path(settings.record_directory or settings.save_dir)
    probe = record_dir if record_dir.exists() else record_dir.parent
    try:
        free_gb = shutil.disk_usage(probe).free / 1e9
        checks.append(
            _check(
                "ok" if free_gb >= 50 else "warn",
                "Recording disk",
                f"{free_gb:.0f} GB free in {record_dir}",
            )
        )
    except OSError as e:
        checks.append(_check("error", "Recording disk", f"{record_dir}: {e}"))

    if settings.transfer_directory:
        status = users_mod.path_status(settings.transfer_directory)
        checks.append(
            _check(
                {"ok": "ok", "readonly": "error"}.get(status, "warn"),
                "Transfer destination",
                settings.transfer_directory
                + {"ok": "", "readonly": " (not writable)"}.get(
                    status, " (not reachable: is it mounted?)"
                ),
            )
        )
    else:
        checks.append(_check("info", "Transfer destination", "none set"))

    user = state.config.users.get(settings.user)
    config_dir = Path(state.config_dir) if state.config_dir else None
    for entry in state.config.plugins:
        name = plugins_mod.canonical_name(entry.name)
        options = {**entry.options, **(user.plugins.get(name, {}) if user else {})}
        try:
            found = plugins_mod.plugin_class(name).doctor(
                options, state.config, config_dir
            )
        except Exception as e:
            found = [("warn", f"{name}: its checks failed ({e})")]
        checks += [_check(status, name, text) for status, text in found]
    return checks
