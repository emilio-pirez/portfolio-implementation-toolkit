"""Client cash flows: from a CSV file or randomly generated for backtests."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd

from pit.config import BacktestConfig
from pit.db.models import CashFlowType


@dataclass(frozen=True)
class Flow:
    date: date
    amount: float  # always positive; ``type`` gives the direction
    type: CashFlowType

    @property
    def signed(self) -> float:
        return self.amount if self.type is CashFlowType.SUBSCRIPTION else -self.amount


def read_cash_flows(path: Path) -> list[Flow]:
    """``date,amount,type`` with type SUBSCRIPTION or REDEMPTION and amount > 0."""
    if not path.exists():
        return []
    df = pd.read_csv(path)
    missing = {"date", "amount", "type"} - set(df.columns)
    if missing:
        raise ValueError(f"{path.name}: missing columns {sorted(missing)}")
    flows = []
    for row in df.to_dict("records"):
        kind = str(row["type"]).strip().upper()
        if kind not in CashFlowType.__members__:
            raise ValueError(f"{path.name}: unknown flow type {row['type']!r}")
        amount = float(row["amount"])
        if amount <= 0:
            raise ValueError(f"{path.name}: flow amounts must be positive (use type for direction)")
        flows.append(Flow(pd.Timestamp(str(row["date"])).date(), amount, CashFlowType(kind)))
    return flows


def by_date(flows: list[Flow]) -> dict[date, list[Flow]]:
    out: dict[date, list[Flow]] = defaultdict(list)
    for f in flows:
        out[f.date].append(f)
    return dict(out)


def random_flows(days: list[date], nav: float, cfg: BacktestConfig) -> list[Flow]:
    """Seeded random flows sized as a share of the *initial* NAV, so both
    backtest scenarios see exactly the same flows."""
    rng = np.random.default_rng(cfg.flow_seed)
    flows = []
    for d in days[1:]:  # no flow on the inception day
        if rng.random() < cfg.flow_probability:
            size = rng.uniform(cfg.flow_min_pct, cfg.flow_max_pct) * nav
            kind = (
                CashFlowType.SUBSCRIPTION
                if rng.random() < cfg.subscription_share
                else CashFlowType.REDEMPTION
            )
            flows.append(Flow(d, round(size, -3), kind))
    return flows
