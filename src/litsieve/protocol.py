"""Render a search protocol's planned searches into `searches` rows (workflow step 1).

Protocol shape (search_protocol.yaml):

- `rendering.<source>`: `join` and `wrap` turn a concept set's terms into one
  fragment; `query` is the template of the full query, with `{formula}` and the
  names of that source's `limits` entries as placeholders.
- `limits.<source>`: named templates using `{start_year}`, `{year_before_start}` and
  `{end_year}` from the block. An entry whose placeholders the block cannot fill
  (e.g. no start year) is dropped from both the query and the `limits` text.
- `concept_sets.<SET>.<source>`: the set's terms for that source.
- `blocks.<block>`: `formula` (set names with AND, OR, NOT and parentheses), years,
  and any values for `searches` fields filled at plan time (questions, contributions).
- `non_database_sources.<block>`: `queries` (or `keywords`) joined by '; ', or a
  title-match rule (`title_match_any`, `title_match_all`); a text under the source's
  or the search type's name becomes the method in `limits`. `sources` or `venues`, if
  given, list the sources the entry applies to.
- `planned_searches`: one entry per search. `query_file` (relative to the project
  root) supplies the query verbatim and is required for `llm_agent` searches.

A search is rendered from the block's formula when its source has a `rendering`
entry, and from `non_database_sources` otherwise, so one block can mix both.

Values of `searches` fields filled at plan time are taken from the planned search,
then its block, then the protocol's top level; a missing list or value becomes NA
where the field allows it.
"""

from __future__ import annotations

import re
import string
from dataclasses import dataclass
from typing import Any

from litsieve.config import ConfigError, Project
from litsieve.rules import check_row, is_blank
from litsieve.workbook import UpsertReport, upsert_rows

SHEET = "searches"
TOKEN_RE = re.compile(r"\s*(\(|\)|\bAND\b|\bOR\b|\bNOT\b|[A-Za-z][A-Za-z0-9_]*)")
OPERATORS = {"AND", "OR", "NOT"}


@dataclass
class PlannedSearch:
    search_id: str
    row: dict[str, Any]
    acquisition: str | None = None


@dataclass
class SkippedSearch:
    search_id: str
    reason: str


# ---- formulas and sets -----------------------------------------------------


def tokenize(formula: str) -> list[str]:
    tokens, pos = [], 0
    formula = formula.rstrip()
    while pos < len(formula):
        m = TOKEN_RE.match(formula, pos)
        if not m:
            raise ConfigError(f"Cannot parse formula {formula!r} at {formula[pos:]!r}")
        tokens.append(m.group(1))
        pos = m.end()
    depth = 0
    for t in tokens:
        depth += {"(": 1, ")": -1}.get(t, 0)
        if depth < 0:
            break
    if depth != 0:
        raise ConfigError(f"Unbalanced parentheses in formula {formula!r}")
    return tokens


def has_top_level_or(tokens: list[str]) -> bool:
    depth = 0
    for t in tokens:
        depth += {"(": 1, ")": -1}.get(t, 0)
        if t == "OR" and depth == 0:
            return True
    return False


def render_set(protocol: dict, name: str, source: str) -> str:
    sets = protocol.get("concept_sets") or {}
    if name not in sets:
        raise ConfigError(f"Concept set {name!r} not found")
    terms = (sets[name] or {}).get(source)
    if not terms:
        raise ConfigError(f"Concept set {name!r} has no terms for {source!r}")
    style = (protocol.get("rendering") or {}).get(source) or {}
    joined = style.get("join", " OR ").join(str(t) for t in terms)
    return style.get("wrap", "({terms})").replace("{terms}", joined)


def render_formula(protocol: dict, formula: str, source: str) -> str:
    """Replace set names with their rendered fragments; keep operators and parentheses."""
    tokens = tokenize(formula)
    out = ""
    for t in tokens:
        piece = t if t in OPERATORS or t in "()" else render_set(protocol, t, source)
        if out and not out.endswith("(") and t != ")":
            out += " "
        out += piece
    return f"({out})" if has_top_level_or(tokens) else out


# ---- limits ----------------------------------------------------------------


def _years(block: dict) -> dict[str, Any]:
    start, end = block.get("start_year"), block.get("end_year")
    return {
        "start_year": start,
        "year_before_start": None if start is None else int(start) - 1,
        "end_year": end,
    }


def _fill(template: Any, values: dict[str, Any]) -> str | None:
    """Fill a template; None if a placeholder has no value."""
    text = str(template)
    names = [n for _, n, _, _ in string.Formatter().parse(text) if n]
    if any(values.get(n) is None for n in names):
        return None
    return text.format(**{n: values[n] for n in names})


def render_limits(protocol: dict, source: str, block: dict) -> dict[str, str | None]:
    """Each limit of the source, filled for the block; None where it does not apply."""
    templates = (protocol.get("limits") or {}).get(source) or {}
    values = _years(block)
    return {name: _fill(tmpl, values) for name, tmpl in templates.items()}


def _limits_text(limits: dict[str, str | None]) -> str:
    return "; ".join(f"{k}: {v}" for k, v in limits.items() if v is not None)


def _clean(query: str) -> str:
    query = re.sub(r"\s+", " ", query).strip()
    query = re.sub(r",{2,}", ",", query)
    return query.strip(", ")


# ---- queries ---------------------------------------------------------------


