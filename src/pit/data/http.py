"""Thin HTTP layer shared by the market-data clients: timeouts, retries with
exponential back-off on 429 / 5xx, and API keys kept out of error messages."""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

import requests

RETRY_STATUSES = {429, 500, 502, 503, 504}
AUTH_STATUSES = {401, 402, 403}


class DataSourceError(RuntimeError):
    """Raised when a market-data provider cannot return usable data."""


class JsonHttpClient:
    def __init__(
        self,
        base_url: str,
        api_key: str,
        api_key_param: str,
        provider: str,
        session: requests.Session | None = None,
        timeout: float = 20.0,
        max_retries: int = 3,
        backoff_seconds: float = 1.0,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if not api_key:
            raise DataSourceError(f"{provider}: missing API key")
        self.base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._api_key_param = api_key_param
        self.provider = provider
        self.session = session or requests.Session()
        self.timeout = timeout
        self.max_retries = max_retries
        self.backoff_seconds = backoff_seconds
        self._sleep = sleep
        self.calls = 0  # number of HTTP requests sent (useful to verify caching)

    def get_json(self, path: str, params: dict[str, Any] | None = None) -> Any:
        url = f"{self.base_url}/{path.lstrip('/')}"
        query = dict(params or {})
        query[self._api_key_param] = self._api_key

        last_error = ""
        for attempt in range(self.max_retries + 1):
            self.calls += 1
            try:
                resp = self.session.get(url, params=query, timeout=self.timeout)
            except requests.RequestException as exc:
                last_error = type(exc).__name__
            else:
                if resp.status_code == 200:
                    try:
                        return resp.json()
                    except ValueError as exc:
                        raise DataSourceError(f"{self.provider}: invalid JSON from {path}") from exc
                if resp.status_code in AUTH_STATUSES:
                    raise DataSourceError(
                        f"{self.provider}: HTTP {resp.status_code} on {path} - check the API "
                        "key and whether this endpoint is included in your plan"
                    )
                if resp.status_code not in RETRY_STATUSES:
                    raise DataSourceError(f"{self.provider}: HTTP {resp.status_code} on {path}")
                last_error = f"HTTP {resp.status_code}"

            if attempt < self.max_retries:
                self._sleep(self.backoff_seconds * 2**attempt)

        raise DataSourceError(
            f"{self.provider}: {path} failed after {self.max_retries + 1} attempts ({last_error})"
        )
