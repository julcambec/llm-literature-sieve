import pytest
import yaml

from litsieve.config import ConfigError, load_project


def test_loads_example(example):
    project = load_project(example)
    d = project.dictionary
    assert d.has_field("records", "overlap_c1")
    assert d.has_field("records", "overlap_c2")
    assert (
        d.field("records", "overlap_c2").description
        == 'Overlap with C2, "Low-cost sensor networks".'
    )
    assert d.codes("question") == ["Q1", "Q2"]
    assert d.codes("block") == ["A", "B", "R", "L", "NA"]
    assert d.codes("run_id") == []  # no register yet
    assert project.path("workbook") == (example / "data" / "review.xlsx").resolve()
    assert project.path("prompts") == (example / "prompts").resolve()


def test_run_ids_come_from_register(example):
    (example / "data" / "runs").mkdir()
    (example / "data" / "runs" / "register.csv").write_text("run_id,model\nRUN01,m\nRUN02,m\n")
    assert load_project(example).dictionary.codes("run_id") == ["RUN01", "RUN02"]


def _edit(path, change):
    doc = yaml.safe_load(path.read_text())
    change(doc)
    path.write_text(yaml.safe_dump(doc, sort_keys=False))


def test_reports_every_problem(example):
    def break_fields(doc):
        doc["sheets"]["searches"]["fields"]["source"]["values"] = "sources"  # typo
        doc["sheets"]["records"]["fields"]["uses"]["colour"] = "red"  # unknown key
        doc["checks"][1]["then"]["value"] = "N/A"  # not a valid value

    _edit(example / "config" / "fields.yaml", break_fields)
    with pytest.raises(ConfigError) as err:
        load_project(example)
    text = str(err.value)
    assert "values list 'sources' not found" in text
    assert "unknown keys ['colour']" in text
    assert "'N/A' is not a valid seed_record_id value" in text


def test_bad_condition_value_is_caught(example):
    _edit(
        example / "config" / "fields.yaml",
        lambda doc: doc["checks"][1]["when"].update(value=["citation_sideways"]),
    )
    with pytest.raises(ConfigError, match="citation_sideways"):
        load_project(example)


def test_tier_rule_codes_are_checked(example):
    _edit(
        example / "config" / "project.yaml",
        lambda doc: doc["tier_rule"]["extended_if_any"][1].update(value=["weights"]),
    )
    with pytest.raises(ConfigError, match="'weights' is not a uses code"):
        load_project(example)
