from datetime import date

import numpy as np
import pandas as pd
import pytest

from pit.config import AppConfig
from pit.portfolio.state import FuturesPosition, PortfolioState
from pit.risk import RiskModel, betas, covariance, daily_returns
from pit.risk.cash_drag import (
    annualized_drag_bps,
    benchmark_total_return,
    cumulative_drag_bps,
    daily_cash_drag,
)

DAYS = pd.bdate_range("2023-01-02", periods=300)
D = DAYS[-1].date()


def _market(seed: int = 0) -> tuple[pd.DataFrame, pd.Series]:
    """AAA = 1.2 x index + noise, BBB = 0.8 x index + noise."""
    rng = np.random.default_rng(seed)
    m = rng.normal(0.0004, 0.01, len(DAYS))
    a = 1.2 * m + rng.normal(0, 0.005, len(DAYS))
    b = 0.8 * m + rng.normal(0, 0.005, len(DAYS))
    level = lambda r: 100 * np.cumprod(1 + r)  # noqa: E731
    closes = pd.DataFrame({"AAA": level(a), "BBB": level(b)}, index=DAYS)
    return closes, pd.Series(level(m), index=DAYS)


def _state(shares: dict[str, float], cash: float, es: int = 0) -> PortfolioState:
    closes, _ = _market()
    futures = (
        (FuturesPosition("ESM6", "ES", date(2026, 6, 19), 50.0, es, 1_000.0, 1_000.0),)
        if es
        else ()
    )
    return PortfolioState(D, pd.Series(shares), closes.iloc[-1], cash, futures)


def test_daily_returns_window() -> None:
    closes, _ = _market()
    r = daily_returns(closes, D, 252)
    assert len(r) == 252 and r.index[-1].date() == D


def test_betas_recovered(cfg: AppConfig) -> None:
    closes, idx = _market()
    r = daily_returns(closes.join(idx.rename("SPX")), D, 252)
    b = betas(r)
    assert b["AAA"] == pytest.approx(1.2, abs=0.08)
    assert b["BBB"] == pytest.approx(0.8, abs=0.08)


def test_ledoit_wolf_shrinks_toward_identity() -> None:
    closes, idx = _market()
    r = daily_returns(closes.join(idx.rename("SPX")), D, 252)
    sample, lw = covariance(r, "none"), covariance(r, "ledoit_wolf")
    assert abs(lw.loc["AAA", "BBB"]) <= abs(sample.loc["AAA", "BBB"])
    with pytest.raises(ValueError):
        covariance(r, "magic")


def test_fully_in_futures_has_zero_te_and_beta_one(cfg: AppConfig) -> None:
    closes, idx = _market()
    rm = RiskModel.fit(closes, idx, D, cfg)
    # NAV 1,000,000 all cash, 20 ES x 1,000 x 50 = 1,000,000 notional
    s = _state({}, 1_000_000.0, es=20)
    assert rm.tracking_error(s) == pytest.approx(0.0, abs=1e-12)
    assert rm.beta(s) == pytest.approx(1.0)


def test_single_stock_te_by_hand(cfg: AppConfig) -> None:
    closes, idx = _market()
    rm = RiskModel.fit(closes, idx, D, cfg)
    price = closes["AAA"].iloc[-1]
    s = _state({"AAA": 1_000_000 / price}, 0.0)  # 100 % in AAA, nothing else
    c = rm.cov
    # a = [1 (AAA), -1 (SPX)] -> var = s_AA + s_MM - 2 s_AM
    var = c.loc["AAA", "AAA"] + c.loc["SPX", "SPX"] - 2 * c.loc["AAA", "SPX"]
    assert rm.tracking_error(s) == pytest.approx(np.sqrt(var * 252))
    assert rm.beta(s) == pytest.approx(rm.betas["AAA"])
    te, beta = rm(s)
    assert te > 0 and beta > 1


def test_cash_lowers_beta_and_adds_te(cfg: AppConfig) -> None:
    closes, idx = _market()
    rm = RiskModel.fit(closes, idx, D, cfg)
    s = _state({}, 1_000_000.0, es=19)  # 95 % in futures, 5 % idle cash
    assert rm.beta(s) == pytest.approx(0.95)
    expected = 0.05 * np.sqrt(rm.cov.loc["SPX", "SPX"] * 252)
    assert rm.tracking_error(s) == pytest.approx(expected)


def test_missing_history_raises(cfg: AppConfig) -> None:
    closes, idx = _market()
    rm = RiskModel.fit(closes[["AAA"]], idx, D, cfg)
    with pytest.raises(ValueError, match="BBB"):
        rm.beta(_state({"BBB": 10.0}, 0.0))
    with pytest.raises(ValueError, match="history"):
        RiskModel.fit(closes.iloc[:5], idx, DAYS[4].date(), cfg)


def test_cash_drag_by_hand() -> None:
    idx = pd.date_range("2024-01-01", periods=3)
    cash = pd.Series([0.05, 0.05, 0.0], index=idx)
    bench = pd.Series([0.0, 0.02, -0.01], index=idx)
    rf = pd.Series([0.0, 0.0002, 0.0002], index=idx)
    drag = daily_cash_drag(cash, bench, rf)
    # day 2: 5 % x (2 % - 0.02 %) = 9.9 bp ; day 3: 5 % x (-1 % - 0.02 %) = -5.1 bp
    assert drag.tolist() == pytest.approx([0.00099, -0.00051])
    assert cumulative_drag_bps(drag) == pytest.approx(4.8)
    assert annualized_drag_bps(drag) == pytest.approx(4.8 / 2 * 252)
    assert annualized_drag_bps(pd.Series(dtype=float)) == 0.0
    tr = benchmark_total_return(pd.Series([0.01]), 0.0252)
    assert tr.iloc[0] == pytest.approx(0.0101)
