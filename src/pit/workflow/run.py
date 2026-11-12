"""``pit run-day``: process one trading day and persist everything.

Governance rules:

* Days are processed **once and in order**. Re-running a processed day, or a
  day before the last processed one, is refused, so the audit log stays
  append-only and consistent with the positions.
* Every action of the day (flows, futures trades, basket generation,
  compliance results, status changes, fills, post-trade checks) is written to
  ``audit_log``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from pathlib import Path

import pandas as pd
from sqlalchemy import Engine, delete, select
from sqlalchemy.orm import Session

from pit.compliance.store import record_review, save_checks
from pit.config import AppConfig
from pit.db import log_event, session_scope
from pit.db.models import CashFlow, Order, OrderStatus
from pit.orders.basket import ORDER_COLUMNS, Basket, export_basket_csv
from pit.orders.store import set_basket_status
from pit.portfolio.store import latest_position_date, load_positions, save_positions
from pit.workflow.day import DayResult, Event, PendingBasket, ReviewRecorded, StatusChange, step_day
from pit.workflow.flows import Flow, read_cash_flows
from pit.workflow.market import MarketData


class WorkflowError(RuntimeError):
    """Raised when a day cannot be processed (wrong order, missing data...)."""


@dataclass(frozen=True)
class DayReport:
    result: DayResult
    basket_csv: Path | None
    synthetic: bool


def load_basket(session: Session, batch_id: str) -> Basket | None:
    """Rebuild a stored basket (orders only) from the ``orders`` table."""
    orders = session.scalars(select(Order).where(Order.batch_id == batch_id)).all()
    if not orders:
        return None
    df = pd.DataFrame(
        [
            {
                "ticker": o.ticker,
                "side": o.side,
                "quantity": o.quantity,
                "est_price": o.est_price,
                "est_notional": o.est_notional,
                "est_cost": o.est_cost,
                "current_shares": float("nan"),
                "target_shares": float("nan"),
                "weight_change": float("nan"),
            }
            for o in orders
        ],
        columns=ORDER_COLUMNS,
    )
    return Basket(batch_id, orders[0].date, df, pd.DataFrame(columns=ORDER_COLUMNS))


def load_pending_basket(session: Session, before: date) -> PendingBasket | None:
    """Most recent basket generated before ``before`` that is still waiting
    (PROPOSED or APPROVED, not yet FILLED or REJECTED)."""
    row = session.execute(
        select(Order.batch_id, Order.date, Order.status)
        .where(
            Order.date < before,
            Order.status.in_([OrderStatus.PROPOSED.value, OrderStatus.APPROVED.value]),
        )
        .order_by(Order.date.desc(), Order.id.desc())
        .limit(1)
    ).first()
    if row is None:
        return None
    batch_id, generated, status = row
    basket = load_basket(session, batch_id)
    assert basket is not None
    return PendingBasket(basket, OrderStatus(status), generated)


def _store_flows(session: Session, as_of: date, flows: list[Flow]) -> None:
    session.execute(delete(CashFlow).where(CashFlow.date == as_of))
    session.add_all(CashFlow(date=f.date, amount=f.amount, type=f.type.value) for f in flows)


def _write_actions(session: Session, res: DayResult) -> None:
    for action in res.actions:
        if isinstance(action, Event):
            log_event(session, action.name, action.details)
        elif isinstance(action, StatusChange):
            set_basket_status(session, action.batch_id, action.status, action.actor, action.reason)
        elif isinstance(action, ReviewRecorded):
            record_review(session, action.review)


def run_day(
    engine: Engine,
    cfg: AppConfig,
    as_of: date,
    md: MarketData | None = None,
    flows: list[Flow] | None = None,
) -> DayReport:
    md = md or MarketData.load(engine, cfg)
    if not md.is_trading_day(as_of):
        raise WorkflowError(f"{as_of} is not a trading day in the loaded price history")
    if flows is None:
        flows = read_cash_flows(cfg.resolve_path(cfg.data.cash_flows_file))
    todays = [f for f in flows if f.date == as_of]

    with session_scope(engine) as s:
        last = latest_position_date(s, date.max)
        if last is not None and as_of <= last:
            raise WorkflowError(
                f"{as_of} is not after the last processed day ({last}); days are processed "
                "once and in order"
            )
        state = load_positions(s, last, cfg) if last else None
        pending = load_pending_basket(s, as_of)

    try:
        result = step_day(state, md, as_of, todays, pending, cfg)
    except (LookupError, ValueError) as exc:
        raise WorkflowError(str(exc)) from exc

    csv_path: Path | None = None
    with session_scope(engine) as s:
        log_event(s, "DAY_STARTED", {"date": as_of, "synthetic_data": md.is_synthetic})
        _store_flows(s, as_of, todays)
        _write_actions(s, result)
        save_positions(s, result.state)
        save_checks(s, f"POST-{as_of.isoformat()}", result.post_trade)
        if result.review is not None and result.review.status is not OrderStatus.REJECTED:
            out = cfg.resolve_path(cfg.workflow.outputs_dir) / "baskets"
            name = f"{as_of.isoformat()}_{result.review.basket.batch_id[:8]}.csv"
            csv_path = export_basket_csv(result.review.basket, out / name)
            log_event(
                s,
                "BASKET_EXPORTED",
                {"batch_id": result.review.basket.batch_id, "path": str(csv_path)},
            )
    return DayReport(result=result, basket_csv=csv_path, synthetic=md.is_synthetic)
