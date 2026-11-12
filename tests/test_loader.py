from datetime import date
from pathlib import Path

import pandas as pd
import pytest
import responses
from sqlalchemy import Engine, func, select

from pit.config import AppConfig, Settings
from pit.data.fmp import FMPClient
from pit.data.fred import FREDClient
from pit.data.http import DataSourceError, JsonHttpClient
from pit.data.loader import (
    INDEX_TICKER,
    _missing_range,
    load_data,
    read_target_weights,
    read_universe,
)
from pit.db import read_audit_log, session_scope
from pit.db.models import BenchmarkWeight, Price, Rate, Security, TargetWeight
from pit.db.repository import data_sources
from tests.conftest import FMP_BASE, FRED_BASE, INPUTS, FakeMarketAPI

NO_KEYS = Settings(fmp_api_key=None, fred_api_key=None, _env_file=None)  # type: ignore[call-arg]


def _clients() -> tuple[FMPClient, FREDClient]:
    no_sleep = {"sleep": lambda _s: None, "max_retries": 1}
    return (
        FMPClient(JsonHttpClient(FMP_BASE, "k", "apikey", "FMP", **no_sleep)),
        FREDClient(JsonHttpClient(FRED_BASE, "k", "api_key", "FRED", **no_sleep)),
    )


def _model_tickers() -> list[str]:
    return pd.read_csv(INPUTS / "target_weights.csv")["ticker"].tolist()


def _count(engine: Engine, model) -> int:  # type: ignore[no-untyped-def]
    with session_scope(engine) as s:
        return s.scalar(select(func.count()).select_from(model)) or 0


# --------------------------------------------------------------------------- input files


def test_example_target_weights_are_valid() -> None:
    df = read_target_weights(INPUTS / "target_weights.csv")
    assert 40 <= len(df) <= 60
    assert df["target_weight"].sum() == pytest.approx(1.0)


@pytest.mark.parametrize(
    ("csv", "error"),
    [
        ("ticker,target_weight\nAAA,0.5\nBBB,0.4\n", "sum to"),
        ("ticker,target_weight\nAAA,0.5\naaa,0.5\n", "duplicated"),
        ("ticker,target_weight\nAAA,1.2\nBBB,-0.2\n", "negative"),
        ("ticker,weight\nAAA,1.0\n", "missing columns"),
    ],
)
def test_invalid_target_weights_rejected(tmp_path: Path, csv: str, error: str) -> None:
    path = tmp_path / "tw.csv"
    path.write_text(csv)
    with pytest.raises(ValueError, match=error):
        read_target_weights(path)


def test_universe_requires_columns(tmp_path: Path) -> None:
    path = tmp_path / "u.csv"
    path.write_text("ticker,name\nAAA,A\n")
    with pytest.raises(ValueError, match="sector"):
        read_universe(path)


def test_model_tickers_in_offline_universe() -> None:
    universe = read_universe(INPUTS / "offline_universe.csv")
    assert set(_model_tickers()) <= set(universe["ticker"])


@pytest.mark.parametrize(
    ("coverage", "expected"),
    [
        (None, (date(2024, 1, 1), date(2024, 3, 1))),
        ((date(2024, 2, 1), date(2024, 3, 1)), (date(2024, 1, 1), date(2024, 3, 1))),
        ((date(2023, 1, 1), date(2024, 3, 1)), None),
        ((date(2023, 1, 1), date(2024, 2, 1)), (date(2024, 2, 2), date(2024, 3, 1))),
    ],
)
def test_missing_range(coverage, expected) -> None:  # type: ignore[no-untyped-def]
    assert _missing_range(coverage, date(2024, 1, 1), date(2024, 3, 1)) == expected


# --------------------------------------------------------------------------- offline


def test_offline_load_fills_database(
    engine: Engine, cfg: AppConfig, short_period: tuple[date, date]
) -> None:
    summary = load_data(engine, cfg, NO_KEYS, *short_period, offline=True)
    n_days = len(pd.bdate_range(*short_period))
    assert summary.mode == "offline"
    assert _count(engine, Security) == 101  # 100 stocks + SPX
    assert _count(engine, Price) == 101 * n_days
    assert _count(engine, Rate) == n_days
    assert _count(engine, BenchmarkWeight) == 100
    assert _count(engine, TargetWeight) == 55
    with session_scope(engine) as s:
        assert data_sources(s) == {"SYNTHETIC"}
        total = s.scalar(select(func.sum(BenchmarkWeight.weight)))
        assert total == pytest.approx(1.0)
        log = read_audit_log(s)
    assert log.iloc[0]["event"] == "DATA_LOADED"
    assert log.iloc[0]["details"]["mode"] == "offline"


def test_offline_load_is_idempotent(
    engine: Engine, cfg: AppConfig, short_period: tuple[date, date]
) -> None:
    load_data(engine, cfg, NO_KEYS, *short_period, offline=True)
    before = _count(engine, Price)
    load_data(engine, cfg, NO_KEYS, *short_period, offline=True)
    assert _count(engine, Price) == before


def test_no_keys_falls_back_to_offline(
    engine: Engine, cfg: AppConfig, short_period: tuple[date, date]
) -> None:
    summary = load_data(engine, cfg, NO_KEYS, *short_period)
    assert summary.mode == "offline"
    assert "no FMP/FRED API keys" in summary.notes[0]


