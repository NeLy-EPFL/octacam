import contextlib
import fcntl
import hashlib
import json
import logging
import os
import resource
import shlex
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import webbrowser
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Annotated

if TYPE_CHECKING:
    from rich.progress import TaskID

    from octacam.config import RecordConfig
    from octacam.controller import RecordingSettings
    from octacam.writer import ProgressCallback

import typer

import octacam

log = logging.getLogger("octacam")


class LogLevel(StrEnum):
    debug = "debug"
    info = "info"
    warning = "warning"
    error = "error"


class ProgressStyle(StrEnum):
    octacam = "octacam"
    ffmpeg = "ffmpeg"


_stderr_console_singleton = None


def _stderr_console():
    """The single rich Console octacam draws on stderr (logs + progress bars).

    Sharing one Console lets a live progress bar and the logger coordinate, so
    log lines render cleanly above the bar instead of corrupting it."""
    global _stderr_console_singleton
    if _stderr_console_singleton is None:
        from rich.console import Console

        _stderr_console_singleton = Console(stderr=True)
    return _stderr_console_singleton


def _setup_logging(level: LogLevel) -> None:
    """Route the "octacam" logger through rich (colored level, pretty tracebacks).

    Logs go to stderr so stdout stays clean for the machine-readable output of
    `record`/`doctor --json`."""
    from rich.logging import RichHandler

    handler = RichHandler(
        console=_stderr_console(),
        show_time=False,
        show_path=False,
        markup=False,
        rich_tracebacks=True,
    )
    logger = logging.getLogger("octacam")
    logger.handlers.clear()
    logger.addHandler(handler)
    logger.setLevel(getattr(logging, level.value.upper()))
    logger.propagate = False


def _raise_fd_limit() -> None:
    """Raise the soft open-file limit to the hard limit.

    pylon's USB stack uses ~150 file descriptors per streaming camera
    (one eventfd per queued URB), so 8 cameras exceed the common 1024
    soft limit and StartGrabbing fails with "Insufficient system
    resources exist to complete the API".
    """
    soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    if soft < hard:
        resource.setrlimit(resource.RLIMIT_NOFILE, (hard, hard))
        log.debug("Raised open file limit: %d -> %d", soft, hard)


def _port_available(host: str, port: int) -> bool:
    """Return False if a server is already bound to ``host:port``.

    A second `octacam gui` on a rig would otherwise spend seconds opening
    cameras only to die when uvicorn cannot bind; probing first lets it fail
    instantly with a useful message. SO_REUSEADDR mirrors uvicorn so a socket
    lingering in TIME_WAIT (which uvicorn could still rebind) is not misreported
    as in use, while an actively listening server still fails to bind and is
    reported correctly. There is a tiny race between this probe and uvicorn's
    own bind; losing it just falls back to uvicorn's own bind error.
    """
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    with socket.socket(family, socket.SOCK_STREAM) as probe:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            probe.bind((host, port))
        except OSError:
            return False
    return True


class _NoLock:
    """Placeholder lock for when the lock file itself cannot be opened (e.g. an
    unwritable temp dir): the single-instance guard is skipped and the exclusive
    camera open is the only backstop. close() no-ops so the caller need not
    special-case it."""

    def close(self) -> None:
        pass


_LOCK_UNAVAILABLE = _NoLock()


def _instance_lock_path(config_dir: Path) -> Path:
    """Stable per-config-dir lock path under the system temp directory."""
    key = hashlib.sha1(str(config_dir).encode()).hexdigest()[:16]
    return Path(tempfile.gettempdir()) / f"octacam-{key}.lock"


def _acquire_instance_lock(config_dir: Path):
    """Take an exclusive, auto-releasing lock so one octacam owns this rig.

    A second `octacam gui <config_dir>` — on any ``--port`` — must not fight the
    running one over the cameras. We flock a per-config-dir file: the lock is
    held for the life of the process and released by the OS on exit, even on a
    crash or SIGKILL, so there is never a stale lock to clear. Keying on the
    config dir (not the port) means re-running the same rig is refused while two
    genuinely different configs may still run side by side.

    Returns the locked file handle on success (keep it referenced for the whole
    run), ``None`` if another instance already holds it (the caller should
    exit), or :data:`_LOCK_UNAVAILABLE` if the lock file could not be opened.
    """
    path = _instance_lock_path(config_dir)
    try:
        handle = open(path, "a+")
    except OSError as e:
        log.debug("Instance lock %s unavailable (%s); relying on camera lock", path, e)
        return _LOCK_UNAVAILABLE
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        return None
    # Record our PID for anyone inspecting the file (purely informational).
    handle.seek(0)
    handle.truncate()
    handle.write(f"{os.getpid()}\n")
    handle.flush()
    return handle


app = typer.Typer(
    add_completion=False,
    no_args_is_help=False,
    # "rich" gives the coloured help panels. It also treats `[...]` in help text
    # as markup, so literal TOML section names below are escaped as `\[record]`.
    rich_markup_mode="rich",
    # Accept `-h` alongside `--help` on the root and every subcommand.
    context_settings={"help_option_names": ["-h", "--help"]},
)


def _version_callback(value: bool) -> None:
    if value:
        typer.echo(octacam.__version__)
        raise typer.Exit()


# Plugins are opt-in; the default launch loads none. Enable them per-rig in the
# config's `plugins` section, or per-launch with these options.
EnabledPlugins = Annotated[
    list[str] | None,
    typer.Option(
        "--plugin",
        help="Enable a plugin (repeatable); adds to the config's `plugins` "
        "(e.g. --plugin flywheel). See `octacam doctor`.",
    ),
]
NoPlugins = Annotated[
    bool,
    typer.Option(
        "--no-plugins",
        help="Disable all plugins for this launch, ignoring the config.",
    ),
]


def _resolve_enabled(enabled_plugins, no_plugins):
    """Map the CLI flags to build_plugins' `enabled` argument.

    None = no override (use the config), [] = --no-plugins, or the list of
    --plugin names to add to the config selection.
    """
    if no_plugins:
        return []
    return list(enabled_plugins) if enabled_plugins else None


def _settings_from_record(record, transcode, transfer) -> "RecordingSettings":
    """Build RecordingSettings from the config's record/transcode/transfer sections.

    Resolves the templated save directory at *this* moment (a single ``when``
    snapshot so directory and relative_directory share one date) and translates
    the config's ``save_transformed``/``save_timestamps`` booleans to the
    internal ``record_form``/``save_frame_timestamps`` vocabulary. The
    ``[transcode]``/``[transfer]`` values seed the GUI's Process fields, which
    are baked into each recording's config snapshot for ``octacam process``
    (``transfer`` is ``None`` when the rig has no ``[transfer]`` section)."""
    import time as _time

    from octacam.config import (
        duration_to_seconds,
        resolve_record_directory,
        resolve_relative_directory,
        resolve_save_dir,
    )
    from octacam.controller import RecordingSettings

    when = _time.localtime()
    return RecordingSettings(
        fps=record.fps,
        duration_s=duration_to_seconds(
            record.duration, record.duration_unit, record.fps
        ),
        save_dir=resolve_save_dir(record, when),
        record_directory=resolve_record_directory(record, when),
        relative_directory=resolve_relative_directory(record, when),
        trigger_source=record.trigger_source,
        save_method=record.save_method,
        ffmpeg_params=record.ffmpeg_params,
        record_form="display" if record.save_transformed else "sensor",
        save_frame_timestamps=record.save_timestamps,
        transcode_ffmpeg_params=transcode.ffmpeg_params,
        transfer_directory=transfer.directory if transfer else "",
        transfer_checksum=transfer.checksum if transfer else True,
    )


def _in_ssh_session() -> bool:
    """True when this shell was started over SSH (sshd exports these)."""
    return any(
        os.environ.get(var) for var in ("SSH_CONNECTION", "SSH_CLIENT", "SSH_TTY")
    )


def _browser_skip_reason(no_browser: bool) -> str | None:
    """Why auto-opening the browser should be skipped, or None to open it.

    A browser is only useful when octacam runs on the machine the user is
    sitting at. Over SSH the browser would launch on the rig rather than the
    user's laptop, so we skip it and let them reach the GUI through the tunnel.
    The headless check covers SSH setups that strip the SSH_* variables.
    """
    if no_browser:
        return "--no-browser was passed"
    if _in_ssh_session():
        return "running over SSH — open the GUI on your local machine instead"
    if sys.platform.startswith("linux") and not (
        os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")
    ):
        return "no local display detected (headless session)"
    return None


