"""Unit tests for octacam.updates — the read-only PyPI update check.

No network: the HTTP call and the install-origin probes are monkeypatched, so
these exercise the parsing, version comparison, install-method classification,
and the fail-soft / opt-out behavior deterministically.
"""

from __future__ import annotations

import json
import urllib.error

from octacam import updates
from octacam.updates import UpdateNotice


class _FakeResp:
    def __init__(self, body: bytes):
        self._body = body

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _fake_urlopen(body: bytes):
    def _open(req, timeout=None):
        return _FakeResp(body)

    return _open


def _simple_payload(versions):
    return json.dumps({"name": "octacam", "versions": versions}).encode()


# --------------------------------------------------------------- latest_stable


def test_latest_stable_picks_max_stable(monkeypatch):
    body = _simple_payload(["0.1.0", "0.3.0", "0.2.5", "0.4.0rc1", "0.4.0.dev1"])
    monkeypatch.setattr(updates.urllib.request, "urlopen", _fake_urlopen(body))
    assert updates.latest_stable() == "0.3.0"


def test_latest_stable_none_when_only_prereleases(monkeypatch):
    body = _simple_payload(["0.4.0rc1", "0.4.0.dev1"])
    monkeypatch.setattr(updates.urllib.request, "urlopen", _fake_urlopen(body))
    assert updates.latest_stable() is None


def test_latest_stable_none_on_http_error(monkeypatch):
    def _raise(req, timeout=None):
        raise urllib.error.HTTPError(
            updates.PYPI_SIMPLE_URL, 404, "Not Found", None, None
        )

    monkeypatch.setattr(updates.urllib.request, "urlopen", _raise)
    assert updates.latest_stable() is None


def test_latest_stable_none_on_network_error(monkeypatch):
    def _raise(req, timeout=None):
        raise urllib.error.URLError("offline")

    monkeypatch.setattr(updates.urllib.request, "urlopen", _raise)
    assert updates.latest_stable() is None


def test_latest_stable_none_on_bad_json(monkeypatch):
    monkeypatch.setattr(updates.urllib.request, "urlopen", _fake_urlopen(b"not json"))
    assert updates.latest_stable() is None


# ----------------------------------------------------- detect_install_method


def test_detect_editable(monkeypatch):
    monkeypatch.setattr(
        updates, "_read_direct_url", lambda: {"dir_info": {"editable": True}}
    )
    assert updates.detect_install_method() == "editable"


def test_detect_vcs(monkeypatch):
    monkeypatch.setattr(
        updates, "_read_direct_url", lambda: {"vcs_info": {"vcs": "git"}}
    )
    assert updates.detect_install_method() == "vcs"


def test_detect_conda(monkeypatch):
    monkeypatch.setattr(updates, "_read_direct_url", lambda: None)
    monkeypatch.setattr(updates, "_in_conda", lambda: True)
    assert updates.detect_install_method() == "conda"


def test_detect_uv_tool(monkeypatch, tmp_path):
    monkeypatch.setattr(updates, "_read_direct_url", lambda: None)
    monkeypatch.setattr(updates, "_in_conda", lambda: False)
    (tmp_path / "uv-receipt.toml").write_text("")
    monkeypatch.setattr(updates.sys, "prefix", str(tmp_path))
    assert updates.detect_install_method() == "uv-tool"


def test_detect_pipx(monkeypatch, tmp_path):
    monkeypatch.setattr(updates, "_read_direct_url", lambda: None)
    monkeypatch.setattr(updates, "_in_conda", lambda: False)
    prefix = tmp_path / "pipx" / "venvs" / "octacam"
    prefix.mkdir(parents=True)
    monkeypatch.setattr(updates.sys, "prefix", str(prefix))
    assert updates.detect_install_method() == "pipx"


def test_detect_pip_default(monkeypatch, tmp_path):
    monkeypatch.setattr(updates, "_read_direct_url", lambda: None)
    monkeypatch.setattr(updates, "_in_conda", lambda: False)
    monkeypatch.setattr(updates.sys, "prefix", str(tmp_path))
    assert updates.detect_install_method() == "pip"


# ------------------------------------------------------------------- advice_for


def test_advice_for_each_manager():
    assert updates.advice_for("pip") == "pip install --upgrade octacam"
    assert updates.advice_for("uv-tool") == "uv tool upgrade octacam"
    assert updates.advice_for("pipx") == "pipx upgrade octacam"
    assert updates.advice_for("conda") == "conda update octacam"
    assert updates.advice_for("editable") == "git pull && uv sync"
    assert updates.advice_for("something-else") == ""


# ------------------------------------------------------------------------ check


def _clear_optout(monkeypatch):
    monkeypatch.delenv("OCTACAM_NO_UPDATE_CHECK", raising=False)
    monkeypatch.delenv("DO_NOT_TRACK", raising=False)


def test_check_opt_out_makes_no_network_call(monkeypatch):
    monkeypatch.setenv("OCTACAM_NO_UPDATE_CHECK", "1")

    def _boom(*a, **k):
        raise AssertionError("opt-out must not hit the network")

    monkeypatch.setattr(updates, "latest_stable", _boom)
    n = updates.check()
    assert n.latest is None and n.update_available is False
    assert n.note == "update check disabled"


def test_check_dev_install_short_circuits(monkeypatch):
    _clear_optout(monkeypatch)
    monkeypatch.setattr(updates, "detect_install_method", lambda: "editable")

    def _boom(*a, **k):
        raise AssertionError("a dev install must not hit the network")

    monkeypatch.setattr(updates, "latest_stable", _boom)
    n = updates.check()
    assert n.note == "development install" and n.update_available is False


def test_check_update_available(monkeypatch):
    _clear_optout(monkeypatch)
    monkeypatch.setattr(updates, "detect_install_method", lambda: "uv-tool")
    monkeypatch.setattr(updates, "current_version", lambda: "0.3.0")
    monkeypatch.setattr(updates, "latest_stable", lambda timeout=4.0: "0.9.0")
    n = updates.check()
    assert n.update_available is True
    assert n.latest == "0.9.0"
    assert n.command == "uv tool upgrade octacam"


def test_check_up_to_date(monkeypatch):
    _clear_optout(monkeypatch)
    monkeypatch.setattr(updates, "detect_install_method", lambda: "pip")
    monkeypatch.setattr(updates, "current_version", lambda: "0.3.0")
    monkeypatch.setattr(updates, "latest_stable", lambda timeout=4.0: "0.3.0")
    n = updates.check()
    assert n.update_available is False and n.command == ""


def test_check_dev_build_ahead_of_stable_does_not_nag(monkeypatch):
    _clear_optout(monkeypatch)
    monkeypatch.setattr(updates, "detect_install_method", lambda: "pip")
    monkeypatch.setattr(updates, "current_version", lambda: "0.4.0.dev1")
    monkeypatch.setattr(updates, "latest_stable", lambda timeout=4.0: "0.3.0")
    n = updates.check()
    assert n.update_available is False


def test_check_offline_or_unpublished(monkeypatch):
    _clear_optout(monkeypatch)
    monkeypatch.setattr(updates, "detect_install_method", lambda: "pip")
    monkeypatch.setattr(updates, "latest_stable", lambda timeout=4.0: None)
    n = updates.check()
    assert n.latest is None and n.note == "not on PyPI yet or offline"


def test_notice_as_dict_shape():
    d = UpdateNotice("0.3.0", "0.9.0", True, "pip", "cmd", "").as_dict()
    assert set(d) == {
        "current",
        "latest",
        "available",
        "install_method",
        "command",
        "note",
    }
    assert d["available"] is True
