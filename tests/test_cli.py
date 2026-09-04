"""CLI smoke tests for the typer app (no real recording is started)."""

import json
import logging
import os
import socket
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

os.environ.setdefault("PYLON_CAMEMU", "2")

from typer.testing import CliRunner

import octacam
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


def test_gui_reports_cameras_in_use(tmp_path, monkeypatch):
    # The GUI now serves the page before opening the cameras, so a camera-open
    # failure (e.g. another octacam holds them — SDKs open USB3 devices
    # exclusively) no longer exits the process. It is surfaced in the GUI: the
    # background init calls controller.fail_init with a clean message (not a raw
    # SDK traceback), the server stays up, and the browser shows the reason.
    import octacam.cameras as cameras_mod
    from octacam.cameras import BackendError
    from octacam.controller import RecordingSettings

    real_cs = cameras_mod.CameraSystem

    class BusyCameraSystem:
        @classmethod
        def pending(cls, backend="auto"):
            # The sync path still builds a real hardware-free placeholder to
            # serve against; only opening the real cameras fails.
            return real_cs.pending(backend)

        def __init__(self, *_a, **_k):
            raise BackendError("The device is controlled by another application.")

    monkeypatch.setattr("octacam.cameras.CameraSystem", BusyCameraSystem)

    config = SimpleNamespace(
        cameras=[SimpleNamespace(serial_number="0815-0000", name="cam0")],
        backend="fake",
        record=None,
        transcode=None,
        transfer=None,
    )
    monkeypatch.setattr("octacam.config.load_config_dir", lambda _dir: config)
    monkeypatch.setattr("octacam.plugins.build_plugins", lambda *a, **k: _FakePlugins())
    monkeypatch.setattr(
        "octacam.cli._settings_from_record",
        lambda *a, **k: RecordingSettings(save_dir=str(tmp_path / "rec")),
    )

    captured = {}

    def fake_run(app_obj, **_kwargs):
        # Stand in for uvicorn.run: capture the controller, wait for the
        # background init to finish (fail), then "shut down" by returning.
        ctrl = app_obj.state.app_state.controller
        captured["controller"] = ctrl
        for _ in range(500):
            if ctrl.ready or ctrl.init_error:
                break
            time.sleep(0.01)

    monkeypatch.setattr("uvicorn.run", fake_run)

    # --port 0 binds an ephemeral port for the availability probe.
    result = runner.invoke(app, ["gui", str(tmp_path), "--port", "0", "--no-browser"])
    assert result.exit_code == 0, result.output  # served + shut down cleanly
    ctrl = captured["controller"]
    assert ctrl.ready is False
    assert "in use by another octacam" in (ctrl.init_error or "")


def _fake_camera_system(cam):
    class FakeSystem:
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


class _FakePlugins:
    plugins: list = []

    def setup_all(self):
        _FACADE_CALLS.append("setup_all")

    def teardown_all(self):
        _FACADE_CALLS.append("teardown_all")

    def status(self):
        return {}


