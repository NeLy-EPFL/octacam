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
from enum import StrEnum
from pathlib import Path
from typing import Annotated

import typer

import octacam

log = logging.getLogger("octacam")


class LogLevel(StrEnum):
    debug = "debug"
    info = "info"
    warning = "warning"
    error = "error"


class Codec(StrEnum):
    x264 = "x264"
    raw = "raw"


class Trigger(StrEnum):
    software = "software"
    hardware = "hardware"


class RecordForm(StrEnum):
    sensor = "sensor"
    display = "display"


def _setup_logging(level: LogLevel) -> None:
    """Route the "octacam" logger through rich (colored level, pretty tracebacks).

    Logs go to stderr so stdout stays clean for the machine-readable output of
    `list-cameras`/`record`/`transcode`."""
    from rich.console import Console
    from rich.logging import RichHandler

    handler = RichHandler(
        console=Console(stderr=True),
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
    rich_markup_mode=None,
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
        "(e.g. --plugin arduino). Requires its extra: pip install octacam[arduino].",
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


def _print_transcode_hints(session_id: str, config_dir: Path) -> None:
    """On GUI shutdown, print the transcode commands for what was just recorded.

    Stays silent when the session recorded nothing (it only previewed). The
    GUI's config dir carries the ``[transcode]`` encoding defaults, so it is
    appended as ``--config-dir`` whenever it is not the current directory, and
    the printed commands reproduce this rig's settings verbatim.
    """
    from octacam import session_cache

    try:
        folders = session_cache.session_folders(session_id)
    except Exception:
        log.debug(
            "Could not read the recording cache for transcode hints", exc_info=True
        )
        return
    if not folders:
        return
    try:
        same_dir = config_dir.resolve() == Path.cwd().resolve()
    except OSError:
        same_dir = False
    suffix = "" if same_dir else f" --config-dir {shlex.quote(str(config_dir))}"
    # The "this session" line names the exact session id, not the bare --session
    # selector: --session resolves to the *latest* session, so a later recording
    # (this rig or another) would otherwise make the printed command target the
    # wrong batch. --today / --last stay as convenient latest-selectors.
    log.info(
        "Recorded %d folder(s) this session. Transcode them with:\n"
        "  this session:   octacam transcode --session-id %s%s\n"
        "  today:          octacam transcode --today%s\n"
        "  last recording: octacam transcode --last%s",
        len(folders),
        shlex.quote(session_id),
        suffix,
        suffix,
        suffix,
    )


def _warn_if_transcoding() -> None:
    """Warn when an `octacam transcode` is running elsewhere on this machine.

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
            "%d octacam transcode%s running on this machine — transcoding is "
            "CPU-heavy and may slow capture/encoding (risking dropped frames). "
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
    from octacam.controller import (
        RecordingController,
        RecordingSettings,
        normalize_save_dir,
    )
    from octacam.plugins import build_plugins
    from octacam.web.app import create_app
    from octacam.writer import default_codec

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

    gui_cfg = config.gui
    unit_seconds = (1.0, 60.0, 3600.0)[
        gui_cfg.duration_unit_default_index
        if 0 <= gui_cfg.duration_unit_default_index <= 2
        else 0
    ]
    settings = RecordingSettings(
        fps=gui_cfg.fps_default,
        duration_s=gui_cfg.duration_default * unit_seconds,
        save_dir=normalize_save_dir(gui_cfg.save_directory_default),
        trigger_source=(
            "external" if gui_cfg.trigger_source_default_index == 1 else "software"
        ),
        codec=default_codec(gui_cfg),
        crf=gui_cfg.crf_default,
        preset=gui_cfg.preset_default,
        pix_fmt=gui_cfg.pix_fmt_default,
        x264_params=gui_cfg.x264_params_default,
        record_form=gui_cfg.record_form_default,
        save_frame_timestamps=gui_cfg.save_frame_timestamps_default,
    )
    # One session id for this GUI run; every recording made before shutdown is
    # tagged with it in the session cache so `octacam transcode --session` can
    # find the whole batch later (and we print the commands on the way out).
    session_id = session_cache.new_session_id()
    controller = RecordingController(system, settings, plugins, session_id=session_id)
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
        uvicorn.run(app, host=host, port=port, log_level="warning")
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
        # commands for it (and for today / the last recording).
        _print_transcode_hints(session_id, config_dir)
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
    """List the bundled, opt-in plugins and whether each can load.

    Output is `name<TAB>status<TAB>summary`. `available` means the plugin's
    optional dependency is installed; `unavailable` lines carry the install
    hint. Enable one with `--plugin NAME` on `gui`/`record`, or a `[[plugins]]`
    entry in the rig config.
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
        typer.Option("--fps", "-f", help="Frame rate [default: from config]."),
    ] = None,
    duration: Annotated[
        float | None,
        typer.Option(
            "--duration",
            "-d",
            help="Recording duration in seconds [default: from config].",
        ),
    ] = None,
    output: Annotated[
        Path | None,
        typer.Option("--output", "-o", help="Save directory [default: from config]."),
    ] = None,
    codec: Annotated[
        Codec,
        typer.Option(
            help="x264: ffmpeg H.264 mkv (gray 4:0:0); raw: Mono8 dump for "
            "`octacam transcode`."
        ),
    ] = Codec.x264,
    crf: Annotated[
        int | None,
        typer.Option(
            help="x264 quality (lower = better; 0 = lossless) [default: from config]."
        ),
    ] = None,
    preset: Annotated[
        str | None,
        typer.Option(
            help="x264 speed preset. ultrafast is the only one validated at "
            "8 cameras x 150 fps; slower presets compress better [default: from config]."
        ),
    ] = None,
    x264_params: Annotated[
        str | None,
        typer.Option(
            "--x264-params",
            help='Extra libx264 options as ffmpeg -x264-params, e.g. "keyint=30'
            ':scenecut=0" [default: from config].',
        ),
    ] = None,
    trigger: Annotated[
        Trigger,
        typer.Option(
            help="software: trigger from a timer thread at --fps; hardware: use "
            "the trigger source configured in the .pfs files."
        ),
    ] = Trigger.software,
    record_form: Annotated[
        RecordForm | None,
        typer.Option(
            "--record-form",
            help="display: bake each camera's rotation/flips into the video; "
            "sensor: save the raw, untransformed image [default: from config].",
        ),
    ] = None,
    save_frame_timestamps: Annotated[
        bool | None,
        typer.Option(
            "--save-frame-timestamps/--no-save-frame-timestamps",
            help="Also write a per-frame timestamp CSV per camera, for "
            "debugging [default: from config].",
        ),
    ] = None,
    enabled_plugins: EnabledPlugins = None,
    no_plugins: NoPlugins = False,
) -> None:
    """Record videos headlessly from the cameras in CONFIG_DIR."""
    from octacam import session_cache
    from octacam.cameras import BackendUnavailable, CameraSystem
    from octacam.config import load_config_dir
    from octacam.controller import RecordingController, RecordingSettings
    from octacam.plugins import build_plugins

    config = load_config_dir(config_dir)
    if fps is None:
        fps = config.gui.fps_default
    if duration is None:
        duration = config.gui.duration_default
    if output is None:
        output = Path(os.path.expanduser(config.gui.save_directory_default))
    if crf is None:
        crf = config.gui.crf_default
    if preset is None:
        preset = config.gui.preset_default
    if x264_params is None:
        x264_params = config.gui.x264_params_default
    record_form_value = (
        record_form.value if record_form is not None else config.gui.record_form_default
    )
    if save_frame_timestamps is None:
        save_frame_timestamps = config.gui.save_frame_timestamps_default

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

    if output.exists():
        log.warning("Directory already exists, data might be overwritten: %s", output)

    plugins = build_plugins(config, _resolve_enabled(enabled_plugins, no_plugins))
    plugins.setup_all()

    settings = RecordingSettings(
        fps=fps,
        duration_s=duration,
        save_dir=str(output),
        trigger_source="software" if trigger == Trigger.software else "external",
        codec=codec.value,
        crf=crf,
        preset=preset,
        pix_fmt=config.gui.pix_fmt_default,
        x264_params=x264_params,
        record_form=record_form_value,
        save_frame_timestamps=save_frame_timestamps,
    )
    # Tag this headless run in the session cache so `octacam transcode --last`
    # and `--today` pick it up too (a one-off, single-folder "session").
    controller = RecordingController(
        system,
        settings,
        plugins,
        auto_preview=False,
        session_id=session_cache.new_session_id(),
        record_kind="record",
    )
    try:
        log.info(
            "Recording %d camera(s) at %g fps for %g s to %s",
            len(system),
            fps,
            duration,
            output,
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
        typer.echo(f"{output / camera.name}.{extension}")


def _read_summary(path: Path) -> dict | None:
    """Load a recording_summary.json, or None (with a warning) if unreadable."""
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError) as e:
        log.warning("Could not read %s: %s", path, e)
        return None


