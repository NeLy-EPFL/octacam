"""Check PyPI for a newer octacam release and advise the correct upgrade command.

Read-only and fail-silent by design: **octacam never updates itself.** It only
*tells* the user that a newer stable release exists and prints the upgrade command
appropriate to how octacam was installed (pip / uv tool / pipx / conda), leaving
the actual upgrade to the user and their environment's package manager.

This is deliberate, and matches what established Python CLIs do (pip, pipx, HTTPie
all notify rather than self-update): a *library* must never mutate its own install,
and octacam is sometimes a project dependency (`uv add octacam`) or lives inside a
conda env, where an out-of-band self-update would desync a lockfile or corrupt
conda's bookkeeping.

The check is version-based and only becomes meaningful once octacam is published to
PyPI; until then the Simple API returns 404 and every surface stays silent. Nothing
here raises — an offline or air-gapped rig must be unaffected.
"""

from __future__ import annotations

import http.client
import json
import os
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
from importlib.metadata import PackageNotFoundError, distribution, version
from pathlib import Path

from packaging.version import InvalidVersion, Version

# PEP 691 JSON Simple API. PyPI recommends this over the legacy /pypi/<name>/json;
# it returns the full version list, and a 404 (package not yet published) is the
# natural "stay silent" signal.
PYPI_SIMPLE_URL = "https://pypi.org/simple/octacam/"
_SIMPLE_ACCEPT = "application/vnd.pypi.simple.v1+json"
_DEFAULT_TIMEOUT = 4.0

# Environment variables that suppress the check entirely (no network call): the
# octacam-specific opt-out plus the cross-tool DO_NOT_TRACK convention.
_OPT_OUT_ENV = ("OCTACAM_NO_UPDATE_CHECK", "DO_NOT_TRACK")


@dataclass
class UpdateNotice:
    """The result of an update check — always safe to render, never a failure."""

    current: str
    latest: str | None  # newest stable release on PyPI, or None (no signal)
    update_available: bool
    install_method: str  # pip | uv-tool | pipx | conda | editable | vcs
    command: str  # exact upgrade command for this install, or "" (none to suggest)
    note: str  # short reason there is no advice, when applicable

    def as_dict(self) -> dict:
        return {
            "current": self.current,
            "latest": self.latest,
            "available": self.update_available,
            "install_method": self.install_method,
            "command": self.command,
            "note": self.note,
        }


def _opted_out() -> bool:
    return any(os.environ.get(name) for name in _OPT_OUT_ENV)


def current_version() -> str:
    try:
        return version("octacam")
    except PackageNotFoundError:
        return "0.0.0+unknown"


def latest_stable(timeout: float = _DEFAULT_TIMEOUT) -> str | None:
    """Newest non-prerelease octacam version on PyPI, or None.

    None means "no signal": the package is not on PyPI yet (404), the network is
    unreachable, or the response was unparseable. Never raises.
    """
    req = urllib.request.Request(
        PYPI_SIMPLE_URL,
        headers={"Accept": _SIMPLE_ACCEPT, "User-Agent": "octacam-update-check"},
    )
    try:
        # Fixed https PyPI URL (not user input); short timeout; errors caught below.
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError, http.client.HTTPException):
        # HTTPException covers a mid-body connection drop (IncompleteRead), which
        # is neither an OSError nor a URLError. "Never raises" must hold here.
        return None
    versions = payload.get("versions") if isinstance(payload, dict) else None
    if not isinstance(versions, list):
        return None
    # Known limitation: the Simple API's versions[] list carries no yank status
    # (that lives per-file in files[]), so a fully-yanked latest release would be
    # reported as newest and we'd advise an upgrade the package manager then
    # refuses. Accepted for now — read-only advice, and octacam yanks are rare.
    best: Version | None = None
    for raw in versions:
        try:
            v = Version(raw)
        except (InvalidVersion, TypeError):
            continue
        if v.is_prerelease or v.is_devrelease:
            continue
        if best is None or v > best:
            best = v
    return str(best) if best is not None else None


def _read_dist_text(name: str) -> str | None:
    try:
        return distribution("octacam").read_text(name)
    except PackageNotFoundError:
        return None


def _read_direct_url() -> dict | None:
    """PEP 610 direct_url.json for the installed dist, or None.

    Returns None for anything that isn't a JSON object (a malformed file could
    parse to a list/scalar), so callers can assume a dict without a shape check.
    """
    text = _read_dist_text("direct_url.json")
    if not text:
        return None
    try:
        data = json.loads(text)
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def _in_conda() -> bool:
    if not os.environ.get("CONDA_PREFIX"):
        return False
    try:
        return any((Path(sys.prefix) / "conda-meta").glob("octacam-*.json"))
    except OSError:
        return False


def detect_install_method() -> str:
    """Best-effort classification of how octacam was installed, for ADVICE only.

    Never used to mutate anything, so a wrong guess only yields slightly-off advice
    text — never a broken install. Order matters: the most specific
    "do-not-suggest-a-plain-upgrade" signals (dev/vcs/conda) come first.
    """
    direct = _read_direct_url()
    if direct:
        dir_info = direct.get("dir_info")
        # dir_info may be absent, null, or (in a malformed file) a non-object.
        if isinstance(dir_info, dict) and dir_info.get("editable"):
            return "editable"
        if "vcs_info" in direct:
            return "vcs"
    if _in_conda():
        return "conda"
    prefix = Path(sys.prefix)
    if (prefix / "uv-receipt.toml").exists():
        return "uv-tool"
    # pipx installs each tool under <pipx home>/venvs/<name>/; the path is the
    # reliable tell (PIPX_* env vars are only set inside `pipx run`, not for an
    # installed tool's own entry point).
    if "pipx" in prefix.parts and "venvs" in prefix.parts:
        return "pipx"
    return "pip"


def advice_for(method: str) -> str:
    """The upgrade command to suggest for an install method, or "".

    Empty when octacam should not print a command: a dev/VCS checkout (the user
    owns the source tree). conda/pip/uv-tool/pipx each get their manager's command.
    """
    return {
        "pip": "pip install --upgrade octacam",
        "uv-tool": "uv tool upgrade octacam",
        "pipx": "pipx upgrade octacam",
        "conda": "conda update octacam",
        "editable": "git pull && uv sync",
    }.get(method, "")


def check(timeout: float = _DEFAULT_TIMEOUT) -> UpdateNotice:
    """Check PyPI and return advice. Never raises; safe on air-gapped rigs."""
    current = current_version()
    method = detect_install_method()
    if _opted_out():
        return UpdateNotice(current, None, False, method, "", "update check disabled")
    # A dev/editable/VCS build isn't a PyPI release, so a version compare is
    # meaningless (a dev build is usually AHEAD of the latest stable). Don't nag.
    if method in ("editable", "vcs"):
        return UpdateNotice(current, None, False, method, "", "development install")
    latest = latest_stable(timeout=timeout)
    if latest is None:
        return UpdateNotice(
            current, None, False, method, "", "not on PyPI yet or offline"
        )
    try:
        available = Version(latest) > Version(current)
    except InvalidVersion:
        available = False
    command = advice_for(method) if available else ""
    return UpdateNotice(current, latest, available, method, command, "")
