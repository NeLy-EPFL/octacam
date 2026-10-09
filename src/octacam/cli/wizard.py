"""`octacam config`: the first-run wizard. It only enumerates cameras (opening
them just for --snapshot-params) and leaves placement and grid to `gui`.
"""

import logging
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, get_args

import typer

from octacam.cli._common import Verbose, command, enumerate_backend, serial_plugin

if TYPE_CHECKING:
    from octacam.config import RecordConfig

log = logging.getLogger("octacam")


def _resolve_backend(console, cli_backend: str | None) -> str:
    """The wizard's backend, without prompting: `auto` (every installed
    backend, so mixed vendors just work) unless `--backend` pins one.
    """
    from octacam.cameras.registry import BACKENDS, available_backends, is_auto

    if cli_backend is not None:
        if is_auto(cli_backend):
            return "auto"
        key = cli_backend.strip().lower()
        if key not in BACKENDS:
            raise typer.BadParameter(
                f"unknown backend {cli_backend!r}; expected 'auto' or one of "
                f"{', '.join(BACKENDS)}",
                param_hint="--backend",
            )
        return key
    available = available_backends()
    if available:
        console.print(
            f"Auto-detecting cameras from: [bold]{', '.join(available)}[/bold]"
        )
    else:
        console.print(
            "[yellow]No camera SDK detected here[/yellow] \N{EM DASH} you can still "
            "write a "
            "config now and detect cameras later on the rig."
        )
    return "auto"


def _detect_cameras(console, backend: str) -> list[tuple[str, str | None]]:
    """Print and return `[(serial, model|None)]` for *backend* (`auto`
    sweeps the cascade); [] if none or enumeration fails.
    """
    from octacam.cameras.registry import is_auto

    label = "" if is_auto(backend) else f"{backend} "
    try:
        cams = enumerate_backend(backend)
    except Exception as e:
        console.print(f"[yellow]Could not enumerate {label}cameras:[/yellow] {e}")
        return []
    if cams:
        console.print(f"Detected [bold]{len(cams)}[/bold] {label}camera(s):")
        for serial, model in cams:
            console.print(f"  \N{BULLET} {model + '  ' if model else ''}{serial}")
    else:
        console.print(f"[yellow]No {label}cameras detected.[/yellow]")
    return cams


def _prompt_cameras(console, detected: list[tuple[str, str | None]]) -> list[dict]:
    """`cameras` entries (serial plus an optional unique, safe `name`) for
    the detected or typed serials; [] means every camera detected at record time.
    """
    from rich.prompt import Confirm, Prompt

    from octacam.config import is_safe_segment

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
            "No cameras listed \N{EM DASH} the config will use every camera detected "
            "at "
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
            if not is_safe_segment(name):
                console.print(
                    "    [red]Invalid name[/red] \N{EM DASH} no '/', '\\', '.' or '..'."
                )
                continue
            if name in used:
                console.print(
                    f"    [red]{name!r} is already used[/red] \N{EM DASH} pick another."
                )
                continue
            used.add(name)
            entries.append({"serial_number": serial, "name": name})
            break
    return entries


def _prompt_visualization(console, cameras: list[dict]) -> list[dict]:
    """Offer one auto-arranged `grid.mp4` of the named cameras (two or more).

    Declining is the default: `octacam process` builds a grid only for a rig
    with a `[[visualization]]` entry, so this answer is the whole opt-in.
    """
    from rich.prompt import Confirm

    from octacam.grid import auto_layout

    names = [c["name"] for c in cameras if c.get("name")]
    if len(names) < 2:
        return []
    if not Confirm.ask(
        f"Also build a composite grid video of the {len(names)} named camera(s) "
        "when processing recordings?",
        default=False,
        console=console,
    ):
        return []
    return [{"name": "grid.mp4", "layout": auto_layout(names)}]


def _prompt_record(console) -> RecordConfig:
    """Prompt for the [record] section, defaulting every field to the schema default."""
    from rich.prompt import FloatPrompt, Prompt

    from octacam.config import (
        DurationUnit,
        PreviewTriggerSource,
        RecordConfig,
        SaveMethod,
        TriggerSource,
    )

    d = RecordConfig()
    fps = FloatPrompt.ask("Frame rate (fps)", default=d.fps, console=console)
    duration = FloatPrompt.ask(
        "Recording duration", default=d.duration, console=console
    )
    duration_unit = Prompt.ask(
        "Duration unit",
        choices=list(get_args(DurationUnit)),
        default=d.duration_unit,
        console=console,
    )
    trigger_source = Prompt.ask(
        "Trigger source",
        choices=list(get_args(TriggerSource)),
        default=d.trigger_source,
        console=console,
    )
    preview_trigger_source = Prompt.ask(
        "Preview trigger source (auto = mirror the recording trigger)",
        choices=list(get_args(PreviewTriggerSource)),
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
        choices=list(get_args(SaveMethod)),
        default=d.save_method,
        console=console,
    )
    # model_validate narrows the choice strings to their Literal fields.
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
    """Print and return the microcontroller-class serial ports (no legacy
    `/dev/ttyS*`).
    """
    from octacam import serial_ports as sp

    mcus = [p for p in sp.list_serial_ports() if p.likely_microcontroller]
    if mcus:
        console.print(f"Detected [bold]{len(mcus)}[/bold] serial device(s):")
        for p in mcus:
            sn = f"  sn={p.serial_number}" if p.serial_number else ""
            console.print(f"  \N{BULLET} {p.board_name}  {p.device}  [{p.vid_pid}]{sn}")
    else:
        console.print("[yellow]No Arduino-class serial ports detected.[/yellow]")
    return mcus


