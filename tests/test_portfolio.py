from dataclasses import replace
from datetime import date

import pandas as pd
import pytest

from pit.portfolio.state import (
    FuturesPosition,
    PortfolioState,
    initialize_portfolio,
    weights_vs_target,
)

D = date(2026, 3, 16)


def _es(qty: int, price: float = 6_000.0, settle: float | None = None) -> FuturesPosition:
    return FuturesPosition(
        "ESM6", "ES", date(2026, 6, 19), 50.0, qty, price, settle if settle is not None else price
    )


def _state(cash: float = 1_000.0, futures: tuple = ()) -> PortfolioState:  # type: ignore[type-arg]
    return PortfolioState(
        as_of=D,
        shares=pd.Series({"AAA": 10.0, "BBB": 20.0}),
        prices=pd.Series({"AAA": 100.0, "BBB": 50.0, "CCC": 10.0}),
        cash=cash,
        futures=futures,
    )


def test_nav_and_weights() -> None:
    s = _state()
    assert s.equity_value == 2_000.0
    assert s.nav == 3_000.0
    assert s.weights().to_dict() == pytest.approx({"AAA": 1 / 3, "BBB": 1 / 3})
    assert s.cash_pct == pytest.approx(1 / 3)
    assert s.summary()["holdings"] == 2


def test_futures_count_as_exposure_not_value() -> None:
    s = _state(cash=1_000_000.0, futures=(_es(3),))  # 3 x 6,000 x 50 = 900,000 notional
    assert s.futures_notional == 900_000.0
    assert s.nav == 1_002_000.0  # futures add no value after settlement
    assert s.exposure == pytest.approx((2_000 + 900_000) / 1_002_000)
    assert s.unequitized_cash == 100_000.0
    assert s.contracts("ES") == 3 and s.contracts("MES") == 0


def test_variation_margin_and_settlement() -> None:
    s = _state(cash=1_000_000.0, futures=(_es(10),)).mark_futures(6_010.0)
    assert s.unsettled_futures_pnl == 10 * 50 * 10  # 5,000
    nav_before = s.nav
    settled = s.settle_futures()
    assert settled.unsettled_futures_pnl == 0
    assert settled.cash == 1_005_000.0
    assert settled.nav == pytest.approx(nav_before)


def test_mark_futures_only_given_expiry() -> None:
    other = replace(_es(1), code="ESU6", expiry=date(2026, 9, 18))
    s = _state(futures=(_es(1), other)).mark_futures(6_100.0, expiry=date(2026, 6, 19))
    assert [f.price for f in s.futures] == [6_100.0, 6_000.0]


def test_apply_equity_trades_moves_cash() -> None:
    s = _state().apply_equity_trades(pd.Series({"AAA": -5.0, "CCC": 10.0}), costs=1.0)
    assert s.shares.to_dict() == {"AAA": 5.0, "BBB": 20.0, "CCC": 10.0}
    assert s.cash == 1_000 + 500 - 100 - 1


def test_apply_equity_trades_guards() -> None:
    with pytest.raises(ValueError, match="short"):
        _state().apply_equity_trades(pd.Series({"AAA": -11.0}))
    with pytest.raises(ValueError, match="execution price"):
        _state().apply_equity_trades(pd.Series({"ZZZ": 1.0}))


def test_selling_whole_position_removes_it() -> None:
    s = _state().apply_equity_trades(pd.Series({"AAA": -10.0}))
    assert "AAA" not in s.shares.index


def test_missing_price_rejected() -> None:
    with pytest.raises(ValueError, match="no price"):
        PortfolioState(D, pd.Series({"AAA": 1.0}), pd.Series(dtype=float), 0.0)


def test_with_prices_carries_forward() -> None:
    s = _state().with_prices(date(2026, 3, 17), pd.Series({"AAA": 110.0}))
    assert s.as_of == date(2026, 3, 17)
    assert s.prices["AAA"] == 110.0 and s.prices["BBB"] == 50.0


def test_initialize_portfolio() -> None:
    targets = pd.Series({"AAA": 0.6, "BBB": 0.4})
    prices = pd.Series({"AAA": 333.0, "BBB": 77.0})
    s = initialize_portfolio(D, 1_000_000.0, targets, prices, cash_buffer_pct=0.005)
    # floor(0.6 x 995,000 / 333) = 1,792 ; floor(0.4 x 995,000 / 77) = 5,168
    assert s.shares.to_dict() == {"AAA": 1_792.0, "BBB": 5_168.0}
    assert s.nav == pytest.approx(1_000_000.0)
    assert s.cash >= 5_000.0
    assert s.exposure == pytest.approx(1 - s.cash / s.nav)


def test_initialize_requires_prices() -> None:
    with pytest.raises(ValueError, match="no price"):
        initialize_portfolio(D, 1e6, pd.Series({"ZZZ": 1.0}), pd.Series({"AAA": 1.0}), 0.0)


def test_weights_vs_target_and_benchmark() -> None:
    s = _state(cash=0.0)  # AAA 50 %, BBB 50 %
    df = weights_vs_target(
        s,
        pd.Series({"AAA": 0.4, "CCC": 0.6}),
        invested_fraction=1.0,
        benchmark_weights=pd.Series({"AAA": 0.5}),
    )
    assert df.loc["AAA", "diff"] == pytest.approx(0.1)
    assert df.loc["BBB", "target"] == 0.0
    assert df.loc["CCC", "diff"] == pytest.approx(-0.6)
    assert df.loc["BBB", "active"] == pytest.approx(0.5)
