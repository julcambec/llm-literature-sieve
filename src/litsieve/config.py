"""Project configuration: project.yaml and the files it points to.

Path rules for project.yaml:

- `dictionary.fields`, `dictionary.lookups` and `search_protocol` are relative to the
  project root (the directory litsieve runs from, or --root).
- `paths.data_dir` and `paths.prompts` are relative to the project root.
- Every other entry under `paths` is relative to the data folder.

Cross-file references have the form `file#key`, e.g. `project.yaml#questions` or
`runs/register.csv#run_id`. YAML files are matched by file name among the loaded
config files; CSV files are read from the data folder, and a missing CSV counts as
empty (the run register does not exist before the first run).
"""

from __future__ import annotations

import csv
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

import yaml

if TYPE_CHECKING:
    from litsieve.dictionary import Dictionary

DEFAULT_CONFIG = Path("config") / "project.yaml"
ROOT_RELATIVE_PATHS = frozenset({"data_dir", "prompts"})


class ConfigError(Exception):
    """A configuration file is missing, malformed or inconsistent."""

    def __init__(self, problems: str | list[str]):
        self.problems = [problems] if isinstance(problems, str) else list(problems)
        super().__init__("\n".join(self.problems))


def load_yaml(path: Path) -> dict[str, Any]:
    """Read one YAML file that must hold a mapping."""
    if not path.is_file():
        raise ConfigError(f"Missing file: {path}")
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ConfigError(f"{path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ConfigError(f"{path}: expected a mapping at the top level")
    return data


def read_dotenv(path: Path) -> dict[str, str]:
    """KEY=VALUE lines of a .env file; comments, blank lines and `export` are allowed."""
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.removeprefix("export ").partition("=")
        values[key.strip()] = value.strip().strip("'\"")
    return values


@dataclass
class Project:
    """A loaded project: its config files, paths and dictionary."""

    root: Path
    config_path: Path
    config: dict[str, Any]
    protocol_path: Path
    protocol: dict[str, Any]
    dictionary: Dictionary | None = None
    _dotenv: dict[str, str] | None = field(default=None, init=False, repr=False)

    def env(self, name: str) -> str | None:
        """A setting from the environment, else from the project root's .env file."""
        if os.environ.get(name):
            return os.environ[name]
        if self._dotenv is None:
            self._dotenv = read_dotenv(self.root / ".env")
        return self._dotenv.get(name) or None

    @property
    def data_dir(self) -> Path:
        return self.path("data_dir")

    def path(self, key: str) -> Path:
        """Absolute path for an entry under `paths` in project.yaml."""
        paths = self.config.get("paths") or {}
        if key not in paths:
            raise ConfigError(f"{self.config_path}: paths.{key} is not set")
        base = self.root if key in ROOT_RELATIVE_PATHS else self.data_dir
        return (base / str(paths[key]).strip()).resolve()

    def resolve_ref(self, ref: str) -> list[tuple[str, Any]]:
        """Resolve `file#key` to (code, entry) pairs, in file order."""
        file_part, _, key = ref.partition("#")
        if not file_part or not key:
            raise ConfigError(f"Bad reference {ref!r}: expected 'file#key'")

        if file_part.endswith(".csv"):
            path = self.data_dir / file_part
            if not path.is_file():
                return []
            with path.open(newline="", encoding="utf-8") as fh:
                reader = csv.DictReader(fh)
                if reader.fieldnames is None:
                    return []
                if key not in reader.fieldnames:
                    raise ConfigError(f"Reference {ref!r}: {path} has no column {key!r}")
                return [(row[key].strip(), row) for row in reader if (row.get(key) or "").strip()]

        docs = {self.config_path.name: self.config, self.protocol_path.name: self.protocol}
        if file_part not in docs:
            raise ConfigError(
                f"Reference {ref!r}: unknown file {file_part!r} (known: {', '.join(sorted(docs))})"
            )
        node: Any = docs[file_part]
        for part in key.split("."):
            if not isinstance(node, dict) or part not in node:
                raise ConfigError(f"Reference {ref!r}: {part!r} not found")
            node = node[part]
        if not isinstance(node, dict):
            raise ConfigError(f"Reference {ref!r} must point to a mapping")
        return [(str(code), entry) for code, entry in node.items()]


def load_project(root: Path | None = None, config: Path | None = None) -> Project:
    """Load and validate a project. Raises ConfigError listing every problem found."""
    from litsieve.dictionary import Dictionary

    root = Path(root or Path.cwd()).resolve()
    config_path = (root / (config or DEFAULT_CONFIG)).resolve()
    cfg = load_yaml(config_path)

    problems = []
    for key in ("paths", "dictionary", "search_protocol"):
        if key not in cfg:
            problems.append(f"{config_path}: missing top-level key {key!r}")
    if "data_dir" not in (cfg.get("paths") or {}):
        problems.append(f"{config_path}: paths.data_dir is not set")
    for key in ("fields", "lookups"):
        if key not in (cfg.get("dictionary") or {}):
            problems.append(f"{config_path}: dictionary.{key} is not set")
    if problems:
        raise ConfigError(problems)

    protocol_path = (root / cfg["search_protocol"]).resolve()
    project = Project(
        root=root,
        config_path=config_path,
        config=cfg,
        protocol_path=protocol_path,
        protocol=load_yaml(protocol_path),
    )
    fields_doc = load_yaml((root / cfg["dictionary"]["fields"]).resolve())
    lookups_doc = load_yaml((root / cfg["dictionary"]["lookups"]).resolve())
    project.dictionary = Dictionary.load(fields_doc, lookups_doc, project.resolve_ref)
    _check_project_rules(project)
    return project


def _check_project_rules(project: Project) -> None:
    """Check that tier_rule and coverage_rules in project.yaml name real fields and codes."""
    d = project.dictionary
    cfg = project.config
    problems = []

    for i, rule in enumerate((cfg.get("tier_rule") or {}).get("extended_if_any") or []):
        where = f"project.yaml tier_rule.extended_if_any[{i}]"
        if "field_group" in rule:
            if not any(f.field_group == rule["field_group"] for f in d.fields("records")):
                problems.append(f"{where}: no records field in group {rule['field_group']!r}")
        elif not d.has_field("records", rule.get("field", "")):
            problems.append(f"{where}: no records field {rule.get('field')!r}")
        else:
            field = d.field("records", rule["field"])
            values = rule.get("value")
            if field.values and isinstance(values, list):
                allowed = set(d.codes(field.values))
                problems += [
                    f"{where}: {v!r} is not a {field.values} code"
                    for v in values
                    if v not in allowed
                ]

    for rule in cfg.get("coverage_rules") or []:
        for side in ("parent", "child"):
            spec = rule.get(side) or {}
            if not d.has_field(spec.get("sheet", ""), spec.get("field", "")):
                problems.append(
                    f"project.yaml coverage rule {rule.get('name')!r}: no field "
                    f"{spec.get('sheet')}.{spec.get('field')}"
                )

    if problems:
        raise ConfigError(problems)