def render_database_search(protocol: dict, block: dict, source: str) -> tuple[str, str]:
    style = (protocol.get("rendering") or {}).get(source)
    if not style or not style.get("query"):
        raise ConfigError(
            f"rendering.{source}.query is not set; e.g. '{{formula}} {{date}} {{language}}'"
        )
    limits = render_limits(protocol, source, block)
    template = str(style["query"])
    names = [n for _, n, _, _ in string.Formatter().parse(template) if n]
    unknown = [n for n in names if n != "formula" and n not in limits]
    if unknown:
        raise ConfigError(f"rendering.{source}.query: unknown placeholders {unknown}")
    values = {n: (limits[n] or "") for n in names if n != "formula"}
    values["formula"] = render_formula(protocol, block["formula"], source)
    return _clean(template.format(**values)), _limits_text(limits)


def _non_database_entry(protocol: dict, block_id: str, source: str) -> dict:
    entry = (protocol.get("non_database_sources") or {}).get(block_id)
    if entry is None:
        raise ConfigError(f"Block {block_id} has no formula and no non_database_sources entry")
    for key in ("sources", "venues"):
        if key in entry and source not in entry[key]:
            raise ConfigError(f"non_database_sources.{block_id}.{key} does not list {source!r}")
    return entry


def _method_and_years(entry: dict | None, block: dict, source: str, search_type: str) -> str:
    parts = []
    if entry:
        method = entry.get(source) if isinstance(entry.get(source), str) else entry.get(search_type)
        if isinstance(method, str):
            parts.append(" ".join(method.split()).rstrip("."))
    years = _years(block)
    if years["start_year"] is not None:
        parts.append(f"years: {years['start_year']}-{years['end_year'] or ''}")
    return "; ".join(parts) or "none"


def render_non_database_search(
    protocol: dict, block_id: str, block: dict, source: str, search_type: str
) -> tuple[str, str]:
    entry = _non_database_entry(protocol, block_id, source)
    items = entry.get("queries") or entry.get("keywords")
    if items:
        query = "; ".join(str(i) for i in items)
    elif entry.get("title_match_any") or entry.get("title_match_all"):
        parts = [str(t) for t in entry.get("title_match_any") or []]
        parts += [
            " AND ".join(str(t) for t in group) for group in entry.get("title_match_all") or []
        ]
        query = "; ".join(parts)
    else:
        raise ConfigError(
            f"non_database_sources.{block_id}: needs queries, keywords or title_match_*"
        )
    return query, _method_and_years(entry, block, source, search_type)


# ---- rows --------------------------------------------------------------------


def plan_searches(project: Project) -> tuple[list[PlannedSearch], list[SkippedSearch]]:
    """Render every planned search; searches that cannot be rendered yet are skipped."""
    d, protocol = project.dictionary, project.protocol
    blocks = protocol.get("blocks") or {}
    plan_fields = [f for f in d.fields(SHEET) if "plan" in f.filled_by]
    planned, skipped, seen = [], [], set()

    for entry in protocol.get("planned_searches") or []:
        sid, block_id = entry.get("search_id"), entry.get("block")
        source, search_type = entry.get("source"), entry.get("search_type")
        if sid in seen:
            raise ConfigError(f"planned_searches: {sid} appears twice")
        seen.add(sid)
        if block_id not in blocks:
            raise ConfigError(f"planned_searches: {sid} names unknown block {block_id!r}")
        block = blocks[block_id] or {}

        if entry.get("query_file"):
            path = project.root / entry["query_file"]
            if not path.is_file():
                skipped.append(SkippedSearch(sid, f"query_file {entry['query_file']} not found"))
                continue
            query = path.read_text(encoding="utf-8").strip()
            nd = (protocol.get("non_database_sources") or {}).get(block_id)
            limits = _method_and_years(nd, block, source, search_type)
        elif search_type == "llm_agent":
            skipped.append(
                SkippedSearch(sid, "llm_agent search needs a query_file holding the full prompt")
            )
            continue
        elif block.get("formula") and source in (protocol.get("rendering") or {}):
            query, limits = render_database_search(protocol, block, source)
        else:
            query, limits = render_non_database_search(
                protocol, block_id, block, source, search_type
            )

        row: dict[str, Any] = {}
        for f in plan_fields:
            if f.name == "query":
                value = query
            elif f.name == "limits":
                value = limits
            elif f.name in entry:
                value = entry[f.name]
            elif f.name in block:
                value = block[f.name]
            else:
                value = protocol.get(f.name)
            if f.type == "list":
                value = list(value or [])
                if f.values:
                    value = d.sort_codes(f.values, [str(v) for v in value])
                if not value and "NA" in f.tokens:
                    value = ["NA"]
            elif is_blank(value) and "NA" in f.tokens:
                value = "NA"
            row[f.name] = value
        planned.append(PlannedSearch(sid, row, entry.get("acquisition")))
    return planned, skipped


def validate_planned(project: Project, planned: list[PlannedSearch]) -> list[str]:
    d = project.dictionary
    return [f"{p.search_id}: {msg}" for p in planned for msg in check_row(d, SHEET, p.row, "plan")]


def write_planned(
    project: Project, planned: list[PlannedSearch], update: bool = False
) -> UpsertReport:
    """Upsert planned rows into the workbook's searches sheet.

    A row whose date_run is set has run and is never changed: a different query is a
    new search with a new ID. A row that has not run is rewritten only with `update`.
    """
    d = project.dictionary
    if not project.data_dir.is_dir():
        raise ConfigError(f"Data folder not found: {project.data_dir}")
    fields = d.fields(SHEET)
    compare = [f.name for f in fields if "plan" in f.filled_by]

    def frozen(row: dict[str, Any]) -> str | None:
        ran = row.get("date_run")
        if is_blank(ran):
            return None
        return f"already run on {ran.date().isoformat() if hasattr(ran, 'date') else ran}"

    return upsert_rows(
        path=project.path("workbook"),
        sheet=SHEET,
        fields=fields,
        key=d.key(SHEET),
        rows=[p.row for p in planned],
        compare=compare,
        frozen_reason=frozen,
        update=update,
    )
