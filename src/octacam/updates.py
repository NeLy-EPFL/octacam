"""Check PyPI for a newer octacam release and advise the upgrade command for
how octacam was installed (pip / uv tool / pipx / conda).

Read-only: octacam never updates itself (it may be a project dependency or live
in a conda env, whose lockfile or bookkeeping a self-update would corrupt).
Nothing here raises, so an offline rig is unaffected; until octacam is on PyPI
the Simple API 404s and every surface stays silent.
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

# The PEP 691 JSON Simple API: every version, and a 404 while unpublished.
PYPI_SIMPLE_URL = "https://pypi.org/simple/octacam/"
_SIMPLE_ACCEPT = "application/vnd.pypi.simple.v1+json"
_DEFAULT_TIMEOUT = 4.0

# Either one set skips the check, network call included.
_OPT_OUT_ENV = ("OCTACAM_NO_UPDATE_CHECK", "DO_NOT_TRACK")


@dataclass
class UpdateNotice:
    """The result of an update check; always safe to render."""

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


def current_version() -> str:
    try:
        return version("octacam")
    except PackageNotFoundError:
        return "0.0.0+unknown"


def latest_stable(timeout: float = _DEFAULT_TIMEOUT) -> str | None:
    """The newest stable octacam version on PyPI, or None for no signal
    (unpublished, offline, unparseable). Never raises.
    """
    req = urllib.request.Request(
        PYPI_SIMPLE_URL,
        headers={"Accept": _SIMPLE_ACCEPT, "User-Agent": "octacam-update-check"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        e.close()  # it holds the response body open
        return None
    # HTTPException: a connection dropped mid-body (IncompleteRead).
    except urllib.error.URLError, OSError, ValueError, http.client.HTTPException:
        return None
    versions = payload.get("versions") if isinstance(payload, dict) else None
    if not isinstance(versions, list):
        return None
    # versions[] carries no yank status, so a yanked release can be advised.
    stable: list[Version] = []
    for raw in versions:
        try:
            v = Version(raw)
        except InvalidVersion, TypeError:
            continue
        if not v.is_prerelease:  # dev releases count as prereleases
            stable.append(v)
    return str(max(stable)) if stable else None


def _read_direct_url() -> dict | None:
    """The installed dist's PEP 610 direct_url.json if it is a JSON object, else
    None.
    """
    try:
        text = distribution("octacam").read_text("direct_url.json")
    except PackageNotFoundError:
        return None
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
    """How octacam was installed, best-effort and for advice only. The installs
    that must not get a plain upgrade (dev, vcs, conda) are checked first.
    """
    direct = _read_direct_url()
    if direct:
        dir_info = direct.get("dir_info")
        if isinstance(dir_info, dict) and dir_info.get("editable"):
            return "editable"
        if "vcs_info" in direct:
            return "vcs"
    if _in_conda():
        return "conda"
    prefix = Path(sys.prefix)
    if (prefix / "uv-receipt.toml").exists():
        return "uv-tool"
    # <pipx home>/venvs/<name>/ (PIPX_* is set only inside `pipx run`).
    if "pipx" in prefix.parts and "venvs" in prefix.parts:
        return "pipx"
    return "pip"


def advice_for(method: str) -> str:
    """The upgrade command to suggest for an install method; "" for a dev or VCS
    checkout, whose source tree the user owns.
    """
    return {
        "pip": "pip install --upgrade octacam",
        "uv-tool": "uv tool upgrade octacam",
        "pipx": "pipx upgrade octacam",
        "conda": "conda update octacam",
    }.get(method, "")


def check(timeout: float = _DEFAULT_TIMEOUT) -> UpdateNotice:
    """Check PyPI and return advice. Never raises."""
    current = current_version()
    method = detect_install_method()
    if any(os.environ.get(name) for name in _OPT_OUT_ENV):
        return UpdateNotice(current, None, False, method, "", "update check disabled")
    # A dev or VCS build is no PyPI release (and usually ahead of the latest).
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
