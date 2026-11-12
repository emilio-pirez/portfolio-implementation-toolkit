"""Portfolio state and valuation.

A fund holds three things:

* **equities** - shares x close price = market value;
* **cash** - USD at the custodian;
* **index futures** - a contract has (almost) no market value: gains and losses
  are paid daily as variation margin, so after each settlement the position is
  worth zero. What matters is its **notional exposure** =
  contracts x futures price x multiplier.

Definitions used throughout the toolkit::

    NAV            = equity MV + cash + unsettled futures P&L
    exposure       = (equity MV + futures notional) / NAV
    cash %         = cash / NAV                       (physical cash, needed for margin)
    unequitized %  = (cash - futures notional) / NAV  (cash that is truly out of the market)

States are immutable: every operation returns a new ``PortfolioState``.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field, replace
from datetime import date

import pandas as pd


@dataclass(frozen=True)
class FuturesPosition:
    code: str  # e.g. "ESZ6"
    root: str  # "ES" or "MES"
    expiry: date
    multiplier: float  # USD per index point
    quantity: int  # contracts, positive = long
    price: float  # current futures price
    settle_price: float  # price at the last daily settlement

    @property
    def notional(self) -> float:
        return self.quantity * self.price * self.multiplier

    @property
    def unsettled_pnl(self) -> float:
        """Variation margin accrued since the last settlement."""
        return self.quantity * self.multiplier * (self.price - self.settle_price)


@dataclass(frozen=True)
class PortfolioState:
    as_of: date
    shares: pd.Series  # ticker -> shares held
    prices: pd.Series  # ticker -> close price
    cash: float
    futures: tuple[FuturesPosition, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        missing = sorted(set(self.shares.index[self.shares != 0]) - set(self.prices.index))
        if missing:
            raise ValueError(f"no price for held tickers: {missing}")

    # ------------------------------------------------------------------ valuation

    @property
    def market_values(self) -> pd.Series:
        held = self.shares[self.shares != 0]
        return (held * self.prices.reindex(held.index)).astype(float)

    @property
    def equity_value(self) -> float:
        return float(self.market_values.sum())

    @property
    def futures_notional(self) -> float:
        return float(sum(f.notional for f in self.futures))

    @property
    def unsettled_futures_pnl(self) -> float:
        return float(sum(f.unsettled_pnl for f in self.futures))

    @property
    def nav(self) -> float:
        return self.equity_value + self.cash + self.unsettled_futures_pnl

    @property
    def exposure(self) -> float:
        nav = self.nav
        return (self.equity_value + self.futures_notional) / nav if nav else 0.0

    @property
    def cash_pct(self) -> float:
        nav = self.nav
        return self.cash / nav if nav else 0.0

    @property
    def unequitized_cash(self) -> float:
        return self.cash - self.futures_notional

    def contracts(self, root: str) -> int:
        return sum(f.quantity for f in self.futures if f.root == root)

    def weights(self) -> pd.Series:
        """Equity positions as a fraction of NAV."""
        return self.market_values / self.nav

    def summary(self) -> dict[str, float]:
        nav = self.nav
        return {
            "nav": nav,
            "equity_value": self.equity_value,
            "cash": self.cash,
            "futures_notional": self.futures_notional,
            "unsettled_futures_pnl": self.unsettled_futures_pnl,
            "exposure": self.exposure,
            "cash_pct": self.cash / nav,
            "unequitized_pct": self.unequitized_cash / nav,
            "holdings": int((self.shares != 0).sum()),
        }

    # ------------------------------------------------------------------ updates

    def with_prices(self, as_of: date, prices: pd.Series) -> PortfolioState:
        """Re-mark equities to new closes (carrying forward any missing price)."""
        merged = prices.combine_first(self.prices)
        return replace(self, as_of=as_of, prices=merged)

    def mark_futures(self, price: float, expiry: date | None = None) -> PortfolioState:
        """Re-price futures; only contracts with ``expiry`` if given."""
        new = tuple(
            replace(f, price=price) if expiry is None or f.expiry == expiry else f
            for f in self.futures
        )
        return replace(self, futures=new)

    def settle_futures(self) -> PortfolioState:
        """Daily settlement: variation margin moves into cash, P&L resets to zero."""
        pnl = self.unsettled_futures_pnl
        new = tuple(replace(f, settle_price=f.price) for f in self.futures)
        return replace(self, cash=self.cash + pnl, futures=new)

    def apply_equity_trades(
        self, trades: pd.Series, prices: pd.Series | None = None, costs: float = 0.0
    ) -> PortfolioState:
        """Execute share trades (positive = buy) at ``prices`` (default: current closes).

        Cash moves by -sum(trade x price) - costs.
        """
        px = (prices if prices is not None else self.prices).reindex(trades.index)
        if px.isna().any():
            raise ValueError(f"no execution price for {sorted(px[px.isna()].index)}")
        new_shares = self.shares.add(trades, fill_value=0.0)
        new_shares = new_shares[new_shares.abs() > 1e-9]
        if (new_shares < 0).any():
            raise ValueError(
                f"trade would leave short positions: {sorted(new_shares[new_shares < 0].index)}"
            )
        cash = self.cash - float((trades * px).sum()) - costs
        return replace(self, shares=new_shares, prices=px.combine_first(self.prices), cash=cash)

    def with_cash(self, delta: float) -> PortfolioState:
        return replace(self, cash=self.cash + delta)


# --------------------------------------------------------------------------- construction


def initialize_portfolio(
    as_of: date,
    nav: float,
    target_weights: pd.Series,
    prices: pd.Series,
    cash_buffer_pct: float,
    round_lot: int = 1,
) -> PortfolioState:
    """Build the fund on day one: invest NAV x (1 - buffer) at the model weights.

    Shares are rounded **down** to the lot size so the purchase never needs more
    cash than available; the rounding residue stays in cash.
    """
    px = prices.reindex(target_weights.index)
    if px.isna().any():
        raise ValueError(f"no price for model tickers: {sorted(px[px.isna()].index)}")
    investable = nav * (1.0 - cash_buffer_pct)
    raw = target_weights * investable / px
    shares = raw.apply(lambda x: math.floor(x / round_lot) * round_lot).astype(float)
    cost = float((shares * px).sum())
    return PortfolioState(
        as_of=as_of, shares=shares[shares > 0], prices=prices.astype(float), cash=nav - cost
    )


def weights_vs_target(
    state: PortfolioState,
    target_weights: pd.Series,
    invested_fraction: float = 1.0,
    benchmark_weights: pd.Series | None = None,
) -> pd.DataFrame:
    """Current vs. target (and optionally benchmark) weights, as fractions of NAV.

    Target weights describe the equity sleeve (they sum to 1), so they are
    scaled by ``invested_fraction`` (= 1 - cash buffer) to be comparable with
    weights of NAV.
    """
    current = state.weights()
    target = target_weights * invested_fraction
    idx = current.index.union(target.index)
    df = pd.DataFrame(
        {
            "current": current.reindex(idx, fill_value=0.0),
            "target": target.reindex(idx, fill_value=0.0),
        }
    )
    df["diff"] = df["current"] - df["target"]
    if benchmark_weights is not None:
        df["benchmark"] = benchmark_weights.reindex(idx, fill_value=0.0)
        df["active"] = df["current"] - df["benchmark"]
    return df.sort_values("target", ascending=False)
