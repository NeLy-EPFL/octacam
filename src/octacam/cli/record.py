"""`octacam record`: one headless take."""

import contextlib
import logging
import sys
import time
from pathlib import Path
from typing import Annotated

import typer

from octacam.cli._common import (
    EnabledPlugins,
    NoPlugins,
    open_rig,
    resolve_config_arg,
    resolve_enabled,
    stderr_console,
    warn_if_transcoding,
)
from octacam.cli.flash import preflight_firmware

log = logging.getLogger("octacam")


def _confirm_gate(question: str, *, force: bool, forced: str, headless: str) -> None:
    """Ask *question* on a terminal and exit 1 unless it is confirmed; under
    --force, or with no terminal to ask on, warn (*forced* / *headless*) and go on."""
    if force:
        log.warning(forced)
    elif sys.stdin.isatty() and sys.stderr.isatty():
        if not typer.confirm(question):
            raise typer.Exit(1)
    else:
        log.warning(headless)


def _drive_record_progress(controller, duration_s: float) -> None:
    """Show progress until the recording is no longer active: a bar on a TTY,
    else a log heartbeat every 2 s. The caller then joins the monitor."""
    poll = 0.1
    if not sys.stderr.isatty():
        next_beat = 0.0
        while controller.recording_active:
            now = time.monotonic()
            if now >= next_beat:
                snap = controller.snapshot()
                frames = sum(c["frames"] for c in snap["cameras"])
                log.info("Recording (%s): %d frames captured", snap["state"], frames)
                next_beat = now + 2.0
            time.sleep(poll)
        return

    from rich.progress import BarColumn, Progress, TextColumn, TimeRemainingColumn

    with Progress(
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TimeRemainingColumn(),
        console=stderr_console(),
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
            time.sleep(poll)
        progress.update(task, completed=duration_s)


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
            help="Before recording, reflash without asking a serial plugin's board "
            "that runs an old build of its firmware (a blank or foreign board is "
            "only warned about).",
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
    from octacam.config import RecordingSettings, load_config_dir
    from octacam.controller import RecordingController
    from octacam.plugins import build_plugins

    config_dir = resolve_config_arg(config_dir)
    config = load_config_dir(config_dir)

    settings = RecordingSettings.from_config(config, fps=fps)
    if duration is not None:
        settings.duration_s = duration
    if output is not None:
        # Bypasses the template; the summary's relative_directory falls back to
        # the folder name.
        settings = settings.with_save_dir(str(output))

    warn_if_transcoding()

    system = open_rig(config, config_dir)
    # Until the controller owns the cameras, every exit closes them.
    try:
        log.info(
            "Opened %d of %d configured camera(s)",
            len(system),
            len(system.requested_serial_numbers) or len(system),
        )
        if system.incomplete:
            # (CameraSystem logged INCOMPLETE RIG.) Nothing downstream can tell a
            # short take from a smaller rig, so recording one is an explicit
            # choice, like overwriting a take.
            _confirm_gate(
                f"Only {len(system)} of {len(system.requested_serial_numbers)} "
                "configured cameras opened. Record anyway?",
                force=force,
                forced="Recording with an incomplete rig (--force).",
                headless="Recording with an incomplete rig (pass --force to silence this).",
            )
        if Path(settings.save_dir).exists():
            _confirm_gate(
                f"Save directory already exists and will be overwritten:\n"
                f"  {settings.save_dir}\nContinue?",
                force=force,
                forced=f"Save directory exists; overwriting: {settings.save_dir}",
                headless=f"Save directory exists, data may be overwritten: "
                f"{settings.save_dir} (pass --force to silence this)",
            )

        plugins = build_plugins(config, resolve_enabled(enabled_plugins, no_plugins))
        plugins.setup_all()

        # Wrong firmware would record against the wrong protocol, or get no
        # triggers at all.
        preflight_firmware(plugins, assume_yes=yes)

        # A one-take session, so `octacam process --last session` finds it too.
        controller = RecordingController(
            system,
            settings,
            plugins,
            auto_preview=False,
            session_id=session_cache.new_session_id(),
            record_kind="record",
            config_dir=config_dir,
        )
    except BaseException:
        system.close()
        raise
    # The capture marker pauses `octacam process` until the cameras are closed. It
    # is entered inside the try, so a failure there still tears down, and released
    # by the with, after close().
    with contextlib.ExitStack() as capture:
        try:
            capture.enter_context(session_cache.mark_capture_active("recording"))
            log.info(
                "Recording %d camera(s) at %g fps for %g s to %s",
                len(system),
                settings.fps,
                settings.duration_s,
                settings.save_dir,
            )
            # No GUI posts plugin_params here: without a start slice triggerbox
            # never arms and its cameras wait forever for a trigger.
            plugin_params = plugins.default_start_params(settings.fps, settings.duration_s)
            result = controller.start_recording(
                confirm_overwrite=True, plugin_params=plugin_params or None
            )
            if not result.ok:
                sys.exit(f"Failed to start recording: {result.message}")
            _drive_record_progress(controller, settings.duration_s)
            controller.join()
        finally:
            # close() joins the monitor (which writes the summary and timestamps),
            # then closes the cameras; system.close() would race the monitor on a
            # Ctrl-C and lose them.
            controller.close()
            plugins.teardown_all()

    # stdout lists just the videos (scriptable); the info folder goes to stderr.
    from octacam.recording_format import recording_info_dir

    extension = settings.video_format().extension
    for camera in system:
        typer.echo(f"{Path(settings.save_dir) / camera.name}.{extension}")
    log.info(
        "Recording summary and config snapshot: %s",
        recording_info_dir(settings.save_dir),
    )

    # 0 frames (usually a trigger that never fired) fails the exit code.
    empty = [c.name for c in system if c.take is None or c.take.frames == 0]
    if empty:
        sys.exit(
            f"{len(empty)} camera(s) recorded 0 frames ({', '.join(empty)}); "
            "the recording is incomplete (no trigger, or no frames delivered)."
        )
