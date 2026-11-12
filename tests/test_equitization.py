"""Equitization: sizing, subscriptions, de-equitization, redemptions and rolls.

The first tests reproduce the hand calculation from the project plan:

    USD 25 m subscription, F = 6,030, no cash buffer
    ES notional per contract  = 6,030 x 50 = 301,500
    N_ES  = floor(25,000,000 / 301,500) = floor(82.92) = 82   -> 24,723,000
    residual = 277,000 ; MES notional = 6,030 x 5 = 30,150
    N_MES = round(277,000 / 30,150) = round(9.19) = 9         ->    271,350
    cash left un-equitized = 25,000,000 - 24,723,000 - 271,350 = 5,650
"""

from datetime import date

import pandas as pd
import pytest

from pit.config import AppConfig, EquitizationConfig
from pit.equitization import (
    FuturesQuote,
    de_equitize,
    equitize_subscription,
    meet_redemption,
    rebalance_overlay,
    roll_alerts,
    size_futures,
)
from pit.equitization.engine import cash_to_equitize, pro_rata_sales
from pit.portfolio.state import FuturesPosition, PortfolioState, initialize_portfolio

D = date(2026, 3, 16)
JUNE = date(2026, 6, 19)


def _quote(price: float = 6_030.0, expiry: date = JUNE) -> FuturesQuote:
    return FuturesQuote(expiry, price, 0.26, price, 0.04, 0.013)


def _no_buffer(cfg: AppConfig) -> AppConfig:
    return cfg.model_copy(update={"equitization": EquitizationConfig(cash_buffer_pct=0.0)})


def _empty_fund() -> PortfolioState:
    return PortfolioState(D, pd.Series(dtype=float), pd.Series(dtype=float), cash=0.0)


def _fund(cfg: AppConfig, nav: float = 100_000_000.0) -> PortfolioState:
    """Fully invested fund: 4 stocks, 0.5 % cash buffer."""
    targets = pd.Series({"AAA": 0.4, "BBB": 0.3, "CCC": 0.2, "DDD": 0.1})
    prices = pd.Series({"AAA": 250.0, "BBB": 120.0, "CCC": 80.0, "DDD": 40.0})
    return initialize_portfolio(D, nav, targets, prices, cfg.equitization.cash_buffer_pct)


# --------------------------------------------------------------------------- hand calculation


def test_size_futures_hand_example() -> None:
    s = size_futures(25_000_000, 6_030.0, 50, 5)
    assert (s.es_contracts, s.mes_contracts) == (82, 9)
    assert s.es_notional == 24_723_000
    assert s.mes_notional == 271_350
    assert s.residual == pytest.approx(5_650)


def test_subscription_hand_example(cfg: AppConfig) -> None:
    res = equitize_subscription(_empty_fund(), 25_000_000, _quote(), _no_buffer(cfg))
    assert (res.es_trade, res.mes_trade) == (82, 9)
    assert res.futures_notional == 24_994_350
    assert res.unequitized_cash == pytest.approx(5_650)
    assert res.exposure_before == 0.0  # empty fund: no NAV before the flow
    assert res.exposure_after == pytest.approx(24_994_350 / 25_000_000)
    assert res.state is not None and res.state.cash == 25_000_000
    codes = sorted(f.code for f in res.state.futures)
    assert codes == ["ESM6", "MESM6"]


def test_size_futures_exact_multiple_and_short() -> None:
    exact = size_futures(301_500 * 10, 6_030.0)
    assert (exact.es_contracts, exact.mes_contracts) == (10, 0)
    short = size_futures(-25_000_000, 6_030.0)
    assert (short.es_contracts, short.mes_contracts) == (-82, -9)
    with pytest.raises(ValueError):
        size_futures(1e6, 0.0)


def test_mes_rounds_up_when_closer() -> None:
    # residual of 0.6 micro contracts rounds to 1 -> tiny over-exposure, by design
    s = size_futures(301_500 + 0.6 * 30_150, 6_030.0)
    assert (s.es_contracts, s.mes_contracts) == (1, 1)
    assert s.residual < 0


# --------------------------------------------------------------------------- buffer and overlay


def test_buffer_is_left_unequitized(cfg: AppConfig) -> None:
    fund = _fund(cfg)
    res = equitize_subscription(fund, 25_000_000, _quote(), cfg)
    nav = res.state.nav  # type: ignore[union-attr]
    expected = fund.cash + 25_000_000 - 0.005 * nav
    assert res.target_notional == pytest.approx(expected)
    assert abs(res.unequitized_cash) <= 0.5 * 6_030 * 5  # within half a micro contract
    # exposure stays at ~ 1 - buffer before and after the flow
    assert res.exposure_after == pytest.approx(0.995, abs=1e-4)


def test_no_futures_when_cash_below_buffer(cfg: AppConfig) -> None:
    fund = _fund(cfg)
    assert cash_to_equitize(fund, 0.005) == pytest.approx(0.0, abs=1e3)
    res = rebalance_overlay(fund.with_cash(-fund.cash), _quote(), cfg)
    assert res.es_after == 0 and res.mes_after == 0


