"""`octacam gui`: serve the web GUI first, then open the rig behind it."""

import contextlib
import logging
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
import webbrowser
from pathlib import Path
from typing import Annotated

import typer

from octacam.cli._common import (
    EnabledPlugins,
    NoPlugins,
    Sets,
    Verbose,
    browser_skip_reason,
    camera_open_error,
    command,
    pick_port,
    resolve_config_arg,
    resolve_enabled,
    warn_if_transcoding,
    with_sets,
)

log = logging.getLogger("octacam")

#: The GUI's port when `--port` does not say; taken, the next free one.
DEFAULT_PORT = 8765


def _launch_browser(url: str) -> bool:
    """Open url in the default browser; return True if a launcher started.

    $BROWSER wins; otherwise xdg-open/open, which honor the desktop's default,
    whereas webbrowser's hunt on Linux can "succeed" with a browser that never
    shows a window. Everything else falls back to webbrowser.
    """

    def _via_webbrowser() -> bool:
        try:
            return webbrowser.open(url)
        except webbrowser.Error as e:
            log.debug("webbrowser.open failed: %s", e)
            return False

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
    """Open the browser once the server accepts connections (on a daemon thread
    beside uvicorn.run; opening earlier shows an error page).
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
            log.warning(
                "octacam GUI never became reachable \N{EM DASH} open %s manually.", url
            )
            return
        if not _launch_browser(url):
            log.warning(
                "Couldn't open a browser automatically \N{EM DASH} open %s manually.",
                url,
            )
    except Exception:  # a helper thread must never die silently
        log.warning(
            "Failed to open a browser \N{EM DASH} open %s manually.", url, exc_info=True
        )


def _print_transcode_hints(session_id: str) -> None:
    """Print the `process` commands for this session's recordings, if any."""
    from octacam import session_cache

    try:
        folders = session_cache.session_folders(session_id)
    except Exception:
        log.debug("Could not read the recording cache for process hints", exc_info=True)
        return
    if not folders:
        return
    log.info(
        "Recorded %d folder(s) this session. Transcode and transfer them with:\n"
        "  last session:  octacam process --last session\n"
        "  all sessions:  octacam process --all",
        len(folders),
    )


