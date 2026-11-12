from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import date
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pandas as pd
import pytest
import yaml
from sqlalchemy import Engine

from pit.config import DEFAULT_CONFIG_PATH, PROJECT_ROOT, AppConfig, load_config
from pit.db import init_db, make_engine

INPUTS = PROJECT_ROOT / "data" / "inputs"
FMP_BASE = "https://fmp.test/stable"
FRED_BASE = "https://fred.test/fred"


@pytest.fixture
def tmp_config_path(tmp_path: Path) -> Path:
    """Copy of config.yaml whose database lives in tmp_path and whose input files
    point at the real example inputs."""
    raw = yaml.safe_load(DEFAULT_CONFIG_PATH.read_text(encoding="utf-8"))
    raw["data"]["db_url"] = "sqlite:///test.db"
    raw["data"]["fmp_base_url"] = FMP_BASE
    raw["data"]["fred_base_url"] = FRED_BASE
    for key in ("target_weights_file", "offline_universe_file"):
        raw["data"][key] = str(PROJECT_ROOT / raw["data"][key])
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    return path


@pytest.fixture
def cfg(tmp_config_path: Path) -> AppConfig:
    return load_config(tmp_config_path)


@pytest.fixture
def engine(cfg: AppConfig) -> Iterator[Engine]:
    eng = make_engine(cfg.database_url)
    init_db(eng)
    yield eng
    eng.dispose()


# --------------------------------------------------------------------------- fake APIs


def _query(url: str) -> dict[str, str]:
    return {k: v[0] for k, v in parse_qs(urlparse(url).query).items()}


class FakeMarketAPI:
    """Callbacks for the ``responses`` library that imitate FMP and FRED.

    Prices are deterministic: close = 100 + day number, so tests can assert on
    exact values. ``fail_on`` makes a given endpoint return HTTP 500.
    """

    def __init__(self, tickers: list[str], fail_on: str | None = None) -> None:
        self.tickers = tickers
        self.fail_on = fail_on
        self.price_requests: list[tuple[str, str, str]] = []

    def register(self, rsps) -> None:  # type: ignore[no-untyped-def]
        import re

        rsps.add_callback("GET", f"{FMP_BASE}/sp500-constituent", callback=self._constituents)
        rsps.add_callback(
            "GET", f"{FMP_BASE}/market-capitalization-batch", callback=self._market_caps
        )
        rsps.add_callback(
            "GET", re.compile(rf"{re.escape(FMP_BASE)}/historical-price-eod/.*"), self._prices
        )
        rsps.add_callback("GET", f"{FRED_BASE}/series/observations", callback=self._fred)

    def _maybe_fail(self, name: str) -> tuple[int, dict, str] | None:
        return (500, {}, "boom") if self.fail_on == name else None

    def _constituents(self, request):  # type: ignore[no-untyped-def]
        if fail := self._maybe_fail("constituents"):
            return fail
        sector = "Technology"  # FMP alias -> must be mapped to Information Technology
        body = [{"symbol": t, "name": f"{t} Inc.", "sector": sector} for t in self.tickers]
        return 200, {}, json.dumps(body)

    def _market_caps(self, request):  # type: ignore[no-untyped-def]
        symbols = _query(request.url)["symbols"].split(",")
        body = [
            {"symbol": s, "date": "2024-03-28", "marketCap": 1e9 * (i + 1)}
            for i, s in enumerate(symbols)
        ]
        return 200, {}, json.dumps(body)

    def _prices(self, request):  # type: ignore[no-untyped-def]
        if fail := self._maybe_fail("prices"):
            return fail
        q = _query(request.url)
        self.price_requests.append((q["symbol"], q["from"], q["to"]))
        days = pd.bdate_range(q["from"], q["to"])
        key = "adjClose" if "dividend-adjusted" in request.url else "close"
        body = [
            {
                "symbol": q["symbol"],
                "date": d.strftime("%Y-%m-%d"),
                key: 100.0 + d.dayofyear,
                "volume": 1_000_000,
            }
            for d in reversed(days)  # FMP returns newest first
        ]
        return 200, {}, json.dumps(body)

    def _fred(self, request):  # type: ignore[no-untyped-def]
        if fail := self._maybe_fail("fred"):
            return fail
        q = _query(request.url)
        days = pd.bdate_range(q["observation_start"], q["observation_end"])
        obs = [{"date": d.strftime("%Y-%m-%d"), "value": "5.25"} for d in days]
        obs[0]["value"] = "."  # FRED marks missing observations with a dot
        return 200, {}, json.dumps({"observations": obs})


@pytest.fixture
def short_period() -> tuple[date, date]:
    return date(2024, 1, 2), date(2024, 3, 28)
