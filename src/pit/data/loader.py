"""Load market data and PM inputs into the database (``pit load-data``).

Two modes:

* **online**  - S&P 500 constituents and market caps (FMP), adjusted prices for
  the model's tickers plus the index (FMP), and the 3M T-Bill rate (FRED).
  The database doubles as the cache: for each ticker only the dates after the
  last cached date are requested.
* **offline** - a fixed 100-stock universe with synthetic prices (see
  ``pit.data.synthetic``). Every row is tagged ``SYNTHETIC``.

Real and synthetic data never coexist: each load first purges market data from
the other mode. Online data is fetched completely *before* anything is written,
so a failed API call leaves the database untouched.

Why prices only for the model's tickers? Ex-ante tracking error is computed
against the index return series itself (see ``docs/methodology.md``), so the
covariance matrix only needs the stocks we may hold plus SPX. That keeps the
online load at ~60 API calls, inside FMP's free daily quota.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import pandas as pd
from sqlalchemy import Engine, delete
from sqlalchemy.orm import Session

from pit.config import AppConfig, Settings
from pit.data.fmp import FMPClient
from pit.data.fred import FREDClient
from pit.data.http import DataSourceError
from pit.data.synthetic import generate_market
from pit.db import init_db, log_event, session_scope
from pit.db.models import (
    AssetType,
    BenchmarkWeight,
    DataSource,
    Price,
    Rate,
    Security,
    TargetWeight,
)
from pit.db.repository import price_coverage, purge_market_data_not_from, upsert

INDEX_TICKER = "SPX"
WEIGHT_SUM_TOLERANCE = 1e-6


@dataclass
class LoadSummary:
    mode: str
    start: date
    end: date
    securities: int = 0
    prices: int = 0
    rates: int = 0
    benchmark_weights: int = 0
    target_weights: int = 0
    api_calls: int = 0
    purged_rows: int = 0
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


# --------------------------------------------------------------------------- input files


def read_target_weights(path: Path) -> pd.DataFrame:
    """Read and validate the PM's model: unique tickers, weights >= 0, sum = 1."""
    df = pd.read_csv(path)
    missing = {"ticker", "target_weight"} - set(df.columns)
    if missing:
        raise ValueError(f"{path.name}: missing columns {sorted(missing)}")
    df["ticker"] = df["ticker"].astype(str).str.strip().str.upper()
    if df["ticker"].duplicated().any():
        dupes = df.loc[df["ticker"].duplicated(), "ticker"].tolist()
        raise ValueError(f"{path.name}: duplicated tickers {dupes}")
    if (df["target_weight"] < 0).any():
        raise ValueError(f"{path.name}: negative weights are not allowed (long-only fund)")
    total = float(df["target_weight"].sum())
    if abs(total - 1.0) > WEIGHT_SUM_TOLERANCE:
        raise ValueError(f"{path.name}: weights sum to {total:.8f}, expected 1.0")
    return df[["ticker", "target_weight"]]


def read_universe(path: Path) -> pd.DataFrame:
    df = pd.read_csv(path)
    missing = {"ticker", "name", "sector"} - set(df.columns)
    if missing:
        raise ValueError(f"{path.name}: missing columns {sorted(missing)}")
    return df[["ticker", "name", "sector"]]


def _store_target_weights(session: Session, targets: pd.DataFrame, as_of: date) -> int:
    session.execute(delete(TargetWeight).where(TargetWeight.as_of == as_of))
    rows = [
        {"as_of": as_of, "ticker": t, "weight": float(w)}
        for t, w in zip(targets["ticker"], targets["target_weight"], strict=True)
    ]
    return upsert(session, TargetWeight, rows)


