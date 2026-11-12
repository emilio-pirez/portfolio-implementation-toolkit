"""Read-only queries behind the dashboard.

Every function takes an engine and returns a DataFrame (or a small value), so
the Streamlit app only does layout and the logic is covered by pytest.
"""

from __future__ import annotations

import json
from datetime import date

import pandas as pd
from sqlalchemy import Engine, func, select

from pit.compliance.pretrade import load_benchmark_weights
from pit.compliance.store import load_checks
from pit.config import AppConfig
from pit.db import read_audit_log, session_scope
from pit.db.models import (
    AssetType,
    AuditLog,
    BacktestDaily,
    DataSource,
    Order,
    OrderStatus,
    Position,
    Price,
    Security,
)
from pit.equitization.futures import parse_contract_code
from pit.orders.store import load_orders
from pit.portfolio.store import CASH_TICKER, load_target_weights

# --------------------------------------------------------------------------- status


def data_sources(engine: Engine) -> set[str]:
    with session_scope(engine) as s:
        return set(s.scalars(select(Price.source).distinct()))


def is_synthetic(engine: Engine) -> bool:
    return DataSource.SYNTHETIC.value in data_sources(engine)


def _count(engine: Engine, model: type) -> int:
    with session_scope(engine) as s:
        return int(s.scalar(select(func.count()).select_from(model)) or 0)


def has_positions(engine: Engine) -> bool:
    return _count(engine, Position) > 0


def has_backtest(engine: Engine) -> bool:
    return _count(engine, BacktestDaily) > 0


def position_dates(engine: Engine) -> list[date]:
    with session_scope(engine) as s:
        return sorted(s.scalars(select(Position.date).distinct()))


# --------------------------------------------------------------------------- portfolio


def _raw_positions(engine: Engine, as_of: date | None = None) -> pd.DataFrame:
    stmt = select(
        Position.date,
        Position.ticker,
        Position.quantity,
        Position.price,
        Position.market_value,
        Security.asset_type,
    ).join(Security, Security.ticker == Position.ticker, isouter=True)
    if as_of is not None:
        stmt = stmt.where(Position.date == as_of)
    with session_scope(engine) as s:
        rows = s.execute(stmt).all()
    return pd.DataFrame(
        rows, columns=["date", "ticker", "quantity", "price", "market_value", "asset_type"]
    )


def _futures_notional(df: pd.DataFrame, cfg: AppConfig) -> pd.Series:
    """contracts x price x multiplier for futures rows (0 for everything else)."""

    def one(row: pd.Series) -> float:
        if row["asset_type"] != AssetType.FUTURE.value:
            return 0.0
        root, _ = parse_contract_code(str(row["ticker"]), row["date"])
        return float(row["quantity"] * row["price"] * cfg.futures.contracts[root].multiplier)

    return df.apply(one, axis=1) if not df.empty else pd.Series(dtype=float)


def portfolio_history(engine: Engine, cfg: AppConfig) -> pd.DataFrame:
    """One row per processed day: NAV, cash, futures and exposure."""
    df = _raw_positions(engine)
    cols = ["nav", "equity", "cash", "futures_notional", "exposure", "cash_pct", "net_cash_pct"]
    if df.empty:
        return pd.DataFrame(columns=cols)
    df["notional"] = _futures_notional(df, cfg)
    is_cash = df["ticker"] == CASH_TICKER
    is_fut = df["asset_type"] == AssetType.FUTURE.value
    g = df.groupby("date")
    out = pd.DataFrame(
        {
            "nav": g["market_value"].sum(),
            "equity": df[~is_cash & ~is_fut].groupby("date")["market_value"].sum(),
            "cash": df[is_cash].groupby("date")["market_value"].sum(),
            "futures_notional": df[is_fut].groupby("date")["notional"].sum(),
        }
    ).fillna(0.0)
    out["exposure"] = (out["equity"] + out["futures_notional"]) / out["nav"]
    out["cash_pct"] = out["cash"] / out["nav"]
    out["net_cash_pct"] = (out["cash"] - out["futures_notional"]) / out["nav"]
    out.index = pd.to_datetime(out.index)
    return out[cols]


