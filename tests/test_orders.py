from datetime import date
from pathlib import Path

import pandas as pd
import pytest
from sqlalchemy import Engine

from pit.config import AppConfig
from pit.db import read_audit_log, session_scope
from pit.db.models import OrderStatus
from pit.orders import export_basket_csv, generate_basket, load_orders, set_basket_status
from pit.orders.basket import round_to_lot
from pit.orders.store import basket_status, save_orders
from pit.portfolio.state import PortfolioState

D = date(2026, 3, 16)


def _state() -> PortfolioState:
    """AAA 2 m + BBB 6 m + OLD 1k in stocks, 1,999,000 cash -> NAV = 10 m."""
    return PortfolioState(
        as_of=D,
        shares=pd.Series({"AAA": 20_000.0, "BBB": 30_000.0, "OLD": 100.0}),
        prices=pd.Series({"AAA": 100.0, "BBB": 200.0, "CCC": 50.0, "OLD": 10.0}),
        cash=1_999_000.0,
    )


def _cfg(cfg: AppConfig, buffer: float = 0.0, threshold_bps: float = 5.0) -> AppConfig:
    return cfg.model_copy(
        update={
            "equitization": cfg.equitization.model_copy(update={"cash_buffer_pct": buffer}),
            "orders": cfg.orders.model_copy(update={"no_trade_threshold_bps": threshold_bps}),
        }
    )


def test_basket_by_hand(cfg: AppConfig) -> None:
    s = _state()
    assert s.nav == pytest.approx(10_000_000)
    targets = pd.Series({"AAA": 0.30, "BBB": 0.50, "CCC": 0.19})
    b = generate_basket(s, targets, _cfg(cfg))
    o = b.orders.set_index("ticker")
    # AAA: 0.30 x 10m / 100 = 30,000 -> buy 10,000
    # BBB: 0.50 x 10m / 200 = 25,000 -> sell 5,000
    # CCC: 0.19 x 10m / 50  = 38,000 -> buy 38,000
    # OLD: not in the model -> sell all 100 (full exit, kept despite 1 bp size)
    assert o.loc["AAA", ["side", "quantity"]].tolist() == ["BUY", 10_000]
    assert o.loc["BBB", ["side", "quantity"]].tolist() == ["SELL", 5_000]
    assert o.loc["CCC", ["side", "quantity"]].tolist() == ["BUY", 38_000]
    assert o.loc["OLD", ["side", "quantity"]].tolist() == ["SELL", 100]
    # cost = notional x (1 + 2) bp
    assert o.loc["AAA", "est_cost"] == pytest.approx(1_000_000 * 3 / 10_000)
    assert b.buy_notional == pytest.approx(2_900_000)
    assert b.sell_notional == pytest.approx(1_001_000)
    assert b.signed_quantities["BBB"] == -5_000
    assert b.summary()["orders"] == 4
    assert len(b.batch_id) == 36


def test_cash_buffer_reduces_equity_budget(cfg: AppConfig) -> None:
    b = generate_basket(_state(), pd.Series({"AAA": 0.5}), _cfg(cfg, buffer=0.01))
    o = b.orders.set_index("ticker")
    # 0.5 x 10m x 0.99 / 100 = 49,500 shares -> buy 29,500
    assert o.loc["AAA", "quantity"] == 29_500


def test_buys_scaled_to_available_cash(cfg: AppConfig) -> None:
    """Buys of 3.0 m vs 1,999,000 cash + 1,001,000 sells - costs: slightly short,
    so every buy is scaled down and the fund keeps its minimum cash."""
    s = _state()
    b = generate_basket(s, pd.Series({"AAA": 0.30, "BBB": 0.50, "CCC": 0.20}), _cfg(cfg))
    o = b.orders.set_index("ticker")
    assert o.loc["AAA", "quantity"] < 10_000 and o.loc["CCC", "quantity"] < 40_000
    assert o.loc["BBB", "quantity"] == 5_000  # sells are untouched
    cash_after = s.cash + b.sell_notional - b.buy_notional - b.total_cost
    assert cash_after >= 0
    # with a 1 % buffer the scaling is much stronger and keeps 100 k in cash
    b1 = generate_basket(s, pd.Series({"AAA": 0.30, "BBB": 0.50, "CCC": 0.20}), _cfg(cfg, 0.01))
    cash1 = s.cash + b1.sell_notional - b1.buy_notional - b1.total_cost
    assert cash1 >= 0.01 * s.nav - 1


