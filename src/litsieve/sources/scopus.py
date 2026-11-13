"""
Scopus importer: the CSV file from Export > CSV.

Include at least "Citation information" and "Abstract & keywords". Columns are found
by name, so the selection and order of the other columns do not matter. Row order
(the sort chosen in Scopus) becomes each hit's rank.
"""

from __future__ import annotations

import csv
import sys
from pathlib import Path
from typing import Any

from litsieve.sources.base import ImportFormatError

NO_ABSTRACT = "[no abstract available]"


def _field_limit() -> None:
    limit = sys.maxsize
    while True:  # the reference column can exceed csv's default 128 kB field limit
        try:
            csv.field_size_limit(limit)
            return
        except OverflowError:
            limit //= 10


def parse_csv(path: Path) -> list[dict[str, Any]]:
    _field_limit()
    with path.open(encoding="utf-8-sig", newline="") as fh:
        reader = csv.DictReader(fh)
        columns = {(name or "").strip().lower(): name for name in reader.fieldnames or []}
        if "title" not in columns or "eid" not in columns:
            raise ImportFormatError(
                f"{path.name}: no Title or EID column; export from Scopus as CSV with citation information"
            )

        def get(row: dict, name: str) -> str | None:
            column = columns.get(name)
            value = (row.get(column) or "").strip() if column else ""
            return value or None

        hits = []
        for rank, row in enumerate(reader, start=1):
            abstract = get(row, "abstract")
            if abstract and abstract.strip().lower() == NO_ABSTRACT:
                abstract = None
            hits.append(
                {
                    "rank": rank,
                    "source_id": get(row, "eid"),
                    "doi": get(row, "doi"),
                    "pmid": get(row, "pubmed id"),
                    "title": get(row, "title"),
                    "abstract": abstract,
                    "authors": get(row, "authors"),
                    "year": get(row, "year"),
                    "venue": get(row, "source title"),
                    "doc_type": get(row, "document type"),
                    "url": get(row, "link"),
                }
            )
    return hits
