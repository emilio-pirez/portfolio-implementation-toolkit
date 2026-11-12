from typer.testing import CliRunner

from pit import __version__
from pit.cli import app

runner = CliRunner()


def test_version_command() -> None:
    result = runner.invoke(app, ["version"])
    assert result.exit_code == 0
    assert __version__ in result.output


def test_show_config_command() -> None:
    result = runner.invoke(app, ["show-config"])
    assert result.exit_code == 0
    assert "ES x50, MES x5" in result.output
    assert "500,000,000" in result.output
