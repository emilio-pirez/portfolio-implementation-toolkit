from datetime import date
from pathlib import Path

import pytest
from sqlalchemy import Engine, inspect, select
from sqlalchemy.exc import IntegrityError

from pit.config import PROJECT_ROOT
from pit.db import log_event, make_engine, read_audit_log, render_schema_sql, session_scope
from pit.db.models import DataSource, Order, Price, Rate, Security
from pit.db.repository import (
    data_sources,
    load_prices,
    price_coverage,
    purge_market_data_not_from,
    rate_coverage,
    upsert,
)

EXPECTED_TABLES = {
    "securities",
    "prices",
    "rates",
    "benchmark_weights",
    "target_weights",
    "positions",
    "cash_flows",
    "orders",
    "compliance_checks",
    "audit_log",
}


def _price(ticker: str, d: date, close: float, source: str = "SYNTHETIC") -> dict:
    return {"ticker": ticker, "date": d, "close": close, "volume": 1.0, "source": source}


def test_committed_schema_matches_models() -> None:
    committed = (PROJECT_ROOT / "src" / "pit" / "db" / "schema.sql").read_text(encoding="utf-8")
    assert committed == render_schema_sql(), "run `pit db-schema > src/pit/db/schema.sql`"


def test_postgres_ddl_renders() -> None:
    ddl = render_schema_sql("postgresql")
    assert "CREATE TABLE prices" in ddl


def test_init_db_creates_all_tables(engine: Engine) -> None:
    assert set(inspect(engine).get_table_names()) >= EXPECTED_TABLES


def test_make_engine_creates_parent_folder(tmp_path: Path) -> None:
    db = tmp_path / "nested" / "dir" / "x.db"
    make_engine(f"sqlite:///{db}")
    assert db.parent.is_dir()


def test_upsert_inserts_then_updates(engine: Engine) -> None:
    with session_scope(engine) as s:
        assert upsert(s, Price, [_price("AAA", date(2024, 1, 2), 10.0)]) == 1
        upsert(s, Price, [_price("AAA", date(2024, 1, 2), 11.0)])
    with session_scope(engine) as s:
        rows = s.scalars(select(Price)).all()
        assert len(rows) == 1
        assert rows[0].close == 11.0


def test_upsert_empty_is_noop(engine: Engine) -> None:
    with session_scope(engine) as s:
        assert upsert(s, Price, []) == 0


def test_check_constraint_rejects_non_positive_price(engine: Engine) -> None:
    with pytest.raises(IntegrityError), session_scope(engine) as s:
        upsert(s, Price, [_price("AAA", date(2024, 1, 2), -1.0)])


def test_check_constraint_rejects_unknown_order_side(engine: Engine) -> None:
    order = Order(
        batch_id="b1",
        date=date(2024, 1, 2),
        ticker="AAA",
        side="HOLD",
        quantity=1,
        est_price=1,
        est_notional=1,
        est_cost=0,
        status="PROPOSED",
    )
    with pytest.raises(IntegrityError), session_scope(engine) as s:
        s.add(order)


def test_session_scope_rolls_back_on_error(engine: Engine) -> None:
    with pytest.raises(RuntimeError), session_scope(engine) as s:
        upsert(s, Price, [_price("AAA", date(2024, 1, 2), 10.0)])
        raise RuntimeError("boom")
    with session_scope(engine) as s:
        assert s.scalars(select(Price)).first() is None


def test_purge_keeps_only_requested_sources(engine: Engine) -> None:
    with session_scope(engine) as s:
        upsert(s, Price, [_price("AAA", date(2024, 1, 2), 10.0, "SYNTHETIC")])
        upsert(s, Price, [_price("BBB", date(2024, 1, 2), 10.0, "FMP")])
        upsert(
            s,
            Security,
            [
                {
                    "ticker": "AAA",
                    "name": "A",
                    "sector": "X",
                    "asset_type": "EQUITY",
                    "source": "SYNTHETIC",
                }
            ],
        )
        deleted = purge_market_data_not_from(s, keep=[DataSource.FMP, DataSource.FRED])
        assert deleted == 2
        assert data_sources(s) == {"FMP"}


def test_coverage_and_wide_prices(engine: Engine) -> None:
    with session_scope(engine) as s:
        assert price_coverage(s, "AAA") is None
        upsert(
            s,
            Price,
            [
                _price("AAA", date(2024, 1, 2), 10.0),
                _price("AAA", date(2024, 1, 3), 11.0),
                _price("BBB", date(2024, 1, 3), 20.0),
            ],
        )
        upsert(
            s, Rate, [{"series": "DTB3", "date": date(2024, 1, 2), "value": 0.05, "source": "FRED"}]
        )
        assert price_coverage(s, "AAA") == (date(2024, 1, 2), date(2024, 1, 3))
        assert rate_coverage(s, "DTB3") == (date(2024, 1, 2), date(2024, 1, 2))
        assert rate_coverage(s, "NOPE") is None
        wide = load_prices(s, ["AAA", "BBB"], start=date(2024, 1, 3), end=date(2024, 1, 3))
        assert list(wide.columns) == ["AAA", "BBB"]
        assert wide.iloc[0].tolist() == [11.0, 20.0]
        assert load_prices(s, ["ZZZ"]).empty


def test_audit_log_roundtrip(engine: Engine) -> None:
    with session_scope(engine) as s:
        log_event(s, "TEST_EVENT", {"when": date(2024, 1, 2), "n": 3})
        log_event(s, "OTHER")
    with session_scope(engine) as s:
        log = read_audit_log(s)
        assert list(log["event"]) == ["OTHER", "TEST_EVENT"]  # newest first
        only = read_audit_log(s, event="TEST_EVENT")
        assert only.iloc[0]["details"] == {"n": 3, "when": "2024-01-02"}


def test_price_coverage_can_filter_by_source(engine: Engine) -> None:
    with session_scope(engine) as s:
        upsert(s, Price, [_price("AAA", date(2024, 1, 2), 10.0, "SYNTHETIC")])
        assert price_coverage(s, "AAA") is not None
        assert price_coverage(s, "AAA", sources=[DataSource.FMP]) is None
