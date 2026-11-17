import json
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from litsieve import runs
from litsieve.acquire import import_search
from litsieve.cli import app
from litsieve.config import load_project
from litsieve.protocol import plan_searches, write_planned
from litsieve.schema import pass_schema
from litsieve.screen import ScreenError, ingest_screen, prepare_screen
from litsieve.sources import export as ex

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def ready(example):
    project = load_project(example)
    write_planned(project, plan_searches(project)[0])
    import_search(project, "S001", FIXTURES / "pubmed_sample.txt", run_date="2026-11-04")
    return load_project(example)


def reply(hit_id, decision="include", reason="NA", note=None):
    return json.dumps(
        {
            "hit_id": hit_id,
            "llm_decision": decision,
            "llm_exclusion_reason": reason,
            "llm_note": note,
        }
    )


def write_replies(project, run_id, *lines):
    (runs.run_dir(project, run_id) / "replies.txt").write_text("\n\n".join(lines), encoding="utf-8")


def rows(project, sid="S001"):
    return {r["hit_id"]: r for r in ex.read_export(project, ex.export_path(project, sid))}


def test_schema_from_dictionary(ready):
    schema = pass_schema(ready.dictionary, "screen")
    assert schema["required"] == ["hit_id", "llm_decision", "llm_exclusion_reason"]
    assert schema["properties"]["llm_exclusion_reason"]["enum"] == ["off_topic", "other", "NA"]
    assert schema["additionalProperties"] is False


def test_prepare_writes_one_prompt_per_hit(ready, example):
    result = prepare_screen(ready, ["S001"], "Model X", limit=2)
    assert (result.run_id, result.n_prompts, result.prompt_id) == ("RUN01", 2, "screen_v1")
    assert result.template_created and (example / "prompts" / "screen_v1.md").is_file()

    folder = result.folder
    prompt = (folder / "prompts" / "0001_S001-0001.md").read_text()
    assert "hit_id: S001-0001" in prompt and "{{" not in prompt
    assert "It measures VOCs" in prompt  # the block's in_if
    assert "Editorials and letters without data → `other`" in prompt
    assert "Outdoor air only" in prompt  # except_blocks [B] does not apply to block A
    assert "published in 2015 or later" in prompt
    assert '"hit_id": {"const": "S001-0001"}' in prompt
    assert (folder / "all_prompts.md").read_text().count("(new chat)") == 2

    manifest = runs.read_manifest(folder)
    assert [i["hit_id"] for i in manifest["items"]] == ["S001-0001", "S001-0002"]
    assert runs.read_register(ready)[0]["model"] == "Model X"

    # Hits waiting in RUN01 are not prepared again.
    assert prepare_screen(ready, ["S001"], "Model X").per_search == {"S001": 1}
    with pytest.raises(ScreenError, match="waiting in earlier runs"):
        prepare_screen(ready, ["S001"], "Model X")


def test_ingest(ready):
    result = prepare_screen(ready, ["S001"], "Model X")
    write_replies(
        ready,
        result.run_id,
        "Sure! ```json\n" + reply("S001-0001") + "\n```",
        reply("S001-0002", "exclude", "off_topic", "Outdoor sensors only."),
        reply("S001-0003", "include", "off_topic"),  # reason with include: fails the rules
        reply("S009-0001"),  # not in this run
    )
    report = ingest_screen(ready, result.run_id)
    assert report.merged == ["S001-0001", "S001-0002"]
    assert report.invalid["S001-0003"] == ["check screen_reason_iff_exclude failed"]
    assert report.invalid["S009-0001"] == ["hit_id 'S009-0001' is not in RUN01"]
    current = rows(ready)
    assert current["S001-0002"]["decision"] == "exclude"
    assert current["S001-0002"]["llm_note"] == "Outdoor sensors only."
    assert current["S001-0003"]["llm_decision"] is None
    assert report.completed_searches == []

    write_replies(
        ready,
        result.run_id,
        reply("S001-0001"),
        reply("S001-0002", "exclude", "off_topic", "Outdoor sensors only."),
        reply("S001-0003", "unsure"),
    )
    report = ingest_screen(ready, result.run_id)
    assert report.merged == ["S001-0003"] and len(report.unchanged) == 2
    assert report.completed_searches == ["S001"]


