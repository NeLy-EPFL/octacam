import contextlib

with contextlib.suppress(ImportError):
    import pypylon.pylon  # noqa: F401

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
from pathlib import Path
from typing import TYPE_CHECKING, Annotated

from octacam._compat import StrEnum

if TYPE_CHECKING:
    from collections.abc import Callable

    from rich.progress import TaskID

    from octacam.config import RecordConfig
    from octacam.controller import RecordingSettings
    from octacam.process_jobs import JobReporter
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

# `octacam jobs …` manages detached `octacam process --detach` jobs. Kept as a
# sub-group (its own verbs) rather than more flags on `process`.
jobs_app = typer.Typer(
    no_args_is_help=True,
    help="Manage detached `octacam process` jobs (list, attach, pause, cancel).",
    context_settings={"help_option_names": ["-h", "--help"]},
)
app.add_typer(jobs_app, name="jobs")

# `octacam cache …` inspects and clears octacam's on-disk cache under
# ~/.cache/octacam (the recording list, detached-job logs, activity markers).
cache_app = typer.Typer(
    no_args_is_help=True,
    help="Inspect and clear the octacam cache (recording list, job logs, markers).",
    context_settings={"help_option_names": ["-h", "--help"]},
)
app.add_typer(cache_app, name="cache")


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
        preview_trigger_source=record.preview_trigger_source,
        save_method=record.save_method,
        ffmpeg_params=record.ffmpeg_params,
        nvenc_params=record.nvenc_params,
        max_nvenc_sessions=record.max_nvenc_sessions,
        writer_queue_size=record.writer_queue_size,
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
        "  last session:  octacam process --last session\n"
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
            "%d octacam process run%s transcoding on this machine. A detached job "
            "auto-pauses while these cameras run and resumes when they are free; a "
            "foreground `octacam process` does not — it may slow capture (risking "
            "dropped frames).",
            count,
            " is" if count == 1 else "s are",
        )


def _finish_gui_session(session_id: str, config_dir: Path, process_after: bool) -> None:
    """On GUI shutdown, either kick off detached processing or print the hints.

    If the operator chose "shut down & process" and this session actually recorded
    something, start a detached `octacam process --session-id …` job (which they
    reattach to from a terminal). Best-effort: any failure falls back to printing
    the ready-to-run hints and never raises during teardown.
    """
    from octacam import process_jobs, session_cache

    if process_after:
        try:
            folders = session_cache.session_folders(session_id)
            if not folders:
                log.info("Nothing was recorded this session; not starting processing.")
                return
            status = process_jobs.spawn_detached(
                argv_tail=["--session-id", session_id, "--config", str(config_dir)],
                folders=folders,
                session_id=session_id,
            )
            log.info(
                "Started detached processing job %s for this session. "
                "Reattach with: octacam jobs attach %s",
                status.job_id,
                status.job_id,
            )
            return
        except Exception:
            log.exception("Could not start detached processing; printing hints instead")
    _print_transcode_hints(session_id)


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
    """octacam: preview, record, and save video streams from multiple cameras.

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

    plugins = build_plugins(config, _resolve_enabled(enabled_plugins, no_plugins))

    settings = _settings_from_record(config.record, config.transcode, config.transfer)
    # One session id for this GUI run; every recording made before shutdown is
    # tagged with it in the session cache so `octacam process --last session`
    # can find the whole batch later (and we print the commands on the way out).
    session_id = session_cache.new_session_id()
    # Serve the page *before* touching hardware: the controller starts holding a
    # hardware-free placeholder system (ready=False) so uvicorn can bind and the
    # browser render the shell immediately. The slow work — opening the cameras,
    # arming the serial plugins, starting preview — runs on the init thread below
    # and is swapped in via controller.attach_system, which pushes the filled-in
    # system to every connected browser.
    system = CameraSystem.pending(config.backend)
    controller = RecordingController(
        system,
        settings,
        plugins,
        session_id=session_id,
        config_dir=config_dir,
        ready=False,
    )
    # Assigned by create_app() inside the try so a failure there still runs the
    # finally (controller.close + plugins.teardown_all); the init closure and the
    # finally read it lazily and tolerate None.
    app = None

    capture_stack = contextlib.ExitStack()
    # Set on shutdown so a still-running init bails before arming hardware the
    # teardown is about to release (join() below also serializes the two).
    stopping = threading.Event()

    def _initialize_rig() -> None:
        """Open the cameras + arm the plugins (in parallel), load params, start
        preview, then push the ready system to any connected browser.

        Runs on a daemon thread so ``octacam gui`` serves the page immediately.
        A failure here surfaces to the GUI (an error event + placeholder) and the
        log rather than aborting the already-running server."""
        from concurrent.futures import ThreadPoolExecutor

        app_state = getattr(getattr(app, "state", None), "app_state", None)

        def _publish() -> None:
            # Push the current system descriptor + state to every connected
            # browser, on success OR failure. On failure this still matters: the
            # serial plugins arm in parallel with the camera open and may have
            # succeeded, so their tabs must flip to ready even though the grid
            # shows the init error — otherwise an armed board reads as
            # "unavailable" until a manual reload (the WS-connect handshake, sent
            # before setup_all finished, reported it not-ready).
            if app_state is not None:
                app_state.broadcast_system()
            controller.notify_state()

        def _open_cameras() -> CameraSystem:
            opened = CameraSystem(
                [c.serial_number for c in config.cameras], backend=config.backend
            )
            if len(opened) == 0:
                opened.close()
                raise BackendError("no cameras were opened")
            names = {c.serial_number: c.name for c in config.cameras if c.name}
            for camera in opened:
                camera.name = names.get(camera.serial_number, camera.name)
            opened.load_config(config_dir)
            opened.apply_display_config(config.cameras)
            return opened

        opened_system: CameraSystem | None = None
        try:
            # Cameras (USB) and serial plugins are independent hardware, so open
            # and arm them concurrently — startup is bounded by the slower of the
            # two, not their sum. plugins.setup_all logs and swallows its own
            # errors; the ThreadPoolExecutor's exit waits for it even if the
            # camera open raises first.
            with ThreadPoolExecutor(max_workers=2, thread_name_prefix="rig-init") as ex:
                cam_future = ex.submit(_open_cameras)
                ex.submit(plugins.setup_all)
                opened_system = cam_future.result()
        except BackendUnavailable as e:
            controller.fail_init(str(e))
            _publish()
            return
        except BackendError as e:
            # Most often another octacam already holds the cameras (vendor SDKs
            # open USB3 devices exclusively), or one is disconnected.
            controller.fail_init(
                f"Could not open the cameras: {e}. They may already be in use by "
                "another octacam instance on this rig, or disconnected — only one "
                "process can open them at a time."
            )
            _publish()
            return
        except Exception as e:  # never let the init thread die silently
            log.exception("Camera initialization failed")
            controller.fail_init(f"Camera initialization failed: {e}")
            _publish()
            return

        if stopping.is_set():
            # Shutdown began while we were opening; release rather than arm
            # hardware the finally is about to close (it join()s us first). Don't
            # publish — we're on our way down.
            with contextlib.suppress(Exception):
                opened_system.close()
            return

        controller.attach_system(opened_system)
        log.info("Opened %d camera(s)", len(opened_system))
        try:
            controller.start_preview()
        except Exception:
            log.exception("Failed to start live preview")
        # Fill in the already-served GUI: push the now-ready system descriptor +
        # a fresh state so a browser that loaded against the placeholder shows the
        # grid (and enables camera controls) without a reload.
        _publish()

    init_thread = threading.Thread(
        target=_initialize_rig, name="octacam-init", daemon=True
    )
    try:
        # Build the app first (inside the try so a failure still hits the finally,
        # though no hardware is armed yet); the init closure reads `app` lazily.
        app = create_app(controller, config, plugins, config_dir=str(config_dir))
        # While this GUI owns the cameras, publish a capture-active marker so any
        # detached `octacam process` job on this machine pauses (it resumes when
        # we exit). Best-effort; released in the finally before we might kick off
        # a new job on shutdown.
        capture_stack.enter_context(session_cache.mark_capture_active("gui session"))
        # Kick off the hardware init in the background, then serve immediately.
        init_thread.start()
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
        # ws_ping_timeout: uvicorn's websocket keepalive pings the browser and
        # drops the socket if no pong returns within this window. That same
        # socket also carries the preview stream (up to ~8 cameras worth of JPEG
        # frames at the display refresh rate), so over a slow `ssh -L` tunnel the
        # link can stay congested long enough that the default 20s pong wait
        # elapses and a perfectly live GUI is killed with a 1011 "keepalive ping
        # timeout". Give the pong a generous window; the ping still runs at the
        # default interval, so a genuinely dead (half-open) client is reaped and
        # its preview encoding stops.
        # ws="websockets-sansio": uvicorn's default "auto" still selects its
        # legacy websockets implementation, which imports the deprecated
        # websockets.legacy / WebSocketServerProtocol APIs and prints
        # DeprecationWarnings on startup and on every connection (removed in a
        # future websockets release). The sansio implementation is the supported
        # successor and speaks the same protocol to the browser.
        uvicorn.run(
            app,
            host=host,
            port=port,
            log_level="warning",
            ws="websockets-sansio",
            ws_ping_timeout=60.0,
        )
    finally:
        # Runs on Ctrl+C, on the /api/shutdown self-signal, and on errors.
        # Cleanup can take a moment (finalizing recordings, draining ffmpeg,
        # closing cameras), so bracket it with messages.
        log.info("Shutting down — finalizing recordings and releasing cameras…")
        # Tell a still-running init thread to skip arming, then wait for it: the
        # join makes its attach_system/start_preview happen-before the close
        # below, so the two never race over the cameras. Bounded so a wedged SDK
        # open can't hang shutdown forever.
        stopping.set()
        if init_thread.ident is not None:  # skip if create_app raised before start
            init_thread.join(timeout=30)
        controller.close()
        plugins.teardown_all()
        # Release the single-instance lock so a relaunch is not briefly blocked
        # while this process lingers; the OS would also drop it on exit.
        instance_lock.close()
        # Drop the capture-active marker before a kicked-off job starts, so it
        # runs unpaused rather than parking on our just-released cameras.
        capture_stack.close()
        # The shutdown button can ask us to start processing this session's
        # recordings on the way out (POST /api/shutdown {process_after}); else we
        # just print the ready-to-run `octacam process` hints.
        state = getattr(getattr(app, "state", None), "app_state", None)
        process_after = bool(getattr(state, "process_after", False))
        _finish_gui_session(session_id, config_dir, process_after)
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

    ``"auto"`` (or an empty selector) sweeps the available backend cascade and
    returns each camera once, claimed by the highest-priority tier that sees it
    (so a Basler served by the vendor tier is not also listed under the pycameleon
    floor) — mirroring how :class:`CameraSystem` opens them. Basler goes through
    the pylon TL factory directly so model names come along; every other backend
    exposes an optional module-level ``read_model(handle)`` (the model-name
    analogue of its serial read, from the same pre-Init transport-layer node), so
    doctor can label its cameras too — a backend without one simply yields
    ``None``. Enumeration never opens/grabs a device, so this is safe alongside a
    live session."""
    import importlib

    from octacam.cameras import select_backend

    key = (name or "auto").strip().lower()
    if key in ("auto", "all", ""):
        return [(serial, model) for serial, _backend, model in _cascade_assignment()]
    if key == "basler":
        from pypylon import pylon

        devices = pylon.TlFactory.GetInstance().EnumerateDevices()
        return [(str(d.GetSerialNumber()), str(d.GetModelName())) for d in devices]
    enumerate_fn, _factory, _extension = select_backend(name)
    read_model = getattr(importlib.import_module(enumerate_fn.__module__), "read_model", None)

    def _model(handle) -> str | None:
        if read_model is None:
            return None
        try:
            return read_model(handle)
        except Exception:  # a model read must never fail enumeration
            return None

    return [(str(serial), _model(handle)) for serial, handle in enumerate_fn(None)]


def _cascade_assignment() -> list[tuple[str, str, str | None]]:
    """``[(serial, backend, model|None), ...]``: the tier that would claim each camera.

    Sweeps the available cascade in priority order and claims each serial for the
    first tier that enumerates it, so the result shows exactly which backend
    :class:`CameraSystem` would open each detected camera through under ``auto``.
    A backend whose enumeration fails is skipped."""
    from octacam.cameras.registry import available_backends

    claimed: dict[str, tuple[str, str | None]] = {}
    order: list[str] = []
    for backend in available_backends():
        try:
            cams = _enumerate_backend(backend)
        except Exception:
            continue
        for serial, model in cams:
            if serial in claimed:
                continue
            claimed[serial] = (backend, model)
            order.append(serial)
    return [(serial, claimed[serial][0], claimed[serial][1]) for serial in order]


class _CameraScan:
    """Enumerate each relevant camera backend exactly ONCE, concurrently.

    ``octacam doctor`` used to enumerate the backends ~3× per run — the
    Camera-backends sweep, the cascade-selection line, and the cameras-vs-config
    cross-check each re-enumerated — and every re-entry into a vendor SDK paid a
    steep re-init cost (``enumerate_spinnaker`` releases and re-acquires the
    Spinnaker ``System`` on each call, ~2.4 s), so the redundancy dominated the
    whole command (~11 s). This scans every backend the report will show once,
    caches the result, and serves the three consumers (:meth:`get`,
    :meth:`cascade`, :meth:`detected_serials`) from that single pass.

    The scan runs the per-backend enumerations on worker threads, but every SDK is
    imported first on the *calling* thread (the ``select_backend`` gate in
    ``__init__``), so no two cold vendor-SDK imports race the Python import lock;
    the worker threads only run the device scan itself. The slowest single backend
    is the wall-clock floor — e.g. ``enumerate_spinnaker``'s ~2.4 s ``System``
    re-init — but the tiers now overlap instead of running back-to-back.

    Enumeration never opens a camera, so this is safe alongside a live session.
    """

    def __init__(self, only_backend: str | None) -> None:
        from octacam.cameras.registry import BACKENDS, CASCADE, select_backend

        key = (only_backend or "").strip().lower()
        self.only = key if key and key not in ("auto", "all") else None
        # The backends doctor's Camera-backends section enumerates: a single
        # explicit --backend, else every real tier (fake is synthetic, named-only).
        display = [self.only] if self.only else [b for b in BACKENDS if b != "fake"]
        # Gate on the main thread: select_backend triggers the SDK import / CDLL
        # load, so the workers only do the (I/O-bound, GIL-releasing) device scan
        # and no two cold imports ever race. An unavailable tier is dropped here
        # and reported as "info" by _doctor_backends' own select_backend pass.
        self.targets: list[str] = []
        for name in display:
            try:
                select_backend(name)
            except Exception:
                continue
            self.targets.append(name)
        # Cascade selection order (priority) is CASCADE restricted to the tiers we
        # scanned.
        self._cascade_order = [b for b in CASCADE if b in self.targets]
        self._cams: dict[str, list[tuple[str, str | None]]] = {}
        self._errs: dict[str, Exception] = {}

    def run(
        self, on_done: "Callable[[str, Exception | None], None] | None" = None
    ) -> None:
        """Enumerate every target once, concurrently; cache results/exceptions.

        ``on_done(name, err)`` fires as each backend finishes — on THIS thread (the
        ``as_completed`` loop runs on the caller), so updating a rich ``Progress``
        from it is safe. Calls the module-level :func:`_enumerate_backend` by name
        so a test can count enumerations by monkeypatching it.
        """
        from concurrent.futures import ThreadPoolExecutor, as_completed

        if not self.targets:
            return
        with ThreadPoolExecutor(max_workers=max(1, len(self.targets))) as pool:
            futures = {
                pool.submit(_enumerate_backend, name): name for name in self.targets
            }
            for future in as_completed(futures):
                name = futures[future]
                try:
                    self._cams[name] = future.result()
                    err: Exception | None = None
                except Exception as exc:  # native SDK / subprocess enumerate failure
                    self._errs[name] = exc
                    err = exc
                if on_done is not None:
                    on_done(name, err)

    def get(self, name: str) -> list[tuple[str, str | None]]:
        """Cached ``[(serial, model), ...]`` for a scanned backend.

        Re-raises a stored enumeration failure so the caller reports it exactly as
        the pre-scan direct ``_enumerate_backend`` call did. Case-insensitive to
        match ``select_backend``/``_enumerate_backend`` — the cache is keyed by the
        normalized name, so ``--backend BASLER`` resolves like ``basler``."""
        key = name.strip().lower()
        if key in self._errs:
            raise self._errs[key]
        return self._cams[key]

    def cascade(self) -> list[tuple[str, str, str | None]]:
        """``[(serial, backend, model), ...]``: the tier that claims each camera.

        Mirrors :func:`_cascade_assignment` but reads the single scan instead of
        re-enumerating; a tier whose scan failed is skipped, never re-raised."""
        claimed: dict[str, tuple[str, str | None]] = {}
        order: list[str] = []
        for backend in self._cascade_order:
            if backend in self._errs:
                continue
            for serial, model in self._cams.get(backend, []):
                if serial in claimed:
                    continue
                claimed[serial] = (backend, model)
                order.append(serial)
        return [(s, claimed[s][0], claimed[s][1]) for s in order]

    def detected_serials(self, backend: str | None) -> set[str]:
        """Serials to cross-check the config against, mirroring ``_enumerate_backend``.

        ``auto`` uses the cascade; a specific backend is served from the scan when
        it was a target, else enumerated live once (a config that pins an
        uninstalled backend — the live call raises and the caller warns, as before)."""
        key = (backend or "auto").strip().lower()
        if key in ("auto", "all", ""):
            return {serial for serial, _backend, _model in self.cascade()}
        if key in self._cams or key in self._errs:
            return {serial for serial, _model in self.get(key)}
        return {serial for serial, _model in _enumerate_backend(key)}


