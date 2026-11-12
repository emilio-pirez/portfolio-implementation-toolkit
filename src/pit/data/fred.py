"""FRED client (St. Louis Fed) for the risk-free rate.

DTB3 is the 3-month T-Bill secondary-market rate, quoted in percent on a
discount basis. It is stored as a decimal (4.30 -> 0.0430). The difference
between the discount rate and the bond-equivalent yield is a few basis points
at current rate levels and is ignored (documented in docs/methodology.md).
"""

from __future__ import annotations

from datetime import date
from typing import Any

import pandas as pd

from pit.data.http import DataSourceError, JsonHttpClient


class FREDClient:
    def __init__(self, http: JsonHttpClient) -> None:
        self.http = http

    @classmethod
    def from_key(cls, api_key: str, base_url: str, **kwargs: Any) -> FREDClient:
        return cls(JsonHttpClient(base_url, api_key, "api_key", "FRED", **kwargs))

    def series(self, series_id: str, start: date, end: date) -> pd.DataFrame:
        """Columns: date, value (decimal). Missing observations ('.') are dropped."""
        data = self.http.get_json(
            "series/observations",
            {
                "series_id": series_id,
                "file_type": "json",
                "observation_start": start.isoformat(),
                "observation_end": end.isoformat(),
            },
        )
        if not isinstance(data, dict) or "observations" not in data:
            raise DataSourceError(f"FRED: unexpected payload for {series_id}")
        records = [
            {"date": pd.Timestamp(o["date"]).date(), "value": float(o["value"]) / 100.0}
            for o in data["observations"]
            if o.get("value") not in (None, ".", "")
        ]
        return pd.DataFrame(records, columns=["date", "value"])
