"""Build a ready-to-explore demo database (offline, synthetic data).

Used by ``pit demo`` and by the dashboard on first launch, so that
``streamlit run app/dashboard.py`` works on a fresh clone with nothing configured:

0. if the database is empty and a prebuilt snapshot exists
   (``data.demo_snapshot_file``), copy it - this takes seconds, which matters
   for a cloud deployment that starts from scratch;
1. load offline synthetic market data (if the database has no prices);
2. process a stretch of trading days with ``run_day`` and the example flows
   in ``data/inputs/cash_flows.csv`` (if no positions exist yet);
3. run the with/without-equitization backtest (if none is stored).

Each step is skipped when its data already exists, so real data loaded with
``pit load-data`` is never overwritten.
"""

from __future__ import annotations

import shutil
import tempfile
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from sqlalchemy import Engine

from pit.config import AppConfig, Settings
from pit.dashboard import queries as q
from pit.data.loader import load_data
from pit.db import init_db, log_event, make_engine, session_scope
from pit.workflow import (
    MarketData,
    random_flows,
    read_cash_flows,
    run_backtest,
    run_day,
    save_backtest,
)

DEMO_DAYS = (date(2026, 3, 16), date(2026, 4, 30))
DEMO_BACKTEST = (date(2025, 1, 2), date(2026, 10, 30))

Progress = Callable[[str, float], None]


@dataclass
class DemoReport:
    steps: list[str] = field(default_factory=list)


def _noop(_msg: str, _frac: float) -> None:
    return None


def _sqlite_file(engine: Engine) -> Path | None:
    db = engine.url.database
    if engine.dialect.name != "sqlite" or not db or db == ":memory:":
        return None
    return Path(db)


def restore_snapshot(engine: Engine, cfg: AppConfig) -> bool:
    """Copy the prebuilt demo database over an *empty* SQLite database."""
    snapshot = cfg.resolve_path(cfg.data.demo_snapshot_file)
    target = _sqlite_file(engine)
    if target is None or not snapshot.exists():
        return False
    engine.dispose()  # close pooled connections before replacing the file
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(snapshot, target)
    return True


def write_snapshot(
    cfg: AppConfig,
    out: Path,
    progress: Progress = _noop,
    days: tuple[date, date] = DEMO_DAYS,
    backtest: tuple[date, date] | None = DEMO_BACKTEST,
) -> DemoReport:
    """Build a fresh demo in a temporary database and save it (compacted) to ``out``."""
    with tempfile.TemporaryDirectory() as tmp:
        engine = make_engine(f"sqlite:///{Path(tmp) / 'demo.db'}")
        report = build_demo(engine, cfg, progress, days, backtest, use_snapshot=False)
        with engine.connect() as conn:
            conn.exec_driver_sql("VACUUM")
        engine.dispose()
        out.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(Path(tmp) / "demo.db", out)
    report.steps.append(f"snapshot written to {out}")
    return report


def build_demo(
    engine: Engine,
    cfg: AppConfig,
    progress: Progress = _noop,
    days: tuple[date, date] = DEMO_DAYS,
    backtest: tuple[date, date] | None = DEMO_BACKTEST,
    use_snapshot: bool = True,
) -> DemoReport:
    report = DemoReport()
    init_db(engine)

    empty = not q.data_sources(engine) and not q.has_positions(engine)
    if empty and use_snapshot and restore_snapshot(engine, cfg):
        progress("Restored the prebuilt demo database", 0.9)
        with session_scope(engine) as s:
            log_event(s, "DEMO_RESTORED", {"snapshot": cfg.data.demo_snapshot_file})
        report.steps.append("restored demo snapshot")

    if not q.data_sources(engine):
        progress("Loading offline synthetic market data", 0.05)
        no_keys = Settings(fmp_api_key=None, fred_api_key=None, _env_file=None)
        load_data(engine, cfg, no_keys, offline=True)
        report.steps.append("loaded offline data")

    md = MarketData.load(engine, cfg)
    if not q.has_positions(engine):
        flows = read_cash_flows(cfg.resolve_path(cfg.data.cash_flows_file))
        trading = md.trading_days(*days)
        for i, d in enumerate(trading):
            progress(f"Processing {d} ({i + 1}/{len(trading)})", 0.1 + 0.4 * i / len(trading))
            run_day(engine, cfg, d, md=md, flows=flows)
        report.steps.append(f"processed {len(trading)} days")

    if backtest is not None and not q.has_backtest(engine):
        progress("Running the backtest (with vs without equitization)", 0.55)
        bt_days = md.trading_days(*backtest)
        flows = random_flows(bt_days, cfg.fund.initial_nav, cfg.backtest)
        result = run_backtest(md, cfg, backtest[0], backtest[1], flows)
        save_backtest(engine, cfg, result)
        report.steps.append(f"backtest over {len(bt_days)} days")

    progress("Done", 1.0)
    return report
