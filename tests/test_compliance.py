"""Every compliance check has one passing and one failing case, and the basket
status logic is tested end to end (HARD -> REJECTED, SOFT -> PROPOSED)."""

from dataclasses import replace
from datetime import date

import pandas as pd
import pytest
from sqlalchemy import Engine

from pit.compliance import (
    ComplianceContext,
    ComplianceInputs,
    approve_basket,
    decide_status,
    load_checks,
    load_compliance_inputs,
    post_trade_checks,
    record_review,
    reject_basket,
    review_basket,
    run_checks,
)
from pit.compliance import checks as chk
from pit.config import AppConfig, Severity
from pit.data.loader import load_data
from pit.db import read_audit_log, session_scope
from pit.db.models import OrderStatus
from pit.equitization import FuturesQuote
from pit.orders import generate_basket, load_orders
from pit.portfolio.state import FuturesPosition, PortfolioState
from tests.test_loader import NO_KEYS

D = date(2026, 3, 16)
SECTORS = ["Tech", "Health", "Energy", "Financials"]


def _diversified(n: int = 40, cash_pct: float = 0.005) -> PortfolioState:
    """n equal-weighted stocks across 4 sectors, ``cash_pct`` in cash, NAV = 100 m."""
    nav = 100_000_000.0
    tickers = [f"S{i:02d}" for i in range(n)]
    per_name = nav * (1 - cash_pct) / n
    return PortfolioState(
        as_of=D,
        shares=pd.Series(per_name / 100.0, index=tickers),
        prices=pd.Series(100.0, index=tickers),
        cash=nav * cash_pct,
    )


def _sectors(state: PortfolioState) -> pd.Series:
    t = list(state.shares.index)
    return pd.Series([SECTORS[i % 4] for i in range(len(t))], index=t)


def _ctx(state: PortfolioState | None = None, **kw) -> ComplianceContext:  # type: ignore[no-untyped-def]
    state = state or _diversified()
    n = max(len(state.shares), 1)
    bench = pd.Series(1 / n, index=state.shares.index)
    base = {
        "state": state,
        "sectors": _sectors(state),
        "benchmark_weights": bench,
        "restricted": frozenset({"BAD"}),
        "orders": pd.DataFrame({"ticker": ["S00"], "side": ["BUY"], "quantity": [1_000.0]}),
        "adv": pd.Series(1_000_000.0, index=state.shares.index),
        "tracking_error": 0.03,
        "beta": 1.0,
    }
    base.update(kw)
    return ComplianceContext(**base)


def _one(ctx: ComplianceContext, check, cfg: AppConfig) -> chk.CheckResult:  # type: ignore[no-untyped-def]
    return check(ctx, cfg.compliance)


# --------------------------------------------------------------------------- baseline


def test_diversified_portfolio_passes_everything(cfg: AppConfig) -> None:
    results = run_checks(_ctx(), cfg.compliance)
    assert len(results) == 10
    assert all(r.passed for r in results), [r.message for r in results if not r.passed]
    assert decide_status(results) is OrderStatus.APPROVED


# --------------------------------------------------------------------------- 1. issuer


def test_issuer_weight(cfg: AppConfig) -> None:
    assert _one(_ctx(), chk.check_max_issuer_weight, cfg).passed
    s = _diversified()
    # a new 8 m subscription all invested in S00: (2.4875 m + 8 m) / 108 m = 9.71 %
    heavy = s.with_cash(8_000_000).apply_equity_trades(pd.Series({"S00": 80_000.0}))
    res = _one(_ctx(heavy), chk.check_max_issuer_weight, cfg)
    assert not res.passed and res.severity is Severity.HARD
    assert "S00" in res.message and res.value == pytest.approx(10.4875 / 108, rel=1e-6)


# --------------------------------------------------------------------------- 2. sector


def test_sector_weight(cfg: AppConfig) -> None:
    assert _one(_ctx(), chk.check_max_sector_weight, cfg).passed  # 4 sectors ~25 % each
    s = _diversified()
    tech_heavy = pd.Series(["Tech"] * 20 + ["Health"] * 20, index=s.shares.index)
    res = _one(_ctx(sectors=tech_heavy), chk.check_max_sector_weight, cfg)
    assert not res.passed and res.is_hard_breach


