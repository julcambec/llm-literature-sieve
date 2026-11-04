import shutil
from pathlib import Path

import pytest

EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "minimal"


@pytest.fixture
def example(tmp_path: Path) -> Path:
    """A writable copy of the example project; returns its root."""
    root = tmp_path / "project"
    shutil.copytree(EXAMPLE, root)
    return root
