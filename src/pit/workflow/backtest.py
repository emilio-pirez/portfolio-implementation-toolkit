"""Backtest: the same daily workflow over a period, with and without equitization.

Both scenarios start from the same fund, see the same client flows, and run
the same basket and compliance logic. The only difference is
``equitization.enabled``. In the "without" scenario, subscription cash waits
in cash until the stock basket is filled the next day, and residual cash
(below the no-trade band) is never covered. The gap between the two is the
value of equitization.

SOFT compliance alerts are auto-approved in the backtest (and counted), since
nobody is there to approve them. HARD breaches still block baskets.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date

import numpy as np
import pandas as pd
from sqlalchemy import Engine

from pit.config import AppConfig
from pit.db import log_event, session_scope
from pit.db.models import BacktestDaily, OrderStatus
from pit.db.repository import upsert
from pit.risk.cash_drag import (
    TRADING_DAYS,
    annualized_drag_bps,
    benchmark_total_return,
    cumulative_drag_bps,
    daily_cash_drag,
)
from pit.workflow.day import ReviewRecorded, StatusChange, step_day
from pit.workflow.flows import Flow, by_date
from pit.workflow.market import MarketData

WITH, WITHOUT = "with_equitization", "without_equitization"


@dataclass(frozen=True)
class BacktestResult:
    run_id: str
    start: date
    end: date
    daily: dict[str, pd.DataFrame]  # scenario -> daily series
    summary: pd.DataFrame  # metric x scenario
    flows: list[Flow]
    synthetic: bool


def _simulate(
    md: MarketData, cfg: AppConfig, days: list[date], flows: dict[date, list[Flow]]
) -> tuple[pd.DataFrame, dict[str, int]]:
    state, pending = None, None
    rows, counts = [], {"baskets": 0, "approved": 0, "soft_approved": 0, "rejected": 0, "filled": 0}
    for d in days:
        todays = flows.get(d, [])
        res = step_day(state, md, d, todays, pending, cfg, auto_approve_soft=True)
        for a in res.actions:
            if isinstance(a, ReviewRecorded):
                counts["baskets"] += 1
                st = a.review.status
                counts["approved"] += st is OrderStatus.APPROVED
                counts["rejected"] += st is OrderStatus.REJECTED
            elif isinstance(a, StatusChange):
                counts["soft_approved"] += a.status is OrderStatus.APPROVED
                counts["filled"] += a.status is OrderStatus.FILLED
        s = res.state
        rows.append(
            {
                "date": d,
                "nav": s.nav,
                "flow": sum(f.signed for f in todays),
                "cash_pct": s.cash_pct,
                "unequitized_pct": s.unequitized_cash / s.nav,
                "exposure": s.exposure,
                "futures_notional": s.futures_notional,
                "hard_breaches": sum(c.is_hard_breach for c in res.post_trade),
                "soft_breaches": sum(c.is_soft_breach for c in res.post_trade),
            }
        )
        state, pending = res.state, res.pending
    return pd.DataFrame(rows).set_index("date"), counts


def _add_returns(df: pd.DataFrame, md: MarketData, cfg: AppConfig) -> pd.DataFrame:
    """Flow-adjusted fund return, benchmark total return and daily cash drag."""
    out = df.copy()
    prev_nav = out["nav"].shift(1)
    out["fund_return"] = (out["nav"] - out["flow"]) / prev_nav - 1.0
    idx = md.index_levels.copy()
    idx.index = pd.DatetimeIndex(idx.index).date
    spx = idx.reindex(out.index)
    out["benchmark_return"] = benchmark_total_return(
        spx.pct_change(), cfg.futures.index_dividend_yield
    )
    dates = pd.Series(out.index, index=out.index)
    gap = dates.diff().map(lambda x: x.days if pd.notna(x) else np.nan)
    rf = pd.Series([md.rate(d) for d in out.index], index=out.index).shift(1) * gap / 365.0
    out["rf_return"] = rf
    out["cash_drag"] = daily_cash_drag(
        out["unequitized_pct"], out["benchmark_return"], out["rf_return"]
    ).reindex(out.index)
    return out


def _metrics(df: pd.DataFrame, counts: dict[str, int]) -> dict[str, float]:
    r = df["fund_return"].dropna()
    b = df["benchmark_return"].reindex(r.index)
    n = len(r)

    def ann(x: pd.Series) -> float:
        growth = float(np.prod(1.0 + x.to_numpy()))
        return growth ** (TRADING_DAYS / n) - 1 if n else 0.0

    fund_ann, bench_ann = ann(r), ann(b)
    years = n / TRADING_DAYS if n else 1.0
    rf_ann = float(df["rf_return"].dropna().sum() / years)
    avg_uneq = float(df["unequitized_pct"].mean())
    return {
        "Annualized return": fund_ann,
        "Benchmark annualized return": bench_ann,
        "Tracking difference (bp/yr)": (fund_ann - bench_ann) * 10_000,
        "Realized tracking error": float((r - b).std() * np.sqrt(TRADING_DAYS)),
        "Avg cash % of NAV": float(df["cash_pct"].mean()),
        "Avg un-equitized cash %": avg_uneq,
        "Max un-equitized cash %": float(df["unequitized_pct"].max()),
        "Avg total exposure": float(df["exposure"].mean()),
        "Cash drag, cumulative (bp)": cumulative_drag_bps(df["cash_drag"].dropna()),
        "Cash drag, annualized (bp)": annualized_drag_bps(df["cash_drag"].dropna()),
        # Less noisy: average idle cash x realized equity premium over the period.
        "Cash drag, expected (bp/yr)": avg_uneq * (bench_ann - rf_ann) * 10_000,
        "Baskets generated": counts["baskets"],
        "Baskets rejected (HARD)": counts["rejected"],
        "Baskets with SOFT alerts (auto-approved)": counts["soft_approved"],
        "Days with post-trade HARD breach": int((df["hard_breaches"] > 0).sum()),
        "Days with post-trade SOFT alert": int((df["soft_breaches"] > 0).sum()),
    }


def run_backtest(
    md: MarketData, cfg: AppConfig, start: date, end: date, flows: list[Flow]
) -> BacktestResult:
    days = md.trading_days(start, end)
    if len(days) < 2:
        raise ValueError(f"need at least two trading days between {start} and {end}")
    fl = by_date([f for f in flows if start <= f.date <= end])
    no_eq = cfg.model_copy(
        update={"equitization": cfg.equitization.model_copy(update={"enabled": False})}
    )
    daily, metrics = {}, {}
    for name, scenario_cfg in ((WITH, cfg), (WITHOUT, no_eq)):
        df, counts = _simulate(md, scenario_cfg, days, fl)
        daily[name] = _add_returns(df, md, cfg)
        metrics[name] = _metrics(daily[name], counts)
    summary = pd.DataFrame(metrics)
    summary["difference"] = summary[WITH] - summary[WITHOUT]
    return BacktestResult(
        run_id=str(uuid.uuid4()),
        start=days[0],
        end=days[-1],
        daily=daily,
        summary=summary,
        flows=[f for d in sorted(fl) for f in fl[d]],
        synthetic=md.is_synthetic,
    )


def save_backtest(engine: Engine, cfg: AppConfig, result: BacktestResult) -> tuple[int, str]:
    """Persist daily rows + an audit event; write CSVs. Returns (rows, output folder)."""
    rows = []
    for scenario, df in result.daily.items():
        for d, r in df.iterrows():
            rows.append(
                {
                    "run_id": result.run_id,
                    "scenario": scenario,
                    "date": d,
                    **{
                        k: (None if pd.isna(r[k]) else float(r[k]))
                        for k in (
                            "nav",
                            "flow",
                            "fund_return",
                            "benchmark_return",
                            "cash_pct",
                            "unequitized_pct",
                            "exposure",
                            "futures_notional",
                            "cash_drag",
                        )
                    },
                    "hard_breaches": int(r["hard_breaches"]),
                    "soft_breaches": int(r["soft_breaches"]),
                }
            )
    out_dir = cfg.resolve_path(cfg.workflow.outputs_dir) / f"backtest_{result.run_id[:8]}"
    out_dir.mkdir(parents=True, exist_ok=True)
    for scenario, df in result.daily.items():
        df.to_csv(out_dir / f"daily_{scenario}.csv")
    result.summary.to_csv(out_dir / "summary.csv")
    with session_scope(engine) as s:
        upsert(s, BacktestDaily, rows)
        log_event(
            s,
            "BACKTEST_COMPLETED",
            {
                "run_id": result.run_id,
                "start": result.start,
                "end": result.end,
                "flows": len(result.flows),
                "synthetic_data": result.synthetic,
                "summary": result.summary.to_dict(),
                "output_dir": str(out_dir),
            },
        )
    return len(rows), str(out_dir)