def _finish_gui_session(session_id: str, config_dir: Path, process_after: bool) -> None:
    """On GUI shutdown, start a detached `process` job for this session's
    recordings when asked, else print the hints. Never raises.
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


@command
def gui(
    config_dir: Annotated[
        Path,
        typer.Argument(
            exists=True,
            file_okay=False,
            dir_okay=True,
            help="The rig's config directory (`octacam_config.toml` and its camera "
            "files), or a recording folder, whose config snapshot it uses.",
        ),
    ] = Path("."),
    host: Annotated[
        str,
        typer.Option(
            "--host",
            help="Interface to bind. Keep the loopback default and reach the GUI "
            "remotely with `ssh -L 8765:127.0.0.1:8765 <rig-hostname>`.",
        ),
    ] = "127.0.0.1",
    port: Annotated[
        int | None,
        typer.Option(
            "--port",
            help=f"Port to serve on. Default: {DEFAULT_PORT}, or the next free port.",
            show_default=False,
        ),
    ] = None,
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
    sets: Sets = None,
    verbose: Verbose = False,
) -> None:
    """Launch the octacam web GUI for the cameras in `config_dir`."""
    import uvicorn

    from octacam import locks, session_cache
    from octacam.cameras import BackendError, BackendUnavailable, CameraSystem
    from octacam.config import RecordingSettings, load_config_dir
    from octacam.controller import RecordingController
    from octacam.plugins import build_plugins
    from octacam.web.app import create_app

    # A wildcard bind address is not connectable; the browser uses loopback.
    browser_host = "127.0.0.1" if host in ("0.0.0.0", "::") else host

    config_dir = resolve_config_arg(config_dir).resolve()
    log.info("Using config directory: %s", config_dir)

    # Released in the teardown below, before the capture marker.
    rig_lock = contextlib.ExitStack()
    try:
        rig_lock.enter_context(locks.instance_lock(config_dir))
    except locks.RigInUse:
        sys.exit(
            f"Another octacam instance is already running for this config "
            f"({config_dir}). Open its GUI in a browser, or stop it first."
        )

    port = pick_port(host, port, DEFAULT_PORT)

    config = with_sets(load_config_dir(config_dir), sets)
    warn_if_transcoding()

    plugins = build_plugins(config, resolve_enabled(enabled_plugins, no_plugins))

    settings = RecordingSettings.from_config(config)
    # Tags every recording of this run, for `octacam process --last session`.
    session_id = session_cache.new_session_id()
    # Serve first: the controller starts on a hardware-free placeholder
    # (ready=False) so the page renders at once; _initialize_rig opens the
    # hardware and swaps it in with attach_system.
    system = CameraSystem.pending(config.backend)
    controller = RecordingController(
        system,
        settings,
        plugins,
        session_id=session_id,
        config_dir=config_dir,
        ready=False,
    )
    capture_stack = contextlib.ExitStack()
    # Set on shutdown: a still-running init releases the cameras instead of arming.
    stopping = threading.Event()

    def _initialize_rig(app_state) -> None:
        """Open the cameras and arm the plugins in parallel, start preview, publish.

        A failure goes to the GUI (fail_init) and the log; the server keeps
        running.
        """
        from concurrent.futures import ThreadPoolExecutor

        def _publish() -> None:
            # On failure too: the plugins armed in parallel may be ready, and
            # their tabs must say so without a reload.
            app_state.broadcast_system()
            controller.notify_state()

        try:
            # Cameras and serial plugins are independent hardware. setup_all logs
            # its own errors; the pool's exit waits for it if the open raises.
            with ThreadPoolExecutor(max_workers=2, thread_name_prefix="rig-init") as ex:
                cam_future = ex.submit(CameraSystem.for_config, config, config_dir)
                ex.submit(plugins.setup_all)
                opened_system = cam_future.result()
        except Exception as e:  # never let the init thread die silently
            if isinstance(e, (BackendError, BackendUnavailable)):
                message = camera_open_error(e)
            else:
                log.exception("Camera initialization failed")
                message = f"Camera initialization failed: {e}"
            controller.fail_init(message)
            _publish()
            return

        if stopping.is_set():  # the finally joins us before closing anything
            with contextlib.suppress(Exception):
                opened_system.close()
            return

        controller.attach_system(opened_system)
        # The capture-active marker parks every `octacam process` on this machine,
        # with no timeout, so it is taken only once the GUI owns cameras. The
        # finally joins this thread before closing capture_stack.
        capture_stack.enter_context(session_cache.mark_capture_active("gui session"))
        log.info("Opened %d camera(s)", len(opened_system))
        try:
            controller.start_preview()
        except Exception:
            log.exception("Failed to start live preview")
        _publish()

    # Assigned inside the try, so a create_app failure still runs the teardown.
    app = None
    init_thread: threading.Thread | None = None
    try:
        app = create_app(controller, config)
        init_thread = threading.Thread(
            target=_initialize_rig,
            args=(app.state.app_state,),
            name="octacam-init",
            daemon=True,
        )
        init_thread.start()
        log.info(
            "octacam web GUI on http://%s:%d/ (remote: ssh -L %d:127.0.0.1:%d "
            "<rig-hostname>)",
            host,
            port,
            port,
            port,
        )
        browser_url = f"http://{browser_host}:{port}/"
        skip = browser_skip_reason(no_browser)
        if skip:
            log.info("Not opening a browser automatically: %s.", skip)
        else:
            log.info(
                "Opening the web GUI in your default browser\N{HORIZONTAL ELLIPSIS}"
            )
            threading.Thread(
                target=_open_browser_when_ready,
                args=(browser_url, host, port),
                daemon=True,
            ).start()
        # ws_ping_timeout: the keepalive socket also carries the preview, and over a
        # slow `ssh -L` tunnel congestion outlasts the default 20 s pong wait and
        # kills a live GUI (1011); the default ping interval still reaps dead
        # clients. websockets-sansio: "auto" picks uvicorn's legacy websockets
        # implementation, which warns of deprecation on every connection.
        uvicorn.run(
            app,
            host=host,
            port=port,
            log_level="warning",
            ws="websockets-sansio",
            ws_ping_timeout=60.0,
        )
    finally:  # Ctrl+C, /api/shutdown and errors all land here
        log.info(
            "Shutting down \N{EM DASH} finalizing recordings and releasing "
            "cameras\N{HORIZONTAL ELLIPSIS}"
        )
        # The join orders the init's attach/start_preview before close(); bounded
        # so a wedged SDK open cannot hang shutdown.
        stopping.set()
        if init_thread is not None and init_thread.is_alive():
            init_thread.join(timeout=30)
        controller.close()
        plugins.teardown_all()
        rig_lock.close()  # a relaunch need not wait for this process to exit
        # Drop the marker before a processing job starts, or it parks at once.
        capture_stack.close()
        process_after = app is not None and app.state.app_state.process_after
        _finish_gui_session(session_id, config_dir, process_after)
        log.info("octacam stopped.")
