"""Equity index futures: expiry calendar, contract codes and theoretical fair value.

S&P 500 futures (ES, MES) expire quarterly on the third Friday of March, June,
September and December. Contract codes follow the CME convention
root + month code + last digit of the year, e.g. ``ESZ6`` = E-mini Dec-2026.

Fair value (cost of carry)::

    F = S * exp((r - q) * T)

* S - index level (SPX close)
* r - risk-free rate (3M T-Bill, used as a continuously-compounded proxy)
* q - index dividend yield (``futures.index_dividend_yield``)
* T - years to expiry, ACT/365

Holding the futures instead of the stocks means not paying for the stocks
today (earning r on the cash) but also not receiving the dividends (losing q).
This is an approximation: in production the actual traded contract price is
used, which also embeds the market's financing spread ("roll richness/cheapness").
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, timedelta

import numpy as np

QUARTERLY_MONTHS = (3, 6, 9, 12)
MONTH_CODES = {3: "H", 6: "M", 9: "U", 12: "Z"}
CODE_MONTHS = {v: k for k, v in MONTH_CODES.items()}
DAYS_PER_YEAR = 365.0


def third_friday(year: int, month: int) -> date:
    first = date(year, month, 1)
    # weekday(): Monday=0 ... Friday=4
    offset = (4 - first.weekday()) % 7
    return first + timedelta(days=offset + 14)


def next_expiry(as_of: date) -> date:
    """First quarterly expiry strictly after ``as_of``.

    On expiry day itself the contract settles at the open, so the front
    contract is already the next one.
    """
    year = as_of.year
    for _ in range(2):
        for month in QUARTERLY_MONTHS:
            exp = third_friday(year, month)
            if exp > as_of:
                return exp
        year += 1
    raise AssertionError("unreachable")  # pragma: no cover


def following_expiry(expiry: date) -> date:
    """The quarterly expiry after ``expiry``."""
    return next_expiry(expiry)


def contract_code(root: str, expiry: date) -> str:
    return f"{root}{MONTH_CODES[expiry.month]}{expiry.year % 10}"


def parse_contract_code(code: str, as_of: date) -> tuple[str, date]:
    """``"MESZ6"`` -> ``("MES", date(2026, 12, 18))``.

    The single year digit is ambiguous across decades; it is resolved to the
    expiry closest to ``as_of``.
    """
    root, month_code, digit = code[:-2], code[-2], code[-1]
    if month_code not in CODE_MONTHS or not digit.isdigit() or not root:
        raise ValueError(f"not a quarterly futures code: {code}")
    month = CODE_MONTHS[month_code]
    decade = as_of.year - as_of.year % 10
    candidates = [third_friday(decade + k + int(digit), month) for k in (-10, 0, 10)]
    return root, min(candidates, key=lambda d: abs((d - as_of).days))


def year_fraction(as_of: date, expiry: date) -> float:
    """ACT/365 year fraction (never negative)."""
    return max((expiry - as_of).days, 0) / DAYS_PER_YEAR


def fair_value(spot: float, rate: float, dividend_yield: float, t_years: float) -> float:
    """Cost-of-carry futures price ``S * exp((r - q) T)``."""
    if spot <= 0:
        raise ValueError("spot must be positive")
    return spot * math.exp((rate - dividend_yield) * t_years)


def business_days_to(as_of: date, expiry: date) -> int:
    """Mon-Fri days from ``as_of`` (inclusive) to ``expiry`` (exclusive)."""
    return int(np.busday_count(as_of, expiry))


def needs_roll(as_of: date, expiry: date, window_business_days: int) -> bool:
    """True when the contract is within the roll window (or already expired)."""
    return business_days_to(as_of, expiry) <= window_business_days


@dataclass(frozen=True)
class FuturesQuote:
    """Price of the contract the fund should hold on ``as_of``."""

    expiry: date
    price: float
    t_years: float
    spot: float
    rate: float
    dividend_yield: float

    def code(self, root: str) -> str:
        return contract_code(root, self.expiry)


def active_quote(
    as_of: date,
    spot: float,
    rate: float,
    dividend_yield: float,
    roll_window_business_days: int,
) -> FuturesQuote:
    """Fair-value quote for the contract to trade today.

    That is the front contract, unless it is inside the roll window, in which
    case new exposure goes straight into the next contract.
    """
    expiry = next_expiry(as_of)
    if needs_roll(as_of, expiry, roll_window_business_days):
        expiry = following_expiry(expiry)
    t = year_fraction(as_of, expiry)
    return FuturesQuote(
        expiry=expiry,
        price=fair_value(spot, rate, dividend_yield, t),
        t_years=t,
        spot=spot,
        rate=rate,
        dividend_yield=dividend_yield,
    )
