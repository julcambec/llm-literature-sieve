"""Field dictionary and controlled vocabularies (fields.yaml, lookups.yaml).

The only module that reads field definitions. Everything else asks a Dictionary.
Loading validates the two files against each other and fails with every problem
found, so a typo in a code or field name surfaces before any data is touched.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from litsieve.config import ConfigError

TYPES = frozenset({"text", "int", "num", "date", "enum", "list", "bool"})
TOKENS = ("NR", "NA")
FORMATS = frozenset({"url", "date", "field_ref"})
OPS = frozenset({"eq", "ne", "in", "nin", "contains_any", "ge", "le", "blank", "nonblank"})
CHECK_KINDS = frozenset({"conditional", "order", "range_by_lookup"})

FIELD_KEYS = frozenset(
    {
        "description",
        "type",
        "values",
        "tokens",
        "range",
        "range_by",
        "pattern",
        "item_pattern",
        "format",
        "item_format",
        "key_values",
        "max_words",
        "required_at",
        "required_if",
        "needs_human_check",
        "filled_by",
        "pass",
        "use",
        "reserved",
        "field_group",
        "column_group",
        "for_each",
    }
)
SHEET_KEYS = frozenset({"description", "key", "file", "parent", "defaults", "fields", "exempt_if"})
LIST_KEYS = frozenset({"description", "codes", "open", "from", "extra"})
# Keys tried, in order, for the definition of a code taken from another file.
NAME_KEYS = ("text", "name", "definition", "description", "label", "looks_for")

ResolveRef = Callable[[str], list[tuple[str, Any]]]


def _as_tuple(value: Any) -> tuple:
    if value is None:
        return ()
    if isinstance(value, (list, tuple)):
        return tuple(value)
    return (value,)


@dataclass(frozen=True)
class Code:
    code: str
    definition: str
    attrs: dict[str, Any]
    entry: Any = None  # raw entry for codes taken from another file


@dataclass(frozen=True)
class Field:
    sheet: str
    name: str
    type: str
    description: str = ""
    values: str | None = None
    tokens: tuple[str, ...] = ()
    range: tuple[Any, Any] | None = None
    range_by: dict | None = None
    pattern: str | None = None
    item_pattern: str | None = None
    format: str | None = None
    item_format: str | None = None
    key_values: str | None = None
    max_words: int | None = None
    required_at: str | None = None
    required_if: dict | None = None
    needs_human_check: bool = False
    filled_by: tuple[str, ...] = ()
    passes: tuple[str, ...] = ()
    use: tuple[str, ...] = ()
    reserved: bool = False
    field_group: str | None = None
    column_group: str | None = None


class Dictionary:
    """Fields per sheet, vocabularies, levels, checks and passes."""

    def __init__(self, fields_doc: dict, lists: dict[str, dict[str, Code]]):
        self.doc = fields_doc
        self.lists = lists
        self.types: dict[str, dict] = fields_doc.get("types") or {}
        self.sheets: dict[str, dict] = fields_doc.get("sheets") or {}
        self.levels: dict[str, Any] = fields_doc.get("levels") or {}
        self.passes: dict[str, dict] = fields_doc.get("passes") or {}
        self._checks: list[dict] = fields_doc.get("checks") or []
        self._fields: dict[str, dict[str, Field]] = {}

    # ---- loading ---------------------------------------------------------

    @classmethod
    def load(cls, fields_doc: dict, lookups_doc: dict, resolve_ref: ResolveRef) -> Dictionary:
        problems: list[str] = []
        lists = _load_lists(lookups_doc, resolve_ref, problems)
        d = cls(fields_doc, lists)
        for sheet_name, sheet in d.sheets.items():
            d._fields[sheet_name] = d._load_sheet(sheet_name, sheet, problems)
        problems += d._consistency()
        if problems:
            raise ConfigError(problems)
        return d

    def _load_sheet(self, sheet_name: str, sheet: dict, problems: list[str]) -> dict[str, Field]:
        out: dict[str, Field] = {}
        unknown = set(sheet) - SHEET_KEYS
        if unknown:
            problems.append(f"sheet {sheet_name}: unknown keys {sorted(unknown)}")
        defaults = sheet.get("defaults") or {}
        for raw_name, raw_spec in (sheet.get("fields") or {}).items():
            spec = {**defaults, **(raw_spec or {})}
            where = f"{sheet_name}.{raw_name}"
            unknown = set(spec) - FIELD_KEYS
            if unknown:
                problems.append(f"{where}: unknown keys {sorted(unknown)}")
                continue
            if "for_each" in spec:
                list_name = spec.pop("for_each")
                if list_name not in self.lists:
                    problems.append(f"{where}: for_each list {list_name!r} not found")
                    continue
                for code in self.lists[list_name].values():
                    name_attr = code.entry.get("name") if isinstance(code.entry, dict) else None
                    subs = {
                        "{id}": code.code.lower(),
                        "{ID}": code.code,
                        "{name}": name_attr or code.definition,
                    }
                    name, desc = raw_name, spec.get("description", "")
                    for placeholder, value in subs.items():
                        name, desc = (
                            name.replace(placeholder, value),
                            desc.replace(placeholder, value),
                        )
                    out[name] = _make_field(
                        sheet_name, name, {**spec, "description": desc}, problems
                    )
            else:
                out[raw_name] = _make_field(sheet_name, raw_name, spec, problems)
        return out

    # ---- queries ---------------------------------------------------------

    def sheet_names(self) -> list[str]:
        return list(self.sheets)

    def fields(self, sheet: str) -> list[Field]:
        if sheet not in self._fields:
            raise ConfigError(f"Unknown sheet {sheet!r}")
        return list(self._fields[sheet].values())

    def has_field(self, sheet: str, name: str) -> bool:
        return name in self._fields.get(sheet, {})

    def field(self, sheet: str, name: str) -> Field:
        if not self.has_field(sheet, name):
            raise ConfigError(f"Unknown field {sheet}.{name}")
        return self._fields[sheet][name]

    def key(self, sheet: str) -> str:
        return self.sheets[sheet]["key"]

    def parent(self, sheet: str) -> dict | None:
        return self.sheets[sheet].get("parent")

    def codes(self, list_name: str) -> list[str]:
        if list_name not in self.lists:
            raise ConfigError(f"Unknown lookup list {list_name!r}")
        return list(self.lists[list_name])

    def code(self, list_name: str, code: str) -> Code | None:
        return self.lists.get(list_name, {}).get(code)

    def sort_codes(self, list_name: str, codes: list[str]) -> list[str]:
        """Order codes by vocabulary order; unknown codes go last, in input order."""
        order = {c: i for i, c in enumerate(self.codes(list_name))}
        return sorted(codes, key=lambda c: order.get(c, len(order)))

    def type_range(self, type_name: str) -> tuple[Any, Any] | None:
        rng = (self.types.get(type_name) or {}).get("range")
        return tuple(rng) if rng else None

    def all_levels(self, sheet: str) -> set[str]:
        spec = self.levels.get(sheet)
        if isinstance(spec, list):
            return set(spec)
        if isinstance(spec, dict):
            if spec and all(isinstance(v, list) for v in spec.values()):
                return {lvl for lvls in spec.values() for lvl in lvls}
            return set(spec)
        return set()

    def levels_in_force(self, sheet: str, reached: str | None) -> set[str]:
        """Levels in force for a row at `reached` (a level name, or a status for status maps)."""
        spec = self.levels.get(sheet)
        if isinstance(spec, list):
            return set(spec)
        if not isinstance(spec, dict) or reached is None:
            return set()
        if spec and all(isinstance(v, list) for v in spec.values()):
            return set(spec.get(reached, []))
        names = list(spec)
        if reached not in names:
            raise ConfigError(f"Unknown level {reached!r} for sheet {sheet}")
        return set(names[: names.index(reached) + 1])

    def exemptions(self, sheet: str) -> dict[str, dict]:
        return self.sheets[sheet].get("exempt_if") or {}

    def checks(self, sheet: str) -> list[dict]:
        return [c for c in self._checks if c.get("sheet") == sheet]

    # ---- consistency -----------------------------------------------------

    def _consistency(self) -> list[str]:
        p: list[str] = []
        known_lists = set(self.lists)
        for sheet_name, sheet in self.sheets.items():
            key = sheet.get("key")
            if key not in self._fields[sheet_name]:
                p.append(f"sheet {sheet_name}: key {key!r} is not one of its fields")
            parent = sheet.get("parent")
            if parent and parent.get("sheet") not in self.sheets:
                p.append(f"sheet {sheet_name}: parent sheet {parent.get('sheet')!r} not found")
            if sheet_name not in self.levels:
                p.append(f"levels: no entry for sheet {sheet_name}")
            for level, cond in (sheet.get("exempt_if") or {}).items():
                if level not in self.all_levels(sheet_name):
                    p.append(f"sheet {sheet_name}: exempt_if names unknown level {level!r}")
                p += self._check_condition(sheet_name, cond, f"sheet {sheet_name} exempt_if")
            for f in self._fields[sheet_name].values():
                p += self._check_field(f, known_lists)
        for check in self._checks:
            p += self._check_check(check)
        for name, spec in self.passes.items():
            sheet = spec.get("sheet")
            if sheet not in self.sheets:
                p.append(f"passes.{name}: sheet {sheet!r} not found")
                continue
            for g in spec.get("given") or []:
                if not self.has_field(sheet, g):
                    p.append(f"passes.{name}: given field {g!r} not in {sheet}")
            if spec.get("key") and not self.has_field(sheet, spec["key"]):
                p.append(f"passes.{name}: key {spec['key']!r} not in {sheet}")
        return p

    def _check_field(self, f: Field, known_lists: set[str]) -> list[str]:
        p, where = [], f"{f.sheet}.{f.name}"
        if f.type not in TYPES:
            return [f"{where}: unknown type {f.type!r}"]
        if f.values and f.values not in known_lists:
            p.append(f"{where}: values list {f.values!r} not found")
        if f.type == "enum" and not f.values:
            p.append(f"{where}: enum field without values")
        if f.type == "list" and not (f.values or f.item_pattern or f.item_format):
            p.append(f"{where}: list field needs values, item_pattern or item_format")
        if f.values and f.type not in ("enum", "list"):
            p.append(f"{where}: values given for a {f.type} field")
        for t in f.tokens:
            if t not in TOKENS:
                p.append(f"{where}: unknown token {t!r}")
        for fmt in (f.format, f.item_format):
            if fmt and fmt not in FORMATS:
                p.append(f"{where}: unknown format {fmt!r}")
        for pat in (f.pattern, f.item_pattern):
            if pat:
                try:
                    re.compile(pat)
                except re.error as exc:
                    p.append(f"{where}: bad pattern {pat!r}: {exc}")
        if f.key_values and f.key_values not in known_lists:
            p.append(f"{where}: key_values list {f.key_values!r} not found")
        if f.range_by:
            if f.range_by.get("lookup") not in known_lists:
                p.append(f"{where}: range_by lookup {f.range_by.get('lookup')!r} not found")
            if not self.has_field(f.sheet, f.range_by.get("field", "")):
                p.append(f"{where}: range_by field {f.range_by.get('field')!r} not found")
        if f.required_at is not None and f.required_at not in self.all_levels(f.sheet):
            p.append(f"{where}: required_at {f.required_at!r} is not a level of {f.sheet}")
        if f.required_if is not None:
            p += self._check_condition(f.sheet, f.required_if, f"{where}.required_if")
        for list_name, codes in (("filled_by", f.filled_by), ("field_use", f.use)):
            if list_name in known_lists:
                allowed = set(self.lists[list_name])
                p += [
                    f"{where}: {c!r} is not a {list_name} code" for c in codes if c not in allowed
                ]
        for pass_name in f.passes:
            if self.passes and pass_name not in self.passes:
                p.append(f"{where}: pass {pass_name!r} is not defined under passes")
        return p

    def _check_condition(self, sheet: str, cond: Any, where: str) -> list[str]:
        if not isinstance(cond, dict):
            return [f"{where}: condition must be a mapping"]
        for combinator in ("any", "all"):
            if combinator in cond:
                return [
                    msg for c in cond[combinator] for msg in self._check_condition(sheet, c, where)
                ]
        if cond.get("op") not in OPS:
            return [f"{where}: unknown op {cond.get('op')!r}"]
        if "parent_field" in cond:
            parent = self.parent(sheet)
            target_sheet, name = (parent or {}).get("sheet"), cond["parent_field"]
            if parent is None:
                return [f"{where}: parent_field used on sheet {sheet}, which has no parent"]
        else:
            target_sheet, name = sheet, cond.get("field")
        if not self.has_field(target_sheet, name):
            return [f"{where}: no field {target_sheet}.{name}"]
        f = self.field(target_sheet, name)
        if "value" not in cond or cond["op"] in ("ge", "le"):
            return []
        values = cond["value"] if isinstance(cond["value"], list) else [cond["value"]]
        if f.values:
            allowed = set(self.lists.get(f.values, {})) | set(f.tokens)
            bad = [v for v in values if v not in allowed]
        elif f.pattern or f.item_pattern:
            pattern = f.pattern or f.item_pattern
            bad = [v for v in values if v not in f.tokens and not re.fullmatch(pattern, str(v))]
        else:
            bad = []
        return [f"{where}: {v!r} is not a valid {f.name} value" for v in bad]

    def _check_check(self, check: dict) -> list[str]:
        name, sheet = check.get("name", "?"), check.get("sheet")
        where = f"checks.{name}"
        if sheet not in self.sheets:
            return [f"{where}: sheet {sheet!r} not found"]
        kind = check.get("kind")
        if kind not in CHECK_KINDS:
            return [f"{where}: unknown kind {kind!r}"]
        p = []
        if check.get("at") and check["at"] not in self.all_levels(sheet):
            p.append(f"{where}: at {check['at']!r} is not a level of {sheet}")
        if kind == "conditional":
            if "when" not in check or "then" not in check:
                p.append(f"{where}: conditional needs when and then")
            for part in ("when", "then", "else"):
                if part in check:
                    p += self._check_condition(sheet, check[part], f"{where}.{part}")
        else:
            for fname in check.get("fields") or []:
                if not self.has_field(sheet, fname):
                    p.append(f"{where}: no field {sheet}.{fname}")
        if kind == "range_by_lookup":
            if check.get("lookup") not in self.lists:
                p.append(f"{where}: lookup {check.get('lookup')!r} not found")
            if not self.has_field(sheet, check.get("key_field", "")):
                p.append(f"{where}: key_field {check.get('key_field')!r} not found")
        return p


# ---- helpers ---------------------------------------------------------------


def _make_field(sheet: str, name: str, spec: dict, problems: list[str]) -> Field:
    if "type" not in spec:
        problems.append(f"{sheet}.{name}: no type")
    rng = spec.get("range")
    passes = _as_tuple(spec.get("pass"))
    return Field(
        sheet=sheet,
        name=name,
        type=spec.get("type", "text"),
        description=" ".join(str(spec.get("description", "")).split()),
        values=spec.get("values"),
        tokens=_as_tuple(spec.get("tokens")),
        range=tuple(rng) if rng else None,
        range_by=spec.get("range_by"),
        pattern=spec.get("pattern"),
        item_pattern=spec.get("item_pattern"),
        format=spec.get("format"),
        item_format=spec.get("item_format"),
        key_values=spec.get("key_values"),
        max_words=spec.get("max_words"),
        required_at=spec.get("required_at"),
        required_if=spec.get("required_if"),
        needs_human_check=bool(spec.get("needs_human_check", False)),
        filled_by=_as_tuple(spec.get("filled_by")),
        passes=tuple(p for p in passes if p is not None),
        use=_as_tuple(spec.get("use")),
        reserved=bool(spec.get("reserved", False)),
        field_group=spec.get("field_group"),
        column_group=spec.get("column_group"),
    )


def _code_from_value(code: str, value: Any) -> Code:
    if isinstance(value, dict):
        attrs = {k: v for k, v in value.items() if k != "definition"}
        return Code(code, " ".join(str(value.get("definition", "")).split()), attrs)
    return Code(code, " ".join(str(value or "").split()), {})


def _definition_from_entry(entry: Any) -> str:
    if isinstance(entry, dict):
        for key in NAME_KEYS:
            if entry.get(key):
                return " ".join(str(entry[key]).split())
        return ""
    return " ".join(str(entry or "").split())


def _load_lists(
    doc: dict, resolve_ref: ResolveRef, problems: list[str]
) -> dict[str, dict[str, Code]]:
    out: dict[str, dict[str, Code]] = {}
    for name, spec in (doc.get("lists") or {}).items():
        spec = spec or {}
        unknown = set(spec) - LIST_KEYS
        if unknown:
            problems.append(f"lookups.{name}: unknown keys {sorted(unknown)}")
        codes: dict[str, Code] = {}
        if "from" in spec:
            try:
                for code, entry in resolve_ref(spec["from"]):
                    codes[code] = Code(code, _definition_from_entry(entry), {}, entry)
            except ConfigError as exc:
                problems += [f"lookups.{name}: {msg}" for msg in exc.problems]
            for code, value in (spec.get("extra") or {}).items():
                codes[str(code)] = _code_from_value(str(code), value)
        elif "codes" in spec:
            for code, value in (spec["codes"] or {}).items():
                codes[str(code)] = _code_from_value(str(code), value)
        else:
            problems.append(f"lookups.{name}: needs codes or from")
        out[name] = codes
    return out
