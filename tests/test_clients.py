"""Market-data clients, tested against mocked HTTP responses (no network)."""

from datetime import date

import pytest
import requests
import responses

from pit.data.fmp import FMPClient, to_fmp_symbol
from pit.data.fred import FREDClient
from pit.data.http import DataSourceError, JsonHttpClient
from tests.conftest import FMP_BASE, FRED_BASE, FakeMarketAPI

SECRET = "super-secret-key"


def _http(**kw) -> JsonHttpClient:  # type: ignore[no-untyped-def]
    return JsonHttpClient(FMP_BASE, SECRET, "apikey", "FMP", sleep=lambda _s: None, **kw)


# --------------------------------------------------------------------------- http layer


def test_missing_key_raises() -> None:
    with pytest.raises(DataSourceError, match="missing API key"):
        JsonHttpClient(FMP_BASE, "", "apikey", "FMP")


@responses.activate
def test_retries_on_429_then_succeeds() -> None:
    url = f"{FMP_BASE}/x"
    responses.add(responses.GET, url, status=429)
    responses.add(responses.GET, url, status=503)
    responses.add(responses.GET, url, json=[1, 2])
    http = _http()
    assert http.get_json("x") == [1, 2]
    assert http.calls == 3
    assert f"apikey={SECRET}" in responses.calls[0].request.url


@responses.activate
def test_gives_up_after_max_retries() -> None:
    responses.add(responses.GET, f"{FMP_BASE}/x", status=500)
    http = _http(max_retries=2)
    with pytest.raises(DataSourceError, match="failed after 3 attempts") as exc:
        http.get_json("x")
    assert SECRET not in str(exc.value)


@responses.activate
def test_connection_errors_are_retried() -> None:
    responses.add(responses.GET, f"{FMP_BASE}/x", body=requests.ConnectionError("down"))
    with pytest.raises(DataSourceError, match="ConnectionError"):
        _http(max_retries=1).get_json("x")


@responses.activate
@pytest.mark.parametrize("status", [401, 402, 403])
def test_auth_errors_fail_fast(status: int) -> None:
    responses.add(responses.GET, f"{FMP_BASE}/x", status=status)
    http = _http()
    with pytest.raises(DataSourceError, match="check the API key") as exc:
        http.get_json("x")
    assert http.calls == 1
    assert SECRET not in str(exc.value)


@responses.activate
def test_other_client_errors_are_not_retried() -> None:
    responses.add(responses.GET, f"{FMP_BASE}/x", status=404)
    http = _http()
    with pytest.raises(DataSourceError, match="HTTP 404"):
        http.get_json("x")
    assert http.calls == 1


@responses.activate
def test_invalid_json() -> None:
    responses.add(responses.GET, f"{FMP_BASE}/x", body="not json")
    with pytest.raises(DataSourceError, match="invalid JSON"):
        _http().get_json("x")


# --------------------------------------------------------------------------- FMP


def _fmp() -> FMPClient:
    return FMPClient(_http())


def test_to_fmp_symbol() -> None:
    assert to_fmp_symbol("BRK.B") == "BRK-B"
    assert to_fmp_symbol("AAPL") == "AAPL"


@responses.activate
def test_constituents_parsed_and_sector_mapped_to_gics() -> None:
    FakeMarketAPI(["AAPL", "MSFT", "AAPL"]).register(responses)
    df = _fmp().sp500_constituents()
    assert df["ticker"].tolist() == ["AAPL", "MSFT"]
    assert set(df["sector"]) == {"Information Technology"}


@responses.activate
def test_empty_constituents_raise() -> None:
    responses.add(responses.GET, f"{FMP_BASE}/sp500-constituent", json=[])
    with pytest.raises(DataSourceError, match="empty"):
        _fmp().sp500_constituents()


@responses.activate
def test_fmp_error_payload_raises() -> None:
    responses.add(
        responses.GET,
        f"{FMP_BASE}/sp500-constituent",
        json={"Error Message": "Exclusive Endpoint"},
    )
    with pytest.raises(DataSourceError, match="Exclusive Endpoint"):
        _fmp().sp500_constituents()


@responses.activate
def test_fmp_unexpected_payload_raises() -> None:
    responses.add(responses.GET, f"{FMP_BASE}/sp500-constituent", json={"foo": 1})
    with pytest.raises(DataSourceError, match="unexpected payload"):
        _fmp().sp500_constituents()


@responses.activate
def test_market_caps_requested_in_batches_of_100() -> None:
    FakeMarketAPI([]).register(responses)
    tickers = [f"T{i:03d}" for i in range(150)]
    fmp = _fmp()
    df = fmp.market_caps(tickers)
    assert len(df) == 150
    assert fmp.http.calls == 2
    assert df["market_cap"].gt(0).all()


@responses.activate
def test_historical_prices_adjusted_sorted_ascending() -> None:
    api = FakeMarketAPI([])
    api.register(responses)
    df = _fmp().historical_prices("BRK.B", date(2024, 1, 2), date(2024, 1, 5))
    assert df["date"].is_monotonic_increasing
    assert df.iloc[0]["close"] == 102.0  # Jan 2 = day 2 of the year
    assert api.price_requests == [("BRK-B", "2024-01-02", "2024-01-05")]
    assert "dividend-adjusted" in responses.calls[0].request.url


@responses.activate
def test_historical_prices_unadjusted_uses_full_endpoint() -> None:
    FakeMarketAPI([]).register(responses)
    df = _fmp().historical_prices("^GSPC", date(2024, 1, 2), date(2024, 1, 3), adjusted=False)
    assert len(df) == 2
    assert "historical-price-eod/full" in responses.calls[0].request.url


# --------------------------------------------------------------------------- FRED


@responses.activate
def test_fred_series_in_decimals_and_drops_missing() -> None:
    FakeMarketAPI([]).register(responses)
    fred = FREDClient(JsonHttpClient(FRED_BASE, SECRET, "api_key", "FRED"))
    df = fred.series("DTB3", date(2024, 1, 2), date(2024, 1, 5))
    assert len(df) == 3  # first observation was "."
    assert df["value"].eq(0.0525).all()


@responses.activate
def test_fred_bad_payload() -> None:
    responses.add(responses.GET, f"{FRED_BASE}/series/observations", json=[])
    fred = FREDClient(JsonHttpClient(FRED_BASE, SECRET, "api_key", "FRED"))
    with pytest.raises(DataSourceError, match="unexpected payload"):
        fred.series("DTB3", date(2024, 1, 2), date(2024, 1, 5))


def test_from_key_constructors() -> None:
    assert FMPClient.from_key("k", FMP_BASE).http.provider == "FMP"
    assert FREDClient.from_key("k", FRED_BASE).http.provider == "FRED"