def _prompt_serial_plugin(console) -> list[dict]:
    """Optionally enable one serial plugin; its `plugins` entries ([] if not)."""
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
    cls = serial_plugin(name)
    default_device = (
        ports[0].device if ports else (cls and cls.default_device) or "auto"
    )
    console.print(
        "  Enter a device path, or [bold]auto[/bold] to pick the single board "
        "connected at launch."
    )
    device = Prompt.ask("  Device", default=default_device, console=console).strip()
    options = {"device": device} if device else {}
    # A udev rule gives the board a /dev path that survives re-enumeration.
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
    record_cfg: RecordConfig,
    cameras: list[dict],
    visualization: list[dict],
    transfer: dict | None,
    plugins: list[dict] | None = None,
) -> dict:
    """The raw-TOML dict for the config writer. `backend` is written only when
    pinned (`auto` stays implicit); empty sections are omitted.
    """
    from octacam.cameras.registry import is_auto
    from octacam.config import TranscodeConfig

    doc: dict = {}
    if not is_auto(backend):
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
    """Save each camera's sensor params into *target*; return the filenames.

    Never fatal: a camera that will not open (say, a live session holds it) is
    skipped with a warning, and the GUI's Save... completes the config later.
    """
    if not serials:
        return []
    from octacam import config_writer
    from octacam.cameras.base import BackendError
    from octacam.cameras.system import CameraSystem

    try:
        system = CameraSystem(requested_serial_numbers=serials, backend=backend)
    except BackendError as e:
        console.print(
            f"[yellow]Skipping sensor parameters[/yellow] \N{EM DASH} could not open "
            "the "
            f"camera(s): {e}\n  A camera is likely in use by another session; run "
            "`octacam gui` and use Save\N{HORIZONTAL ELLIPSIS} to capture them later."
        )
        return []
    except Exception as e:  # missing SDK, unknown serial, ... -- never fatal here
        console.print(
            f"[yellow]Skipping sensor parameters[/yellow] \N{EM DASH} could not open "
            "the "
            f"camera(s): {e}"
        )
        return []
    try:
        pfs = system.save_all_params()
        if not pfs:
            return []
        # Each backend has its own format (.pfs / .txt): a mixed rig writes per serial.
        ext_by_serial = system.extension_by_serial()
        config_writer.write_pfs_files(target, pfs, ext_by_serial)
        return [f"{serial}.{ext_by_serial.get(serial, 'pfs')}" for serial in pfs]
    finally:
        system.close()


@command
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
    verbose: Verbose = False,
) -> None:
    """Interactively scaffold a new rig config directory.

    Auto-detects the connected cameras (across every installed backend, so a
    Basler+FLIR rig just works), then prompts for the record and transfer
    settings and writes an octacam_config.toml. The visual per-camera bits --
    window placement, rotation, and the grid -- are left to `octacam gui`, which
    tunes them against a live preview; run it next on the new directory.

    By default it also opens each detected camera once to snapshot its current
    sensor parameters into a per-camera file; a busy camera is skipped with a
    warning. Pass --no-snapshot-params to skip that and never open a camera.
    """
    from rich.console import Console
    from rich.prompt import Confirm, Prompt

    from octacam import config_writer

    console = Console()
    console.print("[bold]octacam config[/bold] \N{EM DASH} set up a new rig config\n")

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
            f"{cfg_file} already exists \N{EM DASH} overwrite?",
            default=False,
            console=console,
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

    console.print(f"\n[green]\N{CHECK MARK}[/green] Wrote [bold]{written}[/bold]")
    if snapshot_params:
        serials = [c["serial_number"] for c in cameras] or [s for s, _ in detected]
        saved = _snapshot_camera_params(console, chosen_backend, serials, target)
        if saved:
            console.print(
                f"[green]\N{CHECK MARK}[/green] Saved sensor parameters: "
                f"{', '.join(saved)}"
            )
    console.print("\nNext steps:")
    console.print(f"  \N{BULLET} Validate it:          octacam doctor {target}")
    console.print(f"  \N{BULLET} Place cameras & grid: octacam gui {target}")
    console.print(f"  \N{BULLET} Record headlessly:    octacam record {target}")