def _launch_browser(url: str) -> bool:
    """Open url in the default browser; return True if a launcher started.

    On Linux/macOS we prefer the desktop's own opener (xdg-open / open): it
    honours the user's default-browser setting and is more reliable than the
    stdlib's browser hunt, which on Linux can "succeed" by spawning some other
    browser that never actually shows a window. An explicit $BROWSER preference
    and every other platform go through webbrowser, which also covers Windows.
    """

    def _via_webbrowser() -> bool:
        try:
            return webbrowser.open(url)
        except webbrowser.Error as e:
            log.debug("webbrowser.open failed: %s", e)
            return False

    # A user who set $BROWSER asked for that specific browser; webbrowser honours it.
    if os.environ.get("BROWSER") and _via_webbrowser():
        return True

    opener = None
    if sys.platform.startswith("linux"):
        opener = "xdg-open"
    elif sys.platform == "darwin":
        opener = "open"
    if opener and shutil.which(opener):
        try:
            subprocess.Popen(
                [opener, url],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            return True
        except OSError as e:
            log.debug("%s failed: %s", opener, e)

    return _via_webbrowser()


def _open_browser_when_ready(url: str, host: str, port: int) -> None:
    """Wait for the server to accept connections, then open the default browser.

    Runs in a daemon thread so the blocking uvicorn.run() can start the server
    while we poll the port; opening before the server is up shows an error page.
    """
    connect_host = "127.0.0.1" if host in ("0.0.0.0", "::") else host
    deadline = time.monotonic() + 10.0
    try:
        while time.monotonic() < deadline:
            try:
                with socket.create_connection((connect_host, port), timeout=0.5):
                    break
            except OSError:
                time.sleep(0.1)
        else:
            log.warning("octacam GUI never became reachable — open %s manually.", url)
            return
        if not _launch_browser(url):
            log.warning(
                "Couldn't open a browser automatically — open %s manually.", url
            )
    except Exception:
        # A helper thread must never die silently; tell the user what to do.
        log.warning("Failed to open a browser — open %s manually.", url, exc_info=True)


def _print_transcode_hints(session_id: str) -> None:
    """On GUI shutdown, print the `process` commands for what was just recorded.

    Stays silent when the session recorded nothing (it only previewed).
    """
    from octacam import session_cache

    try:
        folders = session_cache.session_folders(session_id)
    except Exception:
        log.debug("Could not read the recording cache for process hints", exc_info=True)
        return
    if not folders:
        return
    log.info(
        "Recorded %d folder(s) this session. Transcode, grid, and transfer them with:\n"
        "  last session:  octacam process --session\n"
        "  all sessions:  octacam process --all",
        len(folders),
    )


def _warn_if_transcoding() -> None:
    """Warn when an `octacam process` is transcoding elsewhere on this machine.

    Transcoding runs slow x264 presets across many files and saturates the CPU,
    so it competes with live capture/encoding and can cause dropped frames. The
    operator should know before starting a GUI session or a headless recording.
    Best-effort: any failure to check is silently ignored.
    """
    from octacam import session_cache

    try:
        count = session_cache.transcode_running()
    except Exception:
        log.debug("Could not check for running transcodes", exc_info=True)
        return
    if count:
        log.warning(
            "%d octacam process run%s transcoding on this machine — transcoding "
            "is CPU-heavy and may slow capture/encoding (risking dropped frames). "
            "Consider waiting for it to finish.",
            count,
            " is" if count == 1 else "s are",
        )


@app.callback(invoke_without_command=True)
def main_callback(
    ctx: typer.Context,
    log_level: Annotated[
        LogLevel,
        typer.Option("--log-level", "-l", help="Logging verbosity."),
    ] = LogLevel.info,
    version: Annotated[
        bool,
        typer.Option(
            "--version",
            callback=_version_callback,
            is_eager=True,
            help="Show the version and exit.",
        ),
    ] = False,
) -> None:
    """octacam: preview, record, and save video streams from multiple Basler cameras.

    Run `octacam gui <config_dir>` for the web GUI, or see the commands below.
    """
    _setup_logging(log_level)
    _raise_fd_limit()
    if ctx.invoked_subcommand is None:
        typer.echo(ctx.get_help())
        raise typer.Exit()


@app.command()
def gui(
    config_dir: Annotated[
        Path,
        typer.Argument(exists=True, file_okay=False, dir_okay=True),
    ] = Path("."),
    host: Annotated[
        str,
        typer.Option(
            help="Bind address. Keep the loopback default and reach the GUI "
            "remotely with: ssh -L 8765:127.0.0.1:8765 <rig-hostname>"
        ),
    ] = "127.0.0.1",
    port: Annotated[
        int,
        typer.Option(help="Port to bind; override if it clashes with other software."),
    ] = 8765,
    no_browser: Annotated[
        bool,
        typer.Option(
            "--no-browser",
            help="Don't open the web GUI in a browser automatically. Auto-open "
            "is also skipped over SSH and on headless sessions.",
        ),
    ] = False,
    enabled_plugins: EnabledPlugins = None,
    no_plugins: NoPlugins = False,
) -> None:
    """Launch the octacam web GUI for the cameras in CONFIG_DIR."""
    import uvicorn

    from octacam import session_cache
    from octacam.cameras import BackendError, BackendUnavailable, CameraSystem
    from octacam.config import load_config_dir
    from octacam.controller import RecordingController
    from octacam.plugins import build_plugins
    from octacam.web.app import create_app

    # Reachable loopback address for the browser / hint messages (binding to
    # 0.0.0.0 or :: is not connectable, so point at localhost in that case).
    browser_host = "127.0.0.1" if host in ("0.0.0.0", "::") else host

    config_dir = config_dir.resolve()
    log.info("Using config directory: %s", config_dir)

    # One octacam process must own a rig's cameras at a time. The guard is keyed
    # on the config dir rather than the port (which --port can change), so
    # re-running `octacam gui <config>` is refused even on a different port.
    instance_lock = _acquire_instance_lock(config_dir)
    if instance_lock is None:
        sys.exit(
            f"Another octacam instance is already running for this config "
            f"({config_dir}). Open its GUI in a browser, or stop it first."
        )

    # Separately, fail early if the chosen port is taken (by other software, or
    # by an octacam serving a different config) instead of letting uvicorn's
    # bind fail later with an opaque traceback.
    if not _port_available(host, port):
        sys.exit(
            f"Port {port} is already in use on {host}. "
            f"Choose a free one with --port (e.g. --port {port + 1})."
        )

    config = load_config_dir(config_dir)

    # A transcode running on this machine will fight live capture for the CPU.
    _warn_if_transcoding()

    try:
        system = CameraSystem(
            [c.serial_number for c in config.cameras], backend=config.backend
        )
    except BackendUnavailable as e:
        sys.exit(str(e))
    except BackendError as e:
        # The cameras could not be opened — most often because another octacam
        # already holds them (vendor SDKs open USB3 devices exclusively), but
        # also a disconnected camera. Either way, a clean message beats a raw
        # pylon/PySpin traceback.
        sys.exit(
            f"Could not open the cameras: {e}\n"
            "They may already be in use by another octacam instance on this "
            "rig, or disconnected — only one process can open them at a time."
        )
    if len(system) == 0:
        log.warning("No cameras opened. Exiting.")
        sys.exit(1)
    log.info("Opened %d camera(s)", len(system))

    names = {c.serial_number: c.name for c in config.cameras if c.name}
    for camera in system:
        camera.name = names.get(camera.serial_number, camera.name)
    system.load_config(config_dir)
    system.apply_display_config(config.cameras)

    plugins = build_plugins(config, _resolve_enabled(enabled_plugins, no_plugins))
    plugins.setup_all()

    settings = _settings_from_record(config.record, config.transcode, config.transfer)
    # One session id for this GUI run; every recording made before shutdown is
    # tagged with it in the session cache so `octacam process --session` can
    # find the whole batch later (and we print the commands on the way out).
    session_id = session_cache.new_session_id()
    controller = RecordingController(
        system, settings, plugins, session_id=session_id, config_dir=config_dir
    )
    controller.start_preview()
    app = create_app(controller, config, plugins, config_dir=str(config_dir))
    log.info(
        "octacam web GUI on http://%s:%d/ (remote: ssh -L %d:127.0.0.1:%d <rig-hostname>)",
        host,
        port,
        port,
        port,
    )
    browser_url = f"http://{browser_host}:{port}/"
    skip = _browser_skip_reason(no_browser)
    if skip:
        log.info("Not opening a browser automatically: %s.", skip)
    else:
        log.info("Opening the web GUI in your default browser…")
        threading.Thread(
            target=_open_browser_when_ready,
            args=(browser_url, host, port),
            daemon=True,
        ).start()
    try:
        # ws_ping_timeout: uvicorn's websocket keepalive pings the browser and
        # drops the socket if no pong returns within this window. That same
        # socket also carries the preview stream (up to ~8 cameras worth of JPEG
        # frames at the display refresh rate), so over a slow `ssh -L` tunnel the
        # link can stay congested long enough that the default 20s pong wait
        # elapses and a perfectly live GUI is killed with a 1011 "keepalive ping
        # timeout". Give the pong a generous window; the ping still runs at the
        # default interval, so a genuinely dead (half-open) client is reaped and
        # its preview encoding stops.
        uvicorn.run(
            app,
            host=host,
            port=port,
            log_level="warning",
            ws_ping_timeout=60.0,
        )
    finally:
        # Runs on Ctrl+C, on the /api/shutdown self-signal, and on errors.
        # Cleanup can take a moment (finalizing recordings, draining ffmpeg,
        # closing cameras), so bracket it with messages.
        log.info("Shutting down — finalizing recordings and releasing cameras…")
        controller.close()
        plugins.teardown_all()
        # Release the single-instance lock so a relaunch is not briefly blocked
        # while this process lingers; the OS would also drop it on exit.
        instance_lock.close()
        # If anything was recorded this session, print the ready-to-run transcode
        # commands for it.
        _print_transcode_hints(session_id)
        log.info("octacam stopped.")


# ---------------------------------------------------------------------------
# `octacam doctor` — environment + rig diagnostics
#
# Lists detected cameras and bundled plugins and adds pass/warn/fail checks for
# the toolchain, storage, recording cache, and runtime conflicts. It never
# *opens* a camera (vendor SDKs open USB3 devices exclusively), so it is safe to
# run while a GUI/record session is live: it only enumerates and reads locks.
# ---------------------------------------------------------------------------

# status -> (marker, rich style). "list" is a plain indented enumeration line.
_MARKERS = {
    "ok": ("✓", "green"),
    "warn": ("⚠", "yellow"),
    "error": ("✗", "red"),
    "info": ("•", "cyan"),
    "list": ("", ""),
}


class _Report:
    """Accumulates doctor findings as ordered sections of (status, text) lines."""

    def __init__(self) -> None:
        self.sections: list[tuple[str, list[tuple[str, str]]]] = []

    def section(self, title: str) -> None:
        self.sections.append((title, []))

    def add(self, status: str, text: str) -> None:
        self.sections[-1][1].append((status, text))

    def counts(self) -> tuple[int, int]:
        """(errors, warnings) across every section, for the exit code."""
        errors = warns = 0
        for _title, items in self.sections:
            for status, _text in items:
                errors += status == "error"
                warns += status == "warn"
        return errors, warns


def _enumerate_backend(name: str) -> list[tuple[str, str | None]]:
    """``[(serial, model|None), ...]`` for a backend without opening any camera.

    Basler goes through the pylon TL factory directly so model names come along;
    other backends expose only serials via their
    enumeration function. Enumeration never opens/grabs a device, so this is safe
    to run alongside a live session."""
    key = (name or "basler").strip().lower()
    if key == "basler":
        from pypylon import pylon

        devices = pylon.TlFactory.GetInstance().EnumerateDevices()
        return [(str(d.GetSerialNumber()), str(d.GetModelName())) for d in devices]
    from octacam.cameras import select_backend

    enumerate_fn, _factory, _extension = select_backend(name)
    return [(str(serial), None) for serial, _handle in enumerate_fn(None)]


def _run_ffmpeg_probe(exe: str, args: list[str]) -> str:
    """Run a fast, read-only ffmpeg query and return its combined output ("" on error)."""
    try:
        out = subprocess.run([exe, *args], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return ""
    return (out.stdout or "") + (out.stderr or "")


def _ffmpeg_version(exe: str) -> str:
    """The version token from ``ffmpeg -version`` (e.g. "7.0.2"), or ""."""
    for line in _run_ffmpeg_probe(exe, ["-hide_banner", "-version"]).splitlines():
        line = line.strip()
        if line.startswith("ffmpeg version"):
            toks = line.split()
            return toks[2] if len(toks) >= 3 else line
    return ""


def _ffmpeg_source(exe: str) -> str:
    """Where the resolved ffmpeg came from, matching find_ffmpeg's precedence."""
    if os.environ.get("OCTACAM_FFMPEG"):
        return "OCTACAM_FFMPEG override"
    try:
        import imageio_ffmpeg

        if os.path.realpath(imageio_ffmpeg.get_ffmpeg_exe()) == os.path.realpath(exe):
            from importlib.metadata import PackageNotFoundError, version

            try:
                return f"bundled imageio-ffmpeg {version('imageio-ffmpeg')}"
            except PackageNotFoundError:
                return "bundled imageio-ffmpeg"
    except Exception:  # pragma: no cover - depends on environment
        pass
    return "system PATH"


def _instance_lock_holder(config_dir: Path) -> str | None:
    """The PID holding this rig's instance lock, or None if it is free.

    Read-only: we try a non-blocking flock and release it immediately if we win,
    so probing never steals the lock from — nor blocks — a running session. A
    failed acquire means another octacam owns the rig; its PID is read from the
    file (purely informational, so "unknown" if the file is empty)."""
    path = _instance_lock_path(config_dir)
    try:
        handle = open(path)
    except OSError:
        return None  # never created -> nobody has ever locked this rig
    try:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return handle.read().strip() or "unknown"
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        return None
    finally:
        handle.close()


def _report_free_space(report: _Report, path: Path, label: str) -> None:
    """Report free space on the filesystem holding ``path`` (or its nearest parent)."""
    probe = path
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    try:
        usage = shutil.disk_usage(probe)
    except OSError as e:
        report.add("warn", f"{label}: could not check free space on {probe} ({e})")
        return
    free_gb = usage.free / 1e9
    report.add(
        "warn" if free_gb < 5 else "ok",
        f"{label}: {free_gb:.1f} GB free on {probe}",
    )


def _doctor_system(report: _Report) -> None:
    import platform

    report.section("System")
    report.add("info", f"octacam {octacam.__version__}")
    report.add(
        "info", f"Python {platform.python_version()} on {platform.platform(terse=True)}"
    )
    soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    need = 1200  # ~150 fds/camera (pylon USB stack) for an 8-camera rig
    if hard != resource.RLIM_INFINITY and hard < need:
        report.add(
            "warn",
            f"open-file hard limit is low ({hard}); ~150 fds/camera means an "
            f"8-camera rig needs ~{need}",
        )
    elif soft < hard:
        report.add("ok", f"open-file limit {soft}→{hard} (raised to hard at launch)")
    else:
        report.add("ok", f"open-file limit {soft}")


def _doctor_backends(report: _Report, only_backend: str | None) -> None:
    from octacam.cameras import BackendUnavailable
    from octacam.cameras.registry import BACKENDS, select_backend

    report.section("Camera backends")
    # `fake` is a synthetic test/CI backend that always reports FAKE-* serials
    # regardless of hardware, so it is only enumerated when explicitly requested
    # (--backend fake) — never in the default all-backends sweep.
    backends = (
        (only_backend,) if only_backend else tuple(b for b in BACKENDS if b != "fake")
    )
    for name in backends:
        try:
            select_backend(name)
        except BackendUnavailable as e:
            # A backend whose SDK isn't installed is expected on a single-vendor
            # rig — report it, but don't cry wolf.
            report.add("info", str(e))
            continue
        except Exception as e:
            report.add("warn", f"{name}: could not select backend ({e})")
            continue
        try:
            cams = _enumerate_backend(name)
        except Exception as e:
            report.add("warn", f"{name}: available, but enumeration failed ({e})")
            continue
        report.add("ok", f"{name}: available — {len(cams)} camera(s) detected")
        for serial, model in cams:
            report.add("list", f"{model}  {serial}" if model else serial)
    if not only_backend and os.environ.get("PYLON_CAMEMU"):
        report.add(
            "info",
            f"PYLON_CAMEMU={os.environ['PYLON_CAMEMU']} (emulated Basler cameras)",
        )


def _doctor_encoding(report: _Report) -> None:
    from octacam.writer import (
        DEFAULT_FFMPEG_PARAMS,
        DEFAULT_TRANSCODE_FFMPEG_PARAMS,
        find_ffmpeg,
    )

    report.section("Encoding toolchain")
    try:
        exe = find_ffmpeg()
    except RuntimeError as e:
        report.add("error", str(e))
        return
    version = _ffmpeg_version(exe)
    report.add(
        "ok" if version else "warn",
        f"ffmpeg {version or 'version unknown'} ({_ffmpeg_source(exe)})",
    )
    report.add("list", exe)
    has_x264 = "libx264" in _run_ffmpeg_probe(exe, ["-hide_banner", "-encoders"])
    report.add(
        "ok" if has_x264 else "error",
        "libx264 encoder present"
        if has_x264
        else "libx264 encoder MISSING — the default record/transcode params need it",
    )
    system = shutil.which("ffmpeg")
    if system and os.path.realpath(system) != os.path.realpath(exe):
        sysver = _ffmpeg_version(system)
        report.add(
            "info",
            f"system ffmpeg on PATH: {sysver or system} (unused; the resolved "
            "binary takes precedence — colour-range flags can differ by version)",
        )
    report.add("info", f"default record params:    {DEFAULT_FFMPEG_PARAMS}")
    report.add("info", f"default transcode params: {DEFAULT_TRANSCODE_FFMPEG_PARAMS}")


def _doctor_config(report: _Report, config_dir: Path):
    import tomllib

    from octacam.config import (
        find_config_file,
        load_config_dir,
        resolve_dir_template,
        resolve_save_dir,
    )

    report.section(f"Config ({config_dir})")
    cfg_file = find_config_file(config_dir)
    if not cfg_file.exists():
        report.add(
            "warn",
            f"no {cfg_file.name} here — all detected cameras would be used, with defaults",
        )
        return None
    try:
        tomllib.loads(cfg_file.read_text())
    except (OSError, tomllib.TOMLDecodeError) as e:
        report.add("error", f"{cfg_file.name} could not be parsed: {e}")
        return None
    cfg = load_config_dir(config_dir)
    report.add(
        "ok",
        f"{cfg_file.name} loaded (backend={cfg.backend}, "
        f"{len(cfg.cameras)} camera(s) declared)",
    )
    report.add("info", f"next recording → {resolve_save_dir(cfg.record)}")
    if cfg.transfer and cfg.transfer.directory:
        report.add("info", f"transfer → {resolve_dir_template(cfg.transfer.directory)}")
    else:
        report.add("info", "no [transfer] destination configured")
    return cfg


def _doctor_cameras_vs_config(report: _Report, cfg, only_backend: str | None) -> None:
    report.section("Cameras vs config")
    declared = [c.serial_number for c in cfg.cameras]
    if not declared:
        report.add("info", "config declares no serials; all detected cameras are used")
        return
    backend = only_backend or cfg.backend
    try:
        detected = {serial for serial, _model in _enumerate_backend(backend)}
    except Exception as e:
        report.add("warn", f"could not enumerate {backend} to cross-check ({e})")
        return
    missing = [s for s in declared if s not in detected]
    extra = sorted(detected - set(declared))
    if not missing:
        report.add(
            "ok", f"all {len(declared)} declared camera(s) detected on {backend}"
        )
    for serial in missing:
        report.add(
            "error",
            f"serial {serial} declared but NOT detected (unplugged? wrong serial?)",
        )
    for serial in extra:
        report.add("info", f"serial {serial} detected but not in config (won't record)")


def _doctor_storage(report: _Report, cfg) -> None:
    from octacam.config import resolve_dir_template, resolve_save_dir

    report.section("Storage & transfer")
    _report_free_space(report, Path(resolve_save_dir(cfg.record)), "record dir")
    transfer = cfg.transfer
    if transfer is None or not transfer.directory:
        report.add("info", "no [transfer] destination configured")
        return
    dest = Path(resolve_dir_template(transfer.directory))
    if not dest.exists():
        report.add(
            "warn",
            f"transfer dest not present/mounted: {dest} (local recording still works)",
        )
        return
    if not os.access(dest, os.W_OK):
        report.add("error", f"transfer dest not writable: {dest}")
        return
    _report_free_space(report, dest, "transfer dest")
    report.add(
        "info", f"checksum verify: {'on' if transfer.checksum else 'off (size-only)'}"
    )


def _doctor_plugins(report: _Report, cfg) -> None:
    from octacam import plugins as plugins_mod

    report.section("Plugins")
    infos = plugins_mod.available_plugins()
    by_name = {info.name: info for info in infos}
    for info in infos:
        if info.available:
            suffix = f" ({info.summary})" if info.summary else ""
            report.add("ok", f"{info.name} — available{suffix}")
        else:
            suffix = f" ({info.detail})" if info.detail else ""
            report.add("info", f"{info.name} — unavailable{suffix}")
    if cfg is None:
        return
    for pc in cfg.plugins:
        name = plugins_mod._ALIASES.get(pc.name, pc.name)
        info = by_name.get(name)
        if info is None:
            report.add("error", f"config enables unknown plugin {pc.name!r}")
        elif not info.available:
            report.add(
                "error",
                f"config enables {name!r} but it is unavailable ({info.detail})",
            )
        else:
            report.add("ok", f"config enables {name!r} (available)")


def _doctor_runtime(report: _Report, config_dir: Path | None) -> None:
    from octacam import session_cache

    report.section("Recording cache & runtime")
    cdir = session_cache.cache_dir()
    try:
        tracked = {
            e["folder"] for e in session_cache._read_entries() if e.get("folder")
        }
    except Exception:
        tracked = set()
    existing = session_cache.all_folders()
    stale = len(tracked - {str(p) for p in existing})
    writable = os.access(cdir if cdir.exists() else cdir.parent, os.W_OK)
    report.add(
        "ok" if writable else "warn",
        f"cache {cdir} — {len(existing)} recording(s)"
        + (f", {stale} stale (deleted)" if stale else ""),
    )
    try:
        running = session_cache.transcode_running()
    except Exception:
        running = 0
    if running:
        report.add(
            "warn",
            f"{running} transcode(s) running here — CPU-heavy, may cause dropped "
            "frames if you start recording now",
        )
    else:
        report.add("ok", "no transcode running on this machine")
    if config_dir is not None:
        holder = _instance_lock_holder(config_dir.resolve())
        if holder:
            report.add(
                "warn",
                f"another octacam holds this rig's lock (pid {holder}) — its "
                "cameras are in use",
            )
        else:
            report.add("ok", "no other octacam instance holds this rig")
    if _port_available("127.0.0.1", 8765):
        report.add("ok", "GUI port 8765 is free")
    else:
        report.add("warn", "GUI port 8765 is in use (launch gui with --port to change)")
    if _in_ssh_session():
        report.add(
            "info",
            "SSH session — the GUI won't auto-open a browser; use an ssh -L tunnel",
        )
    elif sys.platform.startswith("linux") and not (
        os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")
    ):
        report.add("info", "no local display — the GUI won't auto-open a browser")


def _render_doctor(report: _Report) -> None:
    from rich.console import Console
    from rich.text import Text

    console = Console()
    console.print()
    console.print(Text(f"octacam doctor — octacam {octacam.__version__}", style="bold"))
    for title, items in report.sections:
        console.print()
        console.print(Text(title, style="bold"))
        for status, text in items:
            marker, style = _MARKERS[status]
            if not marker:  # a plain listing line
                console.print(Text("      " + text))
                continue
            line = Text("  ")
            line.append(marker + " ", style=style or None)
            line.append(text)
            console.print(line)
    errors, warns = report.counts()
    console.print()
    if errors or warns:
        console.print(
            Text(
                f"{errors} error(s), {warns} warning(s)",
                style="bold red" if errors else "bold yellow",
            )
        )
    else:
        console.print(Text("All checks passed.", style="bold green"))


def _emit_doctor_json(report: _Report) -> None:
    errors, warns = report.counts()
    payload = {
        "octacam_version": octacam.__version__,
        "sections": [
            {
                "title": title,
                "findings": [{"status": s, "text": t} for s, t in items],
            }
            for title, items in report.sections
        ],
        "errors": errors,
        "warnings": warns,
    }
    typer.echo(json.dumps(payload, indent=2))


@app.command()
def doctor(
    config_dir: Annotated[
        Path | None,
        typer.Argument(
            exists=True,
            file_okay=False,
            dir_okay=True,
            help="Optional rig config dir. When given, doctor also validates that "
            "rig's config, resolves its save/transfer paths, cross-checks declared "
            "vs detected cameras, and reports the plugin selection.",
        ),
    ] = None,
    backend: Annotated[
        str | None,
        typer.Option(
            "--backend",
            help="Only enumerate this backend (basler/flir/fake). Default: every "
            "available backend.",
        ),
    ] = None,
    json_output: Annotated[
        bool,
        typer.Option(
            "--json", help="Emit machine-readable JSON instead of the report."
        ),
    ] = False,
    strict: Annotated[
        bool,
        typer.Option(
            "--check",
            help="Exit nonzero on warnings too (for CI), not only on errors.",
        ),
    ] = False,
) -> None:
    """Diagnose the octacam install and, optionally, a rig config.

    Lists detected cameras and bundled plugins, and checks the encoding toolchain,
    storage, recording cache, and runtime conflicts. Pass a CONFIG_DIR to also
    validate that rig. doctor never opens the cameras, so it is safe to run while
    a GUI or `record` session is live.

    Exits 0 when no errors are found (nonzero on errors, or on warnings too with
    --check), so it is usable as a pre-flight check in scripts.
    """
    report = _Report()
    _doctor_system(report)
    _doctor_backends(report, backend)
    _doctor_encoding(report)
    cfg = _doctor_config(report, config_dir) if config_dir is not None else None
    if cfg is not None:
        _doctor_cameras_vs_config(report, cfg, backend)
        _doctor_storage(report, cfg)
    _doctor_plugins(report, cfg)
    _doctor_runtime(report, config_dir)

    if json_output:
        _emit_doctor_json(report)
    else:
        _render_doctor(report)

    errors, warns = report.counts()
    if errors or (strict and warns):
        raise typer.Exit(1)


# ---------------------------------------------------------------------------
# `octacam config` — interactive first-run config wizard
#
# Scaffolds a new rig config dir (octacam_config.toml) by detecting the camera
# backend and serials, then prompting for the record/transfer scalars. The
# visual per-camera bits — window placement, rotation, the grid — are left to
# `octacam gui`, which tunes them against a live preview; this wizard just
# produces a good starting point and hands off. It never opens a camera
# (enumeration only), so it is safe to run while another session is live.
# ---------------------------------------------------------------------------


def _available_backends() -> list[str]:
    """Which SDK-backed backends (basler/flir) are installed here, in order.

    Lets the wizard default to a backend this machine can actually use. ``fake``
    is a synthetic test backend, never auto-detected. Selection imports the SDK
    but never opens a device, so this is side-effect free."""
    from octacam.cameras.registry import select_backend

    available: list[str] = []
    for name in ("basler", "flir"):
        try:
            select_backend(name)
        except Exception:
            continue
        available.append(name)
    return available


def _prompt_backend(console, cli_backend: str | None) -> str:
    """Resolve the camera backend from --backend or an interactive prompt."""
    from rich.prompt import Prompt

    from octacam.cameras.registry import BACKENDS

    if cli_backend is not None:
        key = cli_backend.strip().lower()
        if key not in BACKENDS:
            raise typer.BadParameter(
                f"unknown backend {cli_backend!r}; expected one of "
                f"{', '.join(BACKENDS)}",
                param_hint="--backend",
            )
        return key
    available = _available_backends()
    if available:
        console.print(
            f"Detected camera backend(s): [bold]{', '.join(available)}[/bold]"
        )
    else:
        console.print(
            "[yellow]No camera SDK detected here[/yellow] — you can still write a "
            "config now and detect cameras later on the rig."
        )
    return Prompt.ask(
        "Camera backend",
        choices=list(BACKENDS),
        default=available[0] if available else "basler",
        console=console,
    )


def _detect_cameras(console, backend: str) -> list[tuple[str, str | None]]:
    """List (serial, model|None) for *backend*; [] if none or enumeration fails."""
    try:
        cams = _enumerate_backend(backend)
    except Exception as e:
        console.print(f"[yellow]Could not enumerate {backend} cameras:[/yellow] {e}")
        return []
    if cams:
        console.print(f"Detected [bold]{len(cams)}[/bold] {backend} camera(s):")
        for serial, model in cams:
            console.print(f"  • {model + '  ' if model else ''}{serial}")
    else:
        console.print(f"[yellow]No {backend} cameras detected.[/yellow]")
    return cams


def _prompt_cameras(console, detected: list[tuple[str, str | None]]) -> list[dict]:
    """Build ``cameras`` entry dicts from detected serials (+ optional manual).

    Each entry is ``{"serial_number": ...}`` plus a validated, unique ``name``
    when the user gives one. Left empty when no serials are known, so the config
    falls back to using every camera detected at record time."""
    from rich.prompt import Confirm, Prompt

    from octacam.config import _is_safe_camera_name

    serials = [serial for serial, _model in detected]
    if not serials and Confirm.ask(
        "Add camera serial numbers manually?", default=False, console=console
    ):
        while True:
            serial = Prompt.ask(
                "  Serial number (blank to finish)", default="", console=console
            ).strip()
            if not serial:
                break
            serials.append(serial)
    if not serials:
        console.print(
            "No cameras listed — the config will use every camera detected at "
            "record time."
        )
        return []
    if not Confirm.ask("Name these cameras now?", default=True, console=console):
        return [{"serial_number": s} for s in serials]

    entries: list[dict] = []
    used: set[str] = set()
    for serial in serials:
        while True:
            name = Prompt.ask(
                f"  Name for {serial} (blank to use the serial)",
                default="",
                console=console,
            ).strip()
            if not name:
                entries.append({"serial_number": serial})
                break
            if not _is_safe_camera_name(name):
                console.print(
                    r"    [red]Invalid name[/red] — no '/', '\', '.' or '..'."
                )
                continue
            if name in used:
                console.print(
                    f"    [red]{name!r} is already used[/red] — pick another."
                )
                continue
            used.add(name)
            entries.append({"serial_number": serial, "name": name})
            break
    return entries


def _prompt_visualization(console, cameras: list[dict]) -> list[dict]:
    """Offer a single auto-arranged ``grid.mp4`` visualization from named cameras.

    Only offered when at least two cameras are named (a grid of one is pointless);
    the near-square layout reuses :func:`octacam.grid.auto_layout`."""
    from rich.prompt import Confirm

    from octacam.grid import auto_layout

    names = [c["name"] for c in cameras if c.get("name")]
    if len(names) < 2:
        return []
    if not Confirm.ask(
        f"Add a visualization grid from the {len(names)} named camera(s)?",
        default=True,
        console=console,
    ):
        return []
    return [{"name": "grid.mp4", "layout": auto_layout(names)}]


def _prompt_record(console) -> "RecordConfig":
    """Prompt for the [record] section, defaulting every field to the schema default."""
    from rich.prompt import FloatPrompt, Prompt

    from octacam.config import RecordConfig

    d = RecordConfig()
    fps = FloatPrompt.ask("Frame rate (fps)", default=d.fps, console=console)
    duration = FloatPrompt.ask(
        "Recording duration", default=d.duration, console=console
    )
    duration_unit = Prompt.ask(
        "Duration unit",
        choices=["frames", "seconds", "minutes", "hours"],
        default=d.duration_unit,
        console=console,
    )
    trigger_source = Prompt.ask(
        "Trigger source",
        choices=["software", "external"],
        default=d.trigger_source,
        console=console,
    )
    directory = Prompt.ask(
        "Save directory (base)", default=d.directory, console=console
    )
    relative_directory = Prompt.ask(
        "Relative directory template (strftime %-codes ok, blank for none)",
        default=d.relative_directory,
        console=console,
    )
    save_method = Prompt.ask(
        "Save method",
        choices=["ffmpeg", "raw"],
        default=d.save_method,
        console=console,
    )
    # model_validate (not the constructor) so pydantic narrows the choice strings
    # to their Literal fields at runtime instead of pyright rejecting `str` here.
    return RecordConfig.model_validate(
        {
            "fps": fps,
            "duration": duration,
            "duration_unit": duration_unit,
            "trigger_source": trigger_source,
            "directory": directory,
            "relative_directory": relative_directory,
            "save_method": save_method,
        }
    )


def _prompt_transfer(console) -> dict | None:
    """Optionally prompt for a transfer destination; None to leave it unset."""
    from rich.prompt import Confirm, Prompt

    if not Confirm.ask(
        "Configure a transfer destination (mirror recordings elsewhere)?",
        default=False,
        console=console,
    ):
        return None
    directory = Prompt.ask("  Transfer destination directory", console=console)
    checksum = Confirm.ask(
        "  Verify each copy with a checksum?", default=True, console=console
    )
    return {"directory": directory, "checksum": checksum}


def _build_config_doc(
    backend: str,
    record_cfg: "RecordConfig",
    cameras: list[dict],
    visualization: list[dict],
    transfer: dict | None,
) -> dict:
    """Assemble the raw-TOML dict the config writer serializes.

    ``backend`` is written only when non-default (absent means basler), matching
    how existing configs are kept clean; empty sections are omitted entirely."""
    from octacam.config import TranscodeConfig

    doc: dict = {}
    if backend != "basler":
        doc["backend"] = backend
    doc["record"] = record_cfg.model_dump()
    doc["transcode"] = TranscodeConfig().model_dump()
    if cameras:
        doc["cameras"] = cameras
    if visualization:
        doc["visualization"] = visualization
    if transfer:
        doc["transfer"] = transfer
    return doc


@app.command()
def config(
    config_dir: Annotated[
        Path | None,
        typer.Argument(
            file_okay=False,
            dir_okay=True,
            help="Directory to create the config in. Omit to be prompted for one.",
        ),
    ] = None,
    backend: Annotated[
        str | None,
        typer.Option(
            "--backend",
            help="Camera backend (basler/flir/fake). Default: auto-detect, else ask.",
        ),
    ] = None,
    force: Annotated[
        bool,
        typer.Option(
            "--force",
            help="Overwrite an existing octacam_config.toml without asking.",
        ),
    ] = False,
) -> None:
    """Interactively scaffold a new rig config directory.

    Detects the camera backend and serials, then prompts for the record and
    transfer settings and writes an octacam_config.toml. The visual per-camera
    bits — window placement, rotation, and the grid — are left to `octacam gui`,
    which tunes them against a live preview; run it next on the new directory.
    Never opens a camera, so it is safe to run while another session is live.
    """
    from rich.console import Console
    from rich.prompt import Confirm, Prompt

    from octacam import config_writer

    console = Console()
    console.print("[bold]octacam config[/bold] — set up a new rig config\n")

    chosen_backend = _prompt_backend(console, backend)
    detected = _detect_cameras(console, chosen_backend)
    cameras = _prompt_cameras(console, detected)
    visualization = _prompt_visualization(console, cameras)
    console.print()
    record_cfg = _prompt_record(console)
    console.print()
    transfer = _prompt_transfer(console)

    if config_dir is None:
        console.print()
        target = Path(
            Prompt.ask(
                "Config directory to create", default="octacam-rig", console=console
            )
        ).expanduser()
    else:
        target = config_dir.expanduser()

    cfg_file = target / "octacam_config.toml"
    if (
        cfg_file.exists()
        and not force
        and not Confirm.ask(
            f"{cfg_file} already exists — overwrite?", default=False, console=console
        )
    ):
        console.print("Aborted.")
        raise typer.Exit(1)

    doc = _build_config_doc(
        chosen_backend, record_cfg, cameras, visualization, transfer
    )
    try:
        target.mkdir(parents=True, exist_ok=True)
        written = config_writer.write_config(target, doc)
    except OSError as e:
        sys.exit(f"Failed to write config: {e}")

    console.print(f"\n[green]✓[/green] Wrote [bold]{written}[/bold]")
    console.print("\nNext steps:")
    console.print(f"  • Validate it:          octacam doctor {target}")
    console.print(f"  • Place cameras & grid: octacam gui {target}")
    console.print(f"  • Record headlessly:    octacam record {target}")


@app.command()
def record(
    config_dir: Annotated[
        Path,
        typer.Argument(exists=True, file_okay=False, dir_okay=True),
    ] = Path("."),
    fps: Annotated[
        float | None,
        typer.Option("--fps", "-f", help=r"Frame rate \[default: from config]."),
    ] = None,
    duration: Annotated[
        float | None,
        typer.Option(
            "--duration",
            "-d",
            help=r"Recording duration in seconds \[default: from config's "
            "duration/duration_unit].",
        ),
    ] = None,
    output: Annotated[
        Path | None,
        typer.Option(
            "--output",
            "-o",
            help="Save directory, overriding the templated directory/"
            "relative_directory from config.",
        ),
    ] = None,
    enabled_plugins: EnabledPlugins = None,
    no_plugins: NoPlugins = False,
) -> None:
    r"""Record videos headlessly from the cameras in CONFIG_DIR.

    Encoding, save method, transform, and the save-directory template all come
    from the config's \[record] section; the options here override only the
    day-to-day values (fps and duration, or an explicit --output save directory).
    """
    from octacam import session_cache
    from octacam.cameras import BackendUnavailable, CameraSystem
    from octacam.config import load_config_dir
    from octacam.controller import RecordingController, normalize_save_dir
    from octacam.plugins import build_plugins

    config = load_config_dir(config_dir)

    # Apply the fps override to the [record] section before resolving the
    # templated save directory from it.
    record_cfg = (
        config.record.model_copy(update={"fps": fps})
        if fps is not None
        else config.record
    )

    settings = _settings_from_record(record_cfg, config.transcode, config.transfer)
    if duration is not None:
        settings.duration_s = duration
    if output is not None:
        # An explicit save dir bypasses the template; drop the record_directory
        # base and relative sub-path so the summary's relative_directory falls
        # back to the folder name.
        settings.save_dir = normalize_save_dir(str(output))
        settings.record_directory = ""
        settings.relative_directory = ""

    # A transcode running on this machine will fight live capture for the CPU.
    _warn_if_transcoding()

    try:
        system = CameraSystem(
            [c.serial_number for c in config.cameras], backend=config.backend
        )
    except BackendUnavailable as e:
        sys.exit(str(e))
    if len(system) == 0:
        log.warning("No cameras opened. Exiting.")
        sys.exit(1)
    log.info("Opened %d camera(s)", len(system))

    names = {c.serial_number: c.name for c in config.cameras if c.name}
    for camera in system:
        camera.name = names.get(camera.serial_number, camera.name)

    system.load_config(config_dir)
    system.apply_display_config(config.cameras)

    if Path(settings.save_dir).exists():
        log.warning(
            "Directory already exists, data might be overwritten: %s", settings.save_dir
        )

    plugins = build_plugins(config, _resolve_enabled(enabled_plugins, no_plugins))
    plugins.setup_all()

    # Tag this headless run in the session cache so `octacam process --last`
    # and `--session` pick it up too (a one-off, single-folder "session").
    controller = RecordingController(
        system,
        settings,
        plugins,
        auto_preview=False,
        session_id=session_cache.new_session_id(),
        record_kind="record",
        config_dir=config_dir,
    )
    try:
        log.info(
            "Recording %d camera(s) at %g fps for %g s to %s",
            len(system),
            settings.fps,
            settings.duration_s,
            settings.save_dir,
        )
        result = controller.start_recording(confirm_overwrite=True)
        if not result.ok:
            sys.exit(f"Failed to start recording: {result.message}")
        controller.join()
    finally:
        system.close()
        plugins.teardown_all()

    extension = settings.video_format().extension
    for camera in system:
        typer.echo(f"{Path(settings.save_dir) / camera.name}.{extension}")


def _read_summary(path: Path) -> dict | None:
    """Load a recording_summary.json, or None (with a warning) if unreadable."""
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError) as e:
        log.warning("Could not read %s: %s", path, e)
        return None


@dataclass
class TranscodeJob:
    """One source file to transcode, with the geometry a .raw input needs.

    For a ``.raw`` input, width/height/fps/pixel_format/frames come from the
    recording_summary.json (the raw stream carries none of its own). Encoded
    inputs (.mkv/.mp4) read their own geometry, so those stay None."""

    input_path: Path
    frames: int | None = None
    width: int | None = None
    height: int | None = None
    fps: float | None = None
    pixel_format: str = "Mono8"


def _transcode_jobs(paths: list[Path], recursive: bool) -> list[TranscodeJob]:
    """Resolve folders/files to a deduped list of :class:`TranscodeJob`.

    A folder with a recording_summary.json is driven by it (each camera entry's
    geometry threaded onto the job); one without has its loose .mkv/.raw
    transcoded with a warning. A file is matched against a summary in its own
    folder, else transcoded plainly. Recordings are reproduced as-saved (the
    display transform, if any, was baked in at record time)."""
    from octacam.transform import RECORDING_SUMMARY_FILENAME
    from octacam.writer import is_partial_transcode

    jobs: dict[Path, TranscodeJob] = {}

    def add(job: TranscodeJob) -> None:
        jobs.setdefault(job.input_path.resolve(), job)

    def _job_from_entry(
        video: Path, entry: dict, fps_target: float | None
    ) -> TranscodeJob:
        frames = entry.get("frames")
        return TranscodeJob(
            input_path=video,
            frames=frames if isinstance(frames, int) else None,
            width=entry.get("width"),
            height=entry.get("height"),
            fps=entry.get("fps") or fps_target,
            pixel_format=entry.get("pixel_format") or "Mono8",
        )

    def handle_dir(directory: Path) -> None:
        summary_path = directory / RECORDING_SUMMARY_FILENAME
        if summary_path.exists():
            data = _read_summary(summary_path)
            if data is not None:
                fps_target = data.get("fps_target")
                for entry in data.get("cameras", []):
                    name = entry.get("file")
                    if not name:
                        continue
                    video = directory / name
                    if not video.exists():
                        log.warning("%s lists %s but it is missing", summary_path, name)
                    elif entry.get("frames") == 0:
                        # A 0-frame recording is a header-only file with no video
                        # (e.g. an external trigger that never fired). Feeding it
                        # to ffmpeg only yields a cryptic matroska/EBML error, so
                        # skip it here with a clear message instead.
                        log.warning(
                            "Skipping %s: recording captured 0 frames "
                            "(empty header-only file)",
                            video,
                        )
                    else:
                        add(_job_from_entry(video, entry, fps_target))
                return
        loose = sorted(
            p
            for p in directory.iterdir()
            if p.suffix in (".mkv", ".raw") and not is_partial_transcode(p)
        )
        if loose:
            log.warning(
                "No %s in %s; transcoding %d file(s) with defaults",
                RECORDING_SUMMARY_FILENAME,
                directory,
                len(loose),
            )
        for video in loose:
            add(TranscodeJob(input_path=video))

    for path in paths:
        if path.is_dir():
            handle_dir(path)
            if recursive:
                for sub in sorted(path.rglob("*")):
                    if sub.is_dir():
                        handle_dir(sub)
        elif is_partial_transcode(path):
            # An orphaned in-progress temp (left by a hard kill) named directly
            # is not a real recording — skip it as the folder scan does, so it
            # is never fed to ffmpeg.
            log.warning("Skipping orphaned partial transcode: %s", path)
        else:
            entry = None
            fps_target = None
            summary_path = path.parent / RECORDING_SUMMARY_FILENAME
            if summary_path.exists():
                data = _read_summary(summary_path)
                if data is not None:
                    fps_target = data.get("fps_target")
                    entry = next(
                        (
                            e
                            for e in data.get("cameras", [])
                            if e.get("file") == path.name
                        ),
                        None,
                    )
            if entry is not None:
                add(_job_from_entry(path, entry, fps_target))
            else:
                log.warning(
                    "No %s entry for %s; transcoding with defaults",
                    RECORDING_SUMMARY_FILENAME,
                    path,
                )
                add(TranscodeJob(input_path=path))

    return list(jobs.values())


def _resolve_transcode_paths(
    paths: list[Path],
    last: bool,
    session: bool,
    session_id: str | None,
    all_: bool,
) -> list[Path]:
    """Resolve explicit PATHS or one cache selector to a list of folders.

    The selectors --last/--session/--session-id/--all are mutually
    exclusive and cannot be combined with explicit PATHS. They read the recording
    cache (octacam.session_cache) and skip folders that have since been deleted,
    so a removed recording is simply ignored. ``--session`` means the most recent
    session; ``--session-id`` names an exact one (what the GUI prints on exit, so
    the command stays correct even if another recording happens afterwards);
    ``--all`` is every folder the cache still holds (last RETENTION_DAYS).
    Exits with a clear message on a bad combination or when nothing is found.
    """
    from octacam import session_cache

    chosen = [
        name
        for name, on in (
            ("--last", last),
            ("--session", session),
            ("--session-id", session_id is not None),
            ("--all", all_),
        )
        if on
    ]
    if len(chosen) > 1:
        sys.exit(f"Choose at most one of {', '.join(chosen)}.")
    if chosen and paths:
        sys.exit(f"{chosen[0]} cannot be combined with explicit PATHS.")
    if not chosen:
        if not paths:
            sys.exit(
                "Provide one or more PATHS, or one of "
                "--last/--session/--session-id/--all."
            )
        return paths

    if last:
        selector = "--last"
        folder = session_cache.last_folder()
        folders = [folder] if folder else []
    elif all_:
        selector = "--all"
        folders = session_cache.all_folders()
    elif session:
        selector = "--session"
        folders = session_cache.session_folders()
    else:
        selector = f"--session-id {shlex.quote(session_id or '')}"
        folders = session_cache.session_folders(session_id)
    if not folders:
        sys.exit(
            f"No recordings found for {selector} in the cache "
            f"({session_cache.cache_dir()}). Record something first, or pass "
            "explicit PATHS."
        )
    log.info(
        "%s: transcoding %d folder(s) from the recording cache", selector, len(folders)
    )
    return folders


class _TranscodeProgressBar:
    """An octacam-styled rich progress bar fed by writer progress callbacks.

    One bar walks the batch, each file labelled ``[i/N] name``. A fresh task is
    started per file (the previous one removed) so a file with no known frame
    total stays genuinely indeterminate — rich's reset/update treat ``total=None``
    as "keep the current total", so reusing one task would leak a prior file's
    total. It draws on the shared stderr console so octacam log lines (and the
    stdout result paths) render cleanly above the live bar."""

    def __init__(self, total_jobs: int):
        from rich.progress import (
            BarColumn,
            Progress,
            SpinnerColumn,
            TaskProgressColumn,
            TextColumn,
            TimeElapsedColumn,
        )

        self._total_jobs = total_jobs
        self._progress = Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            TaskProgressColumn(),
            TextColumn("{task.fields[stats]}"),
            TimeElapsedColumn(),
            console=_stderr_console(),
            transient=True,
        )
        self._task: TaskID | None = None

    def __enter__(self):
        self._progress.start()
        return self

    def __exit__(self, *exc) -> None:
        self._progress.stop()

    def file(self, index: int, path: Path) -> "ProgressCallback":
        """Start a fresh bar for one file and return its progress callback."""
        from octacam.writer import TranscodeProgress

        if self._task is not None:
            self._progress.remove_task(self._task)  # keep only one bar visible
        desc = f"[{index}/{self._total_jobs}] {path.name}"
        task = self._progress.add_task(desc, total=None, stats="")
        self._task = task

        def on_progress(p: TranscodeProgress) -> None:
            stats = [f"{p.frame} frames"]
            if p.fps:
                stats.append(f"{p.fps:.0f} fps")
            if p.speed:
                stats.append(f"{p.speed:.3g}x")
            # On the final block, snap the bar to a clean 100%. The frame total
            # is only a hint and may over- or undershoot the frames actually
            # encoded (e.g. a recording with dropped frames), and a file with no
            # known total has been drawing an indeterminate bar — so adopt the
            # final frame count as the total whenever it would otherwise leave
            # the bar shy of (or past) full, or mid indeterminate-pulse.
            completed = p.frame
            total = p.total_frames
            if p.done:
                total = max(p.frame, total or 0) or None
                completed = p.frame if total is None else total
            self._progress.update(
                task,
                total=total,
                completed=completed,
                stats="  ".join(stats),
                # Paint the full bar now: rich's auto-refresh runs on a timer and
                # may not tick before this task is removed (next file) or the
                # batch stops (transient bar is wiped), leaving the last painted
                # frame short of 100%. refresh() no-ops until the live is started.
                refresh=p.done,
            )

        return on_progress


