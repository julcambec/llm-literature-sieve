from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from litsieve.acquire import import_search
from litsieve.cli import app
from litsieve.config import ConfigError, load_project
from litsieve.protocol import plan_searches, write_planned
from litsieve.recall import RecallError, check_recall, load_seeds

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def imported(example):
    project = load_project(example)
    write_planned(project, plan_searches(project)[0])
    import_search(project, "S001", FIXTURES / "pubmed_sample.txt", run_date="2026-11-04")
    return project


def _status(report):
    return {r.seed.id: r.status for r in report.results}


def test_single_search(imported):
    report = check_recall(imported, search_ids=["S001"])
    assert report.blocks == ["A"]
    assert _status(report) == {
        "lost2018": "missed",
        "handbook2020": "found_title",
        "doe2021": "found",
        "virtanen2019": "found",
    }
    by_id = {r.seed.id: r for r in report.results}
    assert by_id["virtanen2019"].matches == [("S001-0001", "doi")]  # DOI case is normalised
    assert by_id["doe2021"].matches == [("S001-0002", "pmid")]
    assert by_id["handbook2020"].matches == [("S001-0003", "title")]
    assert "pmcid not checked" in by_id["lost2018"].note
    assert report.results[0].seed.id == "lost2018"  # misses first
    assert not report.passed and report.mode == "searches"


def test_block_mode_reports_missing_exports(imported):
    report = check_recall(imported, block="A")
    assert report.search_ids == ["S001"]
    assert report.missing_exports == ["S002"]
    assert report.mode == "block"


def test_manual_and_seedless_blocks(imported, tmp_path):
    hand = tmp_path / "hand.csv"
    hand.write_text(
        "source_id,title,year,url\nx/y,A sensor repo,2024,https://github.com/x/y\n",
        encoding="utf-8",
    )
    import_search(imported, "S004", hand, run_date="2026-11-04", fmt="common")
    report = check_recall(imported, search_ids=["S004"])
    assert _status(report) == {"doe2021": "manual"}
    assert "look for" in report.results[0].note and report.passed

    import_search(
        imported, "S003", FIXTURES / "scopus_sample.csv", run_date="2026-11-04", fmt="scopus"
    )
    report = check_recall(imported, search_ids=["S003"])
    assert report.blocks_without_seeds == ["B"] and report.results == []


def test_scope_errors(imported):
    with pytest.raises(RecallError, match="either"):
        check_recall(imported)
    with pytest.raises(RecallError, match="no export yet"):
        check_recall(imported, search_ids=["S002"])
    with pytest.raises(RecallError, match="not in the searches sheet"):
        check_recall(imported, search_ids=["S099"])


def test_seed_config_is_checked(example):
    path = example / "config" / "search_protocol.yaml"
    doc = yaml.safe_load(path.read_text())
    doc["recall"]["seeds"][0]["expected_in"] = ["Z"]
    doc["recall"]["seeds"][1]["alt_ids"] = {"isbn": "123"}
    doc["recall"]["match_on"] = ["doi", "eid"]
    path.write_text(yaml.safe_dump(doc, sort_keys=False))
    with pytest.raises(ConfigError) as err:
        load_seeds(load_project(example))
    text = str(err.value)
    assert "unknown block 'Z'" in text and "'isbn'" in text and "'eid'" in text


def test_cli_recall(imported, example):
    result = CliRunner().invoke(app, ["--root", str(example), "recall", "S001"])
    assert result.exit_code == 1, result.output
    assert "MISSED" in result.output and "Lost 2018" in result.output
    assert "title only" in result.output
    assert "First check whether the source indexes them" in result.output

    result = CliRunner().invoke(app, ["--root", str(example), "recall", "--block", "A"])
    assert "Partial: no export yet for S002." in result.output
    assert "log the query as a new search" in result.output.replace("\n", " ")
