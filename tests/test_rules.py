import pytest

from litsieve.config import load_project
from litsieve.rules import check_row, evaluate


@pytest.fixture
def d(example):
    return load_project(example).dictionary


GOOD = {
    "search_id": "S001",
    "review_stage": 1,
    "block": "A",
    "questions": ["Q1"],
    "contributions": ["NA"],
    "search_type": "database",
    "source": "pubmed",
    "seed_record_id": "NA",
    "query": "x",
    "limits": "y",
}


def test_good_plan_row(d):
    assert check_row(d, "searches", GOOD, "plan") == []


def test_required_by_level(d):
    assert check_row(d, "searches", GOOD, "run") == ["date_run: required", "n_hits: required"]


@pytest.mark.parametrize(
    "change, message",
    [
        ({"block": "Z"}, "block: 'Z' is not a block code"),
        ({"questions": ["Q1", "NA"]}, "questions: token NA must be the only item"),
        ({"questions": ["Q1", "Q1"]}, "questions: 'Q1' appears twice"),
        ({"review_stage": 7}, "review_stage: 7 is outside [1, 4]"),
        ({"search_id": "S1"}, "search_id: 'S1' does not match"),
        ({"seed_record_id": "R0001"}, "check seed_iff_chase failed"),
        ({"search_type": "citation_back"}, "check seed_iff_chase failed"),
        ({"colour": "red"}, "unknown field 'colour'"),
    ],
)
def test_problems(d, change, message):
    problems = check_row(d, "searches", {**GOOD, **change}, "plan")
    assert any(message in p for p in problems), problems


def test_order_check(d):
    row = {**GOOD, "n_hits": 10, "n_screened": 12, "n_included": 3}
    assert "check search_counts_order failed: n_screened (12) > n_hits (10)" in check_row(
        d, "searches", row, "plan"
    )


def test_evaluate_semantics():
    row = {"tags": ["a", "b"], "n": 3, "empty": None}
    assert evaluate({"field": "tags", "op": "eq", "value": "b"}, row)
    assert evaluate({"field": "tags", "op": "nin", "value": ["c"]}, row)
    assert not evaluate({"field": "empty", "op": "ne", "value": "NA"}, row)  # blank fails
    assert evaluate({"field": "empty", "op": "blank"}, row)
    assert evaluate({"field": "n", "op": "ge", "value": 2}, row)
    assert evaluate(
        {
            "any": [
                {"field": "n", "op": "le", "value": 1},
                {"field": "tags", "op": "eq", "value": "a"},
            ]
        },
        row,
    )
    assert evaluate({"parent_field": "role", "op": "eq", "value": "x"}, row) is None