class _GridProgressBar:
    """Rich progress bar for grid video generation, driven by TranscodeProgress callbacks.

    Mirrors ``_TranscodeProgressBar`` but labels each job as ``grid: <folder>``
    to distinguish it from the transcode phase."""

    def __init__(self, total_jobs: int):
        from rich.progress import (
            BarColumn,
            Progress,
            SpinnerColumn,
            TaskProgressColumn,
            TextColumn,
            TimeElapsedColumn,
        )

        self._total_jobs = total_jobs
        self._progress = Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            TaskProgressColumn(),
            TextColumn("{task.fields[stats]}"),
            TimeElapsedColumn(),
            console=_stderr_console(),
            transient=True,
        )
        self._task: TaskID | None = None

    def __enter__(self):
        self._progress.start()
        return self

    def __exit__(self, *exc) -> None:
        self._progress.stop()

    def folder(self, index: int, path: Path) -> "ProgressCallback":
        """Start a fresh bar for one grid encode and return its progress callback."""
        from octacam.writer import TranscodeProgress

        if self._task is not None:
            self._progress.remove_task(self._task)
        desc = f"[{index}/{self._total_jobs}] grid: {path.name}"
        task = self._progress.add_task(desc, total=None, stats="")
        self._task = task

        def on_progress(p: TranscodeProgress) -> None:
            stats = [f"{p.frame} frames"]
            if p.fps:
                stats.append(f"{p.fps:.0f} fps")
            if p.speed:
                stats.append(f"{p.speed:.3g}x")
            completed = p.frame
            total = p.total_frames
            if p.done:
                total = max(p.frame, total or 0) or None
                completed = p.frame if total is None else total
            self._progress.update(
                task,
                total=total,
                completed=completed,
                stats="  ".join(stats),
                refresh=p.done,
            )

        return on_progress


