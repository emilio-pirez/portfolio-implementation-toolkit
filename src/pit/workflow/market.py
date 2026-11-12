"""In-memory market data for the workflow and the backtest.

Everything the daily process needs is read from the database once and then
served by date, so a backtest of hundreds of days does not hit the database
for every step.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import cast

import pandas as pd
from sqlalchemy import Engine, select

from pit.compliance.pretrade import (
    ComplianceInputs,
    load_benchmark_weights,
    load_sectors,
    read_restricted_list,
)
from pit.config import AppConfig
from pit.db import session_scope
from pit.db.models import DataSource, Price, Rate, TargetWeight
from pit.equitization.futures import FuturesQuote, fair_value, year_fraction
from pit.portfolio.store import INDEX_TICKER, MarketSnapshot
from pit.risk.model import RiskModel


@dataclass
class MarketData:
    cfg: AppConfig
    closes: pd.DataFrame  # DatetimeIndex x stock tickers
    volumes: pd.DataFrame
    index_levels: pd.Series  # SPX close
    rates: pd.Series  # risk-free, decimal
    targets: dict[date, pd.Series]  # model weights by effective date
    sectors: pd.Series
    benchmark: pd.Series
    restricted: frozenset[str]
    sources: frozenset[str]
    _risk_cache: dict[date, RiskModel] = field(default_factory=dict, repr=False)
    _adv: pd.DataFrame | None = field(default=None, repr=False)

    # ------------------------------------------------------------------ loading

    @classmethod
    def load(cls, engine: Engine, cfg: AppConfig) -> MarketData:
        with session_scope(engine) as s:
            px = pd.DataFrame(
                s.execute(
                    select(Price.date, Price.ticker, Price.close, Price.volume, Price.source)
                ).all(),
                columns=["date", "ticker", "close", "volume", "source"],
            )
            if px.empty:
                raise LookupError("no prices in the database; run `pit load-data` first")
            rates = pd.DataFrame(
                s.execute(
                    select(Rate.date, Rate.value).where(Rate.series == cfg.data.risk_free_series)
                ).all(),
                columns=["date", "value"],
            )
            tw = pd.DataFrame(
                s.execute(
                    select(TargetWeight.as_of, TargetWeight.ticker, TargetWeight.weight)
                ).all(),
                columns=["as_of", "ticker", "weight"],
            )
            sectors = load_sectors(s)
            benchmark = load_benchmark_weights(s, date.max)

        px["date"] = pd.to_datetime(px["date"])
        closes = px.pivot(index="date", columns="ticker", values="close").sort_index()
        volumes = px.pivot(index="date", columns="ticker", values="volume").sort_index()
        index_levels = closes.pop(INDEX_TICKER)
        volumes = volumes.drop(columns=INDEX_TICKER, errors="ignore")
        rate_series = pd.Series(
            rates["value"].to_numpy(), index=pd.to_datetime(rates["date"]), dtype=float
        ).sort_index()
        targets = {
            cast(date, d): g.set_index("ticker")["weight"].astype(float)
            for d, g in tw.groupby("as_of")
        }
        return cls(
            cfg=cfg,
            closes=closes,
            volumes=volumes,
            index_levels=index_levels.dropna(),
            rates=rate_series,
            targets=targets,
            sectors=sectors,
            benchmark=benchmark,
            restricted=read_restricted_list(cfg.resolve_path(cfg.data.restricted_list_file)),
            sources=frozenset(px["source"].unique()),
        )

    @property
    def is_synthetic(self) -> bool:
        return DataSource.SYNTHETIC.value in self.sources

    # ------------------------------------------------------------------ calendar

    def trading_days(self, start: date, end: date) -> list[date]:
        idx = self.index_levels.loc[pd.Timestamp(start) : pd.Timestamp(end)].index
        return [d.date() for d in idx]

    def is_trading_day(self, d: date) -> bool:
        return pd.Timestamp(d) in self.index_levels.index

    def trading_days_between(self, start: date, end: date) -> int:
        """Trading days after ``start`` up to and including ``end``."""
        idx = self.index_levels.index
        return int(((idx > pd.Timestamp(start)) & (idx <= pd.Timestamp(end))).sum())

    # ------------------------------------------------------------------ point-in-time data

    def rate(self, d: date) -> float:
        r = self.rates.loc[: pd.Timestamp(d)]
        if r.empty:
            raise LookupError(f"no risk-free rate on or before {d}")
        return float(r.iloc[-1])

    def snapshot(self, d: date) -> MarketSnapshot:
        if not self.is_trading_day(d):
            raise LookupError(f"{d} is not a trading day in the price history")
        ts = pd.Timestamp(d)
        return MarketSnapshot(
            as_of=d,
            prices=cast(pd.Series, self.closes.loc[ts]).dropna(),
            index_level=float(self.index_levels.loc[ts]),
            risk_free=self.rate(d),
        )

    def quote(self, d: date) -> FuturesQuote:
        return self.snapshot(d).futures_quote(self.cfg)

    def futures_price(self, d: date, expiry: date) -> float:
        """Fair value on ``d`` of the contract expiring on ``expiry`` (S at expiry)."""
        spot = float(self.index_levels.loc[pd.Timestamp(d)])
        t = year_fraction(d, expiry)
        return fair_value(spot, self.rate(d), self.cfg.futures.index_dividend_yield, t)

    def target_weights(self, d: date) -> pd.Series:
        eligible = [k for k in self.targets if k <= d]
        if not eligible:
            raise LookupError(f"no target weights effective on {d}")
        return self.targets[max(eligible)]

    def adv(self, d: date) -> pd.Series:
        if self._adv is None:
            window = self.cfg.orders.adv_window_days
            self._adv = self.volumes.rolling(window, min_periods=1).mean()
        row = self._adv.loc[: pd.Timestamp(d)]
        return row.iloc[-1].dropna() if not row.empty else pd.Series(dtype=float)

    def risk_model(self, d: date, tickers: list[str] | None = None) -> RiskModel:
        if d not in self._risk_cache:
            cols = tickers or list(self.target_weights(d).index)
            cols = [c for c in cols if c in self.closes.columns]
            self._risk_cache[d] = RiskModel.fit(self.closes[cols], self.index_levels, d, self.cfg)
        return self._risk_cache[d]

    def compliance_inputs(self, d: date, held: list[str] | None = None) -> ComplianceInputs:
        universe = sorted(set(self.target_weights(d).index) | set(held or []))
        return ComplianceInputs(
            sectors=self.sectors,
            benchmark_weights=self.benchmark,
            restricted=self.restricted,
            adv=self.adv(d),
            risk_model=self.risk_model(d, universe),
        )
