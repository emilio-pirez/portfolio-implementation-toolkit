"""Audit trail.

Every material action (data loaded, flow received, basket generated, check
failed, order filled) is written here with a UTC timestamp and a JSON payload.
This is the governance backbone: the log alone should let a reviewer
reconstruct what happened on any day and why.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from typing import Any

import pandas as pd
from sqlalchemy import select
from sqlalchemy.orm import Session

from pit.db.models import AuditLog


def _default(obj: Any) -> Any:
    if isinstance(obj, date | datetime):
        return obj.isoformat()
    if hasattr(obj, "item"):  # numpy scalars
        return obj.item()
    return str(obj)


def log_event(session: Session, event: str, details: dict[str, Any] | None = None) -> AuditLog:
    """Append one event to ``audit_log`` (flushed, committed by the caller's scope)."""
    entry = AuditLog(
        timestamp=datetime.now(UTC).replace(tzinfo=None),
        event=event,
        details_json=json.dumps(details or {}, default=_default, sort_keys=True),
    )
    session.add(entry)
    session.flush()
    return entry


def read_audit_log(session: Session, event: str | None = None) -> pd.DataFrame:
    """Audit log as a DataFrame, newest first."""
    stmt = select(AuditLog).order_by(AuditLog.id.desc())
    if event is not None:
        stmt = stmt.where(AuditLog.event == event)
    rows = session.scalars(stmt).all()
    return pd.DataFrame(
        [
            {
                "id": r.id,
                "timestamp": r.timestamp,
                "event": r.event,
                "details": json.loads(r.details_json),
            }
            for r in rows
        ],
        columns=["id", "timestamp", "event", "details"],
    )
