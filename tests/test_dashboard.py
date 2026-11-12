"""Dashboard: query layer, demo builder and a smoke test of every page."""

from datetime import date
from pathlib import Path

import pytest
from sqlalchemy import Engine

from pit.config import AppConfig
from pit.dashboard import queries as q
from pit.db import make_engine
from pit.demo import build_demo

DAYS = (date(2024, 4, 1), date(2024, 4, 5))
BACKTEST = (date(2024, 4, 1), date(2024, 4, 30))
PAGES = [
    "Overview",
    "Positions vs. Model",
    "Order Blotter",
    "Compliance",
    "Cash & Equitization",
    "Audit Log",
]


@pytest.fixture
def demo_engine(loaded_engine: Engine, cfg: AppConfig, tmp_path: Path) -> Engine:
    flows = tmp_path / "flows.csv"
    flows.write_text("date,amount,type\n2024-04-01,25000000,SUBSCRIPTION\n")
    cfg2 = cfg.model_copy(
        update={"data": cfg.data.model_copy(update={"cash_flows_file": str(flows)})}
    )
    steps: list[str] = []
    report = build_demo(loaded_engine, cfg2, lambda m, f: steps.append(m), DAYS, BACKTEST)
    assert report.steps == ["processed 5 days", "backtest over 22 days"]
    assert steps[-1] == "Done"
    return loaded_engine


def test_status_queries(demo_engine: Engine) -> None:
    assert q.is_synthetic(demo_engine)
    assert q.has_positions(demo_engine) and q.has_backtest(demo_engine)
    assert q.position_dates(demo_engine)[0] == date(2024, 4, 1)
    assert len(q.position_dates(demo_engine)) == 5


def test_portfolio_history_and_holdings(demo_engine: Engine, cfg: AppConfig) -> None:
    hist = q.portfolio_history(demo_engine, cfg)
    assert len(hist) == 5
    first = hist.iloc[0]
    assert first["nav"] == pytest.approx(cfg.fund.initial_nav + 25e6, rel=1e-6)
    assert first["futures_notional"] > 20e6  # subscription equitized on day one
    assert first["net_cash_pct"] == pytest.approx(0.005, abs=1e-3)
    assert (hist["exposure"] - 0.995).abs().max() < 2e-3

    h = q.holdings(demo_engine, cfg, date(2024, 4, 5))
    assert len(h) >= 55 and h["weight"].sum() == pytest.approx(0.995, abs=0.01)
    assert {"vs_target", "vs_benchmark", "sector"} <= set(h.columns)
    sec = q.sector_weights(demo_engine, cfg, date(2024, 4, 5))
    assert sec["benchmark"].sum() == pytest.approx(1.0)
    assert sec["target"].sum() == pytest.approx(0.995)
    fut = q.futures_positions(demo_engine, cfg, date(2024, 4, 1))
    assert set(fut["contract"]) <= {"ESM4", "MESM4"} and fut["notional"].sum() > 20e6


def test_orders_compliance_and_alerts(demo_engine: Engine) -> None:
    b = q.baskets(demo_engine)
    assert len(b) >= 1 and b.iloc[0]["date"] >= b.iloc[-1]["date"]
    first = b.iloc[-1]["batch_id"]
    assert len(q.basket_orders(demo_engine, first)) == b.iloc[-1]["orders"]
    assert len(q.basket_checks(demo_engine, first)) == 10
    hist = q.compliance_history(demo_engine)
    assert set(hist["kind"]) == {"pre-trade", "post-trade"}
    assert hist["date"].notna().all()
    counts = q.breach_counts(demo_engine)
    assert list(counts.columns) == ["date", "kind", "severity", "breaches"]
    alerts = q.active_alerts(demo_engine)
    assert list(alerts.columns) == ["type", "detail"]


def test_backtest_and_audit_queries(demo_engine: Engine) -> None:
    runs = q.backtest_runs(demo_engine)
    assert len(runs) == 1
    daily = q.backtest_daily(demo_engine, runs.iloc[0]["run_id"])
    assert set(daily["scenario"]) == {"with_equitization", "without_equitization"}
    assert "cum_cash_drag_bp" in daily.columns
    assert "Cash drag, expected (bp/yr)" in runs.iloc[0]["summary"].index

    types = q.event_types(demo_engine)
    assert {"DAY_STARTED", "BASKET_GENERATED", "BACKTEST_COMPLETED"} <= set(types)
    only = q.audit_events(demo_engine, events=["FLOW_RECEIVED"])
    assert len(only) == 1 and "SUBSCRIPTION" in only.iloc[0]["details"]
    today = date.today()
    assert len(q.audit_events(demo_engine, start=today, end=today)) > 0
    assert q.audit_events(demo_engine, text="no-such-text").empty


