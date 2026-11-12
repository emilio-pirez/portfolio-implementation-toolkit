"""Order persistence and basket status changes."""

from __future__ import annotations

import pandas as pd
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from pit.db.audit import log_event
from pit.db.models import Order, OrderStatus
from pit.orders.basket import Basket

# Allowed status transitions (anything else is a governance error).
TRANSITIONS: dict[OrderStatus, set[OrderStatus]] = {
    OrderStatus.PROPOSED: {OrderStatus.APPROVED, OrderStatus.REJECTED},
    OrderStatus.APPROVED: {OrderStatus.FILLED},
    OrderStatus.REJECTED: set(),
    OrderStatus.FILLED: set(),
}


def save_orders(session: Session, basket: Basket, status: OrderStatus) -> int:
    rows = [
        Order(
            batch_id=basket.batch_id,
            date=basket.as_of,
            ticker=str(r["ticker"]),
            side=str(r["side"]),
            quantity=float(r["quantity"]),
            est_price=float(r["est_price"]),
            est_notional=float(r["est_notional"]),
            est_cost=float(r["est_cost"]),
            status=status.value,
        )
        for r in basket.orders.to_dict("records")
    ]
    session.add_all(rows)
    session.flush()
    return len(rows)


def basket_status(session: Session, batch_id: str) -> OrderStatus | None:
    status = session.scalar(select(Order.status).where(Order.batch_id == batch_id).limit(1))
    return OrderStatus(status) if status else None


def set_basket_status(
    session: Session,
    batch_id: str,
    new: OrderStatus,
    actor: str = "system",
    reason: str = "",
) -> None:
    """Move every order of a batch to ``new``, enforcing the allowed transitions."""
    current = basket_status(session, batch_id)
    if current is None:
        raise LookupError(f"unknown batch {batch_id}")
    if new not in TRANSITIONS[current]:
        raise ValueError(f"cannot move batch from {current.value} to {new.value}")
    session.execute(update(Order).where(Order.batch_id == batch_id).values(status=new.value))
    log_event(
        session,
        f"BASKET_{new.value}",
        {"batch_id": batch_id, "from": current.value, "actor": actor, "reason": reason},
    )


def load_orders(session: Session, batch_id: str | None = None) -> pd.DataFrame:
    """Orders of one batch (or all), oldest first."""
    stmt = select(Order).order_by(Order.id)
    if batch_id is not None:
        stmt = stmt.where(Order.batch_id == batch_id)
    cols = [
        "id",
        "batch_id",
        "date",
        "ticker",
        "side",
        "quantity",
        "est_price",
        "est_notional",
        "est_cost",
        "status",
    ]
    return pd.DataFrame(
        [{c: getattr(o, c) for c in cols} for o in session.scalars(stmt)], columns=cols
    )
