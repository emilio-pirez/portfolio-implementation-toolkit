"""Pre-trade and post-trade compliance checks.

Checks run on a **pro-forma** portfolio: the fund as it would look after the
basket is executed and the futures overlay is resized. Each check returns its
value, its limit, pass/fail and a severity:

* **HARD** - a breach blocks the basket (status ``REJECTED``);
* **SOFT** - a breach raises an alert; the basket needs a manual approval.

All limits come from ``config.yaml`` (``compliance`` section).

Weight conventions: position and sector weights are fractions of **NAV**,
and benchmark weights sum to 1. Futures exposure is not allocated to sectors
(it is index exposure, spread across all sectors).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

import pandas as pd

from pit.config import ComplianceConfig, LimitRule, RangeRule, Severity
from pit.db.models import OrderSide, OrderStatus
from pit.portfolio.state import PortfolioState

UNKNOWN_SECTOR = "Unknown"


@dataclass(frozen=True)
class CheckResult:
    name: str
    value: float | None
    limit: str
    passed: bool
    severity: Severity
    message: str

    @property
    def is_hard_breach(self) -> bool:
        return not self.passed and self.severity is Severity.HARD

    @property
    def is_soft_breach(self) -> bool:
        return not self.passed and self.severity is Severity.SOFT

    def as_row(self) -> dict[str, object]:
        return {
            "check_name": self.name,
            "value": self.value,
            "limit_value": self.limit,
            "passed": self.passed,
            "severity": self.severity.value,
            "message": self.message,
        }


@dataclass(frozen=True)
class ComplianceContext:
    """Everything the checks need, gathered once."""

    state: PortfolioState  # pro-forma (or actual, for post-trade checks)
    sectors: pd.Series  # ticker -> sector
    benchmark_weights: pd.Series  # ticker -> weight (sums to 1)
    restricted: frozenset[str] = frozenset()
    orders: pd.DataFrame = field(default_factory=pd.DataFrame)  # ticker, side, quantity
    adv: pd.Series = field(default_factory=lambda: pd.Series(dtype=float))  # shares/day
    tracking_error: float | None = None  # ex-ante, annualised (Phase 5 risk model)
    beta: float | None = None  # ex-ante vs the index

    def sector_weights(self) -> pd.Series:
        w = self.state.weights()
        sec = self.sectors.reindex(w.index).fillna(UNKNOWN_SECTOR)
        return w.groupby(sec).sum()

    def benchmark_sector_weights(self) -> pd.Series:
        b = self.benchmark_weights
        sec = self.sectors.reindex(b.index).fillna(UNKNOWN_SECTOR)
        return b.groupby(sec).sum()


# --------------------------------------------------------------------------- helpers


def _fmt_range(rule: RangeRule) -> str:
    return f"[{rule.min:g}, {rule.max:g}]"


def _upper(name: str, value: float, rule: LimitRule, what: str) -> CheckResult:
    ok = value <= rule.limit + 1e-12
    msg = f"{what} {value:.2%} {'within' if ok else 'above'} limit {rule.limit:.2%}"
    return CheckResult(name, value, f"{rule.limit:g}", ok, rule.severity, msg)


def _range(name: str, value: float, rule: RangeRule, what: str, pct: bool = True) -> CheckResult:
    ok = rule.min - 1e-12 <= value <= rule.max + 1e-12
    fmt = (lambda x: f"{x:.2%}") if pct else (lambda x: f"{x:g}")
    where = "within" if ok else ("below" if value < rule.min else "above")
    msg = f"{what} {fmt(value)} {where} range {fmt(rule.min)} - {fmt(rule.max)}"
    return CheckResult(name, value, _fmt_range(rule), ok, rule.severity, msg)


# --------------------------------------------------------------------------- the 10 checks


def check_max_issuer_weight(ctx: ComplianceContext, cfg: ComplianceConfig) -> CheckResult:
    w = ctx.state.weights()
    if w.empty:
        return _upper("max_issuer_weight", 0.0, cfg.max_issuer_weight, "largest position")
    ticker = str(w.idxmax())
    res = _upper("max_issuer_weight", float(w.max()), cfg.max_issuer_weight, f"{ticker} weight")
    return res


def check_max_sector_weight(ctx: ComplianceContext, cfg: ComplianceConfig) -> CheckResult:
    sw = ctx.sector_weights()
    if sw.empty:
        return _upper("max_sector_weight", 0.0, cfg.max_sector_weight, "largest sector")
    sector = str(sw.idxmax())
    return _upper("max_sector_weight", float(sw.max()), cfg.max_sector_weight, f"{sector} weight")


def check_sector_active_weight(ctx: ComplianceContext, cfg: ComplianceConfig) -> CheckResult:
    port, bench = ctx.sector_weights(), ctx.benchmark_sector_weights()
    idx = port.index.union(bench.index)
    active = port.reindex(idx, fill_value=0.0) - bench.reindex(idx, fill_value=0.0)
    if active.empty:
        value, sector, signed = 0.0, "-", 0.0
    else:
        sector = str(active.abs().idxmax())
        signed = float(active[sector])
        value = abs(signed)
    rule = cfg.sector_active_weight
    ok = value <= rule.limit + 1e-12
    msg = f"{sector} active weight {signed:+.2%} vs benchmark (limit +/-{rule.limit:.2%})"
    return CheckResult("sector_active_weight", value, f"{rule.limit:g}", ok, rule.severity, msg)


def check_cash_range(ctx: ComplianceContext, cfg: ComplianceConfig) -> CheckResult:
    return _range("cash_range", ctx.state.cash_pct, cfg.cash_range, "cash")


def check_total_exposure(ctx: ComplianceContext, cfg: ComplianceConfig) -> CheckResult:
    return _range(
        "total_exposure_range", ctx.state.exposure, cfg.total_exposure_range, "total exposure"
    )


def check_tracking_error(ctx: ComplianceContext, cfg: ComplianceConfig) -> CheckResult:
    rule = cfg.max_tracking_error
    if ctx.tracking_error is None:
        return CheckResult(
            "max_tracking_error",
            None,
            f"{rule.limit:g}",
            True,
            rule.severity,
            "not computed (no risk model supplied)",
        )
    return _upper("max_tracking_error", ctx.tracking_error, rule, "ex-ante tracking error")


def check_beta(ctx: ComplianceContext, cfg: ComplianceConfig) -> CheckResult:
    rule = cfg.beta_range
    if ctx.beta is None:
        return CheckResult(
            "beta_range",
            None,
            _fmt_range(rule),
            True,
            rule.severity,
            "not computed (no risk model supplied)",
        )
    return _range("beta_range", ctx.beta, rule, "ex-ante beta", pct=False)


def check_holdings_count(ctx: ComplianceContext, cfg: ComplianceConfig) -> CheckResult:
    n = float((ctx.state.shares > 0).sum())
    return _range("holdings_count_range", n, cfg.holdings_count_range, "holdings", pct=False)


def check_restricted_list(ctx: ComplianceContext, cfg: ComplianceConfig) -> CheckResult:
    """Fails if the fund would hold, or would buy, a restricted name.

    Selling a restricted name is allowed (it is how a breach is cured).
    """
    rule = cfg.restricted_list
    held = set(ctx.state.shares[ctx.state.shares > 0].index) & ctx.restricted
    bought: set[str] = set()
    if not ctx.orders.empty:
        buys = ctx.orders.loc[ctx.orders["side"] == OrderSide.BUY.value, "ticker"]
        bought = set(buys) & ctx.restricted
    hits = sorted(held | bought)
    ok = not hits
    msg = "no restricted names held or bought" if ok else f"restricted names: {hits}"
    return CheckResult(
        "restricted_list", float(len(hits)), f"{len(ctx.restricted)} names", ok, rule.severity, msg
    )


def check_order_vs_adv(ctx: ComplianceContext, cfg: ComplianceConfig) -> CheckResult:
    """Largest order as a share of average daily volume (liquidity / market impact)."""
    rule = cfg.max_order_pct_adv
    if ctx.orders.empty:
        return _upper("max_order_pct_adv", 0.0, rule, "largest order")
    qty = ctx.orders.set_index("ticker")["quantity"].astype(float)
    adv = ctx.adv.reindex(qty.index)
    no_volume = sorted(adv[(adv.isna()) | (adv <= 0)].index)
    if no_volume:
        return CheckResult(
            "max_order_pct_adv",
            None,
            f"{rule.limit:g}",
            False,
            rule.severity,
            f"no volume data for {no_volume}",
        )
    ratio = qty / adv
    ticker = str(ratio.idxmax())
    return _upper("max_order_pct_adv", float(ratio.max()), rule, f"{ticker} order / ADV")


CHECKS: tuple[Callable[[ComplianceContext, ComplianceConfig], CheckResult], ...] = (
    check_max_issuer_weight,
    check_max_sector_weight,
    check_sector_active_weight,
    check_cash_range,
    check_total_exposure,
    check_tracking_error,
    check_beta,
    check_holdings_count,
    check_restricted_list,
    check_order_vs_adv,
)


def run_checks(ctx: ComplianceContext, cfg: ComplianceConfig) -> list[CheckResult]:
    return [check(ctx, cfg) for check in CHECKS]


def decide_status(results: list[CheckResult]) -> OrderStatus:
    """HARD breach -> REJECTED; SOFT breach only -> PROPOSED (manual approval);
    everything passes -> APPROVED."""
    if any(r.is_hard_breach for r in results):
        return OrderStatus.REJECTED
    if any(r.is_soft_breach for r in results):
        return OrderStatus.PROPOSED
    return OrderStatus.APPROVED