def _benchmark_rows(caps: pd.DataFrame, source: DataSource) -> list[dict[str, Any]]:
    """weight_i = market_cap_i / sum(market_cap), one snapshot per date."""
    out: list[dict[str, Any]] = []
    for snap_date, grp in caps.groupby("date"):
        total = grp["market_cap"].sum()
        for t, cap in zip(grp["ticker"], grp["market_cap"], strict=True):
            out.append(
                {
                    "date": snap_date,
                    "ticker": t,
                    "weight": float(cap / total),
                    "market_cap": float(cap),
                    "source": source.value,
                }
            )
    return out


# --------------------------------------------------------------------------- offline


def load_offline(engine: Engine, cfg: AppConfig, start: date, end: date) -> LoadSummary:
    summary = LoadSummary(mode="offline", start=start, end=end)
    targets = read_target_weights(cfg.resolve_path(cfg.data.target_weights_file))
    universe = read_universe(cfg.resolve_path(cfg.data.offline_universe_file))
    not_in_universe = sorted(set(targets["ticker"]) - set(universe["ticker"]))
    if not_in_universe:
        raise ValueError(f"model tickers missing from the offline universe: {not_in_universe}")

    mkt = generate_market(
        universe, start, end, cfg.data.synthetic_seed, cfg.data.synthetic_risk_free_rate
    )
    src = DataSource.SYNTHETIC.value

    init_db(engine)
    with session_scope(engine) as s:
        summary.purged_rows = purge_market_data_not_from(s, keep=[DataSource.SYNTHETIC])
        sec_rows = [
            {
                "ticker": r.ticker,
                "name": r.name,
                "sector": r.sector,
                "asset_type": AssetType.EQUITY.value,
                "source": src,
            }
            for r in universe.itertuples()
        ]
        sec_rows.append(
            {
                "ticker": INDEX_TICKER,
                "name": "S&P 500 Index (synthetic)",
                "sector": None,
                "asset_type": AssetType.INDEX.value,
                "source": src,
            }
        )
        summary.securities = upsert(s, Security, sec_rows)

        px = mkt.prices.assign(source=src).to_dict("records")
        idx = mkt.index_levels.assign(ticker=INDEX_TICKER, volume=None, source=src)
        summary.prices = upsert(s, Price, px) + upsert(s, Price, idx.to_dict("records"))

        rates = mkt.rates.assign(series=cfg.data.risk_free_series, source=src)
        summary.rates = upsert(s, Rate, rates.to_dict("records"))
        summary.benchmark_weights = upsert(
            s, BenchmarkWeight, _benchmark_rows(mkt.market_caps, DataSource.SYNTHETIC)
        )
        summary.target_weights = _store_target_weights(s, targets, start)
        summary.notes.append("SYNTHETIC data - generated offline, not real market prices")
        log_event(s, "DATA_LOADED", summary.as_dict())
    return summary


# --------------------------------------------------------------------------- online


def _missing_range(
    coverage: tuple[date, date] | None, start: date, end: date
) -> tuple[date, date] | None:
    """Date range still to download given what is cached.

    Only the tail after the last cached date is requested; if the cache starts
    after ``start`` the whole range is re-requested (upsert makes that safe).
    """
    if coverage is None or coverage[0] > start:
        return (start, end)
    if coverage[1] >= end:
        return None
    return (coverage[1] + timedelta(days=1), end)


