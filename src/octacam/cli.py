import logging
import os
import resource
import sys
from pathlib import Path

import click

import octacam

log = logging.getLogger("octacam")


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



@click.group(invoke_without_command=True)
@click.version_option(octacam.__version__)
@click.option(
    "--log-level",
    "-l",
    type=click.Choice(["debug", "info", "warning", "error"]),
    default="info",
    show_default=True,
)
@click.pass_context
def main(ctx: click.Context, log_level: str) -> None:
    """octacam: preview, record, and save video streams from multiple Basler cameras.

    Run `octacam serve <config_dir>` for the web GUI, or see the commands below.
    """
    logging.basicConfig(
        format="[%(levelname)s] %(message)s",
        level=getattr(logging, log_level.upper()),
    )
    _raise_fd_limit()
    if ctx.invoked_subcommand is None:
        click.echo(ctx.get_help())
        ctx.exit()


# Plugins are opt-in; the default launch loads none. Enable them per-rig in the
# config's `plugins:` section, or per-launch with these flags.
def _plugin_options(func):
    func = click.option(
        "--plugin",
        "enabled_plugins",
        multiple=True,
        help="Enable a plugin (repeatable); adds to the config's `plugins:` "
        "(e.g. --plugin arduino). Requires its extra: pip install octacam[arduino].",
    )(func)
    func = click.option(
        "--no-plugins",
        is_flag=True,
        help="Disable all plugins for this launch, ignoring the config.",
    )(func)
    return func


def _resolve_enabled(enabled_plugins, no_plugins):
    """Map the CLI flags to build_plugins' `enabled` argument.

    None = no override (use the config), [] = --no-plugins, or the list of
    --plugin names to add to the config selection.
    """
    if no_plugins:
        return []
    return list(enabled_plugins) or None


@main.command()
@click.argument(
    "config_dir",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    default=".",
)
@click.option(
    "--host",
    default="127.0.0.1",
    show_default=True,
    help="Bind address. Keep the loopback default and reach the GUI "
    "remotely with: ssh -L 8000:127.0.0.1:8000 <rig-hostname>",
)
@click.option("--port", default=8000, show_default=True)
@_plugin_options
def serve(
    config_dir: Path,
    host: str,
    port: int,
    enabled_plugins: tuple[str, ...],
    no_plugins: bool,
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


@main.command("list-cameras")
def list_cameras() -> None:
    """List detected cameras (set PYLON_CAMEMU=N for emulated ones)."""
    from pypylon import pylon

    devices = pylon.TlFactory.GetInstance().EnumerateDevices()
    if not devices:
        click.echo("No cameras detected.")
        return
    for device in devices:
        click.echo(f"{device.GetModelName()}\t{device.GetSerialNumber()}")


@main.command()
@click.argument(
    "config_dir",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    default=".",
)
@click.option("--fps", "-f", type=float, help="Frame rate [default: from config].")
@click.option(
    "--duration",
    "-d",
    type=float,
    help="Recording duration in seconds [default: from config].",
)
@click.option(
    "--output",
    "-o",
    type=click.Path(path_type=Path),
    help="Save directory [default: from config].",
)
@click.option(
    "--codec",
    type=click.Choice(["x264", "raw", "mjpg", "h264"]),
    default="x264",
    show_default=True,
    help="x264: ffmpeg H.264 mkv (gray 4:0:0); raw: Mono8 dump for "
    "`octacam transcode`; mjpg/h264: the legacy OpenCV writers.",
)
@click.option(
    "--crf",
    type=int,
    default=16,
    show_default=True,
    help="x264 quality (lower = better; 0 = lossless).",
)
@click.option(
    "--preset",
    default="ultrafast",
    show_default=True,
    help="x264 speed preset. ultrafast is the only one validated at "
    "8 cameras x 150 fps; slower presets compress better.",
)
@click.option(
    "--trigger",
    type=click.Choice(["software", "hardware"]),
    default="software",
    show_default=True,
    help="software: trigger from a timer thread at --fps; hardware: use the "
    "trigger source configured in the .pfs files.",
)
@_plugin_options
def record(
    config_dir: Path,
    fps: float | None,
    duration: float | None,
    output: Path | None,
    codec: str,
    crf: int,
    preset: str,
    trigger: str,
    enabled_plugins: tuple[str, ...],
    no_plugins: bool,
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
        trigger_source="software" if trigger == "software" else "external",
        codec=codec,
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
        click.echo(f"{output / camera.name}.{extension}")


@main.command()
@click.argument(
    "paths",
    nargs=-1,
    required=True,
    type=click.Path(exists=True, path_type=Path),
)
@click.option("--crf", type=int, default=16, show_default=True)
@click.option(
    "--preset",
    default="veryslow",
    show_default=True,
    help="x264 speed preset; offline transcoding can afford a slow one.",
)
def transcode(paths: tuple[Path, ...], crf: int, preset: str) -> None:
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
            click.echo(transcode_raw(raw_file, crf=crf, preset=preset))
        except Exception as e:  # one bad file must not abort the batch
            failures += 1
            log.error("Failed to transcode %s: %s", raw_file, e)
    if failures:
        sys.exit(f"{failures} file(s) failed to transcode")


if __name__ == "__main__":
    main()