class _TransferProgressBar:
    """Rich progress bar for file transfers, driven by TransferCallback events.

    Shows one file at a time with byte-level progress and transfer speed.
    A new task is created each time the (file index, phase) changes, so the bar
    cycles through each file's copy then its verify read-back without needing to
    know the list upfront."""

    def __init__(self) -> None:
        from rich.progress import (
            BarColumn,
            Progress,
            SpinnerColumn,
            TaskProgressColumn,
            TextColumn,
            TimeElapsedColumn,
        )

        self._progress = Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            TaskProgressColumn(),
            TextColumn("{task.fields[speed]}"),
            TimeElapsedColumn(),
            console=_stderr_console(),
            transient=True,
        )
        self._task: TaskID | None = None
        self._current_key: tuple[int, str] = (-1, "")

    def __enter__(self):
        self._progress.start()
        return self

    def __exit__(self, *exc) -> None:
        self._progress.stop()

    def make_callback(self):
        """Return a TransferCallback that updates this bar as files are copied."""
        from octacam.transfer import TransferProgress

        def on_progress(p: TransferProgress) -> None:
            key = (p.file_index, p.phase)
            if key != self._current_key:
                if self._task is not None:
                    self._progress.remove_task(self._task)
                self._current_key = key
                verb = "verify" if p.phase == "verify" else "copy"
                desc = f"[{p.file_index}/{p.file_count}] {verb}: {p.filename}"
                self._task = self._progress.add_task(
                    desc, total=p.file_size, speed="", completed=0
                )
            assert self._task is not None  # set on the first event for each key
            speed_str = f"{p.speed_mbs:.1f} MB/s" if p.speed_mbs > 0 else ""
            self._progress.update(
                self._task,
                completed=p.bytes_done,
                speed=speed_str,
                refresh=p.done,
            )

        return on_progress


