"""
OpenAlex runner: executes a search's logged query against the works API.

The searches row's `query` is passed verbatim as the `filter` parameter, so what ran
is exactly what is logged. From the protocol's `limits.openalex`, `sort` sets the
order and `export_cap` the number of hits exported; the source's total count is
returned separately and goes to n_hits.

Optional settings, read from the environment or the project's .env file:
OPENALEX_MAILTO (an e-mail address, for OpenAlex's polite pool) and OPENALEX_API_KEY.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

import requests

from litsieve.sources.base import Hit, RunnerError, RunResult

API = "https://api.openalex.org/works"
SELECT = (
    "id,doi,display_name,publication_year,type,authorships,primary_location,ids,"
    "abstract_inverted_index,locations"
)
PAGE_SIZE = 200
RETRY_STATUS = {429, 500, 502, 503, 504}
SECRET_PARAMS = {"api_key"}


def reconstruct_abstract(inverted: dict[str, list[int]] | None) -> str | None:
    if not inverted:
        return None
    positions = [(pos, word) for word, places in inverted.items() for pos in places]
    return " ".join(word for _, word in sorted(positions)) or None


def _arxiv(work: dict[str, Any]) -> str | None:
    doi = (work.get("doi") or "").lower()
    if "10.48550/arxiv." in doi:
        return doi.split("10.48550/arxiv.", 1)[1]
    for loc in work.get("locations") or []:
        url = (loc or {}).get("landing_page_url") or ""
        if "arxiv.org/abs/" in url:
            return url.split("arxiv.org/abs/", 1)[1]
    return None


def to_hit(work: dict[str, Any], rank: int) -> Hit:
    ids = work.get("ids") or {}
    primary = work.get("primary_location") or {}
    source = primary.get("source") or {}
    authors = [
        ((a or {}).get("author") or {}).get("display_name") for a in work.get("authorships") or []
    ]
    work_id = (work.get("id") or "").rsplit("/", 1)[-1] or None
    return {
        "rank": rank,
        "source_id": work_id,
        "doi": work.get("doi"),
        "pmid": ids.get("pmid"),
        "pmcid": ids.get("pmcid"),
        "arxiv_id": _arxiv(work),
        "title": work.get("display_name"),
        "abstract": reconstruct_abstract(work.get("abstract_inverted_index")),
        "authors": "; ".join(a for a in authors if a) or None,
        "year": work.get("publication_year"),
        "venue": source.get("display_name"),
        "doc_type": work.get("type"),
        "url": primary.get("landing_page_url") or work.get("doi") or work.get("id"),
    }


def _get(session: Any, params: dict[str, Any], attempts: int = 4) -> dict[str, Any]:
    for attempt in range(attempts):
        try:
            resp = session.get(API, params=params, timeout=60)
        except requests.RequestException as exc:
            if attempt == attempts - 1:
                raise RunnerError(f"OpenAlex request failed: {exc}") from exc
        else:
            if resp.status_code == 200:
                return resp.json()
            if resp.status_code not in RETRY_STATUS or attempt == attempts - 1:
                raise RunnerError(f"OpenAlex returned {resp.status_code}: {resp.text[:500]}")
        time.sleep(2**attempt)
    raise RunnerError("OpenAlex request failed")  # pragma: no cover


def run(
    query: str,
    limits: dict[str, Any],
    env: Callable[[str], str | None],
    session: Any = None,
) -> RunResult:
    cap = int(limits.get("export_cap") or 0) or None
    params: dict[str, Any] = {
        "filter": query,
        "select": SELECT,
        "per-page": min(PAGE_SIZE, cap or PAGE_SIZE),
        "cursor": "*",
    }
    if limits.get("sort"):
        params["sort"] = limits["sort"]
    if env("OPENALEX_MAILTO"):
        params["mailto"] = env("OPENALEX_MAILTO")
    if env("OPENALEX_API_KEY"):
        params["api_key"] = env("OPENALEX_API_KEY")

    session = session or requests.Session()
    works: list[dict[str, Any]] = []
    total: int | None = None
    while True:
        data = _get(session, params)
        meta = data.get("meta") or {}
        if total is None:
            total = int(meta.get("count") or 0)
        page = data.get("results") or []
        works.extend(page)
        cursor = meta.get("next_cursor")
        if not page or not cursor or (cap and len(works) >= cap):
            break
        params["cursor"] = cursor
    if cap:
        works = works[:cap]

    logged = {k: v for k, v in params.items() if k not in SECRET_PARAMS and k != "cursor"}
    raw = {"endpoint": API, "params": logged, "count": total, "results": works}
    return RunResult(
        hits=[to_hit(w, i) for i, w in enumerate(works, start=1)],
        total=total or 0,
        raw=raw,
        params=logged,
    )
