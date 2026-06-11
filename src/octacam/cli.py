import click

import octacam


@click.group(invoke_without_command=True)
@click.version_option(octacam.__version__)
@click.pass_context
def main(ctx: click.Context) -> None:
    """octacam: preview, record, and save video streams from multiple Basler cameras."""
    if ctx.invoked_subcommand is None:
        click.echo(
            "The octacam Python port is under development; the GUI is not yet "
            "available. Use the C++ app in cpp/ in the meantime."
        )


if __name__ == "__main__":
    main()