def _find_recording_dirs(roots: list[Path], recursive: bool) -> list[Path]:
    """Collect recording directories from *roots*.

    A directory is considered a recording if it contains a
    ``recording_summary.json``.  In non-recursive mode each *root* must itself be
    a recording directory; a root that is not one is warned about and skipped
    (so a stray folder mixed in with valid recordings never aborts the batch).
    If that leaves *nothing* to do, this exits with a hint — suggesting ``-r``
    when recordings exist beneath the given path(s).  In recursive mode every
    subdirectory that contains a summary is collected, including *roots*
    themselves if they qualify.  Results are deduped and returned in sorted
    order so the output is deterministic.
    """
    from octacam.transform import RECORDING_SUMMARY_FILENAME

    seen: set[Path] = set()
    result: list[Path] = []

    def _add(p: Path) -> None:
        key = p.resolve()
        if key not in seen:
            seen.add(key)
            result.append(p)

    def _nested_recordings(root: Path) -> list[Path]:
        if not root.is_dir():
            return []
        return [
            sub
            for sub in sorted(root.rglob("*"))
            if sub.is_dir() and (sub / RECORDING_SUMMARY_FILENAME).exists()
        ]

    saw_nested = False
    for root in roots:
        if recursive:
            if (root / RECORDING_SUMMARY_FILENAME).exists():
                _add(root)
            for sub in _nested_recordings(root):
                _add(sub)
            continue
        if (root / RECORDING_SUMMARY_FILENAME).exists():
            _add(root)
            continue
        # Not itself a recording: warn and skip rather than abort, so valid
        # recordings passed alongside it are still processed.
        nested = _nested_recordings(root)
        if nested:
            saw_nested = True
            log.warning(
                "%s is not a recording directory; %d recording(s) found beneath "
                "it — pass -r/--recursive to include them. Skipping.",
                root,
                len(nested),
            )
        else:
            log.warning(
                "%s is not a recording directory (no %s). Skipping.",
                root,
                RECORDING_SUMMARY_FILENAME,
            )

    # Nothing usable was given directly: turn the silent no-op into a hint.
    if not recursive and not result and saw_nested:
        sys.exit(
            "No recording directory given directly — re-run with -r/--recursive "
            "to copy the recordings found beneath the path(s) above."
        )

    return result


