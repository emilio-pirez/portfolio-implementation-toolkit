"""step_day: the daily process, tested on a small offline market."""

from dataclasses import replace
from datetime import date

import pandas as pd
import pytest
from sqlalchemy import Engine

from pit.config import AppConfig
from pit.db.models import CashFlowType, OrderStatus
from pit.portfolio.state import FuturesPosition
from pit.workflow import MarketData, step_day
from pit.workflow.day import Event, PendingBasket, ReviewRecorded, StatusChange
from pit.workflow.flows import Flow, by_date, random_flows, read_cash_flows

D0, D1, D2 = date(2024, 4, 1), date(2024, 4, 2), date(2024, 4, 3)


@pytest.fixture
def md(loaded_engine: Engine, cfg: AppConfig) -> MarketData:
    return MarketData.load(loaded_engine, cfg)


def _names(res) -> list[str]:  # type: ignore[no-untyped-def]
    out = []
    for a in res.actions:
        if isinstance(a, Event):
            out.append(a.name)
        elif isinstance(a, StatusChange):
            out.append(f"->{a.status.value}")
        elif isinstance(a, ReviewRecorded):
            out.append(f"REVIEW:{a.review.status.value}")
    return out


def _sub(d: date, amount: float = 25e6) -> Flow:
    return Flow(d, amount, CashFlowType.SUBSCRIPTION)


def test_market_data(md: MarketData) -> None:
    assert md.is_synthetic
    assert md.is_trading_day(D0) and not md.is_trading_day(date(2024, 4, 6))
    assert md.trading_days_between(D0, D2) == 2
    assert md.adv(D0).gt(0).all()
    assert len(md.target_weights(D0)) == 55
    with pytest.raises(LookupError):
        md.snapshot(date(2024, 4, 6))
    with pytest.raises(LookupError):
        md.target_weights(date(2020, 1, 1))
    with pytest.raises(LookupError):
        md.rate(date(2020, 1, 1))
    te, beta = md.compliance_inputs(D0).risk_model(  # type: ignore[misc]
        step_day(None, md, D0, [], None, md.cfg).state
    )
    assert 0 < te < 0.06 and 0.85 < beta < 1.15


def test_subscription_equitized_then_basket_filled_next_day(md: MarketData, cfg: AppConfig) -> None:
    day0 = step_day(None, md, D0, [_sub(D0)], None, cfg)
    assert _names(day0)[:3] == ["PORTFOLIO_INITIALIZED", "FLOW_RECEIVED", "FUTURES_EQUITIZED"]
    assert day0.state.contracts("ES") > 70
    assert day0.state.exposure == pytest.approx(0.995, abs=1e-3)
    assert day0.review is not None and day0.review.status is OrderStatus.APPROVED
    assert day0.pending is not None and day0.filled is None

    day1 = step_day(day0.state, md, D1, [], day0.pending, cfg)
    names = _names(day1)
    assert names[:4] == ["MARKED_TO_MARKET", "BASKET_EXECUTED", "->FILLED", names[3]]
    assert day1.filled is not None and day1.filled.batch_id == day0.pending.basket.batch_id
    assert day1.state.contracts("ES") < day0.state.contracts("ES") / 2  # de-equitized
    assert day1.interest > 0
    assert names[-1] == "DAY_COMPLETED"
    assert not any(c.is_hard_breach for c in day1.post_trade)


def test_proposed_basket_expires_without_approval(md: MarketData, cfg: AppConfig) -> None:
    day0 = step_day(None, md, D0, [_sub(D0)], None, cfg)
    pending = PendingBasket(day0.review.basket, OrderStatus.PROPOSED, D0)  # type: ignore[union-attr]
    day1 = step_day(day0.state, md, D1, [], pending, cfg)
    change = next(a for a in day1.actions if isinstance(a, StatusChange))
    assert change.status is OrderStatus.REJECTED and "expired" in change.reason
    assert day1.filled is None

    auto = step_day(day0.state, md, D1, [], pending, cfg, auto_approve_soft=True)
    assert [a.status for a in auto.actions if isinstance(a, StatusChange)] == [
        OrderStatus.APPROVED,
        OrderStatus.FILLED,
    ]


def test_pending_not_due_and_rejected_pending(md: MarketData, cfg: AppConfig) -> None:
    lag2 = cfg.model_copy(
        update={"workflow": cfg.workflow.model_copy(update={"execution_lag_days": 2})}
    )
    day0 = step_day(None, md, D0, [_sub(D0)], None, lag2)
    day1 = step_day(day0.state, md, D1, [], day0.pending, lag2)
    assert "BASKET_PENDING" in _names(day1) and day1.filled is None
    day2 = step_day(day1.state, md, D2, [], day1.pending, lag2)
    assert day2.filled is not None
    rejected = replace(day0.pending, status=OrderStatus.REJECTED)  # type: ignore[type-var]
    assert step_day(day0.state, md, D1, [], rejected, cfg).filled is None