def test_empty_database_queries(engine: Engine, cfg: AppConfig) -> None:
    assert not q.has_positions(engine) and not q.is_synthetic(engine)
    assert q.portfolio_history(engine, cfg).empty
    assert q.baskets(engine).empty
    assert q.backtest_runs(engine).empty
    assert q.backtest_daily(engine, "x").empty
    assert q.breach_counts(engine).empty
    assert q.active_alerts(engine).empty
    assert q.compliance_history(engine).empty


def test_build_demo_from_empty_database(cfg: AppConfig, tmp_path: Path) -> None:
    """Fresh clone: no prices at all -> offline data is loaded, then days are run."""
    eng = make_engine(f"sqlite:///{tmp_path / 'fresh.db'}")
    report = build_demo(
        eng, cfg, days=(date(2026, 3, 16), date(2026, 3, 17)), backtest=None, use_snapshot=False
    )
    assert report.steps == ["loaded offline data", "processed 2 days"]
    again = build_demo(eng, cfg, days=(date(2026, 3, 16), date(2026, 3, 17)), backtest=None)
    assert again.steps == []  # idempotent: existing data is never overwritten
    eng.dispose()


def test_every_page_renders(demo_engine: Engine, tmp_config_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from streamlit.testing.v1 import AppTest

    from pit.config import PROJECT_ROOT

    monkeypatch.setenv("PIT_CONFIG_PATH", str(tmp_config_path))
    at = AppTest.from_file(str(PROJECT_ROOT / "app" / "dashboard.py"), default_timeout=60).run()
    assert not at.exception
    assert any("SYNTHETIC DATA" in w.value for w in at.warning)
    for page in PAGES:
        at.sidebar.radio(key="page").set_value(page).run()
        assert not at.exception, f"{page}: {[e.value for e in at.exception]}"
        assert at.title[0].value == page
    at.sidebar.radio(key="page").set_value("Overview").run()
    labels = [m.label for m in at.metric]
    assert {"NAV (USD)", "Total exposure", "Net cash"} <= set(labels)


# --------------------------------------------------------------------------- demo snapshot


def test_committed_snapshot_matches_schema() -> None:
    """data/demo/demo.db must stay compatible with the models (regenerate with
    `pit demo --snapshot-out data/demo/demo.db` after a schema change)."""
    from sqlalchemy import inspect

    from pit.config import PROJECT_ROOT
    from pit.db.models import Base

    snap = PROJECT_ROOT / "data" / "demo" / "demo.db"
    eng = make_engine(f"sqlite:///{snap}")
    insp = inspect(eng)
    for table in Base.metadata.sorted_tables:
        cols = {c["name"] for c in insp.get_columns(table.name)}
        assert cols == {c.name for c in table.columns}, table.name
    assert q.is_synthetic(eng) and q.has_positions(eng) and q.has_backtest(eng)
    eng.dispose()


def test_empty_database_restores_snapshot(cfg: AppConfig, tmp_path: Path) -> None:
    from pit.config import PROJECT_ROOT
    from pit.db import read_audit_log, session_scope

    snap = str(PROJECT_ROOT / "data" / "demo" / "demo.db")
    cfg2 = cfg.model_copy(update={"data": cfg.data.model_copy(update={"demo_snapshot_file": snap})})
    eng = make_engine(f"sqlite:///{tmp_path / 'cloud.db'}")
    report = build_demo(eng, cfg2)
    assert report.steps == ["restored demo snapshot"]
    assert q.has_positions(eng) and q.has_backtest(eng)
    with session_scope(eng) as s:
        assert read_audit_log(s).iloc[0]["event"] == "DEMO_RESTORED"
    eng.dispose()


def test_write_snapshot(cfg: AppConfig, tmp_path: Path) -> None:
    from pit.demo import write_snapshot

    out = tmp_path / "snap" / "demo.db"
    report = write_snapshot(cfg, out, days=(date(2026, 3, 16), date(2026, 3, 17)), backtest=None)
    assert out.exists() and report.steps[-1].startswith("snapshot written")
    eng = make_engine(f"sqlite:///{out}")
    assert len(q.position_dates(eng)) == 2
    eng.dispose()


def test_config_found_from_working_directory(monkeypatch, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    """Non-editable installs: the package is outside the repo, so config.yaml is
    looked up in the working directory (the repo root on Streamlit Cloud)."""
    import pit.config as config_module

    monkeypatch.delenv(config_module.CONFIG_ENV_VAR, raising=False)
    monkeypatch.setattr(config_module, "DEFAULT_CONFIG_PATH", tmp_path / "missing.yaml")
    monkeypatch.chdir(tmp_path)
    assert config_module.resolve_config_path() == tmp_path / "config.yaml"
