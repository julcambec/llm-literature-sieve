"""
Workflow step 2: get a search's hits into the common export format.

`import_search` converts a file exported by hand; `run_search` executes a scripted
search. Both write exports/<search_id>.csv, keep the raw file under raw/, and record
date_run, n_hits and export_file on the search's row. Every check runs before anything
is written, and an export that already holds screening decisions is never replaced.
"""

from __future__ import annotations

import datetime as dt
import json
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from litsieve.config import Project
from litsieve.rules import is_blank
from litsieve.sources import IMPORTERS, RUNNERS
from litsieve.sources import export as ex
from litsieve.workbook import ensure_writable, find_row, update_row

SHEET = "searches"


class AcquireError(Exception):
    """A search cannot be imported or run as asked."""


@dataclass
class AcquireResult:
    search_id: str
    export_path: Path
    raw_path: Path
    rows: list[dict[str, Any]]
    n_hits: int
    date_run: str
    warnings: list[str] = field(default_factory=list)


def _iso(value: Any) -> str:
    if isinstance(value, dt.datetime):
        return value.date().isoformat()
    if isinstance(value, dt.date):
        return value.isoformat()
    return str(value).strip()


def _search_row(project: Project, search_id: str) -> dict[str, Any]:
    d = project.dictionary
    path = project.path("workbook")
    row = find_row(path, SHEET, d.fields(SHEET), d.key(SHEET), search_id)
    if row is None:
        raise AcquireError(
            f"{search_id} is not in the searches sheet of {path}; run `litsieve plan` first"
        )
    return row


def _check_export_free(project: Project, path: Path, force: bool) -> None:
    if not path.exists():
        return
    if not force:
        raise AcquireError(f"{path} already exists; use --force to replace it")
    if ex.has_decisions(project, path):
        raise AcquireError(f"{path} already holds screening decisions and is never replaced")


def _check_rows(project: Project, rows: list[dict[str, Any]]) -> None:
    problems = ex.validate_rows(project, rows)
    if problems:
        shown = "\n".join(f"  - {p}" for p in problems[:30])
        more = f"\n  ... and {len(problems) - 30} more" if len(problems) > 30 else ""
        raise AcquireError(f"The hits fail validation; nothing written:\n{shown}{more}")


def _record(project: Project, search_id: str, values: dict[str, Any]) -> None:
    d = project.dictionary
    update_row(project.path("workbook"), SHEET, d.fields(SHEET), d.key(SHEET), search_id, values)


def import_search(
    project: Project,
    search_id: str,
    file: Path,
    run_date: str | None = None,
    fmt: str | None = None,
    force: bool = False,
) -> AcquireResult:
    """Convert a hand-exported file for `search_id` into the common export format."""
    row = _search_row(project, search_id)
    fmt = fmt or row.get("source")
    if fmt not in IMPORTERS:
        raise AcquireError(
            f"No importer for {fmt!r}; available: {', '.join(sorted(IMPORTERS))} (choose one with --format)"
        )
    file = file.resolve()
    if not file.is_file():
        raise AcquireError(f"File not found: {file}")

    logged = row.get("date_run")
    if run_date is not None:
        try:
            run_date = dt.date.fromisoformat(run_date).isoformat()
        except ValueError as exc:
            raise AcquireError(f"--date {run_date!r} is not an ISO date (YYYY-MM-DD)") from exc
    if is_blank(logged) and run_date is None:
        raise AcquireError(
            f"{search_id} has no date_run; give the date the search ran with --date YYYY-MM-DD"
        )
    if not is_blank(logged) and run_date is not None and _iso(logged) != run_date:
        raise AcquireError(
            f"{search_id} already has date_run {_iso(logged)}; --date {run_date} conflicts"
        )
    date_run = run_date or _iso(logged)

    rows = ex.build_rows(project, search_id, IMPORTERS[fmt](file))
    _check_rows(project, rows)

    out = ex.export_path(project, search_id)
    _check_export_free(project, out, force)
    raw_dir = project.path("raw_exports")
    raw_path = raw_dir / f"{search_id}_{fmt}{file.suffix}"
    if (
        file != raw_path
        and raw_path.exists()
        and raw_path.read_bytes() != file.read_bytes()
        and not force
    ):
        raise AcquireError(f"{raw_path} exists with different content; use --force to replace it")
    ensure_writable(project.path("workbook"))

    warnings = []
    n_hits = row.get("n_hits")
    if is_blank(n_hits):
        n_hits = len(rows)
    elif int(n_hits) != len(rows):
        warnings.append(
            f"n_hits is {n_hits} but the file holds {len(rows)} hits; check the export is complete"
        )

    raw_dir.mkdir(parents=True, exist_ok=True)
    if file != raw_path:
        shutil.copy2(file, raw_path)
    ex.write_export(project, out, rows)
    values = {"export_file": ex.relative_to_data(project, out), "n_hits": int(n_hits)}
    if is_blank(logged):
        values["date_run"] = date_run
    _record(project, search_id, values)
    return AcquireResult(search_id, out, raw_path, rows, int(n_hits), date_run, warnings)


def run_search(
    project: Project,
    search_id: str,
    force: bool = False,
    session: Any = None,
    today: dt.date | None = None,
) -> AcquireResult:
    """Execute a scripted search (e.g. OpenAlex) from its logged query."""
    row = _search_row(project, search_id)
    source = row.get("source")
    if source not in RUNNERS:
        raise AcquireError(
            f"No runner for {source!r} (runners: {', '.join(sorted(RUNNERS))}); "
            f"export by hand and use `litsieve import`"
        )
    if not is_blank(row.get("date_run")):
        raise AcquireError(
            f"{search_id} already ran on {_iso(row['date_run'])}; a new run is a new search with a new ID"
        )
    if is_blank(row.get("query")):
        raise AcquireError(f"{search_id} has no query")
    out = ex.export_path(project, search_id)
    _check_export_free(project, out, force)
    ensure_writable(project.path("workbook"))

    limits = (project.protocol.get("limits") or {}).get(source) or {}
    result = RUNNERS[source](row["query"], limits, project.env, session=session)
    rows = ex.build_rows(project, search_id, result.hits)
    _check_rows(project, rows)

    date_run = (today or dt.date.today()).isoformat()
    raw_dir = project.path("raw_exports")
    raw_dir.mkdir(parents=True, exist_ok=True)
    raw_path = raw_dir / f"{search_id}_{source}.json"
    raw_path.write_text(
        json.dumps(
            {"search_id": search_id, "date_run": date_run, **result.raw},
            ensure_ascii=False,
            indent=1,
        ),
        encoding="utf-8",
    )
    ex.write_export(project, out, rows)
    _record(
        project,
        search_id,
        {
            "date_run": date_run,
            "n_hits": result.total,
            "export_file": ex.relative_to_data(project, out),
        },
    )
    warnings = []
    if result.total > len(rows):
        warnings.append(f"exported the top {len(rows)} of {result.total} hits (export cap)")
    return AcquireResult(search_id, out, raw_path, rows, result.total, date_run, warnings)