def _job_vf(entry: dict, as_displayed: bool) -> str:
    """The ffmpeg -vf chain for one summary camera entry under the chosen mode."""
    from octacam.transform import DisplayTransform, display_vf_filter

    if not as_displayed or entry.get("transform_applied"):
        # as-saved, or the transform is already baked into the file.
        return ""
    return display_vf_filter(DisplayTransform.from_dict(entry.get("transform") or {}))


def _transcode_jobs(
    paths: list[Path], recursive: bool, as_displayed: bool, out_format: str
) -> list[tuple[Path, str]]:
    """Resolve folders/files to a deduped list of (input_path, vf) jobs.

    A folder with a recording_summary.json is driven by it; one without has its
    loose .mkv/.raw transcoded with no transform (and a warning). A file is
    matched against a summary in its own folder, else transcoded plainly."""
    from octacam.transform import RECORDING_SUMMARY_FILENAME

    jobs: dict[Path, tuple[Path, str]] = {}

    def add(input_path: Path, vf: str) -> None:
        jobs.setdefault(input_path.resolve(), (input_path, vf))

    def handle_dir(directory: Path) -> None:
        summary_path = directory / RECORDING_SUMMARY_FILENAME
        if summary_path.exists():
            data = _read_summary(summary_path)
            if data is not None:
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
                        add(video, _job_vf(entry, as_displayed))
                return
        loose = sorted(p for p in directory.iterdir() if p.suffix in (".mkv", ".raw"))
        if loose:
            log.warning(
                "No %s in %s; transcoding %d file(s) with defaults and no transform",
                RECORDING_SUMMARY_FILENAME,
                directory,
                len(loose),
            )
        for video in loose:
            add(video, "")

    for path in paths:
        if path.is_dir():
            handle_dir(path)
            if recursive:
                for sub in sorted(path.rglob("*")):
                    if sub.is_dir():
                        handle_dir(sub)
        else:
            entry = None
            summary_path = path.parent / RECORDING_SUMMARY_FILENAME
            if summary_path.exists():
                data = _read_summary(summary_path)
                if data is not None:
                    entry = next(
                        (
                            e
                            for e in data.get("cameras", [])
                            if e.get("file") == path.name
                        ),
                        None,
                    )
            if entry is not None:
                add(path, _job_vf(entry, as_displayed))
            else:
                log.warning(
                    "No %s entry for %s; transcoding with defaults and no transform",
                    RECORDING_SUMMARY_FILENAME,
                    path,
                )
                add(path, "")

    return list(jobs.values())


