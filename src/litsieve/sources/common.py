"""
Importer for files already in the common export format, e.g. hits entered by hand.

Only the hit columns (HIT_KEYS) are read; hit_id, search_id and screening columns are
assigned afresh. Rows with no value at all are skipped.
"""

from __future__ import annotations

import csv
from pathlib import Path
from typing import Any

from litsieve.sources.base import ImportFormatError
from litsieve.sources.export import HIT_KEYS


def parse_csv(path: Path) -> list[dict[str, Any]]:
    with path.open(encoding="utf-8-sig", newline="") as fh:
        reader = csv.DictReader(fh)
        names = [n.strip() for n in reader.fieldnames or []]
        if "title" not in names:
            raise ImportFormatError(
                f"{path.name}: needs a title column; columns: {', '.join(HIT_KEYS)}"
            )
        hits = []
        for raw in reader:
            hit = {
                k.strip(): (v or "").strip() or None
                for k, v in raw.items()
                if k and k.strip() in HIT_KEYS
            }
            if any(hit.values()):
                hits.append(hit)
    return hits
