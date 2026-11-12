"""Cash equitization with index futures.

Problem: when a subscription arrives the cash is out of the market until the
stock basket is bought; when stocks are bought the futures must be unwound;
when a redemption arrives cash must be raised without leaving the fund over-
or under-exposed.

All three cases are handled by one idea, the **futures overlay**: on any day
the fund should hold futures for the cash it does not need to keep::

    cash to equitize  = max(cash - buffer% x NAV, 0)
    target notional   = cash to equitize x beta_target
    N_ES              = floor(target notional / (F x 50))          (never over-expose)
    N_MES             = round((target notional - N_ES F 50) / (F x 5))

* subscription  -> cash rises  -> overlay buys futures   (equitize)
* stock buys    -> cash falls  -> overlay sells futures  (de-equitize, proportional
                                  to the dollars invested)
* redemption    -> cash pays out; if it is not enough (plus the buffer), stocks
                   are sold pro rata; then the overlay is resized

Rounding: ES is floored so the big contract never adds exposure beyond the
cash; MES is rounded to the nearest contract, so the residual is at most half a
micro contract (~USD 15k at F = 6,000), i.e. ~0.003 % of a USD 500 m fund.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field, replace
from datetime import date

import pandas as pd

from pit.config import AppConfig
from pit.equitization.futures import FuturesQuote, business_days_to, contract_code, needs_roll
from pit.portfolio.state import FuturesPosition, PortfolioState

EPS = 1e-9


@dataclass(frozen=True)
class FuturesSizing:
    target_notional: float
    futures_price: float
    es_contracts: int
    mes_contracts: int
    es_notional: float
    mes_notional: float

    @property
    def futures_notional(self) -> float:
        return self.es_notional + self.mes_notional

    @property
    def residual(self) -> float:
        """Exposure left uncovered because contracts are indivisible."""
        return self.target_notional - self.futures_notional


def size_futures(
    target_notional: float,
    futures_price: float,
    es_multiplier: float = 50.0,
    mes_multiplier: float = 5.0,
) -> FuturesSizing:
    """Contracts needed for a dollar exposure (negative notional -> short)."""
    if futures_price <= 0:
        raise ValueError("futures price must be positive")
    sign = 1 if target_notional >= 0 else -1
    amount = abs(target_notional)
    es_unit = futures_price * es_multiplier
    mes_unit = futures_price * mes_multiplier
    n_es = math.floor(amount / es_unit + EPS)
    residual = amount - n_es * es_unit
    n_mes = round(residual / mes_unit)
    return FuturesSizing(
        target_notional=target_notional,
        futures_price=futures_price,
        es_contracts=sign * n_es,
        mes_contracts=sign * n_mes,
        es_notional=sign * n_es * es_unit,
        mes_notional=sign * n_mes * mes_unit,
    )


@dataclass(frozen=True)
class EquitizationResult:
    """What the overlay did and the resulting portfolio."""

    action: str  # SUBSCRIPTION | REDEMPTION | DE_EQUITIZE | REBALANCE
    as_of: date
    flow: float
    contract_expiry: date
    futures_price: float
    es_trade: int
    mes_trade: int
    es_after: int
    mes_after: int
    target_notional: float
    futures_notional: float
    unequitized_cash: float  # target notional not covered (rounding residual)
    exposure_before: float
    exposure_after: float
    cash_after: float
    stock_sales: pd.Series = field(default_factory=lambda: pd.Series(dtype=float))
    rolled: tuple[str, ...] = ()
    state: PortfolioState | None = None

    @property
    def stock_sales_notional(self) -> float:
        if self.state is None or self.stock_sales.empty:
            return 0.0
        px = self.state.prices.reindex(self.stock_sales.index)
        return float(-(self.stock_sales * px).sum())

    def as_dict(self) -> dict[str, object]:
        """Flat summary for the audit log (no DataFrames / states)."""
        d = {k: v for k, v in self.__dict__.items() if k not in ("state", "stock_sales")}
        d["stock_sales_notional"] = self.stock_sales_notional
        d["stocks_sold"] = int(len(self.stock_sales))
        return d


def _multipliers(cfg: AppConfig) -> tuple[float, float]:
    c = cfg.futures.contracts
    return c["ES"].multiplier, c["MES"].multiplier


def cash_to_equitize(state: PortfolioState, cash_buffer_pct: float) -> float:
    """Cash above the buffer (never negative: the overlay is long-only)."""
    return max(state.cash - cash_buffer_pct * state.nav, 0.0)


def rebalance_overlay(
    state: PortfolioState,
    quote: FuturesQuote,
    cfg: AppConfig,
    action: str = "REBALANCE",
    flow: float = 0.0,
    exposure_before: float | None = None,
    stock_sales: pd.Series | None = None,
) -> EquitizationResult:
    """Resize the futures position to equitize exactly the cash above the buffer.

    Positions in any other contract month are closed and reopened in the
    quoted contract (a roll). Futures are re-marked to ``quote.price`` first.
    """
    es_mult, mes_mult = _multipliers(cfg)
    before = state.exposure if exposure_before is None else exposure_before

    # Close-and-reopen anything not in the active contract month (roll).
    rolled = tuple(sorted({f.code for f in state.futures if f.expiry != quote.expiry}))
    current = {
        "ES": sum(f.quantity for f in state.futures if f.root == "ES"),
        "MES": sum(f.quantity for f in state.futures if f.root == "MES"),
    }
    # Variation margin on the old contracts is realised before resizing.
    marked = state.settle_futures() if state.futures else state

    target = cash_to_equitize(marked, cfg.equitization.cash_buffer_pct) * cfg.futures.target_beta
    sizing = size_futures(target, quote.price, es_mult, mes_mult)

    new_futures = tuple(
        FuturesPosition(
            code=contract_code(root, quote.expiry),
            root=root,
            expiry=quote.expiry,
            multiplier=mult,
            quantity=qty,
            price=quote.price,
            settle_price=quote.price,
        )
        for root, mult, qty in (
            ("ES", es_mult, sizing.es_contracts),
            ("MES", mes_mult, sizing.mes_contracts),
        )
        if qty != 0
    )
    after = replace(marked, futures=new_futures)
    return EquitizationResult(
        action=action,
        as_of=state.as_of,
        flow=flow,
        contract_expiry=quote.expiry,
        futures_price=quote.price,
        es_trade=sizing.es_contracts - current["ES"],
        mes_trade=sizing.mes_contracts - current["MES"],
        es_after=sizing.es_contracts,
        mes_after=sizing.mes_contracts,
        target_notional=target,
        futures_notional=sizing.futures_notional,
        unequitized_cash=sizing.residual,
        exposure_before=before,
        exposure_after=after.exposure,
        cash_after=after.cash,
        stock_sales=stock_sales if stock_sales is not None else pd.Series(dtype=float),
        rolled=rolled,
        state=after,
    )


def equitize_subscription(
    state: PortfolioState, amount: float, quote: FuturesQuote, cfg: AppConfig
) -> EquitizationResult:
    """Receive a subscription and immediately cover it with futures."""
    if amount <= 0:
        raise ValueError("subscription amount must be positive")
    before = state.exposure
    return rebalance_overlay(state.with_cash(amount), quote, cfg, "SUBSCRIPTION", amount, before)


def de_equitize(
    state: PortfolioState,
    stock_trades: pd.Series,
    quote: FuturesQuote,
    cfg: AppConfig,
    prices: pd.Series | None = None,
    costs: float = 0.0,
) -> EquitizationResult:
    """Execute stock trades and unwind the futures they replace.

    Buying USD X of stock lowers cash by X, so the overlay sells about
    X / (F x multiplier) contracts: futures are sold in proportion to the
    dollars that went into the stocks.
    """
    before = state.exposure
    traded = state.apply_equity_trades(stock_trades, prices, costs)
    return rebalance_overlay(traded, quote, cfg, "DE_EQUITIZE", 0.0, before)


def pro_rata_sales(state: PortfolioState, amount: float, round_lot: int = 1) -> pd.Series:
    """Shares to sell to raise ``amount`` USD, pro rata to current holdings.

    Selling the same fraction of every position keeps the portfolio's weights
    (and so its active risk) unchanged. Shares are rounded **up** to the lot
    so that enough cash is raised, and capped at the shares held.
    """
    mv = state.market_values
    if amount <= 0 or mv.empty:
        return pd.Series(dtype=float)
    fraction = min(amount / mv.sum(), 1.0)
    held = state.shares.reindex(mv.index)
    raw = held * fraction
    lots = raw.apply(lambda x: math.ceil(x / round_lot - EPS) * round_lot).astype(float)
    return -lots.clip(upper=held)[lots > 0]


def meet_redemption(
    state: PortfolioState, amount: float, quote: FuturesQuote, cfg: AppConfig
) -> EquitizationResult:
    """Pay a redemption: cash first; sell stocks only for the shortfall.

    Cash needed = redemption + buffer on the post-redemption NAV, so the fund
    still holds its minimum cash afterwards. If cash is short, stocks are sold
    pro rata (executed at today's close in the simulation; in production they
    would go through the order and compliance workflow). The overlay is then
    resized: futures that were equitizing the cash paid out are sold.
    """
    if amount <= 0:
        raise ValueError("redemption amount must be positive")
    if amount >= state.nav:
        raise ValueError("redemption exceeds NAV")
    before = state.exposure
    settled = state.settle_futures() if state.futures else state
    nav_after = settled.nav - amount
    needed = amount + cfg.equitization.cash_buffer_pct * nav_after
    shortfall = needed - settled.cash

    sales = pd.Series(dtype=float)
    if shortfall > 0:
        sales = pro_rata_sales(settled, shortfall, cfg.orders.round_lot)
        settled = settled.apply_equity_trades(sales)

    paid = settled.with_cash(-amount)
    return rebalance_overlay(paid, quote, cfg, "REDEMPTION", -amount, before, sales)


@dataclass(frozen=True)
class RollAlert:
    code: str
    expiry: date
    business_days_left: int
    quantity: int


def roll_alerts(state: PortfolioState, as_of: date, window_business_days: int) -> list[RollAlert]:
    """Futures positions inside the roll window (the workflow logs these)."""
    return [
        RollAlert(f.code, f.expiry, business_days_to(as_of, f.expiry), f.quantity)
        for f in state.futures
        if needs_roll(as_of, f.expiry, window_business_days)
    ]
