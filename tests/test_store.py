from datetime import date

import pandas as pd
import pytest
from sqlalchemy import Engine

from pit.config import AppConfig
from pit.data.loader import load_data
from pit.db import session_scope
from pit.db.models import DataSource, Security
from pit.db.repository import purge_market_data_not_from
from pit.equitization import equitize_subscription
from pit.portfolio import (
    initialize_portfolio,
    load_positions,
    load_snapshot,
    load_target_weights,
    save_positions,
)
from pit.portfolio.store import latest_position_date
from tests.test_loader import NO_KEYS

D = date(2024, 3, 15)


@pytest.fixture
def loaded(engine: Engine, cfg: AppConfig) -> Engine:
    load_data(engine, cfg, NO_KEYS, date(2024, 1, 2), date(2024, 3, 28), offline=True)
    return engine


def test_snapshot_uses_last_trading_day(loaded: Engine, cfg: AppConfig) -> None:
    with session_scope(loaded) as s:
        snap = load_snapshot(s, date(2024, 3, 17))  # Sunday -> Friday 15-Mar
    assert snap.as_of == D
    assert "SPX" not in snap.prices.index
    assert len(snap.prices) == 100
    assert snap.risk_free == pytest.approx(0.04)
    quote = snap.futures_quote(cfg)
    assert quote.expiry == date(2024, 6, 21)  # March contract is inside the roll window
    assert quote.price > snap.index_level  # r > q -> futures above spot


def test_snapshot_without_data(engine: Engine) -> None:
    with session_scope(engine) as s, pytest.raises(LookupError, match="load-data"):
        load_snapshot(s, D)


def test_target_weights_lookup(loaded: Engine) -> None:
    with session_scope(loaded) as s:
        tw = load_target_weights(s, D)
        assert len(tw) == 55
        assert tw.sum() == pytest.approx(1.0)
        with pytest.raises(LookupError):
            load_target_weights(s, date(2023, 1, 1))


def test_positions_roundtrip_with_futures_and_cash(loaded: Engine, cfg: AppConfig) -> None:
    with session_scope(loaded) as s:
        snap = load_snapshot(s, D)
        tw = load_target_weights(s, D)
    fund = initialize_portfolio(D, 500e6, tw, snap.prices, cfg.equitization.cash_buffer_pct)
    res = equitize_subscription(fund, 25e6, snap.futures_quote(cfg), cfg)
    assert res.state is not None
    state = res.state.mark_futures(res.futures_price + 12.5)  # leave some unsettled P&L

    with session_scope(loaded) as s:
        n = save_positions(s, state)
        assert n == 55 + len(state.futures) + 1
        assert s.get(Security, "USD").asset_type == "CASH"  # type: ignore[union-attr]
    with session_scope(loaded) as s:
        back = load_positions(s, D, cfg)
        assert latest_position_date(s, date(2024, 12, 31)) == D

    assert back.nav == pytest.approx(state.nav)
    assert back.cash == pytest.approx(state.cash)
    assert back.unsettled_futures_pnl == pytest.approx(state.unsettled_futures_pnl)
    assert {f.code for f in back.futures} == {"ESM4", "MESM4"}
    pd.testing.assert_series_equal(back.shares.sort_index(), state.shares.sort_index())


def test_save_replaces_same_day(loaded: Engine, cfg: AppConfig) -> None:
    with session_scope(loaded) as s:
        snap = load_snapshot(s, D)
        tw = load_target_weights(s, D)
    fund = initialize_portfolio(D, 500e6, tw, snap.prices, 0.005)
    with session_scope(loaded) as s:
        save_positions(s, fund)
        save_positions(s, fund.with_cash(1.0))
    with session_scope(loaded) as s:
        assert load_positions(s, D, cfg).cash == pytest.approx(fund.cash + 1.0)


def test_load_positions_missing_date(engine: Engine, cfg: AppConfig) -> None:
    with session_scope(engine) as s, pytest.raises(LookupError):
        load_positions(s, D, cfg)


def test_internal_securities_survive_purge(loaded: Engine, cfg: AppConfig) -> None:
    with session_scope(loaded) as s:
        snap = load_snapshot(s, D)
        tw = load_target_weights(s, D)
        save_positions(s, initialize_portfolio(D, 500e6, tw, snap.prices, 0.005))
        purge_market_data_not_from(s, keep=[DataSource.FMP])
        assert s.get(Security, "USD") is not None
