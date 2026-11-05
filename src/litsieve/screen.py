"""
Workflow step 5: LLM title-and-abstract screening, one hit per prompt.

`prepare_screen` opens a run: it picks the unscreened hits of the given searches,
renders one self-contained prompt per hit, and records the run in the register.
`ingest_screen` reads the replies, validates each against the pass's JSON Schema and
the dictionary's rules, and writes llm_decision, llm_exclusion_reason and llm_note
into the export. `decision` is pre-filled from llm_decision only where it is blank,
so a decision already corrected by hand is never overwritten.

Prompt templates live in the project's prompts folder as screen_v<N>.md. The first
run copies the engine's default there as screen_v1.md. A template is frozen once a
run has used it: an edited template must be saved under the next version.

Protocol conventions read here: `eligibility.general.include` (texts),
`eligibility.general.exclude` (each {criterion, exclusion_reason, except_blocks}),
and per block `looks_for`, `in_if`, `start_year`, `end_year`.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
import shutil
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator

from litsieve import runs
from litsieve.config import Project
from litsieve.rules import check_row, evaluate, is_blank
from litsieve.schema import pass_fields, pass_schema, without_descriptions
from litsieve.sources import export as ex
from litsieve.workbook import ensure_writable, read_rows, update_row

PASS = "screen"
BACKENDS = ("manual_chat",)  # api and local come later
PLACEHOLDER = re.compile(r"\{\{\s*([a-z_]+)\s*\}\}")
TEMPLATE_NAME = re.compile(r"^screen_v(\d+)\.md$")
MISSING = "(not available)"


class ScreenError(Exception):
    """Screening cannot proceed as asked."""


@dataclass
class PrepareResult:
    run_id: str
    folder: Path
    n_prompts: int
    prompt_id: str
    template_created: bool
    per_search: dict[str, int]


@dataclass
class IngestReport:
    run_id: str
    n_items: int
    merged: list[str] = field(default_factory=list)
    unchanged: list[str] = field(default_factory=list)
    invalid: dict[str, list[str]] = field(default_factory=dict)
    conflicts: dict[str, str] = field(default_factory=dict)  # hit_id -> what differs
    missing: list[str] = field(default_factory=list)
    completed_searches: list[str] = field(default_factory=list)


# ---- templates ---------------------------------------------------------------


def default_template() -> str:
    return resources.files("litsieve").joinpath("templates/screen.md").read_text(encoding="utf-8")


def resolve_template(project: Project, prompt_id: str | None) -> tuple[str, str, bool]:
    """(prompt_id, template text, whether the default was copied in)."""
    folder = project.path("prompts")
    if prompt_id:
        path = folder / f"{prompt_id}.md"
        if not path.is_file():
            raise ScreenError(f"Prompt template not found: {path}")
        return prompt_id, path.read_text(encoding="utf-8"), False
    versions = sorted(
        (int(m.group(1)), p)
        for p in folder.glob("screen_v*.md")
        if (m := TEMPLATE_NAME.match(p.name))
    )
    if versions:
        path = versions[-1][1]
        return path.stem, path.read_text(encoding="utf-8"), False
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / "screen_v1.md"
    shutil.copyfile(resources.files("litsieve").joinpath("templates/screen.md"), path)
    return path.stem, path.read_text(encoding="utf-8"), True


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _check_frozen(project: Project, prompt_id: str, sha: str) -> None:
    for m in runs.manifests(project):
        if m.get("prompt_id") == prompt_id and m.get("prompt_sha256") != sha:
            raise ScreenError(
                f"Template {prompt_id} changed after {m['run_id']} used it; save the edited "
                f"version as a new file (e.g. the next screen_v<N>.md) and leave {prompt_id} as it was"
            )


# ---- prompt parts ------------------------------------------------------------


def _years(block: dict[str, Any]) -> str:
    start, end = block.get("start_year"), block.get("end_year")
    if start and end:
        return f"from {start} to {end}"
    if start:
        return f"in {start} or later"
    if end:
        return f"in {end} or earlier"
    return "in any year"


def _bullets(items: list[str]) -> str:
    return "\n".join(f"- {' '.join(str(i).split())}" for i in items) or "- (none)"


def _codes(project: Project, list_name: str) -> str:
    d = project.dictionary
    return "\n".join(f"- `{c}`: {d.code(list_name, c).definition}" for c in d.codes(list_name))


def _field_lines(project: Project) -> str:
    d = project.dictionary
    _, key, fields = pass_fields(d, PASS)
    lines = [f"- `{key}`: copy it exactly from the record."]
    for f in fields:
        extras = []
        extras.append("required" if f.required_at is not None else "optional; null if not needed")
        if f.max_words:
            extras.append(f"at most {f.max_words} words")
        values: list[str] = []
        if f.values:
            codes = d.codes(f.values)
            values = codes + [t for t in f.tokens if t not in codes]
        if values:
            extras.append("one of " + ", ".join(f"`{v}`" for v in values))
        lines.append(f"- `{f.name}` ({'; '.join(extras)}): {f.description}")
    return "\n".join(lines)


EXCLUDE_KEYS = {"criterion", "exclusion_reason", "except_blocks"}


def check_eligibility(project: Project) -> list[str]:
    """Problems in eligibility.general, e.g. an unquoted comma that split a criterion."""
    d = project.dictionary
    general = (project.protocol.get("eligibility") or {}).get("general") or {}
    blocks = set((project.protocol.get("blocks") or {}).keys())
    codes = set(d.codes("exclusion_reason")) if "exclusion_reason" in d.lists else None
    problems = []
    for i, e in enumerate(general.get("exclude") or []):
        where = f"eligibility.general.exclude[{i}]"
        if isinstance(e, str):
            continue
        extra = sorted(set(e) - EXCLUDE_KEYS)
        if extra:
            problems.append(
                f"{where}: unexpected keys {extra}; quote a criterion that contains commas"
            )
        if not e.get("criterion"):
            problems.append(f"{where}: no criterion")
        if codes is not None and e.get("exclusion_reason") not in codes:
            problems.append(
                f"{where}: {e.get('exclusion_reason')!r} is not an exclusion_reason code"
            )
        problems += [
            f"{where}: unknown block {b!r}" for b in e.get("except_blocks") or [] if b not in blocks
        ]
    for i, text in enumerate(general.get("include") or []):
        if not isinstance(text, str):
            problems.append(
                f"eligibility.general.include[{i}]: expected text; quote it if it contains ': '"
            )
    return problems


def _exclusions(project: Project, block_id: str) -> str:
    general = (project.protocol.get("eligibility") or {}).get("general") or {}
    items = []
    for e in general.get("exclude") or []:
        if isinstance(e, str):
            items.append(e)
        elif block_id not in (e.get("except_blocks") or []):
            items.append(f"{e['criterion']} → `{e['exclusion_reason']}`")
    return _bullets(items)


def render_prompt(project: Project, template: str, block_id: str, hit: dict[str, Any]) -> str:
    d = project.dictionary
    spec = d.passes[PASS]
    block = (project.protocol.get("blocks") or {}).get(block_id) or {}
    general = (project.protocol.get("eligibility") or {}).get("general") or {}
    _, key, fields = pass_fields(d, PASS)

    record = "\n".join(
        f"{name}: {MISSING if is_blank(hit.get(name)) else ' '.join(str(hit[name]).split())}"
        for name in spec["given"]
    )
    schema = without_descriptions(pass_schema(d, PASS))
    schema["properties"][key] = {"const": hit[key]}
    lists = {f.name: f.values for f in fields}
    decision_list = lists.get("llm_decision")
    reason_list = lists.get("llm_exclusion_reason")

    parts = {
        "looks_for": " ".join(str(block.get("looks_for") or "").split()) or MISSING,
        "in_if": " ".join(str(block.get("in_if") or "").split()) or MISSING,
        "include": _bullets(general.get("include") or []),
        "years": _years(block),
        "exclude": _exclusions(project, block_id),
        "decisions": _codes(project, decision_list) if decision_list else "",
        "reasons": _codes(project, reason_list) if reason_list else "",
        "record": record,
        "fields": _field_lines(project),
        "schema": json.dumps(schema, ensure_ascii=False),
        "hit_id": str(hit[key]),
        "block": block_id,
    }
    unknown = sorted(set(PLACEHOLDER.findall(template)) - set(parts))
    if unknown:
        raise ScreenError(f"Template uses unknown placeholders: {', '.join(unknown)}")
    return PLACEHOLDER.sub(lambda m: parts[m.group(1)], template).strip() + "\n"


# ---- prepare -----------------------------------------------------------------


def _search_rows(project: Project) -> dict[str, dict[str, Any]]:
    d = project.dictionary
    rows = read_rows(project.path("workbook"), "searches", d.fields("searches"))
    return {str(r["search_id"]): r for r in rows if r.get("search_id")}


def _export_of(project: Project, row: dict[str, Any]) -> Path:
    if is_blank(row.get("export_file")):
        raise ScreenError(f"{row['search_id']} has no export yet; import or run it first")
    path = project.data_dir / str(row["export_file"])
    if not path.is_file():
        raise ScreenError(f"Export file missing: {path}")
    return path


def pending_hits(project: Project) -> dict[str, str]:
    """Hits in earlier screening runs that still lack an llm_decision: hit_id -> run_id."""
    pending: dict[str, str] = {}
    exports: dict[str, dict[str, Any]] = {}
    for m in runs.manifests(project):
        if m.get("pass") != PASS:
            continue
        for item in m.get("items", []):
            sid = item["search_id"]
            if sid not in exports:
                path = ex.export_path(project, sid)
                exports[sid] = (
                    {r["hit_id"]: r for r in ex.read_export(project, path)}
                    if path.is_file()
                    else {}
                )
            row = exports[sid].get(item["hit_id"])
            if row is not None and is_blank(row.get("llm_decision")):
                pending[item["hit_id"]] = m["run_id"]
    return pending


def prepare_screen(
    project: Project,
    search_ids: list[str],
    model: str,
    limit: int | None = None,
    prompt_id: str | None = None,
    include_pending: bool = False,
    today: dt.date | None = None,
) -> PrepareResult:
    d = project.dictionary
    backend = project.config.get("backend", "manual_chat")
    if backend not in BACKENDS:
        raise ScreenError(
            f"backend {backend!r} is not supported yet (supported: {', '.join(BACKENDS)})"
        )
    if not model.strip():
        raise ScreenError("Give the model you will use with --model")
    problems = check_eligibility(project)
    if problems:
        raise ScreenError(
            "Fix the protocol's eligibility criteria first:\n"
            + "\n".join(f"  - {p}" for p in problems)
        )
    spec = d.passes.get(PASS) or {}
    skip_if = spec.get("skip_if")
    searches = _search_rows(project)
    pending = {} if include_pending else pending_hits(project)

    chosen: list[tuple[str, str, dict[str, Any]]] = []  # (search_id, block, hit)
    skipped_pending = 0
    for sid in search_ids:
        if sid not in searches:
            raise ScreenError(f"{sid} is not in the searches sheet")
        block_id = str(searches[sid].get("block"))
        if block_id not in (project.protocol.get("blocks") or {}):
            raise ScreenError(
                f"{sid} belongs to block {block_id!r}, which has no eligibility criteria"
            )
        for hit in ex.read_export(project, _export_of(project, searches[sid])):
            if not is_blank(hit.get("llm_decision")):
                continue
            if skip_if and evaluate(skip_if, hit) is True:
                continue
            if hit["hit_id"] in pending:
                skipped_pending += 1
                continue
            chosen.append((sid, block_id, hit))
    if limit:
        chosen = chosen[:limit]
    if not chosen:
        note = f" ({skipped_pending} are waiting in earlier runs)" if skipped_pending else ""
        raise ScreenError(f"No unscreened hits in {', '.join(search_ids)}{note}")

    prompt_id, template, created = resolve_template(project, prompt_id)
    sha = _sha(template)
    _check_frozen(project, prompt_id, sha)
    run_id = runs.next_run_id(project)
    folder = runs.run_dir(project, run_id)
    (folder / "prompts").mkdir(parents=True, exist_ok=False)

    items, combined = [], []
    for i, (sid, block_id, hit) in enumerate(chosen, start=1):
        text = render_prompt(project, template, block_id, hit)
        name = f"{i:04d}_{hit['hit_id']}.md"
        (folder / "prompts" / name).write_text(text, encoding="utf-8")
        items.append({"hit_id": hit["hit_id"], "search_id": sid, "prompt": f"prompts/{name}"})
        combined.append(f"===== {i}/{len(chosen)}  {hit['hit_id']}  (new chat) =====\n\n{text}")
    (folder / "all_prompts.md").write_text("\n\n".join(combined), encoding="utf-8")
    (folder / "replies.txt").write_text("", encoding="utf-8")

    date = (today or dt.date.today()).isoformat()
    runs.write_manifest(
        folder,
        {
            "run_id": run_id,
            "pass": PASS,
            "date": date,
            "model": model,
            "backend": backend,
            "prompt_id": prompt_id,
            "prompt_sha256": sha,
            "search_ids": sorted({sid for sid, _, _ in chosen}),
            "items": items,
        },
    )
    runs.append_register(
        project,
        {
            "run_id": run_id,
            "date": date,
            "model": model,
            "prompt_id": prompt_id,
            "pass": PASS,
            "n_records": len(items),
            "backend": backend,
        },
    )
    per_search: dict[str, int] = {}
    for sid, _, _ in chosen:
        per_search[sid] = per_search.get(sid, 0) + 1
    return PrepareResult(run_id, folder, len(items), prompt_id, created, per_search)


# ---- ingest ------------------------------------------------------------------


def ingest_screen(project: Project, run_id: str, force: bool = False) -> IngestReport:
    d = project.dictionary
    folder = runs.run_dir(project, run_id)
    try:
        manifest = runs.read_manifest(folder)
    except FileNotFoundError as exc:
        raise ScreenError(str(exc)) from exc
    if manifest.get("pass") != PASS:
        raise ScreenError(f"{run_id} is a {manifest.get('pass')} run, not a screening run")

    _, key, fields = pass_fields(d, PASS)
    returned = [f.name for f in fields]
    items = {i["hit_id"]: i for i in manifest["items"]}
    report = IngestReport(run_id, len(items))
    validator = Draft202012Validator(pass_schema(d, PASS))

    replies: dict[str, dict[str, Any]] = {}
    twice: set[str] = set()
    for obj in runs.read_replies(folder):
        if not isinstance(obj, dict):
            continue
        hit_id = str(obj.get(key, "?"))
        if hit_id not in items:
            report.invalid.setdefault(hit_id, []).append(f"{key} {hit_id!r} is not in {run_id}")
            continue
        errors = [e.message for e in validator.iter_errors(obj)]
        if errors:
            report.invalid[hit_id] = errors
            continue
        if hit_id in replies and replies[hit_id] != obj:
            twice.add(hit_id)
        replies[hit_id] = obj
    for hit_id in twice:
        replies.pop(hit_id)
        report.invalid[hit_id] = ["two different replies; keep one in the replies"]
    for hit_id in replies:  # a valid reply supersedes a broken one pasted earlier
        report.invalid.pop(hit_id, None)

    if replies:
        ensure_writable(project.path("workbook"))
    by_search: dict[str, list[str]] = {}
    for hit_id in replies:
        by_search.setdefault(items[hit_id]["search_id"], []).append(hit_id)

    for sid, hit_ids in by_search.items():
        path = ex.export_path(project, sid)
        rows = ex.read_export(project, path)
        index = {r["hit_id"]: r for r in rows}
        changed = False
        for hit_id in hit_ids:
            row = index.get(hit_id)
            if row is None:
                report.invalid[hit_id] = [f"not found in {path.name}"]
                continue
            new = {name: replies[hit_id].get(name) for name in returned}
            merged = {**row, **new}
            if is_blank(merged.get("decision")):
                merged["decision"] = new.get("llm_decision")
            problems = check_row(d, ex.SHEET, merged, "screen")
            if problems:
                report.invalid[hit_id] = problems
                continue
            old = {name: row.get(name) for name in returned}
            if old == new:
                report.unchanged.append(hit_id)
                continue
            if not is_blank(row.get("llm_decision")) and not force:
                diffs = [n for n in returned if old[n] != new[n]]
                report.conflicts[hit_id] = ", ".join(diffs)
                continue
            row.update(merged)
            report.merged.append(hit_id)
            changed = True
        if changed:
            ex.write_export(project, path, rows)

    for sid in manifest.get("search_ids", []):
        path = ex.export_path(project, sid)
        rows = ex.read_export(project, path) if path.is_file() else []
        skip_if = (d.passes.get(PASS) or {}).get("skip_if")
        todo = [r for r in rows if not (skip_if and evaluate(skip_if, r) is True)]
        if todo and all(not is_blank(r.get("llm_decision")) for r in todo):
            report.completed_searches.append(sid)
            searches = _search_rows(project)
            if is_blank(searches.get(sid, {}).get("run_id")):
                update_row(
                    project.path("workbook"),
                    "searches",
                    d.fields("searches"),
                    "search_id",
                    sid,
                    {"run_id": run_id},
                )

    report.missing = [h for h in items if h not in replies and h not in report.invalid]
    return report
