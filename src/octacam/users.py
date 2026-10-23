"""The rig's users (`[users.<initials>]` in its config): who records here and
where their data goes.

A user replaces `[transfer].directory` with their own `transfer_directory`, and
gives each plugin its per-user options (`[users.MD.twophoton] source = ...`,
the ones a plugin lists in `Plugin.user_options`). Users live in the rig's own
config, so each rig keeps its own (two microscopes rarely share paths); adding
one appends a self-contained block to that file and touches nothing else in
it. Which user was last chosen on this machine, and each user's last
recording folder name, are remembered in octacam's cache.
"""

from __future__ import annotations

import json
import logging
import os
import tomllib
from collections.abc import Collection
from dataclasses import dataclass, field
from pathlib import Path

from octacam.config import (
    USER_INITIALS_RE,
    ConfigError,
    OctacamConfig,
    UserConfig,
    find_config_file,
    parse_config,
)
from octacam.files import atomic_write_text

log = logging.getLogger("octacam")


def _toml_string(value: str) -> str:
    return json.dumps(value)  # a JSON string is a valid TOML basic string


def user_block(
    initials: str,
    *,
    name: str = "",
    transfer_directory: str = "",
    plugins: dict[str, dict[str, str]] | None = None,
) -> str:
    """The TOML lines declaring one user."""
    lines = ["", f"[users.{initials}]"]
    if name:
        lines.append(f"name = {_toml_string(name)}")
    lines.append(f"transfer_directory = {_toml_string(transfer_directory)}")
    for plugin, options in sorted((plugins or {}).items()):
        values = {k: v for k, v in options.items() if v}
        if values:
            lines += ["", f"[users.{initials}.{plugin}]"]
            lines += [
                f"{k} = {_toml_string(str(v))}" for k, v in sorted(values.items())
            ]
    return "\n".join(lines) + "\n"


def add_user(
    config_dir: str | Path,
    initials: str,
    *,
    name: str = "",
    transfer_directory: str = "",
    plugins: dict[str, dict[str, str]] | None = None,
) -> OctacamConfig:
    """Append *initials* to the rig config in *config_dir* and return the
    config read back. ValueError for bad initials or an existing user.
    """
    if not USER_INITIALS_RE.match(initials):
        raise ValueError("initials are 2 to 4 capital letters, like MD")
    path = find_config_file(config_dir)
    text = path.read_text() if path.exists() else ""
    try:
        existing = tomllib.loads(text).get("users", {})
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"{path}: {e}") from e
    if initials in existing:
        raise ValueError(f"user {initials} already exists in {path}")
    block = user_block(
        initials, name=name, transfer_directory=transfer_directory, plugins=plugins
    )
    new_text = (text if text.endswith("\n") or not text else text + "\n") + block
    tomllib.loads(new_text)  # never write a config that no longer parses
    atomic_write_text(path, new_text)
    log.info("Added user %s to %s", initials, path)
    return parse_config(path)


# ----------------------------------------------------------------- suggestions


def _swap_initials(path: str, old: str, new: str) -> str | None:
    """*path* with its folder named *old* replaced by *new*, cut after it
    (`/nas/MD/BallPushing` -> `/nas/MA`); None when no folder is named *old*.
    """
    parts = Path(path).parts
    if old not in parts:
        return None
    i = parts.index(old)
    return str(Path(*parts[:i], new))


def suggest_paths(users: dict[str, UserConfig], initials: str) -> dict:
    """Where *initials*' data probably goes, from where the other users' goes:
    the same root with their own initials folder (`/nas/MD/x` -> `/nas/MA`).
    `{"transfer_directory": ..., "plugins": {plugin: {option: path}}}`, a
    path only when it can be guessed, with whether it exists.
    """
    transfer = ""
    plugins: dict[str, dict[str, str]] = {}
    for other, user in users.items():
        guess = _swap_initials(user.transfer_directory, other, initials)
        if guess and not transfer:
            transfer = guess
        for plugin, options in user.plugins.items():
            for option, value in options.items():
                if not isinstance(value, str):
                    continue
                guess = _swap_initials(value, other, initials)
                if guess:
                    plugins.setdefault(plugin, {}).setdefault(option, guess)
    return {"transfer_directory": transfer, "plugins": plugins}


