import math
from datetime import date

import pytest

from pit.equitization.futures import (
    active_quote,
    business_days_to,
    contract_code,
    fair_value,
    following_expiry,
    needs_roll,
    next_expiry,
    parse_contract_code,
    third_friday,
    year_fraction,
)


@pytest.mark.parametrize(
    ("year", "month", "expected"),
    [
        (2026, 3, date(2026, 3, 20)),
        (2026, 6, date(2026, 6, 19)),
        (2026, 9, date(2026, 9, 18)),
        (2026, 12, date(2026, 12, 18)),
        (2025, 3, date(2025, 3, 21)),
    ],
)
def test_third_friday(year: int, month: int, expected: date) -> None:
    assert third_friday(year, month) == expected
    assert expected.weekday() == 4


def test_next_expiry_rolls_over_year_end() -> None:
    assert next_expiry(date(2026, 11, 12)) == date(2026, 12, 18)
    assert next_expiry(date(2026, 12, 21)) == date(2027, 3, 19)


def test_on_expiry_day_front_is_next_contract() -> None:
    assert next_expiry(date(2026, 3, 20)) == date(2026, 6, 19)
    assert following_expiry(date(2026, 3, 20)) == date(2026, 6, 19)


def test_contract_codes() -> None:
    assert contract_code("ES", date(2026, 12, 18)) == "ESZ6"
    assert contract_code("MES", date(2027, 3, 19)) == "MESH7"
    assert parse_contract_code("MESZ6", date(2026, 11, 12)) == ("MES", date(2026, 12, 18))
    # year digit resolved to the nearest decade
    assert parse_contract_code("ESH0", date(2029, 12, 1)) == ("ES", date(2030, 3, 15))
    assert parse_contract_code("ESZ9", date(2030, 1, 5)) == ("ES", date(2029, 12, 21))


@pytest.mark.parametrize("bad", ["ES", "ESX6", "ESZa", "Z6"])
def test_parse_rejects_bad_codes(bad: str) -> None:
    with pytest.raises(ValueError):
        parse_contract_code(bad, date(2026, 1, 1))


def test_fair_value_by_hand() -> None:
    # S = 6,000, r = 4.3 %, q = 1.3 %, T = 0.25  ->  6,000 x e^(0.03 x 0.25) = 6,045.17
    assert fair_value(6_000, 0.043, 0.013, 0.25) == pytest.approx(6_045.169, abs=1e-3)
    # carry is zero when r = q
    assert fair_value(6_000, 0.02, 0.02, 1.0) == pytest.approx(6_000)
    with pytest.raises(ValueError):
        fair_value(0, 0.04, 0.01, 0.25)


def test_year_fraction_act_365() -> None:
    assert year_fraction(date(2026, 3, 20), date(2026, 6, 19)) == pytest.approx(91 / 365)
    assert year_fraction(date(2026, 7, 1), date(2026, 6, 19)) == 0.0


def test_roll_window() -> None:
    # Mon 16-Mar-2026 -> Fri 20-Mar-2026 expiry: Mon..Thu = 4 business days left
    assert business_days_to(date(2026, 3, 16), date(2026, 3, 20)) == 4
    assert needs_roll(date(2026, 3, 16), date(2026, 3, 20), 5)
    assert not needs_roll(date(2026, 3, 2), date(2026, 3, 20), 5)


def test_active_quote_skips_contract_inside_roll_window() -> None:
    q = active_quote(date(2026, 3, 16), 6_000, 0.04, 0.013, roll_window_business_days=5)
    assert q.expiry == date(2026, 6, 19)
    assert q.code("ES") == "ESM6"
    t = (date(2026, 6, 19) - date(2026, 3, 16)).days / 365
    assert q.price == pytest.approx(6_000 * math.exp((0.04 - 0.013) * t))


def test_active_quote_uses_front_outside_window() -> None:
    q = active_quote(date(2026, 2, 2), 6_000, 0.04, 0.013, roll_window_business_days=5)
    assert q.expiry == date(2026, 3, 20)
