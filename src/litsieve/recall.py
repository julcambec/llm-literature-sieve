"""
Workflow step 3: check that searches retrieve the protocol's known seed records.

Scope is either a set of searches (one export alone, e.g. while testing a source) or
a whole block (the union of its exports, which is the gate before screening). The
seeds in scope are those whose `expected_in` names a block in scope.

Protocol shape (search_protocol.yaml, under `recall`):

- `match_on`: identifier kinds compared with the exports: doi, pmid, pmcid, arxiv.
- `seeds`: each with `id`, `short_cite`, `title`, `doi`, `alt_ids` ({kind: value}),
  `expected_in` (blocks) and optionally `manual_check` ({block: instruction}) for
  blocks whose exports carry no identifiers.
- `blocks_without_seeds`: blocks that rely on stop rules and citation chasing.

A seed found by identifier is `found`. A seed whose title matches a hit exactly
(ignoring case and punctuation) but whose identifiers do not is `found_title`: the
query retrieved it, but the export lacks the identifier. Otherwise it is `missed`,
unless every block in scope asks for a manual check, in which case it is `manual`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from litsieve.config import ConfigError, Project
from litsieve.rules import is_blank
from litsieve.sources import export as ex
from litsieve.workbook import read_rows

KIND_COLUMNS = {
    "doi": "doi",
    "pmid": "pmid",
    "pmcid": "pmcid",
    "arxiv": "arxiv_id",
    "arxiv_id": "arxiv_id",
}
KIND_NORMALIZERS = {
    "doi": ex.normalize_doi,
    "pmid": ex.normalize_pmid,
    "pmcid": ex.normalize_pmcid,
    "arxiv_id": ex.normalize_arxiv,
}
STATUS_ORDER = ("missed", "found_title", "manual", "found")


class RecallError(Exception):
    """The recall check cannot run as asked."""


@dataclass
class Seed:
    id: str
    short_cite: str
    title: str | None
    ids: dict[str, str]  # export column -> normalised value
    expected_in: list[str]
    manual: dict[str, str]


@dataclass
class SeedResult:
    seed: Seed
    status: str
    matches: list[tuple[str, str]] = field(default_factory=list)  # (hit_id, matched on)
    note: str = ""


@dataclass
class RecallReport:
    search_ids: list[str]
    blocks: list[str]
    mode: str  # "searches" or "block"
    results: list[SeedResult]
    missing_exports: list[str]  # searches in the block without an export yet
    blocks_without_seeds: list[str]

    @property
    def missed(self) -> list[SeedResult]:
        return [r for r in self.results if r.status == "missed"]

    @property
    def passed(self) -> bool:
        return not self.missed


def normalize_title(title: Any) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(title or "").lower()).strip()


def load_seeds(project: Project) -> tuple[list[str], list[Seed], list[str], list[str]]:
    """(match columns, seeds, blocks without seeds, kinds in alt_ids but not in match_on)."""
    spec = project.protocol.get("recall") or {}
    blocks = set((project.protocol.get("blocks") or {}).keys())
    problems: list[str] = []

    match_on = []
    for kind in spec.get("match_on") or ["doi"]:
        if kind not in KIND_COLUMNS:
            problems.append(
                f"recall.match_on: unknown kind {kind!r} (known: {', '.join(KIND_COLUMNS)})"
            )
        else:
            match_on.append(KIND_COLUMNS[kind])

    seeds, seen, ignored = [], set(), set()
    for i, raw in enumerate(spec.get("seeds") or []):
        sid = raw.get("id") or f"seed {i + 1}"
        if sid in seen:
            problems.append(f"recall.seeds: {sid} appears twice")
        seen.add(sid)
        ids: dict[str, str] = {}
        candidates = {"doi": raw.get("doi"), **(raw.get("alt_ids") or {})}
        for kind, value in candidates.items():
            if value in (None, ""):
                continue
            if kind not in KIND_COLUMNS:
                problems.append(f"recall.seeds.{sid}: unknown identifier kind {kind!r}")
                continue
            column = KIND_COLUMNS[kind]
            normalised = KIND_NORMALIZERS[column](value)
            if normalised is None:
                problems.append(f"recall.seeds.{sid}: {kind} {value!r} is not valid")
            elif column in match_on:
                ids[column] = normalised
            else:
                ignored.add(kind)
        expected = [str(b) for b in raw.get("expected_in") or []]
        problems += [
            f"recall.seeds.{sid}: unknown block {b!r}" for b in expected if b not in blocks
        ]
        if not expected:
            problems.append(f"recall.seeds.{sid}: expected_in is empty")
        if not ids and not raw.get("title"):
            problems.append(f"recall.seeds.{sid}: needs an identifier in match_on or a title")
        seeds.append(
            Seed(
                id=sid,
                short_cite=raw.get("short_cite") or sid,
                title=raw.get("title"),
                ids=ids,
                expected_in=expected,
                manual={
                    str(k): " ".join(str(v).split())
                    for k, v in (raw.get("manual_check") or {}).items()
                },
            )
        )

    without = [str(b) for b in spec.get("blocks_without_seeds") or []]
    problems += [
        f"recall.blocks_without_seeds: unknown block {b!r}" for b in without if b not in blocks
    ]
    if problems:
        raise ConfigError(problems)
    return match_on, seeds, without, sorted(ignored)


def _searches(project: Project) -> dict[str, dict[str, Any]]:
    d = project.dictionary
    rows = read_rows(project.path("workbook"), "searches", d.fields("searches"))
    return {str(r["search_id"]): r for r in rows if r.get("search_id")}


def _export_file(project: Project, row: dict[str, Any]) -> Any:
    if is_blank(row.get("export_file")):
        return None
    path = project.data_dir / str(row["export_file"])
    return path if path.is_file() else None


def check_recall(
    project: Project, search_ids: list[str] | None = None, block: str | None = None
) -> RecallReport:
    if bool(search_ids) == bool(block):
        raise RecallError("Give either search IDs or --block, not both or neither")
    match_on, seeds, without, ignored = load_seeds(project)
    searches = _searches(project)

    missing_exports: list[str] = []
    if block:
        in_block = [sid for sid, r in searches.items() if r.get("block") == block]
        if not in_block:
            raise RecallError(f"No searches in block {block!r} in the searches sheet")
        scope = [sid for sid in in_block if _export_file(project, searches[sid])]
        missing_exports = [sid for sid in in_block if sid not in scope]
        if not scope:
            raise RecallError(f"No search in block {block} has an export yet")
        blocks = [block]
    else:
        scope = []
        for sid in search_ids:
            if sid not in searches:
                raise RecallError(f"{sid} is not in the searches sheet")
            if not _export_file(project, searches[sid]):
                raise RecallError(f"{sid} has no export yet; import or run it first")
            scope.append(sid)
        blocks = sorted({str(searches[sid].get("block")) for sid in scope} - {"NA", "None"})

    by_column: dict[str, dict[str, list[str]]] = {c: {} for c in match_on}
    by_title: dict[str, list[str]] = {}
    for sid in scope:
        for row in ex.read_export(project, _export_file(project, searches[sid])):
            for column in match_on:
                value = row.get(column)
                if value:
                    by_column[column].setdefault(str(value), []).append(row["hit_id"])
            title = normalize_title(row.get("title"))
            if title:
                by_title.setdefault(title, []).append(row["hit_id"])

    results = []
    for seed in seeds:
        relevant = [b for b in seed.expected_in if b in blocks]
        if not relevant:
            continue
        if all(b in seed.manual for b in relevant):
            note = "; ".join(f"[{b}] {seed.manual[b]}" for b in relevant)
            results.append(SeedResult(seed, "manual", note=note))
            continue
        matches = [
            (hit_id, column)
            for column, value in seed.ids.items()
            for hit_id in by_column[column].get(value, [])
        ]
        if matches:
            results.append(SeedResult(seed, "found", sorted(set(matches))))
            continue
        title_hits = by_title.get(normalize_title(seed.title), []) if seed.title else []
        if title_hits:
            results.append(
                SeedResult(
                    seed,
                    "found_title",
                    [(h, "title") for h in title_hits],
                    note="the export lacks a matching identifier",
                )
            )
            continue
        unchecked = [k for k in ignored if k in _raw_alt_kinds(project, seed.id)]
        note = (
            f"alt_ids {', '.join(unchecked)} not checked (not in recall.match_on)"
            if unchecked
            else ""
        )
        results.append(SeedResult(seed, "missed", note=note))

    results.sort(key=lambda r: (STATUS_ORDER.index(r.status), r.seed.short_cite))
    return RecallReport(
        search_ids=scope,
        blocks=blocks,
        mode="block" if block else "searches",
        results=results,
        missing_exports=missing_exports,
        blocks_without_seeds=[b for b in blocks if b in without],
    )


def _raw_alt_kinds(project: Project, seed_id: str) -> set[str]:
    for raw in (project.protocol.get("recall") or {}).get("seeds") or []:
        if raw.get("id") == seed_id:
            return set((raw.get("alt_ids") or {}).keys())
    return set()
