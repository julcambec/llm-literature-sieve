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


def _report(result) -> None:
    from litsieve.sources.export import summarize

    typer.echo(f"{result.search_id}: {summarize(result.rows)}")
    typer.echo(f"  export   {result.export_path}")
    typer.echo(f"  raw      {result.raw_path}")
    typer.echo(f"  logged   date_run {result.date_run}, n_hits {result.n_hits}")
    for warning in result.warnings:
        typer.echo(f"  warning: {warning}")


@app.command("import")
def import_cmd(
    ctx: typer.Context,
    search_id: Annotated[str, typer.Argument(help="Search ID, e.g. S001.")],
    file: Annotated[Path, typer.Argument(help="The file exported from the source.")],
    date: Annotated[
        str | None,
        typer.Option("--date", help="Date the search ran (YYYY-MM-DD), if date_run is blank."),
    ] = None,
    fmt: Annotated[
        str | None, typer.Option("--format", help="Importer to use (default: the search's source).")
    ] = None,
    force: Annotated[
        bool,
        typer.Option("--force", help="Replace an existing export that has no screening decisions."),
    ] = False,
) -> None:
    """Step 2: convert a hand-exported file into the common export format."""
    from litsieve.acquire import AcquireError, import_search
    from litsieve.sources import ImportFormatError

    project = _load(ctx)
    try:
        result = import_search(project, search_id, file, run_date=date, fmt=fmt, force=force)
    except (AcquireError, ImportFormatError, WorkbookError, ConfigError, ValueError) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from exc
    _report(result)


@app.command()
def run(
    ctx: typer.Context,
    search_id: Annotated[str, typer.Argument(help="Search ID, e.g. S003.")],
    force: Annotated[
        bool,
        typer.Option("--force", help="Replace an existing export that has no screening decisions."),
    ] = False,
) -> None:
    """Step 2: run a scripted search (e.g. OpenAlex) from its logged query."""
    from litsieve.acquire import AcquireError, run_search
    from litsieve.sources import RunnerError

    project = _load(ctx)
    try:
        result = run_search(project, search_id, force=force)
    except (AcquireError, RunnerError, WorkbookError, ConfigError, ValueError) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from exc
    _report(result)


STATUS_LABELS = {
    "found": "found",
    "found_title": "title only",
    "manual": "manual",
    "missed": "MISSED",
}


@app.command()
def recall(
    ctx: typer.Context,
    search_ids: Annotated[
        list[str] | None,
        typer.Argument(help="Searches to check together, e.g. S001 (or use --block)."),
    ] = None,
    block: Annotated[
        str | None, typer.Option("--block", help="Check the union of all exports of this block.")
    ] = None,
) -> None:
    """Step 3: check that the exports retrieve the protocol's seed records."""
    from litsieve.recall import RecallError, check_recall

    project = _load(ctx)
    try:
        report = check_recall(project, search_ids=search_ids or None, block=block)
    except (RecallError, ConfigError, WorkbookError) as exc:
        problems = exc.problems if isinstance(exc, ConfigError) else [str(exc)]
        for problem in problems:
            typer.echo(problem, err=True)
        raise typer.Exit(2) from exc

    scope = ", ".join(report.search_ids)
    typer.echo(f"Recall check of {scope} (block {', '.join(report.blocks) or 'none'})")
    for b in report.blocks_without_seeds:
        typer.echo(f"  Block {b} has no seeds; it relies on stop rules and citation chasing.")
    if not report.results:
        typer.echo("  No seeds are expected in this scope.")
    width = max((len(r.seed.short_cite) for r in report.results), default=0)
    for r in report.results:
        where = ", ".join(f"{hit} ({via})" for hit, via in r.matches)
        detail = "; ".join(x for x in (where, r.note) if x)
        typer.echo(f"  {STATUS_LABELS[r.status]:<10} {r.seed.short_cite:<{width}}  {detail}")

    if report.missing_exports:
        typer.echo(f"  Partial: no export yet for {', '.join(report.missing_exports)}.")
    n_missed = len(report.missed)
    if not n_missed:
        typer.echo("Result: every seed in scope was retrieved.")
        return
    if report.mode == "block":
        typer.echo(
            f"Result: {n_missed} seed(s) missed by the block. Add the missing term and log the "
            "query as a new search in the same block; never edit a query that has run."
        )
    else:
        typer.echo(
            f"Result: {n_missed} seed(s) missed by {scope}. First check whether the source "
            "indexes them at all; the block check (--block) is the gate before screening."
        )
    raise typer.Exit(1)


