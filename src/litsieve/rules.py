"""Field and row checks, shared by validate.py (LLM output) and lint.py (workbook).

A row is a mapping of field name to value in its in-memory form: lists are Python
lists, booleans are bools, missing-value tokens are the strings "NR" and "NA", and
blank is None, "" or [].
"""

from __future__ import annotations

import datetime as dt
import itertools
import re
from typing import Any

from litsieve.dictionary import TOKENS, Dictionary, Field

URL_RE = re.compile(r"^https?://\S+$")
FIELD_REF_RE = re.compile(r"^([A-Za-z0-9_-]+\.)?[a-z][a-z0-9_]*$")


def is_blank(value: Any) -> bool:
    return (
        value is None
        or (isinstance(value, str) and not value.strip())
        or (isinstance(value, list) and not value)
    )


def _norm(value: Any) -> str:
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    return str(value).strip()


def _num(value: Any) -> float | None:
    if isinstance(value, bool) or value in TOKENS:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def evaluate(cond: dict, row: dict, parent: dict | None = None) -> bool | None:
    """Evaluate a condition; None means it cannot be decided (no parent row given).

    On a list field, eq/in/contains_any are true if any item matches, and ne/nin are
    true if no item matches. Every comparison on a blank field is false, except
    `blank` itself.
    """
    for combinator, test in (("any", any), ("all", all)):
        if combinator in cond:
            results = [evaluate(c, row, parent) for c in cond[combinator]]
            if combinator == "any" and True in results:
                return True
            if combinator == "all" and False in results:
                return False
            return None if None in results else test(results)

    if "parent_field" in cond:
        if parent is None:
            return None
        value = parent.get(cond["parent_field"])
    else:
        value = row.get(cond["field"])

    op, target = cond["op"], cond.get("value")
    if op == "blank":
        return is_blank(value)
    if op == "nonblank":
        return not is_blank(value)
    if is_blank(value):
        return False

    items = [_norm(v) for v in (value if isinstance(value, list) else [value])]
    if op in ("eq", "ne"):
        hit = _norm(target) in items
        return hit if op == "eq" else not hit
    if op in ("in", "nin", "contains_any"):
        targets = {_norm(t) for t in (target if isinstance(target, list) else [target])}
        hit = any(i in targets for i in items)
        return not hit if op == "nin" else hit
    numbers = [n for n in (_num(i) for i in items) if n is not None]
    if not numbers:
        return False
    if op == "ge":
        return any(n >= float(target) for n in numbers)
    return any(n <= float(target) for n in numbers)  # le


def _in_range(number: float, rng: tuple[Any, Any] | None) -> bool:
    if not rng:
        return True
    low, high = rng
    return (low is None or number >= low) and (high is None or number <= high)


def _check_format(fmt: str, value: str) -> list[str]:
    if fmt == "url" and not URL_RE.match(value):
        return [f"{value!r} is not an http(s) URL"]
    if fmt == "field_ref" and not FIELD_REF_RE.match(value):
        return [f"{value!r} is not a field name"]
    if fmt == "date":
        try:
            dt.date.fromisoformat(value)
        except ValueError:
            return [f"{value!r} is not an ISO date"]
    return []


def _check_item(d: Dictionary, f: Field, item: Any) -> list[str]:
    if not isinstance(item, str):
        return [f"list item {item!r} is not text"]
    if f.values:
        return [] if item in d.lists[f.values] else [f"{item!r} is not a {f.values} code"]
    if f.item_pattern and not re.fullmatch(f.item_pattern, item):
        return [f"{item!r} does not match {f.item_pattern}"]
    if f.item_format:
        return _check_format(f.item_format, item)
    return []


