"""`octacam flash`: compare each serial plugin board's build fingerprint with its
sketch and (unless --check) upload the current firmware with arduino-cli.
`record` runs the same check at start (`preflight_firmware`).
"""

import contextlib
import logging
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Annotated

import typer

from octacam.cli._common import Verbose, command, resolve_config_arg, stderr_console

if TYPE_CHECKING:
    from octacam.config import OctacamConfig
    from octacam.plugins.serial import SerialPlugin

log = logging.getLogger("octacam")


def _load_config_or_empty(config_dir: Path | None) -> OctacamConfig:
    """Load a rig config if present, else the default (empty) one."""
    from octacam.config import OctacamConfig, load_config_dir

    if config_dir is None:
        return OctacamConfig()
    try:
        return load_config_dir(config_dir)
    except Exception as e:
        log.debug("flash: could not load config at %s: %s", config_dir, e)
        return OctacamConfig()


def _flashable_plugins(plugins, only: str | None) -> list[SerialPlugin]:
    """The plugins with board firmware, optionally filtered to one."""
    from octacam.plugins import canonical_name
    from octacam.plugins.serial import SerialPlugin

    out = [p for p in plugins.plugins if isinstance(p, SerialPlugin)]
    if only:
        # build_plugins loaded an alias (arduino) under its current name.
        out = [p for p in out if p.name == canonical_name(only)]
    return out


def _confirm_flash(console, prov: dict, *, assume_yes: bool, indent: str = "") -> bool:
    """Whether to upload to this board: `assume_yes`, else the operator's
    answer. A board that sent no identity is warned about first, since a flash
    overwrites whatever it runs.
    """
    from rich.prompt import Confirm

    if prov.get("state") == "unidentified":
        console.print(
            f"{indent}[yellow]\N{WARNING SIGN} the board sent no identity[/yellow] "
            f"\N{EM DASH} flashing "
            "overwrites whatever is on it; only proceed if this is the right board."
        )
    return assume_yes or Confirm.ask(
        f"{indent}Upload the current firmware to {prov.get('device')}?",
        default=False,
        console=console,
    )


def _flash_one(
    console, plugin, prov: dict, *, assume_yes: bool, check_only: bool
) -> int:
    """Report one board's firmware and, unless --check, offer to flash it.
    Returns 0 when up to date or flashed, or under --check when not known to be
    out of date; else 1.
    """
    device = prov.get("device")
    console.print()
    console.print(f"[bold]{plugin.name}[/bold] \N{EM DASH} {device or 'no device'}")
    # A board that did not open cannot be probed: never call it up to date.
    if not plugin.is_ready():
        console.print(
            "  [red]could not open the board[/red] \N{EM DASH} it may be unplugged, "
            "the "
            "device path may be wrong, or the port may be held by a running octacam "
            "session. Its firmware can't be read or flashed here."
        )
        return 1
    console.print(f"  board firmware: {prov.get('firmware') or '(no identity reply)'}")
    console.print(f"  source build:   {prov.get('needed_build') or '(not available)'}")
    # An unclassified board is never called up to date; the provisioner says why.
    if prov.get("state") is None:
        verdict = (
            "[yellow]? unknown[/yellow]"
            if prov.get("firmware_ok")
            else "[red]\N{BALLOT X} incompatible firmware[/red]"
        )
        console.print(f"  {verdict} \N{EM DASH} {prov.get('detail', '')}")
        if check_only:  # --check fails only on a board known to be out of date
            return 0 if prov.get("firmware_ok") else 1
        if prov.get("needed_build") is None:
            console.print(
                "  Flash it manually with arduino-cli, or set OCTACAM_ARDUINO_DIR to "
                "a checkout's arduino/ folder."
            )
        return 1
    if not prov.get("needs_flash"):
        console.print("  [green]\N{CHECK MARK} up to date[/green]")
        return 0
    console.print(
        f"  [yellow]needs flashing[/yellow] \N{EM DASH} {prov.get('detail', '')}"
    )
    if check_only:
        return 1
    # Classified, so the source is there: only arduino-cli can be missing.
    if not prov.get("can_flash"):
        console.print(
            "  [red]Can't auto-flash:[/red] arduino-cli was not found. Install it "
            "(https://arduino.github.io/arduino-cli/) or set OCTACAM_ARDUINO_CLI."
        )
        return 1
    if not _confirm_flash(console, prov, assume_yes=assume_yes, indent="  "):
        console.print("  skipped \N{EM DASH} the board keeps its current firmware.")
        return 1
    console.print(
        "  Flashing (compile + upload, ~1 min; the board reboots at the "
        "end)\N{HORIZONTAL ELLIPSIS}"
    )
    result = plugin.flash_firmware(
        on_line=lambda ln: console.print(f"    [dim]{ln}[/dim]")
    )
    if result.ok:
        console.print(f"  [green]\N{CHECK MARK} {result.message}[/green]")
        return 0
    console.print(f"  [red]\N{BALLOT X} {result.message}[/red]")
    return 1


