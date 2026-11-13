"""
PubMed importer: the MEDLINE (.txt) file saved with Save > Format: PubMed.

Each record is a block of `TAG - value` lines separated by a blank line; long values
continue on lines indented by six spaces. The file keeps PubMed's display order,
which becomes each hit's rank.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from litsieve.sources.base import ImportFormatError

TAG_LINE = re.compile(r"^([A-Z][A-Z0-9]{1,3})\s*- ?(.*)$")
CONTINUATION = "      "


def parse_records(text: str) -> list[dict[str, list[str]]]:
    records: list[dict[str, list[str]]] = []
    current: dict[str, list[str]] | None = None
    last: str | None = None
    for line in text.splitlines():
        if not line.strip():
            if current:
                records.append(current)
            current, last = None, None
            continue
        m = TAG_LINE.match(line)
        if m and not line.startswith(" "):
            tag, value = m.group(1), m.group(2).strip()
            current = current if current is not None else {}
            current.setdefault(tag, []).append(value)
            last = tag
        elif current is not None and last and line.startswith(CONTINUATION):
            current[last][-1] = f"{current[last][-1]} {line.strip()}".strip()
    if current:
        records.append(current)
    return records


def _first(record: dict[str, list[str]], *tags: str) -> str | None:
    for tag in tags:
        for value in record.get(tag, []):
            if value:
                return value
    return None


def _tagged_id(record: dict[str, list[str]], kind: str) -> str | None:
    """A value of LID or AID ending in [kind], e.g. '10.1000/x [doi]'."""
    suffix = f"[{kind}]"
    for value in record.get("LID", []) + record.get("AID", []):
        if value.endswith(suffix):
            return value[: -len(suffix)].strip()
    return None


def to_hit(record: dict[str, list[str]], rank: int) -> dict[str, Any]:
    pmid = _first(record, "PMID")
    if not pmid:
        raise ImportFormatError(f"record {rank} has no PMID")
    authors = record.get("AU") or record.get("CN") or []
    return {
        "rank": rank,
        "source_id": pmid,
        "pmid": pmid,
        "doi": _tagged_id(record, "doi"),
        "pmcid": _first(record, "PMC") or _tagged_id(record, "pmc"),
        "title": _first(record, "TI", "BTI"),
        "abstract": " ".join(record.get("AB", [])) or None,
        "authors": "; ".join(authors) or None,
        "year": _first(record, "DP"),
        "venue": _first(record, "JT", "TA") or ("Book" if "BTI" in record else None),
        "doc_type": "; ".join(record.get("PT", [])) or None,
        "url": f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/",
    }


def parse_medline(path: Path) -> list[dict[str, Any]]:
    records = parse_records(path.read_text(encoding="utf-8-sig"))
    if not records or not any("PMID" in r for r in records):
        raise ImportFormatError(
            f"{path.name}: no MEDLINE records found; save from PubMed with Format: PubMed"
        )
    return [to_hit(r, i) for i, r in enumerate(records, start=1)]