def _run_scan_with_progress(scan: _CameraScan, quiet: bool) -> None:
    """Run the parallel backend scan behind a live per-backend spinner.

    The report renders on stdout; this spinner lives on the stderr console, so it
    never corrupts ``--json`` output or a piped report, and it is suppressed when
    stderr is not a terminal or ``--json`` was requested. ``transient=True`` clears
    it before the report prints. The work (``scan.run``) executes even when the
    display is disabled — a disabled ``Progress`` just makes the updates no-ops."""
    from rich.progress import Progress, SpinnerColumn, TextColumn

    console = _stderr_console()
    disable = quiet or not console.is_terminal
    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        console=console,
        transient=True,
        disable=disable,
    ) as progress:
        tasks = {
            name: progress.add_task(f"enumerating {name}…", total=1)
            for name in scan.targets
        }

        def on_done(name: str, err: "Exception | None") -> None:
            if err is not None:
                desc = f"{name}: enumeration failed"
            else:
                desc = f"{name}: {len(scan.get(name))} camera(s)"
            progress.update(tasks[name], completed=1, description=desc)

        scan.run(on_done=on_done)


def _run_ffmpeg_probe(exe: str, args: list[str]) -> str:
    """Run a fast, read-only ffmpeg query and return its combined output ("" on error)."""
    try:
        # stdin=DEVNULL keeps even these non-encoding queries off the controlling
        # tty, so a timeout kill can never leave the terminal in no-echo mode.
        out = subprocess.run(
            [exe, *args], stdin=subprocess.DEVNULL, capture_output=True,
            text=True, timeout=10,
        )  # fmt: skip
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


def _nvidia_gpus() -> list[str]:
    """Detected NVIDIA GPUs as "<name> (driver <ver>)", via nvidia-smi.

    Empty when no NVIDIA driver/GPU is present (nvidia-smi missing or failing) —
    the signal that GPU (NVENC) encoding is unavailable on this host."""
    if not shutil.which("nvidia-smi"):
        return []
    try:
        out = subprocess.run(
            [
                "nvidia-smi",
                "--query-gpu=name,driver_version",
                "--format=csv,noheader",
            ],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    if out.returncode != 0:
        return []
    gpus: list[str] = []
    for line in out.stdout.splitlines():
        parts = [p.strip() for p in line.split(",")]
        if not parts or not parts[0]:
            continue
        if len(parts) >= 2:
            gpus.append(f"{parts[0]} (driver {parts[1]})")
        else:
            gpus.append(parts[0])
    return gpus


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
    _doctor_updates(report)


def _doctor_updates(report: _Report) -> None:
    """Add one line to the System section: is a newer octacam release available?

    Read-only and fail-soft — octacam never updates itself, this only advises the
    correct command for the install (see octacam.updates). Stays silent/graceful
    (a dev install or a pre-PyPI package returns no signal), and a slow/unreachable
    PyPI never fails the command.
    """
    from octacam import updates

    notice = updates.check()
    if notice.update_available and notice.latest:
        msg = f"octacam {notice.latest} is available (you have {notice.current})"
        if notice.command:
            msg += f" — update with: {notice.command}"
        report.add("warn", msg)
    elif notice.latest:
        report.add("ok", f"octacam {notice.current} is the latest release")
    else:
        report.add("info", f"update check skipped ({notice.note})")


def _camera_lines(cams: "list[tuple[str, str | None]]") -> list[str]:
    """Collapse ``[(serial, model), ...]`` into compact ``model: s1, s2, …`` lines.

    One line per distinct model (first-seen order), serials comma-joined, so four
    same-model cameras read as a single line instead of four. Cameras whose model
    is unknown fall back to a bare serial line each."""
    groups: dict[str | None, list[str]] = {}
    for serial, model in cams:
        groups.setdefault(model, []).append(serial)
    lines: list[str] = []
    for model, serials in groups.items():
        if model:
            lines.append(f"{model}: {', '.join(serials)}")
        else:
            lines.extend(serials)  # unknown model → bare serial per line
    return lines


# USB vendor IDs of the USB3-Vision camera makers octacam drives. Used by the
# doctor USB-link-speed check to spot a camera without opening it; extend as more
# vendors are used (the pycameleon floor also matches any detected serial).
_CAMERA_USB_VENDORS = {"2676": "Basler", "1e10": "FLIR"}
_SUPERSPEED_MBPS = 5000


def _usb_camera_links(
    detected_serials: "set[str]", root: Path = Path("/sys/bus/usb/devices")
) -> "list[tuple[str, str, int]]":
    """``[(serial, product, speed_mbps), ...]`` for connected camera USB devices.

    Reads sysfs (``<root>/*/{idVendor,serial,product,speed}``) so it needs no
    device open — safe during a live session and backend-agnostic. A device
    counts as a camera if its vendor is a known camera maker or its serial
    matches one octacam detected. Returns ``[]`` on any platform without that
    sysfs layout (doctor then simply skips the link-speed check)."""

    def _read(dev: Path, field: str) -> str:
        try:
            return (dev / field).read_text().strip()
        except OSError:
            return ""

    out: list[tuple[str, str, int]] = []
    seen: set[str] = set()
    try:
        devices = sorted(root.iterdir())
    except OSError:
        return out
    for dev in devices:
        serial = _read(dev, "serial")
        if not serial or serial in seen:
            continue
        if _read(dev, "idVendor") not in _CAMERA_USB_VENDORS and serial not in detected_serials:
            continue
        try:
            speed = int(float(_read(dev, "speed")))
        except ValueError:
            continue  # no/garbled speed node (e.g. a non-USB match); skip it
        seen.add(serial)
        out.append((serial, _read(dev, "product"), speed))
    return out


def _doctor_backends(
    report: _Report, only_backend: str | None, scan: _CameraScan
) -> None:
    from octacam.cameras import BackendUnavailable
    from octacam.cameras.registry import BACKENDS, select_backend

    report.section("Camera backends")
    # `fake` is a synthetic test/CI backend that always reports FAKE-* serials
    # regardless of hardware, so it is only enumerated when explicitly requested
    # (--backend fake) — never in the default all-backends sweep.
    backends = (
        (only_backend,) if only_backend else tuple(b for b in BACKENDS if b != "fake")
    )
    detected_serials: set[str] = set()
    for name in backends:
        try:
            select_backend(name)
        except BackendUnavailable as e:
            # A backend whose SDK isn't installed is expected — report it as info,
            # not a warning (the cascade simply skips that tier).
            report.add("info", str(e))
            continue
        except Exception as e:
            report.add("warn", f"{name}: could not select backend ({e})")
            continue
        try:
            # Served from the single parallel scan; a select_backend success above
            # guarantees this name was a scan target (see _CameraScan).
            cams = scan.get(name)
        except Exception as e:
            report.add("warn", f"{name}: available, but enumeration failed ({e})")
            continue
        report.add("ok", f"{name}: available — {len(cams)} camera(s) detected")
        detected_serials.update(serial for serial, _model in cams)
        for line in _camera_lines(cams):
            report.add("list", line)
    # USB link-speed check: a USB3 camera whose SuperSpeed link fails to train
    # falls back to USB 2.0 (480 Mb/s) and then fails to open — the error the GUI
    # reports at open time. doctor never opens a camera, so surface it from the
    # negotiated link speed in sysfs, visible without touching a device.
    for serial, product, speed in _usb_camera_links(detected_serials):
        if speed >= _SUPERSPEED_MBPS:
            continue
        label = f"{product} {serial}" if product else serial
        usb2 = " (USB 2.0)" if speed == 480 else ""
        report.add(
            "warn",
            f"{label} is linked at only {speed} Mb/s{usb2}, not USB 3 SuperSpeed "
            f"({_SUPERSPEED_MBPS} Mb/s) — it will fail to open. A USB3 camera whose "
            "SuperSpeed link fails to train drops back to USB 2.0 even in a USB 3 "
            "port; check/replace its cable, reseat it, or try another USB 3 port.",
        )
    if not only_backend and os.environ.get("PYLON_CAMEMU"):
        report.add(
            "info",
            f"PYLON_CAMEMU={os.environ['PYLON_CAMEMU']} (emulated Basler cameras)",
        )
    # Under "auto" a camera may be seen by several tiers; show which one wins.
    if not only_backend:
        assignment = scan.cascade()
        if assignment:
            report.add("info", "cascade selection (backend each camera opens through):")
            # Group by (backend, model) in first-seen order so same-model cameras
            # on one tier collapse to a single "model: s1, s2 → backend" line.
            grouped: dict[tuple[str, str | None], list[str]] = {}
            for serial, backend, model in assignment:
                grouped.setdefault((backend, model), []).append(serial)
            for (backend, model), serials in grouped.items():
                joined = ", ".join(serials)
                label = f"{model}: {joined}" if model else joined
                report.add("list", f"{label} → {backend}")


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
    _doctor_gpu_encoding(report)


def _doctor_gpu_encoding(report: _Report) -> None:
    """Report GPU (NVIDIA NVENC) encode availability — the opt-in save_method="nvenc"."""
    from octacam.writer import (
        NVENC_H264_PARAMS,
        find_ffmpeg,
        probe_nvenc_max_sessions,
    )

    gpus = _nvidia_gpus()
    if not gpus:
        report.add(
            "info",
            'no NVIDIA GPU detected (nvidia-smi) — GPU encoding unavailable; '
            'save_method="nvenc" would fall back to CPU (libx264)',
        )
        return
    for gpu in gpus:
        report.add("ok", f"NVIDIA GPU: {gpu}")
    try:
        nvexe = find_ffmpeg(require_encoder="h264_nvenc")
    except RuntimeError:
        report.add(
            "warn",
            'no ffmpeg with a working h264_nvenc encoder found — GPU encoding '
            "unavailable (the bundled imageio-ffmpeg has no NVENC, and a system "
            "ffmpeg's NVENC needs an API version the driver supports). Install a "
            'system ffmpeg built with NVENC; until then save_method="nvenc" '
            "falls back to CPU (libx264).",
        )
        return
    report.add(
        "ok",
        f"h264_nvenc works via {_ffmpeg_version(nvexe) or 'ffmpeg'} at {nvexe}",
    )
    sessions = probe_nvenc_max_sessions()
    if sessions is not None:
        suffix = "+ (probe ceiling)" if sessions >= 12 else ""
        report.add(
            "info",
            f"concurrent NVENC sessions detected: {sessions}{suffix} "
            "(cameras beyond record.max_nvenc_sessions encode on CPU)",
        )
    report.add("info", f"NVENC record params: {NVENC_H264_PARAMS}")


def _doctor_config(report: _Report, config_dir: Path):
    from octacam._compat import tomllib
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


def _doctor_cameras_vs_config(
    report: _Report, cfg, only_backend: str | None, scan: _CameraScan
) -> None:
    report.section("Cameras vs config")
    declared = [c.serial_number for c in cfg.cameras]
    if not declared:
        report.add("info", "config declares no serials; all detected cameras are used")
        return
    backend = only_backend or cfg.backend
    where = "across all backends" if backend in ("auto", "all", "") else f"on {backend}"
    try:
        detected = scan.detected_serials(backend)
    except Exception as e:
        report.add("warn", f"could not enumerate {where} to cross-check ({e})")
        return
    missing = [s for s in declared if s not in detected]
    extra = sorted(detected - set(declared))
    if not missing:
        report.add("ok", f"all {len(declared)} declared camera(s) detected {where}")
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


def _plugin_default_device(name: str) -> str | None:
    """The ``DEFAULT_DEVICE`` a serial plugin falls back to (None if unknown)."""
    import importlib

    try:
        mod = importlib.import_module(f"octacam.plugins.{name}")
        return getattr(mod, "DEFAULT_DEVICE", None)
    except Exception:
        return None


def _configured_device(pc) -> tuple[str | None, bool]:
    """``(device, is_auto)`` for a plugin config, mirroring the factory logic.

    Returns the device string the plugin would use (explicit ``device`` option
    or the plugin's ``DEFAULT_DEVICE``), and whether it is ``"auto"`` (dynamic)."""
    from octacam import plugins as plugins_mod

    name = plugins_mod._ALIASES.get(pc.name, pc.name)
    raw = pc.options.get("device")
    if isinstance(raw, str) and raw.strip().lower() == "auto":
        return None, True
    device = str(raw) if raw else _plugin_default_device(name)
    return device, False


def _doctor_serial(report: _Report, cfg, probe: bool = False) -> None:
    """List connected serial/Arduino devices and cross-check plugin ports.

    Passive by default (never opens a port), so it is safe alongside a live
    session. ``probe=True`` (``--probe-serial``) additionally reads each board's
    firmware identity, skipping any port already held by a running session."""
    from octacam import serial_ports as sp

    report.section("Serial devices")
    ports = sp.list_serial_ports()
    if sp.comports is None:
        report.add("warn", "pyserial not available; cannot enumerate serial ports")
        return
    if not ports:
        report.add("info", "no serial ports detected")

    # Microcontroller-class ports (Arduino/ESP/bridge chips) are the interesting
    # ones; a host can have dozens of legacy /dev/ttyS* — collapse those to one
    # line so the report stays readable.
    mcus = [p for p in ports if p.likely_microcontroller]
    generic = [p for p in ports if not p.likely_microcontroller]
    for p in mcus:
        sn = f"  sn={p.serial_number}" if p.serial_number else ""
        line = f"{p.board_name}  {p.device}  [{p.vid_pid}]{sn}"
        # Highlight boards that are (almost certainly) an Arduino with a marker.
        report.add("info" if p.likely_arduino else "list", line)
    if generic:
        shown = ", ".join(p.device for p in generic[:4])
        more = f", … (+{len(generic) - 4} more)" if len(generic) > 4 else ""
        report.add("list", f"{len(generic)} other/generic serial port(s): {shown}{more}")

    _doctor_serial_vs_config(report, cfg, ports)
    if probe:
        _doctor_serial_probe(report, cfg, mcus)


def _doctor_serial_vs_config(report: _Report, cfg, ports) -> None:
    """Cross-check each enabled serial plugin's device against detected ports.

    The serial analogue of :func:`_doctor_cameras_vs_config`: an "error" when a
    configured device is absent (drives a nonzero exit), an "info" for a detected
    board no plugin uses."""
    from octacam import plugins as plugins_mod
    from octacam import serial_ports as sp

    if cfg is None:
        return
    detected_real = {os.path.realpath(p.device) for p in ports}
    used_real: set[str] = set()
    any_serial_plugin = False
    for pc in getattr(cfg, "plugins", []):
        name = plugins_mod._ALIASES.get(pc.name, pc.name)
        if name not in sp.SERIAL_PLUGINS:
            continue
        any_serial_plugin = True
        device, is_auto = _configured_device(pc)
        if is_auto:
            resolved, reason = sp.resolve_device("auto")
            if resolved is None:
                report.add("error", f"plugin {name!r}: {reason}")
            else:
                report.add("ok", f"plugin {name!r} device=auto → {reason}")
                used_real.add(os.path.realpath(resolved))
            continue
        if not device:
            continue
        real = os.path.realpath(device)
        if real in detected_real:
            report.add("ok", f"plugin {name!r} device {device} is connected")
            used_real.add(real)
        else:
            report.add(
                "error",
                f"plugin {name!r} device {device} not found among connected "
                f"serial ports ({sp.format_candidates(ports)})",
            )
            arduino = next((p for p in ports if p.likely_arduino), None)
            if arduino is not None:
                report.add(
                    "info",
                    f"a stable udev rule for {arduino.device}: "
                    f"{sp.udev_rule_for(arduino)}",
                )
    if any_serial_plugin:
        for p in ports:
            if p.likely_microcontroller and os.path.realpath(p.device) not in used_real:
                report.add(
                    "info",
                    f"{p.board_name} {p.device} detected but not used by any plugin",
                )


def _firmware_spec_for(name: str):
    """``(spec, needed_build)`` for a serial plugin whose board carries a firmware
    fingerprint, or ``(None, None)`` for an unknown/source-less plugin."""
    from octacam import firmware as fw

    if name == "triggerbox":
        from octacam.plugins.triggerbox import _firmware_spec

        spec = _firmware_spec()
    elif name == "twophoton":
        from octacam.plugins.twophoton import _firmware_spec

        spec = _firmware_spec()
    elif name == "flywheel":
        from octacam.plugins.flywheel import _DEFAULT_FQBN, _firmware_spec

        spec = _firmware_spec(_DEFAULT_FQBN)  # fqbn is irrelevant to classification
    else:
        return None, None
    if spec is None:
        return None, None
    try:
        return spec, fw.sketch_fingerprint(spec.sketch_dir)
    except Exception:
        return spec, None


def _doctor_serial_probe(report: _Report, cfg, mcus) -> None:
    """Read each microcontroller port's firmware identity (opt-in, invasive)."""
    from octacam import firmware as fw
    from octacam import plugins as plugins_mod
    from octacam import serial_ports as sp

    expected: dict[str, tuple[str, str]] = {}
    if cfg is not None:
        for pc in getattr(cfg, "plugins", []):
            name = plugins_mod._ALIASES.get(pc.name, pc.name)
            banner = sp.EXPECTED_BANNER.get(name)
            device, is_auto = _configured_device(pc)
            if banner and device and not is_auto:
                expected[os.path.realpath(device)] = (name, banner)
    for p in mcus:
        ident = sp.probe_identity(p.device)
        if ident.busy:
            report.add(
                "info",
                f"{p.device}: port in use (held by a running session); "
                "skipped identity probe",
            )
            continue
        if ident.banner:
            report.add("info", f"{p.device}: firmware identity {ident.banner!r}")
        else:
            report.add(
                "info",
                f"{p.device}: no identity reply (not an octacam-firmware board, "
                "or its firmware has no identify command)",
            )
        exp = expected.get(os.path.realpath(p.device))
        if not exp:
            continue
        name = exp[0]
        spec, needed = _firmware_spec_for(name)
        if spec is not None and needed is not None:
            check = fw.classify(spec, ident.banner, needed)
            if check.state is fw.FirmwareState.CURRENT:
                report.add("ok", f"{p.device}: {name} firmware up to date (build {needed})")
            elif check.needs_flash and check.state is not fw.FirmwareState.UNIDENTIFIED:
                # UNIDENTIFIED is already covered by the "no identity reply" line above.
                report.add(
                    "warn",
                    f"{p.device}: {name} firmware needs flashing — {check.detail}; "
                    "run `octacam flash`",
                )
        elif ident.banner and not ident.banner.upper().startswith(exp[1]):
            report.add(
                "warn",
                f"{p.device}: expected {name} firmware (banner {exp[1]!r}) but "
                f"got {ident.banner!r} — wrong board?",
            )


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
    # Report the GUI port only by exception — a free port is the unremarkable case.
    if not _port_available("127.0.0.1", 8765):
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
            help="Only enumerate this backend (basler/flir/spinnaker/"
            "pycameleon/fake). Default: the whole available cascade.",
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
    probe_serial: Annotated[
        bool,
        typer.Option(
            "--probe-serial",
            help="Also open each detected serial port briefly to read its "
            "firmware identity. Skips ports held by a running session; skip "
            "this if a board may be armed.",
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
    # The only slow part of doctor is camera enumeration; do it ONCE, up front, in
    # parallel across backends, behind a live progress spinner (suppressed under
    # --json). Every _doctor_* section below reads this single cached scan.
    scan = _CameraScan(backend)
    _run_scan_with_progress(scan, quiet=json_output)
    _doctor_system(report)
    _doctor_backends(report, backend, scan)
    _doctor_encoding(report)
    cfg = _doctor_config(report, config_dir) if config_dir is not None else None
    if cfg is not None:
        _doctor_cameras_vs_config(report, cfg, backend, scan)
        _doctor_storage(report, cfg)
    _doctor_plugins(report, cfg)
    _doctor_serial(report, cfg, probe=probe_serial)
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
    """The cascade tiers installed here, in priority order.

    Lets the wizard tell the user which backends it will auto-detect through
    (vendor SDKs, the always-present pycameleon floor). ``fake`` is a synthetic
    test backend, never auto-detected. Selection imports the SDK but never opens a
    device, so this is side-effect free."""
    from octacam.cameras.registry import available_backends

    return available_backends()


def _resolve_backend(console, cli_backend: str | None) -> str:
    """Resolve the wizard's backend selector; no interactive prompt.

    Without ``--backend`` the wizard does not ask which vendor to use: a rig
    auto-detects every installed backend (Basler, FLIR) and simply uses whatever
    is plugged in, so mixing vendors just works. ``--backend`` still pins the rig
    to one vendor, or selects the synthetic ``fake`` backend for tests/CI."""
    from octacam.cameras.registry import BACKENDS

    if cli_backend is not None:
        key = cli_backend.strip().lower()
        if key in ("auto", "all"):
            return "auto"
        if key not in BACKENDS:
            raise typer.BadParameter(
                f"unknown backend {cli_backend!r}; expected 'auto' or one of "
                f"{', '.join(BACKENDS)}",
                param_hint="--backend",
            )
        return key
    available = _available_backends()
    if available:
        console.print(
            f"Auto-detecting cameras from: [bold]{', '.join(available)}[/bold]"
        )
    else:
        console.print(
            "[yellow]No camera SDK detected here[/yellow] — you can still write a "
            "config now and detect cameras later on the rig."
        )
    return "auto"


def _detect_cameras(console, backend: str) -> list[tuple[str, str | None]]:
    """List (serial, model|None) for *backend*; [] if none or enumeration fails.

    ``backend`` may be ``"auto"``, in which case every installed backend is swept
    and the combined list returned."""
    label = "" if backend in ("auto", "all", "") else f"{backend} "
    try:
        cams = _enumerate_backend(backend)
    except Exception as e:
        console.print(f"[yellow]Could not enumerate {label}cameras:[/yellow] {e}")
        return []
    if cams:
        console.print(f"Detected [bold]{len(cams)}[/bold] {label}camera(s):")
        for serial, model in cams:
            console.print(f"  • {model + '  ' if model else ''}{serial}")
    else:
        console.print(f"[yellow]No {label}cameras detected.[/yellow]")
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
        choices=["software", "managed", "external"],
        default=d.trigger_source,
        console=console,
    )
    preview_trigger_source = Prompt.ask(
        "Preview trigger source (auto = mirror the recording trigger)",
        choices=["auto", "software", "free_running"],
        default=d.preview_trigger_source,
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
        "Save method (ffmpeg=CPU x264, nvenc=NVIDIA GPU, raw=Mono8 dump)",
        choices=["ffmpeg", "nvenc", "raw"],
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
            "preview_trigger_source": preview_trigger_source,
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


def _detect_serial_ports(console):
    """Print detected microcontroller-class serial ports; return that list.

    Mirrors :func:`_detect_cameras` — the legacy ``/dev/ttyS*`` ports are omitted
    so only the plausible Arduino candidates are shown."""
    from octacam import serial_ports as sp

    mcus = [p for p in sp.list_serial_ports() if p.likely_microcontroller]
    if mcus:
        console.print(f"Detected [bold]{len(mcus)}[/bold] serial device(s):")
        for p in mcus:
            sn = f"  sn={p.serial_number}" if p.serial_number else ""
            console.print(f"  • {p.board_name}  {p.device}  [{p.vid_pid}]{sn}")
    else:
        console.print("[yellow]No Arduino-class serial ports detected.[/yellow]")
    return mcus


def _prompt_serial_plugin(console) -> list[dict]:
    """Optionally enable one serial/trigger plugin and choose its device.

    Returns a list of plugin entry dicts for the config (empty when declined)."""
    from rich.prompt import Confirm, Prompt

    from octacam import serial_ports as sp

    console.print()
    if not Confirm.ask(
        "Enable a hardware trigger / serial plugin (Arduino)?",
        default=False,
        console=console,
    ):
        return []
    name = Prompt.ask(
        "  Plugin",
        choices=["triggerbox", "twophoton", "flywheel"],
        default="triggerbox",
        console=console,
    )
    ports = _detect_serial_ports(console)
    default_device = ports[0].device if ports else (_plugin_default_device(name) or "auto")
    console.print(
        "  Enter a device path, or [bold]auto[/bold] to pick the single board "
        "connected at launch."
    )
    device = Prompt.ask("  Device", default=default_device, console=console).strip()
    options = {"device": device} if device else {}
    # Offer a stable udev rule for the chosen board (survives re-enumeration).
    chosen = next((p for p in ports if p.device == device), None)
    if (
        chosen is not None
        and chosen.serial_number
        and Confirm.ask(
            "  Print a udev rule for a stable /dev path for this board?",
            default=False,
            console=console,
        )
    ):
        console.print(f"    {sp.udev_rule_for(chosen)}")
        console.print(
            "    Add it to /etc/udev/rules.d/99-octacam.rules, then reload with "
            "`sudo udevadm control --reload && sudo udevadm trigger`."
        )
    return [{"name": name, "options": options}]


def _build_config_doc(
    backend: str,
    record_cfg: "RecordConfig",
    cameras: list[dict],
    visualization: list[dict],
    transfer: dict | None,
    plugins: list[dict] | None = None,
) -> dict:
    """Assemble the raw-TOML dict the config writer serializes.

    ``backend`` is written only when the user pinned a vendor with ``--backend``;
    the default ``"auto"`` is left absent (auto-detect all) so the rig keeps
    picking up whatever is plugged in. Empty sections are omitted entirely."""
    from octacam.config import TranscodeConfig

    doc: dict = {}
    if backend not in ("auto", "all", ""):
        doc["backend"] = backend
    doc["record"] = record_cfg.model_dump()
    doc["transcode"] = TranscodeConfig().model_dump()
    if cameras:
        doc["cameras"] = cameras
    if visualization:
        doc["visualization"] = visualization
    if plugins:
        doc["plugins"] = plugins
    if transfer:
        doc["transfer"] = transfer
    return doc


def _snapshot_camera_params(
    console, backend: str, serials: list[str], target: Path
) -> list[str]:
    """Open the given cameras once and save each one's sensor params into *target*.

    This is the wizard's only step that needs exclusive camera access, so a
    camera already held by a live session (or otherwise un-openable) is never
    fatal: we warn and skip, leaving a valid but parameter-less config the GUI's
    Save… dialog can complete later. Returns the parameter filenames written
    (``[]`` when skipped or there was nothing to snapshot)."""
    if not serials:
        return []
    from octacam import config_writer
    from octacam.cameras.base import BackendError
    from octacam.cameras.system import CameraSystem

    try:
        system = CameraSystem(requested_serial_numbers=serials, backend=backend)
    except BackendError as e:
        console.print(
            f"[yellow]Skipping sensor parameters[/yellow] — could not open the "
            f"camera(s): {e}\n  A camera is likely in use by another session; run "
            "`octacam gui` and use Save… to capture them later."
        )
        return []
    except Exception as e:  # missing SDK, unknown serial, … — never fatal here
        console.print(
            f"[yellow]Skipping sensor parameters[/yellow] — could not open the "
            f"camera(s): {e}"
        )
        return []
    try:
        pfs = system.save_all_params()
        if not pfs:
            return []
        # Each camera persists in its own backend's format (.pfs / .txt), so a
        # mixed rig writes per-serial rather than one shared extension.
        ext_by_serial = system.extension_by_serial()
        config_writer.write_pfs_files(target, pfs, ext_by_serial)
        return [f"{serial}.{ext_by_serial.get(serial, 'pfs')}" for serial in pfs]
    finally:
        system.close()


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
            help="Pin the rig to one camera backend (basler/flir/spinnaker/"
            "pycameleon/fake). Default: auto-detect through the cascade "
            "and use whatever is connected.",
        ),
    ] = None,
    force: Annotated[
        bool,
        typer.Option(
            "--force",
            help="Overwrite an existing octacam_config.toml without asking.",
        ),
    ] = False,
    snapshot_params: Annotated[
        bool,
        typer.Option(
            "--snapshot-params/--no-snapshot-params",
            help="Open each detected camera once to save its current sensor "
            "parameters (.pfs/.txt). Skipped when a camera is busy. On by default.",
        ),
    ] = True,
) -> None:
    """Interactively scaffold a new rig config directory.

    Auto-detects the connected cameras (across every installed backend, so a
    Basler+FLIR rig just works), then prompts for the record and transfer
    settings and writes an octacam_config.toml. The visual per-camera bits —
    window placement, rotation, and the grid — are left to `octacam gui`, which
    tunes them against a live preview; run it next on the new directory.

    By default it also opens each detected camera once to snapshot its current
    sensor parameters into a per-camera file; a busy camera is skipped with a
    warning. Pass --no-snapshot-params to skip that and never open a camera.
    """
    from rich.console import Console
    from rich.prompt import Confirm, Prompt

    from octacam import config_writer

    console = Console()
    console.print("[bold]octacam config[/bold] — set up a new rig config\n")

    chosen_backend = _resolve_backend(console, backend)
    detected = _detect_cameras(console, chosen_backend)
    cameras = _prompt_cameras(console, detected)
    visualization = _prompt_visualization(console, cameras)
    console.print()
    record_cfg = _prompt_record(console)
    console.print()
    transfer = _prompt_transfer(console)
    plugins_cfg = _prompt_serial_plugin(console)

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
        chosen_backend, record_cfg, cameras, visualization, transfer, plugins_cfg
    )
    try:
        target.mkdir(parents=True, exist_ok=True)
        written = config_writer.write_config(target, doc)
    except OSError as e:
        sys.exit(f"Failed to write config: {e}")

    console.print(f"\n[green]✓[/green] Wrote [bold]{written}[/bold]")
    if snapshot_params:
        serials = [c["serial_number"] for c in cameras] or [s for s, _ in detected]
        saved = _snapshot_camera_params(console, chosen_backend, serials, target)
        if saved:
            console.print(
                f"[green]✓[/green] Saved sensor parameters: {', '.join(saved)}"
            )
    console.print("\nNext steps:")
    console.print(f"  • Validate it:          octacam doctor {target}")
    console.print(f"  • Place cameras & grid: octacam gui {target}")
    console.print(f"  • Record headlessly:    octacam record {target}")


