"""Financial Modeling Prep client (``/stable`` API).

Endpoints used:

* ``sp500-constituent``                         -> S&P 500 members with GICS sector
* ``market-capitalization-batch``               -> current market caps (benchmark weights)
* ``historical-price-eod/dividend-adjusted``    -> adjusted daily closes + volume (stocks)
* ``historical-price-eod/full``                 -> daily closes (index level, ^GSPC)
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date
from typing import Any

import pandas as pd

from pit.data.http import DataSourceError, JsonHttpClient

# FMP occasionally uses its own sector taxonomy; map it to GICS names so that the
# benchmark and the model use the same sector labels.
SECTOR_ALIASES = {
    "Technology": "Information Technology",
    "Financial Services": "Financials",
    "Healthcare": "Health Care",
    "Consumer Cyclical": "Consumer Discretionary",
    "Consumer Defensive": "Consumer Staples",
    "Basic Materials": "Materials",
    "Communication": "Communication Services",
}

MARKET_CAP_BATCH_SIZE = 100


def _gics_sector(sector: str | None) -> str | None:
    return SECTOR_ALIASES.get(sector, sector) if sector else None


def to_fmp_symbol(ticker: str) -> str:
    """FMP writes share classes with a dash (BRK-B), not a dot (BRK.B)."""
    return ticker.replace(".", "-")


class FMPClient:
    def __init__(self, http: JsonHttpClient) -> None:
        self.http = http

    @classmethod
    def from_key(cls, api_key: str, base_url: str, **kwargs: Any) -> FMPClient:
        return cls(JsonHttpClient(base_url, api_key, "apikey", "FMP", **kwargs))

    def _list(self, path: str, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        data = self.http.get_json(path, params)
        if isinstance(data, dict) and ("Error Message" in data or "error" in data):
            msg = data.get("Error Message") or data.get("error")
            raise DataSourceError(f"FMP: {path}: {msg}")
        if not isinstance(data, list):
            raise DataSourceError(f"FMP: unexpected payload from {path}")
        return data

    def sp500_constituents(self) -> pd.DataFrame:
        """Columns: ticker, name, sector (GICS)."""
        rows = self._list("sp500-constituent")
        if not rows:
            raise DataSourceError("FMP: empty S&P 500 constituent list")
        df = pd.DataFrame(
            {
                "ticker": [r["symbol"] for r in rows],
                "name": [r.get("name") for r in rows],
                "sector": [_gics_sector(r.get("sector")) for r in rows],
            }
        )
        return df.drop_duplicates("ticker").reset_index(drop=True)

    def market_caps(self, tickers: Sequence[str]) -> pd.DataFrame:
        """Columns: ticker, date, market_cap. Requested in batches of 100 symbols."""
        out: list[dict[str, Any]] = []
        for i in range(0, len(tickers), MARKET_CAP_BATCH_SIZE):
            chunk = [to_fmp_symbol(t) for t in tickers[i : i + MARKET_CAP_BATCH_SIZE]]
            for r in self._list("market-capitalization-batch", {"symbols": ",".join(chunk)}):
                cap = r.get("marketCap")
                if cap:
                    out.append(
                        {
                            "ticker": r["symbol"],
                            "date": pd.Timestamp(r["date"]).date(),
                            "market_cap": float(cap),
                        }
                    )
        return pd.DataFrame(out, columns=["ticker", "date", "market_cap"])

    def historical_prices(
        self, symbol: str, start: date, end: date, adjusted: bool = True
    ) -> pd.DataFrame:
        """Columns: date, close, volume - sorted ascending.

        ``adjusted=True`` uses dividend- and split-adjusted closes, which is what
        returns, betas and covariances must be computed on.
        """
        path = "historical-price-eod/dividend-adjusted" if adjusted else "historical-price-eod/full"
        rows = self._list(
            path,
            {"symbol": to_fmp_symbol(symbol), "from": start.isoformat(), "to": end.isoformat()},
        )
        close_key = "adjClose" if adjusted else "close"
        records = [
            {
                "date": pd.Timestamp(r["date"]).date(),
                "close": float(r[close_key]),
                "volume": float(r["volume"]) if r.get("volume") is not None else None,
            }
            for r in rows
            if r.get(close_key) is not None and float(r[close_key]) > 0
        ]
        df = pd.DataFrame(records, columns=["date", "close", "volume"])
        return df.sort_values("date").reset_index(drop=True)