def test_start_must_precede_end(engine: Engine, cfg: AppConfig) -> None:
    with pytest.raises(ValueError, match="before end"):
        load_data(engine, cfg, NO_KEYS, date(2024, 2, 1), date(2024, 1, 1), offline=True)


def test_offline_rejects_model_ticker_outside_universe(
    engine: Engine, cfg: AppConfig, tmp_path: Path, short_period: tuple[date, date]
) -> None:
    bad = tmp_path / "tw.csv"
    bad.write_text("ticker,target_weight\nZZZZ,1.0\n")
    cfg = cfg.model_copy(
        update={"data": cfg.data.model_copy(update={"target_weights_file": str(bad)})}
    )
    with pytest.raises(ValueError, match="ZZZZ"):
        load_data(engine, cfg, NO_KEYS, *short_period, offline=True)


# --------------------------------------------------------------------------- online (mocked)


@responses.activate
def test_online_load_writes_real_sources(
    engine: Engine, cfg: AppConfig, short_period: tuple[date, date]
) -> None:
    api = FakeMarketAPI([*_model_tickers(), "XTRA"])
    api.register(responses)
    fmp, fred = _clients()
    summary = load_data(engine, cfg, NO_KEYS, *short_period, fmp=fmp, fred=fred)

    n_days = len(pd.bdate_range(*short_period))
    assert summary.mode == "online"
    assert _count(engine, Price) == (55 + 1) * n_days  # model tickers + index
    assert _count(engine, BenchmarkWeight) == 56
    assert _count(engine, Rate) == n_days - 1  # one "." observation dropped
    assert summary.api_calls == 1 + 1 + 56 + 1  # constituents, caps, prices, FRED
    with session_scope(engine) as s:
        assert data_sources(s) == {"FMP"}
        spx = s.get(Security, INDEX_TICKER)
        assert spx is not None and spx.asset_type == "INDEX"
        assert s.get(Security, "AAPL").sector == "Information Technology"  # type: ignore[union-attr]
    assert ("^GSPC", "2024-01-02", "2024-03-28") in api.price_requests


@responses.activate
def test_online_load_uses_cache_on_second_run(
    engine: Engine, cfg: AppConfig, short_period: tuple[date, date]
) -> None:
    FakeMarketAPI(_model_tickers()).register(responses)
    fmp, fred = _clients()
    load_data(engine, cfg, NO_KEYS, *short_period, fmp=fmp, fred=fred)

    api = FakeMarketAPI(_model_tickers())
    responses.reset()
    api.register(responses)
    fmp2, fred2 = _clients()
    later_end = date(2024, 4, 5)
    summary = load_data(engine, cfg, NO_KEYS, short_period[0], later_end, fmp=fmp2, fred=fred2)
    # every price request only covers the new days
    assert {(r[1], r[2]) for r in api.price_requests} == {("2024-03-29", "2024-04-05")}
    assert summary.prices == 56 * len(pd.bdate_range("2024-03-29", "2024-04-05"))


@responses.activate
def test_online_replaces_synthetic_data(
    engine: Engine, cfg: AppConfig, short_period: tuple[date, date]
) -> None:
    load_data(engine, cfg, NO_KEYS, *short_period, offline=True)
    FakeMarketAPI(_model_tickers()).register(responses)
    fmp, fred = _clients()
    summary = load_data(engine, cfg, NO_KEYS, *short_period, fmp=fmp, fred=fred)
    assert summary.purged_rows > 0
    with session_scope(engine) as s:
        assert data_sources(s) == {"FMP"}


@responses.activate
def test_api_failure_falls_back_to_offline_and_is_audited(
    engine: Engine, cfg: AppConfig, short_period: tuple[date, date]
) -> None:
    FakeMarketAPI(_model_tickers(), fail_on="prices").register(responses)
    fmp, fred = _clients()
    summary = load_data(engine, cfg, NO_KEYS, *short_period, fmp=fmp, fred=fred)
    assert summary.mode == "offline"
    assert "online load failed" in summary.notes[0]
    with session_scope(engine) as s:
        assert data_sources(s) == {"SYNTHETIC"}
        events = set(read_audit_log(s)["event"])
    assert {"DATA_SOURCE_ERROR", "DATA_LOADED"} <= events


@responses.activate
def test_api_failure_without_fallback_leaves_db_untouched(
    engine: Engine, cfg: AppConfig, short_period: tuple[date, date]
) -> None:
    FakeMarketAPI(_model_tickers(), fail_on="fred").register(responses)
    fmp, fred = _clients()
    with pytest.raises(DataSourceError):
        load_data(engine, cfg, NO_KEYS, *short_period, fmp=fmp, fred=fred, fallback=False)
    assert _count(engine, Price) == 0


def test_keys_build_real_clients(
    engine: Engine, cfg: AppConfig, short_period: tuple[date, date], monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    """With keys and no injected clients, real clients are built (HTTP mocked to fail)."""
    keys = Settings(fmp_api_key="a", fred_api_key="b", _env_file=None)  # type: ignore[call-arg]
    with responses.RequestsMock(assert_all_requests_are_fired=False) as rsps:
        FakeMarketAPI([], fail_on="constituents").register(rsps)
        monkeypatch.setattr("time.sleep", lambda _s: None)
        summary = load_data(engine, cfg, keys, *short_period)
    assert summary.mode == "offline"