# --------------------------------------------------------------------------- 3. active sector


def test_sector_active_weight(cfg: AppConfig) -> None:
    assert _one(_ctx(), chk.check_sector_active_weight, cfg).passed
    s = _diversified()
    # benchmark entirely in Energy names -> portfolio is ~75 pts underweight Energy
    bench = pd.Series(0.0, index=s.shares.index)
    energy = [t for t, sec in _sectors(s).items() if sec == "Energy"]
    bench[energy] = 1 / len(energy)
    res = _one(_ctx(benchmark_weights=bench), chk.check_sector_active_weight, cfg)
    assert not res.passed and res.is_soft_breach
    assert "Energy" in res.message


# --------------------------------------------------------------------------- 4. cash


def test_cash_range(cfg: AppConfig) -> None:
    assert _one(_ctx(), chk.check_cash_range, cfg).passed  # 0.5 %
    low = _one(_ctx(_diversified(cash_pct=0.001)), chk.check_cash_range, cfg)
    high = _one(_ctx(_diversified(cash_pct=0.05)), chk.check_cash_range, cfg)
    assert not low.passed and "below" in low.message and low.is_hard_breach
    assert not high.passed and "above" in high.message


def test_cash_check_uses_cash_net_of_futures(cfg: AppConfig) -> None:
    """5 % physical cash fully covered by futures is not idle cash."""
    s = _diversified(cash_pct=0.05)  # 5 m cash
    es = FuturesPosition("ESM6", "ES", date(2026, 6, 19), 50.0, 15, 6_000.0, 6_000.0)  # 4.5 m
    res = _one(_ctx(replace(s, futures=(es,))), chk.check_cash_range, cfg)
    assert res.passed and res.value == pytest.approx(0.005)


# --------------------------------------------------------------------------- 5. exposure


def test_total_exposure(cfg: AppConfig) -> None:
    assert _one(_ctx(), chk.check_total_exposure, cfg).passed  # 99.5 %
    under = _one(_ctx(_diversified(cash_pct=0.05)), chk.check_total_exposure, cfg)
    assert not under.passed and under.is_hard_breach  # 95 %
    s = _diversified()
    es = FuturesPosition("ESM6", "ES", date(2026, 6, 19), 50.0, 20, 6_000.0, 6_000.0)  # +6 m
    over = _one(_ctx(replace(s, futures=(es,))), chk.check_total_exposure, cfg)
    assert not over.passed and "above" in over.message


# --------------------------------------------------------------------------- 6-7. risk


def test_tracking_error(cfg: AppConfig) -> None:
    assert _one(_ctx(tracking_error=0.05), chk.check_tracking_error, cfg).passed
    res = _one(_ctx(tracking_error=0.08), chk.check_tracking_error, cfg)
    assert not res.passed and res.is_soft_breach
    skipped = _one(_ctx(tracking_error=None), chk.check_tracking_error, cfg)
    assert skipped.passed and skipped.value is None


def test_beta(cfg: AppConfig) -> None:
    assert _one(_ctx(beta=1.1), chk.check_beta, cfg).passed
    assert not _one(_ctx(beta=0.80), chk.check_beta, cfg).passed
    assert not _one(_ctx(beta=1.20), chk.check_beta, cfg).passed
    assert _one(_ctx(beta=None), chk.check_beta, cfg).passed


# --------------------------------------------------------------------------- 8. count


def test_holdings_count(cfg: AppConfig) -> None:
    assert _one(_ctx(), chk.check_holdings_count, cfg).passed  # 40
    few = _one(_ctx(_diversified(n=20)), chk.check_holdings_count, cfg)
    many = _one(_ctx(_diversified(n=90)), chk.check_holdings_count, cfg)
    assert not few.passed and few.is_soft_breach
    assert not many.passed


# --------------------------------------------------------------------------- 9. restricted


