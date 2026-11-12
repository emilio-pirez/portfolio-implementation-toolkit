"""Record a basket review (orders, checks, audit trail) and manual decisions."""

from __future__ import annotations

import pandas as pd
from sqlalchemy import select
from sqlalchemy.orm import Session

from pit.compliance.checks import CheckResult
from pit.compliance.pretrade import BasketReview
from pit.db.audit import log_event
from pit.db.models import ComplianceCheck, OrderStatus
from pit.orders.store import basket_status, save_orders, set_basket_status


def save_checks(session: Session, batch_id: str, checks: list[CheckResult]) -> int:
    session.add_all(ComplianceCheck(batch_id=batch_id, **c.as_row()) for c in checks)
    session.flush()
    return len(checks)


def record_review(session: Session, review: BasketReview) -> None:
    """Persist a reviewed basket. Audit trail:

    BASKET_GENERATED -> one COMPLIANCE_BREACH per failed check -> the decision
    (BASKET_APPROVED, BASKET_PENDING_APPROVAL or BASKET_REJECTED).
    """
    batch = review.basket.batch_id
    save_orders(session, review.basket, review.status)
    save_checks(session, batch, review.checks)
    log_event(session, "BASKET_GENERATED", review.basket.summary())
    for c in review.checks:
        if not c.passed:
            log_event(
                session,
                "COMPLIANCE_BREACH",
                {
                    "batch_id": batch,
                    "check": c.name,
                    "severity": c.severity.value,
                    "value": c.value,
                    "limit": c.limit,
                    "message": c.message,
                },
            )
    decision = {
        OrderStatus.APPROVED: "BASKET_APPROVED",
        OrderStatus.PROPOSED: "BASKET_PENDING_APPROVAL",
        OrderStatus.REJECTED: "BASKET_REJECTED",
    }[review.status]
    log_event(session, decision, review.summary())


def approve_basket(session: Session, batch_id: str, approver: str, reason: str) -> None:
    """Manual sign-off of a basket with SOFT alerts. A reason is mandatory."""
    if not reason.strip():
        raise ValueError("a reason is required to approve a basket with alerts")
    set_basket_status(session, batch_id, OrderStatus.APPROVED, actor=approver, reason=reason)


def reject_basket(session: Session, batch_id: str, approver: str, reason: str) -> None:
    set_basket_status(session, batch_id, OrderStatus.REJECTED, actor=approver, reason=reason)


def load_checks(session: Session, batch_id: str | None = None) -> pd.DataFrame:
    stmt = select(ComplianceCheck).order_by(ComplianceCheck.id)
    if batch_id is not None:
        stmt = stmt.where(ComplianceCheck.batch_id == batch_id)
    cols = ["id", "batch_id", "check_name", "value", "limit_value", "passed", "severity", "message"]
    return pd.DataFrame(
        [{c: getattr(r, c) for c in cols} for r in session.scalars(stmt)], columns=cols
    )


__all__ = [
    "approve_basket",
    "basket_status",
    "load_checks",
    "record_review",
    "reject_basket",
    "save_checks",
]