def discover_initials(users: dict[str, UserConfig]) -> list[str]:
    """Initials-named folders beside the users' own (the lab's NAS is one
    folder per person): people who could be added, sorted, without the users.
    """
    roots: set[Path] = set()
    for initials, user in users.items():
        parts = Path(user.transfer_directory).parts
        if initials in parts:
            roots.add(Path(*parts[: parts.index(initials)]))
    found: set[str] = set()
    for root in roots:
        try:
            with os.scandir(root) as entries:
                found.update(
                    e.name
                    for e in entries
                    if USER_INITIALS_RE.match(e.name) and e.is_dir()
                )
        except OSError:
            continue
    return sorted(found - users.keys())


def path_status(path: str) -> str:
    """The GUI's check of *path*: ok (a writable folder), missing or readonly."""
    if not path:
        return "missing"
    from octacam.config import resolve_dir_template

    p = Path(resolve_dir_template(path))  # ~ and %-codes, as the transfer
    if not p.is_dir():
        return "missing"
    return "ok" if os.access(p, os.W_OK) else "readonly"


# ------------------------------------------------- per-machine memory


@dataclass
class UserMemory:
    """What this machine remembers for a rig: the last user, and each user's
    last recording folder name (so switching user brings back theirs).
    """

    last_user: str = ""
    relative_by_user: dict[str, str] = field(default_factory=dict)


def _memory_file() -> Path:
    from octacam.session_cache import cache_dir

    return cache_dir() / "users.json"


def _rig_key(config_dir: str | Path) -> str:
    return str(Path(config_dir).resolve())


def load_memory(config_dir: str | Path) -> UserMemory:
    try:
        data = json.loads(_memory_file().read_text()).get(_rig_key(config_dir), {})
        return UserMemory(
            last_user=str(data.get("last_user", "")),
            relative_by_user=dict(data.get("relative_by_user", {})),
        )
    except OSError, ValueError, AttributeError:
        return UserMemory()


def save_memory(config_dir: str | Path, memory: UserMemory) -> None:
    path = _memory_file()
    try:
        data = json.loads(path.read_text()) if path.exists() else {}
        if not isinstance(data, dict):
            data = {}
    except OSError, ValueError:
        data = {}
    data[_rig_key(config_dir)] = {
        "last_user": memory.last_user,
        "relative_by_user": memory.relative_by_user,
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(path, json.dumps(data, indent=2) + "\n")
    except OSError as e:
        log.debug("Could not remember the user: %s", e)


# ------------------------------------------------------------ choosing


def settings_for_user(
    config: OctacamConfig,
    initials: str,
    relative_directory: str | None = None,
    *,
    pinned: Collection[str] = (),
) -> dict:
    """The settings changes choosing *initials* makes: the user, their
    transfer directory (else the rig's), and their remembered folder name.
    *pinned*: the config keys the launch's `--set` gave; those win over the
    user's (`transfer.directory`: a test take to /tmp stays there).
    ValueError for an unknown user ("" clears the choice).
    """
    if initials and initials not in config.users:
        known = ", ".join(config.users) or "none configured"
        raise ValueError(f"unknown user {initials!r} (users: {known})")
    user = config.users.get(initials)
    rig_transfer = config.transfer.directory if config.transfer else ""
    changes: dict = {"user": initials}
    if "transfer.directory" not in pinned:
        changes["transfer_directory"] = (
            user.transfer_directory if user else ""
        ) or rig_transfer
    if relative_directory:
        changes["relative_directory"] = relative_directory
    return changes


def recording_user(
    config: OctacamConfig, config_dir: str | Path, user: str | None
) -> str:
    """Who records: *user*, else the last user of this rig on this machine
    when the rig still has them, else "" (no user).
    """
    if user:
        return user
    last = load_memory(config_dir).last_user
    return last if last in config.users else ""
