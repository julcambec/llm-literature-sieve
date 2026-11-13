"""
The common export format: one CSV per search at exports/<search_id>.csv.

Every runner and importer returns plain hit dicts using any of HIT_KEYS. This module
numbers the hits, normalises their identifiers, validates them against the
dictionary's `exports` sheet and reads and writes the files. Column order follows the
dictionary, and the files are UTF-8 with a byte-order mark so Excel opens them cleanly.
"""

from __future__ import annotations

import csv
import re
from pathlib import Path
from typing import Any

from litsieve.config import ConfigError, Project
from litsieve.rules import check_row

SHEET = "exports"
HIT_KEYS = (
    "rank",
    "source_id",
    "doi",
    "pmid",
    "pmcid",
    "arxiv_id",
    "title",
    "abstract",
    "authors",
    "year",
    "venue",
    "doc_type",
    "url",
)
ENGINE_FIELDS = ("hit_id", "search_id", *HIT_KEYS)
MAX_HITS = 9999  # hit_id carries a 4-digit position
ENCODING = "utf-8-sig"

DOI_PREFIX = re.compile(r"^(https?://(dx\.)?doi\.org/|doi:\s*)", re.IGNORECASE)
ARXIV_NEW = re.compile(r"(\d{4}\.\d{4,5})(v\d+)?", re.IGNORECASE)
ARXIV_OLD = re.compile(r"([a-z-]+(\.[A-Z]{2})?/\d{7})(v\d+)?")


# ---- normalisation ----------------------------------------------------------


def clean_text(value: Any) -> str | None:
    if value is None:
        return None
    text = " ".join(str(value).split())
    return text or None


def normalize_doi(value: Any) -> str | None:
    """Bare lowercase DOI, or None if the value is not a DOI."""
    text = clean_text(value)
    if not text:
        return None
    text = DOI_PREFIX.sub("", text).strip().lower()
    return text if text.startswith("10.") and "/" in text and " " not in text else None


def normalize_pmid(value: Any) -> str | None:
    text = clean_text(value)
    m = re.search(r"(\d+)/?$", text or "")
    return m.group(1) if m else None


def normalize_pmcid(value: Any) -> str | None:
    m = re.search(r"PMC\d+", clean_text(value) or "", re.IGNORECASE)
    return m.group(0).upper() if m else None


def normalize_arxiv(value: Any) -> str | None:
    text = clean_text(value) or ""
    m = ARXIV_NEW.search(text) or ARXIV_OLD.search(text)
    return m.group(1) if m else None


def normalize_year(value: Any) -> int | None:
    m = re.search(r"\b(1[89]\d{2}|2\d{3})\b", str(value or ""))
    return int(m.group(1)) if m else None


NORMALIZERS = {
    "doi": normalize_doi,
    "pmid": normalize_pmid,
    "pmcid": normalize_pmcid,
    "arxiv_id": normalize_arxiv,
    "year": normalize_year,
}


# ---- rows --------------------------------------------------------------------


def _fields(project: Project):
    d = project.dictionary
    missing = [name for name in ENGINE_FIELDS if not d.has_field(SHEET, name)]
    if missing:
        raise ConfigError(f"fields.yaml: the {SHEET} sheet lacks engine fields {missing}")
    return d.fields(SHEET)


def build_rows(
    project: Project, search_id: str, hits: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Number and normalise hits in the order given; ranks default to that order."""
    _fields(project)
    if len(hits) > MAX_HITS:
        raise ValueError(f"{len(hits)} hits; the common export format holds at most {MAX_HITS}")
    rows = []
    for i, hit in enumerate(hits, start=1):
        unknown = set(hit) - set(HIT_KEYS)
        if unknown:
            raise ValueError(f"hit {i}: unknown keys {sorted(unknown)}")
        row: dict[str, Any] = {"hit_id": f"{search_id}-{i:04d}", "search_id": search_id}
        for key in HIT_KEYS:
            value = hit.get(key)
            row[key] = NORMALIZERS[key](value) if key in NORMALIZERS else clean_text(value)
        row["rank"] = int(hit["rank"]) if hit.get("rank") not in (None, "") else i
        rows.append(row)
    return rows


def validate_rows(project: Project, rows: list[dict[str, Any]]) -> list[str]:
    d = project.dictionary
    return [f"{r['hit_id']}: {msg}" for r in rows for msg in check_row(d, SHEET, r, "import")]


def summarize(rows: list[dict[str, Any]]) -> str:
    n = len(rows)
    with_doi = sum(1 for r in rows if r.get("doi"))
    with_abstract = sum(1 for r in rows if r.get("abstract"))
    return f"{n} hits, {with_doi} with a DOI, {with_abstract} with an abstract"


# ---- files -------------------------------------------------------------------


def export_path(project: Project, search_id: str) -> Path:
    return project.path("exports") / f"{search_id}.csv"


def relative_to_data(project: Project, path: Path) -> str:
    try:
        return path.resolve().relative_to(project.data_dir).as_posix()
    except ValueError:
        return str(path)


def write_export(project: Project, path: Path, rows: list[dict[str, Any]]) -> None:
    columns = [f.name for f in _fields(project)]
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    with tmp.open("w", encoding=ENCODING, newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=columns, extrasaction="raise")
        writer.writeheader()
        for row in rows:
            writer.writerow({c: "" if row.get(c) is None else row[c] for c in columns})
    tmp.replace(path)


def read_export(project: Project, path: Path) -> list[dict[str, Any]]:
    """Rows of an export, with int fields as ints and blanks as None."""
    fields = {f.name: f for f in _fields(project)}
    rows = []
    with path.open(encoding=ENCODING, newline="") as fh:
        for raw in csv.DictReader(fh):
            row: dict[str, Any] = {}
            for name, value in raw.items():
                value = value.strip() if isinstance(value, str) else value
                if value in (None, ""):
                    row[name] = None
                elif name in fields and fields[name].type == "int":
                    try:
                        row[name] = int(value)
                    except ValueError:
                        row[name] = value
                else:
                    row[name] = value
            rows.append(row)
    return rows


def has_decisions(project: Project, path: Path) -> bool:
    """True if any hit in the export already carries a screening value."""
    d = project.dictionary
    screen_fields = [
        f.name for f in d.fields(SHEET) if f.required_at == "screen" or "screen" in f.passes
    ]
    return any(row.get(name) for row in read_export(project, path) for name in screen_fields)