def holdings(engine: Engine, cfg: AppConfig, as_of: date) -> pd.DataFrame:
    """Stocks held or in the model on ``as_of``: weights vs target vs benchmark."""
    df = _raw_positions(engine, as_of)
    stocks = df[(df["ticker"] != CASH_TICKER) & (df["asset_type"] != AssetType.FUTURE.value)]
    nav = float(df["market_value"].sum()) if not df.empty else 0.0
    with session_scope(engine) as s:
        targets = load_target_weights(s, as_of) * (1 - cfg.equitization.cash_buffer_pct)
        bench = load_benchmark_weights(s, as_of)
        meta = pd.DataFrame(
            s.execute(select(Security.ticker, Security.name, Security.sector)).all(),
            columns=["ticker", "name", "sector"],
        ).set_index("ticker")
    held = stocks.set_index("ticker")
    tickers = held.index.union(targets.index)
    out = pd.DataFrame(index=tickers)
    out.index.name = "ticker"
    out["name"] = meta["name"].reindex(tickers)
    out["sector"] = meta["sector"].reindex(tickers).fillna("Unknown")
    out["shares"] = held["quantity"].reindex(tickers).fillna(0.0)
    out["price"] = held["price"].reindex(tickers)
    out["market_value"] = held["market_value"].reindex(tickers).fillna(0.0)
    out["weight"] = out["market_value"] / nav if nav else 0.0
    out["target"] = targets.reindex(tickers).fillna(0.0)
    out["benchmark"] = bench.reindex(tickers).fillna(0.0)
    out["vs_target"] = out["weight"] - out["target"]
    out["vs_benchmark"] = out["weight"] - out["benchmark"]
    return out.sort_values("weight", ascending=False).reset_index()


def sector_weights(engine: Engine, cfg: AppConfig, as_of: date) -> pd.DataFrame:
    """Portfolio, target and benchmark weight by sector (benchmark uses every member)."""
    h = holdings(engine, cfg, as_of)
    with session_scope(engine) as s:
        bench = load_benchmark_weights(s, as_of)
        sectors = pd.Series(
            dict(s.execute(select(Security.ticker, Security.sector)).all()), dtype=object
        )
    bench_sec = bench.groupby(sectors.reindex(bench.index).fillna("Unknown")).sum()
    out = pd.DataFrame(
        {
            "portfolio": h.groupby("sector")["weight"].sum(),
            "target": h.groupby("sector")["target"].sum(),
            "benchmark": bench_sec,
        }
    ).fillna(0.0)
    out["active"] = out["portfolio"] - out["benchmark"]
    out.index.name = "sector"
    return out.sort_values("benchmark", ascending=False).reset_index()


def futures_positions(engine: Engine, cfg: AppConfig, as_of: date) -> pd.DataFrame:
    df = _raw_positions(engine, as_of)
    fut = df[df["asset_type"] == AssetType.FUTURE.value].copy()
    if fut.empty:
        return pd.DataFrame(columns=["contract", "expiry", "contracts", "price", "notional"])
    fut["notional"] = _futures_notional(fut, cfg)
    fut["expiry"] = [parse_contract_code(t, as_of)[1] for t in fut["ticker"]]
    return fut.rename(columns={"ticker": "contract", "quantity": "contracts"})[
        ["contract", "expiry", "contracts", "price", "notional"]
    ].reset_index(drop=True)


# --------------------------------------------------------------------------- orders & compliance


def baskets(engine: Engine) -> pd.DataFrame:
    """One row per basket, newest first."""
    with session_scope(engine) as s:
        rows = s.execute(
            select(
                Order.batch_id,
                func.min(Order.date),
                func.min(Order.status),
                func.count(),
                func.sum(Order.est_notional).filter(Order.side == "BUY"),
                func.sum(Order.est_notional).filter(Order.side == "SELL"),
                func.sum(Order.est_cost),
                func.min(Order.id),
            ).group_by(Order.batch_id)
        ).all()
    df = pd.DataFrame(
        rows,
        columns=[
            "batch_id",
            "date",
            "status",
            "orders",
            "buy_notional",
            "sell_notional",
            "est_cost",
            "first_id",
        ],
    )
    df[["buy_notional", "sell_notional"]] = df[["buy_notional", "sell_notional"]].fillna(0.0)
    return (
        df.sort_values("first_id", ascending=False).drop(columns="first_id").reset_index(drop=True)
    )


def basket_orders(engine: Engine, batch_id: str) -> pd.DataFrame:
    with session_scope(engine) as s:
        return load_orders(s, batch_id)


def basket_checks(engine: Engine, batch_id: str) -> pd.DataFrame:
    with session_scope(engine) as s:
        return load_checks(s, batch_id)


def compliance_history(engine: Engine) -> pd.DataFrame:
    """Every stored check with its date and kind (pre-trade basket or post-trade)."""
    with session_scope(engine) as s:
        checks = load_checks(s)
        batch_dates = dict(
            s.execute(select(Order.batch_id, func.min(Order.date)).group_by(Order.batch_id)).all()
        )
    if checks.empty:
        return checks.assign(date=pd.Series(dtype=object), kind=pd.Series(dtype=object))
    post = checks["batch_id"].str.startswith("POST-")
    checks["kind"] = post.map({True: "post-trade", False: "pre-trade"})
    checks["date"] = [
        date.fromisoformat(b[5:]) if p else batch_dates.get(b)
        for b, p in zip(checks["batch_id"], post, strict=True)
    ]
    return checks


