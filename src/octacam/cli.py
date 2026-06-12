import logging
import os
import resource
import sys
import time
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

    Without a subcommand, launches the GUI for the current directory.
    """
    logging.basicConfig(
        format="[%(levelname)s] %(message)s",
        level=getattr(logging, log_level.upper()),
    )
    _raise_fd_limit()
    if ctx.invoked_subcommand is None:
        ctx.invoke(gui)


@main.command()
@click.argument(
    "config_dir",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    default=".",
)
@click.option(
    "--serial-port",
    default="/dev/ttyACM0",
    show_default=True,
    help="Serial device of the Arduino stepper controller.",
)
def gui(config_dir: Path, serial_port: str = "/dev/ttyACM0") -> None:
    """Launch the octacam GUI for the cameras in CONFIG_DIR."""
    from PySide6.QtWidgets import QApplication

    from octacam.camera import CameraSystem
    from octacam.config import load_config_dir
    from octacam.gui.main_window import MainWindow
    from octacam.serial_link import SerialLink

    serial_link = SerialLink()
    try:
        serial_link.open(serial_port, 115200)
    except Exception as e:
        log.warning("Failed to open serial port %s: %s", serial_port, e)

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
    system.start_preview()

    app = QApplication(sys.argv)
    window = MainWindow(system, config, serial_link)
    window.setWindowTitle("octacam")
    window.showNormal()
    try:
        sys.exit(app.exec())
    finally:
        system.close()
        serial_link.close()


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
def record(
    config_dir: Path,
    fps: float | None,
    duration: float | None,
    output: Path | None,
    codec: str,
    crf: int,
    preset: str,
    trigger: str,
) -> None:
    """Record videos headlessly from the cameras in CONFIG_DIR."""
    import dataclasses

    from octacam.camera import CameraSystem
    from octacam.config import load_config_dir
    from octacam.writer import FORMATS

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
    output.mkdir(parents=True, exist_ok=True)

    video_format = dataclasses.replace(FORMATS[codec], crf=crf, preset=preset)
    use_software_trigger = trigger == "software"

    try:
        system.enable_frame_trigger()
        system.set_trigger_source(use_software_trigger)
        system.set_software_trigger_frequency(fps)

        log.info(
            "Recording %d camera(s) at %g fps for %g s to %s",
            len(system),
            fps,
            duration,
            output,
        )
        system.start_record(output, fps, video_format)
        if use_software_trigger:
            system.start_software_trigger(duration)

        deadline = time.monotonic() + 3.0
        while not system.all_cameras_started and time.monotonic() < deadline:
            time.sleep(0.1)
        if system.all_cameras_started:
            log.info("All cameras started")
        else:
            log.warning("Not all cameras delivered a frame within 3 s")

        time.sleep(duration)
        time.sleep(0.5)  # grace period for in-flight frames
        system.stop_software_trigger()
        system.stop()  # grab loops exit; writers drain and close; CSVs written
    finally:
        system.close()

    for camera in system:
        click.echo(f"{output / camera.name}.{video_format.extension}")


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
    for raw_file in dict.fromkeys(raw_files):
        click.echo(transcode_raw(raw_file, crf=crf, preset=preset))


if __name__ == "__main__":
    main()
