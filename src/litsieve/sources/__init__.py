"""Runners and importers, one per source, registered by name.

An importer turns a file exported by hand into hits; a runner executes a logged
query and returns hits itself. Both feed sources.export, which writes the common
export format. The importer is chosen by the search's `source`, or by --format.
"""

from __future__ import annotations

from collections.abc import Callable

from litsieve.sources import common, openalex, pubmed, scopus
from litsieve.sources.base import Importer, ImportFormatError, RunnerError, RunResult

IMPORTERS: dict[str, Importer] = {
    "pubmed": pubmed.parse_medline,
    "scopus": scopus.parse_csv,
    "common": common.parse_csv,
}

RUNNERS: dict[str, Callable[..., RunResult]] = {
    "openalex": openalex.run,
}

__all__ = ["IMPORTERS", "RUNNERS", "ImportFormatError", "RunResult", "RunnerError"]