def test_target_beta_scales_notional(cfg: AppConfig) -> None:
    cfg_beta = _no_buffer(cfg)
    cfg_beta = cfg_beta.model_copy(
        update={"futures": cfg_beta.futures.model_copy(update={"target_beta": 1.1})}
    )
    res = equitize_subscription(_empty_fund(), 25_000_000, _quote(), cfg_beta)
    assert res.target_notional == pytest.approx(27_500_000)


def test_subscription_must_be_positive(cfg: AppConfig) -> None:
    with pytest.raises(ValueError):
        equitize_subscription(_empty_fund(), 0, _quote(), cfg)


# --------------------------------------------------------------------------- de-equitization


def test_buying_stocks_sells_futures_proportionally(cfg: AppConfig) -> None:
    sub = equitize_subscription(_fund(cfg), 25_000_000, _quote(), cfg)
    assert sub.state is not None
    # invest USD 20 m in AAA at 250
    buy = pd.Series({"AAA": 80_000.0})
    res = de_equitize(sub.state, buy, _quote(), cfg)
    assert res.state is not None
    sold_notional = sub.futures_notional - res.futures_notional
    assert sold_notional == pytest.approx(20_000_000, abs=6_030 * 5)
    # exposure unchanged: stocks replaced futures
    assert res.exposure_after == pytest.approx(sub.exposure_after, abs=1e-4)
    assert res.es_trade < 0


# --------------------------------------------------------------------------- redemptions


def test_redemption_covered_by_cash_unwinds_futures(cfg: AppConfig) -> None:
    sub = equitize_subscription(_fund(cfg), 25_000_000, _quote(), cfg)
    assert sub.state is not None
    res = meet_redemption(sub.state, 10_000_000, _quote(), cfg)
    assert res.stock_sales.empty
    # Futures fall by 10 m x (1 - 0.5 %): NAV shrinks, so the required buffer
    # shrinks by 50 k and that cash is equitized too.
    expected = sub.futures_notional - 10_000_000 * (1 - 0.005)
    assert res.futures_notional == pytest.approx(expected, abs=6_030 * 5)
    assert res.state is not None
    assert res.state.nav == pytest.approx(sub.state.nav - 10_000_000)


def test_redemption_larger_than_cash_sells_stocks(cfg: AppConfig) -> None:
    fund = _fund(cfg)  # USD 100 m NAV, ~0.5 m cash, no futures
    cash_before = fund.cash
    res = meet_redemption(fund, 10_000_000, _quote(), cfg)
    after = res.state
    assert after is not None

    # 1. stocks were sold for the shortfall: redemption + buffer on new NAV - cash
    shortfall = 10_000_000 + 0.005 * (fund.nav - 10_000_000) - cash_before
    assert res.stock_sales_notional == pytest.approx(shortfall, rel=1e-3)
    assert res.stock_sales_notional >= shortfall

    # 2. pro rata: the same fraction of every position was sold, weights unchanged
    sold_frac = (-res.stock_sales / fund.shares).dropna()
    assert sold_frac.max() - sold_frac.min() < 1e-3
    mix_before = fund.market_values / fund.equity_value
    mix_after = after.market_values / after.equity_value
    pd.testing.assert_series_equal(mix_after, mix_before, atol=1e-4)

    # 3. the fund never goes overdrawn and keeps its buffer
    assert after.cash >= 0.005 * after.nav - 1
    assert after.nav == pytest.approx(fund.nav - 10_000_000)
    assert res.es_after == 0 and res.mes_after == 0
    assert res.exposure_after == pytest.approx(0.995, abs=1e-3)


def test_redemption_guards(cfg: AppConfig) -> None:
    fund = _fund(cfg)
    with pytest.raises(ValueError):
        meet_redemption(fund, -1, _quote(), cfg)
    with pytest.raises(ValueError, match="exceeds NAV"):
        meet_redemption(fund, fund.nav, _quote(), cfg)


def test_pro_rata_sales_caps_and_rounds() -> None:
    s = PortfolioState(D, pd.Series({"A": 3.0, "B": 7.0}), pd.Series({"A": 10.0, "B": 10.0}), 0.0)
    assert pro_rata_sales(s, 0.0).empty
    sales = pro_rata_sales(s, 55.0)  # 55 % of USD 100 -> 1.65 A, 3.85 B -> rounded up
    assert sales.to_dict() == {"A": -2.0, "B": -4.0}
    assert pro_rata_sales(s, 1_000.0).to_dict() == {"A": -3.0, "B": -7.0}


# --------------------------------------------------------------------------- rolls


def test_roll_alert_and_roll_into_next_contract(cfg: AppConfig) -> None:
    march = FuturesPosition("ESH6", "ES", date(2026, 3, 20), 50.0, 10, 6_000.0, 6_000.0)
    fund = _fund(cfg).with_cash(3_000_000)
    fund = PortfolioState(fund.as_of, fund.shares, fund.prices, fund.cash, (march,))

    alerts = roll_alerts(fund, D, cfg.futures.roll_window_business_days)
    assert [(a.code, a.business_days_left) for a in alerts] == [("ESH6", 4)]
    assert roll_alerts(fund, date(2026, 3, 2), 5) == []

    res = rebalance_overlay(fund, _quote(), cfg)
    assert res.rolled == ("ESH6",)
    assert res.state is not None
    assert {f.expiry for f in res.state.futures} == {JUNE}
    assert res.as_dict()["rolled"] == ("ESH6",)
