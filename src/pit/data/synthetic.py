"""Synthetic market data for offline mode.

Everything produced here is fake and is stored with ``source = 'SYNTHETIC'``.
It exists only so that the project runs end-to-end without API keys.

Return model (daily log returns, one-factor + sector structure)::

    r_m,t      = mu_m dt + sigma_m sqrt(dt) z_m,t                     market factor (= index)
    r_s,t      = sigma_s sqrt(dt) z_s,t                               sector factor
    r_i,t      = beta_i r_m,t + r_s(i),t + sigma_i sqrt(dt) z_i,t - 1/2 var_i dt

Prices follow ``P_t = P_0 exp(cumsum r)`` - a correlated geometric Brownian
motion. Stocks in the same sector are more correlated than stocks in different
sectors, and every stock co-moves with the index through its beta, which is
what the beta and tracking-error calculations need.

Reproducibility: parameters and shocks come from generators seeded with
``seed``. Shocks are drawn row by row (one row per business day), so extending
the end date never changes the history already generated.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

import numpy as np
import pandas as pd

TRADING_DAYS = 252

# Annualised parameters of the synthetic market.
MARKET_DRIFT = 0.08
MARKET_VOL = 0.16
SECTOR_VOL = 0.08
IDIO_VOL_RANGE = (0.15, 0.30)
BETA_RANGE = (0.75, 1.25)
START_PRICE_RANGE = (30.0, 400.0)
INDEX_START_LEVEL = 5_000.0

# Market caps at the LAST simulated date follow a Zipf-like curve by position in
# the universe file (largest first), so benchmark weights look realistic no
# matter how the random price paths turned out. Daily dollar volume is a
# fraction of market cap.
LARGEST_CAP = 3.0e12
ZIPF_EXPONENT = 0.6
DAILY_TURNOVER = 0.004


@dataclass(frozen=True)
class SyntheticMarket:
    prices: pd.DataFrame  # long: ticker, date, close, volume
    index_levels: pd.DataFrame  # date, close
    market_caps: pd.DataFrame  # ticker, date, market_cap (snapshot at the last date)
    rates: pd.DataFrame  # date, value
    betas: pd.Series  # true betas used in the simulation (for tests / sanity checks)


def business_days(start: date, end: date) -> pd.DatetimeIndex:
    """Mon-Fri calendar. Exchange holidays are ignored (documented simplification)."""
    return pd.bdate_range(start, end)


def generate_market(
    universe: pd.DataFrame,
    start: date,
    end: date,
    seed: int,
    risk_free_rate: float = 0.04,
) -> SyntheticMarket:
    """Simulate prices, volumes, index levels, market caps and a flat T-Bill rate.

    ``universe`` needs columns ``ticker`` and ``sector``; row order is used as
    the size ranking for market caps.
    """
    dates = business_days(start, end)
    if len(dates) < 2:
        raise ValueError("synthetic history needs at least two business days")

    tickers = universe["ticker"].tolist()
    sectors = sorted(universe["sector"].unique())
    sector_idx = universe["sector"].map({s: k for k, s in enumerate(sectors)}).to_numpy()
    n_days, n_assets, n_sectors = len(dates), len(tickers), len(sectors)
    dt = 1.0 / TRADING_DAYS

    # --- static parameters (independent of the date range)
    prm = np.random.default_rng(seed)
    betas = prm.uniform(*BETA_RANGE, n_assets)
    idio_vol = prm.uniform(*IDIO_VOL_RANGE, n_assets)
    p0 = prm.uniform(*START_PRICE_RANGE, n_assets)
    rank = np.arange(1, n_assets + 1)
    cap_end = LARGEST_CAP / rank**ZIPF_EXPONENT * prm.lognormal(0.0, 0.10, n_assets)

    # --- shocks, one row per day: [market | sectors | idiosyncratic | volume noise]
    shocks = np.random.default_rng(seed + 1).standard_normal((n_days, 1 + n_sectors + 2 * n_assets))
    z_m = shocks[:, 0]
    z_s = shocks[:, 1 : 1 + n_sectors]
    z_i = shocks[:, 1 + n_sectors : 1 + n_sectors + n_assets]
    z_v = shocks[:, 1 + n_sectors + n_assets :]

    r_m = MARKET_DRIFT * dt + MARKET_VOL * np.sqrt(dt) * z_m
    r_s = SECTOR_VOL * np.sqrt(dt) * z_s
    total_var = (betas * MARKET_VOL) ** 2 + SECTOR_VOL**2 + idio_vol**2
    r_i = (
        np.outer(r_m, betas)
        + r_s[:, sector_idx]
        + idio_vol * np.sqrt(dt) * z_i
        - 0.5 * total_var * dt  # Ito correction so the drift is in arithmetic terms
    )
    # First day is the starting point: no return applied.
    r_i[0, :] = 0.0
    r_m_idx = r_m - 0.5 * MARKET_VOL**2 * dt
    r_m_idx[0] = 0.0

    closes = p0 * np.exp(np.cumsum(r_i, axis=0))
    index_close = INDEX_START_LEVEL * np.exp(np.cumsum(r_m_idx))

    # Constant share count, calibrated so that cap(last date) = cap_end.
    shares_out = cap_end / closes[-1]
    market_cap = closes * shares_out
    volume = shares_out * DAILY_TURNOVER * np.exp(0.3 * z_v - 0.5 * 0.3**2)

    prices = pd.DataFrame(
        {
            "ticker": np.repeat(np.array(tickers, dtype=object)[None, :], n_days, 0).ravel(),
            "date": np.repeat(dates.date, n_assets),
            "close": np.round(closes.ravel(), 4),
            "volume": np.round(volume.ravel()),
        }
    )
    index_levels = pd.DataFrame({"date": dates.date, "close": np.round(index_close, 2)})
    market_caps = pd.DataFrame(
        {"ticker": tickers, "date": dates[-1].date(), "market_cap": market_cap[-1]}
    )
    rates = pd.DataFrame({"date": dates.date, "value": risk_free_rate})
    return SyntheticMarket(
        prices=prices,
        index_levels=index_levels,
        market_caps=market_caps,
        rates=rates,
        betas=pd.Series(betas, index=tickers, name="beta"),
    )