def test_restricted_list(cfg: AppConfig) -> None:
    assert _one(_ctx(), chk.check_restricted_list, cfg).passed
    held = _one(_ctx(restricted=frozenset({"S03"})), chk.check_restricted_list, cfg)
    assert not held.passed and held.is_hard_breach and "S03" in held.message
    buy = pd.DataFrame({"ticker": ["BAD"], "side": ["BUY"], "quantity": [10.0]})
    bought = _one(_ctx(orders=buy), chk.check_restricted_list, cfg)
    assert not bought.passed
    # selling a restricted name cures a breach and is allowed
    sell = pd.DataFrame({"ticker": ["BAD"], "side": ["SELL"], "quantity": [10.0]})
    assert _one(_ctx(orders=sell), chk.check_restricted_list, cfg).passed


# --------------------------------------------------------------------------- 10. ADV


def test_order_vs_adv(cfg: AppConfig) -> None:
    assert _one(_ctx(), chk.check_order_vs_adv, cfg).passed  # 1,000 / 1,000,000 = 0.1 %
    big = pd.DataFrame({"ticker": ["S01"], "side": ["SELL"], "quantity": [150_000.0]})
    res = _one(_ctx(orders=big), chk.check_order_vs_adv, cfg)
    assert not res.passed and res.is_soft_breach and res.value == pytest.approx(0.15)
    no_vol = pd.DataFrame({"ticker": ["NEW"], "side": ["BUY"], "quantity": [1.0]})
    missing = _one(_ctx(orders=no_vol), chk.check_order_vs_adv, cfg)
    assert not missing.passed and "no volume" in missing.message
    assert _one(_ctx(orders=pd.DataFrame()), chk.check_order_vs_adv, cfg).passed


def test_empty_portfolio_edge_cases(cfg: AppConfig) -> None:
    empty = PortfolioState(D, pd.Series(dtype=float), pd.Series(dtype=float), 1.0)
    ctx = _ctx(empty, sectors=pd.Series(dtype=object), benchmark_weights=pd.Series(dtype=float))
    assert _one(ctx, chk.check_max_issuer_weight, cfg).value == 0.0
    assert _one(ctx, chk.check_max_sector_weight, cfg).value == 0.0
    assert _one(ctx, chk.check_sector_active_weight, cfg).value == 0.0


# --------------------------------------------------------------------------- status logic


def test_decide_status() -> None:
    ok = chk.CheckResult("a", 0.0, "1", True, Severity.HARD, "")
    soft = chk.CheckResult("b", 2.0, "1", False, Severity.SOFT, "")
    hard = chk.CheckResult("c", 2.0, "1", False, Severity.HARD, "")
    assert decide_status([ok]) is OrderStatus.APPROVED
    assert decide_status([ok, soft]) is OrderStatus.PROPOSED
    assert decide_status([ok, soft, hard]) is OrderStatus.REJECTED


# --------------------------------------------------------------------------- review pipeline


def _inputs(state: PortfolioState, **kw) -> ComplianceInputs:  # type: ignore[no-untyped-def]
    names = list(state.shares.index) + ["NEW"]
    base = {
        "sectors": pd.Series([SECTORS[i % 4] for i in range(len(names))], index=names),
        "benchmark_weights": pd.Series(1 / len(names), index=names),
        "restricted": frozenset({"BAD"}),
        "adv": pd.Series(5_000_000.0, index=names),
        "risk_model": None,
    }
    base.update(kw)
    return ComplianceInputs(**base)


QUOTE = FuturesQuote(date(2026, 6, 19), 6_000.0, 0.26, 5_950.0, 0.04, 0.013)


def test_basket_breaking_hard_limit_is_rejected(cfg: AppConfig, engine: Engine) -> None:
    state = _diversified()
    targets = pd.Series(1 / 40, index=state.shares.index)
    targets = targets * 0.88 / targets.sum()
    targets["S00"] += 0.12  # 12 % in one name -> above the 8 % HARD issuer limit
    basket = generate_basket(state, targets, cfg)
    review = review_basket(state, basket, QUOTE, cfg, _inputs(state))
    assert review.status is OrderStatus.REJECTED
    # S00 at ~12 % breaks the issuer limit, and lifts its sector (Tech: 10 names x
    # 2.2 % + 12 % = 34 %) above the 30 % sector limit too.
    assert [c.name for c in review.hard_breaches] == ["max_issuer_weight", "max_sector_weight"]

    with session_scope(engine) as s:
        record_review(s, review)
    with session_scope(engine) as s:
        orders = load_orders(s, basket.batch_id)
        checks = load_checks(s, basket.batch_id)
        log = read_audit_log(s)
    assert set(orders["status"]) == {"REJECTED"}
    assert len(checks) == 10
    assert checks.set_index("check_name").loc["max_issuer_weight", "passed"] == False  # noqa: E712
    events = log["event"].tolist()
    assert events[0] == "BASKET_REJECTED"
    assert "COMPLIANCE_BREACH" in events and "BASKET_GENERATED" in events