def _config_for_recording(folder: Path, cli_config_dir: Path | None):
    """Resolve the config governing one recording folder.

    Precedence: the octacam_config.toml snapshot saved into the folder at record
    time > a --config dir passed on the command line > built-in defaults. This is
    what lets `octacam process` run with no --config for anything recorded after
    the snapshot feature landed.
    """
    from octacam.config import OctacamConfig, load_config_dir

    if (folder / "octacam_config.toml").exists():
        return load_config_dir(folder)
    if cli_config_dir is not None:
        log.warning(
            "%s has no embedded config; falling back to --config %s",
            folder,
            cli_config_dir,
        )
        return load_config_dir(cli_config_dir)
    log.warning(
        "%s has no embedded config and no --config given; using built-in defaults",
        folder,
    )
    return OctacamConfig()


def _visualizations_for(
    cfg, summary_cameras: list[str]
) -> list[tuple[str, list[list[str]] | None, str]]:
    """The (name, layout, ffmpeg_params) grids to build for one folder.

    Uses the explicit ``[[visualization]]`` entries when present; otherwise
    derives a single default ``grid.mp4`` from the rig's cameras (config, else
    the summary's recorded camera names) so a grid is still produced. ``--no-grid``
    skips grid generation entirely upstream of this.
    """
    if cfg.visualization:
        return [(v.name, v.layout, v.ffmpeg_params) for v in cfg.visualization]
    names = [c.name for c in cfg.cameras if c.name] or summary_cameras
    if names:
        from octacam.grid import auto_layout

        return [("grid.mp4", auto_layout(names), "")]
    # No camera names anywhere: let build_grid_video use its built-in default.
    return [("grid.mp4", None, "")]


