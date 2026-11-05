"""
LLM runs: the run register (runs/register.csv) and one folder per run.

A run folder holds manifest.json (what the run covers and how), the rendered
prompts, and the replies. On the manual_chat backend, replies are pasted into
replies.txt (or saved as files under replies/) in any order; each reply is one JSON
object carrying its own key, so text around it and code fences do no harm.
"""

from __future__ import annotations

import csv
import json
import re
from pathlib import Path
from typing import Any

from litsieve.config import Project

SHEET = "run_register"
RUN_ID = re.compile(r"^RUN(\d{2,})$")
REPLY_SUFFIXES = {".txt", ".json", ".md", ".jsonl"}


def register_path(project: Project) -> Path:
    return project.path("run_register")


def read_register(project: Project) -> list[dict[str, str]]:
    path = register_path(project)
    if not path.is_file():
        return []
    with path.open(encoding="utf-8", newline="") as fh:
        return list(csv.DictReader(fh))


def next_run_id(project: Project) -> str:
    numbers = [
        int(m.group(1)) for r in read_register(project) if (m := RUN_ID.match(r.get("run_id", "")))
    ]
    runs = project.path("runs")
    if runs.is_dir():
        numbers += [
            int(m.group(1)) for p in runs.iterdir() if p.is_dir() and (m := RUN_ID.match(p.name))
        ]
    return f"RUN{max(numbers, default=0) + 1:02d}"


def append_register(project: Project, row: dict[str, Any]) -> None:
    columns = [f.name for f in project.dictionary.fields(SHEET)]
    path = register_path(project)
    path.parent.mkdir(parents=True, exist_ok=True)
    new = not path.is_file()
    with path.open("a", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=columns)
        if new:
            writer.writeheader()
        writer.writerow({c: row.get(c, "") for c in columns})


def run_dir(project: Project, run_id: str) -> Path:
    return project.path("runs") / run_id


def write_manifest(folder: Path, manifest: dict[str, Any]) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "manifest.json").write_text(
        json.dumps(manifest, indent=1, ensure_ascii=False), encoding="utf-8"
    )


def read_manifest(folder: Path) -> dict[str, Any]:
    path = folder / "manifest.json"
    if not path.is_file():
        raise FileNotFoundError(f"No manifest at {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def manifests(project: Project) -> list[dict[str, Any]]:
    runs = project.path("runs")
    if not runs.is_dir():
        return []
    out = []
    for folder in sorted(p for p in runs.iterdir() if p.is_dir() and RUN_ID.match(p.name)):
        if (folder / "manifest.json").is_file():
            out.append(read_manifest(folder))
    return out


def extract_json_objects(text: str) -> list[Any]:
    """Every top-level JSON object in free text, in order."""
    decoder = json.JSONDecoder()
    objects, pos = [], 0
    while (start := text.find("{", pos)) != -1:
        try:
            obj, end = decoder.raw_decode(text, start)
        except json.JSONDecodeError:
            pos = start + 1
            continue
        objects.append(obj)
        pos = end
    return objects


def read_replies(folder: Path) -> list[Any]:
    """JSON objects from replies.txt and any files under replies/, in file order."""
    files = []
    if (folder / "replies.txt").is_file():
        files.append(folder / "replies.txt")
    if (folder / "replies").is_dir():
        files += sorted(p for p in (folder / "replies").iterdir() if p.suffix in REPLY_SUFFIXES)
    objects: list[Any] = []
    for path in files:
        objects += extract_json_objects(path.read_text(encoding="utf-8-sig"))
    return objects
