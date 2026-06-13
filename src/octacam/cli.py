import logging
import os
import resource
import sys
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
    mjpg = "mjpg"
    h264 = "h264"


class Trigger(StrEnum):
    software = "software"
    hardware = "hardware"


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


app = typer.Typer(
    add_completion=False,
    no_args_is_help=False,
    rich_markup_mode=None,
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

    Run `octacam serve <config_dir>` for the web GUI, or see the commands below.
    """
    _setup_logging(log_level)
    _raise_fd_limit()
    if ctx.invoked_subcommand is None:
        typer.echo(ctx.get_help())
        raise typer.Exit()


@app.command()
def serve(
    config_dir: Annotated[
        Path,
        typer.Argument(exists=True, file_okay=False, dir_okay=True),
    ] = Path("."),
    host: Annotated[
        str,
        typer.Option(
            help="Bind address. Keep the loopback default and reach the GUI "
            "remotely with: ssh -L 8000:127.0.0.1:8000 <rig-hostname>"
        ),
    ] = "127.0.0.1",
    port: Annotated[int, typer.Option(help="Port to bind.")] = 8000,
    enabled_plugins: EnabledPlugins = None,
    no_plugins: NoPlugins = False,
) -> None:
    """Serve the octacam web GUI for the cameras in CONFIG_DIR."""
    import uvicorn

    from octacam.camera import CameraSystem
    from octacam.config import load_config_dir
    from octacam.controller import (
        RecordingController,
        RecordingSettings,
        normalize_save_dir,
    )
    from octacam.plugins import build_plugins
    from octacam.web.app import create_app
    from octacam.writer import default_codec

    config_dir = config_dir.resolve()
    log.info("Using config directory: %s", config_dir)
    config = load_config_dir(config_dir)

    system = CameraSystem([c.serial_number for c in config.cameras])
    if len(system) == 0:
        log.warning("No cameras opened. Exiting.")
        sys.exit(1)
    log.info("Opened %d camera(s)", len(system))

    names = {c.serial_number: c.name for c in config.cameras if c.name}
    for camera in system:
        camera.name = names.get(camera.serial_number, camera.name)
    system.load_config(config_dir)

    plugins = build_plugins(config, _resolve_enabled(enabled_plugins, no_plugins))
    plugins.setup_all()

    gui = config.gui
    unit_seconds = (1.0, 60.0, 3600.0)[
        gui.duration_unit_default_index
        if 0 <= gui.duration_unit_default_index <= 2
        else 0
    ]
    settings = RecordingSettings(
        fps=gui.fps_default,
        duration_s=gui.duration_default * unit_seconds,
        save_dir=normalize_save_dir(gui.save_directory_default),
        trigger_source=(
            "external" if gui.trigger_source_default_index == 1 else "software"
        ),
        codec=default_codec(gui),
    )
    controller = RecordingController(system, settings, plugins)
    controller.start_preview()
    app = create_app(controller, config, plugins, config_dir=str(config_dir))
    log.info(
        "octacam web GUI on http://%s:%d/ (remote: ssh -L %d:127.0.0.1:%d <rig-hostname>)",
        host,
        port,
        port,
        port,
    )
    try:
        uvicorn.run(app, host=host, port=port, log_level="warning")
    finally:
        # Runs on Ctrl+C, on the /api/shutdown self-signal, and on errors.
        # Cleanup can take a moment (finalizing recordings, draining ffmpeg,
        # closing cameras), so bracket it with messages.
        log.info("Shutting down — finalizing recordings and releasing cameras…")
        controller.close()
        plugins.teardown_all()
        log.info("octacam stopped.")


@app.command("list-cameras")
def list_cameras() -> None:
    """List detected cameras (set PYLON_CAMEMU=N for emulated ones)."""
    from pypylon import pylon

    devices = pylon.TlFactory.GetInstance().EnumerateDevices()
    if not devices:
        typer.echo("No cameras detected.")
        return
    for device in devices:
        typer.echo(f"{device.GetModelName()}\t{device.GetSerialNumber()}")


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
            "`octacam transcode`; mjpg/h264: the legacy OpenCV writers."
        ),
    ] = Codec.x264,
    crf: Annotated[
        int, typer.Option(help="x264 quality (lower = better; 0 = lossless).")
    ] = 16,
    preset: Annotated[
        str,
        typer.Option(
            help="x264 speed preset. ultrafast is the only one validated at "
            "8 cameras x 150 fps; slower presets compress better."
        ),
    ] = "ultrafast",
    trigger: Annotated[
        Trigger,
        typer.Option(
            help="software: trigger from a timer thread at --fps; hardware: use "
            "the trigger source configured in the .pfs files."
        ),
    ] = Trigger.software,
    enabled_plugins: EnabledPlugins = None,
    no_plugins: NoPlugins = False,
) -> None:
    """Record videos headlessly from the cameras in CONFIG_DIR."""
    from octacam.camera import CameraSystem
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

    system = CameraSystem([c.serial_number for c in config.cameras])
    if len(system) == 0:
        log.warning("No cameras opened. Exiting.")
        sys.exit(1)
    log.info("Opened %d camera(s)", len(system))

    names = {c.serial_number: c.name for c in config.cameras if c.name}
    for camera in system:
        camera.name = names.get(camera.serial_number, camera.name)

    system.load_config(config_dir)

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
    )
    controller = RecordingController(system, settings, plugins, auto_preview=False)
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


@app.command()
def transcode(
    paths: Annotated[
        list[Path],
        typer.Argument(
            exists=True,
            help="PATHS are .raw files or directories to scan for them.",
        ),
    ],
    crf: Annotated[int, typer.Option()] = 16,
    preset: Annotated[
        str,
        typer.Option(
            help="x264 speed preset; offline transcoding can afford a slow one."
        ),
    ] = "veryslow",
) -> None:
    """Transcode .raw recordings (with .json sidecars) to x264 MKV.

    PATHS are .raw files or directories to scan for them.
    """
    from octacam.writer import transcode_raw

    raw_files: list[Path] = []
    for path in paths:
        raw_files.extend(sorted(path.glob("*.raw")) if path.is_dir() else [path])
    if not raw_files:
        log.warning("No .raw files found in: %s", ", ".join(map(str, paths)))
        return
    failures = 0
    for raw_file in dict.fromkeys(raw_files):
        try:
            typer.echo(transcode_raw(raw_file, crf=crf, preset=preset))
        except Exception as e:  # one bad file must not abort the batch
            failures += 1
            log.error("Failed to transcode %s: %s", raw_file, e)
    if failures:
        sys.exit(f"{failures} file(s) failed to transcode")


def main() -> None:
    from rich.traceback import install

    install(show_locals=False)
    app()


if __name__ == "__main__":
    main()