def _transfer_dest(cfg, folder: Path) -> Path | None:
    """Destination for one folder's transfer, or None to skip it.

    Mirrors the recording's ``relative_directory`` (resolved at record time and
    stored in the summary) under the resolved ``transfer.directory``.
    """
    from octacam.config import resolve_dir_template
    from octacam.transform import RECORDING_SUMMARY_FILENAME

    transfer = cfg.transfer
    if transfer is None or not transfer.directory:
        log.warning(
            "No [transfer].directory resolvable for %s; skipping transfer", folder
        )
        return None
    base = resolve_dir_template(transfer.directory)
    summary = _read_summary(folder / RECORDING_SUMMARY_FILENAME) or {}
    rel = summary.get("relative_directory") or folder.name
    return Path(base) / rel


def _grid_and_transfer(
    folder_outputs: dict[Path, list[Path]],
    do_grid: bool,
    do_transfer: bool,
    cli_config_dir: Path | None,
    dry_run: bool,
    show_bar: bool,
    force: bool = False,
) -> int:
    """Build visualization grids and/or transfer each folder to its destination.

    Two sequential phases (grids then transfers) so each gets its own progress
    bar. Returns the number of files that failed to transfer."""
    from octacam.grid import build_grid_video
    from octacam.transfer import transfer_folder
    from octacam.transform import RECORDING_SUMMARY_FILENAME

    folder_cfgs = {f: _config_for_recording(f, cli_config_dir) for f in folder_outputs}

    # --- Phase 1: visualization grids ---------------------------------------
    grid_files: dict[Path, list[Path]] = {}
    if do_grid:
        grid_bar = (
            _GridProgressBar(len(folder_outputs))
            if (show_bar and not dry_run)
            else None
        )
        grid_skipped = 0
        with grid_bar or contextlib.nullcontext():
            for i, folder in enumerate(folder_outputs, 1):
                cfg = folder_cfgs[folder]
                summary = _read_summary(folder / RECORDING_SUMMARY_FILENAME) or {}
                summary_cams = [
                    c.get("name") for c in summary.get("cameras", []) if c.get("name")
                ]
                built: list[Path] = []
                for name, layout, ff in _visualizations_for(cfg, summary_cams):
                    out_path = folder / name
                    if out_path.exists() and not force:
                        # Idempotent re-run: the grid is already built. Grid
                        # generation is atomic (temp + rename), so a present file
                        # is complete — skip rebuilding, but still hand it to the
                        # transfer phase (which skips it if already copied).
                        # Counted for the summary rather than logged per grid.
                        grid_skipped += 1
                        built.append(out_path)
                        continue
                    on_prog = grid_bar.folder(i, folder) if grid_bar else None
                    out = build_grid_video(
                        folder,
                        layout=layout,
                        output=out_path,
                        ffmpeg_params=ff or cfg.transcode.ffmpeg_params,
                        dry_run=dry_run,
                        on_progress=on_prog,
                    )
                    if out is not None:
                        built.append(out)
                grid_files[folder] = built
        if grid_skipped:
            log.info(
                "%sGrid: %d already exist — skipping (use --force to rebuild)",
                "[dry-run] " if dry_run else "",
                grid_skipped,
            )

    # --- Phase 2: transfer --------------------------------------------------
    transfer_failed = 0
    if do_transfer:
        n_copied = n_skipped = 0
        transfer_bar = _TransferProgressBar() if (show_bar and not dry_run) else None
        with transfer_bar or contextlib.nullcontext():
            transfer_cb = transfer_bar.make_callback() if transfer_bar else None
            for folder, outputs in folder_outputs.items():
                cfg = folder_cfgs[folder]
                dest = _transfer_dest(cfg, folder)
                if dest is None:
                    continue
                files = list(outputs) + grid_files.get(folder, [])
                result = transfer_folder(
                    folder,
                    dest,
                    files_only=files,
                    dry_run=dry_run,
                    verify=cfg.transfer.checksum,
                    on_progress=transfer_cb,
                )
                n_copied += len(result.copied)
                n_skipped += len(result.skipped)
                transfer_failed += len(result.failed)
        if dry_run:
            log.info(
                "[dry-run] Transfer: %d to copy, %d already up to date",
                n_copied,
                n_skipped,
            )
        else:
            log.info(
                "Transfer: %d copied, %d skipped, %d failed",
                n_copied,
                n_skipped,
                transfer_failed,
            )
            if transfer_failed:
                log.error("%d file(s) failed to transfer", transfer_failed)
    return transfer_failed