def _drive_record_progress(controller, duration_s: float) -> None:
    """Show live recording progress until the recording leaves the active state.

    On a TTY a determinate rich bar tracks the countdown with a running total
    frame count; off a TTY a periodic log heartbeat stands in (so a piped/cron
    run still shows it is alive). Returns once the recording is no longer active
    — the caller then join()s the monitor to finalize the summary."""
    import time as _time

    poll = 0.1
    if not sys.stderr.isatty():
        next_beat = 0.0
        while controller.recording_active:
            now = _time.monotonic()
            if now >= next_beat:
                snap = controller.snapshot()
                frames = sum(c["frames"] for c in snap["cameras"])
                log.info("Recording (%s): %d frames captured", snap["state"], frames)
                next_beat = now + 2.0
            _time.sleep(poll)
        return

    from rich.progress import BarColumn, Progress, TextColumn, TimeRemainingColumn

    with Progress(
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TimeRemainingColumn(),
        console=_stderr_console(),
        transient=True,
    ) as progress:
        task = progress.add_task("Waiting for the first frame…", total=duration_s)
        while controller.recording_active:
            snap = controller.snapshot()
            state = snap["state"]
            frames = sum(c["frames"] for c in snap["cameras"])
            if state == "recording" and snap.get("remaining_ms") is not None:
                elapsed = max(0.0, duration_s - snap["remaining_ms"] / 1000.0)
                progress.update(
                    task, completed=elapsed, description=f"Recording — {frames} frames"
                )
            elif state == "waiting":
                progress.update(
                    task,
                    description="Waiting for the first frame / external trigger…",
                )
            elif state == "finishing":
                progress.update(task, description=f"Finishing — {frames} frames")
            _time.sleep(poll)
        progress.update(task, completed=duration_s)


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
    yes: Annotated[
        bool,
        typer.Option(
            "--yes",
            "-y",
            help="Don't prompt: if a serial plugin's board firmware is out of "
            "date, reflash it before recording (also lets a headless run flash).",
        ),
    ] = False,
    force: Annotated[
        bool,
        typer.Option(
            "--force",
            "-F",
            help="Overwrite an existing save directory without the interactive "
            "confirmation prompt.",
        ),
    ] = False,
    enabled_plugins: EnabledPlugins = None,
    no_plugins: NoPlugins = False,
) -> None:
    r"""Record videos headlessly from the cameras in CONFIG_DIR.

    Encoding, save method, transform, and the save-directory template all come
    from the config's \[record] section; the options here override only the
    day-to-day values (fps and duration, or an explicit --output save directory).
    """
    from octacam import session_cache
    from octacam.cameras import BackendError, BackendUnavailable, CameraSystem
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
    except BackendError as e:
        # The cameras could not be opened — most often because another octacam
        # already holds them (vendor SDKs open USB3 devices exclusively), or a
        # camera is disconnected. A clean message beats a raw SDK traceback.
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

    if Path(settings.save_dir).exists():
        if force:
            log.warning("Save directory exists; overwriting: %s", settings.save_dir)
        elif sys.stdin.isatty() and sys.stderr.isatty():
            # Interactive: let the operator confirm before clobbering data.
            if not typer.confirm(
                f"Save directory already exists and will be overwritten:\n"
                f"  {settings.save_dir}\nContinue?"
            ):
                raise typer.Exit(1)
        else:
            # Non-interactive (scripted/cron): keep overwriting for backward
            # compatibility, but say so loudly. Use --force to silence this.
            log.warning(
                "Save directory exists, data may be overwritten: %s "
                "(pass --force to silence this)",
                settings.save_dir,
            )

    plugins = build_plugins(config, _resolve_enabled(enabled_plugins, no_plugins))
    plugins.setup_all()

    # If a serial plugin's board is running stale/wrong firmware, offer to reflash
    # it before we start (interactive prompt) — or, headless, warn unless opted in
    # via --yes / auto_flash. Without this an external-triggered rig would silently
    # record against the wrong firmware (or, for a wrong protocol version, get no
    # triggers at all).
    _preflight_firmware(plugins, assume_yes=yes)

    # Tag this headless run in the session cache so `octacam process --last`
    # and `--last session` pick it up too (a one-off, single-folder "session").
    controller = RecordingController(
        system,
        settings,
        plugins,
        auto_preview=False,
        session_id=session_cache.new_session_id(),
        record_kind="record",
        config_dir=config_dir,
    )
    # Hand a controller reference to plugins that read live device state (e.g.
    # triggerbox's auto strobe duty reads each camera's ExposureTime). Duck-typed
    # so core stays decoupled from concrete plugin classes; mirrors create_app.
    for plugin in plugins.plugins:
        if hasattr(plugin, "set_controller"):
            plugin.set_controller(controller)
    # While this recording owns the cameras, publish a capture-active marker so a
    # detached `octacam process` on this machine pauses until we are done.
    capture_stack = contextlib.ExitStack()
    capture_stack.enter_context(session_cache.mark_capture_active("recording"))
    try:
        log.info(
            "Recording %d camera(s) at %g fps for %g s to %s",
            len(system),
            settings.fps,
            settings.duration_s,
            settings.save_dir,
        )
        # Headless record has no GUI to POST plugin_params, so build each enabled
        # plugin's start slice from the recording's fps/duration (e.g. triggerbox
        # arms its trigger board — without this the external-trigger cameras wait
        # forever for a trigger that never fires). Empty -> None (no-op dispatch).
        plugin_params = plugins.default_start_params(settings.fps, settings.duration_s)
        result = controller.start_recording(
            confirm_overwrite=True, plugin_params=plugin_params or None
        )
        if not result.ok:
            sys.exit(f"Failed to start recording: {result.message}")
        _drive_record_progress(controller, settings.duration_s)
        controller.join()
    finally:
        # controller.close() sets abort, joins the daemon recording monitor (so
        # its finishing block writes recording-summary.json/timestamps.npz and the
        # session-cache note), then closes the camera system exactly once — mirror
        # of the gui shutdown above. Calling system.close() directly would race the
        # still-running monitor on a Ctrl-C/exception stop and lose that metadata.
        controller.close()
        plugins.teardown_all()
        capture_stack.close()

    extension = settings.video_format().extension
    for camera in system:
        typer.echo(f"{Path(settings.save_dir) / camera.name}.{extension}")

    # A camera that captured 0 frames wrote only a header (no video) — usually an
    # external trigger that never fired during the window. Fail the exit code so a
    # scripted rig can detect it instead of seeing a "successful" run of empty files.
    empty = [c.name for c in system if c.frames_recorded == 0]
    if empty:
        sys.exit(
            f"{len(empty)} camera(s) recorded 0 frames ({', '.join(empty)}); "
            "the recording is incomplete (no trigger, or no frames delivered)."
        )


# ---------------------------------------------------------------------------
# `octacam flash` — check & upload a serial plugin's Arduino firmware
#
# Serial plugins whose board needs a specific sketch (today: triggerbox) expose
# firmware_provisioning()/flash_firmware(). This command opens the board, compares
# its build fingerprint to the sketch source, and (unless --check) offers to
# compile + upload the current firmware with arduino-cli. `octacam record` runs the
# same check at start; `octacam doctor --probe-serial` reports it read-only.
# ---------------------------------------------------------------------------


