"""Small data-access helpers shared by the loaders and, later, the workflow."""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from datetime import date
from typing import Any, cast

import pandas as pd
from sqlalchemy import Table, delete, func, select
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.orm import Session

from pit.db.models import (
    MARKET_SOURCES,
    Base,
    BenchmarkWeight,
    DataSource,
    Price,
    Rate,
    Security,
)

MARKET_DATA_MODELS: tuple[type[Base], ...] = (Security, Price, Rate, BenchmarkWeight)
_CHUNK = 500


def upsert(session: Session, model: type[Base], rows: Sequence[dict[Any, Any]]) -> int:
    """Insert rows, updating non-key columns when the primary key already exists.

    Works on SQLite and Postgres (both support ``INSERT ... ON CONFLICT``).
    Returns the number of rows sent.
    """
    if not rows:
        return 0
    table = cast(Table, model.__table__)
    pk_cols = [c.name for c in table.primary_key.columns]
    dialect = session.get_bind().dialect.name
    insert_fn = sqlite.insert if dialect == "sqlite" else postgresql.insert

    for start in range(0, len(rows), _CHUNK):
        chunk = list(rows[start : start + _CHUNK])
        stmt = insert_fn(table).values(chunk)
        update_cols = {c: stmt.excluded[c] for c in chunk[0] if c not in pk_cols}
        if update_cols:
            stmt = stmt.on_conflict_do_update(index_elements=pk_cols, set_=update_cols)
        else:
            stmt = stmt.on_conflict_do_nothing(index_elements=pk_cols)
        session.execute(stmt)
    return len(rows)


def purge_market_data_not_from(session: Session, keep: Iterable[DataSource]) -> int:
    """Delete market data whose source is not in ``keep``.

    Called before every load so synthetic and real data never coexist in the
    same database. ``INTERNAL`` rows (futures contracts, cash) are never
    touched. Returns the number of rows deleted.
    """
    keep_set = set(keep)
    drop = [s.value for s in MARKET_SOURCES if s not in keep_set]
    deleted = 0
    for model in MARKET_DATA_MODELS:
        source_col = model.source  # type: ignore[attr-defined]
        result = session.execute(delete(model).where(source_col.in_(drop)))
        deleted += result.rowcount or 0  # type: ignore[attr-defined]
    return deleted


def price_coverage(
    session: Session, ticker: str, sources: Iterable[DataSource] | None = None
) -> tuple[date, date] | None:
    """First and last cached price date for ``ticker`` (``None`` if not cached).

    ``sources`` restricts the lookup, so that synthetic rows are never taken
    as a cache hit for real data.
    """
    stmt = select(func.min(Price.date), func.max(Price.date)).where(Price.ticker == ticker)
    if sources is not None:
        stmt = stmt.where(Price.source.in_([s.value for s in sources]))
    lo, hi = session.execute(stmt).one()
    return (lo, hi) if lo is not None else None


def rate_coverage(session: Session, series: str) -> tuple[date, date] | None:
    lo, hi = session.execute(
        select(func.min(Rate.date), func.max(Rate.date)).where(Rate.series == series)
    ).one()
    return (lo, hi) if lo is not None else None


def data_sources(session: Session) -> set[str]:
    """Distinct sources present in the price table (used for the 'synthetic' banner)."""
    return set(session.scalars(select(Price.source).distinct()))


def load_prices(
    session: Session,
    tickers: Sequence[str] | None = None,
    start: date | None = None,
    end: date | None = None,
    field: str = "close",
) -> pd.DataFrame:
    """Wide DataFrame (index = date, columns = ticker) of ``close`` or ``volume``."""
    stmt = select(Price.date, Price.ticker, getattr(Price, field))
    if tickers is not None:
        stmt = stmt.where(Price.ticker.in_(list(tickers)))
    if start is not None:
        stmt = stmt.where(Price.date >= start)
    if end is not None:
        stmt = stmt.where(Price.date <= end)
    rows = session.execute(stmt).all()
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows, columns=["date", "ticker", field])
    wide = df.pivot(index="date", columns="ticker", values=field).sort_index()
    wide.index = pd.to_datetime(wide.index)
    wide.columns.name = None
    return wide
