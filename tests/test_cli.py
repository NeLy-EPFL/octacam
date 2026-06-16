"""CLI smoke tests for the typer app (no real recording is started)."""

import logging
import os
import socket
import sys
from types import SimpleNamespace

os.environ.setdefault("PYLON_CAMEMU", "2")

from typer.testing import CliRunner

import octacam
from octacam.cli import (
    _LOCK_UNAVAILABLE,
    _acquire_instance_lock,
    _browser_skip_reason,
    _port_available,
    _resolve_enabled,
    app,
)

runner = CliRunner()


def test_version():
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert octacam.__version__ in result.output


def test_help_lists_commands():
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    for command in ("gui", "list-cameras", "list-plugins", "record", "transcode"):
        assert command in result.output


def test_no_args_prints_help():
    result = runner.invoke(app, [])
    assert result.exit_code == 0
    assert "Usage" in result.output


def test_dash_h_is_a_help_alias():
    # `-h` works on the root and on every subcommand (via context_settings).
    for args in (["-h"], ["gui", "-h"], ["list-plugins", "-h"]):
        result = runner.invoke(app, args)
        assert result.exit_code == 0, args
        assert "Usage" in result.output


def test_list_cameras_emits_tab_separated_lines():
    result = runner.invoke(app, ["list-cameras"])
    assert result.exit_code == 0
    # PYLON_CAMEMU=2 guarantees the emulated cameras show up.
    assert "0815-0000" in result.output
    emulated = [line for line in result.output.splitlines() if "0815-" in line]
    assert emulated and all("\t" in line for line in emulated)


def test_list_plugins_lists_bundled_arduino():
    result = runner.invoke(app, ["list-plugins"])
    assert result.exit_code == 0
    arduino = [
        line for line in result.output.splitlines() if line.startswith("arduino\t")
    ]
    assert len(arduino) == 1
    name, status, *_ = arduino[0].split("\t")
    assert name == "arduino"
    # `available` or `unavailable` depending on whether pyserial is installed.
    assert status in ("available", "unavailable")


def test_record_help_shows_enum_choices():
    result = runner.invoke(app, ["record", "--help"])
    assert result.exit_code == 0
    assert "[x264|raw]" in result.output
    assert "[software|hardware]" in result.output


def test_invalid_log_level_rejected():
    result = runner.invoke(app, ["--log-level", "bogus", "list-cameras"])
    assert result.exit_code != 0


def test_gui_rejects_missing_config_dir():
    result = runner.invoke(app, ["gui", "/no/such/dir"])
    assert result.exit_code != 0


def test_gui_help_shows_no_browser_flag():
    result = runner.invoke(app, ["gui", "--help"])
    assert result.exit_code == 0
    assert "--no-browser" in result.output


def test_port_available_detects_bound_socket():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("127.0.0.1", 0))
        sock.listen(1)
        port = sock.getsockname()[1]
        # A live listener makes the port unavailable...
        assert _port_available("127.0.0.1", port) is False
    # ...and it is free again once the listener closes.
    assert _port_available("127.0.0.1", port) is True


def test_gui_exits_when_port_already_in_use(tmp_path):
    # A taken port must fail fast (before opening cameras) with a clear hint to
    # pick another, rather than an opaque uvicorn bind traceback.
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("127.0.0.1", 0))
        sock.listen(1)
        port = sock.getsockname()[1]
        result = runner.invoke(
            app,
            [
                "gui",
                str(tmp_path),
                "--host",
                "127.0.0.1",
                "--port",
                str(port),
                "--no-browser",
            ],
        )
    assert result.exit_code != 0
    assert "already in use" in result.output
    assert "--port" in result.output  # tells the operator how to pick another