def _load_config_or_empty(config_dir: Path | None):
    """Load a rig config if present, else an empty stand-in (``.plugins == []``)."""
    from types import SimpleNamespace

    if config_dir is None:
        return SimpleNamespace(plugins=[])
    try:
        from octacam.config import load_config_dir

        return load_config_dir(config_dir)
    except Exception as e:
        log.debug("flash: could not load config at %s: %s", config_dir, e)
        return SimpleNamespace(plugins=[])


def _flashable_plugins(plugins, only: str | None):
    """The plugins that support firmware provisioning, optionally filtered to one."""
    from octacam import plugins as plugins_mod

    out = [
        p
        for p in getattr(plugins, "plugins", [])
        if hasattr(p, "flash_firmware") and hasattr(p, "firmware_provisioning")
    ]
    if only:
        # build_plugins resolves legacy aliases (e.g. arduino->flywheel), so
        # match on the canonical name the requested name resolves to.
        canonical = plugins_mod._ALIASES.get(only, only)
        out = [p for p in out if p.name == canonical]
    return out


def _flash_one(console, plugin, prov: dict, *, assume_yes: bool, check_only: bool) -> int:
    """Report one board's firmware and, unless --check, offer to flash it.

    Returns 0 when up to date or freshly flashed, 1 otherwise (out of date and
    not flashed)."""
    from rich.prompt import Confirm

    device = prov.get("device")
    console.print()
    console.print(f"[bold]{plugin.name}[/bold] — {device or 'no device'}")
    # An un-openable board (unplugged, wrong path, or port held by a running
    # session) can't be probed — never report that as "up to date".
    if not getattr(plugin, "is_ready", lambda: True)():
        console.print(
            "  [red]could not open the board[/red] — it may be unplugged, the "
            "device path may be wrong, or the port may be held by a running octacam "
            "session. Its firmware can't be read or flashed here."
        )
        return 1
    console.print(f"  board firmware: {prov.get('firmware') or '(no identity reply)'}")
    console.print(f"  source build:   {prov.get('needed_build') or '(source not found)'}")
    if not prov.get("needs_flash"):
        console.print("  [green]✓ up to date[/green]")
        return 0
    console.print(f"  [yellow]needs flashing[/yellow] — {prov.get('detail', '')}")
    if check_only:
        return 1
    if not prov.get("can_flash"):
        if not prov.get("sketch_found"):
            console.print(
                "  [red]Can't auto-flash:[/red] the sketch source wasn't found (a "
                "wheel install without a checkout). Flash manually with arduino-cli, "
                "or set OCTACAM_ARDUINO_DIR."
            )
        elif not prov.get("cli_available"):
            console.print(
                "  [red]Can't auto-flash:[/red] arduino-cli was not found. Install it "
                "(https://arduino.github.io/arduino-cli/) or set OCTACAM_ARDUINO_CLI."
            )
        else:
            console.print("  [red]Can't auto-flash on this host.[/red]")
        return 1
    if prov.get("state") == "unidentified":
        console.print(
            "  [yellow]⚠ the board sent no identity[/yellow] — flashing overwrites "
            "whatever is on it; only proceed if this is the right board."
        )
    if not assume_yes and not Confirm.ask(
        f"  Upload the current firmware to {device}?", default=False, console=console
    ):
        console.print("  skipped — the board keeps its current firmware.")
        return 1
    console.print("  Flashing (compile + upload, ~1 min; the board reboots at the end)…")
    result = plugin.flash_firmware(on_line=lambda ln: console.print(f"    [dim]{ln}[/dim]"))
    if result.ok:
        console.print(f"  [green]✓ {result.message}[/green]")
        return 0
    console.print(f"  [red]✗ {result.message}[/red]")
    return 1


def _preflight_firmware(plugins, *, assume_yes: bool) -> None:
    """At record start, offer to reflash a serial plugin's stale board.

    Interactive (a TTY): prompt per out-of-date board. Headless: warn only —
    unless ``--yes`` or the plugin's ``auto_flash`` is set AND the board is
    unambiguously *this* board running an old build (a blank/foreign board is
    never auto-flashed; the operator must confirm with ``octacam flash``)."""
    interactive = sys.stdin.isatty()
    console = None
    for p in getattr(plugins, "plugins", []):
        if not (hasattr(p, "firmware_provisioning") and hasattr(p, "flash_firmware")):
            continue
        try:
            prov = p.firmware_provisioning()
        except Exception:
            log.debug("firmware preflight: %s provisioning failed", getattr(p, "name", "?"),
                      exc_info=True)
            continue
        if not prov.get("needs_flash"):
            continue
        device = prov.get("device")
        can = bool(prov.get("can_flash"))
        safe = bool(prov.get("safe_to_auto_flash"))
        auto = bool(getattr(p, "_auto_flash", False))
        msg = f"{p.name}: board firmware on {device} is out of date — {prov.get('detail', '')}"
        do_flash = False
        if interactive and can:
            from rich.console import Console
            from rich.prompt import Confirm

            console = console or Console(stderr=True)
            console.print(f"[yellow]{msg}[/yellow]")
            if prov.get("state") == "unidentified":
                console.print(
                    "[yellow]  no identity — flashing overwrites whatever is on the "
                    "board[/yellow]"
                )
            do_flash = Confirm.ask(
                f"Upload the current firmware to {device} now?", default=False, console=console
            )
        elif (assume_yes or auto) and can and safe:
            do_flash = True
        else:
            hint = (
                "run `octacam flash`"
                if not can or not safe
                else "pass --yes or set auto_flash=true"
            )
            log.warning("%s; %s to reflash. Continuing WITHOUT reflashing.", msg, hint)
        if do_flash:
            log.info("%s: flashing current firmware…", p.name)
            result = p.flash_firmware(on_line=lambda ln: log.info("  arduino-cli: %s", ln))
            log.log(logging.INFO if result.ok else logging.ERROR, "%s: %s", p.name, result.message)


@app.command()
def flash(
    config_dir: Annotated[
        Path | None,
        typer.Argument(
            exists=True,
            file_okay=False,
            dir_okay=True,
            help="Rig config dir whose serial plugins to check. Optional if --plugin "
            "is given.",
        ),
    ] = None,
    plugin: Annotated[
        str | None,
        typer.Option(
            "--plugin",
            help="Serial plugin whose firmware to manage (e.g. triggerbox); enables "
            "it even if not in the config.",
        ),
    ] = None,
    device: Annotated[
        str | None,
        typer.Option("--device", help="Serial device override (e.g. /dev/ttyACM0 or auto)."),
    ] = None,
    yes: Annotated[
        bool,
        typer.Option("--yes", "-y", help="Flash without prompting when out of date."),
    ] = False,
    check_only: Annotated[
        bool,
        typer.Option(
            "--check",
            help="Report only; exit nonzero if any board is out of date. Never flashes.",
        ),
    ] = False,
) -> None:
    """Check a serial plugin's board firmware and upload the current sketch if needed.

    Reads the board's identify banner, compares its build fingerprint to the sketch
    source in ``arduino/<name>``, and (unless ``--check``) compiles + uploads the
    current firmware with arduino-cli. Exits 0 when every board is up to date (or
    was flashed), nonzero if any remains out of date.
    """
    from rich.console import Console

    from octacam.plugins import build_plugins

    console = Console()
    config = _load_config_or_empty(config_dir)
    if config_dir is None and not plugin and not getattr(config, "plugins", []):
        raise typer.BadParameter(
            "give a rig CONFIG_DIR or --plugin <name>", param_hint="--plugin"
        )

    # Flashing resets the board, so refuse to flash a rig another octacam owns
    # (its board may be armed mid-recording). Same per-config lock the GUI takes.
    instance_lock = None
    if config_dir is not None:
        instance_lock = _acquire_instance_lock(config_dir)
        if instance_lock is None:
            holder = _instance_lock_holder(config_dir.resolve())
            who = f" (pid {holder})" if holder else ""
            console.print(
                f"[red]Another octacam instance owns this rig{who}[/red] — its board "
                "may be armed. Stop it before flashing."
            )
            raise typer.Exit(2)

    try:
        plugins = build_plugins(config, [plugin] if plugin else None)
        flashable = _flashable_plugins(plugins, plugin)
        if not flashable:
            which = f" {plugin!r}" if plugin else ""
            console.print(
                f"[yellow]No firmware-flashable serial plugin{which} is enabled.[/yellow] "
                "triggerbox, twophoton, and flywheel support firmware flashing."
            )
            raise typer.Exit(1 if plugin else 0)

        exit_code = 0
        for p in flashable:
            if device and hasattr(p, "_configured_device"):
                p._configured_device = device
            try:
                p.setup()  # open the link + read the identity banner
            except Exception as e:
                console.print(f"[yellow]{p.name}: could not open the board: {e}[/yellow]")
            try:
                prov = p.firmware_provisioning()
                if _flash_one(console, p, prov, assume_yes=yes, check_only=check_only) != 0:
                    exit_code = 1
            finally:
                try:
                    p.teardown()
                except Exception:
                    pass
        raise typer.Exit(exit_code)
    finally:
        if instance_lock is not None and instance_lock is not _LOCK_UNAVAILABLE:
            instance_lock.close()


# ---------------------------------------------------------------------------
# `octacam benchmark` — frame-rate diagnostic / stress-test
#
# A short instrumented dry-run against the real cameras: is the target fps
# achievable, what is the maximum achievable rate, and which pipeline stage
# (acquisition / encoding / host) is the bottleneck. No video is kept. Unlike
# `doctor` (which only enumerates and never opens a camera), this opens and
# drives the cameras, so it needs exclusive access like `record`/`gui`.
# ---------------------------------------------------------------------------


def _bottleneck_label(bottleneck: str) -> str:
    from octacam import diagnostics as diag

    return {
        diag.ACQUISITION: "acquisition (camera can't deliver fast enough)",
        diag.TRANSFER: "transfer (cameras share more bus bandwidth than the link provides)",
        diag.ENCODE: "encoding (the encoder can't keep up)",
        diag.HOST: "host contention (CPU / GIL)",
        diag.NONE: "none",
    }.get(bottleneck, bottleneck)


class _BenchmarkProgressBar:
    """Determinate benchmark progress bar, animated between phase boundaries.

    The diagnostic only reports progress at phase boundaries (each measurement
    window blocks for a few seconds), so a background ticker interpolates the bar
    from the phase's start ``fraction`` toward its ``target`` over the phase's
    expected ``eta_s`` — the bar keeps moving during a window instead of freezing.
    Fed by :class:`octacam.diagnostics.Progress` via :meth:`update`.
    """

    def __init__(self) -> None:
        from rich.progress import (
            BarColumn,
            Progress,
            TaskProgressColumn,
            TextColumn,
            TimeElapsedColumn,
        )

        self._progress = Progress(
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            TaskProgressColumn(),
            TimeElapsedColumn(),
            console=_stderr_console(),
            transient=True,
        )
        self._task = self._progress.add_task("Benchmarking…", total=1000)
        self._lock = threading.Lock()
        self._shown = 0.0  # last displayed fraction (monotonic, never regresses)
        self._anchor = 0.0  # bar position when the current goal was set
        self._goal = 0.0  # fraction to ease toward (monotonic)
        self._eta = 0.0
        self._phase_start = time.monotonic()
        self._stop = threading.Event()
        self._ticker = threading.Thread(target=self._run, daemon=True)

    def __enter__(self) -> "_BenchmarkProgressBar":
        self._progress.start()
        self._ticker.start()
        return self

    def __exit__(self, *exc) -> None:
        self._stop.set()
        self._ticker.join(timeout=1.0)
        self._progress.update(self._task, completed=1000)
        self._progress.stop()

    def update(self, p) -> None:
        """Phase-boundary update (an :class:`octacam.diagnostics.Progress`).

        Eases toward the phase target from wherever the bar currently sits — the
        goal is clamped monotonic and the bar is never snapped back to the phase
        start, so the many probe updates in the max-fps search keep it moving
        forward instead of resetting it each time.
        """
        with self._lock:
            self._goal = max(self._goal, p.target)
            self._anchor = self._shown
            self._eta = p.eta_s
            self._phase_start = time.monotonic()
            desc = p.phase if not p.detail else f"{p.phase} {p.detail}"
            self._progress.update(
                self._task, description=desc, completed=self._shown * 1000
            )

    def _run(self) -> None:
        while not self._stop.wait(0.05):
            with self._lock:
                if self._eta > 0 and self._goal > self._anchor:
                    ratio = min(1.0, (time.monotonic() - self._phase_start) / self._eta)
                    frac = self._anchor + (self._goal - self._anchor) * ratio
                else:
                    frac = self._goal
                frac = min(1.0, max(self._shown, frac))  # monotonic, never regress
                self._shown = frac
            self._progress.update(self._task, completed=frac * 1000)


def _fps(value) -> str:
    """Format an fps/ceiling for the report ("–" for None/inf)."""
    import math

    if value is None or (isinstance(value, float) and not math.isfinite(value)):
        return "–"
    return f"{value:.0f}"


def _render_benchmark(report) -> None:
    """Render a DiagnosticReport as a human report on stdout (rich).

    Inverted pyramid: the two numbers an operator acts on first (KEY RESULTS),
    then which pipeline stage caps the system (BY STAGE), then the per-camera
    detail (BY CAMERA).
    """
    from rich.console import Console
    from rich.table import Table
    from rich.text import Text

    from octacam import diagnostics as diag

    console = Console()
    r = report
    c = r.ceilings
    console.print()
    console.print(
        Text(
            f"octacam benchmark — {r.n_cameras} camera(s) via {r.backend}", style="bold"
        )
    )
    encoder = r.save_method + (f" ({r.ffmpeg_params})" if r.ffmpeg_params else "")
    console.print(
        f"target {r.target_fps:g} fps · {r.trigger_source} trigger · sink={encoder}"
    )

    # ---- KEY RESULTS: the two headline max rates + the target verdict ----
    console.print()
    console.print(Text("KEY RESULTS", style="bold"))
    if r.measured_max_fps is not None:
        confidence = "confirmed" if r.max_confirmed else "safety margin"
        console.print(
            f"  Synchronized (software) max:  {_fps(r.measured_max_fps)} fps/cam "
            f"({confidence})"
        )
    else:
        console.print(
            f"  Synchronized (software) max:  {_fps(r.predicted_max_fps)} fps/cam "
            "(predicted)"
        )
    if r.hardware_max_fps is not None:
        measured = " (measured)" if r.freerun_trials else ""
        console.print(
            f"  Free-run / hardware max:      {_fps(r.hardware_max_fps)} fps/cam"
            f"{measured}"
        )
    if r.achievable:
        console.print(
            Text(f"  ✓ {r.target_fps:g} fps is ACHIEVABLE", style="bold green")
        )
    else:
        console.print(
            Text(
                f"  ✗ {r.target_fps:g} fps is NOT achievable — "
                f"limited by {_bottleneck_label(r.bottleneck)}",
                style="bold red",
            )
        )

    # ---- BY STAGE: which pipeline stage caps the synchronized rate ----
    if c is not None:
        console.print()
        console.print(
            Text("BY STAGE", style="bold"),
            Text("  (system ceiling = slowest camera)", style="dim"),
        )

        def mark(name):
            return Text("  ← limits", style="red") if r.bottleneck == name else Text("")

        console.print(
            Text(f"  acquisition  {_fps(c.grab_min):>4} fps/cam  "),
            Text("(software; exposure+transfer serial)", style="dim"),
            mark(diag.ACQUISITION),
        )
        if r.throughput_mbps_total:
            per_cam = r.throughput_mbps_total / r.n_cameras if r.n_cameras else 0.0
            solo = (
                f" · alone {_fps(c.grab_solo_min)} fps/cam" if c.grab_solo_fps else ""
            )
            console.print(
                Text(
                    f"  transfer     {per_cam:>4.0f} MB/s/cam · "
                    f"{r.throughput_mbps_total:.0f} MB/s total"
                ),
                Text(f"(derived from frame size × fps{solo})", style="dim"),
                mark(diag.TRANSFER),
            )
        if c.encode_fps:
            console.print(f"  encode       {_fps(c.encode_min):>4} fps/cam")
        else:
            console.print(
                Text(
                    "  encode        n/a  (null sink — encoder not measured)",
                    style="dim",
                )
            )

    # ---- BY CAMERA: per-camera ceilings + end-to-end trial detail ----
    console.print()
    console.print(Text("BY CAMERA", style="bold"))
    table = Table(show_header=True, header_style="bold", box=None, pad_edge=False)
    table.add_column("camera")
    table.add_column("size", justify="right")
    table.add_column("acq", justify="right")
    table.add_column("free", justify="right")
    table.add_column("enc", justify="right")
    table.add_column("fps", justify="right")
    table.add_column("drop%", justify="right")
    table.add_column("queue peak", justify="right")
    table.add_column("acquire p50/p99 ms", justify="right")
    table.add_column("encode p50/p99 ms", justify="right")
    for t in r.trials:
        acq = t.stages.get("acquire")
        enc = t.stages.get("encode")
        s = t.serial
        table.add_row(
            t.name,
            f"{t.width}×{t.height}",
            _fps(c.grab_fps.get(s)) if c else "–",
            _fps(c.freerun_fps.get(s)) if c and c.freerun_fps else "–",
            _fps(c.encode_fps.get(s)) if c and c.encode_fps else "–",
            f"{t.achieved_fps:.1f}",
            f"{100 * t.drop_rate:.2f}",
            f"{t.max_queue_depth} of {diag.WRITER_QUEUE_SIZE}",
            f"{acq.p50_ms:.1f}/{acq.p99_ms:.1f}" if acq else "–",
            f"{enc.p50_ms:.2f}/{enc.p99_ms:.2f}" if enc and enc.samples else "–",
        )
    console.print(table)
    console.print(
        Text(
            "  acq/free/enc = per-camera ceilings (concurrent / free-run / encode); "
            "fps/drop%/queue = the end-to-end trial at the target. drop% counts only "
            "frames the encoder queue refused (host couldn't keep up), not camera "
            "transport gaps.",
            style="dim",
        )
    )

    # ---- Free-run trial (measured real free-run pipeline, encoder in loop) ----
    if r.freerun_trials:
        console.print()
        console.print(
            Text("  Free-run trial", style="bold"),
            Text("(real free-run pipeline, encoder in the loop)", style="dim"),
        )
        ft = Table(show_header=True, header_style="bold", box=None, pad_edge=False)
        ft.add_column("  camera")
        ft.add_column("fps", justify="right")
        ft.add_column("drop%", justify="right")
        ft.add_column("queue peak", justify="right")
        for t in r.freerun_trials:
            ft.add_row(
                f"  {t.name}",
                f"{t.achieved_fps:.1f}",
                f"{100 * t.drop_rate:.2f}",
                f"{t.max_queue_depth} of {diag.WRITER_QUEUE_SIZE}",
            )
        console.print(ft)

    extras = []
    if r.system_cpu_percent is not None:
        extras.append(f"machine load (pre-run) {r.system_cpu_percent:.0f}% cpu")
    if r.cpu_percent is not None:
        extras.append(f"benchmark cpu {r.cpu_percent:.0f}%")
    if r.jitter_p99_ms is not None:
        extras.append(f"scheduler jitter p99 {r.jitter_p99_ms:.2f} ms")
    if extras:
        console.print("  " + " · ".join(extras))

    if r.recommendations:
        console.print()
        for rec in r.recommendations:
            console.print(Text(f"  → {rec}", style="yellow"))
    for note in r.notes:
        console.print(Text(f"  • {note}", style="dim"))
    console.print()


