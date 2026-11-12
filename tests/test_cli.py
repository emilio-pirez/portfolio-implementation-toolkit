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


def test_load_data_offline_command(tmp_config_path) -> None:  # type: ignore[no-untyped-def]
    result = runner.invoke(
        app,
        [
            "load-data",
            "--offline",
            "--start",
            "2024-01-02",
            "--end",
            "2024-02-02",
            "--config",
            str(tmp_config_path),
        ],
    )
    assert result.exit_code == 0, result.output
    assert "SYNTHETIC (offline)" in result.output
    assert "Target weights:    55" in result.output


def test_load_data_bad_dates_exit_code(tmp_config_path) -> None:  # type: ignore[no-untyped-def]
    result = runner.invoke(
        app,
        [
            "load-data",
            "--offline",
            "--start",
            "2024-03-01",
            "--end",
            "2024-02-01",
            "--config",
            str(tmp_config_path),
        ],
    )
    assert result.exit_code == 1


def test_db_schema_command() -> None:
    result = runner.invoke(app, ["db-schema"])
    assert result.exit_code == 0
    assert "CREATE TABLE audit_log" in result.output