def preflight_firmware(plugins, *, assume_yes: bool) -> None:
    """At record start, offer to reflash a stale board: prompt on a TTY unless
    `--yes`; under `--yes`, or headless with `auto_flash`, flash without
    asking, but only a board known to run an old build of this sketch (never a
    blank or foreign one); otherwise warn.
    """
    from octacam.plugins.serial import SerialPlugin

    interactive = sys.stdin.isatty()
    for p in plugins.plugins:
        if not isinstance(p, SerialPlugin):
            continue
        try:
            prov = p.firmware_provisioning()
        except Exception:
            log.debug(
                "firmware preflight: %s provisioning failed", p.name, exc_info=True
            )
            continue
        if not prov.get("needs_flash"):
            continue
        device = prov.get("device")
        can = bool(prov.get("can_flash"))
        safe = bool(prov.get("safe_to_auto_flash"))
        auto = bool(prov.get("auto_flash"))
        msg = (
            f"{p.name}: board firmware on {device} is out of date \N{EM DASH} "
            f"{prov.get('detail', '')}"
        )
        do_flash = False
        if interactive and can and not assume_yes:
            console = stderr_console()
            console.print(f"[yellow]{msg}[/yellow]")
            do_flash = _confirm_flash(console, prov, assume_yes=False)
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
            log.info("%s: flashing current firmware\N{HORIZONTAL ELLIPSIS}", p.name)
            result = p.flash_firmware(
                on_line=lambda ln: log.info("  arduino-cli: %s", ln)
            )
            log.log(
                logging.INFO if result.ok else logging.ERROR,
                "%s: %s",
                p.name,
                result.message,
            )


def _flash_boards(
    console, config, plugin: str | None, device: str | None, yes: bool, check_only: bool
) -> int:
    """Report (and unless *check_only*, flash) each flashable plugin's board;
    the command's exit code.
    """
    from octacam.plugins import build_plugins

    plugins = build_plugins(config, [plugin] if plugin else None)
    flashable = _flashable_plugins(plugins, plugin)
    if not flashable:
        which = f" {plugin!r}" if plugin else ""
        console.print(
            f"[yellow]No firmware-flashable serial plugin{which} is enabled.[/yellow] "
            "triggerbox, twophoton, and flywheel support firmware flashing."
        )
        return 1 if plugin else 0

    exit_code = 0
    for p in flashable:
        if device:
            p.configured_device = device
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
    return exit_code


@command
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
        typer.Option(
            "--device", help="Serial device override (e.g. /dev/ttyACM0 or auto)."
        ),
    ] = None,
    yes: Annotated[
        bool,
        typer.Option("--yes", "-y", help="Flash without prompting when out of date."),
    ] = False,
    check_only: Annotated[
        bool,
        typer.Option(
            "--check",
            help="Report only; exit nonzero if any board is out of date. Never "
            "flashes.",
        ),
    ] = False,
    verbose: Verbose = False,
) -> None:
    """Check a serial plugin's board firmware and upload the current sketch if needed.

    Reads the board's identify banner, compares its build fingerprint to the sketch
    source in `arduino/<name>`, and (unless `--check`) compiles + uploads the
    current firmware with arduino-cli. Exits 0 when every board is up to date (or
    was flashed), nonzero otherwise; without the sketch source a board's build is
    unknown, which fails a flash but not `--check`.
    """
    from rich.console import Console

    from octacam import locks

    console = Console()
    if config_dir is not None:
        config_dir = resolve_config_arg(config_dir)
    config = _load_config_or_empty(config_dir)
    if config_dir is None and not plugin and not config.plugins:
        raise typer.BadParameter(
            "give a rig config directory or --plugin <name>", param_hint="--plugin"
        )

    # Flashing resets the board, which another octacam may have armed.
    try:
        with (
            locks.instance_lock(config_dir)
            if config_dir is not None
            else contextlib.nullcontext()
        ):
            raise typer.Exit(
                _flash_boards(console, config, plugin, device, yes, check_only)
            )
    except locks.RigInUse as e:
        console.print(
            f"[red]Another octacam instance owns this rig (pid {e.holder})[/red] "
            f"\N{EM DASH} "
            "its board may be armed. Stop it before flashing."
        )
        raise typer.Exit(2) from None
