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


def _cli(*args: str):  # type: ignore[no-untyped-def]
    return runner.invoke(app, list(args))


def _with_flows(config_path, tmp_path):  # type: ignore[no-untyped-def]
    """Point the test config at a flows file with a subscription on 2024-04-01."""
    import yaml

    flows = tmp_path / "cli_flows.csv"
    flows.write_text("date,amount,type\n2024-04-01,25000000,SUBSCRIPTION\n")
    raw = yaml.safe_load(config_path.read_text())
    raw["data"]["cash_flows_file"] = str(flows)
    config_path.write_text(yaml.safe_dump(raw))
    return ["--config", str(config_path)]


def test_run_day_approve_export_and_backtest(loaded_engine, tmp_config_path, tmp_path) -> None:  # type: ignore[no-untyped-def]
    cfg = _with_flows(tmp_config_path, tmp_path)
    r = _cli("run-day", "--date", "2024-04-01", *cfg)
    assert r.exit_code == 0, r.output
    assert "SYNTHETIC DATA" in r.output and "Futures:" in r.output and "New basket:" in r.output
    batch = r.output.split("New basket:")[1].split()[0]

    out = tmp_path / "desk.csv"
    r = _cli("export-basket", "--batch-id", batch, "--out", str(out), *cfg)
    assert r.exit_code == 0 and out.exists()
    assert _cli("export-basket", "--batch-id", "nope", "--out", str(out), *cfg).exit_code == 1

    # the basket is already APPROVED, so approving or rejecting it again is refused
    assert _cli("approve", "--batch-id", batch, "--reason", "x", *cfg).exit_code == 1
    assert _cli("reject", "--batch-id", batch, "--reason", "x", *cfg).exit_code == 1

    r = _cli("run-day", "--date", "2024-04-02", *cfg)
    assert r.exit_code == 0 and "Filled basket:" in r.output
    assert _cli("run-day", "--date", "2024-04-02", *cfg).exit_code == 1  # already processed

    r = _cli("backtest", "--start", "2024-04-01", "--end", "2024-04-30", "--lag", "2", *cfg)
    assert r.exit_code == 0, r.output
    assert "execution lag 2 day(s)" in r.output and "Cash drag" in r.output
    flows = tmp_path / "flows.csv"
    flows.write_text("date,amount,type\n2024-04-10,5000000,SUBSCRIPTION\n")
    r = _cli(
        "backtest", "--start", "2024-04-01", "--end", "2024-04-15", "--flows", str(flows), *cfg
    )
    assert r.exit_code == 0, r.output
    bad = _cli("backtest", "--start", "2024-04-06", "--end", "2024-04-07", *cfg)
    assert bad.exit_code == 1


def test_approve_and_reject_proposed(loaded_engine, tmp_config_path, tmp_path) -> None:  # type: ignore[no-untyped-def]
    from sqlalchemy import update

    from pit.db import session_scope
    from pit.db.models import Order

    cfg = _with_flows(tmp_config_path, tmp_path)
    r = _cli("run-day", "--date", "2024-04-01", *cfg)
    batch = r.output.split("New basket:")[1].split()[0]
    with session_scope(loaded_engine) as s:
        s.execute(update(Order).where(Order.batch_id == batch).values(status="PROPOSED"))
    r = _cli(
        "approve", "--batch-id", batch, "--reason", "liquidity ok", "--approver", "emilio", *cfg
    )
    assert r.exit_code == 0 and "APPROVED by emilio" in r.output
    with session_scope(loaded_engine) as s:
        s.execute(update(Order).where(Order.batch_id == batch).values(status="PROPOSED"))
    r = _cli("reject", "--batch-id", batch, "--reason", "no", *cfg)
    assert r.exit_code == 0 and "REJECTED" in r.output


def test_run_day_without_data(tmp_config_path) -> None:  # type: ignore[no-untyped-def]
    r = _cli("run-day", "--date", "2024-04-01", "--config", str(tmp_config_path))
    assert r.exit_code == 1


def test_demo_command(loaded_engine, tmp_config_path) -> None:  # type: ignore[no-untyped-def]
    r = _cli("demo", "--skip-backtest", "--config", str(tmp_config_path))
    assert r.exit_code == 0, r.output
    assert "Demo ready" in r.output and "streamlit run app/dashboard.py" in r.output
