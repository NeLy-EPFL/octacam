"""The ``octacam`` command line: the app and its command tree (one module per
command group), logging, and the process-wide setup every command shares."""

import logging
import resource
from typing import Annotated

import typer

import octacam
from octacam._compat import StrEnum
from octacam.cli import (
    admin,
    benchmark,
    doctor,
    flash,
    gui,
    process,
    record,
    wizard,
)
from octacam.cli._common import stderr_console

log = logging.getLogger("octacam")


class LogLevel(StrEnum):
    debug = "debug"
    info = "info"
    warning = "warning"
    error = "error"


def _setup_logging(level: LogLevel) -> None:
    """Route the "octacam" logger through rich on stderr, keeping stdout clean for
    machine-readable output (`record`'s video paths, `--json`)."""
    from rich.logging import RichHandler

    handler = RichHandler(
        console=stderr_console(),
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

    pylon uses ~150 fds per streaming camera (one eventfd per queued URB), so
    8 cameras exceed the usual 1024 and StartGrabbing fails ("Insufficient
    system resources")."""
    soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    if soft < hard:
        resource.setrlimit(resource.RLIMIT_NOFILE, (hard, hard))
        log.debug("Raised open file limit: %d -> %d", soft, hard)


def _version_callback(value: bool) -> None:
    if value:
        typer.echo(octacam.__version__)
        raise typer.Exit()


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


app = typer.Typer(
    add_completion=False,
    no_args_is_help=False,
    # rich markup reads `[...]` in help text, so TOML section names are escaped
    # as `\[record]`.
    rich_markup_mode="rich",
    context_settings={"help_option_names": ["-h", "--help"]},
)
app.callback(invoke_without_command=True)(main_callback)
app.command()(gui.gui)
app.command()(doctor.doctor)
app.command("config")(wizard.config)
app.command()(record.record)
app.command()(flash.flash)
app.command()(benchmark.benchmark)
app.command()(process.check)
app.command(cls=process.ProcessCommand)(process.process)

jobs_app = typer.Typer(
    no_args_is_help=True,
    help="Manage detached `octacam process` jobs (list, attach, pause, cancel).",
    context_settings={"help_option_names": ["-h", "--help"]},
)
jobs_app.command("list")(admin.jobs_list)
jobs_app.command("attach")(admin.jobs_attach)
jobs_app.command("pause")(admin.jobs_pause)
jobs_app.command("resume")(admin.jobs_resume)
jobs_app.command("cancel")(admin.jobs_cancel)
app.add_typer(jobs_app, name="jobs")

cache_app = typer.Typer(
    no_args_is_help=True,
    help="Inspect and clear the octacam cache (recording list, job logs, markers).",
    context_settings={"help_option_names": ["-h", "--help"]},
)
cache_app.command("path")(admin.cache_path)
cache_app.command("info")(admin.cache_info)
cache_app.command("clear")(admin.cache_clear)
app.add_typer(cache_app, name="cache")


def main() -> None:
    from rich.traceback import install

    from octacam.config import ConfigError

    install(show_locals=False)
    try:
        app()
    except ConfigError as e:  # operator error: the file and line, no traceback
        stderr_console().print(f"[bold red]Config error:[/bold red] {e}")
        raise SystemExit(2) from None
