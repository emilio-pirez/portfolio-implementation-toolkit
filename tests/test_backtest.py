from datetime import date

import pytest
from sqlalchemy import Engine, func, select

from pit.config import AppConfig
from pit.db import read_audit_log, session_scope
from pit.db.models import BacktestDaily, CashFlowType
from pit.workflow import Flow, MarketData, run_backtest, save_backtest
from pit.workflow.backtest import WITH, WITHOUT

START, END = date(2024, 4, 1), date(2024, 5, 31)
FLOWS = [
    Flow(date(2024, 4, 10), 25e6, CashFlowType.SUBSCRIPTION),
    Flow(date(2024, 4, 24), 10e6, CashFlowType.REDEMPTION),
    Flow(date(2024, 5, 8), 20e6, CashFlowType.SUBSCRIPTION),
    Flow(date(2030, 1, 1), 1e6, CashFlowType.SUBSCRIPTION),  # outside the window: ignored
]


@pytest.fixture
def result(loaded_engine: Engine, cfg: AppConfig):  # type: ignore[no-untyped-def]
    md = MarketData.load(loaded_engine, cfg)
    return run_backtest(md, cfg, START, END, FLOWS)


def test_equitization_keeps_cash_invested(result) -> None:  # type: ignore[no-untyped-def]
    s = result.summary
    assert list(s.columns) == [WITH, WITHOUT, "difference"]
    assert s.loc["Avg un-equitized cash %", WITH] < s.loc["Avg un-equitized cash %", WITHOUT]
    assert s.loc["Max un-equitized cash %", WITH] < 0.006
    assert s.loc["Max un-equitized cash %", WITHOUT] > 0.04  # a 25 m subscription sits in cash
    assert s.loc["Days with post-trade HARD breach", WITH] == 0
    assert s.loc["Days with post-trade HARD breach", WITHOUT] >= 2
    assert s.loc["Avg total exposure", WITH] == pytest.approx(0.995, abs=1e-3)
    assert len(result.flows) == 3
    daily = result.daily[WITH]
    assert {"fund_return", "benchmark_return", "cash_drag"} <= set(daily.columns)
    assert daily["fund_return"].iloc[1:].notna().all()


def test_backtest_is_reproducible(loaded_engine: Engine, cfg: AppConfig, result) -> None:  # type: ignore[no-untyped-def]
    md = MarketData.load(loaded_engine, cfg)
    again = run_backtest(md, cfg, START, END, FLOWS)
    assert again.summary.equals(result.summary)


def test_backtest_saved(loaded_engine: Engine, cfg: AppConfig, result) -> None:  # type: ignore[no-untyped-def]
    n, out_dir = save_backtest(loaded_engine, cfg, result)
    with session_scope(loaded_engine) as s:
        assert s.scalar(select(func.count()).select_from(BacktestDaily)) == n
        event = read_audit_log(s, event="BACKTEST_COMPLETED").iloc[0]["details"]
    assert event["run_id"] == result.run_id and event["synthetic_data"] is True
    assert n == 2 * len(result.daily[WITH])
    from pathlib import Path

    assert (Path(out_dir) / "summary.csv").exists()


def test_backtest_needs_two_days(loaded_engine: Engine, cfg: AppConfig) -> None:
    md = MarketData.load(loaded_engine, cfg)
    with pytest.raises(ValueError, match="two trading days"):
        run_backtest(md, cfg, date(2024, 4, 6), date(2024, 4, 7), [])
