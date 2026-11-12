from datetime import date
from pathlib import Path

import pandas as pd
import pytest
from sqlalchemy import Engine

from pit.compliance import approve_basket, load_checks
from pit.config import AppConfig
from pit.db import read_audit_log, session_scope
from pit.db.models import CashFlowType, OrderStatus
from pit.orders import load_orders
from pit.orders.store import basket_status
from pit.portfolio import load_positions
from pit.workflow import Flow, MarketData, WorkflowError, run_day
from pit.workflow.run import load_basket, load_pending_basket

D0, D1, D2 = date(2024, 4, 1), date(2024, 4, 2), date(2024, 4, 3)
FLOWS = [Flow(D0, 25e6, CashFlowType.SUBSCRIPTION)]


def test_run_day_full_audit_trail(loaded_engine: Engine, cfg: AppConfig) -> None:
    md = MarketData.load(loaded_engine, cfg)
    r0 = run_day(loaded_engine, cfg, D0, md=md, flows=FLOWS)
    r1 = run_day(loaded_engine, cfg, D1, md=md, flows=FLOWS)
    assert r0.synthetic and r0.basket_csv is not None and r0.basket_csv.exists()
    assert pd.read_csv(r0.basket_csv).columns.tolist() == [
        "ticker",
        "side",
        "quantity",
        "est_price",
        "est_notional",
    ]
    with session_scope(loaded_engine) as s:
        all_events = read_audit_log(s).sort_values("id")["event"].tolist()
        events = all_events[all_events.index("DAY_STARTED") :]  # skip DATA_LOADED
        first_batch = r0.result.review.basket.batch_id  # type: ignore[union-attr]
        assert basket_status(s, first_batch) is OrderStatus.FILLED
        assert set(load_orders(s, first_batch)["status"]) == {"FILLED"}
        assert len(load_checks(s, f"POST-{D1.isoformat()}")) == 10
        state = load_positions(s, D1, cfg)
    day0 = events[: events.index("BASKET_EXPORTED") + 1]
    assert day0 == [
        "DAY_STARTED",
        "PORTFOLIO_INITIALIZED",
        "FLOW_RECEIVED",
        "FUTURES_EQUITIZED",
        "BASKET_GENERATED",
        "BASKET_APPROVED",
        "DAY_COMPLETED",
        "BASKET_EXPORTED",
    ]
    day1 = events[len(day0) :]
    assert day1[:4] == ["DAY_STARTED", "MARKED_TO_MARKET", "BASKET_EXECUTED", "BASKET_FILLED"]
    assert "DAY_COMPLETED" in day1
    assert state.nav == pytest.approx(r1.result.state.nav)


def test_run_day_guards(loaded_engine: Engine, cfg: AppConfig) -> None:
    md = MarketData.load(loaded_engine, cfg)
    run_day(loaded_engine, cfg, D1, md=md, flows=[])
    with pytest.raises(WorkflowError, match="once and in order"):
        run_day(loaded_engine, cfg, D1, md=md, flows=[])
    with pytest.raises(WorkflowError, match="once and in order"):
        run_day(loaded_engine, cfg, D0, md=md, flows=[])
    with pytest.raises(WorkflowError, match="trading day"):
        run_day(loaded_engine, cfg, date(2024, 4, 6), md=md, flows=[])


def test_manual_approval_between_days(loaded_engine: Engine, cfg: AppConfig) -> None:
    md = MarketData.load(loaded_engine, cfg)
    r0 = run_day(loaded_engine, cfg, D0, md=md, flows=FLOWS)
    batch = r0.result.review.basket.batch_id  # type: ignore[union-attr]
    # simulate a basket that needed approval: set it back to PROPOSED, then approve it
    from sqlalchemy import update

    from pit.db.models import Order

    with session_scope(loaded_engine) as s:
        s.execute(update(Order).where(Order.batch_id == batch).values(status="PROPOSED"))
    with session_scope(loaded_engine) as s:
        pending = load_pending_basket(s, D1)
        assert pending is not None and pending.status is OrderStatus.PROPOSED
        approve_basket(s, batch, "emilio", "reviewed")
        assert load_basket(s, "nope") is None
    r1 = run_day(loaded_engine, cfg, D1, md=md, flows=[])
    assert r1.result.filled is not None and r1.result.filled.batch_id == batch


def test_run_day_reads_flows_file(loaded_engine: Engine, cfg: AppConfig, tmp_path: Path) -> None:
    flows = tmp_path / "flows.csv"
    flows.write_text("date,amount,type\n2024-04-01,10000000,SUBSCRIPTION\n")
    cfg2 = cfg.model_copy(
        update={"data": cfg.data.model_copy(update={"cash_flows_file": str(flows)})}
    )
    r = run_day(loaded_engine, cfg2, D0)
    assert r.result.state.nav == pytest.approx(cfg.fund.initial_nav + 10e6)