def test_gui_exits_when_another_instance_holds_the_config(tmp_path):
    # The single-instance guard is keyed on the config dir, not the port: while
    # one instance holds the lock, a second launch is refused on any port.
    held = _acquire_instance_lock(tmp_path.resolve())
    assert held is not None and held is not _LOCK_UNAVAILABLE
    try:
        # --port 0 leaves the port probe free, so only the lock can block us.
        result = runner.invoke(
            app, ["gui", str(tmp_path), "--port", "0", "--no-browser"]
        )
    finally:
        held.close()
    assert result.exit_code != 0
    assert "already running for this config" in result.output


def test_gui_reports_cameras_in_use(tmp_path, monkeypatch):
    # When the port is free but the cameras cannot be opened (e.g. another
    # octacam holds them, since SDKs open USB3 devices exclusively), the GUI
    # exits with a clean message rather than a raw SDK traceback.
    from octacam.cameras import BackendError

    config = SimpleNamespace(
        cameras=[SimpleNamespace(serial_number="0815-0000")], backend="fake"
    )
    monkeypatch.setattr("octacam.config.load_config_dir", lambda _dir: config)

    def _busy(*_args, **_kwargs):
        raise BackendError("The device is controlled by another application.")

    monkeypatch.setattr("octacam.cameras.CameraSystem", _busy)
    # --port 0 binds an ephemeral port for the availability probe, so the run
    # reaches the camera-open step regardless of what else is listening.
    result = runner.invoke(app, ["gui", str(tmp_path), "--port", "0", "--no-browser"])
    assert result.exit_code != 0
    assert "in use by another octacam" in result.output


