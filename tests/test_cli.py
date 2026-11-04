from typer.testing import CliRunner

from litsieve import __version__
from litsieve.cli import app


def test_version_command():
    result = CliRunner().invoke(app, ["version"])
    assert result.exit_code == 0
    assert __version__ in result.stdout
