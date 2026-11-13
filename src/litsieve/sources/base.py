"""Shared types for runners and importers."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

Hit = dict[str, Any]
Importer = Callable[[Path], list[Hit]]


class ImportFormatError(ValueError):
    """An export file is not in the format its importer expects."""


class RunnerError(RuntimeError):
    """A scripted search failed."""


@dataclass
class RunResult:
    hits: list[Hit]
    total: int  # the source's own hit count, even when the export is capped
    raw: Any  # what the source returned, saved under raw/ for reproducibility
    params: dict[str, Any] = field(default_factory=dict)  # request parameters, without secrets
