import csv
import datetime as dt
from pathlib import Path

import pytest
from openpyxl import load_workbook
from test_sources import FakeSession, work
from typer.testing import CliRunner

from litsieve.acquire import AcquireError, import_search, run_search
from litsieve.cli import app
from litsieve.config import load_project
from litsieve.protocol import plan_searches, write_planned

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def planned(example):
    project = load_project(example)
    write_planned(project, plan_searches(project)[0])
    return load_project(example)


def _row(root, sid):
    ws = load_workbook(root / "data" / "review.xlsx")["searches"]
    header = [c.value for c in ws[1]]
    for r in range(2, ws.max_row + 1):
        if ws.cell(r, 1).value == sid:
            return {h: ws.cell(r, i + 1).value for i, h in enumerate(header)}


def test_import_pubmed(planned, example):
    result = import_search(planned, "S001", FIXTURES / "pubmed_sample.txt", run_date="2026-11-04")
    assert [r["hit_id"] for r in result.rows] == ["S001-0001", "S001-0002", "S001-0003"]
    assert result.rows[0]["doi"] == "10.1000/example.2019.001"  # lowercased
    assert result.rows[2]["year"] == 2020
    assert result.raw_path == example / "data" / "raw" / "S001_pubmed.txt"
    assert result.raw_path.read_bytes() == (FIXTURES / "pubmed_sample.txt").read_bytes()

    with (example / "data" / "exports" / "S001.csv").open(encoding="utf-8-sig") as fh:
        rows = list(csv.DictReader(fh))
    assert rows[0]["title"].startswith("Potted plants") and rows[0]["decision"] == ""

    row = _row(example, "S001")
    assert row["date_run"] == "2026-11-04"
    assert row["n_hits"] == 3
    assert row["export_file"] == "exports/S001.csv"
    assert row["query"]  # the plan's cells are untouched


def test_import_guards(planned, example):
    with pytest.raises(AcquireError, match="--date"):
        import_search(planned, "S001", FIXTURES / "pubmed_sample.txt")
    with pytest.raises(AcquireError, match="No importer for 'openalex'"):
        import_search(planned, "S002", FIXTURES / "pubmed_sample.txt", run_date="2026-11-04")
    with pytest.raises(AcquireError, match="not in the searches sheet"):
        import_search(planned, "S099", FIXTURES / "pubmed_sample.txt", run_date="2026-11-04")

    import_search(planned, "S001", FIXTURES / "pubmed_sample.txt", run_date="2026-11-04")
    with pytest.raises(AcquireError, match="already exists"):
        import_search(planned, "S001", FIXTURES / "pubmed_sample.txt")
    with pytest.raises(AcquireError, match="conflicts"):
        import_search(
            planned, "S001", FIXTURES / "pubmed_sample.txt", run_date="2026-11-05", force=True
        )
    import_search(
        planned, "S001", FIXTURES / "pubmed_sample.txt", force=True
    )  # date already logged

    # Once a hit carries a screening value, the export is never replaced.
    path = example / "data" / "exports" / "S001.csv"
    text = path.read_text(encoding="utf-8-sig").splitlines()
    text[1] = text[1] + "include" if text[1].endswith(",") else text[1]
    path.write_text("\n".join(text) + "\n", encoding="utf-8-sig")
    with pytest.raises(AcquireError, match="screening decisions"):
        import_search(planned, "S001", FIXTURES / "pubmed_sample.txt", force=True)


def test_import_warns_on_count_mismatch(planned, example):
    from litsieve.workbook import update_row

    d = planned.dictionary
    update_row(
        example / "data" / "review.xlsx",
        "searches",
        d.fields("searches"),
        "search_id",
        "S001",
        {"date_run": "2026-11-04", "n_hits": 5},
    )
    result = import_search(planned, "S001", FIXTURES / "pubmed_sample.txt")
    assert result.n_hits == 5
    assert "n_hits is 5 but the file holds 3 hits" in result.warnings[0]


def test_import_scopus_by_format(planned, example):
    result = import_search(
        planned, "S003", FIXTURES / "scopus_sample.csv", run_date="2026-11-04", fmt="scopus"
    )
    assert result.raw_path.name == "S003_scopus.csv"
    assert result.rows[1]["abstract"] is None


def test_run_openalex(planned, example):
    session = FakeSession([[work(1), work(2)]], total=2)
    result = run_search(planned, "S002", session=session, today=dt.date(2026, 11, 4))
    assert [r["source_id"] for r in result.rows] == ["W1", "W2"]
    assert result.rows[0]["pmid"] == "1" and result.rows[0]["doi"] == "10.1000/w1"
    assert session.calls[0]["filter"].startswith("title_and_abstract.search:(houseplant")
    assert result.raw_path.name == "S002_openalex.json"
    row = _row(example, "S002")
    assert (row["date_run"], row["n_hits"], row["export_file"]) == (
        "2026-11-04",
        2,
        "exports/S002.csv",
    )

    with pytest.raises(AcquireError, match="already ran on 2026-11-04"):
        run_search(planned, "S002", session=session)
    with pytest.raises(AcquireError, match="No runner for 'pubmed'"):
        run_search(planned, "S001", session=session)


def test_cli_import(planned, example):
    result = CliRunner().invoke(
        app,
        [
            "--root",
            str(example),
            "import",
            "S001",
            str(FIXTURES / "pubmed_sample.txt"),
            "--date",
            "2026-11-04",
        ],
    )
    assert result.exit_code == 0, result.output
    assert "S001: 3 hits, 2 with a DOI, 1 with an abstract" in result.output
    result = CliRunner().invoke(
        app, ["--root", str(example), "import", "S001", str(FIXTURES / "pubmed_sample.txt")]
    )
    assert result.exit_code == 1 and "already exists" in result.output