def test_same_day_execution_with_zero_lag(md: MarketData, cfg: AppConfig) -> None:
    lag0 = cfg.model_copy(
        update={"workflow": cfg.workflow.model_copy(update={"execution_lag_days": 0})}
    )
    day0 = step_day(None, md, D0, [_sub(D0)], None, lag0)
    assert day0.filled is not None and day0.pending is None
    assert day0.state.contracts("ES") < 20


def test_redemption_and_no_equitization(md: MarketData, cfg: AppConfig) -> None:
    day0 = step_day(None, md, D0, [], None, cfg)
    red = Flow(D1, 40e6, CashFlowType.REDEMPTION)
    day1 = step_day(day0.state, md, D1, [red], day0.pending, cfg)
    assert "REDEMPTION_PAID" in _names(day1)
    assert day1.state.nav == pytest.approx(day0.state.nav - 40e6, rel=0.02)

    off = cfg.model_copy(
        update={"equitization": cfg.equitization.model_copy(update={"enabled": False})}
    )
    d0 = step_day(None, md, D0, [_sub(D0)], None, off)
    assert "CASH_RECEIVED" in _names(d0)
    assert d0.state.futures_notional == 0
    assert any(c.name == "total_exposure_range" and c.is_hard_breach for c in d0.post_trade)


def test_roll_flagged_and_executed(md: MarketData, cfg: AppConfig) -> None:
    day0 = step_day(None, md, date(2024, 3, 4), [], None, cfg)
    march = FuturesPosition("ESH4", "ES", date(2024, 3, 15), 50.0, 5, 5_000.0, 5_000.0)
    state = replace(day0.state, futures=(march,)).with_cash(1_250_000)
    res = step_day(state, md, date(2024, 3, 11), [], None, cfg)  # 4 business days to expiry
    names = _names(res)
    assert "ROLL_REQUIRED" in names and "FUTURES_ROLLED" in names
    assert {f.code for f in res.state.futures} <= {"ESM4", "MESM4"}


def test_day_must_move_forward(md: MarketData, cfg: AppConfig) -> None:
    day0 = step_day(None, md, D1, [], None, cfg)
    with pytest.raises(ValueError, match="not after"):
        step_day(day0.state, md, D0, [], None, cfg)


def test_flows_io(tmp_path, cfg: AppConfig) -> None:  # type: ignore[no-untyped-def]
    path = tmp_path / "f.csv"
    path.write_text("date,amount,type\n2024-04-01,1000,subscription\n2024-04-01,500,REDEMPTION\n")
    flows = read_cash_flows(path)
    assert [f.signed for f in flows] == [1000.0, -500.0]
    assert len(by_date(flows)[D0]) == 2
    assert read_cash_flows(tmp_path / "missing.csv") == []
    for bad, msg in [
        ("date,amount\n", "missing"),
        ("date,amount,type\n2024-04-01,1,GIFT\n", "unknown"),
        ("date,amount,type\n2024-04-01,-1,SUBSCRIPTION\n", "positive"),
    ]:
        path.write_text(bad)
        with pytest.raises(ValueError, match=msg):
            read_cash_flows(path)
    days = list(pd.bdate_range("2024-01-01", periods=200).date)
    a, b = random_flows(days, 1e8, cfg.backtest), random_flows(days, 1e8, cfg.backtest)
    assert a == b and 5 <= len(a) <= 40
    assert all(0.005e8 <= f.amount <= 0.05e8 for f in a)


def test_fill_rechecks_cash_after_a_redemption(md: MarketData, cfg: AppConfig) -> None:
    """Basket generated on a subscription day; a redemption is paid before it
    executes; at fill time the buys are scaled down so the fund never overdraws."""
    lag2 = cfg.model_copy(
        update={"workflow": cfg.workflow.model_copy(update={"execution_lag_days": 2})}
    )
    day0 = step_day(None, md, D0, [_sub(D0, 25e6)], None, lag2)
    red = Flow(D1, 30e6, CashFlowType.REDEMPTION)
    day1 = step_day(day0.state, md, D1, [red], day0.pending, lag2)
    day2 = step_day(day1.state, md, D2, [], day1.pending, lag2)
    executed = next(a for a in day2.actions if isinstance(a, Event) and a.name == "BASKET_EXECUTED")
    assert executed.details["buys_scaled_to"] < 1.0
    assert day2.state.unequitized_cash / day2.state.nav >= 0.0025 - 1e-6
    assert not any(c.is_hard_breach for c in day2.post_trade)