def breach_counts(engine: Engine) -> pd.DataFrame:
    """Failed checks per day, split by kind and severity."""
    h = compliance_history(engine)
    failed = h[~h["passed"].astype(bool)] if not h.empty else h
    if failed.empty:
        return pd.DataFrame(columns=["date", "kind", "severity", "breaches"])
    return failed.groupby(["date", "kind", "severity"]).size().rename("breaches").reset_index()


def active_alerts(engine: Engine) -> pd.DataFrame:
    """What needs attention now: latest post-trade breaches, baskets awaiting
    approval, and futures rolls flagged on the latest day."""
    rows: list[dict[str, str]] = []
    dates = position_dates(engine)
    if dates:
        last = dates[-1]
        post = basket_checks(engine, f"POST-{last.isoformat()}")
        for r in post[~post["passed"].astype(bool)].itertuples():
            rows.append(
                {"type": f"Post-trade {r.severity}", "detail": f"{r.check_name}: {r.message}"}
            )
    b = baskets(engine)
    for r in b[b["status"] == OrderStatus.PROPOSED.value].itertuples():
        rows.append({"type": "Awaiting approval", "detail": f"basket {r.batch_id} ({r.date})"})
    with session_scope(engine) as s:
        last_day_id = s.scalar(select(func.max(AuditLog.id)).where(AuditLog.event == "DAY_STARTED"))
        rolls = (
            s.scalars(
                select(AuditLog).where(AuditLog.event == "ROLL_REQUIRED", AuditLog.id > last_day_id)
            ).all()
            if last_day_id is not None
            else []
        )
        for roll in rolls:
            d = json.loads(roll.details_json)
            rows.append(
                {"type": "Roll required", "detail": f"{d.get('code')} expires {d.get('expiry')}"}
            )
    return pd.DataFrame(rows, columns=["type", "detail"])


# --------------------------------------------------------------------------- backtest


def backtest_runs(engine: Engine) -> pd.DataFrame:
    """Completed backtests, newest first, with their summary tables."""
    with session_scope(engine) as s:
        log = read_audit_log(s, event="BACKTEST_COMPLETED")
    if log.empty:
        return pd.DataFrame(columns=["run_id", "timestamp", "start", "end", "flows", "summary"])
    return pd.DataFrame(
        {
            "run_id": [d["run_id"] for d in log["details"]],
            "timestamp": log["timestamp"],
            "start": [d["start"] for d in log["details"]],
            "end": [d["end"] for d in log["details"]],
            "flows": [d["flows"] for d in log["details"]],
            "summary": [pd.DataFrame(d["summary"]) for d in log["details"]],
        }
    )


def backtest_daily(engine: Engine, run_id: str) -> pd.DataFrame:
    with session_scope(engine) as s:
        rows = s.scalars(select(BacktestDaily).where(BacktestDaily.run_id == run_id)).all()
        df = pd.DataFrame(
            [{c.name: getattr(r, c.name) for c in BacktestDaily.__table__.columns} for r in rows]
        )
    if df.empty:
        return df
    df["date"] = pd.to_datetime(df["date"])
    df = df.sort_values(["scenario", "date"])
    df["cum_cash_drag_bp"] = df.groupby("scenario")["cash_drag"].transform(
        lambda x: x.fillna(0).cumsum() * 10_000
    )
    return df.reset_index(drop=True)


# --------------------------------------------------------------------------- audit


def event_types(engine: Engine) -> list[str]:
    with session_scope(engine) as s:
        return sorted(s.scalars(select(AuditLog.event).distinct()))


def audit_events(
    engine: Engine,
    events: list[str] | None = None,
    start: date | None = None,
    end: date | None = None,
    text: str | None = None,
    limit: int = 2_000,
) -> pd.DataFrame:
    """Filtered audit log, newest first, with details as JSON text."""
    stmt = select(AuditLog).order_by(AuditLog.id.desc())
    if events:
        stmt = stmt.where(AuditLog.event.in_(events))
    if start is not None:
        stmt = stmt.where(func.date(AuditLog.timestamp) >= start.isoformat())
    if end is not None:
        stmt = stmt.where(func.date(AuditLog.timestamp) <= end.isoformat())
    if text:
        stmt = stmt.where(AuditLog.details_json.contains(text))
    with session_scope(engine) as s:
        rows = s.scalars(stmt.limit(limit)).all()
        return pd.DataFrame(
            [
                {
                    "id": r.id,
                    "timestamp": r.timestamp,
                    "event": r.event,
                    "details": json.dumps(json.loads(r.details_json), sort_keys=True),
                }
                for r in rows
            ],
            columns=["id", "timestamp", "event", "details"],
        )
