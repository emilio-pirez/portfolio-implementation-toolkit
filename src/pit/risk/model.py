"""Ex-ante risk model: betas, covariance and tracking error.

**Beta** of stock i vs. the S&P 500, from daily returns over the lookback::

    beta_i = cov(r_i, r_SPX) / var(r_SPX)

Portfolio beta = sum(w_i x beta_i) + futures exposure x 1, with weights as
fractions of NAV (cash has beta 0, index futures have beta 1).

**Ex-ante tracking error.** The benchmark is treated as one more asset (the
index itself), so the covariance matrix only needs the stocks the fund can
hold plus SPX::

    a      = [w_1, ..., w_n, w_SPX]      active weights, as fractions of NAV
    w_SPX  = futures notional / NAV - 1  (long the index via futures, short the benchmark)
    TE     = sqrt(a' S a) x sqrt(252)

A fund fully in index futures has a = 0 and TE = 0. A fund with 0.5 % cash has
TE from that 0.5 % "short index" position.

**Covariance.** The sample covariance of 57 assets over 252 days is noisy
(57 x 58 / 2 = 1,653 parameters from 252 observations). Ledoit-Wolf shrinkage
pulls it toward a scaled identity by the optimal amount, which gives better
conditioned, more stable estimates (``risk.covariance_shrinkage``).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

import numpy as np
import pandas as pd
from sklearn.covariance import ledoit_wolf

from pit.config import AppConfig
from pit.portfolio.state import PortfolioState

INDEX = "SPX"


def daily_returns(closes: pd.DataFrame, as_of: date, lookback: int) -> pd.DataFrame:
    """Simple daily returns for the ``lookback`` days ending on ``as_of``."""
    window = closes.loc[: pd.Timestamp(as_of)].tail(lookback + 1)
    return window.pct_change().iloc[1:]


def betas(returns: pd.DataFrame, index_col: str = INDEX) -> pd.Series:
    """OLS beta of every column against ``index_col``."""
    m = returns[index_col]
    var_m = m.var()
    if not var_m > 0:
        raise ValueError("index returns have zero variance")
    cov = returns.drop(columns=index_col).apply(lambda c: c.cov(m))
    return (cov / var_m).rename("beta")


def covariance(returns: pd.DataFrame, shrinkage: str = "ledoit_wolf") -> pd.DataFrame:
    """Daily covariance matrix, optionally Ledoit-Wolf shrunk."""
    clean = returns.dropna()
    if shrinkage == "ledoit_wolf":
        cov, _ = ledoit_wolf(clean.to_numpy())
    elif shrinkage == "none":
        cov = np.cov(clean.to_numpy(), rowvar=False)
    else:
        raise ValueError(f"unknown shrinkage {shrinkage!r}")
    return pd.DataFrame(cov, index=clean.columns, columns=clean.columns)


@dataclass(frozen=True)
class RiskModel:
    """Fitted risk model; call it with a portfolio to get (TE, beta)."""

    as_of: date
    cov: pd.DataFrame  # daily covariance, stocks + SPX
    betas: pd.Series  # stock betas vs SPX
    annualization: int = 252

    @classmethod
    def fit(
        cls,
        closes: pd.DataFrame,
        index_levels: pd.Series,
        as_of: date,
        cfg: AppConfig,
    ) -> RiskModel:
        """Estimate from wide closes (stocks) and the index level series."""
        data = closes.join(index_levels.rename(INDEX), how="inner")
        rets = daily_returns(data, as_of, cfg.risk.lookback_days)
        # A stock needs (almost) full history to enter the model.
        min_obs = int(0.9 * len(rets))
        rets = rets.loc[:, rets.notna().sum() >= min_obs]
        if INDEX not in rets.columns or len(rets) < 20:
            raise ValueError(f"not enough return history before {as_of} for the risk model")
        return cls(
            as_of=as_of,
            cov=covariance(rets, cfg.risk.covariance_shrinkage),
            betas=betas(rets.dropna()),
            annualization=cfg.risk.annualization_factor,
        )

    def _weights(self, state: PortfolioState) -> pd.Series:
        w = state.weights()
        missing = sorted(set(w.index) - set(self.betas.index))
        if missing:
            raise ValueError(f"no return history for held names: {missing}")
        return w

    def beta(self, state: PortfolioState) -> float:
        w = self._weights(state)
        futures_w = state.futures_notional / state.nav if state.nav else 0.0
        return float((w * self.betas.reindex(w.index)).sum() + futures_w)

    def active_weights(self, state: PortfolioState) -> pd.Series:
        w = self._weights(state)
        futures_w = state.futures_notional / state.nav if state.nav else 0.0
        a = w.reindex(self.cov.index, fill_value=0.0)
        a[INDEX] = futures_w - 1.0
        return a

    def tracking_error(self, state: PortfolioState) -> float:
        a = self.active_weights(state).to_numpy()
        var = float(a @ self.cov.to_numpy() @ a)
        return float(np.sqrt(max(var, 0.0) * self.annualization))

    def __call__(self, state: PortfolioState) -> tuple[float, float]:
        return self.tracking_error(state), self.beta(state)
