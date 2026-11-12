"""One trading day of the implementation workflow.

``step_day`` is pure: it takes yesterday's portfolio and today's market data
and returns today's portfolio plus an ordered list of *actions* (audit events,
basket status changes, basket reviews). ``run_day`` writes those actions to the
database; the backtest just counts them. One code path serves both.

Sequence at the close of day T:

1. **Mark to market.** Re-price stocks, re-mark futures at fair value,
   settle variation margin into cash, and accrue interest on cash.
2. **Execute the pending basket** generated ``execution_lag_days`` ago.
   APPROVED: fill at today's close at estimated cost, then unwind futures
   (de-equitize). PROPOSED and not approved in time: expire it (REJECTED).
3. **Client flows.** A subscription is equitized with futures at once. A
   redemption is paid from cash first, then from pro-rata stock sales.
4. **Rolls.** Flag futures within the roll window. The overlay moves them
   into the next contract.
5. **New basket** toward the model, with a pre-trade review that includes the
   ex-ante TE and beta from the risk model.
6. **Final overlay** resize, **post-trade checks** on the actual end-of-day
   book, and a day summary.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date
from typing import Any

import pandas as pd

from pit.compliance.checks import CheckResult
from pit.compliance.pretrade import BasketReview, post_trade_checks, review_basket
from pit.config import AppConfig
from pit.db.models import CashFlowType, OrderStatus
from pit.equitization import (
    de_equitize,
    equitize_subscription,
    meet_redemption,
    rebalance_overlay,
    roll_alerts,
)
from pit.orders.basket import Basket, generate_basket
from pit.portfolio.state import PortfolioState, initialize_portfolio
from pit.workflow.flows import Flow
from pit.workflow.market import MarketData

DAYS_PER_YEAR = 365.0


# --------------------------------------------------------------------------- actions


@dataclass(frozen=True)
class Event:
    name: str
    details: dict[str, Any]


@dataclass(frozen=True)
class StatusChange:
    batch_id: str
    status: OrderStatus
    actor: str
    reason: str


@dataclass(frozen=True)
class ReviewRecorded:
    review: BasketReview


Action = Event | StatusChange | ReviewRecorded


@dataclass(frozen=True)
class PendingBasket:
    basket: Basket
    status: OrderStatus
    generated: date


@dataclass
class DayResult:
    as_of: date
    state: PortfolioState
    actions: list[Action] = field(default_factory=list)
    pending: PendingBasket | None = None
    review: BasketReview | None = None
    filled: Basket | None = None
    post_trade: list[CheckResult] = field(default_factory=list)
    interest: float = 0.0

    def events(self, name: str | None = None) -> list[Event]:
        return [
            a for a in self.actions if isinstance(a, Event) and (name is None or a.name == name)
        ]

    @property
    def summary(self) -> dict[str, Any]:
        s = self.state
        return {
            "date": self.as_of,
            **s.summary(),
            "es_contracts": s.contracts("ES"),
            "mes_contracts": s.contracts("MES"),
            "interest": self.interest,
            "basket_status": self.review.status.value if self.review else None,
            "basket_filled": self.filled.batch_id if self.filled else None,
            "post_trade_hard": sum(c.is_hard_breach for c in self.post_trade),
            "post_trade_soft": sum(c.is_soft_breach for c in self.post_trade),
        }


# --------------------------------------------------------------------------- steps


def _mark_to_market(
    state: PortfolioState, md: MarketData, as_of: date
) -> tuple[PortfolioState, float]:
    """Accrue interest, re-price, re-mark futures and settle variation margin."""
    days = (as_of - state.as_of).days
    interest = state.cash * md.rate(state.as_of) * days / DAYS_PER_YEAR if state.cash > 0 else 0.0
    out = state.with_cash(interest).with_prices(as_of, md.snapshot(as_of).prices)
    for expiry in {f.expiry for f in out.futures}:
        out = out.mark_futures(md.futures_price(as_of, expiry), expiry)
    return out.settle_futures(), interest


def _fill(
    state: PortfolioState, basket: Basket, md: MarketData, as_of: date, cfg: AppConfig
) -> tuple[PortfolioState, Event]:
    """Execute a basket at today's close.

    The book may have changed since the basket was generated (e.g. a
    redemption was paid in between), so two checks are re-run at execution:
    sells are capped at the shares held, and buys are scaled down if they
    would use more than cash + sale proceeds - costs - minimum buffer.
    """
    trades = basket.signed_quantities
    held = state.shares.reindex(trades.index, fill_value=0.0)
    capped = trades.where(trades >= 0, -(held.clip(lower=0).combine(-trades, min)))
    capped = capped[capped != 0]
    prices = md.snapshot(as_of).prices.reindex(capped.index)

    bps = cfg.costs.total_bps / 10_000
    buy_mask = capped > 0
    buys = float((capped[buy_mask] * prices[buy_mask]).sum())
    sells = float((-capped[~buy_mask] * prices[~buy_mask]).sum())
    available = state.cash + sells * (1 - bps) - cfg.equitization.cash_buffer_pct * state.nav
    scale = 1.0
    if buys > 0 and buys * (1 + bps) > available:
        scale = max(available, 0.0) / (buys * (1 + bps))
        lot = cfg.orders.round_lot
        scaled = (capped[buy_mask] * scale / lot).apply(math.floor) * lot
        capped = pd.concat([scaled, capped[~buy_mask]])
        capped = capped[capped != 0]
        prices = prices.reindex(capped.index)
    notional = float((capped.abs() * prices).sum())
    cost = notional * cfg.costs.total_bps / 10_000
    result = de_equitize(state, capped, md.quote(as_of), cfg, prices=prices, costs=cost)
    assert result.state is not None
    est = float((capped.abs() * basket.prices.reindex(capped.index)).sum())
    event = Event(
        "BASKET_EXECUTED",
        {
            "batch_id": basket.batch_id,
            "orders": int(len(capped)),
            "orders_capped": int((capped != trades.reindex(capped.index)).sum()),
            "buys_scaled_to": scale,
            "notional": notional,
            "est_notional": est,
            "cost": cost,
            "futures_after": {"ES": result.es_after, "MES": result.mes_after},
            "exposure_after": result.exposure_after,
        },
    )
    return result.state, event


def _process_pending(
    state: PortfolioState,
    pending: PendingBasket | None,
    md: MarketData,
    as_of: date,
    cfg: AppConfig,
    auto_approve_soft: bool,
    res: DayResult,
) -> tuple[PortfolioState, PendingBasket | None]:
    if pending is None:
        return state, None
    lag = cfg.workflow.execution_lag_days
    if md.trading_days_between(pending.generated, as_of) < lag:
        return state, pending  # not due yet
    batch = pending.basket.batch_id
    status = pending.status
    if status is OrderStatus.PROPOSED:
        if not auto_approve_soft:
            res.actions.append(
                StatusChange(
                    batch,
                    OrderStatus.REJECTED,
                    "system",
                    "expired: SOFT alerts not approved before the execution window",
                )
            )
            return state, None
        res.actions.append(
            StatusChange(batch, OrderStatus.APPROVED, "backtest", "auto-approved SOFT alerts")
        )
    elif status is not OrderStatus.APPROVED:
        return state, None
    state, event = _fill(state, pending.basket, md, as_of, cfg)
    res.actions.append(event)
    res.actions.append(StatusChange(batch, OrderStatus.FILLED, "system", "filled at close"))
    res.filled = pending.basket
    return state, None


def _process_flows(
    state: PortfolioState,
    flows: list[Flow],
    md: MarketData,
    as_of: date,
    cfg: AppConfig,
    res: DayResult,
) -> PortfolioState:
    quote = md.quote(as_of)
    for f in flows:
        res.actions.append(
            Event("FLOW_RECEIVED", {"date": f.date, "amount": f.amount, "type": f.type.value})
        )
        if f.type is CashFlowType.SUBSCRIPTION:
            r = equitize_subscription(state, f.amount, quote, cfg)
            name = "FUTURES_EQUITIZED" if cfg.equitization.enabled else "CASH_RECEIVED"
        else:
            r = meet_redemption(state, f.amount, quote, cfg)
            name = "REDEMPTION_PAID"
        assert r.state is not None
        state = r.state
        res.actions.append(Event(name, r.as_dict()))
    return state


def _new_basket(
    state: PortfolioState, md: MarketData, as_of: date, cfg: AppConfig, res: DayResult
) -> BasketReview | None:
    targets = md.target_weights(as_of)
    basket = generate_basket(state, targets, cfg, md.snapshot(as_of).prices)
    if basket.orders.empty:
        res.actions.append(Event("NO_TRADES", {"skipped_below_threshold": len(basket.skipped)}))
        return None
    inputs = md.compliance_inputs(as_of, list(state.shares.index))
    review = review_basket(state, basket, md.quote(as_of), cfg, inputs)
    res.actions.append(ReviewRecorded(review))
    res.review = review
    return review


def step_day(
    state: PortfolioState | None,
    md: MarketData,
    as_of: date,
    flows: list[Flow],
    pending: PendingBasket | None,
    cfg: AppConfig,
    auto_approve_soft: bool = False,
) -> DayResult:
    """Run one day. ``state=None`` means inception: the fund is built at today's close."""
    snap = md.snapshot(as_of)

    if state is None:
        targets = md.target_weights(as_of)
        state = initialize_portfolio(
            as_of,
            cfg.fund.initial_nav,
            targets,
            snap.prices,
            cfg.equitization.cash_buffer_pct,
            cfg.orders.round_lot,
        )
        res = DayResult(as_of, state)
        res.actions.append(Event("PORTFOLIO_INITIALIZED", state.summary()))
    else:
        if as_of <= state.as_of:
            raise ValueError(f"{as_of} is not after the last processed day {state.as_of}")
        marked, interest = _mark_to_market(state, md, as_of)
        res = DayResult(as_of, marked, interest=interest)
        state = marked
        res.actions.append(Event("MARKED_TO_MARKET", {"nav": state.nav, "interest": interest}))

    # 2. pending basket
    state, pending = _process_pending(state, pending, md, as_of, cfg, auto_approve_soft, res)

    # 3. flows
    state = _process_flows(state, flows, md, as_of, cfg, res)

    # 4. rolls
    for alert in roll_alerts(state, as_of, cfg.futures.roll_window_business_days):
        res.actions.append(Event("ROLL_REQUIRED", alert.__dict__))

    # 5. new basket (unless one is still waiting for its execution day)
    if pending is None:
        review = _new_basket(state, md, as_of, cfg, res)
        if review is not None and review.status is not OrderStatus.REJECTED:
            pending = PendingBasket(review.basket, review.status, as_of)
            if cfg.workflow.execution_lag_days == 0:
                state, pending = _process_pending(
                    state, pending, md, as_of, cfg, auto_approve_soft, res
                )
    else:
        res.actions.append(Event("BASKET_PENDING", {"batch_id": pending.basket.batch_id}))

    # 6. overlay, post-trade checks, summary
    overlay = rebalance_overlay(state, md.quote(as_of), cfg)
    assert overlay.state is not None
    if overlay.es_trade or overlay.mes_trade or overlay.rolled:
        name = "FUTURES_ROLLED" if overlay.rolled else "FUTURES_REBALANCED"
        res.actions.append(Event(name, overlay.as_dict()))
    state = overlay.state

    inputs = md.compliance_inputs(as_of, list(state.shares.index))
    res.post_trade = post_trade_checks(state, cfg, inputs)
    for c in res.post_trade:
        if not c.passed:
            res.actions.append(
                Event(
                    "POST_TRADE_BREACH",
                    {
                        "check": c.name,
                        "severity": c.severity.value,
                        "value": c.value,
                        "message": c.message,
                    },
                )
            )
    res.state = state
    res.pending = pending
    res.actions.append(Event("DAY_COMPLETED", res.summary))
    return res


def flows_summary(flows: list[Flow]) -> pd.DataFrame:
    return pd.DataFrame([{"date": f.date, "amount": f.signed} for f in flows])