def test_no_trade_band_skips_small_orders(cfg: AppConfig) -> None:
    s = _state()
    # AAA target 0.2003 -> +0.03 % of NAV (3 bp) -> skipped with a 5 bp band
    targets = pd.Series({"AAA": 0.2003, "BBB": 0.6, "OLD": 0.0001})
    b = generate_basket(s, targets, _cfg(cfg))
    assert "AAA" in set(b.skipped["ticker"])
    assert "AAA" not in set(b.orders["ticker"])
    # with no band, it trades
    b0 = generate_basket(s, targets, _cfg(cfg, threshold_bps=0))
    assert "AAA" in set(b0.orders["ticker"])


def test_round_lot(cfg: AppConfig) -> None:
    assert round_to_lot(pd.Series([149.0, 151.0]), 100).tolist() == [100.0, 200.0]
    cfg100 = cfg.model_copy(update={"orders": cfg.orders.model_copy(update={"round_lot": 100})})
    b = generate_basket(_state(), pd.Series({"CCC": 0.0123}), _cfg(cfg100))
    qty = b.orders.set_index("ticker").loc["CCC", "quantity"]
    assert qty % 100 == 0


def test_missing_price_raises(cfg: AppConfig) -> None:
    with pytest.raises(ValueError, match="no price"):
        generate_basket(_state(), pd.Series({"ZZZ": 1.0}), cfg)


def test_already_at_target_gives_empty_basket(cfg: AppConfig) -> None:
    s = PortfolioState(D, pd.Series({"AAA": 100.0}), pd.Series({"AAA": 100.0}), 0.0)
    b = generate_basket(s, pd.Series({"AAA": 1.0}), _cfg(cfg))
    assert b.orders.empty
    assert b.signed_quantities.empty
    assert b.total_cost == 0


def test_csv_export_format(cfg: AppConfig, tmp_path: Path) -> None:
    b = generate_basket(_state(), pd.Series({"AAA": 0.3, "BBB": 0.5, "CCC": 0.2}), _cfg(cfg))
    path = export_basket_csv(b, tmp_path / "out" / "basket.csv")
    lines = path.read_text().splitlines()
    assert lines[0] == "ticker,side,quantity,est_price,est_notional"
    df = pd.read_csv(path)
    assert len(df) == len(b.orders)
    assert set(df["side"]) <= {"BUY", "SELL"}
    assert df["quantity"].dtype.kind == "i"
    assert (df["quantity"] > 0).all()
    row = df.set_index("ticker").loc["AAA"]
    assert row["est_notional"] == pytest.approx(row["quantity"] * row["est_price"])


def test_order_persistence_and_status_transitions(engine: Engine, cfg: AppConfig) -> None:
    b = generate_basket(_state(), pd.Series({"AAA": 0.3, "BBB": 0.7}), _cfg(cfg))
    with session_scope(engine) as s:
        assert save_orders(s, b, OrderStatus.PROPOSED) == len(b.orders)
        assert basket_status(s, b.batch_id) is OrderStatus.PROPOSED
        set_basket_status(s, b.batch_id, OrderStatus.APPROVED, actor="pm", reason="ok")
        set_basket_status(s, b.batch_id, OrderStatus.FILLED)
        with pytest.raises(ValueError, match="cannot move"):
            set_basket_status(s, b.batch_id, OrderStatus.PROPOSED)
        with pytest.raises(LookupError):
            set_basket_status(s, "nope", OrderStatus.APPROVED)
    with session_scope(engine) as s:
        orders = load_orders(s, b.batch_id)
        assert set(orders["status"]) == {"FILLED"}
        assert len(load_orders(s)) == len(orders)
        events = read_audit_log(s)["event"].tolist()
    assert events[:2] == ["BASKET_FILLED", "BASKET_APPROVED"]