def _resolve_transcode_paths(
    paths: list[Path],
    last: bool,
    session: bool,
    today: bool,
    session_id: str | None,
    all_: bool,
) -> list[Path]:
    """Resolve explicit PATHS or one cache selector to a list of folders.

    The selectors --last/--session/--today/--session-id/--all are mutually
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
            ("--today", today),
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
                "--last/--session/--today/--session-id/--all."
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
    elif today:
        selector = "--today"
        folders = session_cache.today_folders()
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


@app.command()
def transcode(
    paths: Annotated[
        list[Path] | None,
        typer.Argument(
            exists=True,
            help="Folders and/or video files (.mkv/.raw). A folder is driven by "
            "its recording_summary.json if present. Omit when using "
            "--last/--session/--today/--all.",
        ),
    ] = None,
    last: Annotated[
        bool,
        typer.Option(
            "--last",
            "--last-recording",
            help="Transcode the most recent recording folder (from the recording "
            "cache); no PATHS needed.",
        ),
    ] = False,
    session: Annotated[
        bool,
        typer.Option(
            "--session",
            "--last-session",
            help="Transcode every folder from the last GUI session; no PATHS needed.",
        ),
    ] = False,
    today: Annotated[
        bool,
        typer.Option(
            "--today",
            help="Transcode every folder recorded today; no PATHS needed.",
        ),
    ] = False,
    session_id: Annotated[
        str | None,
        typer.Option(
            "--session-id",
            help="Transcode every folder from one exact session id (the value the "
            "GUI prints on exit); unlike --session it is not hijacked by a later "
            "recording.",
        ),
    ] = None,
    all_: Annotated[
        bool,
        typer.Option(
            "--all",
            help="Transcode every recording folder still in the cache (all "
            "sessions, all days); no PATHS needed.",
        ),
    ] = False,
    recursive: Annotated[
        bool,
        typer.Option("-r", "--recursive", help="Recurse into the given folders."),
    ] = False,
    as_displayed: Annotated[
        bool,
        typer.Option(
            "--as-displayed/--as-saved",
            help="Apply each video's recorded display transform (skipped when "
            "already baked in). Default: reproduce as saved.",
        ),
    ] = False,
    config_dir: Annotated[
        Path,
        typer.Option(
            "--config-dir",
            help="Directory whose octacam_config.toml [transcode] table supplies "
            "the default encoding parameters.",
        ),
    ] = Path("."),
    fmt: Annotated[
        str | None,
        typer.Option("--format", help="Output container [default: from config]."),
    ] = None,
    crf: Annotated[
        int | None, typer.Option(help="x264 quality [default: from config].")
    ] = None,
    preset: Annotated[
        str | None, typer.Option(help="x264 speed preset [default: from config].")
    ] = None,
    pix_fmt: Annotated[
        str | None,
        typer.Option("--pix-fmt", help="Pixel format [default: from config]."),
    ] = None,
    x264_params: Annotated[
        str | None,
        typer.Option(
            "--x264-params",
            help='Extra libx264 -x264-params, e.g. "keyint=30:scenecut=0" '
            "[default: from config].",
        ),
    ] = None,
    remove_source: Annotated[
        bool,
        typer.Option(
            "--remove-source",
            help="Delete each source .mkv/.raw (and a .raw's .json sidecar) once "
            "it transcodes successfully. The recording_summary.json is kept.",
        ),
    ] = False,
) -> None:
    """Transcode recordings to compressed video.

    PATHS may mix folders and video files. A folder is transcoded per its
    recording_summary.json when present (honoring --as-saved/--as-displayed);
    otherwise its .mkv/.raw files are transcoded with default parameters and no
    transform. Defaults come from the [transcode] config table.

    Instead of PATHS, pass one of --last (the most recent recording folder),
    --session (every folder from the last GUI session), --today (every folder
    recorded today), --session-id (an exact session), or --all (every folder the
    cache still holds). These read the recording cache octacam keeps and silently
    skip any folder that has since been deleted.
    """
    from octacam import session_cache
    from octacam.config import load_config_dir
    from octacam.writer import transcode_file

    paths = _resolve_transcode_paths(
        list(paths or []), last, session, today, session_id, all_
    )

    tcfg = load_config_dir(config_dir).transcode
    out_format = (fmt or tcfg.format).lstrip(".")
    crf = crf if crf is not None else tcfg.crf
    preset = preset or tcfg.preset
    pix_fmt = pix_fmt or tcfg.pix_fmt
    x264_params = x264_params if x264_params is not None else tcfg.x264_params

    jobs = _transcode_jobs(paths, recursive, as_displayed, out_format)
    if not jobs:
        log.warning("No videos to transcode in: %s", ", ".join(map(str, paths)))
        return
    failures = 0
    # Advertise this run so a concurrent `gui`/`record` can warn about the CPU
    # contention; the marker is dropped (and cleaned up) when the run ends.
    with session_cache.mark_transcode_active(f"{len(jobs)} file(s)"):
        for input_path, vf in jobs:
            output = input_path.with_suffix("." + out_format)
            if output.resolve() == input_path.resolve():
                log.warning(
                    "Skipping %s: already in target format (%s)",
                    input_path,
                    out_format,
                )
                continue
            try:
                result = transcode_file(
                    input_path,
                    output,
                    crf=crf,
                    preset=preset,
                    pix_fmt=pix_fmt,
                    x264_params=x264_params,
                    vf=vf,
                )
                typer.echo(result)
            except Exception as e:  # one bad file must not abort the batch
                failures += 1
                log.error("Failed to transcode %s: %s", input_path, e)
                continue
            if remove_source:
                _remove_source_files(input_path)
    if failures:
        sys.exit(f"{failures} file(s) failed to transcode")


def _remove_source_files(input_path: Path) -> None:
    """Delete a transcoded source and, for .raw, its .json geometry sidecar.

    Never removes the session recording_summary.json. Deletion failures are
    logged but never fail the run (the transcode already succeeded)."""
    from octacam.transform import RECORDING_SUMMARY_FILENAME

    victims = [input_path]
    if input_path.suffix == ".raw":
        sidecar = input_path.with_suffix(".json")
        if sidecar.name != RECORDING_SUMMARY_FILENAME:
            victims.append(sidecar)
    for victim in victims:
        try:
            victim.unlink(missing_ok=True)
        except OSError as e:
            log.warning("Could not remove %s: %s", victim, e)


def main() -> None:
    from rich.traceback import install

    install(show_locals=False)
    app()


if __name__ == "__main__":
    main()