def test_gui_tears_down_when_create_app_raises(tmp_path, monkeypatch):
    # create_app() runs inside the try (before any hardware is armed); if it
    # raises, the finally must still run controller.close() and
    # plugins.teardown_all() so nothing is left half-initialized.
    import octacam.cli as cli_mod

    _FACADE_CALLS.clear()
    cam = SimpleNamespace(serial_number="s1", name="cam1")
    config = SimpleNamespace(
        cameras=[cam], backend="fake", record=None, transcode=None, transfer=None
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
    monkeypatch.setattr(cli_mod, "_settings_from_record", lambda *a, **k: object())
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

    _FACADE_CALLS.clear()
    cam = SimpleNamespace(serial_number="s1", name="cam1")
    config = SimpleNamespace(
        cameras=[cam], backend="fake", record=object(), transcode=None, transfer=None
    )
    monkeypatch.setattr("octacam.config.load_config_dir", lambda _dir: config)
    monkeypatch.setattr("octacam.cameras.CameraSystem", _fake_camera_system(cam))

    settings = SimpleNamespace(
        save_dir=str(tmp_path / "does-not-exist"),
        duration_s=1.0,
        fps=10.0,
    )
    monkeypatch.setattr(cli_mod, "_settings_from_record", lambda *a, **k: settings)
    monkeypatch.setattr(cli_mod, "_preflight_firmware", lambda *a, **k: None)

    class FakePlugins:
        plugins: list = []

        def setup_all(self):
            _FACADE_CALLS.append("setup_all")

        def teardown_all(self):
            _FACADE_CALLS.append("teardown_all")

        def default_start_params(self, *_a, **_k):
            return {}

    monkeypatch.setattr("octacam.plugins.build_plugins", lambda *a, **k: FakePlugins())

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
    monkeypatch.setattr(
        "octacam.controller.normalize_save_dir", lambda s: s, raising=False
    )

    result = runner.invoke(app, ["record", str(tmp_path)])
    assert result.exit_code != 0  # the RuntimeError from join() propagates
    assert "controller.close" in _FACADE_CALLS
    assert "system.close" not in _FACADE_CALLS  # no bare system teardown race


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
        "--twophoton-sweep",
        "--no-twophoton-sweep",
        "--migrate-layout",
        "--delete-after-transfer",
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
    assert any("transcod" in m and "auto-pause" in m for m in handler.messages)


def test_print_transcode_hints_lists_session_and_all(tmp_path, monkeypatch):
    from octacam import cli, session_cache

    monkeypatch.setenv("OCTACAM_CACHE_DIR", str(tmp_path / "cache"))
    rec = tmp_path / "rec" / "001"
    rec.mkdir(parents=True)
    session_cache.record_recording(rec, "sessZ", "gui")

    logger, handler = _capture_octacam_logs(logging.INFO)
    try:
        cli._print_transcode_hints("sessZ")
    finally:
        logger.removeHandler(handler)
    blob = "\n".join(handler.messages)
    # Two ready-to-run selectors: the last session and every cached session.
    assert "--last session" in blob and "--all" in blob

    # A session that recorded nothing prints no hint.
    logger, handler = _capture_octacam_logs(logging.INFO)
    try:
        cli._print_transcode_hints("sessNONE")
    finally:
        logger.removeHandler(handler)
    assert not handler.messages


def test_resolve_enabled():
    # None / empty -> no override (use the config).
    assert _resolve_enabled(None, False) is None
    assert _resolve_enabled([], False) is None
    # Explicit plugin names are passed through.
    assert _resolve_enabled(["flywheel"], False) == ["flywheel"]
    # --no-plugins wins and disables everything.
    assert _resolve_enabled(["flywheel"], True) == []
    assert _resolve_enabled(None, True) == []


def test_doctor_lists_cameras_plugins_and_toolchain():
    # `doctor` lists cameras + plugins and adds diagnostics.
    result = runner.invoke(app, ["doctor"])
    assert result.exit_code == 0, result.output
    for heading in ("Camera backends", "Encoding toolchain", "Plugins"):
        assert heading in result.output
    # PYLON_CAMEMU=2 guarantees the emulated cameras (and thus the basler
    # backend) show up, and the bundled flywheel plugin is always listed.
    assert "0815-0000" in result.output
    assert "flywheel" in result.output


def test_doctor_json_is_machine_readable():
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


def test_doctor_flags_undetected_camera_and_exits_nonzero(tmp_path):
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


def test_doctor_enumerates_each_backend_at_most_once(monkeypatch):
    # The dedup invariant: one parallel scan, so no backend is enumerated twice —
    # even though the report has three consumers (the tier list, the cascade line,
    # and the cameras-vs-config cross-check). basler is always present (emulated).
    counter = _count_enumerations(monkeypatch)
    result = runner.invoke(app, ["--log-level", "error", "doctor"])
    assert result.exit_code == 0, result.output
    assert counter["basler"] == 1
    assert max(counter.values()) <= 1, dict(counter)


def test_doctor_backend_filter_scans_only_that_backend(monkeypatch):
    # `--backend X` must scan only X (no full-cascade sweep), so the other tiers
    # are never touched.
    counter = _count_enumerations(monkeypatch)
    result = runner.invoke(app, ["--log-level", "error", "doctor", "--backend", "basler"])
    assert result.exit_code == 0, result.output
    assert set(counter) == {"basler"}, dict(counter)


def test_doctor_backend_filter_is_case_insensitive():
    # --backend is normalized like select_backend/_enumerate_backend, so an
    # upper/mixed-case tier name still resolves to its cached scan (regression:
    # the scan cache is keyed by the lowercased name).
    result = runner.invoke(app, ["--log-level", "error", "doctor", "--backend", "BASLER"])
    assert result.exit_code == 0, result.output
    assert "BASLER: available" in result.output
    assert "enumeration failed" not in result.output


def test_doctor_json_has_no_progress_noise():
    # The scan's live spinner renders on stderr and is suppressed for --json / when
    # output is not a terminal, so machine-readable output is never corrupted.
    result = runner.invoke(app, ["--log-level", "error", "doctor", "--json"])
    assert result.exit_code == 0, result.output
    assert "enumerating" not in result.output
    json.loads(result.output)  # still valid JSON


def test_doctor_report_order_is_deterministic():
    # Parallel enumeration must not leak completion order into the report: the
    # Camera-backends section is assembled in a fixed backend order both times.
    def backends_section(output: str) -> str:
        lines = output.splitlines()
        start = next(i for i, ln in enumerate(lines) if ln.strip() == "Camera backends")
        end = next(
            (i for i in range(start + 1, len(lines)) if lines[i].strip() == "Encoding toolchain"),
            len(lines),
        )
        return "\n".join(lines[start:end])

    first = runner.invoke(app, ["--log-level", "error", "doctor"])
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


def test_enumerate_backend_resolves_model_via_backend_read_model(monkeypatch):
    # End-to-end of the asymmetry fix: the REAL _enumerate_backend generic path
    # must resolve the backend's module-level read_model (by module, by name) and
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


def test_doctor_groups_cameras_by_model_including_non_basler(monkeypatch):
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


def test_doctor_omits_free_gui_port_line():
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


def test_doctor_lists_serial_devices(monkeypatch):
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


def test_doctor_serial_flags_missing_configured_device(monkeypatch, tmp_path):
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


def test_doctor_serial_section_in_json(monkeypatch):
    monkeypatch.setattr(
        "octacam.serial_ports.list_serial_ports",
        lambda: [_fake_serial_port("/dev/ttyACM0")],
    )
    result = runner.invoke(app, ["--log-level", "error", "doctor", "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    titles = [s["title"] for s in payload["sections"]]
    assert "Serial devices" in titles


def test_doctor_probe_serial_reports_firmware(monkeypatch):
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


def test_doctor_probe_serial_skips_busy_port(monkeypatch):
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


# --- process: idempotent re-runs (skip existing outputs) --------------------


def _make_recording(folder, *, with_outputs, extra_toml="", extra_summary=None):
    """A recording folder with one camera's source .mkv and its summary.

    When *with_outputs*, also drop a finished ``camera_LF.mp4`` and ``grid.mp4``
    so ``octacam process``'s skip-on-exists path is exercised. *extra_toml*, if
    given, is written as the embedded ``octacam_config.toml`` snapshot.
    *extra_summary*, if given, is merged into the written recording_summary.json
    (e.g. ``start_time_ns``/``duration_s``/``plugins`` for 2P-transfer tests).
    """
    from octacam.transform import RECORDING_SUMMARY_FILENAME

    folder.mkdir(parents=True, exist_ok=True)
    (folder / "camera_LF.mkv").write_bytes(b"source-bytes")
    summary = {
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
    summary.update(extra_summary or {})
    (folder / RECORDING_SUMMARY_FILENAME).write_text(json.dumps(summary))
    if extra_toml:
        (folder / "octacam_config.toml").write_text(extra_toml)
    if with_outputs:
        (folder / "camera_LF.mp4").write_bytes(b"finished-transcode")
        (folder / "grid.mp4").write_bytes(b"finished-grid")


def test_process_skips_existing_transcode_and_grid(tmp_path, monkeypatch):
    monkeypatch.setenv("OCTACAM_CACHE_DIR", str(tmp_path / "cache"))
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


def test_process_force_rebuilds_existing_outputs(tmp_path, monkeypatch):
    monkeypatch.setenv("OCTACAM_CACHE_DIR", str(tmp_path / "cache"))
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


def test_process_transfers_skipped_outputs(tmp_path, monkeypatch):
    # A skipped transcode/grid must still flow to the transfer step, so a
    # re-run finishes the pipeline for a partially-transferred recording.
    monkeypatch.setenv("OCTACAM_CACHE_DIR", str(tmp_path / "cache"))
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
    assert (dest / "Behavior" / "camera_LF.mp4").read_bytes() == b"finished-transcode"
    assert (dest / "Renderings" / "grid.mp4").read_bytes() == b"finished-grid"


# --- process: NAS layout (root metadata / Behavior / Renderings) -----------


def test_process_transfer_splits_root_behavior_renderings(tmp_path, monkeypatch):
    monkeypatch.setenv("OCTACAM_CACHE_DIR", str(tmp_path / "cache"))
    dest_root = tmp_path / "dest"
    folder = tmp_path / "rec"
    _make_recording(
        folder,
        with_outputs=True,
        extra_toml=f'[transfer]\ndirectory = "{dest_root.as_posix()}"\n',
    )
    # Grid already exists on disk (with_outputs=True) — exercise the
    # skip-existing path (like test_process_transfers_skipped_outputs) rather
    # than --no-grid, so the pre-existing grid.mp4 still flows to Renderings/.
    monkeypatch.setattr(
        "octacam.grid.build_grid_video",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("grid should be skipped")),
    )
    result = runner.invoke(app, ["process", str(folder), "--no-transcode"])
    assert result.exit_code == 0, result.output
    dest = dest_root / folder.name
    assert (dest / "Behavior" / "camera_LF.mp4").exists()
    assert (dest / "Renderings" / "grid.mp4").exists()
    # --no-transcode's outputs come from a naive folder.glob("*.mp4"), which
    # can't tell camera_LF.mp4 from grid.mp4 sitting in the same folder — grid
    # must never also land in Behavior/ (found via a real dry-run against
    # already-transcoded production data).
    assert not (dest / "Behavior" / "grid.mp4").exists()


def test_process_no_transcode_no_grid_still_excludes_grid_from_behavior(
    tmp_path, monkeypatch
):
    # Same bug, --no-grid variant: grid_files stays empty (Phase 1 never
    # runs), so only the outputs-filter guards against misrouting grid.mp4.
    monkeypatch.setenv("OCTACAM_CACHE_DIR", str(tmp_path / "cache"))
    dest_root = tmp_path / "dest"
    folder = tmp_path / "rec"
    _make_recording(
        folder,
        with_outputs=True,
        extra_toml=f'[transfer]\ndirectory = "{dest_root.as_posix()}"\n',
    )
    result = runner.invoke(
        app, ["process", str(folder), "--no-transcode", "--no-grid"]
    )
    assert result.exit_code == 0, result.output
    dest = dest_root / folder.name
    assert (dest / "Behavior" / "camera_LF.mp4").exists()
    assert not (dest / "Behavior" / "grid.mp4").exists()
    # --no-grid means grid.mp4 isn't transferred at all this run (pre-existing
    # behavior, unrelated to this bug) — not to Renderings/ either (Renderings/
    # itself still exists: recording_summary.json always rides along).
    assert not (dest / "Renderings" / "grid.mp4").exists()
    assert (dest / "octacam_config.toml").exists()
    assert (dest / "recording_summary.json").exists()
    # The raw .mkv is never part of the NAS layout — only its archival .mp4.
    assert not (dest / "Behavior" / "camera_LF.mkv").exists()
    assert not (dest / "camera_LF.mp4").exists()  # not left flat at root either


# --- process: 2-photon transfer ---------------------------------------------


def _make_sync_folder_2p(root, experiment, name, mtime):
    folder = root / experiment / name
    folder.mkdir(parents=True)
    for fname in ("ThorRealTimeDataSettings.xml", "Episode001.h5"):
        p = folder / fname
        p.write_bytes(b"x")
        os.utime(p, (mtime, mtime))
    # discover_twophoton_folders uses the *directory's* own mtime as the sync
    # folder's start-time proxy (true in production — nothing else ever gets
    # added to a SyncData folder after its two files) — creating files above
    # bumped it to "now", so reset it to line up with the synthetic mtime.
    os.utime(folder, (mtime, mtime))
    return folder


def _armed_recording(folder, *, source_root, dest_root, take_start, duration_s=10.0):
    _make_recording(
        folder,
        with_outputs=True,
        extra_toml=(
            f'[transfer]\ndirectory = "{dest_root.as_posix()}"\n'
            f"[transfer.twophoton]\n"
            f'source = "{source_root.as_posix()}"\n'
            f"match_window_s = 60\n"
            f"settle_s = 60\n"
        ),
        extra_summary={
            "start_time_ns": int(take_start * 1e9),
            "duration_s": duration_s,
            "plugins": {"twophoton": {"armed": True}},
        },
    )


def test_process_transfers_matched_twophoton_data(tmp_path, monkeypatch):
    monkeypatch.setenv("OCTACAM_CACHE_DIR", str(tmp_path / "cache"))
    dest_root = tmp_path / "dest"
    source_root = tmp_path / "windows_share" / "MD"
    take_start = 1_000_000.0
    # 30s before the take starts (within match_window_s=60) — and, being an
    # ancient synthetic epoch, trivially "settled" relative to the real
    # wall-clock `now` is_settled compares against.
    two_p_mtime = take_start - 30
    _make_sync_folder_2p(source_root, "MB247_CI63", "SyncData102", mtime=two_p_mtime)

    folder = tmp_path / "rec"
    _armed_recording(
        folder, source_root=source_root, dest_root=dest_root, take_start=take_start
    )

    result = runner.invoke(app, ["process", str(folder), "--no-transcode", "--no-grid"])
    assert result.exit_code == 0, result.output
    dest_2p = dest_root / folder.name / "2P" / "SyncData102"
    assert (dest_2p / "Episode001.h5").exists()
    assert (dest_2p / "ThorRealTimeDataSettings.xml").exists()
    match_record = json.loads(
        (dest_root / folder.name / "twophoton_match.json").read_text()
    )
    assert match_record["matched"][0]["path"] == "MB247_CI63/SyncData102"
    # Episode001.h5 here is fake bytes (not real HDF5) — verification gracefully
    # falls back to the timestamp tier rather than crashing.
    assert match_record["matched"][0]["confidence"] == "timestamp"


def _make_verifiable_sync_folder_2p(root, experiment, name, *, mtime, camera_frames):
    """A real (synthetic) Episode001.h5 whose Cameras edge count matches
    *camera_frames* exactly, inside one CaptureOn window spanning the file."""
    h5py = pytest.importorskip("h5py")
    import numpy as np

    folder = root / experiment / name
    folder.mkdir(parents=True)
    total = camera_frames * 2 + 100
    capture_on = np.zeros((total, 1), dtype=np.uint32)
    capture_on[10 : total - 10] = 1
    cameras = np.zeros((total, 1), dtype=np.uint32)
    for i in range(camera_frames):
        cameras[20 + i * 2] = 1  # width-1 pulses, spaced by 2 -> stay distinct
    frameout = np.zeros((total, 1), dtype=np.uint32)
    episode_path = folder / "Episode001.h5"
    with h5py.File(episode_path, "w") as f:
        di = f.create_group("DI")
        di.create_dataset("CaptureOn", data=capture_on)
        di.create_dataset("Cameras", data=cameras)
        di.create_dataset("FrameOut", data=frameout)
    # is_settled checks the *file's* own mtime (see _folder_last_mtime), not
    # just the directory's — both need to line up with the synthetic mtime.
    os.utime(episode_path, (mtime, mtime))
    os.utime(folder, (mtime, mtime))
    return folder


def test_process_verifies_twophoton_match_via_real_signal(tmp_path, monkeypatch):
    monkeypatch.setenv("OCTACAM_CACHE_DIR", str(tmp_path / "cache"))
    dest_root = tmp_path / "dest"
    source_root = tmp_path / "windows_share" / "MD"
    take_start = 1_000_000.0
    # Far outside match_window_s=60 (but within the default verify_window_s)
    # — a real edge-count match must still win over the timestamp heuristic.
    _make_verifiable_sync_folder_2p(
        source_root, "MB247_CI63", "SyncData102",
        mtime=take_start + 500, camera_frames=42,
    )

    folder = tmp_path / "rec"
    _make_recording(
        folder,
        with_outputs=True,
        extra_toml=(
            f'[transfer]\ndirectory = "{dest_root.as_posix()}"\n'
            f"[transfer.twophoton]\n"
            f'source = "{source_root.as_posix()}"\n'
            f"match_window_s = 60\n"
            f"settle_s = 60\n"
        ),
        extra_summary={
            "start_time_ns": int(take_start * 1e9),
            "duration_s": 10.0,
            "plugins": {"twophoton": {"armed": True}},
            "cameras": [{"name": "camera_LF", "frames": 42}],
        },
    )
    result = runner.invoke(app, ["process", str(folder), "--no-transcode", "--no-grid"])
    assert result.exit_code == 0, result.output
    dest_2p = dest_root / folder.name / "2P" / "SyncData102"
    assert (dest_2p / "Episode001.h5").exists()
    match_record = json.loads(
        (dest_root / folder.name / "twophoton_match.json").read_text()
    )
    assert match_record["matched"][0]["confidence"] == "verified"


def test_process_skips_twophoton_when_explicitly_not_armed(tmp_path, monkeypatch):
    # schema_version 4 with the checkbox recorded unchecked must skip outright
    # — unlike a pre-feature recording (see the legacy-fallback tests below),
    # this one had the chance to say "armed: true" and didn't.
    monkeypatch.setenv("OCTACAM_CACHE_DIR", str(tmp_path / "cache"))
    dest_root = tmp_path / "dest"
    source_root = tmp_path / "windows_share" / "MD"
    take_start = 1_000_000.0
    # Overlaps cleanly — proves the skip is because of the flag, not a
    # missed time-window match.
    _make_sync_folder_2p(source_root, "MB247_CI63", "SyncData102", mtime=take_start - 30)

    folder = tmp_path / "rec"
    _make_recording(
        folder,
        with_outputs=True,
        extra_toml=(
            f'[transfer]\ndirectory = "{dest_root.as_posix()}"\n'
            f"[transfer.twophoton]\n"
            f'source = "{source_root.as_posix()}"\n'
            f"settle_s = 60\n"
        ),
        extra_summary={
            "schema_version": 4,
            "start_time_ns": int(take_start * 1e9),
            "duration_s": 10.0,
            "plugins": {"twophoton": {"armed": False}},
        },
    )
    result = runner.invoke(app, ["process", str(folder), "--no-transcode", "--no-grid"])
    assert result.exit_code == 0, result.output
    assert not (dest_root / folder.name / "2P").exists()


def test_process_legacy_fallback_matches_predates_feature_recording(tmp_path, monkeypatch):
    # A recording with no "plugins" key at all (schema_version < 4, the real
    # shape of every recording made before this feature shipped — confirmed
    # against actual production recordings) must still get matched, since
    # there was never a chance for it to record "armed".
    monkeypatch.setenv("OCTACAM_CACHE_DIR", str(tmp_path / "cache"))
    dest_root = tmp_path / "dest"
    source_root = tmp_path / "windows_share" / "MD"
    take_start = 1_000_000.0
    _make_sync_folder_2p(source_root, "MB247_CI63", "SyncData102", mtime=take_start - 30)

    folder = tmp_path / "rec"
    _make_recording(
        folder,
        with_outputs=True,
        extra_toml=(
            f'[transfer]\ndirectory = "{dest_root.as_posix()}"\n'
            f"[transfer.twophoton]\n"
            f'source = "{source_root.as_posix()}"\n'
            f"settle_s = 60\n"
        ),
        extra_summary={
            "start_time_ns": int(take_start * 1e9),
            "duration_s": 10.0,
            # No "schema_version" (defaults to 0) and no "plugins" key —
            # exactly what a real pre-feature recording_summary.json looks
            # like.
        },
    )
    result = runner.invoke(app, ["process", str(folder), "--no-transcode", "--no-grid"])
    assert result.exit_code == 0, result.output
    assert (dest_root / folder.name / "2P" / "SyncData102" / "Episode001.h5").exists()


def test_process_legacy_fallback_requires_twophoton_config(tmp_path, monkeypatch):
    # The legacy fallback is still gated on [transfer.twophoton] being
    # configured — a rig that never uses 2P at all must not suddenly start
    # scanning a share it was never told about.
    monkeypatch.setenv("OCTACAM_CACHE_DIR", str(tmp_path / "cache"))
    dest_root = tmp_path / "dest"
    folder = tmp_path / "rec"
    _make_recording(
        folder,
        with_outputs=True,
        extra_toml=f'[transfer]\ndirectory = "{dest_root.as_posix()}"\n',
        extra_summary={"start_time_ns": 1_000_000_000_000_000, "duration_s": 10.0},
    )
    result = runner.invoke(app, ["process", str(folder), "--no-transcode", "--no-grid"])
    assert result.exit_code == 0, result.output
    assert not (dest_root / folder.name / "2P").exists()


def test_process_leaves_unsettled_twophoton_folder_for_next_run(tmp_path, monkeypatch):
    monkeypatch.setenv("OCTACAM_CACHE_DIR", str(tmp_path / "cache"))
    dest_root = tmp_path / "dest"
    source_root = tmp_path / "windows_share" / "MD"
    take_start = time.time() - 5  # a few seconds ago — folder still "fresh"
    _make_sync_folder_2p(
        source_root, "MB247_CI63", "SyncData102", mtime=take_start
    )

    folder = tmp_path / "rec"
    _armed_recording(
        folder, source_root=source_root, dest_root=dest_root, take_start=take_start
    )
    # Override settle_s to something the folder hasn't cleared yet.
    (folder / "octacam_config.toml").write_text(
        (folder / "octacam_config.toml").read_text().replace(
            "settle_s = 60", "settle_s = 3600"
        )
    )
    result = runner.invoke(app, ["process", str(folder), "--no-transcode", "--no-grid"])
    assert result.exit_code == 0, result.output
    assert not (dest_root / folder.name / "2P").exists()


# --- process: --delete-after-transfer safety --------------------------------


def test_process_delete_after_transfer_removes_local_once_verified(tmp_path, monkeypatch):
    monkeypatch.setenv("OCTACAM_CACHE_DIR", str(tmp_path / "cache"))
    dest_root = tmp_path / "dest"
    folder = tmp_path / "rec"
    _make_recording(
        folder,
        with_outputs=True,
        extra_toml=f'[transfer]\ndirectory = "{dest_root.as_posix()}"\n',
    )
    result = runner.invoke(
        app,
        [
            "process", str(folder), "--no-transcode", "--no-grid",
            "--delete-after-transfer",
        ],
    )
    assert result.exit_code == 0, result.output
    assert (dest_root / folder.name / "Behavior" / "camera_LF.mp4").exists()
    assert not folder.exists()  # the whole local take dir is gone


def test_process_delete_after_transfer_skipped_on_transcode_failure(tmp_path, monkeypatch):
    # A camera whose transcode failed this run must never be deleted — its raw
    # .mkv was never archived anywhere.
    monkeypatch.setenv("OCTACAM_CACHE_DIR", str(tmp_path / "cache"))
    dest_root = tmp_path / "dest"
    folder = tmp_path / "rec"
    _make_recording(
        folder,
        with_outputs=False,
        extra_toml=f'[transfer]\ndirectory = "{dest_root.as_posix()}"\n',
    )

    def fake_transcode(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr("octacam.writer.transcode_file", fake_transcode)
    result = runner.invoke(
        app, ["process", str(folder), "--no-grid", "--delete-after-transfer"]
    )
    assert result.exit_code != 0  # transcode failure is reported
    assert folder.exists()  # never deleted


def test_process_config_default_delete_after_transfer(tmp_path, monkeypatch):
    monkeypatch.setenv("OCTACAM_CACHE_DIR", str(tmp_path / "cache"))
    dest_root = tmp_path / "dest"
    folder = tmp_path / "rec"
    _make_recording(
        folder,
        with_outputs=True,
        extra_toml=(
            f'[transfer]\ndirectory = "{dest_root.as_posix()}"\n'
            "delete_after_transfer = true\n"
        ),
    )
    result = runner.invoke(app, ["process", str(folder), "--no-transcode", "--no-grid"])
    assert result.exit_code == 0, result.output
    assert not folder.exists()


def test_process_no_delete_after_transfer_overrides_config(tmp_path, monkeypatch):
    monkeypatch.setenv("OCTACAM_CACHE_DIR", str(tmp_path / "cache"))
    dest_root = tmp_path / "dest"
    folder = tmp_path / "rec"
    _make_recording(
        folder,
        with_outputs=True,
        extra_toml=(
            f'[transfer]\ndirectory = "{dest_root.as_posix()}"\n'
            "delete_after_transfer = true\n"
        ),
    )
    result = runner.invoke(
        app,
        [
            "process", str(folder), "--no-transcode", "--no-grid",
            "--no-delete-after-transfer",
        ],
    )
    assert result.exit_code == 0, result.output
    assert folder.exists()


# --- process: --delete-source config default --------------------------------


def _fake_transcode_success(input_path, output, **kwargs):
    Path(output).write_bytes(b"finished-transcode")
    return str(output)


def test_process_config_default_delete_source(tmp_path, monkeypatch):
    monkeypatch.setenv("OCTACAM_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setattr("octacam.writer.transcode_file", _fake_transcode_success)
    folder = tmp_path / "rec"
    _make_recording(
        folder, with_outputs=False, extra_toml="[transcode]\ndelete_source = true\n"
    )
    result = runner.invoke(app, ["process", str(folder), "--no-grid", "--no-transfer"])
    assert result.exit_code == 0, result.output
    assert not (folder / "camera_LF.mkv").exists()


def test_process_no_delete_source_overrides_config(tmp_path, monkeypatch):
    monkeypatch.setenv("OCTACAM_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setattr("octacam.writer.transcode_file", _fake_transcode_success)
    folder = tmp_path / "rec"
    _make_recording(
        folder, with_outputs=False, extra_toml="[transcode]\ndelete_source = true\n"
    )
    result = runner.invoke(
        app,
        ["process", str(folder), "--no-grid", "--no-transfer", "--no-delete-source"],
    )
    assert result.exit_code == 0, result.output
    assert (folder / "camera_LF.mkv").exists()


# --- process: --twophoton-sweep (2P-only, no matching behavior take) --------


def test_twophoton_sweep_requires_config(tmp_path, monkeypatch):
    monkeypatch.setenv("OCTACAM_CACHE_DIR", str(tmp_path / "cache"))
    result = runner.invoke(app, ["process", "--twophoton-sweep"])
    assert result.exit_code != 0
    assert "--config" in result.output


def test_twophoton_sweep_requires_twophoton_config(tmp_path, monkeypatch):
    monkeypatch.setenv("OCTACAM_CACHE_DIR", str(tmp_path / "cache"))
    config_dir = tmp_path / "rig"
    config_dir.mkdir()
    (config_dir / "octacam_config.toml").write_text(
        f'[transfer]\ndirectory = "{(tmp_path / "dest").as_posix()}"\n'
    )
    result = runner.invoke(
        app, ["process", "--twophoton-sweep", "--config", str(config_dir)]
    )
    assert result.exit_code != 0
    assert "transfer.twophoton" in result.output


def test_twophoton_sweep_transfers_unmatched_folder(tmp_path, monkeypatch):
    monkeypatch.setenv("OCTACAM_CACHE_DIR", str(tmp_path / "cache"))
    dest_root = tmp_path / "dest"
    source_root = tmp_path / "windows_share" / "MD"
    old_enough = time.time() - 10_000
    _make_sync_folder_2p(source_root, "Tests", "SyncData001", mtime=old_enough)

    config_dir = tmp_path / "rig"
    config_dir.mkdir()
    (config_dir / "octacam_config.toml").write_text(
        f'[transfer]\ndirectory = "{dest_root.as_posix()}"\n'
        f"[transfer.twophoton]\n"
        f'source = "{source_root.as_posix()}"\n'
        f"settle_s = 60\n"
    )
    result = runner.invoke(
        app, ["process", "--twophoton-sweep", "--config", str(config_dir)]
    )
    assert result.exit_code == 0, result.output
    dest = dest_root / "2p_only" / "Tests"
    (date_dir,) = list(dest.iterdir())  # exactly one date-named subfolder
    assert (date_dir / "SyncData001" / "Episode001.h5").exists()


def test_twophoton_sweep_skips_folder_still_settling(tmp_path, monkeypatch):
    monkeypatch.setenv("OCTACAM_CACHE_DIR", str(tmp_path / "cache"))
    dest_root = tmp_path / "dest"
    source_root = tmp_path / "windows_share" / "MD"
    _make_sync_folder_2p(source_root, "Tests", "SyncData001", mtime=time.time() - 5)

    config_dir = tmp_path / "rig"
    config_dir.mkdir()
    (config_dir / "octacam_config.toml").write_text(
        f'[transfer]\ndirectory = "{dest_root.as_posix()}"\n'
        f"[transfer.twophoton]\n"
        f'source = "{source_root.as_posix()}"\n'
        f"settle_s = 3600\n"
    )
    result = runner.invoke(
        app, ["process", "--twophoton-sweep", "--config", str(config_dir)]
    )
    assert result.exit_code == 0, result.output
    assert not (dest_root / "2p_only").exists()


def test_twophoton_sweep_excludes_already_matched_folder(tmp_path, monkeypatch):
    # A folder already paired with a behavior take (twophoton_match.json
    # sidecar in a still-cached recording folder) must not be re-copied as
    # if it were 2P-only.
    monkeypatch.setenv("OCTACAM_CACHE_DIR", str(tmp_path / "cache"))
    dest_root = tmp_path / "dest"
    source_root = tmp_path / "windows_share" / "MD"
    old_enough = time.time() - 10_000
    _make_sync_folder_2p(source_root, "MB247_CI63", "SyncData102", mtime=old_enough)

    from octacam import session_cache
    from octacam.transform import TWOPHOTON_MATCH_FILENAME

    matched_recording = tmp_path / "already_processed_take"
    matched_recording.mkdir()
    (matched_recording / TWOPHOTON_MATCH_FILENAME).write_text(
        json.dumps({"matched": [{"path": "MB247_CI63/SyncData102", "kind": "sync"}]})
    )
    session_cache.record_recording(matched_recording, session_id="s1")

    config_dir = tmp_path / "rig"
    config_dir.mkdir()
    (config_dir / "octacam_config.toml").write_text(
        f'[transfer]\ndirectory = "{dest_root.as_posix()}"\n'
        f"[transfer.twophoton]\n"
        f'source = "{source_root.as_posix()}"\n'
        f"settle_s = 60\n"
    )
    result = runner.invoke(
        app, ["process", "--twophoton-sweep", "--config", str(config_dir)]
    )
    assert result.exit_code == 0, result.output
    assert not (dest_root / "2p_only").exists()


# --- process: automatic post-processing sweep (sequential, not separate) ----


def test_process_automatically_sweeps_unmatched_twophoton(tmp_path, monkeypatch):
    # A normal `octacam process` run (no --twophoton-sweep) must also sweep
    # unclaimed 2P folders under the same [transfer.twophoton].source it just
    # used for per-take matching — sequentially, in one command, not a
    # separate manual invocation.
    monkeypatch.setenv("OCTACAM_CACHE_DIR", str(tmp_path / "cache"))
    dest_root = tmp_path / "dest"
    source_root = tmp_path / "windows_share" / "MD"
    take_start = 1_000_000.0
    two_p_mtime = take_start - 30
    _make_sync_folder_2p(source_root, "MB247_CI63", "SyncData102", mtime=two_p_mtime)
    # A second, unrelated, unclaimed folder sitting on the same share.
    old_enough = time.time() - 10_000
    _make_sync_folder_2p(source_root, "Tests", "SyncData999", mtime=old_enough)

    folder = tmp_path / "rec"
    _armed_recording(
        folder, source_root=source_root, dest_root=dest_root, take_start=take_start
    )

    result = runner.invoke(app, ["process", str(folder), "--no-transcode", "--no-grid"])
    assert result.exit_code == 0, result.output
    # matched 2P landed alongside the take, as always.
    assert (dest_root / folder.name / "2P" / "SyncData102" / "Episode001.h5").exists()
    # AND the unrelated, unclaimed folder was swept automatically too.
    sweep_dest = dest_root / "2p_only" / "Tests"
    (date_dir,) = list(sweep_dest.iterdir())
    assert (date_dir / "SyncData999" / "Episode001.h5").exists()


def test_process_no_twophoton_sweep_skips_automatic_sweep(tmp_path, monkeypatch):
    monkeypatch.setenv("OCTACAM_CACHE_DIR", str(tmp_path / "cache"))
    dest_root = tmp_path / "dest"
    source_root = tmp_path / "windows_share" / "MD"
    take_start = 1_000_000.0
    two_p_mtime = take_start - 30
    _make_sync_folder_2p(source_root, "MB247_CI63", "SyncData102", mtime=two_p_mtime)
    old_enough = time.time() - 10_000
    _make_sync_folder_2p(source_root, "Tests", "SyncData999", mtime=old_enough)

    folder = tmp_path / "rec"
    _armed_recording(
        folder, source_root=source_root, dest_root=dest_root, take_start=take_start
    )
    result = runner.invoke(
        app,
        ["process", str(folder), "--no-transcode", "--no-grid", "--no-twophoton-sweep"],
    )
    assert result.exit_code == 0, result.output
    assert (dest_root / folder.name / "2P" / "SyncData102" / "Episode001.h5").exists()
    assert not (dest_root / "2p_only").exists()


def test_process_automatic_sweep_dry_run_touches_nothing(tmp_path, monkeypatch):
    monkeypatch.setenv("OCTACAM_CACHE_DIR", str(tmp_path / "cache"))
    dest_root = tmp_path / "dest"
    source_root = tmp_path / "windows_share" / "MD"
    take_start = 1_000_000.0
    two_p_mtime = take_start - 30
    _make_sync_folder_2p(source_root, "MB247_CI63", "SyncData102", mtime=two_p_mtime)
    old_enough = time.time() - 10_000
    _make_sync_folder_2p(source_root, "Tests", "SyncData999", mtime=old_enough)

    folder = tmp_path / "rec"
    _armed_recording(
        folder, source_root=source_root, dest_root=dest_root, take_start=take_start
    )
    result = runner.invoke(
        app, ["process", str(folder), "--no-transcode", "--no-grid", "--dry-run"]
    )
    assert result.exit_code == 0, result.output
    assert not (dest_root / "2p_only").exists()
    assert "[dry-run] 2P sweep:" in result.output


def test_sweep_also_exclude_covers_a_same_run_match_not_yet_on_disk(tmp_path, monkeypatch):
    # Real bug: dry-run never writes twophoton_match.json to disk, so without
    # this parameter the sweep would (wrongly) also list a folder Phase 3
    # just matched to a take moments earlier in the very same dry-run.
    monkeypatch.setenv("OCTACAM_CACHE_DIR", str(tmp_path / "cache"))
    from octacam.cli import _sweep_unclaimed_twophoton

    source_root = tmp_path / "windows_share" / "MD"
    twophoton_cfg = SimpleNamespace(settle_s=60.0)

    # Without also_exclude: a real sweep copies the folder.
    matched_a = _make_sync_folder_2p(
        source_root, "expA", "SyncData102", mtime=time.time() - 10_000
    )
    dest_root_a = tmp_path / "dest_a"
    n_copied, n_failed = _sweep_unclaimed_twophoton(
        source_root, dest_root_a, twophoton_cfg, checksum=True, dry_run=False
    )
    assert n_failed == 0
    assert n_copied > 0
    assert (dest_root_a / "2p_only").exists()

    # With also_exclude naming the (otherwise identical) folder — as Phase 3
    # would for a same-run match not yet persisted to disk — nothing is
    # copied to a fresh destination.
    matched_b = _make_sync_folder_2p(
        source_root, "expB", "SyncData102", mtime=time.time() - 10_000
    )
    dest_root_b = tmp_path / "dest_b"
    n_copied, n_failed = _sweep_unclaimed_twophoton(
        source_root,
        dest_root_b,
        twophoton_cfg,
        checksum=True,
        dry_run=False,
        also_exclude=frozenset({matched_a.resolve(), matched_b.resolve()}),
    )
    assert (n_copied, n_failed) == (0, 0)
    assert not (dest_root_b / "2p_only").exists()


# --- process: --migrate-layout (old flat NAS layout -> Behavior/Renderings) -


def _make_flat_transferred_recording(dest_root, rel, camera_names):
    """A destination folder shaped like the pre-Behavior/Renderings layout:
    every camera_*.mp4 + grid.mp4 flat, no subfolders."""
    folder = dest_root / rel
    folder.mkdir(parents=True)
    for name in camera_names:
        (folder / f"{name}.mp4").write_bytes(f"{name}-bytes".encode())
    (folder / "grid.mp4").write_bytes(b"grid-bytes")
    (folder / "recording_summary.json").write_text(
        json.dumps({"cameras": [{"name": n} for n in camera_names]})
    )
    return folder


def test_migrate_layout_requires_config(tmp_path, monkeypatch):
    monkeypatch.setenv("OCTACAM_CACHE_DIR", str(tmp_path / "cache"))
    result = runner.invoke(app, ["process", "--migrate-layout"])
    assert result.exit_code != 0
    assert "--config" in result.output


def test_migrate_layout_moves_flat_files_in_place(tmp_path, monkeypatch):
    monkeypatch.setenv("OCTACAM_CACHE_DIR", str(tmp_path / "cache"))
    dest_root = tmp_path / "dest"
    folder = _make_flat_transferred_recording(
        dest_root, "260903_PAM7xCI63/Fly1/002", ["camera_LF", "camera_RF"]
    )
    flat_bytes = {p.name: p.read_bytes() for p in folder.glob("*.mp4")}

    config_dir = tmp_path / "rig"
    config_dir.mkdir()
    (config_dir / "octacam_config.toml").write_text(
        f'[transfer]\ndirectory = "{dest_root.as_posix()}"\n'
    )
    result = runner.invoke(
        app, ["process", "--migrate-layout", "--config", str(config_dir)]
    )
    assert result.exit_code == 0, result.output

    assert (folder / "Behavior" / "camera_LF.mp4").read_bytes() == flat_bytes["camera_LF.mp4"]
    assert (folder / "Behavior" / "camera_RF.mp4").read_bytes() == flat_bytes["camera_RF.mp4"]
    assert (folder / "Renderings" / "grid.mp4").read_bytes() == flat_bytes["grid.mp4"]
    # Flat originals are gone (moved, not copied) — recording_summary.json
    # stays put at the root, same path both layouts expect.
    assert not (folder / "camera_LF.mp4").exists()
    assert not (folder / "camera_RF.mp4").exists()
    assert not (folder / "grid.mp4").exists()
    assert (folder / "recording_summary.json").exists()


def test_migrate_layout_dry_run_touches_nothing(tmp_path, monkeypatch):
    monkeypatch.setenv("OCTACAM_CACHE_DIR", str(tmp_path / "cache"))
    dest_root = tmp_path / "dest"
    folder = _make_flat_transferred_recording(
        dest_root, "260903_PAM7xCI63/Fly1/002", ["camera_LF"]
    )
    config_dir = tmp_path / "rig"
    config_dir.mkdir()
    (config_dir / "octacam_config.toml").write_text(
        f'[transfer]\ndirectory = "{dest_root.as_posix()}"\n'
    )
    result = runner.invoke(
        app,
        ["process", "--migrate-layout", "--config", str(config_dir), "--dry-run"],
    )
    assert result.exit_code == 0, result.output
    assert (folder / "camera_LF.mp4").exists()  # untouched
    assert not (folder / "Behavior").exists()


def test_migrate_layout_is_idempotent(tmp_path, monkeypatch):
    monkeypatch.setenv("OCTACAM_CACHE_DIR", str(tmp_path / "cache"))
    dest_root = tmp_path / "dest"
    _make_flat_transferred_recording(
        dest_root, "260903_PAM7xCI63/Fly1/002", ["camera_LF"]
    )
    config_dir = tmp_path / "rig"
    config_dir.mkdir()
    (config_dir / "octacam_config.toml").write_text(
        f'[transfer]\ndirectory = "{dest_root.as_posix()}"\n'
    )
    args = ["process", "--migrate-layout", "--config", str(config_dir)]
    assert runner.invoke(app, args).exit_code == 0
    result = runner.invoke(app, args)  # nothing flat left — must be a no-op
    assert result.exit_code == 0, result.output


def test_migrate_layout_conflict_leaves_both_and_reports(tmp_path, monkeypatch):
    monkeypatch.setenv("OCTACAM_CACHE_DIR", str(tmp_path / "cache"))
    dest_root = tmp_path / "dest"
    folder = _make_flat_transferred_recording(
        dest_root, "260903_PAM7xCI63/Fly1/002", ["camera_LF"]
    )
    (folder / "Behavior").mkdir()
    (folder / "Behavior" / "camera_LF.mp4").write_bytes(b"different-content-here")

    config_dir = tmp_path / "rig"
    config_dir.mkdir()
    (config_dir / "octacam_config.toml").write_text(
        f'[transfer]\ndirectory = "{dest_root.as_posix()}"\n'
    )
    result = runner.invoke(
        app, ["process", "--migrate-layout", "--config", str(config_dir)]
    )
    assert result.exit_code != 0  # reported, not silently swallowed
    # Neither copy was touched.
    assert (folder / "camera_LF.mp4").exists()
    assert (folder / "Behavior" / "camera_LF.mp4").read_bytes() == b"different-content-here"


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
    monkeypatch.setattr("octacam.cli._available_backends", lambda: ["basler", "flir"])
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

    monkeypatch.setattr("octacam.cli._available_backends", lambda: ["basler", "flir"])
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