def test_soft_breach_needs_manual_approval(cfg: AppConfig, engine: Engine) -> None:
    state = _diversified()
    targets = pd.Series(1 / 40, index=state.shares.index)
    basket = generate_basket(state, targets.drop("S39") / targets.drop("S39").sum(), cfg)
    tiny_adv = _inputs(state, adv=pd.Series(1_000.0, index=list(state.shares.index)))
    review = review_basket(state, basket, QUOTE, cfg, tiny_adv)
    assert review.status is OrderStatus.PROPOSED
    assert [c.name for c in review.soft_breaches] == ["max_order_pct_adv"]

    with session_scope(engine) as s:
        record_review(s, review)
        with pytest.raises(ValueError, match="reason"):
            approve_basket(s, basket.batch_id, "emilio", "  ")
        approve_basket(s, basket.batch_id, "emilio", "liquidity reviewed with the desk")
    with session_scope(engine) as s:
        assert set(load_orders(s, basket.batch_id)["status"]) == {"APPROVED"}
        approval = read_audit_log(s, event="BASKET_APPROVED").iloc[0]["details"]
    assert approval["actor"] == "emilio" and approval["from"] == "PROPOSED"


def test_manual_rejection(cfg: AppConfig, engine: Engine) -> None:
    state = _diversified()
    basket = generate_basket(state, pd.Series(1 / 39, index=state.shares.index[:39]), cfg)
    review = review_basket(
        state, basket, QUOTE, cfg, _inputs(state, adv=pd.Series(1.0, index=state.shares.index))
    )
    with session_scope(engine) as s:
        record_review(s, review)
        reject_basket(s, basket.batch_id, "emilio", "wait for liquidity")
        assert set(load_orders(s, basket.batch_id)["status"]) == {"REJECTED"}


def test_risk_model_is_applied_to_pro_forma(cfg: AppConfig) -> None:
    state = _diversified()
    seen: list[float] = []

    def fake_risk(s: PortfolioState) -> tuple[float, float]:
        seen.append(s.nav)
        return 0.09, 1.0

    basket = generate_basket(state, pd.Series(1 / 40, index=state.shares.index), cfg)
    review = review_basket(state, basket, QUOTE, cfg, _inputs(state, risk_model=fake_risk))
    assert seen, "risk model was not called"
    assert [c.name for c in review.soft_breaches] == ["max_tracking_error"]
    assert review.summary()["status"] == "PROPOSED"


def test_post_trade_checks(cfg: AppConfig) -> None:
    state = _diversified(cash_pct=0.05)
    results = post_trade_checks(state, cfg, _inputs(state))
    failed = {r.name for r in results if not r.passed}
    assert failed == {"cash_range", "total_exposure_range"}


# --------------------------------------------------------------------------- inputs from the DB


def test_load_compliance_inputs(engine: Engine, cfg: AppConfig) -> None:
    load_data(engine, cfg, NO_KEYS, date(2024, 1, 2), date(2024, 3, 28), offline=True)
    with session_scope(engine) as s:
        inp = load_compliance_inputs(s, date(2024, 2, 15), cfg)
    assert inp.restricted == frozenset({"BA", "MO"})
    assert len(inp.sectors) == 100 and inp.sectors["AAPL"] == "Information Technology"
    # benchmark snapshot is dated 2024-03-28, after as_of -> earliest snapshot used
    assert inp.benchmark_weights.sum() == pytest.approx(1.0)
    assert inp.adv.gt(0).all() and "SPX" not in inp.adv.index
