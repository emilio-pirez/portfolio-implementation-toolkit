"""SQLAlchemy ORM models.

The schema mirrors a stripped-down portfolio management system:

* reference / market data: ``securities``, ``prices``, ``rates``, ``benchmark_weights``
* PM inputs: ``target_weights``, ``cash_flows``
* book of record: ``positions``
* implementation: ``orders``, ``compliance_checks``
* governance: ``audit_log``

Every market-data row carries a ``source`` column (``FMP``, ``FRED`` or
``SYNTHETIC``) so synthetic data can never be mistaken for real data.

Only portable column types are used, so moving from SQLite to Postgres is a
change of connection URL.
"""

from __future__ import annotations

from datetime import date, datetime
from enum import StrEnum

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    Float,
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from pit.config import Severity


class Base(DeclarativeBase):
    pass


# --------------------------------------------------------------------------- enums


class AssetType(StrEnum):
    EQUITY = "EQUITY"
    FUTURE = "FUTURE"
    CASH = "CASH"
    INDEX = "INDEX"  # benchmark index level (not tradable; used for futures fair value and beta)


class DataSource(StrEnum):
    FMP = "FMP"
    FRED = "FRED"
    SYNTHETIC = "SYNTHETIC"


class CashFlowType(StrEnum):
    SUBSCRIPTION = "SUBSCRIPTION"
    REDEMPTION = "REDEMPTION"


class OrderSide(StrEnum):
    BUY = "BUY"
    SELL = "SELL"


class OrderStatus(StrEnum):
    PROPOSED = "PROPOSED"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    FILLED = "FILLED"


def _in(column: str, enum: type[StrEnum]) -> CheckConstraint:
    """CHECK constraint restricting a column to the members of an enum."""
    values = ", ".join(f"'{m.value}'" for m in enum)
    return CheckConstraint(f"{column} IN ({values})", name=f"ck_{column}_valid")


# --------------------------------------------------------------------------- market data


class Security(Base):
    __tablename__ = "securities"
    __table_args__ = (_in("asset_type", AssetType), _in("source", DataSource))

    ticker: Mapped[str] = mapped_column(String(16), primary_key=True)
    name: Mapped[str | None] = mapped_column(String(128))
    sector: Mapped[str | None] = mapped_column(String(64))
    asset_type: Mapped[str] = mapped_column(String(8))
    source: Mapped[str] = mapped_column(String(16))


class Price(Base):
    """Daily adjusted close. ``volume`` (shares) feeds the order-vs-ADV check."""

    __tablename__ = "prices"
    __table_args__ = (
        CheckConstraint("close > 0", name="ck_close_positive"),
        _in("source", DataSource),
    )

    ticker: Mapped[str] = mapped_column(String(16), primary_key=True)
    date: Mapped[date] = mapped_column(Date, primary_key=True)
    close: Mapped[float] = mapped_column(Float)
    volume: Mapped[float | None] = mapped_column(Float)
    source: Mapped[str] = mapped_column(String(16))


class Rate(Base):
    """Interest-rate time series stored as decimals (0.043 = 4.3 %), e.g. FRED DTB3."""

    __tablename__ = "rates"
    __table_args__ = (_in("source", DataSource),)

    series: Mapped[str] = mapped_column(String(16), primary_key=True)
    date: Mapped[date] = mapped_column(Date, primary_key=True)
    value: Mapped[float] = mapped_column(Float)
    source: Mapped[str] = mapped_column(String(16))


class BenchmarkWeight(Base):
    """Cap-weighted benchmark: weight = market cap / sum of market caps."""

    __tablename__ = "benchmark_weights"
    __table_args__ = (_in("source", DataSource),)

    date: Mapped[date] = mapped_column(Date, primary_key=True)
    ticker: Mapped[str] = mapped_column(String(16), primary_key=True)
    weight: Mapped[float] = mapped_column(Float)
    market_cap: Mapped[float | None] = mapped_column(Float)
    source: Mapped[str] = mapped_column(String(16))


# --------------------------------------------------------------------------- PM inputs


class TargetWeight(Base):
    """Model portfolio weights handed to the implementation PM."""

    __tablename__ = "target_weights"

    as_of: Mapped[date] = mapped_column(Date, primary_key=True)
    ticker: Mapped[str] = mapped_column(String(16), primary_key=True)
    weight: Mapped[float] = mapped_column(Float)


class CashFlow(Base):
    """Client flows. ``amount`` is always positive; ``type`` gives the direction."""

    __tablename__ = "cash_flows"
    __table_args__ = (
        CheckConstraint("amount > 0", name="ck_amount_positive"),
        _in("type", CashFlowType),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    date: Mapped[date] = mapped_column(Date, index=True)
    amount: Mapped[float] = mapped_column(Float)
    type: Mapped[str] = mapped_column(String(16))


# --------------------------------------------------------------------------- book of record


class Position(Base):
    """End-of-day holdings. For futures, ``market_value`` is the variation-margin
    P&L (close to zero); exposure is quantity x price x multiplier."""

    __tablename__ = "positions"

    date: Mapped[date] = mapped_column(Date, primary_key=True)
    ticker: Mapped[str] = mapped_column(String(16), primary_key=True)
    quantity: Mapped[float] = mapped_column(Float)
    price: Mapped[float] = mapped_column(Float)
    market_value: Mapped[float] = mapped_column(Float)


# --------------------------------------------------------------------------- implementation


class Order(Base):
    __tablename__ = "orders"
    __table_args__ = (
        CheckConstraint("quantity > 0", name="ck_quantity_positive"),
        _in("side", OrderSide),
        _in("status", OrderStatus),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    batch_id: Mapped[str] = mapped_column(String(36), index=True)
    date: Mapped[date] = mapped_column(Date)
    ticker: Mapped[str] = mapped_column(String(16))
    side: Mapped[str] = mapped_column(String(4))
    quantity: Mapped[float] = mapped_column(Float)
    est_price: Mapped[float] = mapped_column(Float)
    est_notional: Mapped[float] = mapped_column(Float)
    est_cost: Mapped[float] = mapped_column(Float)
    status: Mapped[str] = mapped_column(String(10))


class ComplianceCheck(Base):
    __tablename__ = "compliance_checks"
    __table_args__ = (_in("severity", Severity),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    batch_id: Mapped[str] = mapped_column(String(36), index=True)
    check_name: Mapped[str] = mapped_column(String(64))
    value: Mapped[float | None] = mapped_column(Float)
    # Text so that both one-sided ("0.08") and two-sided ("[0.98, 1.02]") limits fit.
    limit_value: Mapped[str | None] = mapped_column(String(64))
    passed: Mapped[bool] = mapped_column(Boolean)
    severity: Mapped[str] = mapped_column(String(4))
    message: Mapped[str | None] = mapped_column(Text)


# --------------------------------------------------------------------------- governance


class AuditLog(Base):
    """Append-only event log: every material action writes one row."""

    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    timestamp: Mapped[datetime] = mapped_column(DateTime, index=True)
    event: Mapped[str] = mapped_column(String(64), index=True)
    details_json: Mapped[str] = mapped_column(Text)