def check_value(d: Dictionary, f: Field, value: Any) -> list[str]:
    """Type, vocabulary, pattern and range checks for one non-blank value."""
    if is_blank(value):
        return []

    if f.type == "list":
        if not isinstance(value, list):
            return [f"expected a list, got {value!r}"]
        if len(value) == 1 and value[0] in f.tokens:
            return []
        problems, seen = [], set()
        for item in value:
            if item in TOKENS:
                problems.append(
                    f"token {item} must be the only item"
                    if item in f.tokens
                    else f"token {item} is not allowed here"
                )
                continue
            if item in seen:
                problems.append(f"{item!r} appears twice")
            seen.add(item)
            problems += _check_item(d, f, item)
        return problems

    if isinstance(value, str) and value in TOKENS:
        return [] if value in f.tokens else [f"token {value} is not allowed here"]

    if f.type == "text":
        if not isinstance(value, str):
            return [f"expected text, got {value!r}"]
        problems = []
        if f.pattern and not re.fullmatch(f.pattern, value):
            problems.append(f"{value!r} does not match {f.pattern}")
        if f.format:
            problems += _check_format(f.format, value)
        if f.max_words and len(value.split()) > f.max_words:
            problems.append(f"{len(value.split())} words; the limit is {f.max_words}")
        if f.key_values and value != "all" and "=" in value:
            dim = value.split("=", 1)[0]
            if dim not in d.lists[f.key_values]:
                problems.append(f"{dim!r} is not a {f.key_values} code")
        return problems

    if f.type == "date":
        if isinstance(value, (dt.date, dt.datetime)):
            return []
        return (
            _check_format("date", value)
            if isinstance(value, str)
            else [f"expected a date, got {value!r}"]
        )

    if f.type == "bool":
        return [] if isinstance(value, bool) else [f"expected true or false, got {value!r}"]

    if f.type == "enum":
        if not isinstance(value, str):
            return [f"expected a code, got {value!r}"]
        return [] if value in d.lists[f.values] else [f"{value!r} is not a {f.values} code"]

    if f.type in ("int", "num"):
        number = _num(value)
        if number is None or isinstance(value, str):
            return [f"expected a number, got {value!r}"]
        if f.type == "int" and not float(number).is_integer():
            return [f"expected a whole number, got {value!r}"]
        rng = f.range if f.range is not None else (None if f.range_by else d.type_range(f.type))
        return [] if _in_range(number, rng) else [f"{value!r} is outside {list(rng)}"]

    return []


def _run_check(d: Dictionary, check: dict, row: dict, parent: dict | None) -> list[str]:
    name, kind = check["name"], check["kind"]

    if kind == "conditional":
        when = evaluate(check["when"], row, parent)
        if when is None:
            return []
        branch = check.get("then") if when else check.get("else")
        if branch is None:
            return []
        return [f"check {name} failed"] if evaluate(branch, row, parent) is False else []

    if kind == "order":
        values = [(f, _num(row.get(f))) for f in check["fields"]]
        values = [(f, n) for f, n in values if n is not None]
        return [
            f"check {name} failed: {a} ({x:g}) > {b} ({y:g})"
            for (a, x), (b, y) in itertools.pairwise(values)
            if x > y
        ]

    if kind == "range_by_lookup":
        key = row.get(check["key_field"])
        code = d.code(check["lookup"], key) if isinstance(key, str) else None
        rng = tuple(code.attrs["range"]) if code and code.attrs.get("range") else None
        problems = []
        for fname in check["fields"]:
            number = _num(row.get(fname))
            if number is not None and not _in_range(number, rng):
                problems.append(
                    f"check {name} failed: {fname} {number:g} is outside {list(rng)} for {key}"
                )
        return problems

    return []


def check_row(
    d: Dictionary, sheet: str, row: dict, reached: str | None, parent: dict | None = None
) -> list[str]:
    """All problems with one row that has reached level (or status) `reached`."""
    in_force = d.levels_in_force(sheet, reached)
    for level, cond in d.exemptions(sheet).items():
        if evaluate(cond, row, parent) is True:
            in_force.discard(level)

    problems = []
    known = {f.name for f in d.fields(sheet)}
    problems += [f"unknown field {k!r}" for k in row if k not in known]

    for f in d.fields(sheet):
        value = row.get(f.name)
        if f.required_if is not None:
            required = evaluate(f.required_if, row, parent) is True
        else:
            required = f.required_at in in_force
        if required and is_blank(value):
            problems.append(f"{f.name}: required")
        problems += [f"{f.name}: {msg}" for msg in check_value(d, f, value)]

    for check in d.checks(sheet):
        if check.get("at") and check["at"] not in in_force:
            continue
        problems += _run_check(d, check, row, parent)
    return problems
