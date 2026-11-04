"""Command-line interface: one command per workflow step (added as they are built)."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

from litsieve import __version__
from litsieve.config import DEFAULT_CONFIG, ConfigError, Project, load_project
from litsieve.workbook import WorkbookError

app = typer.Typer(help="LLM-assisted literature screening and extraction.", no_args_is_help=True)


@app.callback()
def main(
    ctx: typer.Context,
    root: Annotated[
        Path | None,
        typer.Option(
            "--root", envvar="LITSIEVE_ROOT", help="Project root (default: current directory)."
        ),
    ] = None,
    config: Annotated[
        Path, typer.Option("--config", help="Project config, relative to the root.")
    ] = DEFAULT_CONFIG,
) -> None:
    """litsieve command group."""
    ctx.obj = {"root": root, "config": config}


def _load(ctx: typer.Context) -> Project:
    try:
        return load_project(ctx.obj["root"], ctx.obj["config"])
    except ConfigError as exc:
        typer.echo("Configuration problems:", err=True)
        for problem in exc.problems:
            typer.echo(f"  - {problem}", err=True)
        raise typer.Exit(2) from exc


@app.command()
def version() -> None:
    """Print the installed version."""
    typer.echo(__version__)


@app.command("check-config")
def check_config(ctx: typer.Context) -> None:
    """Load and cross-check the project config, the dictionary and the vocabularies."""
    project = _load(ctx)
    d = project.dictionary
    typer.echo(f"Config OK: {project.config_path}")
    for sheet in d.sheet_names():
        fields = d.fields(sheet)
        checked = sum(f.needs_human_check for f in fields)
        typer.echo(f"  {sheet:<13} {len(fields):>3} fields, {checked:>2} need a human check")
    typer.echo(f"  {len(d.lists)} lookup lists; data folder {project.data_dir}")


@app.command()
def plan(
    ctx: typer.Context,
    update: Annotated[
        bool,
        typer.Option(
            "--update", help="Rewrite rows that differ from the protocol and have not run."
        ),
    ] = False,
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Render and validate; write nothing.")
    ] = False,
    show: Annotated[
        list[str] | None,
        typer.Option("--show", help="Print the full query and limits of this search ID."),
    ] = None,
) -> None:
    """Step 1: write the protocol's planned searches as rows of the searches sheet."""
    from litsieve.protocol import plan_searches, validate_planned, write_planned

    project = _load(ctx)
    try:
        planned, skipped = plan_searches(project)
    except ConfigError as exc:
        typer.echo("Cannot render the protocol:", err=True)
        for problem in exc.problems:
            typer.echo(f"  - {problem}", err=True)
        raise typer.Exit(2) from exc

    by_id = {p.search_id: p for p in planned}
    for sid in show or []:
        if sid in by_id:
            row = by_id[sid].row
            typer.echo(
                f"\n{sid} query:\n{row.get('query')}\n\n{sid} limits:\n{row.get('limits')}\n"
            )
        else:
            typer.echo(f"\n{sid}: not rendered (unknown or skipped)\n", err=True)

    problems = validate_planned(project, planned)
    if problems:
        typer.echo("Rendered rows fail validation; nothing written:", err=True)
        for problem in problems:
            typer.echo(f"  - {problem}", err=True)
        raise typer.Exit(1)

    if dry_run:
        typer.echo(f"{len(planned)} searches render and validate:")
        for p in planned:
            r = p.row
            typer.echo(
                f"  {p.search_id}  {r.get('block', ''):<3} {r.get('source', ''):<16} "
                f"{r.get('search_type', ''):<12} {p.acquisition or '':<9} {len(r.get('query') or ''):>5} chars"
            )
    else:
        try:
            report = write_planned(project, planned, update=update)
        except (ConfigError, WorkbookError) as exc:
            typer.echo(str(exc), err=True)
            raise typer.Exit(1) from exc
        typer.echo(f"Workbook: {project.path('workbook')}")
        typer.echo(
            f"  added {len(report.added)}, updated {len(report.updated)}, unchanged {len(report.unchanged)}"
        )
        for sid, fields in report.differs.items():
            typer.echo(
                f"  {sid}: differs from the protocol in {', '.join(fields)} (rerun with --update)"
            )
        for sid, reason in report.frozen.items():
            typer.echo(f"  {sid}: not changed, {reason}; log a new search instead")

    for s in skipped:
        typer.echo(f"  {s.search_id}: skipped, {s.reason}")
