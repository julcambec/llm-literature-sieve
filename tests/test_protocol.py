import pytest
from openpyxl import load_workbook
from typer.testing import CliRunner

from litsieve.cli import app
from litsieve.config import ConfigError, load_project
from litsieve.protocol import plan_searches, render_formula, validate_planned, write_planned


def _rows(root):
    planned, skipped = plan_searches(load_project(root))
    return {p.search_id: p.row for p in planned}, {s.search_id: s.reason for s in skipped}


def test_database_queries(example):
    rows, _ = _rows(example)
    assert rows["S001"]["query"] == (
        '("Plants"[Mesh] OR houseplant*[tiab] OR "potted plant*"[tiab]) AND '
        '(("Air Pollution, Indoor"[Mesh] OR VOC*[tiab]) OR (office*[tiab] OR workplace*[tiab])) '
        "AND 2015:2026[dp] AND english[la]"
    )
    assert rows["S001"]["limits"] == "date: AND 2015:2026[dp]; language: AND english[la]"
    assert rows["S002"]["query"].startswith("title_and_abstract.search:(houseplant OR")
    assert rows["S002"]["query"].endswith(",publication_year:>2014")
    assert "export_cap: 300" in rows["S002"]["limits"]


def test_top_level_or_is_wrapped_and_missing_years_drop_the_date(example):
    rows, _ = _rows(example)
    q = rows["S003"]["query"]
    assert q.startswith("((") and q.endswith(") AND english[la]")
    assert "[dp]" not in q and "date:" not in rows["S003"]["limits"]


def test_plan_fields_from_block(example):
    rows, _ = _rows(example)
    assert rows["S003"]["questions"] == ["Q1", "Q2"]  # vocabulary order
    assert rows["S003"]["contributions"] == ["NA"]  # empty list -> NA
    assert rows["S001"]["review_stage"] == 1
    assert rows["S001"]["seed_record_id"] == "NA"


def test_non_database_and_query_file(example):
    rows, skipped = _rows(example)
    assert rows["S004"]["query"] == '"plant sensor"; "indoor air dataset"'
    assert rows["S004"]["limits"] == "Repository search, one query per keyword; years: 2020-"
    assert "S005" in skipped
    (example / "prompts" / "S005.md").write_text("Find vendors of plant-based purifiers.\n")
    rows, skipped = _rows(example)
    assert rows["S005"]["query"] == "Find vendors of plant-based purifiers."
    assert not skipped


def test_rows_validate(example):
    project = load_project(example)
    assert validate_planned(project, plan_searches(project)[0]) == []


def test_formula_errors():
    protocol = {"concept_sets": {"A": {"pubmed": ["a"]}}, "rendering": {"pubmed": {}}}
    assert render_formula(protocol, "A AND (A OR A)", "pubmed") == "(a) AND ((a) OR (a))"
    with pytest.raises(ConfigError, match="Unbalanced"):
        render_formula(protocol, "A AND (A", "pubmed")
    with pytest.raises(ConfigError, match="no terms for 'scopus'"):
        render_formula(protocol, "A", "scopus")
    with pytest.raises(ConfigError, match="not found"):
        render_formula(protocol, "B", "pubmed")


def _sheet(root):
    ws = load_workbook(root / "data" / "review.xlsx")["searches"]
    header = [c.value for c in ws[1]]
    return ws, {ws.cell(r, 1).value: r for r in range(2, ws.max_row + 1)}, header


def test_write_is_idempotent_and_protects_run_rows(example):
    project = load_project(example)
    planned, _ = plan_searches(project)
    assert len(write_planned(project, planned).added) == 4
    assert len(write_planned(project, planned).unchanged) == 4

    ws, rows, header = _sheet(example)
    assert ws.cell(rows["S003"], header.index("questions") + 1).value == "Q1;Q2"

    # Mark S001 as run, then change the protocol so S001 and S002 render differently.
    ws.cell(rows["S001"], header.index("date_run") + 1).value = "2026-11-04"
    ws.parent.save(example / "data" / "review.xlsx")
    planned, _ = plan_searches(project)
    for p in planned:
        if p.search_id in ("S001", "S002"):
            p.row["query"] += " AND extra"

    report = write_planned(project, planned)
    assert report.differs == {"S002": ["query"]}
    assert "S001" in report.frozen and "already run on 2026-11-04" in report.frozen["S001"]

    report = write_planned(project, planned, update=True)
    assert report.updated == ["S002"] and "S001" in report.frozen
    ws, rows, header = _sheet(example)
    assert ws.cell(rows["S002"], header.index("query") + 1).value.endswith(" AND extra")
    assert not ws.cell(rows["S001"], header.index("query") + 1).value.endswith(" AND extra")


def test_cli_plan(example):
    runner = CliRunner()
    result = runner.invoke(app, ["--root", str(example), "plan", "--dry-run", "--show", "S004"])
    assert result.exit_code == 0, result.output
    assert '"plant sensor"; "indoor air dataset"' in result.output
    assert "S005: skipped" in result.output
    assert not (example / "data" / "review.xlsx").exists()

    result = runner.invoke(app, ["--root", str(example), "plan"])
    assert result.exit_code == 0, result.output
    assert "added 4" in result.output


def test_cli_reports_config_problems(example):
    (example / "config" / "lookups.yaml").write_text("lists: {}\n")
    result = CliRunner().invoke(app, ["--root", str(example), "check-config"])
    assert result.exit_code == 2
    assert "Configuration problems" in result.output
