"""`octacam users`: the rig's users (`[users.<initials>]` in its config)."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

from octacam.cli._common import Verbose, command, resolve_config_arg


@command
def users_list(
    config_dir: Annotated[Path, typer.Argument(help="The rig's config directory.")],
    verbose: Verbose = False,
) -> None:
    """List the rig's users and where each one's data goes."""
    from octacam.config import load_config_dir
    from octacam.users import discover_initials, load_memory, path_status

    config_dir = resolve_config_arg(config_dir)
    config = load_config_dir(config_dir)
    last = load_memory(config_dir).last_user
    if not config.users:
        typer.echo("No users yet: add one with `octacam users add`.")
    for initials, user in sorted(config.users.items()):
        mark = " (last here)" if initials == last else ""
        typer.echo(f"{initials}{mark}  {user.name}".rstrip())
        paths = [("transfer", user.transfer_directory)] + [
            (f"{plugin}.{option}", value)
            for plugin, options in sorted(user.plugins.items())
            for option, value in sorted(options.items())
            if isinstance(value, str)
        ]
        for label, path in paths:
            typer.echo(
                f"    {label}: {path or '(the rig default)'} [{path_status(path)}]"
            )
    found = discover_initials(config.users)
    if found:
        typer.echo(f"Also on the NAS, not set up yet: {', '.join(found)}")


@command
def users_add(
    config_dir: Annotated[Path, typer.Argument(help="The rig's config directory.")],
    initials: Annotated[str, typer.Argument(help="2 to 4 capital letters, like MD.")],
    name: Annotated[str, typer.Option("--name", help="Full name.")] = "",
    transfer_directory: Annotated[
        str | None,
        typer.Option(
            "--transfer-directory",
            help="Where `octacam process` copies their recordings. Default: "
            "guessed from the other users' (their own initials folder).",
        ),
    ] = None,
    options: Annotated[
        list[str] | None,
        typer.Option(
            "--option",
            metavar="PLUGIN.OPTION=VALUE",
            help="A plugin's per-user option, e.g. twophoton.source=/mnt/share/MA; "
            "repeatable. Default: guessed like the transfer directory.",
        ),
    ] = None,
    verbose: Verbose = False,
) -> None:
    """Add a user to the rig config (appended; the rest is left as is)."""
    from octacam.config import load_config_dir
    from octacam.users import add_user, suggest_paths

    config_dir = resolve_config_arg(config_dir)
    initials = initials.strip().upper()
    guess = suggest_paths(load_config_dir(config_dir).users, initials)
    plugins: dict[str, dict[str, str]] = {
        plugin: dict(values) for plugin, values in guess["plugins"].items()
    }
    for item in options or []:
        key, sep, value = item.partition("=")
        plugin, dot, option = key.partition(".")
        if not sep or not dot or not plugin or not option:
            raise typer.BadParameter(f"{item!r} is not PLUGIN.OPTION=VALUE")
        plugins.setdefault(plugin, {})[option] = value
    transfer = (
        transfer_directory
        if transfer_directory is not None
        else guess["transfer_directory"]
    )
    try:
        add_user(
            config_dir,
            initials,
            name=name,
            transfer_directory=transfer,
            plugins=plugins,
        )
    except ValueError as e:
        raise typer.BadParameter(str(e)) from None
    typer.echo(f"Added {initials}: transfer to {transfer or '(the rig default)'}")
    for plugin, values in sorted(plugins.items()):
        for option, value in sorted(values.items()):
            typer.echo(f"  {plugin}.{option} = {value}")
