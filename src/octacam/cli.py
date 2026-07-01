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
    `list-cameras`/`record`/`transcode`."""
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
        "(e.g. --plugin flywheel). See `octacam list-plugins`.",
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


@app.command("list-cameras")
def list_cameras(
    backend: Annotated[
        str,
        typer.Option(help="Camera backend to enumerate: basler, flir, or fake."),
    ] = "basler",
) -> None:
    """List detected cameras (set PYLON_CAMEMU=N for emulated Basler ones)."""
    if backend == "basler":
        from pypylon import pylon

        devices = pylon.TlFactory.GetInstance().EnumerateDevices()
        if not devices:
            typer.echo("No cameras detected.")
            return
        for device in devices:
            typer.echo(f"{device.GetModelName()}\t{device.GetSerialNumber()}")
        return

    from octacam.cameras import BackendUnavailable, select_backend

    try:
        enumerate_fn, _factory, _extension = select_backend(backend)
        entries = enumerate_fn(None)
    except BackendUnavailable as e:
        sys.exit(str(e))
    if not entries:
        typer.echo("No cameras detected.")
        return
    for serial, _handle in entries:
        typer.echo(f"{serial}\t{backend}")


@app.command("list-plugins")
def list_plugins() -> None:
    r"""List the bundled, opt-in plugins and whether each can load.

    Output is `name<TAB>status<TAB>summary`. `available` means the plugin's
    dependencies are present (the bundled plugins' deps ship by default);
    `unavailable` lines carry the reason. Enable one with `--plugin NAME` on
    `gui`/`record`, or a `\[\[plugins]]` entry in the rig config.
    """
    from octacam.plugins import available_plugins

    infos = available_plugins()
    if not infos:
        typer.echo("No plugins bundled.")
        return
    for info in infos:
        status = "available" if info.available else "unavailable"
        summary = info.summary
        if not info.available and info.detail:
            summary = f"{summary} [{info.detail}]" if summary else info.detail
        typer.echo(f"{info.name}\t{status}\t{summary}")


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
        with grid_bar or contextlib.nullcontext():
            for i, folder in enumerate(folder_outputs, 1):
                cfg = folder_cfgs[folder]
                summary = _read_summary(folder / RECORDING_SUMMARY_FILENAME) or {}
                summary_cams = [
                    c.get("name") for c in summary.get("cameras", []) if c.get("name")
                ]
                built: list[Path] = []
                for name, layout, ff in _visualizations_for(cfg, summary_cams):
                    on_prog = grid_bar.folder(i, folder) if grid_bar else None
                    out = build_grid_video(
                        folder,
                        layout=layout,
                        output=folder / name,
                        ffmpeg_params=ff or cfg.transcode.ffmpeg_params,
                        dry_run=dry_run,
                        on_progress=on_prog,
                    )
                    if out is not None:
                        built.append(out)
                grid_files[folder] = built

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
        if not dry_run:
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

    Instead of PATHS, pass --last (the most recent recording), --session (the
    last GUI session), --session-id (an exact session), or --all (every cached
    folder). Deleted folders are silently skipped.
    """
    from octacam import session_cache
    from octacam.writer import transcode_file

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
    else:
        # No transcode: grid/transfer act on the mp4s already present.
        for folder in _find_recording_dirs(folders, recursive):
            folder_outputs.setdefault(folder, sorted(folder.glob("*.mp4")))

    # --- Grid + transfer phases ---------------------------------------------
    transfer_failed = 0
    if not interrupted and (do_grid or do_transfer) and folder_outputs:
        transfer_failed = _grid_and_transfer(
            folder_outputs, do_grid, do_transfer, config_dir, dry_run, show_bar
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