@app.command()
def benchmark(
    config_dir: Annotated[
        Path,
        typer.Argument(exists=True, file_okay=False, dir_okay=True),
    ] = Path("."),
    fps: Annotated[
        float | None,
        typer.Option(
            "--fps", "-f", help=r"Target fps to test \[default: from config]."
        ),
    ] = None,
    duration: Annotated[
        float,
        typer.Option(
            "--duration", "-d", help="Seconds per measurement window (each scenario)."
        ),
    ] = 5.0,
    find_max: Annotated[
        bool,
        typer.Option(
            "--find-max/--no-find-max",
            help="Search for the maximum *stable* fps (software trigger only).",
        ),
    ] = True,
    freerun: Annotated[
        bool,
        typer.Option(
            "--freerun/--no-freerun",
            help="Also measure the free-run (external-trigger-equivalent) ceiling.",
        ),
    ] = True,
    sink: Annotated[
        str,
        typer.Option(
            "--sink",
            help="What to write through: 'config' (the rig's real save_method, so "
            "the encode cost is measured) or 'null' (discard frames — isolate "
            "acquisition, skip the encoder).",
        ),
    ] = "config",
    backend: Annotated[
        str | None,
        typer.Option("--backend", help="Override the config's camera backend."),
    ] = None,
    record_form: Annotated[
        str | None,
        typer.Option(
            "--record-form",
            help=r"'display' (bake the transform) or 'sensor' \[default: from config].",
        ),
    ] = None,
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Emit the report as JSON instead of the table."),
    ] = False,
) -> None:
    """Benchmark a rig: is the target fps achievable, what is the max, and what limits it.

    Runs a short, instrumented dry-run against the cameras in CONFIG_DIR — an
    acquisition-ceiling sweep, an encoder-ceiling sweep, and an end-to-end trial
    at the target fps — then reports the achievable rate, the maximum, and the
    per-stage throughput so you can see the bottleneck. No video is kept. It opens
    the cameras (like `record`), so it cannot run at the same time as a live GUI or
    recording on the same rig.

    Exits nonzero when the target fps is not achievable, so it is usable as a
    pre-flight check in scripts.
    """
    from octacam import diagnostics as diag
    from octacam.cameras import BackendError, BackendUnavailable, CameraSystem
    from octacam.config import load_config_dir

    if sink not in ("config", "null"):
        raise typer.BadParameter("expected 'config' or 'null'", param_hint="--sink")
    if record_form is not None and record_form not in ("display", "sensor"):
        raise typer.BadParameter(
            "expected 'display' or 'sensor'", param_hint="--record-form"
        )

    config = load_config_dir(config_dir)
    record_cfg = (
        config.record.model_copy(update={"fps": fps})
        if fps is not None
        else config.record
    )
    settings = _settings_from_record(record_cfg, config.transcode, config.transfer)
    if record_form is not None:
        settings.record_form = record_form

    # A transcode running here would fight the benchmark for the CPU and skew it.
    _warn_if_transcoding()

    try:
        system = CameraSystem(
            [c.serial_number for c in config.cameras],
            backend=backend or config.backend,
        )
    except BackendUnavailable as e:
        sys.exit(str(e))
    except BackendError as e:
        sys.exit(
            f"Could not open the cameras: {e}\n"
            "They may already be in use by another octacam instance on this rig, "
            "or disconnected — only one process can open them at a time."
        )
    if len(system) == 0:
        log.warning("No cameras opened. Exiting.")
        sys.exit(1)

    names = {c.serial_number: c.name for c in config.cameras if c.name}
    for camera in system:
        camera.name = names.get(camera.serial_number, camera.name)
    system.load_config(config_dir)
    system.apply_display_config(config.cameras)

    log.info(
        "Benchmarking %d camera(s) at %g fps (%s trigger, sink=%s)…",
        len(system),
        settings.fps,
        settings.trigger_source,
        sink,
    )

    try:
        if json_output:
            # JSON mode: no progress bar (keep stdout clean for the machine reader).
            report = diag.diagnose(
                system,
                settings,
                duration_s=duration,
                find_max=find_max,
                measure_freerun=freerun,
                sink=sink,
            )
        else:
            with _BenchmarkProgressBar() as bar:
                report = diag.diagnose(
                    system,
                    settings,
                    duration_s=duration,
                    find_max=find_max,
                    measure_freerun=freerun,
                    sink=sink,
                    progress_cb=bar.update,
                )
    finally:
        system.close()

    if json_output:
        typer.echo(json.dumps(report.to_dict(), indent=2))
    else:
        _render_benchmark(report)

    # The achievable verdict comes from the software-trigger trial. On an
    # external-trigger rig the cameras are hardware-clocked and overlap
    # exposure+transfer (so they can run faster than software triggering), and the
    # rig records fine via its trigger box — a software-trigger "NOT achievable"
    # there is a misleading lower bound, not a failure. Only fail the exit code
    # for software/managed rigs, whose recording really does use this path.
    if not report.achievable and settings.trigger_source != "external":
        raise typer.Exit(1)


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

    def _warn_zero_frames(video: Path) -> None:
        # A 0-frame recording is a header-only file with no video (e.g. an
        # external trigger that never fired). Feeding it to ffmpeg only yields a
        # cryptic matroska/EBML error, so skip it here with a clear message.
        log.warning(
            "Skipping %s: recording captured 0 frames (empty header-only file)",
            video,
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
                        _warn_zero_frames(video)
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
            if entry is not None and entry.get("frames") == 0:
                # Mirror handle_dir: a directly-named 0-frame capture is a
                # header-only file ffmpeg can't transcode — skip it too.
                _warn_zero_frames(path)
            elif entry is not None:
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
    last: str | None,
    session_id: str | None,
    all_: bool,
) -> list[Path]:
    """Resolve explicit PATHS or one cache selector to a list of folders.

    The selectors --last/--session-id/--all are mutually exclusive and cannot be
    combined with explicit PATHS. They read the recording cache
    (octacam.session_cache) and skip folders that have since been deleted, so a
    removed recording is simply ignored. ``--last`` (or ``--last recording``) is
    the single most recent recording folder; ``--last session`` is every folder
    from the most recent session; ``--session-id`` names an exact session (what
    the GUI prints on exit, so the command stays correct even if another
    recording happens afterwards); ``--all`` is every folder the cache still
    holds (last RETENTION_DAYS). Exits with a clear message on a bad value, a bad
    combination, or when nothing is found.
    """
    from octacam import session_cache

    if last is not None and last not in ("recording", "session"):
        sys.exit(
            f"--last takes 'recording' or 'session' (or nothing, for the most "
            f"recent recording), not {last!r}."
        )

    chosen = [
        name
        for name, on in (
            ("--last", last is not None),
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
                "--last/--session-id/--all."
            )
        return paths

    if last == "session":
        selector = "--last session"
        folders = session_cache.session_folders()
    elif last == "recording":
        selector = "--last"
        folder = session_cache.last_folder()
        folders = [folder] if folder else []
    elif all_:
        selector = "--all"
        folders = session_cache.all_folders()
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


def _already_matched_twophoton_paths(source_root: Path) -> set[Path]:
    """2P folders already claimed by some behavior take's Phase-3 transfer.

    Scans every recording folder octacam still has cached (deleted ones are
    silently skipped by session_cache itself) for a twophoton_match.json
    sidecar, and resolves each matched entry's relative path back against
    *source_root* — so the sweep never re-copies a folder that already
    travelled to the NAS alongside its paired behavior take. A folder from a
    take that used a *different* [transfer.twophoton].source won't resolve
    against this sweep's source_root and so isn't excluded — a rare
    multi-share-per-rig edge case, not worth over-engineering here."""
    from octacam import session_cache
    from octacam.transform import TWOPHOTON_MATCH_FILENAME

    matched: set[Path] = set()
    for folder in session_cache.all_folders():
        match_path = folder / TWOPHOTON_MATCH_FILENAME
        if not match_path.is_file():
            continue
        record = _read_summary(match_path) or {}
        for entry in record.get("matched", []):
            rel = entry.get("path")
            if rel:
                matched.add((source_root / rel).resolve())
    return matched


def _write_twophoton_pending(
    folder: Path,
    take,
    *,
    dest: Path,
    source_root: Path,
    twophoton_cfg,
    checksum: bool,
    verify: bool,
) -> None:
    """Record an armed take that this run finalized (behavior data
    transferred) without finding a settled 2P match, so a later, independent
    sweep — the only thing guaranteed to eventually run again against
    *source_root* long after this take stops being explicitly fed into any
    `process` invocation — can retroactively pair it against a 2P folder that
    only settles afterwards (see :func:`_pending_twophoton_takes` and the
    run-ordering-gap note in CLAUDE.md). Best-effort: a write failure (e.g. a
    read-only folder) is logged, not fatal — the take's own transfer already
    succeeded, only the 2P pairing is at stake."""
    from octacam.transform import TWOPHOTON_PENDING_FILENAME

    record = {
        "start_time": take.start_time,
        "duration_s": take.duration_s,
        "camera_frame_counts": take.camera_frame_counts,
        "dest": str(dest),
        "source_root": str(source_root.resolve()),
        "match_window_s": twophoton_cfg.match_window_s,
        "verify_window_s": twophoton_cfg.verify_window_s,
        "verify_with_signals": twophoton_cfg.verify_with_signals,
        "assemble_tiff_stacks": twophoton_cfg.assemble_tiff_stacks,
        "checksum": checksum,
        "verify": verify,
    }
    try:
        (folder / TWOPHOTON_PENDING_FILENAME).write_text(
            json.dumps(record, indent=2) + "\n"
        )
    except OSError as e:
        log.warning("2P transfer: couldn't record pending match state for %s: %s", folder, e)


def _clear_twophoton_pending(folder: Path) -> None:
    """Remove a stale pending-match marker once *folder*'s take got a settled
    match this run — best-effort, a missing marker is not an error."""
    from octacam.transform import TWOPHOTON_PENDING_FILENAME

    try:
        (folder / TWOPHOTON_PENDING_FILENAME).unlink()
    except FileNotFoundError:
        pass
    except OSError as e:
        log.warning("2P transfer: couldn't clear pending marker for %s: %s", folder, e)


def _pending_twophoton_takes(source_root: Path):
    """Every still-cached recording folder with an unresolved
    ``twophoton_pending.json`` whose recorded source resolves to
    *source_root*: an armed take a normal `process` run already finalized
    without finding a settled 2P pair, kept around so a later sweep can
    retroactively check it against a newly-settled 2P folder rather than
    writing that folder off as standalone (the confirmed real bug — a
    behavior take finalized before its true 2P counterpart appears/settles on
    the share was permanently missed; see TWOPHOTON_MATCHING_BUG_REPORT.md).

    Returns a list of ``(folder, TakeInfo, raw record dict)`` — the raw dict
    carries the transfer context (dest/checksum/verify/match settings) the
    caller needs to actually finalize a retroactive match.

    Same reach and the same limitation as
    :func:`_already_matched_twophoton_paths`: scans
    :func:`session_cache.all_folders` (last RETENTION_DAYS, existing folders
    only) — a take whose local folder was removed by
    ``--delete-after-transfer`` before its true pair ever settled can no
    longer be found this way."""
    from octacam import session_cache
    from octacam.transform import TWOPHOTON_PENDING_FILENAME
    from octacam.twophoton_transfer import TakeInfo

    resolved_source = source_root.resolve()
    pending = []
    for folder in session_cache.all_folders():
        pending_path = folder / TWOPHOTON_PENDING_FILENAME
        if not pending_path.is_file():
            continue
        record = _read_summary(pending_path)
        if not record:
            continue
        try:
            if Path(record["source_root"]).resolve() != resolved_source:
                continue
        except (KeyError, OSError):
            continue
        take = TakeInfo(
            folder=folder,
            start_time=record["start_time"],
            duration_s=record["duration_s"],
            camera_frame_counts=record.get("camera_frame_counts", []),
        )
        pending.append((folder, take, record))
    return pending


def _log_twophoton_assembly_preview(folder: Path, assemble: bool) -> None:
    """One dry-run log line per channel — never per-file, which would spam
    thousands of lines for exactly the folders this feature targets. Mirrors
    the coarse-summary style the rest of this file's dry-run branches
    already use (deliberately avoiding ``transfer_tree(dry_run=True)``'s own
    per-file logging)."""
    if not assemble:
        return
    from octacam.twophoton_tiff import plan_assembly

    plan = plan_assembly(folder)
    for ch in plan.ready:
        log.info(
            "[dry-run] 2P tiff-assemble: %s %s: would assemble %d frame(s) -> %s",
            folder, ch.channel, len(ch.source_files), ch.dest_name,
        )
    for ch in plan.problems:
        log.warning(
            "[dry-run] 2P tiff-assemble: %s %s: %s — would copy %d frame file(s) individually",
            folder, ch.channel, ch.reason, len(ch.source_files),
        )


def _transfer_image_folder_with_assembly(src: Path, dest: Path, *, verify: bool, checksum: bool, assemble: bool):
    """``transfer_tree``, but for an ``image``-kind 2P folder: any channel
    ``plan_assembly`` finds ready is excluded from the generic recursive copy
    and replaced by one assembled OME-TIFF stack; anything that can't be
    safely assembled (or assembly isn't enabled/available) falls through to
    a plain per-file copy, same as today. Guarantees every source file ends
    up on *dest* one way or another — an assembly failure means falling back
    to copying those frames individually, never silently dropping them."""
    from octacam.transfer import transfer_folder, transfer_tree
    from octacam.twophoton_tiff import (
        assemble_channel_stack,
        make_progress_logger,
        plan_assembly,
    )
    from octacam.twophoton_transfer import EXPERIMENT_XML_FILENAME

    if not assemble:
        return transfer_tree(src, dest, verify=verify, checksum=checksum)
    plan = plan_assembly(src)
    ready = plan.ready
    if not ready:
        return transfer_tree(src, dest, verify=verify, checksum=checksum)

    exclude = frozenset(f for ch in ready for f in ch.source_files)
    result = transfer_tree(src, dest, verify=verify, checksum=checksum, exclude=exclude)
    experiment_xml = src / EXPERIMENT_XML_FILENAME
    for ch in ready:
        dest_path = dest / ch.dest_name
        assembled = assemble_channel_stack(
            ch.source_files, dest_path,
            axis=ch.axis or "T",
            experiment_xml=experiment_xml if experiment_xml.is_file() else None,
            on_progress=make_progress_logger(src, ch.channel),
        )
        if assembled.ok:
            result.copied.append(dest_path.name)
            log.info(
                "2P tiff-assemble: %s %s: assembled %d frame(s) -> %s",
                src, ch.channel, assembled.frame_count, dest_path,
            )
        else:
            log.error(
                "2P tiff-assemble: %s %s: %s — falling back to per-frame copy",
                src, ch.channel, assembled.error,
            )
            fallback = transfer_folder(
                src, dest, files_only=ch.source_files, verify=verify, checksum=checksum
            )
            result.copied.extend(fallback.copied)
            result.failed.extend(fallback.failed)
    return result


def _finalize_twophoton_matches(
    folder: Path,
    dest: Path,
    settled: list,
    source_root: Path,
    *,
    verify: bool,
    checksum: bool,
    dry_run: bool,
    assemble_tiff_stacks: bool = True,
) -> tuple[list[Path], list[Path], int]:
    """Transfer every settled match to ``dest/2P/<name>`` (plus, for an
    ``image`` kind, its own ``Experiment.xml`` alongside the take), then write
    + transfer the take's ``twophoton_match.json`` sidecar recording what was
    matched. Shared by Phase 3b's normal per-take matching
    (:func:`_grid_and_transfer`) and the 2P-only sweep's retroactive match
    against an already-finalized take (:func:`_sweep_unclaimed_twophoton`) —
    the exact transfer shape both need. Returns (every settled match's source
    path, only the ones that transferred cleanly — safe for a caller to later
    delete as --delete-after-transfer does, number of files that failed to
    transfer); never raises."""
    from octacam.transfer import transfer_folder, transfer_tree
    from octacam.transform import TWOPHOTON_MATCH_FILENAME
    from octacam.twophoton_transfer import EXPERIMENT_XML_FILENAME, build_match_record

    matched_paths: list[Path] = []
    ok_paths: list[Path] = []
    n_failed = 0
    for m in settled:
        matched_paths.append(m.folder.path)
        dest_2p = dest / "2P" / m.folder.path.name
        if dry_run:
            log.info(
                "[dry-run] 2P transfer (%s): %s -> %s", m.confidence, m.folder.path, dest_2p
            )
            if m.folder.kind == "image":
                _log_twophoton_assembly_preview(m.folder.path, assemble_tiff_stacks)
            continue
        if m.folder.kind == "image":
            result = _transfer_image_folder_with_assembly(
                m.folder.path, dest_2p, verify=verify, checksum=checksum,
                assemble=assemble_tiff_stacks,
            )
        else:
            result = transfer_tree(m.folder.path, dest_2p, verify=verify, checksum=checksum)
        n_failed += len(result.failed)
        if result and not result.failed:
            ok_paths.append(m.folder.path)
            log.info("2P transfer: transferred %s -> %s", m.folder.path, dest_2p)
        else:
            log.error("2P transfer: failed copying %s", m.folder.path)
        if m.folder.kind == "image":
            xml_src = m.folder.path / EXPERIMENT_XML_FILENAME
            if xml_src.is_file():
                transfer_folder(
                    m.folder.path, dest, files_only=[xml_src], verify=verify, checksum=checksum
                )
    if not dry_run:
        record = build_match_record(settled, source_root)
        match_path = folder / TWOPHOTON_MATCH_FILENAME
        match_path.write_text(json.dumps(record, indent=2) + "\n")
        transfer_folder(folder, dest, files_only=[match_path], verify=verify, checksum=checksum)
    return matched_paths, ok_paths, n_failed


def _sweep_unclaimed_twophoton(
    source_root: Path,
    dest_root: Path,
    twophoton_cfg,
    *,
    checksum: bool,
    dry_run: bool,
    also_exclude: frozenset[Path] = frozenset(),
) -> tuple[int, int]:
    """Transfer every settled ThorSync/ThorImage folder under *source_root*
    that no behavior take has already claimed (see
    :func:`_already_matched_twophoton_paths`) to
    ``dest_root/2p_only/<experiment>/<date>/<name>`` — the replacement for
    the legacy script's second ("2P data only") loop. Unlike the per-take
    Phase 3 in :func:`_grid_and_transfer`, there is no behavior take to gate
    this on; every settled, unclaimed folder found is copied.

    *also_exclude* covers matches made earlier in the *same* run that aren't
    on disk yet to be discovered by :func:`_already_matched_twophoton_paths`
    — only actually needed for ``--dry-run`` (a real run's Phase 3 writes
    each match's ``twophoton_match.json`` before this ever runs), but passed
    unconditionally so the dry-run preview matches what a real run would do.

    Before writing off any settled, unclaimed folder as genuinely standalone,
    also checks it against every still-pending, already-finalized take (see
    :func:`_pending_twophoton_takes`) — the confirmed real bug this closes: a
    behavior take can be finalized (behavior data transferred) by one
    `process` run before its true 2P counterpart has appeared/settled on the
    share, and without this retroactive check the pair would be permanently
    missed (nothing else ever revisits an already-finalized take).

    Pure: never raises, never exits — returns ``(n_copied, n_failed)`` file
    counts (0, 0) when there's nothing to do. Shared by the standalone
    ``--twophoton-sweep`` mode and the automatic post-processing sweep a
    normal `process` run does for every ``[transfer.twophoton]`` source it
    touched (see the `process` docstring / `--no-twophoton-sweep`).
    """
    from octacam.transfer import transfer_tree
    from octacam.twophoton_transfer import (
        discover_twophoton_folders,
        is_settled,
        match_takes_to_twophoton_batch,
    )

    sweep_root = dest_root / "2p_only"
    already_matched = _already_matched_twophoton_paths(source_root) | also_exclude

    candidates = discover_twophoton_folders(source_root)
    todo = [
        c
        for c in candidates
        if is_settled(c, twophoton_cfg.settle_s) and c.path.resolve() not in already_matched
    ]

    n_copied = n_failed = 0

    pending = _pending_twophoton_takes(source_root)
    if pending and todo:
        pending_groups: dict[tuple, list] = {}
        for local_folder, take, record in pending:
            key = (
                record.get("match_window_s", twophoton_cfg.match_window_s),
                record.get("verify_window_s", twophoton_cfg.verify_window_s),
                record.get("verify_with_signals", twophoton_cfg.verify_with_signals),
            )
            pending_groups.setdefault(key, []).append((local_folder, take, record))
        for (match_window_s, verify_window_s, verify_with_signals), items in (
            pending_groups.items()
        ):
            if not todo:
                break
            takes = [take for _, take, _ in items]
            by_folder = {take.folder: (local_folder, record) for local_folder, take, record in items}
            batch = match_takes_to_twophoton_batch(
                takes, todo, match_window_s, verify_window_s, verify_with_signals
            )
            for take_folder, matches in batch.items():
                if not matches:
                    continue
                local_folder, record = by_folder[take_folder]
                dest = Path(record["dest"])
                _matched, ok_paths, n_failed_here = _finalize_twophoton_matches(
                    local_folder,
                    dest,
                    matches,
                    source_root,
                    verify=record.get("verify", checksum),
                    checksum=record.get("checksum", checksum),
                    dry_run=dry_run,
                    assemble_tiff_stacks=record.get(
                        "assemble_tiff_stacks", twophoton_cfg.assemble_tiff_stacks
                    ),
                )
                n_failed += n_failed_here
                n_copied += len(ok_paths)
                log.info(
                    "2P sweep: retroactively paired %s with already-finalized take %s",
                    ", ".join(str(m.folder.path) for m in matches),
                    local_folder,
                )
                if not dry_run:
                    _clear_twophoton_pending(local_folder)
                    if record.get("checksum"):  # delete_wanted at write time
                        for src_2p in ok_paths:
                            try:
                                shutil.rmtree(src_2p)
                                log.info(
                                    "delete-after-transfer: removed 2P source %s "
                                    "(verified on NAS)",
                                    src_2p,
                                )
                            except OSError as e:
                                log.error(
                                    "delete-after-transfer: failed to remove %s: %s",
                                    src_2p,
                                    e,
                                )
                claimed = {m.folder.path.resolve() for m in matches}
                todo = [c for c in todo if c.path.resolve() not in claimed]

    if not todo:
        log.info("2P sweep: nothing new and settled under %s", source_root)
        return (n_copied, n_failed)

    for folder in todo:
        experiment = folder.path.parent.name
        date_str = time.strftime("%y%m%d", time.localtime(folder.start_time))
        dest = sweep_root / experiment / date_str / folder.path.name
        if dry_run:
            log.info("[dry-run] 2P sweep: %s -> %s", folder.path, dest)
            if folder.kind == "image":
                _log_twophoton_assembly_preview(folder.path, twophoton_cfg.assemble_tiff_stacks)
            continue
        if folder.kind == "image":
            result = _transfer_image_folder_with_assembly(
                folder.path, dest, verify=checksum, checksum=checksum,
                assemble=twophoton_cfg.assemble_tiff_stacks,
            )
        else:
            result = transfer_tree(folder.path, dest, verify=checksum, checksum=checksum)
        n_copied += len(result.copied)
        n_failed += len(result.failed)
        if result.failed:
            log.error("2P sweep: failed transferring %s", folder.path)
        else:
            log.info("2P sweep: transferred %s -> %s", folder.path, dest)
    if dry_run:
        log.info("[dry-run] 2P sweep: %d folder(s) would be transferred", len(todo))
    else:
        log.info("2P sweep: %d file(s) copied, %d failed", n_copied, n_failed)
    return (n_copied, n_failed)


def _run_twophoton_sweep(cli_config_dir: Path | None, dry_run: bool) -> None:
    """``octacam process --twophoton-sweep``: the standalone CLI entry point
    for :func:`_sweep_unclaimed_twophoton` — resolves ``--config`` into a
    source/destination/settings and reports failures as a nonzero exit."""
    from octacam.config import load_config_dir, resolve_dir_template

    if cli_config_dir is None:
        sys.exit("--twophoton-sweep requires --config <rig-config-dir>")
    cfg = load_config_dir(cli_config_dir)
    if cfg.transfer is None or not cfg.transfer.directory:
        sys.exit(f"--twophoton-sweep: no [transfer].directory configured in {cli_config_dir}")
    twophoton_cfg = cfg.transfer.twophoton
    if twophoton_cfg is None or not twophoton_cfg.source:
        sys.exit(f"--twophoton-sweep: no [transfer.twophoton] configured in {cli_config_dir}")

    source_root = Path(resolve_dir_template(twophoton_cfg.source))
    dest_root = Path(resolve_dir_template(cfg.transfer.directory))
    _n_copied, n_failed = _sweep_unclaimed_twophoton(
        source_root,
        dest_root,
        twophoton_cfg,
        checksum=cfg.transfer.checksum,
        dry_run=dry_run,
    )
    if n_failed:
        sys.exit(f"{n_failed} file(s) failed to transfer")


def _run_layout_migration(cli_config_dir: Path | None, dry_run: bool) -> None:
    """``octacam process --migrate-layout``: reorganize old flat transfers.

    Recordings transferred before the Behavior/Renderings/2P split landed sit
    flat under their destination folder (``camera_LF.mp4`` directly, not
    ``Behavior/camera_LF.mp4``) — indistinguishable from "not yet transferred"
    to the new layout-aware skip check, which would otherwise recopy
    everything. This moves each flat ``*.mp4`` into its correct subfolder
    **in place**: a rename within the same destination folder is always a
    same-filesystem operation (true for a local disk and for CIFS/SMB, whose
    rename is itself a server-side directory-entry change), so no bytes are
    read, written, or re-verified — data integrity cannot be at risk the way
    a copy could be. ``recording_summary.json``/``octacam_config.toml`` need
    no move: the old layout already put the summary at the same root path the
    new layout expects, and the config snapshot was never transferred by the
    old code at all (a later `octacam process` run adds it fresh).

    Classifies each flat file by comparing its stem against
    ``recording_summary.json``'s own camera list (a "Behavior" file) vs.
    everything else (a "Renderings" file, e.g. `grid.mp4`) — self-contained,
    no local recording folder needed, so this still works for a recording
    whose local copy is long gone.
    """
    from octacam.config import load_config_dir, resolve_dir_template
    from octacam.transform import RECORDING_SUMMARY_FILENAME

    if cli_config_dir is None:
        sys.exit("--migrate-layout requires --config <rig-config-dir>")
    cfg = load_config_dir(cli_config_dir)
    if cfg.transfer is None or not cfg.transfer.directory:
        sys.exit(f"--migrate-layout: no [transfer].directory configured in {cli_config_dir}")
    dest_root = Path(resolve_dir_template(cfg.transfer.directory))
    if not dest_root.is_dir():
        sys.exit(f"--migrate-layout: {dest_root} does not exist")

    n_moved = n_already = n_conflict = 0
    for summary_path in sorted(dest_root.rglob(RECORDING_SUMMARY_FILENAME)):
        folder = summary_path.parent
        flat_mp4s = sorted(folder.glob("*.mp4"))
        if not flat_mp4s:
            continue  # already migrated, or a fresh new-layout folder
        summary = _read_summary(summary_path) or {}
        camera_names = {
            c.get("name") for c in summary.get("cameras", []) if c.get("name")
        }
        for mp4 in flat_mp4s:
            subdir = "Behavior" if mp4.stem in camera_names else "Renderings"
            target = folder / subdir / mp4.name
            if dry_run:
                log.info("[dry-run] migrate: %s -> %s", mp4, target)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.exists():
                if target.stat().st_size == mp4.stat().st_size:
                    mp4.unlink()
                    n_already += 1
                    log.info(
                        "migrate: %s already present at %s (same size) — "
                        "removed the now-redundant flat copy",
                        mp4,
                        target,
                    )
                else:
                    n_conflict += 1
                    log.warning(
                        "migrate: %s and existing %s differ in size — left "
                        "both; please check by hand",
                        mp4,
                        target,
                    )
                continue
            try:
                mp4.rename(target)
            except OSError as e:
                n_conflict += 1
                log.error("migrate: failed to move %s -> %s: %s", mp4, target, e)
                continue
            n_moved += 1
            log.info("migrate: %s -> %s", mp4, target)
    if dry_run:
        log.info("[dry-run] migrate: scan complete under %s", dest_root)
        return
    log.info(
        "migrate: %d moved, %d already matched (flat copy removed), %d needing "
        "manual attention",
        n_moved,
        n_already,
        n_conflict,
    )
    if n_conflict:
        sys.exit(f"{n_conflict} file(s) need manual attention — see warnings above")


def _run_tiff_reassembly(cli_config_dir: Path | None, dry_run: bool) -> None:
    """``octacam process --reassemble-tiffs``: ad-hoc, manually-invoked
    reprocessing of already-transferred (or manually-copied) per-frame TIFF
    data anywhere under ``[transfer].directory`` into one OME-TIFF stack per
    channel — the standing home for this ability rather than a throwaway
    script, since the assembly library already exists for the transfer-time
    hook (:func:`_transfer_image_folder_with_assembly`) and exposing it this
    way costs almost nothing extra while staying discoverable
    (``octacam process --help``) for a future one-off need, e.g. a labmate
    who manually saved 2P data as multiple TIFFs.

    Unlike the transfer-time hook, this mode DELETES the now-redundant
    per-frame originals once the assembled stack is confirmed correct — a
    much higher bar than the transfer-time hook's cheaper page-count check:
    every page is read back and compared pixel-for-pixel against its source
    frame (``full_verify=True``) before anything is removed, one file at a
    time, only after the assembled stack is safely written and verified.
    Mirrors ``--migrate-layout``: requires ``--config``, scans the whole
    destination tree, safe to re-run (an already-consolidated folder is a
    pure no-op — :func:`~octacam.twophoton_tiff.plan_assembly` only reports a
    channel with more than one file present)."""
    from octacam.config import load_config_dir, resolve_dir_template
    from octacam.twophoton_tiff import (
        assemble_channel_stack,
        make_progress_logger,
        plan_assembly,
        tifffile_available,
    )
    from octacam.twophoton_transfer import EXPERIMENT_XML_FILENAME

    if cli_config_dir is None:
        sys.exit("--reassemble-tiffs requires --config <rig-config-dir>")
    if not tifffile_available():
        sys.exit("--reassemble-tiffs requires tifffile (pip install 'octacam[twophoton]')")
    cfg = load_config_dir(cli_config_dir)
    if cfg.transfer is None or not cfg.transfer.directory:
        sys.exit(f"--reassemble-tiffs: no [transfer].directory configured in {cli_config_dir}")
    dest_root = Path(resolve_dir_template(cfg.transfer.directory))
    if not dest_root.is_dir():
        sys.exit(f"--reassemble-tiffs: {dest_root} does not exist")

    n_assembled = n_problem = 0
    for experiment_xml in sorted(dest_root.rglob(EXPERIMENT_XML_FILENAME)):
        folder = experiment_xml.parent
        plan = plan_assembly(folder)
        for ch in plan.problems:
            n_problem += 1
            log.warning(
                "reassemble: %s channel %s: %s — leaving per-frame files as-is",
                folder, ch.channel, ch.reason,
            )
        for ch in plan.ready:
            dest_path = folder / ch.dest_name
            if dry_run:
                log.info(
                    "[dry-run] reassemble: %s %s: would assemble %d frame(s) -> %s, "
                    "then delete the originals",
                    folder, ch.channel, len(ch.source_files), dest_path,
                )
                continue
            result = assemble_channel_stack(
                ch.source_files, dest_path,
                axis=ch.axis or "T",
                experiment_xml=experiment_xml,
                full_verify=True,
                on_progress=make_progress_logger(folder, ch.channel),
            )
            if not result.ok:
                n_problem += 1
                log.error(
                    "reassemble: %s channel %s: assembly failed (%s) — originals left untouched",
                    folder, ch.channel, result.error,
                )
                continue
            n_assembled += 1
            log.info(
                "reassemble: %s %s: assembled and verified %d frame(s) -> %s",
                folder, ch.channel, result.frame_count, dest_path,
            )
            n_removed = 0
            for src in ch.source_files:
                try:
                    src.unlink()
                    n_removed += 1
                except OSError as e:
                    log.warning("reassemble: couldn't remove original %s: %s", src, e)
            log.info(
                "reassemble: %s %s: removed %d/%d original frame file(s)",
                folder, ch.channel, n_removed, len(ch.source_files),
            )
    if dry_run:
        log.info("[dry-run] reassemble: scan complete under %s", dest_root)
        return
    log.info(
        "reassemble: %d channel(s) assembled, %d needing manual attention",
        n_assembled, n_problem,
    )
    if n_problem:
        sys.exit(f"{n_problem} channel(s) need manual attention — see warnings above")


def _grid_and_transfer(
    folder_outputs: dict[Path, list[Path]],
    do_grid: bool,
    do_transfer: bool,
    cli_config_dir: Path | None,
    dry_run: bool,
    show_bar: bool,
    force: bool = False,
    reporter: "JobReporter | None" = None,
    job_dir: Path | None = None,
    delete_after_transfer: bool = False,
    no_delete_after_transfer: bool = False,
    transcode_failed_folders: frozenset[Path] = frozenset(),
    no_twophoton_sweep: bool = False,
) -> int:
    """Build visualization grids, transfer each folder (behavior + any paired
    2-photon data), and — when requested — delete local copies once
    everything is checksum-verified on the NAS.

    Five sequential phases, each getting its own progress bar: grids, behavior
    transfer (root metadata / ``Behavior`` / ``Renderings`` — see
    ``_transfer_dest``), 2-photon transfer (only for takes the ``twophoton``
    plugin reports as armed and whose config has ``[transfer.twophoton]``
    set), an automatic 2P-only sweep of every ``[transfer.twophoton]`` source
    this run touched (settled folders no take claimed — see
    ``_sweep_unclaimed_twophoton``; ``no_twophoton_sweep`` skips it), then
    delete-after-transfer. Returns the number of files that failed to
    transfer (behavior + 2P + sweep). When ``reporter`` is set (a detached
    job) each phase reports progress and pauses between folders while a
    gui/record owns the cameras."""
    from octacam.config import resolve_dir_template
    from octacam.grid import build_grid_video
    from octacam.transfer import transfer_folder
    from octacam.transform import RECORDING_SUMMARY_FILENAME
    from octacam.twophoton_transfer import (
        TakeInfo,
        discover_twophoton_folders,
        is_settled,
        match_takes_to_twophoton_batch,
    )

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
        if reporter is not None:
            reporter.begin_phase("grid", len(folder_outputs))
        with grid_bar or contextlib.nullcontext():
            for i, folder in enumerate(folder_outputs, 1):
                _pause_gate(reporter, job_dir, unit="folder")
                if reporter is not None:
                    reporter.item_started(i, len(folder_outputs), folder)
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
                if reporter is not None:
                    reporter.item_done()
        if grid_skipped:
            log.info(
                "%sGrid: %d already exist — skipping (use --force to rebuild)",
                "[dry-run] " if dry_run else "",
                grid_skipped,
            )

    # --- Phase 2: behavior transfer (root metadata / Behavior / Renderings) -
    transfer_failed = 0
    folder_dest: dict[Path, Path] = {}
    folder_failed: dict[Path, int] = {}
    folder_delete_after: dict[Path, bool] = {}
    folder_2p_ok: dict[Path, list[Path]] = {}
    if do_transfer:
        n_copied = n_skipped = 0
        transfer_bar = _TransferProgressBar() if (show_bar and not dry_run) else None
        if reporter is not None:
            reporter.begin_phase("transfer", len(folder_outputs))
        with transfer_bar or contextlib.nullcontext():
            transfer_cb = transfer_bar.make_callback() if transfer_bar else None
            for i, (folder, outputs) in enumerate(folder_outputs.items(), 1):
                _pause_gate(reporter, job_dir, unit="folder")
                if reporter is not None:
                    reporter.item_started(i, len(folder_outputs), folder)
                cfg = folder_cfgs[folder]
                dest = _transfer_dest(cfg, folder)
                if dest is None:
                    if reporter is not None:
                        reporter.item_done()
                    continue
                folder_dest[folder] = dest
                # cfg.transfer is guaranteed non-None here: _transfer_dest
                # already returned None (and we `continue`d above) otherwise.
                assert cfg.transfer is not None
                transfer_cfg = cfg.transfer
                delete_wanted = not no_delete_after_transfer and (
                    delete_after_transfer or transfer_cfg.delete_after_transfer
                )
                folder_delete_after[folder] = delete_wanted
                # Deleting the only copy must never ride on a size-only check —
                # force full verification when delete-after-transfer is active
                # for this folder, on top of (never weaker than) the config's
                # own checksum setting.
                verify = transfer_cfg.checksum or delete_wanted
                # Repair-mode re-verify of already-present files is separate
                # and pricier (re-hashes every prior transfer over the network
                # on every rerun) — only force it when a delete actually
                # depends on it, not just because [transfer].checksum is on.
                checksum = delete_wanted

                config_snapshot = folder / "octacam_config.toml"
                root_files = [config_snapshot] if config_snapshot.exists() else []
                on_prog = transfer_cb
                if transfer_bar is None and reporter is not None:
                    on_prog = reporter.transfer_progress(i, len(folder_outputs))

                # `outputs` is precise per-camera transcode output when
                # do_transcode ran, but under --no-transcode it comes from a
                # naive `folder.glob("*.mp4")` (see the transcode `else`
                # branch above) that can't tell a per-camera file from
                # grid.mp4 sitting in the same folder — exclude known
                # visualization output names so a composite never gets
                # misrouted into Behavior/ (raw per-camera data).
                viz_names = {name for name, _, _ in _visualizations_for(cfg, [])}
                behavior_files = [f for f in outputs if f.name not in viz_names]

                failed_here = 0
                for call_dest, files in (
                    (dest, root_files),
                    (dest / "Behavior", behavior_files),
                    (dest / "Renderings", grid_files.get(folder, [])),
                ):
                    result = transfer_folder(
                        folder,
                        call_dest,
                        files_only=files,
                        dry_run=dry_run,
                        verify=verify,
                        checksum=checksum,
                        on_progress=on_prog,
                    )
                    n_copied += len(result.copied)
                    n_skipped += len(result.skipped)
                    failed_here += len(result.failed)
                transfer_failed += failed_here
                folder_failed[folder] = failed_here
                if reporter is not None:
                    reporter.item_done()
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

        # --- Phase 3: 2-photon transfer --------------------------------------
        # 3a: gather every armed take into groups sharing one [transfer.twophoton]
        # source, so matching runs once per group across ALL its takes together
        # (chronological, no-double-booking — see match_takes_to_twophoton_batch)
        # rather than independently per folder.
        # Group key: (source_root, match_window_s, verify_window_s, settle_s, verify_with_signals)
        groups = {}
        group_meta = {}
        # Every distinct (source_root, dest_root) a processed folder's config
        # points at, for the automatic post-processing sweep below — tracked
        # regardless of whether this particular take turned out armed/matched,
        # since the config's mere presence means this rig does 2P work.
        sweep_targets: dict[tuple[Path, Path], tuple[object, bool]] = {}
        for folder in folder_dest:
            cfg = folder_cfgs[folder]
            # cfg.transfer is guaranteed non-None: folder_dest only holds
            # folders _transfer_dest resolved a destination for above.
            assert cfg.transfer is not None
            twophoton_cfg = cfg.transfer.twophoton
            if twophoton_cfg is None or not twophoton_cfg.source:
                continue
            source_root_for_sweep = Path(resolve_dir_template(twophoton_cfg.source))
            dest_root_for_sweep = Path(resolve_dir_template(cfg.transfer.directory))
            sweep_targets[(source_root_for_sweep, dest_root_for_sweep)] = (
                twophoton_cfg,
                cfg.transfer.checksum,
            )
            summary = _read_summary(folder / RECORDING_SUMMARY_FILENAME) or {}
            if summary.get("schema_version", 0) < 4:
                # Predates the recording_metadata feature entirely — there was
                # no way for this take to record "armed" even if it genuinely
                # ran alongside a 2P acquisition. Fall back to attempting the
                # match anyway (still gated on [transfer.twophoton] being
                # configured, checked above) rather than silently losing every
                # pre-feature recording's pairing.
                armed = True
                log.info(
                    "2P transfer: %s has no armed flag (schema_version %s, "
                    "predates the feature) — attempting a legacy time-window "
                    "match anyway",
                    folder,
                    summary.get("schema_version"),
                )
            else:
                armed = (
                    summary.get("plugins", {}).get("twophoton", {}).get("armed", False)
                )
            if not armed:
                continue
            take_start_ns = summary.get("start_time_ns")
            duration_s = summary.get("duration_s")
            if take_start_ns is None or duration_s is None:
                log.warning(
                    "2P transfer: %s has no start_time_ns/duration_s; skipping",
                    folder,
                )
                continue
            camera_frame_counts = [
                c["frames"]
                for c in summary.get("cameras", [])
                if isinstance(c.get("frames"), int)
            ]
            key = (
                source_root_for_sweep,
                twophoton_cfg.match_window_s,
                twophoton_cfg.verify_window_s,
                twophoton_cfg.settle_s,
                twophoton_cfg.verify_with_signals,
            )
            groups.setdefault(key, []).append(
                TakeInfo(
                    folder=folder,
                    start_time=take_start_ns / 1e9,
                    duration_s=duration_s,
                    camera_frame_counts=camera_frame_counts,
                )
            )
            group_meta[key] = twophoton_cfg

        # 3b: match + transfer each group.
        # Folders matched to a take in THIS run — tracked in memory because
        # dry-run never writes twophoton_match.json to disk, so the sweep
        # below (3c) can't rely on _already_matched_twophoton_paths alone to
        # exclude a same-run match from its preview (a real run doesn't need
        # this: the sidecar is written before 3c starts).
        matched_this_run: set[Path] = set()
        for key, takes in groups.items():
            source_root, _, _, settle_s, verify_with_signals = key
            twophoton_cfg = group_meta[key]
            takes_by_folder = {t.folder: t for t in takes}
            candidates = discover_twophoton_folders(source_root)
            batch = match_takes_to_twophoton_batch(
                takes,
                candidates,
                twophoton_cfg.match_window_s,
                twophoton_cfg.verify_window_s,
                verify_with_signals,
            )
            for folder, matches in batch.items():
                dest = folder_dest[folder]
                cfg = folder_cfgs[folder]
                assert cfg.transfer is not None
                delete_wanted = folder_delete_after.get(folder, False)
                verify = cfg.transfer.checksum or delete_wanted
                checksum = delete_wanted
                if not matches:
                    log.info("2P transfer: no matching 2P folder found for %s", folder)
                    if not dry_run:
                        _write_twophoton_pending(
                            folder,
                            takes_by_folder[folder],
                            dest=dest,
                            source_root=source_root,
                            twophoton_cfg=twophoton_cfg,
                            checksum=checksum,
                            verify=verify,
                        )
                    continue
                for m in matches:
                    if m.ambiguous:
                        log.warning(
                            "2P transfer: more than one %s candidate overlapped %s — "
                            "picked the closest (%s, gap %.1fs)",
                            m.folder.kind,
                            folder,
                            m.folder.path,
                            m.gap_s,
                        )
                settled = [m for m in matches if is_settled(m.folder, settle_s)]
                if len(settled) < len(matches):
                    log.info(
                        "2P transfer: %d matched folder(s) for %s not yet settled "
                        "(still writing) — will retry on a later `octacam process` "
                        "run, or the 2P-only sweep once it settles",
                        len(matches) - len(settled),
                        folder,
                    )
                if not settled:
                    if not dry_run:
                        _write_twophoton_pending(
                            folder,
                            takes_by_folder[folder],
                            dest=dest,
                            source_root=source_root,
                            twophoton_cfg=twophoton_cfg,
                            checksum=checksum,
                            verify=verify,
                        )
                    continue
                matched_paths, ok_paths, n_failed = _finalize_twophoton_matches(
                    folder,
                    dest,
                    settled,
                    source_root,
                    verify=verify,
                    checksum=checksum,
                    dry_run=dry_run,
                    assemble_tiff_stacks=twophoton_cfg.assemble_tiff_stacks,
                )
                matched_this_run.update(p.resolve() for p in matched_paths)
                transfer_failed += n_failed
                folder_failed[folder] = folder_failed.get(folder, 0) + n_failed
                folder_2p_ok.setdefault(folder, []).extend(ok_paths)
                if not dry_run:
                    _clear_twophoton_pending(folder)

        # --- Phase 3c: automatic 2P-only sweep --------------------------------
        # Every [transfer.twophoton] source this run touched also gets swept
        # for settled folders no take claimed (calibration/tuning/2P-only
        # recordings, safe to store separately) — sequentially, automatically,
        # right after the per-take matching above, not a separate manual step.
        if not no_twophoton_sweep:
            for (source_root, dest_root), (twophoton_cfg, checksum) in sweep_targets.items():
                _n_copied, n_failed = _sweep_unclaimed_twophoton(
                    source_root,
                    dest_root,
                    twophoton_cfg,
                    checksum=checksum,
                    dry_run=dry_run,
                    also_exclude=frozenset(matched_this_run),
                )
                transfer_failed += n_failed

        # --- Phase 4: delete-after-transfer -----------------------------------
        # Evaluated (and logged) under --dry-run too — a caller deciding
        # whether it's safe to reclaim local disk space needs to see exactly
        # what this would remove before it actually removes anything;
        # `shutil.rmtree` itself stays gated on `not dry_run`.
        for folder in folder_dest:
            if not folder_delete_after.get(folder, False):
                continue
            if folder in transcode_failed_folders:
                log.warning(
                    "delete-after-transfer: skipping %s — it had a transcode "
                    "failure this run",
                    folder,
                )
                continue
            if folder_failed.get(folder, 0):
                log.warning(
                    "delete-after-transfer: skipping %s — %d file(s) failed "
                    "to transfer",
                    folder,
                    folder_failed[folder],
                )
                continue
            if dry_run:
                log.info(
                    "[dry-run] delete-after-transfer: would remove local %s "
                    "(verified on NAS)",
                    folder,
                )
            else:
                try:
                    shutil.rmtree(folder)
                    log.info(
                        "delete-after-transfer: removed local %s (verified on NAS)",
                        folder,
                    )
                except OSError as e:
                    log.error("delete-after-transfer: failed to remove %s: %s", folder, e)
            for src_2p in folder_2p_ok.get(folder, []):
                if dry_run:
                    log.info(
                        "[dry-run] delete-after-transfer: would remove 2P source %s "
                        "(verified on NAS)",
                        src_2p,
                    )
                    continue
                try:
                    shutil.rmtree(src_2p)
                    log.info(
                        "delete-after-transfer: removed 2P source %s "
                        "(verified on NAS)",
                        src_2p,
                    )
                except OSError as e:
                    log.error(
                        "delete-after-transfer: failed to remove %s: %s", src_2p, e
                    )

    return transfer_failed


def _rebuild_process_argv(
    folders: list[Path],
    *,
    recursive: bool,
    no_transcode: bool,
    no_grid: bool,
    no_transfer: bool,
    force: bool,
    config_dir: Path | None,
    delete_source: bool,
    no_delete_source: bool,
    delete_after_transfer: bool,
    no_delete_after_transfer: bool,
    no_twophoton_sweep: bool,
    dry_run: bool,
) -> list[str]:
    """Rebuild a canonical, absolute ``process`` argv for a detached re-exec.

    Cache selectors (``--last``/``--session-id``/``--all``) are already resolved to
    ``folders`` so they are dropped; every path is made absolute (the child runs
    from ``$HOME`` with no inherited cwd). ``--progress-style`` is omitted so the
    detached ``log.txt`` stays line-oriented rather than a verbatim ffmpeg stream.
    """
    argv: list[str] = []
    if no_transcode:
        argv.append("--no-transcode")
    if no_grid:
        argv.append("--no-grid")
    if no_transfer:
        argv.append("--no-transfer")
    if force:
        argv.append("--force")
    if recursive:
        argv.append("--recursive")
    if delete_source:
        argv.append("--delete-source")
    if no_delete_source:
        argv.append("--no-delete-source")
    if delete_after_transfer:
        argv.append("--delete-after-transfer")
    if no_delete_after_transfer:
        argv.append("--no-delete-after-transfer")
    if no_twophoton_sweep:
        argv.append("--no-twophoton-sweep")
    if dry_run:
        argv.append("--dry-run")
    if config_dir is not None:
        argv += ["--config", str(config_dir.resolve())]
    argv += [str(Path(f).resolve()) for f in folders]
    return argv


def _pause_gate(
    reporter: "JobReporter | None", job_dir: Path | None, *, unit: str
) -> None:
    """Block at a work-unit boundary while capture is active or a manual pause is set.

    Polls ~1 s and stays paused while either condition holds, then resumes at the
    next un-done unit (the idempotent output-exists skip gives resume-in-place).
    It deliberately does NOT catch ``KeyboardInterrupt``, so a cancel/Ctrl-C during
    the pause interrupts the sleep and wins over the pause. A plain foreground run
    (``reporter``/``job_dir`` None) pauses the same way — only the capture-active
    condition applies there (there is no job to manually pause).
    """
    from octacam import process_jobs, session_cache

    announced = False
    while True:
        capture = session_cache.capture_active()
        manual = job_dir is not None and process_jobs.is_manually_paused(job_dir)
        if not (capture or manual):
            break
        reasons = []
        if capture:
            reasons.append("capture-active")
        if manual:
            reasons.append("manual")
        reason = "+".join(reasons)
        if not announced:
            log.info("Paused before next %s — %s. Resumes automatically.", unit, reason)
            announced = True
        if reporter is not None:
            reporter.set_paused(True, reason)
        time.sleep(1.0)
    if announced:
        if reporter is not None:
            reporter.set_paused(False, None)
        log.info("Resumed processing.")


def _inject_default_last(args: list[str]) -> list[str]:
    """Give a value-less ``--last`` its default (``recording``) before parsing.

    ``--last`` takes an optional value (``recording`` | ``session``), but typer
    doesn't forward click's optional-value (flag_value) support, and doing it
    via click internals would be brittle across the ``typer>=0.15`` range. So we
    normalize the raw args here instead: a ``--last`` that ends the list or is
    followed by another option gets an explicit ``recording`` inserted after it.
    Tokens after a ``--`` separator are left untouched.
    """
    out: list[str] = []
    seen_ddash = False
    for i, tok in enumerate(args):
        out.append(tok)
        if seen_ddash:
            continue
        if tok == "--":
            seen_ddash = True
        elif tok == "--last":
            nxt = args[i + 1] if i + 1 < len(args) else None
            if nxt is None or nxt.startswith("-"):
                out.append("recording")
    return out


class _ProcessCommand(typer.core.TyperCommand):
    """`process` command whose ``--last`` accepts an optional value.

    Bare ``--last`` means ``--last recording``; see :func:`_inject_default_last`.
    """

    def parse_args(self, ctx, args):  # type: ignore[override]
        return super().parse_args(ctx, _inject_default_last(args))


@app.command(cls=_ProcessCommand)
def process(
    paths: Annotated[
        list[Path] | None,
        typer.Argument(
            exists=True,
            help="Recording folders (or parent directories with -r). Omit when "
            "using --last/--session-id/--all.",
        ),
    ] = None,
    last: Annotated[
        str | None,
        typer.Option(
            "--last",
            metavar="[recording|session]",
            help="Process the most recent recording (--last or --last recording) "
            "or every folder from the last GUI session (--last session).",
        ),
    ] = None,
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
    twophoton_sweep: Annotated[
        bool,
        typer.Option(
            "--twophoton-sweep",
            help="2P-only mode: instead of processing recording folders, sweep "
            "[transfer.twophoton].source for settled ThorSync/ThorImage folders "
            "with no matching behavior take, and transfer them to "
            "<transfer.directory>/2p_only/<experiment>/<date>/<name>. Requires "
            "--config; ignores PATHS/--last/--session-id/--all.",
        ),
    ] = False,
    migrate_layout: Annotated[
        bool,
        typer.Option(
            "--migrate-layout",
            help="One-time mode: instead of processing recording folders, "
            "reorganize already-transferred recordings under [transfer].directory "
            "from the old flat layout into Behavior/Renderings subfolders, "
            "in place (a same-filesystem rename — no bytes are copied or "
            "re-verified, so this cannot corrupt data). Requires --config; "
            "ignores PATHS/--last/--session-id/--all. Safe to re-run.",
        ),
    ] = False,
    reassemble_tiffs: Annotated[
        bool,
        typer.Option(
            "--reassemble-tiffs",
            help="One-time/ad-hoc mode: instead of processing recording folders, "
            "assemble any remaining per-frame ThorImage TIFFs already on the NAS "
            "under [transfer].directory into one OME-TIFF stack per channel, "
            "deleting the now-redundant originals only after the assembled "
            "stack is written and pixel-verified. Requires --config; ignores "
            "PATHS/--last/--session-id/--all. Safe to re-run.",
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
            "-c",
            exists=True,
            file_okay=False,
            dir_okay=True,
            help="Fallback config dir for recordings that lack an embedded "
            "octacam_config.toml (older recordings). Normally not needed.",
        ),
    ] = None,
    delete_source: Annotated[
        bool,
        typer.Option(
            "--delete-source",
            "-d",
            help="Delete each source .mkv/.raw once it transcodes successfully. "
            "The recording_summary.json is kept. Also true when the recording's "
            "[transcode].delete_source config default is set; --no-delete-source "
            "forces it off regardless of that default.",
        ),
    ] = False,
    no_delete_source: Annotated[
        bool,
        typer.Option(
            "--no-delete-source",
            help="Force --delete-source off even when [transcode].delete_source "
            "is set in config.",
        ),
    ] = False,
    delete_after_transfer: Annotated[
        bool,
        typer.Option(
            "--delete-after-transfer",
            help="Once a folder's transfer (behavior + any matched 2P data) is "
            "100% checksum-verified on the NAS, delete the local recording "
            "folder and the matched 2P source folder(s) on the share. Always "
            "forces checksum verification, regardless of [transfer].checksum. "
            "Also true when [transfer].delete_after_transfer is set in config; "
            "--no-delete-after-transfer forces it off regardless.",
        ),
    ] = False,
    no_delete_after_transfer: Annotated[
        bool,
        typer.Option(
            "--no-delete-after-transfer",
            help="Force --delete-after-transfer off even when "
            "[transfer].delete_after_transfer is set in config.",
        ),
    ] = False,
    no_twophoton_sweep: Annotated[
        bool,
        typer.Option(
            "--no-twophoton-sweep",
            help="Skip the automatic 2P-only sweep this run otherwise does, "
            "sequentially, right after per-take 2P matching, for every "
            "[transfer.twophoton] source it touched (settled folders no take "
            "claimed — see --twophoton-sweep, the same logic run standalone).",
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
            "or copying files. Transcoding still runs normally; --delete-source is "
            "suppressed (logged, not applied).",
        ),
    ] = False,
    detach: Annotated[
        bool,
        typer.Option(
            "--detach",
            help="Run the pipeline as a detached background job that survives an "
            "SSH disconnect, then return its id. Watch it with `octacam jobs "
            "attach`; a running gui/record auto-pauses it.",
        ),
    ] = False,
    job_dir: Annotated[
        Path | None,
        # Internal: set on the re-exec'd detached child so it writes status/logs
        # into its job dir. Not for direct use.
        typer.Option("--_job-dir", hidden=True),
    ] = None,
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

    Instead of PATHS, pass --last (the most recent recording; same as --last
    recording), --last session (the last GUI session), --session-id (an exact
    session), or --all (every cached folder). Deleted folders are silently
    skipped.

    --twophoton-sweep, --migrate-layout, and --reassemble-tiffs are separate
    modes (ignore PATHS/--last/--session-id/--all/--detach) — see their own
    help text.
    """
    from octacam import process_jobs, session_cache
    from octacam.writer import is_partial_transcode, transcode_file

    if twophoton_sweep:
        _run_twophoton_sweep(config_dir, dry_run)
        return
    if migrate_layout:
        _run_layout_migration(config_dir, dry_run)
        return
    if reassemble_tiffs:
        _run_tiff_reassembly(config_dir, dry_run)
        return

    do_transcode = not no_transcode
    do_grid = not no_grid
    do_transfer = not no_transfer
    if not (do_transcode or do_grid or do_transfer):
        sys.exit("Nothing to do: --no-transcode, --no-grid and --no-transfer all set.")

    folders = _resolve_transcode_paths(
        list(paths or []), last, session_id, all_
    )

    # --detach: re-exec this same, already-resolved pipeline as a background job
    # that survives an SSH disconnect, then return its id. (The re-exec'd child
    # runs the very same command with --_job-dir set — the worker branch below.)
    if detach and job_dir is None:
        argv_tail = _rebuild_process_argv(
            folders,
            recursive=recursive,
            no_transcode=no_transcode,
            no_grid=no_grid,
            no_transfer=no_transfer,
            force=force,
            config_dir=config_dir,
            delete_source=delete_source,
            no_delete_source=no_delete_source,
            delete_after_transfer=delete_after_transfer,
            no_delete_after_transfer=no_delete_after_transfer,
            no_twophoton_sweep=no_twophoton_sweep,
            dry_run=dry_run,
        )
        status = process_jobs.spawn_detached(argv_tail=argv_tail, folders=folders)
        typer.echo(status.job_id)  # stdout: scriptable
        log.info(
            "Detached processing job %s — watch it with: octacam jobs attach %s",
            status.job_id,
            status.job_id,
        )
        raise typer.Exit()

    # Worker mode: the detached child records progress/logs into its job dir.
    worker = process_jobs.worker_start(job_dir) if job_dir is not None else None
    reporter = worker.reporter if worker is not None else None

    raw_output = progress_style is ProgressStyle.ffmpeg
    # A detached worker (job_dir set) forces color into its log.txt, which makes
    # the stderr console report is_terminal — but its animated bar would then
    # pollute the log with cursor-control codes and, worse, bypass the reporter's
    # status.json percent that `octacam jobs attach` renders. Keep the live bar to
    # real foreground runs; the worker's progress rides status.json instead.
    show_bar = not raw_output and worker is None and _stderr_console().is_terminal

    def _run() -> None:
        # Which output mp4s exist per source folder, so grid/transfer run once per
        # folder after its files are done. Insertion-ordered (3.7+).
        folder_outputs: dict[Path, list[Path]] = {}
        cfg_cache: dict[Path, object] = {}
        failures = 0
        completed = 0
        skipped = 0
        interrupted = False
        # A folder with any failed transcode this run must never be deleted by
        # --delete-after-transfer, even if every *other* camera's output
        # transferred cleanly — the failed camera's raw file was never
        # archived anywhere.
        transcode_failed_folders: set[Path] = set()

        # --- Transcode phase ------------------------------------------------
        if do_transcode:
            jobs = _transcode_jobs(folders, recursive)
            if not jobs:
                log.warning("No videos to transcode in: %s", ", ".join(map(str, folders)))
            else:
                bar = _TranscodeProgressBar(len(jobs)) if show_bar else None
                if reporter is not None:
                    reporter.begin_phase("transcode", len(jobs))
                with (
                    session_cache.mark_transcode_active(f"{len(jobs)} file(s)"),
                    bar or contextlib.nullcontext(),
                ):
                    # A Ctrl-C stops the batch where it stands: transcode_file kills
                    # its ffmpeg child and discards the partial output.
                    try:
                        for index, job in enumerate(jobs, 1):
                            # Pause between files while a gui/record owns the
                            # cameras (or a manual pause is set); a queued cancel
                            # (SIGINT) breaks the gate and wins.
                            _pause_gate(reporter, job_dir, unit="file")
                            input_path = job.input_path
                            output = input_path.with_suffix(".mp4")
                            if reporter is not None:
                                reporter.item_started(index, len(jobs), input_path)
                            if output.resolve() == input_path.resolve():
                                log.warning(
                                    "Skipping %s: already in target format (mp4)",
                                    input_path,
                                )
                                folder_outputs.setdefault(input_path.parent, []).append(
                                    output
                                )
                                if reporter is not None:
                                    reporter.item_done()
                                continue
                            folder = input_path.parent
                            if output.exists() and not force:
                                # Idempotent re-run: a finished .mp4 already sits at
                                # the target. Transcoding is atomic (temp + rename),
                                # so its presence means a complete encode — skip
                                # re-encoding, but still feed it to grid/transfer.
                                # Counted for the summary rather than logged per
                                # file so a full re-run doesn't spam one line per
                                # output.
                                skipped += 1
                                folder_outputs.setdefault(folder, []).append(output)
                                if reporter is not None:
                                    reporter.item_done()
                                continue
                            cfg = cfg_cache.get(folder)
                            if cfg is None:
                                cfg = _config_for_recording(folder, config_dir)
                                cfg_cache[folder] = cfg
                            if bar is not None:
                                on_progress = bar.file(index, input_path)
                            elif reporter is not None:
                                on_progress = reporter.transcode_progress(index, len(jobs))
                            else:
                                on_progress = None
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
                                transcode_failed_folders.add(folder)
                                log.error("Failed to transcode %s: %s", input_path, e)
                                continue
                            completed += 1
                            folder_outputs.setdefault(folder, []).append(output)
                            if reporter is not None:
                                reporter.item_done()
                            effective_delete_source = not no_delete_source and (
                                delete_source or cfg.transcode.delete_source
                            )
                            if effective_delete_source and not dry_run:
                                _delete_source_files(input_path)
                            elif effective_delete_source and dry_run:
                                log.info("[dry-run] would delete source: %s", input_path)
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

        # --- Grid + transfer phases -----------------------------------------
        transfer_failed = 0
        if not interrupted and (do_grid or do_transfer) and folder_outputs:
            transfer_failed = _grid_and_transfer(
                folder_outputs,
                do_grid,
                do_transfer,
                config_dir,
                dry_run,
                show_bar,
                force,
                reporter=reporter,
                job_dir=job_dir,
                delete_after_transfer=delete_after_transfer,
                no_delete_after_transfer=no_delete_after_transfer,
                transcode_failed_folders=frozenset(transcode_failed_folders),
                no_twophoton_sweep=no_twophoton_sweep,
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

    # A plain foreground run just executes; a worker records its terminal state.
    if worker is None:
        _run()
        return
    try:
        _run()
    except typer.Exit as e:
        code = e.exit_code
        if code == 130:
            worker.finish(process_jobs.CANCELLED, 130, None)
        elif code:
            worker.finish(
                process_jobs.FAILED,
                code if isinstance(code, int) else 1,
                "process reported failures",
            )
        else:
            worker.finish(process_jobs.DONE, 0, None)
        raise
    except SystemExit as e:
        msg = e.code if isinstance(e.code, str) else "process failed"
        worker.finish(process_jobs.FAILED, 1, msg)
        raise
    except BaseException as e:
        if isinstance(e, KeyboardInterrupt):
            worker.finish(process_jobs.CANCELLED, 130, None)
        else:
            worker.finish(process_jobs.FAILED, 1, repr(e))
        raise
    else:
        worker.finish(process_jobs.DONE, 0, None)


def _delete_source_files(input_path: Path) -> None:
    """Delete a transcoded source (.mkv/.raw) once it has been transcoded.

    Never removes the recording_summary.json. Deletion failures are logged but
    never fail the run (the transcode already succeeded)."""
    try:
        input_path.unlink(missing_ok=True)
    except OSError as e:
        log.warning("Could not remove %s: %s", input_path, e)


# ---------------------------------------------------------------------------
# `octacam jobs` — manage detached processing jobs
# ---------------------------------------------------------------------------

_JobArg = Annotated[
    str | None,
    typer.Argument(
        metavar="[JOB]",
        help="Job id (from `octacam jobs list`). Omit for the most recent job.",
    ),
]


@jobs_app.command("list")
def jobs_list() -> None:
    """List detached processing jobs and their progress."""
    from octacam import process_jobs

    process_jobs.render_table(process_jobs.list_jobs())


@jobs_app.command("attach")
def jobs_attach(job_id: _JobArg = None) -> None:
    """Follow a detached job's live log + progress (Ctrl-C detaches, does not cancel)."""
    from octacam import process_jobs

    job = process_jobs.resolve_job(job_id)
    if job is None:
        sys.exit("No such job." if job_id else "No processing jobs to attach to.")
    raise typer.Exit(process_jobs.attach(job, _stderr_console()))


@jobs_app.command("pause")
def jobs_pause(job_id: _JobArg = None) -> None:
    """Pause a running detached job (it parks at its next file/folder boundary)."""
    from octacam import process_jobs

    job = process_jobs.resolve_job(job_id, require_live=True)
    if job is None:
        sys.exit("No such live job." if job_id else "No live processing jobs to pause.")
    if process_jobs.pause(job):
        log.info("Requested pause of job %s.", job.job_id)
    else:
        sys.exit(f"Could not pause job {job.job_id}.")


@jobs_app.command("resume")
def jobs_resume(job_id: _JobArg = None) -> None:
    """Clear a manual pause (a gui/record auto-pause clears on its own)."""
    from octacam import process_jobs

    job = process_jobs.resolve_job(job_id, require_live=True)
    if job is None:
        sys.exit("No such live job." if job_id else "No live processing jobs to resume.")
    if process_jobs.resume(job):
        log.info("Cleared manual pause of job %s.", job.job_id)
    else:
        sys.exit(f"Could not resume job {job.job_id}.")


@jobs_app.command("cancel")
def jobs_cancel(job_id: _JobArg = None) -> None:
    """Cancel a running detached job (a clean stop; already-done work is kept)."""
    from octacam import process_jobs

    job = process_jobs.resolve_job(job_id, require_live=True)
    if job is None:
        sys.exit("No such live job." if job_id else "No live processing jobs to cancel.")
    if process_jobs.cancel(job):
        log.info("Cancelling job %s.", job.job_id)
    else:
        sys.exit(f"Could not cancel job {job.job_id} (it may have already finished).")


def _human_size(num_bytes: int) -> str:
    """A short human-readable byte size (e.g. ``0 B``, ``7.0 KB``, ``2.1 MB``)."""
    size = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024:
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"


def _plural(n: int, singular: str, plural: str) -> str:
    return singular if n == 1 else plural


@cache_app.command("path")
def cache_path() -> None:
    """Print the octacam cache directory (respects OCTACAM_CACHE_DIR / XDG_CACHE_HOME)."""
    from octacam import session_cache

    typer.echo(str(session_cache.cache_dir()))


@cache_app.command("info")
def cache_info() -> None:
    """Show the cache location, size, and a breakdown of what is cached."""
    from octacam import process_jobs, session_cache

    root = session_cache.cache_dir()
    if not root.exists():
        typer.echo(f"{root}  (nothing cached yet)")
        return

    n_recordings = session_cache.recordings_count()
    live_jobs, finished_jobs = process_jobs.job_dir_counts()
    live_markers = session_cache.transcode_running() + session_cache.capture_running()
    rec_size = _human_size(session_cache.dir_size(root / session_cache.CACHE_FILENAME))
    jobs_size = _human_size(session_cache.dir_size(process_jobs.jobs_dir()))

    typer.echo(f"{root}  ({_human_size(session_cache.dir_size(root))})")
    typer.echo(
        f"  recordings   {rec_size:>8}   "
        f"{n_recordings} {_plural(n_recordings, 'entry', 'entries')}"
    )
    typer.echo(
        f"  jobs         {jobs_size:>8}   {live_jobs} live, {finished_jobs} finished"
    )
    typer.echo(f"  markers      {'—':>8}   {live_markers} live")


@cache_app.command("clear")
def cache_clear(
    all_: Annotated[
        bool,
        typer.Option(
            "--all",
            help="Also remove finished detached-job logs (kept by default).",
        ),
    ] = False,
    yes: Annotated[
        bool,
        typer.Option("--yes", "-y", help="Clear without the confirmation prompt."),
    ] = False,
) -> None:
    """Clear cached state under the cache dir.

    Removes the recording list and orphaned activity markers; with `--all` it also
    removes finished detached-job logs. A live capture, transcode, or job is never
    touched.
    """
    from octacam import process_jobs, session_cache

    # A pre-confirm snapshot, used only for the "will clear" preview. The report
    # below re-reads the live/finished picture *after* clearing, so a job that
    # finishes (or a capture that ends) while the operator dwells at the prompt is
    # never both "Cleared" and "Kept: live".
    n_recordings = session_cache.recordings_count()
    rec_exists = (session_cache.cache_dir() / session_cache.CACHE_FILENAME).exists()

    if not yes:
        _, pre_finished = process_jobs.job_dir_counts()
        targets = []
        if rec_exists:
            targets.append(
                f"the recording list ({n_recordings} "
                f"{_plural(n_recordings, 'entry', 'entries')})"
            )
        if all_ and pre_finished:
            targets.append(
                f"{pre_finished} finished detached-job "
                f"{_plural(pre_finished, 'log', 'logs')}"
            )
        targets.append("stale activity markers")
        typer.echo(f"Cache dir: {session_cache.cache_dir()}")
        typer.echo("Will clear: " + "; ".join(targets) + ".")
        protected = _live_summary(
            *_live_counts(session_cache, process_jobs)
        )
        if protected:
            typer.echo(f"Protected (kept live): {protected}.")
        typer.confirm("Proceed?", abort=True)

    removed_rec = session_cache.clear_recordings()
    swept, _live_markers = session_cache.sweep_orphan_markers()
    removed_jobs = 0
    if all_:
        removed_jobs, _ = process_jobs.clear_finished()

    cleared = []
    if removed_rec:
        cleared.append("recording list")
    if swept:
        cleared.append(f"{swept} stale {_plural(swept, 'marker', 'markers')}")
    if removed_jobs:
        cleared.append(
            f"{removed_jobs} finished job {_plural(removed_jobs, 'log', 'logs')}"
        )
    typer.echo("Cleared: " + ", ".join(cleared) + "." if cleared else "Nothing needed clearing.")

    # Re-read the post-clear picture so the report matches what remains on disk.
    live_jobs, live_transcode, live_capture = _live_counts(session_cache, process_jobs)
    finished_jobs = process_jobs.job_dir_counts()[1]
    kept = _live_summary(live_jobs, live_transcode, live_capture)
    if not all_ and finished_jobs:
        note = (
            f"{finished_jobs} finished job "
            f"{_plural(finished_jobs, 'log', 'logs')} (use --all to remove)"
        )
        kept = f"{kept}, {note}" if kept else note
    if kept:
        typer.echo(f"Kept: {kept}.")


def _live_counts(session_cache, process_jobs) -> tuple[int, int, int]:
    """(live_jobs, live_transcodes, live_captures) on this machine right now."""
    return (
        process_jobs.job_dir_counts()[0],
        session_cache.transcode_running(),
        session_cache.capture_running(),
    )


def _live_summary(live_jobs: int, live_transcode: int, live_capture: int) -> str:
    """Join the currently-live processes into a human phrase (empty if none)."""
    bits = []
    if live_jobs:
        bits.append(f"{live_jobs} live {_plural(live_jobs, 'job', 'jobs')}")
    if live_transcode:
        bits.append(
            f"{live_transcode} live {_plural(live_transcode, 'transcode', 'transcodes')}"
        )
    if live_capture:
        bits.append(f"{live_capture} live {_plural(live_capture, 'capture', 'captures')}")
    return ", ".join(bits)


def main() -> None:
    from rich.traceback import install

    install(show_locals=False)
    app()


if __name__ == "__main__":
    main()
