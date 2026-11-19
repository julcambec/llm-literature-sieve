from pathlib import Path

import pytest

from litsieve.sources import openalex
from litsieve.sources.base import ImportFormatError, RunnerError
from litsieve.sources.export import normalize_arxiv, normalize_doi, normalize_pmcid, normalize_pmid
from litsieve.sources.pubmed import parse_medline
from litsieve.sources.scopus import parse_csv

FIXTURES = Path(__file__).parent / "fixtures"


def test_medline():
    hits = parse_medline(FIXTURES / "pubmed_sample.txt")
    assert [h["rank"] for h in hits] == [1, 2, 3]
    first, second, book = hits
    assert first["title"] == (
        "Potted plants and volatile organic compounds in open-plan offices: a field study."
    )
    assert first["abstract"].endswith("adding plants. Results: Concentrations fell slightly.")
    assert first["doi"] == "10.1000/EXAMPLE.2019.001"  # normalised later, in export
    assert first["pmcid"] == "PMC6000001"
    assert first["authors"] == "Virtanen A; Smith JQ"
    assert first["venue"] == "Indoor air"
    assert first["doc_type"] == "Journal Article; Randomized Controlled Trial"
    assert first["url"] == "https://pubmed.ncbi.nlm.nih.gov/30000001/"
    assert second["doi"] == "10.1000/example.2021.002"  # from AID; the LID is a [pii]
    assert second["abstract"] is None
    assert book["title"] == "Houseplants: a handbook"
    assert book["authors"] == "Indoor Air Working Group"


def test_medline_rejects_other_formats(tmp_path):
    path = tmp_path / "x.txt"
    path.write_text("Title,Year\nSomething,2020\n")
    with pytest.raises(ImportFormatError, match="Format: PubMed"):
        parse_medline(path)


def test_scopus():
    first, second = parse_csv(FIXTURES / "scopus_sample.csv")
    assert first["source_id"] == "2-s2.0-85000000001"
    assert first["title"] == "Potted plants and VOCs, revisited"
    assert first["pmid"] == "30000099"
    assert second["abstract"] is None and second["doi"] is None
    assert second["doc_type"] == "Conference Paper"


def test_normalizers():
    assert normalize_doi("https://doi.org/10.1000/ABC.1") == "10.1000/abc.1"
    assert normalize_doi("doi: 10.1000/x") == "10.1000/x"
    assert normalize_doi("S0000-0000(21)00001-1") is None
    assert normalize_pmid("https://pubmed.ncbi.nlm.nih.gov/123") == "123"
    assert normalize_pmcid("https://www.ncbi.nlm.nih.gov/pmc/articles/pmc77") == "PMC77"
    assert normalize_arxiv("arXiv:2101.00001v2") == "2101.00001"


class FakeResponse:
    def __init__(self, status, data=None, text=""):
        self.status_code, self._data, self.text = status, data, text

    def json(self):
        return self._data


class FakeSession:
    """Serves pages of works; records the parameters of each call."""

    def __init__(self, pages, total):
        self.pages, self.total, self.calls = pages, total, []

    def get(self, url, params, timeout):
        self.calls.append(dict(params))
        i = len(self.calls) - 1
        cursor = f"c{i + 1}" if i + 1 < len(self.pages) else None
        return FakeResponse(
            200, {"meta": {"count": self.total, "next_cursor": cursor}, "results": self.pages[i]}
        )


def work(n, **extra):
    return {
        "id": f"https://openalex.org/W{n}",
        "doi": f"https://doi.org/10.1000/W{n}",
        "display_name": f"Work {n}",
        "publication_year": 2022,
        "type": "article",
        "authorships": [{"author": {"display_name": "Aino Virtanen"}}],
        "primary_location": {
            "source": {"display_name": "Indoor Air"},
            "landing_page_url": f"https://x.org/{n}",
        },
        "ids": {"pmid": f"https://pubmed.ncbi.nlm.nih.gov/{n}"},
        "abstract_inverted_index": {"plants": [1], "Potted": [0], "help.": [2]},
        **extra,
    }


def test_openalex_pages_and_cap():
    session = FakeSession([[work(1), work(2)], [work(3), work(4)], [work(5)]], total=812)
    env = {"OPENALEX_MAILTO": "me@example.org", "OPENALEX_API_KEY": "secret"}.get
    result = openalex.run(
        "title_and_abstract.search:(plant)",
        {"sort": "relevance_score:desc", "export_cap": 3},
        env,
        session,
    )
    assert result.total == 812
    assert [h["source_id"] for h in result.hits] == ["W1", "W2", "W3"]
    assert len(session.calls) == 2  # stops once the cap is reached
    first = session.calls[0]
    assert first["filter"] == "title_and_abstract.search:(plant)"
    assert first["sort"] == "relevance_score:desc" and first["per-page"] == 3
    assert first["mailto"] == "me@example.org" and first["api_key"] == "secret"
    assert session.calls[1]["cursor"] == "c1"
    assert "api_key" not in result.raw["params"]  # secrets never reach raw/
    assert result.hits[0]["abstract"] == "Potted plants help."


def test_openalex_arxiv_and_errors():
    hit = openalex.to_hit(work(9, doi="https://doi.org/10.48550/arXiv.2101.00001"), 1)
    assert hit["arxiv_id"] == "2101.00001"

    class Failing:
        def get(self, url, params, timeout):
            return FakeResponse(400, text="Invalid query parameters")

    with pytest.raises(RunnerError, match="400: Invalid query"):
        openalex.run("bad", {}, lambda name: None, Failing())
