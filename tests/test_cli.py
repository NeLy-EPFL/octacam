"""CLI smoke tests for the typer app (no real recording is started)."""

import io
import json
import logging
import os
import socket
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from helpers import wait_until
from typer.testing import CliRunner

import octacam
from octacam.cameras import BackendError, BackendUnavailable
from octacam.cli import (
    _LOCK_UNAVAILABLE,
    _acquire_instance_lock,
    _browser_skip_reason,
    _build_config_doc,
    _port_available,
    _resolve_backend,
    _resolve_enabled,
    app,
)
from octacam.firmware import FirmwareSpec
from octacam.plugins.base import Plugin, PluginManager

runner = CliRunner()


def test_version():
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert octacam.__version__ in result.output


def test_help_lists_commands():
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    for command in ("gui", "doctor", "config", "record", "process"):
        assert command in result.output
    # list-cameras/list-plugins were merged into `doctor`.
    assert "list-cameras" not in result.output
    assert "list-plugins" not in result.output
    # The three old post-recording commands are gone (subsumed by `process`).
    assert "transcode " not in result.output
    assert "\n  grid" not in result.output
    assert "\n  nas" not in result.output


def test_no_args_prints_help():
    result = runner.invoke(app, [])
    assert result.exit_code == 0
    assert "Usage" in result.output


def test_dash_h_is_a_help_alias():
    # `-h` works on the root and on every subcommand (via context_settings).
    for args in (["-h"], ["gui", "-h"], ["doctor", "-h"]):
        result = runner.invoke(app, args)
        assert result.exit_code == 0, args
        assert "Usage" in result.output


def test_record_help_has_day_to_day_overrides():
    result = runner.invoke(app, ["record", "--help"])
    assert result.exit_code == 0
    # Only the day-to-day overrides remain (fps/duration/output).
    for opt in ("--fps", "--duration", "--output"):
        assert opt in result.output
    # The identity fields that used to feed the save-directory template were
    # removed as redundant, as were the old encoding/form enum options.
    for opt in ("--experimenter", "--experiment", "--subject", "--trial"):
        assert opt not in result.output
    assert "[x264|raw]" not in result.output
    assert "--record-form" not in result.output


def test_invalid_log_level_rejected():
    result = runner.invoke(app, ["--log-level", "bogus", "doctor"])
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


@pytest.mark.parametrize(
    ("error_type", "expected"),
    [
        (BackendError, "Could not open the cameras: {e}. They may already be in use"),
        (BackendUnavailable, "{e}"),
        (ValueError, "Camera initialization failed: {e}"),
    ],
)
def test_gui_reports_cameras_in_use(tmp_path, monkeypatch, error_type, expected):
    # The GUI serves the page before opening the cameras, so a camera-open
    # failure (e.g. another octacam holds them — SDKs open USB3 devices
    # exclusively) does not exit the process. It is surfaced in the GUI: the
    # background init calls controller.fail_init with a clean message (not a raw
    # SDK traceback), the server stays up, and the browser shows the reason.
    import octacam.cameras as cameras_mod
    from octacam.config import CameraConfig, OctacamConfig, RecordConfig

    message = "The device is controlled by another application."
    expected = expected.format(e=error_type(message))

    real_cs = cameras_mod.CameraSystem

    class BusyCameraSystem:
        @classmethod
        def pending(cls, backend="auto"):
            # The sync path still builds a real hardware-free placeholder to
            # serve against; only opening the real cameras fails.
            return real_cs.pending(backend)

        def __init__(self, *_a, **_k):
            raise error_type(message)

    monkeypatch.setattr("octacam.cameras.CameraSystem", BusyCameraSystem)

    config = OctacamConfig(
        cameras=[CameraConfig(serial_number="0815-0000", name="cam0")],
        backend="fake",
        record=RecordConfig(directory=str(tmp_path / "rec")),
    )
    monkeypatch.setattr("octacam.config.load_config_dir", lambda _dir: config)
    monkeypatch.setattr("octacam.plugins.build_plugins", lambda *a, **k: _FakePlugins())

    captured = {}

    def fake_run(app_obj, **_kwargs):
        # Stand in for uvicorn.run: capture the controller, wait for the
        # background init to finish (fail), then "shut down" by returning.
        ctrl = app_obj.state.app_state.controller
        captured["controller"] = ctrl
        wait_until(lambda: ctrl.ready or ctrl.init_error, timeout=5.0, interval=0.01)

    monkeypatch.setattr("uvicorn.run", fake_run)

    # --port 0 binds an ephemeral port for the availability probe.
    result = runner.invoke(app, ["gui", str(tmp_path), "--port", "0", "--no-browser"])
    assert result.exit_code == 0, result.output  # served + shut down cleanly
    ctrl = captured["controller"]
    assert ctrl.ready is False
    assert (ctrl.init_error or "").startswith(expected)


def _fake_camera_system(cam):
    class FakeSystem:
        # Mirrors the real CameraSystem's incomplete-rig introspection: `record`
        # reports opened-vs-configured and gates on a shortfall, so a fake missing
        # these looks like a rig that failed that check.
        requested_serial_numbers: list[str] = []
        incomplete = False
        missing: dict[str, str] = {}

        def __init__(self, *_a, **_k):
            self._cams = [cam]

        @classmethod
        def pending(cls, *_a, **_k):
            # The GUI builds a hardware-free placeholder to serve against before
            # opening the real cameras; the fake returns itself so len()/iter work.
            return cls()

        def __len__(self):
            return len(self._cams)

        def __iter__(self):
            return iter(self._cams)

        def load_config(self, *_a, **_k):
            pass

        def apply_display_config(self, *_a, **_k):
            pass

        def close(self):
            _FACADE_CALLS.append("system.close")

    return FakeSystem


_FACADE_CALLS: list[str] = []


class _FakePlugins(PluginManager):
    """No plugins; journals the setup and teardown the CLI calls."""

    def setup_all(self):
        _FACADE_CALLS.append("setup_all")

    def teardown_all(self):
        _FACADE_CALLS.append("teardown_all")


def test_gui_tears_down_when_create_app_raises(tmp_path, monkeypatch):
    # create_app() runs inside the try (before any hardware is armed); if it
    # raises, the finally must still run controller.close() and
    # plugins.teardown_all() so nothing is left half-initialized.
    import octacam.cli as cli_mod
    from octacam.config import CameraConfig, OctacamConfig, RecordConfig

    _FACADE_CALLS.clear()
    cam = SimpleNamespace(serial_number="s1", name="cam1")
    config = OctacamConfig(
        cameras=[CameraConfig(serial_number="s1", name="cam1")],
        backend="fake",
        record=RecordConfig(directory=str(tmp_path / "rec")),
    )
    monkeypatch.setattr("octacam.config.load_config_dir", lambda _dir: config)
    monkeypatch.setattr("octacam.cameras.CameraSystem", _fake_camera_system(cam))
    monkeypatch.setattr("octacam.plugins.build_plugins", lambda *a, **k: _FakePlugins())

    class FakeController:
        def __init__(self, *a, **k):
            pass

        def close(self):
            _FACADE_CALLS.append("controller.close")

    monkeypatch.setattr("octacam.controller.RecordingController", FakeController)

    def _boom(*_a, **_k):
        raise RuntimeError("create_app failed")

    monkeypatch.setattr("octacam.web.app.create_app", _boom)
    monkeypatch.setattr(cli_mod, "_print_transcode_hints", lambda *a, **k: None)

    result = runner.invoke(app, ["gui", str(tmp_path), "--port", "0", "--no-browser"])
    assert result.exit_code != 0  # the RuntimeError propagates after cleanup
    # Teardown ran despite the failure — the background init thread never started
    # (create_app raised first), so nothing was armed to leak.
    assert "controller.close" in _FACADE_CALLS
    assert "teardown_all" in _FACADE_CALLS


def test_record_finally_closes_via_controller_not_system(tmp_path, monkeypatch):
    # A Ctrl-C/exception during join() must trigger controller.close() (which
    # aborts+joins the daemon monitor so metadata/timestamps are written, then
    # closes cameras once) — never a bare system.close() that races the monitor.
    import octacam.cli as cli_mod
    from octacam.config import CameraConfig, OctacamConfig, RecordConfig

    _FACADE_CALLS.clear()
    cam = SimpleNamespace(serial_number="s1", name="cam1")
    config = OctacamConfig(
        cameras=[CameraConfig(serial_number="s1", name="cam1")],
        backend="fake",
        record=RecordConfig(
            directory=str(tmp_path / "does-not-exist"), fps=10.0, duration=1.0
        ),
    )
    monkeypatch.setattr("octacam.config.load_config_dir", lambda _dir: config)
    monkeypatch.setattr("octacam.cameras.CameraSystem", _fake_camera_system(cam))

    monkeypatch.setattr(cli_mod, "_preflight_firmware", lambda *a, **k: None)

    monkeypatch.setattr("octacam.plugins.build_plugins", lambda *a, **k: _FakePlugins())

    class FakeController:
        def __init__(self, *a, **k):
            pass

        def start_recording(self, *a, **k):
            return SimpleNamespace(ok=True, message="")

        def join(self):
            raise RuntimeError("interrupted")  # stand in for a Ctrl-C stop

        def close(self):
            _FACADE_CALLS.append("controller.close")

    monkeypatch.setattr("octacam.controller.RecordingController", FakeController)

    result = runner.invoke(app, ["record", str(tmp_path)])
    assert result.exit_code != 0  # the RuntimeError from join() propagates
    assert "controller.close" in _FACADE_CALLS
    assert "system.close" not in _FACADE_CALLS  # no bare system teardown race


def _patch_one_camera_record(monkeypatch, tmp_path, events):
    """Stub `record`'s hardware and controller; each step appends to *events*,
    with whether the capture marker was live then."""
    import octacam.cli as cli_mod
    from octacam import session_cache
    from octacam.config import CameraConfig, OctacamConfig, RecordConfig

    cam = SimpleNamespace(serial_number="s1", name="cam1", frames_recorded=1)
    config = OctacamConfig(
        cameras=[CameraConfig(serial_number="s1", name="cam1")],
        backend="fake",
        record=RecordConfig(directory=str(tmp_path / "take"), save_method="raw"),
    )
    monkeypatch.setattr("octacam.config.load_config_dir", lambda _dir: config)
    monkeypatch.setattr("octacam.cameras.CameraSystem", _fake_camera_system(cam))
    monkeypatch.setattr(cli_mod, "_preflight_firmware", lambda *a, **k: None)

    def note(step):
        events.append((step, session_cache.capture_active()))

    class Plugins(PluginManager):
        def teardown_all(self):
            note("teardown_all")

    monkeypatch.setattr("octacam.plugins.build_plugins", lambda *a, **k: Plugins())

    class FakeController:
        recording_active = False

        def __init__(self, *a, **k):
            pass

        def start_recording(self, *a, **k):
            note("start")
            return SimpleNamespace(ok=True, message="")

        def join(self):
            pass

        def close(self):
            note("close")

    monkeypatch.setattr("octacam.controller.RecordingController", FakeController)


