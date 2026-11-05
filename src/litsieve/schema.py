"""
JSON Schemas for LLM passes, generated from the field dictionary.

A pass's schema has the pass's key plus every field whose `pass` names it.
Fields with a `required_at` level are required; the others may be null. Codes come
from the lookups, plus the field's tokens. Rules the schema cannot express (word
caps, cross-field checks) are applied afterwards by rules.check_row.
"""

from __future__ import annotations

import copy
from typing import Any

from litsieve.config import ConfigError
from litsieve.dictionary import Dictionary, Field

JSON_TYPES = {
    "text": "string",
    "int": "integer",
    "num": "number",
    "date": "string",
    "bool": "boolean",
}


def field_schema(d: Dictionary, f: Field, nullable: bool) -> dict[str, Any]:
    tokens = list(f.tokens)
    if f.type == "enum":
        codes = d.codes(f.values)
        schema: dict[str, Any] = {
            "type": "string",
            "enum": codes + [t for t in tokens if t not in codes],
        }
    elif f.type == "list":
        item: dict[str, Any] = {"type": "string"}
        if f.values:
            item["enum"] = d.codes(f.values)
        schema = {"type": "array", "items": item, "uniqueItems": True}
        if tokens:
            schema = {
                "anyOf": [schema, {"type": "array", "items": {"enum": tokens}, "maxItems": 1}]
            }
    else:
        schema = {"type": JSON_TYPES[f.type]}
        if f.type in ("int", "num") and f.range:
            low, high = f.range
            if low is not None:
                schema["minimum"] = low
            if high is not None:
                schema["maximum"] = high
        if tokens and f.type != "text":
            schema = {"anyOf": [schema, {"enum": tokens}]}
    if nullable:
        schema = {"anyOf": [schema, {"type": "null"}]}
    schema["description"] = f.description
    return schema


def pass_fields(d: Dictionary, pass_name: str) -> tuple[str, str, list[Field]]:
    """(sheet, key, fields returned by the pass)."""
    spec = d.passes.get(pass_name)
    if not spec:
        raise ConfigError(f"fields.yaml: passes.{pass_name} is not defined")
    sheet, key = spec["sheet"], spec["key"]
    return sheet, key, [f for f in d.fields(sheet) if pass_name in f.passes]


def pass_schema(d: Dictionary, pass_name: str) -> dict[str, Any]:
    sheet, key, fields = pass_fields(d, pass_name)
    properties = {key: {"type": "string", "description": d.field(sheet, key).description}}
    required = [key]
    for f in fields:
        is_required = f.required_at is not None and f.required_if is None
        properties[f.name] = field_schema(d, f, nullable=not is_required)
        if is_required:
            required.append(f.name)
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "properties": properties,
        "required": required,
        "additionalProperties": False,
    }


def without_descriptions(schema: dict[str, Any]) -> dict[str, Any]:
    """A copy without `description` keys, for showing compactly in a prompt."""

    def strip(node: Any) -> Any:
        if isinstance(node, dict):
            return {k: strip(v) for k, v in node.items() if k not in ("description", "$schema")}
        if isinstance(node, list):
            return [strip(v) for v in node]
        return node

    return strip(copy.deepcopy(schema))