screen_app = typer.Typer(
    help="Step 5: LLM title-and-abstract screening, one hit per prompt.", no_args_is_help=True
)
app.add_typer(screen_app, name="screen")


@screen_app.command("prepare")
def screen_prepare(
    ctx: typer.Context,
    search_ids: Annotated[
        list[str], typer.Argument(help="Searches whose unscreened hits to include.")
    ],
    model: Annotated[
        str, typer.Option("--model", help="The model you will paste the prompts into.")
    ],
    limit: Annotated[
        int | None, typer.Option("--limit", help="At most this many hits (for a test run).")
    ] = None,
    prompt: Annotated[
        str | None,
        typer.Option(
            "--prompt", help="Template in the prompts folder (default: latest screen_vN)."
        ),
    ] = None,
    include_pending: Annotated[
        bool,
        typer.Option(
            "--include-pending", help="Also include hits waiting for replies in earlier runs."
        ),
    ] = False,
) -> None:
    """Open a screening run: write one prompt per hit and register the run."""
    from litsieve.screen import ScreenError, prepare_screen

    project = _load(ctx)
    try:
        result = prepare_screen(
            project,
            search_ids,
            model,
            limit=limit,
            prompt_id=prompt,
            include_pending=include_pending,
        )
    except (ScreenError, ConfigError, WorkbookError) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from exc
    if result.template_created:
        typer.echo(
            f"Created {project.path('prompts') / (result.prompt_id + '.md')} from the engine default; commit it."
        )
    counts = ", ".join(f"{sid}: {n}" for sid, n in result.per_search.items())
    typer.echo(
        f"{result.run_id}: {result.n_prompts} prompts ({counts}), template {result.prompt_id}"
    )
    typer.echo(f"  prompts  {result.folder / 'all_prompts.md'}")
    typer.echo(f"  replies  {result.folder / 'replies.txt'}")
    typer.echo("Paste each prompt into a new chat, append each JSON reply to replies.txt, then run")
    typer.echo(f"  litsieve screen ingest {result.run_id}")


@screen_app.command("ingest")
def screen_ingest(
    ctx: typer.Context,
    run_id: Annotated[str, typer.Argument(help="The screening run, e.g. RUN01.")],
    force: Annotated[
        bool,
        typer.Option(
            "--force", help="Overwrite earlier LLM values that differ from these replies."
        ),
    ] = False,
) -> None:
    """Validate the replies of a run and write them into the exports."""
    from litsieve.screen import ScreenError, ingest_screen

    project = _load(ctx)
    try:
        r = ingest_screen(project, run_id, force=force)
    except (ScreenError, ConfigError, WorkbookError) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from exc
    typer.echo(
        f"{r.run_id}: {len(r.merged)} merged, {len(r.unchanged)} unchanged, {len(r.invalid)} invalid, "
        f"{len(r.conflicts)} conflicting, {len(r.missing)} without a reply (of {r.n_items})"
    )
    for hit_id, problems in r.invalid.items():
        typer.echo(f"  invalid  {hit_id}: {'; '.join(problems)}")
    for hit_id, fields in r.conflicts.items():
        typer.echo(
            f"  conflict {hit_id}: differs from earlier LLM values in {fields} (use --force)"
        )
    if r.missing:
        shown = ", ".join(r.missing[:10]) + (" ..." if len(r.missing) > 10 else "")
        typer.echo(f"  waiting  {shown}")
    for sid in r.completed_searches:
        typer.echo(f"  {sid}: every hit now has an LLM decision")
    if r.invalid or r.conflicts:
        raise typer.Exit(1)