def test_record_holds_the_capture_marker_until_the_cameras_are_closed(
    tmp_path, monkeypatch
):
    # `octacam process` pauses while the marker is live: it must cover the take
    # and controller.close(), which finalizes it and releases the cameras.
    from octacam import session_cache

    events = []
    _patch_one_camera_record(monkeypatch, tmp_path, events)
    result = runner.invoke(app, ["record", str(tmp_path)])
    assert result.exit_code == 0, result.output
    assert events == [("start", True), ("close", True), ("teardown_all", True)]
    assert not session_cache.capture_active()


def test_record_tears_down_when_the_capture_marker_fails(tmp_path, monkeypatch):
    # A failure entering the marker must still close the cameras and plugins.
    import contextlib

    @contextlib.contextmanager
    def broken_marker(_detail=""):
        raise RuntimeError("no home directory")
        yield

    events = []
    _patch_one_camera_record(monkeypatch, tmp_path, events)
    monkeypatch.setattr("octacam.session_cache.mark_capture_active", broken_marker)
    result = runner.invoke(app, ["record", str(tmp_path)])
    assert isinstance(result.exception, RuntimeError)
    assert events == [("close", False), ("teardown_all", False)]


def test_record_applies_its_fps_duration_and_output_overrides(tmp_path, monkeypatch):
    import octacam.controller

    _patch_one_camera_record(monkeypatch, tmp_path, [])
    seen = []

    class Capture(octacam.controller.RecordingController):
        def __init__(self, _system, settings, *_a, **_k):
            seen.append(settings)

    monkeypatch.setattr("octacam.controller.RecordingController", Capture)
    output = tmp_path / "elsewhere"
    result = runner.invoke(
        app,
        ["record", str(tmp_path), "--fps", "50", "--duration", "2"]
        + ["--output", str(output)],
    )
    assert result.exit_code == 0, result.output
    (settings,) = seen
    assert (settings.fps, settings.duration_s) == (50.0, 2.0)
    # An explicit --output clears the config's split save path.
    assert settings.save_dir == str(output)
    assert (settings.record_directory, settings.relative_directory) == ("", "")


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