def test_human_decisions_and_conflicts(ready):
    result = prepare_screen(ready, ["S001"], "Model X", limit=1)
    write_replies(ready, result.run_id, reply("S001-0001", "unsure"))
    ingest_screen(ready, result.run_id)

    # A corrected decision survives; a different LLM value is a conflict unless forced.
    data = rows(ready)
    data["S001-0001"]["decision"] = "include"
    ex.write_export(ready, ex.export_path(ready, "S001"), list(data.values()))
    write_replies(ready, result.run_id, reply("S001-0001", "exclude", "other"))
    report = ingest_screen(ready, result.run_id)
    assert report.conflicts == {"S001-0001": "llm_decision, llm_exclusion_reason"}
    report = ingest_screen(ready, result.run_id, force=True)
    assert report.merged == ["S001-0001"]
    assert rows(ready)["S001-0001"]["decision"] == "include"
    assert rows(ready)["S001-0001"]["llm_decision"] == "exclude"


def test_schema_errors_and_superseded_replies(ready):
    result = prepare_screen(ready, ["S001"], "Model X", limit=2)
    write_replies(
        ready,
        result.run_id,
        '{"hit_id": "S001-0001", "llm_decision": "maybe", "extra": 1}',
        '{"hit_id": "S001-0002", "llm_decision": "maybe"}',
        reply("S001-0001"),
    )  # the corrected reply, pasted after the broken one
    report = ingest_screen(ready, result.run_id)
    assert report.merged == ["S001-0001"]
    problems = " ".join(report.invalid["S001-0002"])
    assert (
        "'maybe' is not one of" in problems
        and "'llm_exclusion_reason' is a required property" in problems
    )


def test_conflicting_duplicate_replies(ready):
    result = prepare_screen(ready, ["S001"], "Model X", limit=1)
    write_replies(ready, result.run_id, reply("S001-0001"), reply("S001-0001", "unsure"))
    report = ingest_screen(ready, result.run_id)
    assert "two different replies" in report.invalid["S001-0001"][0]


def test_template_is_frozen_once_used(ready, example):
    prepare_screen(ready, ["S001"], "Model X", limit=1)
    template = example / "prompts" / "screen_v1.md"
    template.write_text(template.read_text() + "\nBe brief.\n")
    with pytest.raises(ScreenError, match="changed after RUN01"):
        prepare_screen(ready, ["S001"], "Model X", limit=1)
    (example / "prompts" / "screen_v2.md").write_text(template.read_text())
    assert prepare_screen(ready, ["S001"], "Model X", limit=1).prompt_id == "screen_v2"


def test_bad_eligibility_is_caught(ready, example):
    path = example / "config" / "search_protocol.yaml"
    text = path.read_text().replace(
        "{criterion: Editorials and letters without data, exclusion_reason: other}",
        "{criterion: Editorials, letters, exclusion_reason: other}",
    )
    path.write_text(text)
    assert yaml.safe_load(text)["eligibility"]["general"]["exclude"][0]["letters"] is None
    with pytest.raises(ScreenError, match="quote a criterion that contains commas"):
        prepare_screen(load_project(example), ["S001"], "Model X")


def test_extract_json_objects():
    text = 'noise {"a": 1} more {"b": {"c": [1, 2]}} {broken {"d": "}"}'
    assert runs.extract_json_objects(text) == [{"a": 1}, {"b": {"c": [1, 2]}}, {"d": "}"}]


def test_cli_screen(ready, example):
    runner = CliRunner()
    result = runner.invoke(
        app, ["--root", str(example), "screen", "prepare", "S001", "--model", "M", "--limit", "1"]
    )
    assert result.exit_code == 0, result.output
    assert "RUN01: 1 prompts (S001: 1)" in result.output
    write_replies(ready, "RUN01", reply("S001-0001"))
    result = runner.invoke(app, ["--root", str(example), "screen", "ingest", "RUN01"])
    assert result.exit_code == 0, result.output
    assert "1 merged" in result.output
