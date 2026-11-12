"""Cash drag: the return a fund gives up by holding cash instead of the market.

Daily, for the cash that is *not* equitized with futures::

    drag_t = unequitized cash %_t x (r_benchmark,t - r_f,t)

* ``r_benchmark`` is the benchmark **total** return. SPX is a price index, so
  the dividend yield is added back: r_bench = SPX return + q / 252.
* ``r_f`` is what the cash earns (T-Bill rate x days / 365).

In a rising market the drag is positive (the fund lags); in a falling market
it is negative (cash cushions the fall). Over time equity returns exceed
T-Bills, so the expected drag is positive: roughly cash % x equity risk premium.
For example, 3 % cash in a year where stocks return 25 % and T-Bills 5 % costs
about 0.03 x 20 % = 60 bp.
"""

from __future__ import annotations

import pandas as pd

TRADING_DAYS = 252


def benchmark_total_return(index_returns: pd.Series, dividend_yield: float) -> pd.Series:
    """Daily total return of the benchmark: price return + q / 252."""
    return index_returns + dividend_yield / TRADING_DAYS


def daily_cash_drag(
    unequitized_pct: pd.Series, bench_total_return: pd.Series, rf_daily: pd.Series
) -> pd.Series:
    """Per-day drag (decimal). Inputs are aligned on their common dates.

    The cash held at the close of t-1 earns (or misses) day t's return, so
    the cash weight is lagged by one day.
    """
    df = pd.concat(
        {"cash": unequitized_pct.shift(1), "bench": bench_total_return, "rf": rf_daily}, axis=1
    ).dropna()
    return (df["cash"] * (df["bench"] - df["rf"])).rename("cash_drag")


def cumulative_drag_bps(drag: pd.Series) -> float:
    """Total drag over the period in basis points (sum of daily drags)."""
    return float(drag.sum() * 10_000)


def annualized_drag_bps(drag: pd.Series) -> float:
    if drag.empty:
        return 0.0
    return float(drag.mean() * TRADING_DAYS * 10_000)
