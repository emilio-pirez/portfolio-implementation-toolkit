"""Trade basket (trade list) generation.

Given current holdings, the PM's target weights and today's prices::

    equity budget   = NAV x (1 - cash buffer)
    target shares_i = round_to_lot(w_i x equity budget / price_i)
    order_i         = target shares_i - current shares_i     (> 0 BUY, < 0 SELL)

Names held but not in the model have a target of zero (full exit).

**No-trade band.** An order whose size is below ``no_trade_threshold_bps`` of
NAV is dropped: a 2 bp trade costs about as much as it fixes. Full exits are
always kept so the fund never carries small stub positions in names the PM
removed.

**Estimated cost** (linear model, per order)::

    est_cost = |notional| x (commission_bps + half_spread_bps) / 10,000

Every basket gets a ``batch_id`` that links its orders, its compliance checks
and its audit-log entries.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import pandas as pd

from pit.config import AppConfig
from pit.db.models import OrderSide
from pit.portfolio.state import PortfolioState

ORDER_COLUMNS = [
    "ticker",
    "side",
    "quantity",
    "est_price",
    "est_notional",
    "est_cost",
    "current_shares",
    "target_shares",
    "weight_change",
]
EXPORT_COLUMNS = ["ticker", "side", "quantity", "est_price", "est_notional"]


@dataclass(frozen=True)
class Basket:
    batch_id: str
    as_of: date
    orders: pd.DataFrame  # ORDER_COLUMNS, one row per traded ticker
    skipped: pd.DataFrame  # same columns: orders dropped by the no-trade band

    @property
    def signed_quantities(self) -> pd.Series:
        """Shares to trade, positive = buy, indexed by ticker."""
        if self.orders.empty:
            return pd.Series(dtype=float)
        sign = self.orders["side"].map({OrderSide.BUY.value: 1.0, OrderSide.SELL.value: -1.0})
        return pd.Series(
            (sign * self.orders["quantity"]).to_numpy(), index=self.orders["ticker"].to_numpy()
        )

    @property
    def prices(self) -> pd.Series:
        return pd.Series(
            self.orders["est_price"].to_numpy(), index=self.orders["ticker"].to_numpy()
        )

    def _notional(self, side: str) -> float:
        return float(self.orders.loc[self.orders["side"] == side, "est_notional"].sum())

    @property
    def buy_notional(self) -> float:
        return self._notional(OrderSide.BUY.value)

    @property
    def sell_notional(self) -> float:
        return self._notional(OrderSide.SELL.value)

    @property
    def total_cost(self) -> float:
        return float(self.orders["est_cost"].sum())

    def summary(self) -> dict[str, object]:
        return {
            "batch_id": self.batch_id,
            "as_of": self.as_of,
            "orders": len(self.orders),
            "skipped_below_threshold": len(self.skipped),
            "buy_notional": self.buy_notional,
            "sell_notional": self.sell_notional,
            "est_cost": self.total_cost,
        }


def new_batch_id() -> str:
    return str(uuid.uuid4())


def round_to_lot(shares: pd.Series, lot: int) -> pd.Series:
    """Nearest multiple of ``lot`` (lot = 1 means whole shares)."""
    return ((shares / lot).round() * lot).astype(float)


def generate_basket(
    state: PortfolioState,
    target_weights: pd.Series,
    cfg: AppConfig,
    prices: pd.Series | None = None,
    batch_id: str | None = None,
) -> Basket:
    """Orders that move ``state`` to the target weights at ``prices``."""
    px_all = prices if prices is not None else state.prices
    nav = state.nav
    budget = nav * (1.0 - cfg.equitization.cash_buffer_pct)

    universe = target_weights.index.union(state.shares.index)
    px = px_all.reindex(universe)
    missing = sorted(px[px.isna()].index)
    if missing:
        raise ValueError(f"no price for {missing}")

    w = target_weights.reindex(universe, fill_value=0.0)
    current = state.shares.reindex(universe, fill_value=0.0)
    target = round_to_lot(w * budget / px, cfg.orders.round_lot)
    delta = target - current

    df = pd.DataFrame(
        {
            "ticker": universe,
            "current_shares": current.to_numpy(),
            "target_shares": target.to_numpy(),
            "delta": delta.to_numpy(),
            "est_price": px.to_numpy(),
        }
    )
    df = df[df["delta"] != 0].copy()
    df["side"] = df["delta"].map(lambda d: OrderSide.BUY.value if d > 0 else OrderSide.SELL.value)
    df["quantity"] = df["delta"].abs()
    df["est_notional"] = df["quantity"] * df["est_price"]
    df["est_cost"] = df["est_notional"] * cfg.costs.total_bps / 10_000
    df["weight_change"] = df["delta"] * df["est_price"] / nav

    threshold = cfg.orders.no_trade_threshold_bps / 10_000
    full_exit = df["target_shares"] == 0
    keep = (df["weight_change"].abs() >= threshold) | full_exit

    def _frame(mask: pd.Series) -> pd.DataFrame:
        out = df.loc[mask, ORDER_COLUMNS]
        return out.sort_values(["side", "est_notional"], ascending=[True, False]).reset_index(
            drop=True
        )

    return Basket(
        batch_id=batch_id or new_batch_id(),
        as_of=state.as_of,
        orders=_frame(keep),
        skipped=_frame(~keep),
    )


def export_basket_csv(basket: Basket, path: Path) -> Path:
    """Write the trading-desk file: ticker, side, quantity, est_price, est_notional."""
    path.parent.mkdir(parents=True, exist_ok=True)
    out = basket.orders[EXPORT_COLUMNS].copy()
    out["quantity"] = out["quantity"].astype(int)
    out["est_price"] = out["est_price"].round(4)
    out["est_notional"] = out["est_notional"].round(2)
    out.to_csv(path, index=False)
    return path