@app.command()
def process(
    paths: Annotated[
        list[Path] | None,
        typer.Argument(
            exists=True,
            help="Recording folders (or parent directories with -r). Omit when "
            "using --last/--session/--all.",
        ),
    ] = None,
    last: Annotated[
        bool,
        typer.Option(
            "--last",
            "--last-recording",
            help="Process the most recent recording folder (from the cache).",
        ),
    ] = False,
    session: Annotated[
        bool,
        typer.Option(
            "--session",
            "--last-session",
            help="Process every folder from the last GUI session.",
        ),
    ] = False,
    session_id: Annotated[
        str | None,
        typer.Option(
            "--session-id",
            help="Process every folder from one exact session id (what the GUI "
            "prints on exit).",
        ),
    ] = None,
    all_: Annotated[
        bool,
        typer.Option(
            "--all", help="Process every recording folder still in the cache."
        ),
    ] = False,
    recursive: Annotated[
        bool,
        typer.Option("-r", "--recursive", help="Recurse into the given folders."),
    ] = False,
    no_transcode: Annotated[
        bool,
        typer.Option(
            "--no-transcode",
            help="Skip transcoding; grid/transfer act on the existing *.mp4 files.",
        ),
    ] = False,
    no_grid: Annotated[
        bool,
        typer.Option("--no-grid", help="Skip building the visualization grid(s)."),
    ] = False,
    no_transfer: Annotated[
        bool,
        typer.Option(
            "--no-transfer", help=r"Skip transferring to the \[transfer] destination."
        ),
    ] = False,
    force: Annotated[
        bool,
        typer.Option(
            "--force",
            help="Re-transcode and rebuild grids even when the output already "
            "exists. By default existing transcode/grid outputs are skipped (as "
            "the transfer step skips files already at the destination).",
        ),
    ] = False,
    config_dir: Annotated[
        Path | None,
        typer.Option(
            "--config",
            "-C",
            exists=True,
            file_okay=False,
            dir_okay=True,
            help="Fallback config dir for recordings that lack an embedded "
            "octacam_config.toml (older recordings). Normally not needed.",
        ),
    ] = None,
    remove_source: Annotated[
        bool,
        typer.Option(
            "--remove-source",
            help="Delete each source .mkv/.raw once it transcodes successfully. "
            "The recording_summary.json is kept.",
        ),
    ] = False,
    progress_style: Annotated[
        ProgressStyle,
        typer.Option(
            "--progress-style",
            help="How to show transcode progress. octacam (default): an "
            "octacam-style bar. ffmpeg: stream ffmpeg's own output verbatim.",
        ),
    ] = ProgressStyle.octacam,
    dry_run: Annotated[
        bool,
        typer.Option(
            "--dry-run",
            help="For grid/transfer: log what would be done without running ffmpeg "
            "or copying files. Transcoding still runs normally.",
        ),
    ] = False,
) -> None:
    """Post-recording pipeline: transcode, build grids, and transfer recordings.

    Every setting — encoder args, grid layouts, transfer destination — is read
    from each recording's own octacam_config.toml (copied in at record time), so
    no --config is needed. All three steps run by default; disable any with
    --no-transcode / --no-grid / --no-transfer.

    Re-running is safe and resumes where it left off: each step skips outputs
    that already exist — a finished transcode .mp4, a built grid, or a file
    already at the transfer destination — so only missing work is redone. Pass
    --force to rebuild existing transcodes and grids anyway (e.g. after changing
    the encoder params or grid layout).

    Instead of PATHS, pass --last (the most recent recording), --session (the
    last GUI session), --session-id (an exact session), or --all (every cached
    folder). Deleted folders are silently skipped.
    """
    from octacam import session_cache
    from octacam.writer import is_partial_transcode, transcode_file

    do_transcode = not no_transcode
    do_grid = not no_grid
    do_transfer = not no_transfer
    if not (do_transcode or do_grid or do_transfer):
        sys.exit("Nothing to do: --no-transcode, --no-grid and --no-transfer all set.")

    folders = _resolve_transcode_paths(
        list(paths or []), last, session, session_id, all_
    )

    raw_output = progress_style is ProgressStyle.ffmpeg
    show_bar = not raw_output and _stderr_console().is_terminal
    # Which output mp4s exist per source folder, so grid/transfer run once per
    # folder after its files are done. Insertion-ordered (3.7+).
    folder_outputs: dict[Path, list[Path]] = {}
    cfg_cache: dict[Path, object] = {}
    failures = 0
    completed = 0
    skipped = 0
    interrupted = False

    # --- Transcode phase ----------------------------------------------------
    if do_transcode:
        jobs = _transcode_jobs(folders, recursive)
        if not jobs:
            log.warning("No videos to transcode in: %s", ", ".join(map(str, folders)))
        else:
            bar = _TranscodeProgressBar(len(jobs)) if show_bar else None
            with (
                session_cache.mark_transcode_active(f"{len(jobs)} file(s)"),
                bar or contextlib.nullcontext(),
            ):
                # A Ctrl-C stops the batch where it stands: transcode_file kills
                # its ffmpeg child and discards the partial output.
                try:
                    for index, job in enumerate(jobs, 1):
                        input_path = job.input_path
                        output = input_path.with_suffix(".mp4")
                        if output.resolve() == input_path.resolve():
                            log.warning(
                                "Skipping %s: already in target format (mp4)",
                                input_path,
                            )
                            folder_outputs.setdefault(input_path.parent, []).append(
                                output
                            )
                            continue
                        folder = input_path.parent
                        if output.exists() and not force:
                            # Idempotent re-run: a finished .mp4 already sits at
                            # the target. Transcoding is atomic (temp + rename),
                            # so its presence means a complete encode — skip
                            # re-encoding, but still feed it to grid/transfer.
                            # Counted for the summary rather than logged per file
                            # so a full re-run doesn't spam one line per output.
                            skipped += 1
                            folder_outputs.setdefault(folder, []).append(output)
                            continue
                        cfg = cfg_cache.get(folder)
                        if cfg is None:
                            cfg = _config_for_recording(folder, config_dir)
                            cfg_cache[folder] = cfg
                        on_progress = bar.file(index, input_path) if bar else None
                        try:
                            result = transcode_file(
                                input_path,
                                output,
                                ffmpeg_params=cfg.transcode.ffmpeg_params,
                                width=job.width,
                                height=job.height,
                                fps=job.fps,
                                pixel_format=job.pixel_format,
                                frames=job.frames,
                                total_frames=job.frames,
                                on_progress=on_progress,
                                raw_output=raw_output,
                            )
                            typer.echo(result)
                        except Exception as e:  # one bad file must not abort the batch
                            failures += 1
                            log.error("Failed to transcode %s: %s", input_path, e)
                            continue
                        completed += 1
                        folder_outputs.setdefault(folder, []).append(output)
                        if remove_source:
                            _remove_source_files(input_path)
                except KeyboardInterrupt:
                    interrupted = True
            if not interrupted:
                log.info(
                    "Transcode: %d done, %d skipped, %d failed%s",
                    completed,
                    skipped,
                    failures,
                    " (use --force to re-transcode existing)"
                    if skipped and not force
                    else "",
                )
    else:
        # No transcode: grid/transfer act on the mp4s already present, ignoring
        # any orphaned partial (.octacam-part) temp a hard kill may have left.
        for folder in _find_recording_dirs(folders, recursive):
            folder_outputs.setdefault(
                folder,
                sorted(p for p in folder.glob("*.mp4") if not is_partial_transcode(p)),
            )

    # --- Grid + transfer phases ---------------------------------------------
    transfer_failed = 0
    if not interrupted and (do_grid or do_transfer) and folder_outputs:
        transfer_failed = _grid_and_transfer(
            folder_outputs, do_grid, do_transfer, config_dir, dry_run, show_bar, force
        )

    # Report outside the `with` so messages land after the live bar is gone.
    if interrupted:
        log.warning(
            "Interrupted — stopped after %d file(s); the in-progress transcode "
            "was discarded.",
            completed,
        )
        raise typer.Exit(130)  # 128 + SIGINT, the shell convention for Ctrl-C
    problems = []
    if failures:
        problems.append(f"{failures} file(s) failed to transcode")
    if transfer_failed:
        problems.append(f"{transfer_failed} file(s) failed to transfer")
    if problems:
        sys.exit("; ".join(problems))


def _remove_source_files(input_path: Path) -> None:
    """Delete a transcoded source (.mkv/.raw) once it has been transcoded.

    Never removes the recording_summary.json. Deletion failures are logged but
    never fail the run (the transcode already succeeded)."""
    try:
        input_path.unlink(missing_ok=True)
    except OSError as e:
        log.warning("Could not remove %s: %s", input_path, e)


def main() -> None:
    from rich.traceback import install

    install(show_locals=False)
    app()


if __name__ == "__main__":
    main()
