"""Command-line interface: one command per workflow step (added as they are built)."""

import typer

from litsieve import __version__

app = typer.Typer(help="LLM-assisted literature screening and extraction.", no_args_is_help=True)


@app.callback()
def main() -> None:
    """litsieve command group."""


@app.command()
def version() -> None:
    """Print the installed version."""
    typer.echo(__version__)
