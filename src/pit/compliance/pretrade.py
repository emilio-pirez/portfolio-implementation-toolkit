"""Pre-trade review: basket -> pro-forma portfolio -> checks -> basket status."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import pandas as pd
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from pit.compliance.checks import CheckResult, ComplianceContext, decide_status, run_checks
from pit.config import AppConfig
from pit.db.models import AssetType, BenchmarkWeight, OrderStatus, Price, Security
from pit.equitization import EquitizationResult, FuturesQuote, de_equitize
from pit.orders.basket import Basket
from pit.portfolio.state import PortfolioState

# A risk model maps a portfolio to (ex-ante tracking error, ex-ante beta).
RiskModel = Callable[[PortfolioState], tuple[float, float]]


@dataclass(frozen=True)
class ComplianceInputs:
    sectors: pd.Series
    benchmark_weights: pd.Series
    restricted: frozenset[str]
    adv: pd.Series
    risk_model: RiskModel | None = None


@dataclass(frozen=True)
class BasketReview:
    basket: Basket
    pro_forma: EquitizationResult
    checks: list[CheckResult]
    status: OrderStatus

    @property
    def hard_breaches(self) -> list[CheckResult]:
        return [c for c in self.checks if c.is_hard_breach]

    @property
    def soft_breaches(self) -> list[CheckResult]:
        return [c for c in self.checks if c.is_soft_breach]

    def summary(self) -> dict[str, object]:
        return {
            **self.basket.summary(),
            "status": self.status.value,
            "hard_breaches": [c.name for c in self.hard_breaches],
            "soft_breaches": [c.name for c in self.soft_breaches],
            "pro_forma_exposure": self.pro_forma.exposure_after,
            "pro_forma_cash": self.pro_forma.cash_after,
            "futures_after": {"ES": self.pro_forma.es_after, "MES": self.pro_forma.mes_after},
        }


def build_context(
    state: PortfolioState, inputs: ComplianceInputs, orders: pd.DataFrame | None = None
) -> ComplianceContext:
    te, beta = inputs.risk_model(state) if inputs.risk_model else (None, None)
    return ComplianceContext(
        state=state,
        sectors=inputs.sectors,
        benchmark_weights=inputs.benchmark_weights,
        restricted=inputs.restricted,
        orders=orders if orders is not None else pd.DataFrame(),
        adv=inputs.adv,
        tracking_error=te,
        beta=beta,
    )


def review_basket(
    state: PortfolioState,
    basket: Basket,
    quote: FuturesQuote,
    cfg: AppConfig,
    inputs: ComplianceInputs,
) -> BasketReview:
    """Simulate the basket (at estimated prices and costs), resize the futures
    overlay, and run every check on the resulting pro-forma portfolio."""
    pro_forma = de_equitize(
        state,
        basket.signed_quantities,
        quote,
        cfg,
        prices=basket.prices,
        costs=basket.total_cost,
    )
    assert pro_forma.state is not None
    ctx = build_context(pro_forma.state, inputs, basket.orders)
    checks = run_checks(ctx, cfg.compliance)
    return BasketReview(basket, pro_forma, checks, decide_status(checks))


def post_trade_checks(
    state: PortfolioState, cfg: AppConfig, inputs: ComplianceInputs
) -> list[CheckResult]:
    """Same checks on the actual end-of-day portfolio (no orders)."""
    return run_checks(build_context(state, inputs), cfg.compliance)


# --------------------------------------------------------------------------- inputs from the DB


def read_restricted_list(path: Path) -> frozenset[str]:
    df = pd.read_csv(path)
    if "ticker" not in df.columns:
        raise ValueError(f"{path}: restricted list needs a 'ticker' column")
    return frozenset(df["ticker"].astype(str).str.strip().str.upper())


def load_sectors(session: Session) -> pd.Series:
    rows = session.execute(
        select(Security.ticker, Security.sector).where(
            Security.asset_type == AssetType.EQUITY.value
        )
    ).all()
    return pd.Series({t: s for t, s in rows}, dtype=object)


def load_benchmark_weights(session: Session, as_of: date) -> pd.Series:
    """Latest benchmark snapshot on or before ``as_of``.

    Only one snapshot is usually stored (see methodology: historical index
    weights are a paid dataset), so if none precedes ``as_of`` the earliest
    available snapshot is used.
    """
    snap = session.scalar(
        select(func.max(BenchmarkWeight.date)).where(BenchmarkWeight.date <= as_of)
    ) or session.scalar(select(func.min(BenchmarkWeight.date)))
    if snap is None:
        raise LookupError("no benchmark weights stored; run `pit load-data`")
    rows = session.execute(
        select(BenchmarkWeight.ticker, BenchmarkWeight.weight).where(BenchmarkWeight.date == snap)
    ).all()
    return pd.Series({t: w for t, w in rows}, dtype=float)


def load_adv(session: Session, as_of: date, window: int) -> pd.Series:
    """Average daily volume (shares) over the last ``window`` trading days <= ``as_of``."""
    rows = session.execute(
        select(Price.ticker, Price.date, Price.volume).where(
            Price.date <= as_of, Price.volume.is_not(None)
        )
    ).all()
    if not rows:
        return pd.Series(dtype=float)
    df = pd.DataFrame(rows, columns=["ticker", "date", "volume"]).sort_values("date")
    return df.groupby("ticker")["volume"].apply(lambda v: v.tail(window).mean()).astype(float)


def load_compliance_inputs(
    session: Session, as_of: date, cfg: AppConfig, risk_model: RiskModel | None = None
) -> ComplianceInputs:
    return ComplianceInputs(
        sectors=load_sectors(session),
        benchmark_weights=load_benchmark_weights(session, as_of),
        restricted=read_restricted_list(cfg.resolve_path(cfg.data.restricted_list_file)),
        adv=load_adv(session, as_of, cfg.orders.adv_window_days),
        risk_model=risk_model,
    )
