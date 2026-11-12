from datetime import date

import numpy as np
import pandas as pd
import pytest

from pit.data.synthetic import generate_market
from tests.conftest import INPUTS

START, END = date(2023, 12, 1), date(2024, 12, 31)


@pytest.fixture(scope="module")
def universe() -> pd.DataFrame:
    return pd.read_csv(INPUTS / "offline_universe.csv")


@pytest.fixture(scope="module")
def market(universe: pd.DataFrame):  # type: ignore[no-untyped-def]
    return generate_market(universe, START, END, seed=42)


def _wide(prices: pd.DataFrame) -> pd.DataFrame:
    return prices.pivot(index="date", columns="ticker", values="close")


def test_same_seed_is_reproducible(universe: pd.DataFrame, market) -> None:  # type: ignore[no-untyped-def]
    again = generate_market(universe, START, END, seed=42)
    pd.testing.assert_frame_equal(market.prices, again.prices)


def test_different_seed_differs(universe: pd.DataFrame, market) -> None:  # type: ignore[no-untyped-def]
    other = generate_market(universe, START, END, seed=7)
    assert not market.prices["close"].equals(other.prices["close"])


def test_extending_end_date_keeps_history(universe: pd.DataFrame, market) -> None:  # type: ignore[no-untyped-def]
    longer = generate_market(universe, START, date(2025, 6, 30), seed=42)
    a, b = _wide(market.prices), _wide(longer.prices).loc[_wide(market.prices).index]
    pd.testing.assert_frame_equal(a, b)


def test_prices_positive_and_complete(universe: pd.DataFrame, market) -> None:  # type: ignore[no-untyped-def]
    wide = _wide(market.prices)
    assert wide.shape == (len(pd.bdate_range(START, END)), len(universe))
    assert (wide > 0).all().all()
    assert market.prices["volume"].gt(0).all()
    assert not wide.isna().any().any()


def test_returns_are_correlated_and_betas_recoverable(market) -> None:  # type: ignore[no-untyped-def]
    rets = _wide(market.prices).pct_change().dropna()
    idx = market.index_levels.set_index("date")["close"].pct_change().dropna()
    est_beta = rets.apply(lambda c: np.cov(c, idx)[0, 1] / idx.var())
    assert np.corrcoef(est_beta, market.betas[est_beta.index])[0, 1] > 0.6
    pair_corr = rets.corr().to_numpy()[np.triu_indices(rets.shape[1], 1)].mean()
    assert 0.1 < pair_corr < 0.6


def test_benchmark_caps_realistic(market) -> None:  # type: ignore[no-untyped-def]
    caps = market.market_caps["market_cap"]
    weights = caps / caps.sum()
    assert weights.max() < 0.10
    assert market.market_caps["date"].nunique() == 1


def test_flat_risk_free(market) -> None:  # type: ignore[no-untyped-def]
    assert market.rates["value"].eq(0.04).all()


def test_needs_two_days(universe: pd.DataFrame) -> None:
    with pytest.raises(ValueError):
        generate_market(universe, date(2024, 1, 6), date(2024, 1, 7), seed=1)  # weekend