def test_doctor_runtime_reports_why_the_browser_stays_closed(monkeypatch):
    from octacam.cli import _doctor_runtime, _Report

    def browser_lines():
        report = _Report()
        _doctor_runtime(report, None)
        ((_title, items),) = report.sections
        return [(status, text) for status, text in items if "browser" in text]

    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setenv("SSH_CONNECTION", "1.2.3.4 5 6.7.8.9 22")
    assert browser_lines() == [
        (
            "info",
            "SSH session — the GUI won't auto-open a browser; use an ssh -L tunnel",
        )
    ]

    for var in ("SSH_CONNECTION", "SSH_CLIENT", "SSH_TTY"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    assert browser_lines() == [
        ("info", "no local display — the GUI won't auto-open a browser")
    ]

    monkeypatch.setenv("DISPLAY", ":0")
    assert browser_lines() == []


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


def test_record_help_drops_encoding_options():
    result = runner.invoke(app, ["record", "--help"])
    assert result.exit_code == 0
    for kept in ("--fps", "--duration", "--output"):
        assert kept in result.output
    for gone in ("--crf", "--preset", "--codec", "--save-frame-timestamps"):
        assert gone not in result.output


def test_process_help_lists_options():
    result = runner.invoke(app, ["process", "--help"])
    assert result.exit_code == 0
    for opt in (
        "--recursive",
        "--no-transcode",
        "--no-grid",
        "--no-transfer",
        "--delete-source",
        "--force",
    ):
        assert opt in result.output
    # Encoding is config-driven now: the old per-run encoding flags are gone.
    for gone in ("--as-displayed", "--format", "--crf", "--pix-fmt"):
        assert gone not in result.output


def test_process_help_lists_cache_selectors():
    result = runner.invoke(app, ["process", "--help"])
    assert result.exit_code == 0
    for opt in ("--last", "--session-id", "--all"):
        assert opt in result.output
    # --last carries an optional recording|session value.
    assert "recording|session" in result.output


def test_warn_if_transcoding_logs_only_when_active(caplog):
    from octacam import cli, session_cache

    caplog.set_level(logging.WARNING, logger="octacam")
    cli._warn_if_transcoding()  # nothing running -> silent
    assert not caplog.messages
    with session_cache.mark_transcode_active("3 file(s)"):
        cli._warn_if_transcoding()
    blob = "\n".join(caplog.messages)
    assert "transcod" in blob
    # The warning must describe what _pause_gate actually does. It used to say a
    # foreground `octacam process` does *not* auto-pause, which was the opposite
    # of the code (and of docs/guide/processing.md): the gate applies to both, and
    # only the manual-pause half is detached-only.
    assert "detached and foreground alike" in blob
    assert "--ignore-capture" in blob  # the documented way out


def test_print_transcode_hints_lists_session_and_all(tmp_path, caplog):
    from octacam import cli, session_cache

    rec = tmp_path / "rec" / "001"
    rec.mkdir(parents=True)
    session_cache.record_recording(rec, "sessZ", "gui")

    caplog.set_level(logging.INFO, logger="octacam")
    cli._print_transcode_hints("sessZ")
    blob = "\n".join(caplog.messages)
    # Two ready-to-run selectors: the last session and every cached session.
    assert "--last session" in blob and "--all" in blob

    # A session that recorded nothing prints no hint.
    caplog.clear()
    cli._print_transcode_hints("sessNONE")
    assert not caplog.messages


def test_resolve_enabled():
    # None / empty -> no override (use the config).
    assert _resolve_enabled(None, False) is None
    assert _resolve_enabled([], False) is None
    # Explicit plugin names are passed through.
    assert _resolve_enabled(["flywheel"], False) == ["flywheel"]
    # --no-plugins wins and disables everything.
    assert _resolve_enabled(["flywheel"], True) == []
    assert _resolve_enabled(None, True) == []


@pytest.fixture
def emulated_rig(monkeypatch):
    """What doctor sees: pylon's two emulated cameras and nothing on the USB bus.

    The FLIR tiers report not installed, pycameleon finds no camera, pylon
    enumerates its emulator alone, and no USB link or serial port is read, so a
    report never depends on what is plugged into the machine running the tests."""
    from octacam.cameras import basler, pycameleon, registry

    select_backend = registry.select_backend

    def select_installed(name):
        if (name or "").strip().lower() in ("flir", "spinnaker"):
            raise registry.BackendUnavailable(name, "not installed")
        return select_backend(name)

    factory = basler.tl_factory()

    class Emulator:
        def EnumerateDevices(self):
            tl = factory.CreateTl("BaslerCamEmu")
            try:
                return tl.EnumerateDevices()
            finally:
                factory.ReleaseTl(tl)

    monkeypatch.setattr(registry, "select_backend", select_installed)
    monkeypatch.setattr("octacam.cameras.select_backend", select_installed)
    monkeypatch.setattr(basler, "tl_factory", Emulator)
    monkeypatch.setattr(
        pycameleon, "pycameleon", SimpleNamespace(enumerate_cameras=lambda: [])
    )
    monkeypatch.setattr("octacam.cli._usb_camera_links", lambda detected: [])
    monkeypatch.setattr("octacam.serial_ports.list_serial_ports", lambda: [])


def test_doctor_lists_cameras_plugins_and_toolchain(emulated_rig):
    # `doctor` lists cameras + plugins and adds diagnostics.
    result = runner.invoke(app, ["doctor"])
    assert result.exit_code == 0, result.output
    for heading in ("Camera backends", "Encoding toolchain", "Plugins"):
        assert heading in result.output
    # PYLON_CAMEMU=2 guarantees the emulated cameras (and thus the basler
    # backend) show up, and the bundled flywheel plugin is always listed.
    assert "0815-0000" in result.output
    assert "flywheel" in result.output


def test_doctor_json_is_machine_readable(emulated_rig):
    import json

    result = runner.invoke(app, ["--log-level", "error", "doctor", "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["octacam_version"] == octacam.__version__
    titles = [s["title"] for s in payload["sections"]]
    assert "Camera backends" in titles and "Encoding toolchain" in titles
    assert payload["errors"] == 0


def test_doctor_help_documents_config_dir():
    result = runner.invoke(app, ["doctor", "-h"])
    assert result.exit_code == 0
    assert "Usage" in result.output
    assert "CONFIG_DIR" in result.output


def test_doctor_flags_undetected_camera_and_exits_nonzero(emulated_rig, tmp_path):
    # A rig config declaring a serial that isn't among the emulated cameras is a
    # hard error: doctor lists it and exits nonzero so scripts can pre-flight.
    (tmp_path / "octacam_config.toml").write_text(
        '[[cameras]]\nserial_number = "99999999"\nname = "ghost"\n'
    )
    result = runner.invoke(app, ["--log-level", "error", "doctor", str(tmp_path)])
    assert result.exit_code == 1, result.output
    assert "declared but NOT detected" in result.output
    assert "99999999" in result.output


def _count_enumerations(monkeypatch):
    """Return a Counter that ticks once per octacam.cli._enumerate_backend call.

    doctor now enumerates the backends via a single parallel _CameraScan; this
    seam lets a test assert the scan never re-enumerates a backend (the whole
    point of the dedup — the old code enumerated the cascade ~3× per run)."""
    import collections

    from octacam import cli

    counter: collections.Counter[str] = collections.Counter()
    original = cli._enumerate_backend

    def counting(name):
        counter[name] += 1
        return original(name)

    monkeypatch.setattr("octacam.cli._enumerate_backend", counting)
    return counter


def test_doctor_enumerates_each_backend_at_most_once(emulated_rig, monkeypatch):
    # The dedup invariant: one parallel scan, so no backend is enumerated twice —
    # even though the report has three consumers (the tier list, the cascade line,
    # and the cameras-vs-config cross-check). basler is always present (emulated).
    counter = _count_enumerations(monkeypatch)
    result = runner.invoke(app, ["--log-level", "error", "doctor"])
    assert result.exit_code == 0, result.output
    assert counter["basler"] == 1
    assert max(counter.values()) <= 1, dict(counter)


def test_doctor_backend_filter_scans_only_that_backend(emulated_rig, monkeypatch):
    # `--backend X` must scan only X (no full-cascade sweep), so the other tiers
    # are never touched.
    counter = _count_enumerations(monkeypatch)
    result = runner.invoke(app, ["--log-level", "error", "doctor", "--backend", "basler"])
    assert result.exit_code == 0, result.output
    assert set(counter) == {"basler"}, dict(counter)


def test_doctor_backend_filter_is_case_insensitive(emulated_rig):
    # --backend is normalized like select_backend/_enumerate_backend, so an
    # upper/mixed-case tier name still resolves to its cached scan (regression:
    # the scan cache is keyed by the lowercased name).
    result = runner.invoke(app, ["--log-level", "error", "doctor", "--backend", "BASLER"])
    assert result.exit_code == 0, result.output
    assert "BASLER: available" in result.output
    assert "enumeration failed" not in result.output


def test_doctor_json_has_no_progress_noise(emulated_rig):
    # The scan's live spinner renders on stderr and is suppressed for --json / when
    # output is not a terminal, so machine-readable output is never corrupted.
    result = runner.invoke(app, ["--log-level", "error", "doctor", "--json"])
    assert result.exit_code == 0, result.output
    assert "enumerating" not in result.output
    json.loads(result.output)  # still valid JSON


def test_doctor_report_order_is_deterministic(emulated_rig, monkeypatch):
    # Parallel enumeration must not leak completion order into the report: the
    # Camera-backends section is assembled in a fixed backend order both times,
    # though basler's scan finishes last in the first run and first in the second.
    from octacam import cli

    def backends_section(output: str) -> str:
        lines = output.splitlines()
        start = next(i for i, ln in enumerate(lines) if ln.strip() == "Camera backends")
        end = next(
            (i for i in range(start + 1, len(lines)) if lines[i].strip() == "Encoding toolchain"),
            len(lines),
        )
        return "\n".join(lines[start:end])

    enumerate_backend = cli._enumerate_backend
    slow = []

    def enumerate_slowly(name):
        if name in slow:
            time.sleep(0.2)
        return enumerate_backend(name)

    monkeypatch.setattr("octacam.cli._enumerate_backend", enumerate_slowly)
    slow[:] = ["basler"]
    first = runner.invoke(app, ["--log-level", "error", "doctor"])
    slow[:] = ["pycameleon"]
    second = runner.invoke(app, ["--log-level", "error", "doctor"])
    assert first.exit_code == 0 and second.exit_code == 0
    assert backends_section(first.output) == backends_section(second.output)


def test_camera_lines_groups_by_model_and_handles_unknown():
    # Same-model cameras collapse to one "model: s1, s2" line (first-seen order);
    # an unknown model falls back to a bare serial per line.
    from octacam.cli import _camera_lines

    assert _camera_lines(
        [("s1", "M1"), ("s2", "M1"), ("s3", "M2"), ("s4", None), ("s5", None)]
    ) == ["M1: s1, s2", "M2: s3", "s4", "s5"]
    assert _camera_lines([]) == []


def test_usb_camera_links_reads_speeds_and_filters_non_cameras(tmp_path):
    # The sysfs link-speed reader: camera-vendor devices (Basler 2676, FLIR 1e10)
    # and any detected serial are reported with their negotiated speed; non-camera
    # devices and entries without a serial node are ignored.
    from octacam.cli import _usb_camera_links

    def mkdev(name, **fields):
        d = tmp_path / name
        d.mkdir()
        for k, v in fields.items():
            (d / k).write_text(v)

    mkdev("basler-bad", serial="40018619", idVendor="2676",
          product="acA1920-150um", speed="480")
    mkdev("basler-ok", serial="40018631", idVendor="2676",
          product="acA1920-150um", speed="5000")
    mkdev("flir-bad", serial="010AA673", idVendor="1e10",
          product="Grasshopper3", speed="480")
    mkdev("generic-detected", serial="GEN1", idVendor="ffff",
          product="Cam", speed="480")  # unknown vendor, but octacam detected it
    mkdev("keyboard", serial="KB1", idVendor="046d", speed="12")  # non-camera vendor
    mkdev("hub", idVendor="1d6b", speed="480")  # no serial node -> skipped

    got = {s: (p, spd) for s, p, spd in _usb_camera_links({"GEN1"}, root=tmp_path)}
    assert got["40018619"] == ("acA1920-150um", 480)
    assert got["40018631"] == ("acA1920-150um", 5000)
    assert got["010AA673"][1] == 480  # FLIR matched by vendor id
    assert got["GEN1"][1] == 480  # unknown vendor but detected serial
    assert "KB1" not in got  # non-camera vendor, not detected
    slow = {s for s, (_p, spd) in got.items() if spd < 5000}
    assert slow == {"40018619", "010AA673", "GEN1"}


def test_doctor_warns_on_usb2_linked_camera(monkeypatch):
    # doctor never opens a camera, so a USB3 camera that fell back to USB 2.0 must
    # be surfaced from its sysfs link speed — the gap the user hit (the GUI warned,
    # doctor was silent). The warning names the camera, the speed, and the fix.
    from octacam import cli
    from octacam.cli import _doctor_backends, _Report

    monkeypatch.setattr(
        cli, "_usb_camera_links",
        lambda _detected: [("40018619", "acA1920-150um", 480)],
    )

    class _FakeScan:
        def get(self, _name):
            return []

        def cascade(self):
            return []

    report = _Report()
    _doctor_backends(report, only_backend="fake", scan=_FakeScan())
    warns = [t for _title, items in report.sections for s, t in items if s == "warn"]
    assert any(
        "40018619" in w and "480 Mb/s" in w and "USB 2.0" in w and "cable" in w
        for w in warns
    ), warns


def test_doctor_backends_reads_auto_as_the_cascade(monkeypatch):
    # `doctor --backend auto` reports every tier and the cascade's pick, as the
    # scan and the config cross-check do, not an unknown backend named 'auto'.
    from octacam import cli
    from octacam.cli import _doctor_backends, _Report

    monkeypatch.setattr(cli, "_usb_camera_links", lambda _detected: [])

    class _FakeScan:
        def get(self, _name):
            return [("S1", "M")]

        def cascade(self):
            return [("S1", "basler", "M")]

    for selector in ("auto", "ALL"):
        report = _Report()
        _doctor_backends(report, only_backend=selector, scan=_FakeScan())
        texts = [t for _title, items in report.sections for _s, t in items]
        assert not any("'auto'" in t or "'ALL'" in t for t in texts), texts
        assert "S1 → basler" in " ".join(texts), texts


def test_enumerate_backend_resolves_model_via_backend_read_model(monkeypatch):
    # End-to-end of the asymmetry fix: the REAL _enumerate_backend generic path
    # must resolve the backend's read_model through its registry spec and
    # map each enumerated handle to its model. Driven through pycameleon (always
    # available) with fake handles, so the whole glue runs — not a monkeypatched
    # stand-in. A regressed read_model lookup / handle→model mapping fails here.
    import types

    import octacam.cameras.pycameleon as pcmod
    from octacam.cli import _enumerate_backend

    class _Cam:
        def __init__(self, serial, model):
            self._serial, self._model = serial, model

        def info(self):
            return {"serial_number": self._serial, "model_name": self._model}

    cams = [_Cam("17475187", "GS3-U3"), _Cam("17475185", "GS3-U3"), _Cam("B1", "")]
    monkeypatch.setattr(
        pcmod, "pycameleon", types.SimpleNamespace(enumerate_cameras=lambda: cams)
    )
    # enumerate sorts by serial; the blank model falls back to None (unknown).
    assert _enumerate_backend("pycameleon") == [
        ("17475185", "GS3-U3"),
        ("17475187", "GS3-U3"),
        ("B1", None),
    ]


def test_doctor_groups_cameras_by_model_including_non_basler(emulated_rig, monkeypatch):
    # The grouping half of the change: same-model cameras (including a non-basler
    # tier's, now that every backend surfaces a model) render as a single grouped
    # line. This stubs _enumerate_backend, so it covers _camera_lines + doctor
    # rendering only — the read_model wiring is covered by the test above.
    monkeypatch.setattr(
        "octacam.cli._enumerate_backend",
        lambda name: [
            ("17475185", "GS3-U3-41C6NIR"),
            ("17475187", "GS3-U3-41C6NIR"),
            ("40018619", "acA1920-150um"),
        ],
    )
    result = runner.invoke(
        app, ["--log-level", "error", "doctor", "--backend", "pycameleon"]
    )
    assert result.exit_code == 0, result.output
    assert "GS3-U3-41C6NIR: 17475185, 17475187" in result.output
    assert "acA1920-150um: 40018619" in result.output


def test_doctor_omits_free_gui_port_line(emulated_rig):
    # The "GUI port is free" happy-path line was pruned — the port is reported only
    # when in use. Port 8765 is normally free under test, so the old code would
    # have printed this line; its absence proves the removal (not a vacuous check).
    result = runner.invoke(app, ["--log-level", "error", "doctor"])
    assert result.exit_code == 0, result.output
    assert "GUI port 8765 is free" not in result.output


def test_doctor_gpu_encoding_drops_save_method_hint(monkeypatch):
    # The nvenc "enable per rig with save_method" hint was pruned. That line was the
    # last statement of _doctor_gpu_encoding, reachable only WITH a GPU present, so a
    # plain doctor run on a GPU-less box never emits it (a vacuous check). Force the
    # GPU-present path so the section runs to its end: the NVENC-params line (the new
    # last line) proves we got there, and the hint must be gone.
    import octacam.writer as writer
    from octacam.cli import _doctor_gpu_encoding, _Report

    monkeypatch.setattr("octacam.cli._nvidia_gpus", lambda: ["FakeGPU (driver 999)"])
    monkeypatch.setattr("octacam.cli._ffmpeg_version", lambda exe: "n7.1")
    monkeypatch.setattr(writer, "find_ffmpeg", lambda require_encoder=None: "/usr/bin/ffmpeg")
    monkeypatch.setattr(writer, "probe_nvenc_max_sessions", lambda: None)

    report = _Report()
    report.section("Encoding toolchain")
    _doctor_gpu_encoding(report)
    texts = [text for _status, text in report.sections[-1][1]]
    assert any("NVIDIA GPU: FakeGPU" in t for t in texts)  # took the GPU-present path
    assert any("NVENC record params" in t for t in texts)  # reached the section's end
    assert not any("enable per rig" in t for t in texts)  # …yet the hint is gone


# --- doctor: serial / Arduino devices ---------------------------------------


def _fake_serial_port(device, *, vid=0x2341, pid=0x0070, sn="SN123", arduino=True,
                      mcu=True, board="Arduino Nano ESP32"):
    from octacam.serial_ports import SerialPort

    return SerialPort(
        device=device, description="", manufacturer="Arduino", product=None,
        vid=vid, pid=pid, serial_number=sn, hwid="", board_name=board,
        likely_microcontroller=mcu, likely_arduino=arduino,
    )


def test_doctor_lists_serial_devices(emulated_rig, monkeypatch):
    ports = [
        _fake_serial_port("/dev/ttyACM0"),
        _fake_serial_port("/dev/ttyS0", vid=None, pid=None, sn=None,
                          arduino=False, mcu=False, board="generic serial"),
    ]
    monkeypatch.setattr("octacam.serial_ports.list_serial_ports", lambda: ports)
    result = runner.invoke(app, ["doctor"])
    assert result.exit_code == 0, result.output
    assert "Serial devices" in result.output
    assert "Arduino Nano ESP32" in result.output
    assert "2341:0070" in result.output
    # The single generic port is collapsed into an "other" summary line.
    assert "other/generic serial port" in result.output


def test_doctor_serial_flags_missing_configured_device(emulated_rig, monkeypatch, tmp_path):
    monkeypatch.setattr(
        "octacam.serial_ports.list_serial_ports",
        lambda: [_fake_serial_port("/dev/ttyACM0")],
    )
    (tmp_path / "octacam_config.toml").write_text(
        '[[plugins]]\nname = "triggerbox"\n[plugins.options]\ndevice = "/dev/ttyACM9"\n'
    )
    result = runner.invoke(app, ["--log-level", "error", "doctor", str(tmp_path)])
    assert result.exit_code == 1, result.output
    # Collapse whitespace: Rich wraps the console at 80 cols, so the phrase can
    # span a line break depending on the (variable-length) plugin name.
    flat = " ".join(result.output.split())
    assert "not found among connected serial ports" in flat
    # A detected board not used by any plugin is reported as info, not an error.
    assert "detected but not used by any plugin" in flat


def test_doctor_serial_section_in_json(emulated_rig, monkeypatch):
    monkeypatch.setattr(
        "octacam.serial_ports.list_serial_ports",
        lambda: [_fake_serial_port("/dev/ttyACM0")],
    )
    result = runner.invoke(app, ["--log-level", "error", "doctor", "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    titles = [s["title"] for s in payload["sections"]]
    assert "Serial devices" in titles


def test_doctor_probe_serial_reports_firmware(emulated_rig, monkeypatch):
    from octacam.serial_ports import SerialIdentity

    monkeypatch.setattr(
        "octacam.serial_ports.list_serial_ports",
        lambda: [_fake_serial_port("/dev/ttyACM0")],
    )
    monkeypatch.setattr(
        "octacam.serial_ports.probe_identity",
        lambda device, **kw: SerialIdentity(device, "TRIGGERBOX 1", False, None),
    )
    result = runner.invoke(app, ["doctor", "--probe-serial"])
    assert result.exit_code == 0, result.output
    assert "TRIGGERBOX 1" in result.output


def test_doctor_probe_serial_skips_busy_port(emulated_rig, monkeypatch):
    from octacam.serial_ports import SerialIdentity

    monkeypatch.setattr(
        "octacam.serial_ports.list_serial_ports",
        lambda: [_fake_serial_port("/dev/ttyACM0")],
    )
    monkeypatch.setattr(
        "octacam.serial_ports.probe_identity",
        lambda device, **kw: SerialIdentity(device, None, True, "busy"),
    )
    result = runner.invoke(app, ["doctor", "--probe-serial"])
    assert result.exit_code == 0, result.output
    assert "port in use" in result.output


def test_build_config_doc_includes_plugins():
    # The config wizard threads a serial plugin selection into the written TOML.
    from octacam.config import RecordConfig

    doc = _build_config_doc(
        "fake", RecordConfig(), [], [], None,
        [{"name": "triggerbox", "options": {"device": "/dev/ttyACM0"}}],
    )
    assert doc["plugins"] == [
        {"name": "triggerbox", "options": {"device": "/dev/ttyACM0"}}
    ]


@pytest.mark.parametrize(
    ("name", "device"),
    [("triggerbox", "/dev/ttyACM0"), ("twophoton", "/dev/arduinoCams"), ("flywheel", "/dev/ttyACM0")],
)
def test_config_wizard_offers_the_plugins_default_device_without_a_port(
    monkeypatch, name, device
):
    from rich.console import Console
    from rich.prompt import Confirm, Prompt

    import octacam.cli as cli_mod

    defaults = {}

    def ask(prompt, *, default=None, **_kw):
        defaults[prompt.strip()] = default
        return name if prompt.strip() == "Plugin" else default

    monkeypatch.setattr(Confirm, "ask", lambda *a, **k: True)
    monkeypatch.setattr(Prompt, "ask", ask)
    monkeypatch.setattr(cli_mod, "_detect_serial_ports", lambda console: [])
    entries = cli_mod._prompt_serial_plugin(Console(file=io.StringIO()))
    assert defaults["Device"] == device
    assert entries == [{"name": name, "options": {"device": device}}]


# --- process: idempotent re-runs (skip existing outputs) --------------------


# Grids are opt-in, so a recording whose grid path should run needs this in its
# embedded config snapshot; `visualization=False` gives the bare default rig.
_VIZ_TOML = '''[[visualization]]
name = "grid.mp4"
layout = [["camera_LF", ""]]
'''


def _make_recording(folder, *, with_outputs, extra_toml="", visualization=True):
    """A recording folder with one camera's source .mkv and its summary.

    When *with_outputs*, also drop a finished ``camera_LF.mp4`` and ``grid.mp4``
    so ``octacam process``'s skip-on-exists path is exercised. *extra_toml*, if
    given, is written as the embedded ``octacam_config.toml`` snapshot; unless
    *visualization* is off, a ``[[visualization]]`` entry is appended to it so the
    grid step has something to build.
    """
    from octacam.transform import RECORDING_SUMMARY_FILENAME

    folder.mkdir(parents=True, exist_ok=True)
    (folder / "camera_LF.mkv").write_bytes(b"source-bytes")
    (folder / RECORDING_SUMMARY_FILENAME).write_text(
        json.dumps(
            {
                "fps_target": 100,
                "relative_directory": folder.name,
                "cameras": [
                    {
                        "name": "camera_LF",
                        "file": "camera_LF.mkv",
                        "width": 64,
                        "height": 48,
                        "fps": 100,
                        "frames": 10,
                    }
                ],
            }
        )
    )
    toml = extra_toml + (_VIZ_TOML if visualization else "")
    if toml:
        (folder / "octacam_config.toml").write_text(toml)
    if with_outputs:
        (folder / "camera_LF.mp4").write_bytes(b"finished-transcode")
        (folder / "grid.mp4").write_bytes(b"finished-grid")


def test_process_skips_existing_transcode_and_grid(tmp_path, monkeypatch):
    folder = tmp_path / "rec"
    _make_recording(folder, with_outputs=True)

    calls = {"transcode": 0, "grid": 0}

    def fake_transcode(input_path, output, **kwargs):
        calls["transcode"] += 1
        return output

    def fake_grid(folder, layout=None, output=None, **kwargs):
        calls["grid"] += 1
        return output

    monkeypatch.setattr("octacam.writer.transcode_file", fake_transcode)
    monkeypatch.setattr("octacam.grid.build_grid_video", fake_grid)

    before_mp4 = (folder / "camera_LF.mp4").read_bytes()
    before_grid = (folder / "grid.mp4").read_bytes()

    result = runner.invoke(app, ["process", str(folder), "--no-transfer"])
    assert result.exit_code == 0, result.output
    # Neither the transcoder nor the grid builder ran — both outputs pre-existed.
    assert calls == {"transcode": 0, "grid": 0}
    # And the existing outputs are left byte-for-byte untouched.
    assert (folder / "camera_LF.mp4").read_bytes() == before_mp4
    assert (folder / "grid.mp4").read_bytes() == before_grid


def _age(path, seconds):
    """Backdate a file by *seconds* (the outputs of an earlier take)."""
    stamp = path.stat().st_mtime - seconds
    os.utime(path, (stamp, stamp))


def test_process_redoes_outputs_left_over_from_an_earlier_take(
    tmp_path, monkeypatch, process_log
):
    # Recording into a folder again (confirming the overwrite) replaces only the
    # files the new take writes, so the previous take's mp4/grid stay behind.
    # They must not pass as this recording's finished outputs — that is how a
    # video from a different take ended up transferred as if it were this one's.
    folder = tmp_path / "rec"
    _make_recording(folder, with_outputs=True)
    _age(folder / "camera_LF.mp4", 10)  # older than the new take's source...
    _age(folder / "grid.mp4", 20)  # ...and the grid older still

    calls = {"transcode": 0, "grid": 0}

    def fake_transcode(input_path, output, **kwargs):
        calls["transcode"] += 1
        return output

    def fake_grid(folder, layout=None, output=None, **kwargs):
        calls["grid"] += 1
        return output

    monkeypatch.setattr("octacam.writer.transcode_file", fake_transcode)
    monkeypatch.setattr("octacam.grid.build_grid_video", fake_grid)

    result = runner.invoke(app, ["process", str(folder), "--no-transfer"])

    assert result.exit_code == 0, result.output
    assert calls == {"transcode": 1, "grid": 1}
    assert any("left over from an earlier recording" in m for m in process_log.messages)
    assert any("older than the videos it composites" in m for m in process_log.messages)


def test_process_dry_run_lists_leftover_outputs_as_work(
    tmp_path, monkeypatch, process_log
):
    folder = tmp_path / "rec"
    _make_recording(folder, with_outputs=True)
    _age(folder / "camera_LF.mp4", 10)
    _age(folder / "grid.mp4", 20)
    _forbid(monkeypatch, "octacam.writer.transcode_file", "octacam.grid.build_grid_video")

    result = runner.invoke(app, ["process", str(folder), "--no-transfer", "--dry-run"])

    assert result.exit_code == 0, result.output
    # The leftovers are work to redo, not work already done — and the grid is
    # listed as waiting for the video that will be re-transcoded.
    assert "[dry-run] Transcode: 1 to transcode, 0 already done" in process_log.messages
    assert "[dry-run] Grid: 1 to build, 0 already exist" in process_log.messages
    assert any("waits for: camera_LF.mp4" in m for m in process_log.messages)


def test_process_force_rebuilds_existing_outputs(tmp_path, monkeypatch):
    folder = tmp_path / "rec"
    _make_recording(folder, with_outputs=True)

    calls = {"transcode": 0, "grid": 0}

    def fake_transcode(input_path, output, **kwargs):
        calls["transcode"] += 1
        Path(output).write_bytes(b"reencoded")
        return output

    def fake_grid(folder, layout=None, output=None, **kwargs):
        calls["grid"] += 1
        return output

    monkeypatch.setattr("octacam.writer.transcode_file", fake_transcode)
    monkeypatch.setattr("octacam.grid.build_grid_video", fake_grid)

    result = runner.invoke(app, ["process", str(folder), "--no-transfer", "--force"])
    assert result.exit_code == 0, result.output
    # --force re-runs both steps even though the outputs already existed.
    assert calls == {"transcode": 1, "grid": 1}


def test_process_builds_no_grid_without_visualization_config(tmp_path, monkeypatch):
    # Grids are opt-in: a rig whose config has no [[visualization]] entry gets
    # no composite at all (octacam used to derive one from the camera names and
    # spend minutes of ffmpeg on a video the rig never asked for). The transcode
    # step still runs.
    folder = tmp_path / "rec"
    _make_recording(folder, with_outputs=False, visualization=False)

    calls = {"transcode": 0, "grid": 0}

    def fake_transcode(input_path, output, **kwargs):
        calls["transcode"] += 1
        Path(output).write_bytes(b"encoded")
        return output

    def fake_grid(folder, layout=None, output=None, **kwargs):
        calls["grid"] += 1
        return output

    monkeypatch.setattr("octacam.writer.transcode_file", fake_transcode)
    monkeypatch.setattr("octacam.grid.build_grid_video", fake_grid)

    result = runner.invoke(app, ["process", str(folder), "--no-transfer"])
    assert result.exit_code == 0, result.output
    assert calls == {"transcode": 1, "grid": 0}
    assert not (folder / "grid.mp4").exists()

    # ...and --force doesn't conjure one either: there is nothing configured to
    # rebuild.
    result = runner.invoke(app, ["process", str(folder), "--no-transfer", "--force"])
    assert result.exit_code == 0, result.output
    assert calls["grid"] == 0


def test_process_transfers_skipped_outputs(tmp_path, monkeypatch):
    # A skipped transcode/grid must still flow to the transfer step, so a
    # re-run finishes the pipeline for a partially-transferred recording.
    dest_root = tmp_path / "dest"
    folder = tmp_path / "rec"
    _make_recording(
        folder,
        with_outputs=True,
        extra_toml=(
            f'[transfer]\ndirectory = "{dest_root.as_posix()}"\nchecksum = false\n'
        ),
    )

    def fake_transcode(input_path, output, **kwargs):
        raise AssertionError("transcode should be skipped, not run")

    def fake_grid(folder, layout=None, output=None, **kwargs):
        raise AssertionError("grid should be skipped, not run")

    monkeypatch.setattr("octacam.writer.transcode_file", fake_transcode)
    monkeypatch.setattr("octacam.grid.build_grid_video", fake_grid)

    result = runner.invoke(app, ["process", str(folder)])
    assert result.exit_code == 0, result.output
    dest = dest_root / folder.name
    assert (dest / "camera_LF.mp4").read_bytes() == b"finished-transcode"
    assert (dest / "grid.mp4").read_bytes() == b"finished-grid"


# --- process --dry-run: a plan, never a partial run --------------------------


@pytest.fixture
def process_log(monkeypatch, caplog):
    """caplog at INFO for a CLI invocation.

    The CLI callback would install a rich handler that wraps lines to the
    terminal width and stops propagation, so it is left out."""
    monkeypatch.setattr("octacam.cli._setup_logging", lambda level: None)
    caplog.set_level(logging.INFO, logger="octacam")
    return caplog


def _forbid(monkeypatch, *targets):
    """Make each dotted target raise if a dry run reaches it."""

    def boom(*args, **kwargs):
        raise AssertionError("a dry run must not do real work")

    for target in targets:
        monkeypatch.setattr(target, boom)


def _transfer_toml(dest_root):
    return f'[transfer]\ndirectory = "{dest_root.as_posix()}"\n'


def test_process_dry_run_plans_every_step_without_running_any(
    tmp_path, monkeypatch, process_log
):
    # Nothing is encoded, composited, copied or deleted, yet every step lists
    # what a real run would do, including the grid and the transfer of outputs
    # the transcode step has only planned.
    dest_root = tmp_path / "dest"
    folder = tmp_path / "rec"
    _make_recording(folder, with_outputs=False, extra_toml=_transfer_toml(dest_root))
    _forbid(
        monkeypatch,
        "octacam.writer.transcode_file",
        # Its input isn't transcoded yet, so there is nothing to probe.
        "octacam.grid.build_grid_video",
        "octacam.cli._delete_source_files",
    )
    before = sorted(p.name for p in folder.iterdir())

    result = runner.invoke(app, ["process", str(folder), "--dry-run", "-d"])

    assert result.exit_code == 0, result.output
    assert sorted(p.name for p in folder.iterdir()) == before
    assert not dest_root.exists()
    source = folder / "camera_LF.mkv"
    dest = dest_root / folder.name
    assert f"[dry-run] transcode: {source} → camera_LF.mp4" in process_log.messages
    assert f"[dry-run] would delete source: {source}" in process_log.messages
    assert any(
        m.startswith(f"[dry-run] grid: {folder / 'grid.mp4'}")
        for m in process_log.messages
    )
    for name in ("camera_LF.mp4", "grid.mp4"):
        assert (
            f"[dry-run] transfer: {folder / name} → {dest / name}"
            in process_log.messages
        )
    assert "[dry-run] Transcode: 1 to transcode, 0 already done" in process_log.messages
    assert "[dry-run] Grid: 1 to build, 0 already exist" in process_log.messages


def test_process_dry_run_previews_a_grid_whose_inputs_exist(
    tmp_path, monkeypatch, process_log
):
    # With its inputs on disk the grid's exact ffmpeg call can be previewed, so
    # the dry run hands the grid to the builder, in dry-run mode.
    folder = tmp_path / "rec"
    _make_recording(folder, with_outputs=True)
    (folder / "grid.mp4").unlink()
    _forbid(monkeypatch, "octacam.writer.transcode_file")
    dry_runs = []

    def fake_grid(folder, layout=None, output=None, **kwargs):
        dry_runs.append(kwargs.get("dry_run"))
        return output

    monkeypatch.setattr("octacam.grid.build_grid_video", fake_grid)

    result = runner.invoke(app, ["process", str(folder), "--dry-run", "--no-transfer"])

    assert result.exit_code == 0, result.output
    assert dry_runs == [True]
    assert "[dry-run] Transcode: 0 to transcode, 1 already done" in process_log.messages
    assert "[dry-run] Grid: 1 to build, 0 already exist" in process_log.messages


def test_process_dry_run_never_waits_on_a_live_capture(
    tmp_path, monkeypatch, process_log
):
    # The plan is often wanted mid-session. A dry run does no heavy work, so it
    # must not park behind a live capture the way a real run does, nor tell a
    # gui launch that a transcode is competing for the CPU.
    folder = tmp_path / "rec"
    _make_recording(
        folder, with_outputs=False, extra_toml=_transfer_toml(tmp_path / "dest")
    )
    _forbid(
        monkeypatch,
        "octacam.cli._pause_gate",
        "octacam.session_cache.mark_transcode_active",
        "octacam.writer.transcode_file",
        "octacam.grid.build_grid_video",
    )

    result = runner.invoke(app, ["process", str(folder), "--dry-run"])

    assert result.exit_code == 0, result.output
    assert "[dry-run] Transcode: 1 to transcode, 0 already done" in process_log.messages


def test_process_dry_run_lists_no_work_for_a_finished_recording(
    tmp_path, monkeypatch, process_log
):
    # `process --all --dry-run` doubles as "what is left to process?", so a
    # recording that is fully processed lists no work, only the counts.
    dest_root = tmp_path / "dest"
    folder = tmp_path / "rec"
    _make_recording(folder, with_outputs=True, extra_toml=_transfer_toml(dest_root))
    _forbid(monkeypatch, "octacam.writer.transcode_file", "octacam.grid.build_grid_video")
    finish = runner.invoke(app, ["process", str(folder)])
    assert finish.exit_code == 0, finish.output
    process_log.clear()

    result = runner.invoke(app, ["process", str(folder), "--dry-run"])

    assert result.exit_code == 0, result.output
    steps = ("transcode:", "would delete", "grid:", "transfer:")
    assert not [
        m
        for m in process_log.messages
        if m.startswith(tuple(f"[dry-run] {s}" for s in steps))
    ]
    assert "[dry-run] Transcode: 0 to transcode, 1 already done" in process_log.messages
    assert "[dry-run] Grid: 0 to build, 1 already exist" in process_log.messages
    # The mp4, the grid, the summary and the config snapshot.
    assert "[dry-run] Transfer: 0 to copy, 4 already up to date" in process_log.messages


# --- process: the progress bar (transcode, grid and transfer) ----------------


def test_progress_bar_labels_a_grid_encode(tmp_path):
    from octacam.cli import _FileProgressBar
    from octacam.writer import TranscodeProgress

    bar = _FileProgressBar(2)
    on_progress = bar.file(2, tmp_path / "run1", "grid: ")
    on_progress(TranscodeProgress(5, 10.0, 0.5, 1.0, total_frames=None, done=True))
    (task,) = bar._progress.tasks
    assert task.description == "[2/2] grid: run1"
    assert task.total == 5 and task.finished


def test_progress_bar_shows_one_task_per_file_copy_and_verify():
    from octacam.cli import _FileProgressBar
    from octacam.transfer import TransferProgress

    bar = _FileProgressBar()
    on_progress = bar.transfer_callback()
    seen = []
    for index, phase, done in [(1, "copy", 50), (1, "copy", 100), (1, "verify", 100), (2, "copy", 10)]:
        on_progress(TransferProgress(index, 2, f"cam{index}.mp4", done, 100, 1.0, phase))
        (task,) = bar._progress.tasks  # only the current file's task is kept
        seen.append((task.description, task.completed, task.total))
    assert seen == [
        ("[1/2] copy: cam1.mp4", 50, 100),
        ("[1/2] copy: cam1.mp4", 100, 100),
        ("[1/2] verify: cam1.mp4", 100, 100),
        ("[2/2] copy: cam2.mp4", 10, 100),
    ]


# --- config: the interactive first-run wizard -------------------------------


def test_config_help_documents_scaffolding():
    result = runner.invoke(app, ["config", "-h"])
    assert result.exit_code == 0
    assert "Usage" in result.output
    assert "CONFIG_DIR" in result.output
    assert "--backend" in result.output


def _quiet_console():
    import io

    from rich.console import Console

    return Console(file=io.StringIO())


def test_resolve_backend_defaults_to_auto_without_prompting(monkeypatch):
    # No --backend must not ask which vendor to use: the rig auto-detects every
    # installed backend. (A prompt would block here since no input is provided.)
    monkeypatch.setattr(
        "octacam.cameras.registry.available_backends", lambda: ["basler", "flir"]
    )
    assert _resolve_backend(_quiet_console(), None) == "auto"


def test_resolve_backend_honors_explicit_and_rejects_unknown():
    import typer

    assert _resolve_backend(_quiet_console(), "flir") == "flir"
    assert _resolve_backend(_quiet_console(), "fake") == "fake"
    with pytest.raises(typer.BadParameter):
        _resolve_backend(_quiet_console(), "nikon")


def test_build_config_doc_omits_auto_backend_but_writes_explicit():
    from octacam.config import RecordConfig

    record = RecordConfig()
    auto_doc = _build_config_doc("auto", record, [], [], None)
    assert "backend" not in auto_doc  # the default is left implicit
    flir_doc = _build_config_doc("flir", record, [], [], None)
    assert flir_doc["backend"] == "flir"


def test_config_wizard_auto_detects_across_backends_without_backend_prompt(
    tmp_path, monkeypatch
):
    # The user's scenario: run `octacam config` with no --backend, and cameras
    # from different vendors are detected together. No backend question is asked,
    # and no backend key is pinned into the file (it stays auto-detecting).
    from octacam.config import load_config_dir

    monkeypatch.setattr(
        "octacam.cameras.registry.available_backends", lambda: ["basler", "flir"]
    )
    monkeypatch.setattr(
        "octacam.cli._enumerate_backend",
        lambda name: [("BAS-1", "acA1300"), ("FLIR-1", None)],
    )
    target = tmp_path / "mixed-rig"
    inputs = "\n".join(["n", "", "", "", "", "", "", "", "", "n", "n"]) + "\n"
    result = runner.invoke(
        app, ["config", str(target), "--no-snapshot-params"], input=inputs
    )
    assert result.exit_code == 0, result.output
    text = (target / "octacam_config.toml").read_text()
    assert "backend" not in text
    cfg = load_config_dir(target)
    assert cfg.backend == "auto"
    assert [c.serial_number for c in cfg.cameras] == ["BAS-1", "FLIR-1"]


def test_config_wizard_writes_roundtrippable_config(tmp_path):
    # Full run over the `fake` backend (FAKE-0/FAKE-1): name both cameras, add a
    # grid, take the record defaults, and configure a transfer destination. The
    # written file must parse back to exactly what was entered.
    from octacam.config import load_config_dir

    target = tmp_path / "rig1"
    inputs = (
        "\n".join(
            [
                "y",  # name these cameras now?
                "cam_a",  # FAKE-0 name
                "cam_b",  # FAKE-1 name
                "y",  # add a visualization grid?
                "",  # fps -> default
                "",  # duration -> default
                "",  # duration unit -> default
                "",  # trigger source -> default
                "",  # preview trigger source -> default
                "/data/rig1",  # save directory
                "%y%m%d/001",  # relative directory template
                "",  # save method -> default
                "y",  # configure a transfer destination?
                "/mnt/nas",  # transfer directory
                "",  # checksum -> default (yes)
                "n",  # enable a serial/trigger plugin? no
            ]
        )
        + "\n"
    )
    result = runner.invoke(
        app, ["config", str(target), "--backend", "fake"], input=inputs
    )
    assert result.exit_code == 0, result.output
    assert (target / "octacam_config.toml").exists()
    # The choices are the config's own vocabulary (config.SaveMethod, ...).
    for choices in (
        "[frames/seconds/minutes/hours]",
        "[software/managed/external]",
        "[auto/software/free_running]",
        "[ffmpeg/nvenc/raw]",
    ):
        assert choices in result.output

    cfg = load_config_dir(target)
    assert cfg.backend == "fake"
    assert [(c.serial_number, c.name) for c in cfg.cameras] == [
        ("FAKE-0", "cam_a"),
        ("FAKE-1", "cam_b"),
    ]
    assert cfg.record.directory == "/data/rig1"
    assert cfg.record.relative_directory == "%y%m%d/001"
    assert [(v.name, v.layout) for v in cfg.visualization] == [
        ("grid.mp4", [["cam_a", "cam_b"]])
    ]
    assert cfg.transfer is not None
    assert cfg.transfer.directory == "/mnt/nas"
    assert cfg.transfer.checksum is True
    # Snapshotting is on by default: each detected camera's sensor params were
    # saved next to the config (the fake backend persists as `<serial>.fake`).
    assert {p.name for p in target.glob("*.fake")} == {"FAKE-0.fake", "FAKE-1.fake"}


def test_config_wizard_no_snapshot_params_skips_parameter_files(tmp_path):
    # --no-snapshot-params keeps the wizard enumeration-only: it writes the
    # config but never opens a camera, so no per-camera parameter file appears.
    target = tmp_path / "rig-noparams"
    inputs = "\n".join(["n", "", "", "", "", "", "", "", "", "n", "n"]) + "\n"
    result = runner.invoke(
        app,
        ["config", str(target), "--backend", "fake", "--no-snapshot-params"],
        input=inputs,
    )
    assert result.exit_code == 0, result.output
    assert (target / "octacam_config.toml").exists()
    assert not list(target.glob("*.fake"))


def test_config_wizard_skips_params_when_cameras_busy(tmp_path, monkeypatch):
    # A camera held by a live session cannot be opened: the wizard warns and
    # skips the parameter files rather than failing, leaving a valid config.
    from octacam.cameras.base import BackendError

    def busy(*_args, **_kwargs):
        raise BackendError("device is already exclusively opened by another client")

    monkeypatch.setattr("octacam.cameras.system.CameraSystem", busy)
    target = tmp_path / "rig-busy"
    inputs = "\n".join(["n", "", "", "", "", "", "", "", "", "n", "n"]) + "\n"
    result = runner.invoke(
        app, ["config", str(target), "--backend", "fake"], input=inputs
    )
    assert result.exit_code == 0, result.output
    assert (target / "octacam_config.toml").exists()
    assert not list(target.glob("*.fake"))
    assert "Skipping sensor parameters" in result.output


def test_config_wizard_prompts_for_directory_when_omitted(tmp_path):
    # With no CONFIG_DIR argument the wizard asks for one at the end.
    target = tmp_path / "prompted"
    inputs = (
        "\n".join(
            [
                "n",  # name cameras? no (leaves the grid unoffered)
                "",  # fps
                "",  # duration
                "",  # unit
                "",  # trigger
                "",  # preview trigger
                "",  # directory
                "",  # relative directory
                "",  # save method
                "n",  # transfer? no
                "n",  # enable a serial/trigger plugin? no
                str(target),  # config directory to create
            ]
        )
        + "\n"
    )
    result = runner.invoke(app, ["config", "--backend", "fake"], input=inputs)
    assert result.exit_code == 0, result.output
    assert (target / "octacam_config.toml").exists()


def test_config_wizard_aborts_without_overwriting(tmp_path):
    # An existing config is never clobbered without consent: declining the
    # overwrite prompt exits nonzero and leaves the file byte-for-byte intact.
    target = tmp_path / "existing"
    target.mkdir()
    sentinel = "# do not touch\n"
    (target / "octacam_config.toml").write_text(sentinel)
    inputs = (
        "\n".join(
            [
                "n",  # name cameras? no
                "",  # fps
                "",  # duration
                "",  # unit
                "",  # trigger
                "",  # preview trigger
                "",  # directory
                "",  # relative directory
                "",  # save method
                "n",  # transfer? no
                "n",  # enable a serial/trigger plugin? no
                "n",  # overwrite existing? no
            ]
        )
        + "\n"
    )
    result = runner.invoke(
        app, ["config", str(target), "--backend", "fake"], input=inputs
    )
    assert result.exit_code == 1
    assert (target / "octacam_config.toml").read_text() == sentinel


def test_config_wizard_force_overwrites(tmp_path):
    from octacam.config import load_config_dir

    target = tmp_path / "existing"
    target.mkdir()
    (target / "octacam_config.toml").write_text("# stale\n")
    inputs = "\n".join(["n", "", "", "", "", "", "", "", "", "n", "n"]) + "\n"
    result = runner.invoke(
        app, ["config", str(target), "--backend", "fake", "--force"], input=inputs
    )
    assert result.exit_code == 0, result.output
    # The stale placeholder was replaced by a real, parseable config.
    assert load_config_dir(target).backend == "fake"


def test_config_rejects_unknown_backend(tmp_path):
    result = runner.invoke(app, ["config", str(tmp_path / "rig"), "--backend", "nope"])
    assert result.exit_code == 2
    assert "unknown backend" in result.output
    assert not (tmp_path / "rig").exists()


# --------------------------------------------------------------------------- #
# doctor's update-available line (octacam.updates), monkeypatched — no network.


def _doctor_update_line(monkeypatch, notice):
    from octacam import updates
    from octacam.cli import _doctor_updates, _Report

    monkeypatch.setattr(updates, "check", lambda: notice)
    report = _Report()
    report.section("System")
    _doctor_updates(report)
    return report.sections[-1][1][-1]  # (status, text) of the line just added


def test_doctor_update_line_available(monkeypatch):
    from octacam.updates import UpdateNotice

    status, text = _doctor_update_line(
        monkeypatch,
        UpdateNotice("0.3.0", "0.9.0", True, "uv-tool", "uv tool upgrade octacam", ""),
    )
    assert status == "warn"
    assert "0.9.0" in text and "uv tool upgrade octacam" in text


def test_doctor_update_line_up_to_date(monkeypatch):
    from octacam.updates import UpdateNotice

    status, text = _doctor_update_line(
        monkeypatch, UpdateNotice("0.3.0", "0.3.0", False, "pip", "", "")
    )
    assert status == "ok" and "latest release" in text


def test_doctor_update_line_skipped_for_dev_install(monkeypatch):
    from octacam.updates import UpdateNotice

    status, text = _doctor_update_line(
        monkeypatch,
        UpdateNotice("0.3.1.dev0", None, False, "editable", "", "development install"),
    )
    assert status == "info" and "development install" in text


# --- recording layout: summary/snapshot in an octacam_recording subfolder -----
#
# A recording keeps its summary, timestamps, config snapshot and camera
# parameter files in an ``octacam_recording`` subfolder; one made before that
# keeps them flat beside the videos. Both must be found everywhere, and the
# subfolder must never pass for a recording of its own.


def _layout_recording(folder, *, nested, toml=None):
    """A recording folder in either layout: a summary and, when *toml* is
    given, a config snapshot, both flat or in the ``octacam_recording``
    subfolder (*nested*). Returns the directory holding them."""
    from octacam.transform import RECORDING_INFO_DIRNAME, RECORDING_SUMMARY_FILENAME

    info = folder / RECORDING_INFO_DIRNAME if nested else folder
    info.mkdir(parents=True, exist_ok=True)
    (folder / "cam.mkv").write_bytes(b"source-bytes")
    (info / RECORDING_SUMMARY_FILENAME).write_text(
        json.dumps({"cameras": [{"name": "cam", "file": "cam.mkv", "frames": 1}]})
    )
    if toml is not None:
        (info / "octacam_config.toml").write_text(toml)
    return info


@pytest.mark.parametrize("nested", [False, True], ids=["flat", "nested"])
def test_find_recording_dirs_accepts_a_recording_in_either_layout(tmp_path, nested):
    from octacam.cli import _find_recording_dirs

    rec = tmp_path / "rec"
    _layout_recording(rec, nested=nested)
    assert _find_recording_dirs([rec], recursive=False) == [rec]
    assert _find_recording_dirs([rec], recursive=True) == [rec]


def test_find_recording_dirs_recursive_mixed_tree_never_lists_the_info_dir(tmp_path):
    from octacam.cli import _find_recording_dirs

    flat = tmp_path / "day1" / "fly1"
    nested = tmp_path / "day1" / "fly2"
    deep = tmp_path / "day2" / "session" / "fly3"
    _layout_recording(flat, nested=False)
    _layout_recording(nested, nested=True)
    _layout_recording(deep, nested=True)
    # Recorded into again after the layout change: the folder holds both an
    # older take's flat summary and the new take's nested one; still one recording.
    _layout_recording(flat, nested=True)
    found = _find_recording_dirs([tmp_path], recursive=True)
    assert found == [flat, nested, deep]


def test_find_recording_dirs_hints_recursive_for_nested_layout(tmp_path):
    # Non-recursive on a parent: the nested-layout recordings beneath it are
    # counted for the -r hint (never their subfolders), then it exits.
    from octacam.cli import _find_recording_dirs

    _layout_recording(tmp_path / "a", nested=True)
    _layout_recording(tmp_path / "b", nested=False)
    with pytest.raises(SystemExit) as exc:
        _find_recording_dirs([tmp_path], recursive=False)
    assert "-r/--recursive" in str(exc.value)


def test_find_recording_dirs_info_dir_named_directly_means_its_recording(tmp_path):
    from octacam.cli import _find_recording_dirs
    from octacam.transform import RECORDING_INFO_DIRNAME

    rec = tmp_path / "rec"
    _layout_recording(rec, nested=True)
    info = rec / RECORDING_INFO_DIRNAME
    assert _find_recording_dirs([info], recursive=False) == [rec]
    assert _find_recording_dirs([info, rec], recursive=True) == [rec]


@pytest.mark.parametrize("nested", [False, True], ids=["flat", "nested"])
def test_config_for_recording_reads_the_snapshot_in_either_layout(tmp_path, nested):
    from octacam.cli import _config_for_recording

    rec = tmp_path / "rec"
    _layout_recording(rec, nested=nested, toml='[record]\nfps = 42\n')
    # The fallback must not be consulted when the recording has its own snapshot.
    fallback = tmp_path / "rig"
    fallback.mkdir()
    (fallback / "octacam_config.toml").write_text('[record]\nfps = 7\n')
    assert _config_for_recording(rec, fallback).record.fps == 42


def test_config_for_recording_nested_snapshot_beats_an_older_flat_one(tmp_path):
    from octacam.cli import _config_for_recording

    rec = tmp_path / "rec"
    _layout_recording(rec, nested=False, toml='[record]\nfps = 11\n')
    _layout_recording(rec, nested=True, toml='[record]\nfps = 22\n')
    assert _config_for_recording(rec, None).record.fps == 22


def test_config_for_recording_without_snapshot_falls_back(tmp_path):
    from octacam.cli import _config_for_recording

    rec = tmp_path / "rec"
    _layout_recording(rec, nested=True)
    fallback = tmp_path / "rig"
    fallback.mkdir()
    (fallback / "octacam_config.toml").write_text('[record]\nfps = 7\n')
    assert _config_for_recording(rec, fallback).record.fps == 7


def test_process_no_transcode_finds_nested_recording_and_its_transfer_dest(
    tmp_path, monkeypatch
):
    # The no-transcode path discovers recordings via _find_recording_dirs and
    # reads the summary's relative_directory for the transfer destination.
    from octacam.transform import RECORDING_INFO_DIRNAME, RECORDING_SUMMARY_FILENAME

    rec = tmp_path / "data" / "rec"
    dest = tmp_path / "archive"
    info = _layout_recording(
        rec, nested=True, toml=f'[transfer]\ndirectory = "{dest}"\n'
    )
    summary = json.loads((info / RECORDING_SUMMARY_FILENAME).read_text())
    summary["relative_directory"] = "2026/rec"
    (info / RECORDING_SUMMARY_FILENAME).write_text(json.dumps(summary))
    (rec / "cam.mp4").write_bytes(b"finished")
    seen = {}

    def fake_transfer(folder, destination, **kwargs):
        seen["folder"], seen["destination"] = folder, destination
        return SimpleNamespace(copied=[], skipped=[], failed=[])

    monkeypatch.setattr("octacam.transfer.transfer_folder", fake_transfer)
    result = runner.invoke(
        app, ["--log-level", "error", "process", "-r", str(tmp_path / "data"),
              "--no-transcode", "--no-grid"],
    )
    assert result.exit_code == 0, result.output
    assert seen["folder"] == rec
    assert seen["destination"] == dest / "2026" / "rec"
    assert RECORDING_INFO_DIRNAME not in str(seen["folder"])


# --- CONFIG_DIR may name a recording folder (relaunch from its snapshot) ------


def test_resolve_config_dir_redirects_a_recording_folder(tmp_path):
    from octacam.cli import _resolve_config_dir
    from octacam.transform import RECORDING_INFO_DIRNAME

    rec = tmp_path / "rec"
    _layout_recording(rec, nested=True, toml="")
    assert _resolve_config_dir(rec) == rec / RECORDING_INFO_DIRNAME
    # A rig dir, and a flat (older) recording, are config dirs of their own.
    flat = tmp_path / "flat"
    _layout_recording(flat, nested=False, toml="")
    assert _resolve_config_dir(flat) == flat
    rig = tmp_path / "rig"
    rig.mkdir()
    (rig / "octacam_config.toml").write_text("")
    assert _resolve_config_dir(rig) == rig


def test_gui_relaunches_from_a_recording_folders_snapshot(tmp_path, monkeypatch):
    # The instance lock is the first thing keyed on the resolved dir; refusing it
    # stops the launch before any hardware is touched.
    from octacam.transform import RECORDING_INFO_DIRNAME

    rec = tmp_path / "rec"
    _layout_recording(rec, nested=True, toml="")
    seen = []

    def refuse(config_dir):
        seen.append(config_dir)
        return None

    monkeypatch.setattr("octacam.cli._acquire_instance_lock", refuse)
    result = runner.invoke(app, ["--log-level", "error", "gui", str(rec), "--no-browser"])
    assert result.exit_code != 0
    assert seen == [(rec / RECORDING_INFO_DIRNAME).resolve()]


@pytest.mark.parametrize("command", ["record", "benchmark"])
def test_record_and_benchmark_load_a_recording_folders_snapshot(
    tmp_path, monkeypatch, command
):
    from octacam.transform import RECORDING_INFO_DIRNAME

    rec = tmp_path / "rec"
    _layout_recording(rec, nested=True, toml="")
    seen = []

    def stop(config_dir):
        seen.append(Path(config_dir))
        raise SystemExit("stop")

    monkeypatch.setattr("octacam.config.load_config_dir", stop)
    result = runner.invoke(app, ["--log-level", "error", command, str(rec)])
    assert result.exit_code != 0
    assert seen == [rec / RECORDING_INFO_DIRNAME]


@pytest.mark.parametrize(
    "argv",
    [["doctor", "{rec}"], ["flash", "{rec}"], ["process", "{rec}", "--config", "{rec}"]],
    ids=["doctor", "flash", "process-config"],
)
def test_other_config_dir_commands_resolve_a_recording_folder(
    tmp_path, monkeypatch, argv
):
    # Each goes through the one resolver where it first takes the path.
    from octacam import cli

    rec = tmp_path / "rec"
    _layout_recording(rec, nested=True, toml="")
    seen = []
    real = cli._resolve_config_dir

    def spy(config_dir):
        seen.append(real(config_dir))
        raise SystemExit("stop")

    monkeypatch.setattr("octacam.cli._resolve_config_dir", spy)
    args = [a.format(rec=rec) for a in argv]
    result = runner.invoke(app, ["--log-level", "error", *args])
    assert result.exit_code != 0
    assert seen == [rec / "octacam_recording"]


# --- the rig instance lock is keyed on the resolved config dir ---------------


def test_flash_refuses_a_rig_the_gui_holds_however_the_path_is_spelled(
    tmp_path, monkeypatch
):
    # The GUI locks the resolved config dir. flash resets the board, so it must
    # find that lock from a relative path too: the board may be mid-recording.
    rig = tmp_path / "rig"
    rig.mkdir()
    held = _acquire_instance_lock(rig.resolve())
    assert held is not None and held is not _LOCK_UNAVAILABLE
    monkeypatch.chdir(tmp_path)
    try:
        result = runner.invoke(app, ["flash", "rig"])
    finally:
        held.close()
    assert result.exit_code == 2, result.output
    assert "Another octacam instance owns this rig" in result.output
    assert f"(pid {os.getpid()})" in result.output


# --- `octacam flash` against a faked triggerbox board -------------------------


def _current_triggerbox_build():
    from octacam import firmware as fw

    return fw.sketch_fingerprint(fw.resolve_sketch_dir("triggerbox"))


@pytest.fixture
def fake_triggerbox(monkeypatch):
    """The triggerbox link, faked at the class: the board answers ``banner`` and
    ``devices`` lists every port the plugin opened."""
    from octacam.plugins.triggerbox import TriggerboxLink

    board = SimpleNamespace(open=False, banner="TRIGGERBOX 2 oldbuild", devices=[])

    def open_(self, device, baud):
        board.devices.append(device)
        board.open = True

    monkeypatch.setattr(TriggerboxLink, "open", open_)
    monkeypatch.setattr(TriggerboxLink, "close", lambda self: setattr(board, "open", False))
    monkeypatch.setattr(TriggerboxLink, "is_open", property(lambda self: board.open))
    monkeypatch.setattr(TriggerboxLink, "identify", lambda self, timeout=0.5: board.banner)
    monkeypatch.setattr(TriggerboxLink, "send_cancel", lambda self: None)
    return board


@pytest.mark.parametrize(
    ("flags", "exit_code", "flashed"),
    [(["--check"], 1, False), (["--yes"], 0, True)],
    ids=["check", "yes"],
)
def test_flash_reports_a_stale_board_and_flashes_it_unless_checking(
    fake_triggerbox, flags, exit_code, flashed
):
    result = runner.invoke(
        app,
        ["--log-level", "error", "flash", "--plugin", "triggerbox",
         "--device", "/dev/ttyFAKE7", *flags],
    )
    assert result.exit_code == exit_code, result.output
    assert "triggerbox — /dev/ttyFAKE7" in result.output
    assert "needs flashing" in result.output
    assert ("uploaded build" in result.output) is flashed
    # --device reaches the plugin; a flash closes and reopens the port.
    assert fake_triggerbox.devices == ["/dev/ttyFAKE7"] * (2 if flashed else 1)


def test_flash_names_a_missing_arduino_cli(fake_triggerbox, monkeypatch):
    monkeypatch.setattr("octacam.firmware.arduino_cli_path", lambda: None)
    result = runner.invoke(
        app, ["--log-level", "error", "flash", "--plugin", "triggerbox", "--device", "/dev/x",
              "--yes"],
    )
    assert result.exit_code == 1, result.output
    flat = " ".join(result.output.split())
    assert "needs flashing" in flat
    assert "Can't auto-flash: arduino-cli was not found" in flat
    assert "uploaded build" not in flat


def test_flash_yes_still_warns_of_an_unidentified_board(fake_triggerbox):
    fake_triggerbox.banner = None
    result = runner.invoke(
        app,
        ["--log-level", "error", "flash", "--plugin", "triggerbox", "--device", "/dev/x",
         "--yes"],
    )
    assert result.exit_code == 0, result.output
    flat = " ".join(result.output.split())
    assert "the board sent no identity" in flat
    assert "Upload the current firmware" not in flat


def test_flash_leaves_a_current_board_alone(fake_triggerbox):
    fake_triggerbox.banner = f"TRIGGERBOX 2 {_current_triggerbox_build()}"
    result = runner.invoke(
        app, ["--log-level", "error", "flash", "--plugin", "triggerbox", "--device", "/dev/x"]
    )
    assert result.exit_code == 0, result.output
    assert "up to date" in result.output


def test_flash_warns_before_overwriting_an_unidentified_board(fake_triggerbox):
    fake_triggerbox.banner = None
    result = runner.invoke(
        app,
        ["--log-level", "error", "flash", "--plugin", "triggerbox", "--device", "/dev/x"],
        input="n\n",
    )
    assert result.exit_code == 1, result.output
    flat = " ".join(result.output.split())
    assert "the board sent no identity" in flat
    assert "Upload the current firmware to /dev/x?" in flat
    assert "skipped" in flat


@pytest.mark.parametrize("flags", [[], ["--yes"], ["--check"]], ids=["ask", "yes", "check"])
def test_flash_without_the_sketch_never_calls_a_board_up_to_date(
    fake_triggerbox, monkeypatch, flags
):
    from dataclasses import replace

    from octacam.plugins.triggerbox import TriggerboxPlugin

    # A wheel install: no source build to compare the board against or flash.
    monkeypatch.setattr(
        TriggerboxPlugin, "firmware", replace(TriggerboxPlugin.firmware, sketch_dir=None)
    )
    result = runner.invoke(
        app,
        ["--log-level", "error", "flash", "--plugin", "triggerbox", "--device", "/dev/x",
         *flags],
    )
    assert result.exit_code == 1, result.output
    flat = " ".join(result.output.split())
    assert "board firmware: TRIGGERBOX 2 oldbuild" in flat
    assert "? unknown — the sketch source is not available" in flat
    assert "up to date" not in flat
    assert ("Can't auto-flash" in flat) is (flags != ["--check"])
    assert "uploaded build" not in flat
    assert fake_triggerbox.devices == ["/dev/x"]


def test_flash_reports_a_board_that_does_not_open():
    result = runner.invoke(
        app,
        ["--log-level", "error", "flash", "--plugin", "triggerbox",
         "--device", "/nonexistent/ttyACM0"],
    )
    assert result.exit_code == 1, result.output
    assert "could not open the board" in result.output


@pytest.mark.parametrize(("plugin", "exit_code"), [("bogus", 1), ("", 0)])
def test_flash_without_a_flashable_plugin(tmp_path, plugin, exit_code):
    (tmp_path / "octacam_config.toml").write_text("")
    args = ["--plugin", plugin] if plugin else [str(tmp_path)]
    result = runner.invoke(app, ["--log-level", "error", "flash", *args])
    assert result.exit_code == exit_code, result.output
    assert "No firmware-flashable serial plugin" in result.output


@pytest.mark.parametrize(
    ("banner", "line"),
    [
        ("TRIGGERBOX 2 oldbuild", "triggerbox firmware needs flashing"),
        ("FLYWHEEL 1 abc", "triggerbox firmware needs flashing"),
        (None, "triggerbox firmware up to date"),
    ],
    ids=["outdated", "foreign", "current"],
)
def test_doctor_probe_classifies_a_configured_board(
    emulated_rig, monkeypatch, tmp_path, banner, line
):
    banner = banner or f"TRIGGERBOX 2 {_current_triggerbox_build()}"
    flat = _doctor_probe_triggerbox(monkeypatch, tmp_path, banner)
    assert "plugin 'triggerbox' device /dev/ttyACM0 is connected" in flat
    assert line in flat


@pytest.mark.parametrize(
    ("banner", "foreign"), [("FLYWHEEL 1 abc", True), ("TRIGGERBOX 2 x", False)]
)
def test_doctor_probe_without_the_sketch_checks_the_banner_name(
    emulated_rig, monkeypatch, tmp_path, banner, foreign
):
    from dataclasses import replace

    from octacam.plugins.triggerbox import TriggerboxPlugin

    # A wheel install: no source build to compare, only the board's name.
    monkeypatch.setattr(
        TriggerboxPlugin, "firmware", replace(TriggerboxPlugin.firmware, sketch_dir=None)
    )
    flat = _doctor_probe_triggerbox(monkeypatch, tmp_path, banner)
    assert ("wrong board?" in flat) is foreign
    if foreign:
        assert (
            "/dev/ttyACM0: expected triggerbox firmware (banner 'TRIGGERBOX') but "
            "got 'FLYWHEEL 1 abc' — wrong board?"
        ) in flat


def _doctor_probe_triggerbox(monkeypatch, tmp_path, banner) -> str:
    """``doctor --probe-serial``'s output, whitespace-collapsed, for a rig whose
    triggerbox board on its default port answers ``banner``."""
    from octacam.serial_ports import SerialIdentity

    monkeypatch.setattr(
        "octacam.serial_ports.list_serial_ports",
        lambda: [_fake_serial_port("/dev/ttyACM0")],
    )
    monkeypatch.setattr(
        "octacam.serial_ports.probe_identity",
        lambda device, **kw: SerialIdentity(device, banner, False, None),
    )
    # No device option: the plugin's default port, /dev/ttyACM0.
    (tmp_path / "octacam_config.toml").write_text('[[plugins]]\nname = "triggerbox"\n')
    result = runner.invoke(
        app, ["--log-level", "error", "doctor", "--probe-serial", str(tmp_path)]
    )
    return " ".join(result.output.split())


# --- record's firmware preflight ----------------------------------------------


class _StaleBoardPlugin(Plugin):
    """A serial plugin whose board runs an old build of its own sketch (or, with
    ``state="unidentified"``, sent no banner)."""

    name = "triggerbox"
    firmware = FirmwareSpec(
        name="triggerbox",
        sketch_dir=None,
        fqbn="arduino:esp32:nano_nora",
        banner_prefix="TRIGGERBOX",
        protocol_version=2,
        build_define="TRIGGERBOX_FW_BUILD",
    )

    def __init__(self, auto_flash, state="outdated"):
        self.auto_flash = auto_flash
        self.state = state
        self.flashed = 0

    def firmware_provisioning(self):
        return {
            "device": "/dev/ttyACM0",
            "detail": "board build abc, source build def",
            "state": self.state,
            "needs_flash": True,
            "can_flash": True,
            "safe_to_auto_flash": self.state == "outdated",
            "auto_flash": self.auto_flash,
        }

    def flash_firmware(self, on_line=None):
        self.flashed += 1
        return SimpleNamespace(ok=True, message="flashed")


@pytest.mark.parametrize("auto_flash", [True, False])
def test_record_preflight_reflashes_headless_only_with_auto_flash(
    monkeypatch, caplog, auto_flash
):
    from octacam.cli import _preflight_firmware

    monkeypatch.setattr(sys, "stdin", SimpleNamespace(isatty=lambda: False))
    plugin = _StaleBoardPlugin(auto_flash)
    _preflight_firmware(PluginManager([plugin]), assume_yes=False)
    assert plugin.flashed == (1 if auto_flash else 0)
    if not auto_flash:
        assert "pass --yes or set auto_flash=true" in caplog.text


@pytest.mark.parametrize("answer", [True, False])
def test_record_preflight_asks_on_a_tty_and_warns_of_an_unidentified_board(
    monkeypatch, capsys, answer
):
    from rich.prompt import Confirm

    from octacam.cli import _preflight_firmware

    asked = []
    monkeypatch.setattr(sys, "stdin", SimpleNamespace(isatty=lambda: True))
    monkeypatch.setattr(
        Confirm, "ask", lambda prompt, **kw: asked.append(prompt) or answer
    )
    plugin = _StaleBoardPlugin(auto_flash=False, state="unidentified")
    _preflight_firmware(PluginManager([plugin]), assume_yes=False)
    assert asked == ["Upload the current firmware to /dev/ttyACM0?"]
    assert plugin.flashed == (1 if answer else 0)
    err = " ".join(capsys.readouterr().err.split())
    assert "board firmware on /dev/ttyACM0 is out of date" in err
    assert "the board sent no identity" in err


@pytest.mark.parametrize("interactive", [True, False], ids=["tty", "headless"])
@pytest.mark.parametrize("state", ["outdated", "unidentified"])
def test_record_preflight_yes_never_prompts_or_flashes_a_blank_board(
    monkeypatch, caplog, interactive, state
):
    from rich.prompt import Confirm

    from octacam.cli import _preflight_firmware

    def ask(prompt, **kw):
        raise AssertionError(f"--yes prompted: {prompt}")

    monkeypatch.setattr(sys, "stdin", SimpleNamespace(isatty=lambda: interactive))
    monkeypatch.setattr(Confirm, "ask", ask)
    plugin = _StaleBoardPlugin(auto_flash=False, state=state)
    _preflight_firmware(PluginManager([plugin]), assume_yes=True)
    assert plugin.flashed == (1 if state == "outdated" else 0)
    if state == "unidentified":
        assert "run `octacam flash` to reflash. Continuing WITHOUT reflashing" in caplog.text


# --- record closes the cameras on every exit before the controller owns them --


@pytest.mark.parametrize(
    ("serials", "failure"),
    [
        (["NOT-PRESENT"], None),  # nothing opened
        (["FAKE-0", "NOT-PRESENT"], "decline"),  # the incomplete-rig prompt
        (["FAKE-0"], "decline"),  # the overwrite prompt
        (["FAKE-0"], "load_config"),
        (["FAKE-0"], "apply_display_config"),
    ],
    ids=["no-camera", "incomplete-declined", "overwrite-declined", "load", "display"],
)
def test_record_closes_the_cameras_when_it_exits_before_recording(
    tmp_path, monkeypatch, serials, failure
):
    import io

    from octacam import cli
    from octacam.cameras import CameraSystem

    class Tty(io.StringIO):
        def isatty(self):
            return True

    rig = tmp_path / "rig"
    rig.mkdir()
    (rig / "octacam_config.toml").write_text(
        'backend = "fake"\n'
        + "".join(f'[[cameras]]\nserial_number = "{s}"\n' for s in serials)
    )
    save_dir = tmp_path / "take"
    save_dir.mkdir()  # exists, so an interactive run asks before overwriting it
    closed = []
    close = CameraSystem.close
    monkeypatch.setattr(
        CameraSystem, "close", lambda self: (closed.append(self), close(self))
    )
    if failure == "decline":
        monkeypatch.setattr(sys, "stdin", Tty())
        monkeypatch.setattr(sys, "stderr", Tty())
        monkeypatch.setattr("typer.confirm", lambda *_a, **_k: False)
    elif failure is not None:

        def fail(*_a, **_k):
            raise RuntimeError(failure)

        monkeypatch.setattr(CameraSystem, failure, fail)
    # Called directly: CliRunner's streams are never a tty, so it cannot prompt.
    with pytest.raises((SystemExit, RuntimeError)):  # typer.Exit is a RuntimeError
        cli.record(rig, output=save_dir)
    assert len(closed) == 1


@pytest.mark.parametrize(
    ("serial", "failure"),
    [("NOT-PRESENT", None), ("FAKE-0", "load_config"), ("FAKE-0", "apply_display_config")],
    ids=["no-camera", "load", "display"],
)
def test_benchmark_closes_the_cameras_when_it_exits_before_measuring(
    tmp_path, monkeypatch, serial, failure
):
    from octacam.cameras import CameraSystem

    (tmp_path / "octacam_config.toml").write_text(
        f'backend = "fake"\n[[cameras]]\nserial_number = "{serial}"\n'
    )
    closed = []
    close = CameraSystem.close
    monkeypatch.setattr(
        CameraSystem, "close", lambda self: (closed.append(self), close(self))
    )
    if failure is not None:

        def fail(*_a, **_k):
            raise RuntimeError(failure)

        monkeypatch.setattr(CameraSystem, failure, fail)
    result = runner.invoke(app, ["--log-level", "error", "benchmark", str(tmp_path)])
    assert result.exit_code != 0
    assert len(closed) == 1


@pytest.mark.parametrize(
    ("extra", "sink", "record_form", "fps", "with_bar"),
    [
        ([], "config", "display", 100.0, True),
        (
            ["--sink", "null", "--record-form", "sensor", "--fps", "50", "--json"],
            "null",
            "sensor",
            50.0,
            False,
        ),
    ],
)
def test_benchmark_passes_its_options_to_diagnose(
    tmp_path, monkeypatch, extra, sink, record_form, fps, with_bar
):
    (tmp_path / "octacam_config.toml").write_text(
        'backend = "fake"\n[[cameras]]\nserial_number = "FAKE-0"\n'
    )
    seen = {}

    def diagnose(system, settings, **kwargs):
        seen.update(kwargs, record_form=settings.record_form, fps=settings.fps)
        raise SystemExit("stop")

    monkeypatch.setattr("octacam.diagnostics.diagnose", diagnose)
    result = runner.invoke(
        app, ["--log-level", "error", "benchmark", str(tmp_path), *extra]
    )
    assert result.exit_code != 0
    assert type(seen["sink"]) is str and seen["sink"] == sink
    assert type(seen["record_form"]) is str and seen["record_form"] == record_form
    assert seen["fps"] == fps
    assert callable(seen.get("progress_cb")) is with_bar


@pytest.mark.parametrize("option", ["--sink", "--record-form"])
def test_benchmark_rejects_an_unknown_choice(tmp_path, option):
    result = runner.invoke(app, ["benchmark", str(tmp_path), option, "bogus"])
    assert result.exit_code == 2
    assert option in result.output