def test_browser_skip_reason(monkeypatch):
    for var in ("SSH_CONNECTION", "SSH_CLIENT", "SSH_TTY"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("DISPLAY", ":0")
    # Local graphical session, no SSH -> open the browser.
    assert _browser_skip_reason(False) is None
    # --no-browser always wins.
    assert _browser_skip_reason(True) is not None
    # Ubuntu/GNOME on Wayland: DISPLAY may be unset but WAYLAND_DISPLAY is set,
    # which still counts as a local graphical session -> open the browser.
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-0")
    assert _browser_skip_reason(False) is None
    # An SSH session means the browser would open on the rig, not the laptop.
    monkeypatch.setenv("SSH_CONNECTION", "1.2.3.4 5 6.7.8.9 22")
    assert _browser_skip_reason(False) is not None
    # Headless (no display) is skipped on Linux even without SSH_* set.
    monkeypatch.delenv("SSH_CONNECTION", raising=False)
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    if sys.platform.startswith("linux"):
        assert _browser_skip_reason(False) is not None


def test_launch_browser_prefers_os_opener_on_linux(monkeypatch):
    from octacam import cli

    # On Linux we go straight to xdg-open rather than the stdlib browser hunt.
    monkeypatch.setattr(cli.sys, "platform", "linux")
    monkeypatch.delenv("BROWSER", raising=False)

    def _no_webbrowser(url):
        raise AssertionError("should prefer xdg-open over webbrowser")

    monkeypatch.setattr(cli.webbrowser, "open", _no_webbrowser)
    monkeypatch.setattr(cli.shutil, "which", lambda cmd: f"/usr/bin/{cmd}")
    calls = []
    monkeypatch.setattr(cli.subprocess, "Popen", lambda args, **kw: calls.append(args))
    assert cli._launch_browser("http://127.0.0.1:8000/") is True
    assert calls == [["xdg-open", "http://127.0.0.1:8000/"]]


def test_launch_browser_honors_browser_env(monkeypatch):
    from octacam import cli

    monkeypatch.setattr(cli.sys, "platform", "linux")
    monkeypatch.setenv("BROWSER", "firefox")
    opened = []
    monkeypatch.setattr(cli.webbrowser, "open", lambda url: opened.append(url) or True)

    def _no_fallback(*a, **k):
        raise AssertionError("must not shell out when $BROWSER opens")

    monkeypatch.setattr(cli.subprocess, "Popen", _no_fallback)
    assert cli._launch_browser("http://127.0.0.1:8000/") is True
    assert opened == ["http://127.0.0.1:8000/"]


def test_launch_browser_uses_webbrowser_without_os_opener(monkeypatch):
    from octacam import cli

    # Platforms without an OS opener (e.g. Windows) fall back to webbrowser.
    monkeypatch.setattr(cli.sys, "platform", "win32")
    monkeypatch.delenv("BROWSER", raising=False)
    monkeypatch.setattr(cli.shutil, "which", lambda cmd: None)
    monkeypatch.setattr(cli.webbrowser, "open", lambda url: True)
    assert cli._launch_browser("http://127.0.0.1:8000/") is True


def test_transcode_requires_paths():
    result = runner.invoke(app, ["transcode"])
    assert result.exit_code != 0


def test_record_help_lists_new_options():
    result = runner.invoke(app, ["record", "--help"])
    assert result.exit_code == 0
    assert "--record-form" in result.output
    assert "--save-frame-timestamps" in result.output


def test_transcode_help_lists_new_options():
    result = runner.invoke(app, ["transcode", "--help"])
    assert result.exit_code == 0
    for opt in (
        "--recursive",
        "--as-displayed",
        "--config-dir",
        "--format",
        "--remove-source",
    ):
        assert opt in result.output


def test_transcode_help_lists_cache_selectors():
    result = runner.invoke(app, ["transcode", "--help"])
    assert result.exit_code == 0
    for opt in ("--last", "--session", "--today", "--session-id"):
        assert opt in result.output


class _MsgHandler(logging.Handler):
    def __init__(self):
        super().__init__()
        self.messages = []

    def emit(self, record):
        self.messages.append(record.getMessage())


def _capture_octacam_logs(level=logging.INFO):
    handler = _MsgHandler()
    logger = logging.getLogger("octacam")
    logger.addHandler(handler)
    logger.setLevel(level)
    return logger, handler


def test_warn_if_transcoding_logs_only_when_active(tmp_path, monkeypatch):
    from octacam import cli, session_cache

    monkeypatch.setenv("OCTACAM_CACHE_DIR", str(tmp_path / "cache"))
    logger, handler = _capture_octacam_logs(logging.WARNING)
    try:
        cli._warn_if_transcoding()  # nothing running -> silent
        assert not handler.messages
        with session_cache.mark_transcode_active("3 file(s)"):
            cli._warn_if_transcoding()
    finally:
        logger.removeHandler(handler)
    assert any("transcode" in m and "CPU-heavy" in m for m in handler.messages)


def test_print_transcode_hints_uses_exact_session_id(tmp_path, monkeypatch):
    from octacam import cli, session_cache

    monkeypatch.setenv("OCTACAM_CACHE_DIR", str(tmp_path / "cache"))
    rec = tmp_path / "rec" / "001"
    rec.mkdir(parents=True)
    session_cache.record_recording(rec, "sessZ", "gui")

    logger, handler = _capture_octacam_logs(logging.INFO)
    try:
        cli._print_transcode_hints("sessZ", tmp_path / "cfg")
    finally:
        logger.removeHandler(handler)
    blob = "\n".join(handler.messages)
    # The exact session id (not the hijackable bare --session) is printed.
    assert "--session-id sessZ" in blob
    assert "--today" in blob and "--last" in blob

    # A session that recorded nothing prints no hint.
    logger, handler = _capture_octacam_logs(logging.INFO)
    try:
        cli._print_transcode_hints("sessNONE", tmp_path / "cfg")
    finally:
        logger.removeHandler(handler)
    assert not handler.messages


def test_resolve_enabled():
    # None / empty -> no override (use the config).
    assert _resolve_enabled(None, False) is None
    assert _resolve_enabled([], False) is None
    # Explicit plugin names are passed through.
    assert _resolve_enabled(["arduino"], False) == ["arduino"]
    # --no-plugins wins and disables everything.
    assert _resolve_enabled(["arduino"], True) == []
    assert _resolve_enabled(None, True) == []