def load_online(
    engine: Engine,
    cfg: AppConfig,
    fmp: FMPClient,
    fred: FREDClient,
    start: date,
    end: date,
) -> LoadSummary:
    summary = LoadSummary(mode="online", start=start, end=end)
    targets = read_target_weights(cfg.resolve_path(cfg.data.target_weights_file))
    init_db(engine)

    # ---- 1. fetch everything (no writes yet)
    constituents = fmp.sp500_constituents()
    not_in_index = sorted(set(targets["ticker"]) - set(constituents["ticker"]))
    if not_in_index:
        summary.notes.append(f"model tickers not in the S&P 500 list: {not_in_index}")
    caps = fmp.market_caps(constituents["ticker"].tolist())

    to_fetch: dict[str, pd.DataFrame] = {}
    with session_scope(engine) as s:
        coverage = {
            t: price_coverage(s, t, sources=[DataSource.FMP])
            for t in [*targets["ticker"], INDEX_TICKER]
        }
    for ticker, cov in coverage.items():
        rng = _missing_range(cov, start, end)
        if rng is None:
            continue
        if ticker == INDEX_TICKER:
            df = fmp.historical_prices(cfg.data.index_symbol, *rng, adjusted=False)
        else:
            df = fmp.historical_prices(ticker, *rng)
        to_fetch[ticker] = df
    cached = len(coverage) - len(to_fetch)
    if cached:
        summary.notes.append(f"{cached} tickers already cached up to {end}")

    rf = fred.series(cfg.data.risk_free_series, start, end)

    # ---- 2. write in a single transaction
    fmp_src, fred_src = DataSource.FMP.value, DataSource.FRED.value
    with session_scope(engine) as s:
        summary.purged_rows = purge_market_data_not_from(s, keep=[DataSource.FMP, DataSource.FRED])
        sec_rows = [
            {
                "ticker": r.ticker,
                "name": r.name,
                "sector": r.sector,
                "asset_type": AssetType.EQUITY.value,
                "source": fmp_src,
            }
            for r in constituents.itertuples()
        ]
        sec_rows.append(
            {
                "ticker": INDEX_TICKER,
                "name": "S&P 500 Index",
                "sector": None,
                "asset_type": AssetType.INDEX.value,
                "source": fmp_src,
            }
        )
        summary.securities = upsert(s, Security, sec_rows)

        for ticker, df in to_fetch.items():
            rows = df.assign(ticker=ticker, source=fmp_src).to_dict("records")
            summary.prices += upsert(s, Price, rows)

        rate_rows = rf.assign(series=cfg.data.risk_free_series, source=fred_src)
        summary.rates = upsert(s, Rate, rate_rows.to_dict("records"))
        summary.benchmark_weights = upsert(
            s, BenchmarkWeight, _benchmark_rows(caps, DataSource.FMP)
        )
        summary.target_weights = _store_target_weights(s, targets, start)
        summary.api_calls = fmp.http.calls + fred.http.calls
        log_event(s, "DATA_LOADED", summary.as_dict())
    return summary


# --------------------------------------------------------------------------- entry point


def load_data(
    engine: Engine,
    cfg: AppConfig,
    settings: Settings,
    start: date | None = None,
    end: date | None = None,
    offline: bool = False,
    fallback: bool = True,
    fmp: FMPClient | None = None,
    fred: FREDClient | None = None,
) -> LoadSummary:
    """Load data in the requested mode.

    Without API keys, or if the providers fail and ``fallback`` is on, the
    offline synthetic dataset is loaded instead, and the reason is recorded in
    the audit log and in the summary notes.
    """
    start = start or cfg.data.history_start
    end = end or date.today()
    if start >= end:
        raise ValueError(f"start ({start}) must be before end ({end})")

    if offline:
        return load_offline(engine, cfg, start, end)

    if fmp is None or fred is None:
        if not settings.has_market_data_keys:
            summary = load_offline(engine, cfg, start, end)
            summary.notes.insert(0, "no FMP/FRED API keys found -> offline mode")
            return summary
        fmp = fmp or FMPClient.from_key(settings.fmp_api_key or "", cfg.data.fmp_base_url)
        fred = fred or FREDClient.from_key(settings.fred_api_key or "", cfg.data.fred_base_url)

    try:
        return load_online(engine, cfg, fmp, fred, start, end)
    except DataSourceError as exc:
        init_db(engine)
        with session_scope(engine) as s:
            log_event(s, "DATA_SOURCE_ERROR", {"error": str(exc), "fallback": fallback})
        if not fallback:
            raise
        summary = load_offline(engine, cfg, start, end)
        summary.notes.insert(0, f"online load failed ({exc}) -> offline mode")
        return summary
