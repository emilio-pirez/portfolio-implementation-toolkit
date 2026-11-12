"""Read and write portfolio data in the database.

Positions table layout (one row per holding per day):

* equities  - quantity = shares, price = close, market_value = shares x close
* futures   - ticker = contract code (``ESZ6``), quantity = contracts,
              price = futures price, market_value = unsettled variation margin
              (zero after the daily settlement)
* cash      - ticker = ``USD``, quantity = price-1 units, market_value = cash
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

import pandas as pd
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from pit.config import AppConfig
from pit.db.models import (
    AssetType,
    DataSource,
    Position,
    Price,
    Rate,
    Security,
    TargetWeight,
)
from pit.db.repository import upsert
from pit.equitization.futures import FuturesQuote, active_quote, parse_contract_code
from pit.portfolio.state import FuturesPosition, PortfolioState

CASH_TICKER = "USD"
INDEX_TICKER = "SPX"


# --------------------------------------------------------------------------- market data


@dataclass(frozen=True)
class MarketSnapshot:
    """Everything priced as of one date (latest available on or before it)."""

    as_of: date
    prices: pd.Series  # ticker -> close (stocks only)
    index_level: float
    risk_free: float

    def futures_quote(self, cfg: AppConfig) -> FuturesQuote:
        return active_quote(
            self.as_of,
            self.index_level,
            self.risk_free,
            cfg.futures.index_dividend_yield,
            cfg.futures.roll_window_business_days,
        )


def latest_price_date(session: Session, on_or_before: date) -> date | None:
    return session.scalar(
        select(func.max(Price.date)).where(Price.ticker == INDEX_TICKER, Price.date <= on_or_before)
    )


def load_snapshot(session: Session, as_of: date, risk_free_series: str = "DTB3") -> MarketSnapshot:
    """Closes, index level and T-Bill rate for the last trading day <= ``as_of``."""
    px_date = latest_price_date(session, as_of)
    if px_date is None:
        raise LookupError(f"no index level on or before {as_of}; run `pit load-data` first")
    rows = session.execute(select(Price.ticker, Price.close).where(Price.date == px_date)).all()
    closes = pd.Series({t: c for t, c in rows}, dtype=float)
    index_level = float(closes.pop(INDEX_TICKER))
    rate = session.scalar(
        select(Rate.value)
        .where(Rate.series == risk_free_series, Rate.date <= px_date)
        .order_by(Rate.date.desc())
        .limit(1)
    )
    if rate is None:
        raise LookupError(f"no {risk_free_series} rate on or before {px_date}")
    return MarketSnapshot(as_of=px_date, prices=closes, index_level=index_level, risk_free=rate)


def load_target_weights(session: Session, as_of: date) -> pd.Series:
    """Model weights in force on ``as_of`` (the latest set dated on or before it)."""
    model_date = session.scalar(
        select(func.max(TargetWeight.as_of)).where(TargetWeight.as_of <= as_of)
    )
    if model_date is None:
        raise LookupError(f"no target weights on or before {as_of}")
    rows = session.execute(
        select(TargetWeight.ticker, TargetWeight.weight).where(TargetWeight.as_of == model_date)
    ).all()
    return pd.Series({t: w for t, w in rows}, dtype=float).sort_index()


# --------------------------------------------------------------------------- positions


def _ensure_internal_securities(session: Session, state: PortfolioState) -> None:
    rows = [
        {
            "ticker": CASH_TICKER,
            "name": "US Dollar cash",
            "sector": None,
            "asset_type": AssetType.CASH.value,
            "source": DataSource.INTERNAL.value,
        }
    ]
    for f in state.futures:
        rows.append(
            {
                "ticker": f.code,
                "name": f"{f.root} future {f.expiry:%b-%Y}",
                "sector": None,
                "asset_type": AssetType.FUTURE.value,
                "source": DataSource.INTERNAL.value,
            }
        )
    upsert(session, Security, rows)


def save_positions(session: Session, state: PortfolioState) -> int:
    """Replace the positions stored for ``state.as_of``. Returns rows written."""
    session.execute(delete(Position).where(Position.date == state.as_of))
    _ensure_internal_securities(session, state)
    mv = state.market_values
    rows = [
        {
            "date": state.as_of,
            "ticker": t,
            "quantity": float(state.shares[t]),
            "price": float(state.prices[t]),
            "market_value": float(mv[t]),
        }
        for t in mv.index
    ]
    rows += [
        {
            "date": state.as_of,
            "ticker": f.code,
            "quantity": float(f.quantity),
            "price": f.price,
            "market_value": f.unsettled_pnl,
        }
        for f in state.futures
    ]
    rows.append(
        {
            "date": state.as_of,
            "ticker": CASH_TICKER,
            "quantity": state.cash,
            "price": 1.0,
            "market_value": state.cash,
        }
    )
    return upsert(session, Position, rows)


def latest_position_date(session: Session, on_or_before: date) -> date | None:
    return session.scalar(select(func.max(Position.date)).where(Position.date <= on_or_before))


def load_positions(session: Session, as_of: date, cfg: AppConfig) -> PortfolioState:
    """Rebuild the ``PortfolioState`` stored for exactly ``as_of``."""
    rows = session.execute(
        select(Position, Security.asset_type)
        .join(Security, Security.ticker == Position.ticker, isouter=True)
        .where(Position.date == as_of)
    ).all()
    if not rows:
        raise LookupError(f"no positions stored for {as_of}")

    shares: dict[str, float] = {}
    prices: dict[str, float] = {}
    futures: list[FuturesPosition] = []
    cash = 0.0
    for pos, asset_type in rows:
        if pos.ticker == CASH_TICKER:
            cash = pos.market_value
        elif asset_type == AssetType.FUTURE.value:
            root, expiry = parse_contract_code(pos.ticker, as_of)
            mult = cfg.futures.contracts[root].multiplier
            qty = int(pos.quantity)
            # market_value holds the unsettled P&L, so recover the settle price.
            settle = pos.price - pos.market_value / (qty * mult) if qty else pos.price
            futures.append(FuturesPosition(pos.ticker, root, expiry, mult, qty, pos.price, settle))
        else:
            shares[pos.ticker] = pos.quantity
            prices[pos.ticker] = pos.price
    return PortfolioState(
        as_of=as_of,
        shares=pd.Series(shares, dtype=float),
        prices=pd.Series(prices, dtype=float),
        cash=cash,
        futures=tuple(sorted(futures, key=lambda f: f.code)),
    )
