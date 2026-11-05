"""Read and write workbook sheets as rows keyed by the sheet's key field.

Only cell values are touched here; drop-downs, filters and highlighting are build.py's
job. Columns are found by header name, so column order in the sheet does not matter,
and columns this module does not know about are left alone.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from openpyxl import Workbook, load_workbook
from openpyxl.worksheet.worksheet import Worksheet

from litsieve.dictionary import Field

EXCEL_CELL_LIMIT = 32_767
LIST_SEPARATOR = ";"


class WorkbookError(Exception):
    """The workbook cannot be read or written."""


def to_cell(f: Field, value: Any) -> Any:
    """In-memory value -> cell value (lists joined with ';', bools as yes/no)."""
    if value is None:
        return None
    if f.type == "list" and isinstance(value, list):
        return LIST_SEPARATOR.join(str(v) for v in value) if value else None
    if f.type == "bool" and isinstance(value, bool):
        return "yes" if value else "no"
    return value


def from_cell(f: Field, value: Any) -> Any:
    """Cell value -> in-memory value; the inverse of to_cell."""
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    if f.type == "list":
        return [v for v in str(value).split(LIST_SEPARATOR) if v]
    if f.type == "bool" and isinstance(value, str) and value.strip().lower() in ("yes", "no"):
        return value.strip().lower() == "yes"
    if f.type == "int" and isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str):
        return value.strip()
    return value


def cell_text(value: Any) -> str:
    """Normalised text of a cell value, for comparisons."""
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    return str(value).strip()


@dataclass
class UpsertReport:
    added: list[str] = field(default_factory=list)
    updated: list[str] = field(default_factory=list)
    unchanged: list[str] = field(default_factory=list)
    differs: dict[str, list[str]] = field(default_factory=dict)  # key -> fields; not written
    frozen: dict[str, str] = field(default_factory=dict)  # key -> reason; not written

    @property
    def changed(self) -> bool:
        return bool(self.added or self.updated)


def _header(ws: Worksheet) -> list[str]:
    values = [c.value for c in ws[1]] if ws.max_row >= 1 else []
    while values and values[-1] is None:
        values.pop()
    return [cell_text(v) for v in values]


def _set(ws: Worksheet, row: int, col: int, value: Any) -> None:
    if isinstance(value, str) and len(value) > EXCEL_CELL_LIMIT:
        raise WorkbookError(
            f"Value for {ws.title}!R{row}C{col} exceeds Excel's {EXCEL_CELL_LIMIT}-character limit"
        )
    cell = ws.cell(row=row, column=col, value=value)
    if isinstance(value, str) and value.startswith("="):
        cell.data_type = "s"  # text, never a formula


def read_rows(path: Path, sheet: str, fields: Iterable[Field]) -> list[dict[str, Any]]:
    """All rows of a sheet as in-memory dicts; [] if the file or sheet does not exist."""
    if not path.is_file():
        return []
    wb = load_workbook(path, read_only=True, data_only=True)
    if sheet not in wb.sheetnames:
        return []
    by_name = {f.name: f for f in fields}
    rows = wb[sheet].iter_rows(values_only=True)
    header = [cell_text(v) for v in next(rows, [])]
    out = []
    for values in rows:
        if all(v is None for v in values):
            continue
        out.append(
            {
                name: from_cell(by_name[name], v)
                for name, v in zip(header, values)
                if name in by_name
            }
        )
    wb.close()
    return out


def upsert_rows(
    path: Path,
    sheet: str,
    fields: list[Field],
    key: str,
    rows: list[dict[str, Any]],
    compare: list[str],
    frozen_reason: Callable[[dict[str, Any]], str | None] = lambda row: None,
    update: bool = False,
) -> UpsertReport:
    """Add new rows; for existing rows, compare `compare` fields and rewrite them only
    if `update` is set and `frozen_reason` returns None for the existing row.

    The workbook and sheet are created if missing. The file is written atomically.
    """
    report = UpsertReport()
    by_name = {f.name: f for f in fields}
    created = not path.is_file()
    try:
        wb = Workbook() if created else load_workbook(path)
    except Exception as exc:  # openpyxl raises several types for unreadable files
        raise WorkbookError(f"Cannot open {path}: {exc}") from exc
    if created:
        wb.remove(wb.active)

    new_sheet = sheet not in wb.sheetnames
    ws = wb.create_sheet(sheet) if new_sheet else wb[sheet]
    header = _header(ws)
    header_changed = False
    for f in fields:
        if f.name not in header:
            header.append(f.name)
            _set(ws, 1, len(header), f.name)
            header_changed = True
    col = {name: i + 1 for i, name in enumerate(header)}
    if new_sheet:
        ws.freeze_panes = "B2"

    existing: dict[str, int] = {}
    for r in range(2, ws.max_row + 1):
        k = cell_text(ws.cell(row=r, column=col[key]).value)
        if k:
            existing[k] = r
    next_row = max(existing.values(), default=1) + 1

    for row in rows:
        k = cell_text(row[key])
        cells = {name: to_cell(by_name[name], v) for name, v in row.items() if name in by_name}
        if k in existing:
            r = existing[k]
            current = {
                name: from_cell(by_name[name], ws.cell(row=r, column=col[name]).value)
                for name in col
                if name in by_name
            }
            diffs = [
                name
                for name in compare
                if cell_text(ws.cell(row=r, column=col[name]).value) != cell_text(cells.get(name))
            ]
            reason = frozen_reason(current)
            if not diffs:
                report.unchanged.append(k)
            elif reason:
                report.frozen[k] = f"{reason}; differs in {', '.join(diffs)}"
            elif update:
                for name in diffs:
                    _set(ws, r, col[name], cells.get(name))
                report.updated.append(k)
            else:
                report.differs[k] = diffs
        else:
            for name, value in cells.items():
                _set(ws, next_row, col[name], value)
            existing[k] = next_row
            next_row += 1
            report.added.append(k)

    if report.changed or header_changed or created or new_sheet:
        _save(wb, path)
    return report


def ensure_writable(path: Path) -> None:
    """Fail early if the workbook exists but is locked (open in Excel on Windows)."""
    if not path.is_file():
        return
    try:
        with path.open("r+b"):
            pass
    except PermissionError as exc:
        raise WorkbookError(f"Cannot write {path}; close it in Excel and try again") from exc


def find_row(
    path: Path, sheet: str, fields: list[Field], key: str, value: str
) -> dict[str, Any] | None:
    """The row whose key equals `value`, or None."""
    for row in read_rows(path, sheet, fields):
        if cell_text(row.get(key)) == value:
            return row
    return None


def update_row(
    path: Path, sheet: str, fields: list[Field], key: str, value: str, values: dict[str, Any]
) -> None:
    """Set `values` on the existing row whose key equals `value`; other cells are kept."""
    if not path.is_file():
        raise WorkbookError(f"Workbook not found: {path}")
    wb = load_workbook(path)
    if sheet not in wb.sheetnames:
        raise WorkbookError(f"{path.name} has no {sheet} sheet")
    ws = wb[sheet]
    by_name = {f.name: f for f in fields}
    header = _header(ws)
    for name in values:
        if name not in by_name:
            raise WorkbookError(f"Unknown field {sheet}.{name}")
        if name not in header:
            header.append(name)
            _set(ws, 1, len(header), name)
    col = {name: i + 1 for i, name in enumerate(header)}
    if key not in col:
        raise WorkbookError(f"{sheet} has no {key} column")
    target = next(
        (
            r
            for r in range(2, ws.max_row + 1)
            if cell_text(ws.cell(row=r, column=col[key]).value) == value
        ),
        None,
    )
    if target is None:
        raise WorkbookError(f"{sheet} has no row {value}")
    for name, v in values.items():
        _set(ws, target, col[name], to_cell(by_name[name], v))
    _save(wb, path)


def _save(wb: Workbook, path: Path) -> None:
    tmp = path.with_name(f".{path.stem}.tmp{path.suffix}")
    try:
        wb.save(tmp)
        os.replace(tmp, path)
    except PermissionError as exc:
        tmp.unlink(missing_ok=True)
        raise WorkbookError(f"Cannot write {path}; close it in Excel and try again") from exc
