"""Command-line interface (``pit``).

Commands: ``version``, ``show-config``, ``load-data``, ``db-schema``,
``run-day``, ``approve``, ``reject``, ``export-basket``, ``backtest``, ``demo``.
"""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path
from typing import Annotated

import typer
from sqlalchemy import Engine

from pit import __version__
from pit.config import AppConfig, get_settings, load_config
from pit.data import DataSourceError, load_data
from pit.db import make_engine, render_schema_sql, session_scope

app = typer.Typer(
    name="pit",
    help="Portfolio Implementation Toolkit - daily PM implementation workflow.",
    no_args_is_help=True,
)


ConfigOption = Annotated[Path | None, typer.Option("--config", "-c", help="Path to config.yaml.")]


@app.command()
def version() -> None:
    """Print the toolkit version."""
    typer.echo(f"portfolio-implementation-toolkit {__version__}")


@app.command("show-config")
def show_config(config: ConfigOption = None) -> None:
    """Validate config.yaml and print a short summary of the key parameters."""
    cfg = load_config(config)
    settings = get_settings()
    mode = "online (API keys found)" if settings.has_market_data_keys else "offline (no API keys)"
    es = cfg.futures.contracts["ES"].multiplier
    mes = cfg.futures.contracts["MES"].multiplier
    typer.echo(f"Fund:            {cfg.fund.name}")
    typer.echo(f"Initial NAV:     {cfg.fund.base_currency} {cfg.fund.initial_nav:,.0f}")
    typer.echo(f"Benchmark:       {cfg.fund.benchmark}")
    q = cfg.futures.index_dividend_yield
    typer.echo(f"Futures:         ES x{es:g}, MES x{mes:g}, q = {q:.2%}")
    typer.echo(f"Cash buffer:     {cfg.equitization.cash_buffer_pct:.2%} of NAV")
    typer.echo(f"No-trade band:   {cfg.orders.no_trade_threshold_bps:g} bp")
    typer.echo(f"Est. cost:       {cfg.costs.total_bps:g} bp one-way")
    typer.echo(f"Data mode:       {mode}")


def _parse_date(value: str | None) -> date | None:
    return datetime.strptime(value, "%Y-%m-%d").date() if value else None


@app.command("load-data")
def load_data_cmd(
    offline: Annotated[
        bool, typer.Option("--offline", help="Use synthetic data (no API keys needed).")
    ] = False,
    start: Annotated[
        str | None, typer.Option(help="First date, YYYY-MM-DD (default: data.history_start).")
    ] = None,
    end: Annotated[str | None, typer.Option(help="Last date, YYYY-MM-DD (default: today).")] = None,
    fallback: Annotated[
        bool,
        typer.Option(
            "--fallback/--no-fallback", help="Fall back to offline data if an API call fails."
        ),
    ] = True,
    config: ConfigOption = None,
) -> None:
    """Load prices, benchmark weights, T-Bill rates and model weights into the database."""
    cfg = load_config(config)
    engine = make_engine(cfg.database_url)
    try:
        summary = load_data(
            engine,
            cfg,
            get_settings(),
            start=_parse_date(start),
            end=_parse_date(end),
            offline=offline,
            fallback=fallback,
        )
    except (DataSourceError, ValueError) as exc:
        typer.secho(f"load-data failed: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from exc
    finally:
        engine.dispose()

    label = "SYNTHETIC (offline)" if summary.mode == "offline" else "FMP + FRED (online)"
    typer.echo(f"Mode:              {label}")
    typer.echo(f"Period:            {summary.start} -> {summary.end}")
    typer.echo(f"Securities:        {summary.securities}")
    typer.echo(f"Price rows:        {summary.prices}")
    typer.echo(f"Benchmark weights: {summary.benchmark_weights}")
    typer.echo(f"T-Bill rows:       {summary.rates}")
    typer.echo(f"Target weights:    {summary.target_weights}")
    if summary.mode == "online":
        typer.echo(f"API calls:         {summary.api_calls}")
    for note in summary.notes:
        typer.secho(f"Note: {note}", fg=typer.colors.YELLOW)
    typer.echo(f"Database:          {cfg.database_url}")


@app.command("db-schema")
def db_schema(
    dialect: Annotated[str, typer.Option(help="sqlite or postgresql")] = "sqlite",
) -> None:
    """Print the DDL for every table."""
    typer.echo(render_schema_sql(dialect), nl=False)


def _engine(config: Path | None) -> tuple[AppConfig, Engine]:
    cfg = load_config(config)
    return cfg, make_engine(cfg.database_url)


def _pct(x: float) -> str:
    return f"{x:.2%}"


@app.command("run-day")
def run_day_cmd(
    day: Annotated[str, typer.Option("--date", help="Trading day to process, YYYY-MM-DD.")],
    config: ConfigOption = None,
) -> None:
    """Process one trading day: flows, equitization, basket, compliance, audit log."""
    from pit.workflow import WorkflowError, run_day

    cfg, engine = _engine(config)
    try:
        report = run_day(engine, cfg, _parse_date(day) or date.today())
    except (WorkflowError, LookupError) as exc:
        typer.secho(f"run-day failed: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from exc
    finally:
        engine.dispose()

    res = report.result
    s = res.summary
    if report.synthetic:
        typer.secho("SYNTHETIC DATA - offline mode, not real market prices", fg=typer.colors.YELLOW)
    typer.echo(f"Date:              {res.as_of}")
    typer.echo(f"NAV:               USD {s['nav']:,.0f}")
    typer.echo(f"Total exposure:    {_pct(s['exposure'])}")
    typer.echo(f"Cash / net cash:   {_pct(s['cash_pct'])} / {_pct(s['unequitized_pct'])}")
    typer.echo(f"Futures:           {s['es_contracts']} ES, {s['mes_contracts']} MES")
    if res.filled is not None:
        typer.echo(f"Filled basket:     {res.filled.batch_id}")
    if res.review is not None:
        b = res.review.basket
        typer.echo(
            f"New basket:        {b.batch_id} -> {res.review.status.value} "
            f"({len(b.orders)} orders, buys USD {b.buy_notional:,.0f}, "
            f"sells USD {b.sell_notional:,.0f})"
        )
        for c in res.review.hard_breaches + res.review.soft_breaches:
            typer.secho(f"  {c.severity.value} {c.name}: {c.message}", fg=typer.colors.RED)
    if report.basket_csv is not None:
        typer.echo(f"Desk file:         {report.basket_csv}")
    for c in res.post_trade:
        if not c.passed:
            typer.secho(f"Post-trade {c.severity.value}: {c.message}", fg=typer.colors.RED)
    typer.echo(f"Workflow actions:  {len(res.actions)} (see audit_log)")


@app.command()
def approve(
    batch_id: Annotated[str, typer.Option("--batch-id", help="Basket to approve.")],
    reason: Annotated[str, typer.Option("--reason", help="Why the SOFT alerts are acceptable.")],
    approver: Annotated[str, typer.Option(help="Who approves.")] = "pm",
    config: ConfigOption = None,
) -> None:
    """Approve a PROPOSED basket (SOFT alerts) so it is filled on its execution day."""
    from pit.compliance import approve_basket

    _, engine = _engine(config)
    try:
        with session_scope(engine) as s:
            approve_basket(s, batch_id, approver, reason)
    except (LookupError, ValueError) as exc:
        typer.secho(f"approve failed: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from exc
    finally:
        engine.dispose()
    typer.echo(f"Basket {batch_id} APPROVED by {approver}")


@app.command()
def reject(
    batch_id: Annotated[str, typer.Option("--batch-id", help="Basket to reject.")],
    reason: Annotated[str, typer.Option("--reason", help="Why it is rejected.")],
    approver: Annotated[str, typer.Option(help="Who rejects.")] = "pm",
    config: ConfigOption = None,
) -> None:
    """Reject a PROPOSED basket."""
    from pit.compliance import reject_basket

    _, engine = _engine(config)
    try:
        with session_scope(engine) as s:
            reject_basket(s, batch_id, approver, reason)
    except (LookupError, ValueError) as exc:
        typer.secho(f"reject failed: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from exc
    finally:
        engine.dispose()
    typer.echo(f"Basket {batch_id} REJECTED by {approver}")


@app.command("export-basket")
def export_basket(
    batch_id: Annotated[str, typer.Option("--batch-id", help="Basket to export.")],
    out: Annotated[Path, typer.Option("--out", help="CSV path.")],
    config: ConfigOption = None,
) -> None:
    """Write a stored basket in the trading-desk CSV format."""
    from pit.orders import export_basket_csv
    from pit.workflow.run import load_basket

    _, engine = _engine(config)
    try:
        with session_scope(engine) as s:
            basket = load_basket(s, batch_id)
    finally:
        engine.dispose()
    if basket is None:
        typer.secho(f"unknown batch {batch_id}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)
    typer.echo(f"Wrote {export_basket_csv(basket, out)}")


@app.command()
def backtest(
    start: Annotated[str, typer.Option(help="First day, YYYY-MM-DD.")] = "2025-01-02",
    end: Annotated[str, typer.Option(help="Last day, YYYY-MM-DD.")] = "2026-10-30",
    flows: Annotated[
        Path | None, typer.Option(help="Flows CSV (date,amount,type). Default: random, seeded.")
    ] = None,
    lag: Annotated[int | None, typer.Option(help="Override workflow.execution_lag_days.")] = None,
    config: ConfigOption = None,
) -> None:
    """Run the workflow over a period with and without equitization and compare."""
    import pandas as pd

    from pit.workflow import MarketData, random_flows, read_cash_flows, run_backtest, save_backtest

    cfg, engine = _engine(config)
    if lag is not None:
        cfg = cfg.model_copy(
            update={"workflow": cfg.workflow.model_copy(update={"execution_lag_days": lag})}
        )
    d0, d1 = _parse_date(start), _parse_date(end)
    assert d0 is not None and d1 is not None
    try:
        md = MarketData.load(engine, cfg)
        fl = (
            read_cash_flows(flows)
            if flows is not None
            else random_flows(md.trading_days(d0, d1), cfg.fund.initial_nav, cfg.backtest)
        )
        typer.echo(f"Running {len(md.trading_days(d0, d1))} days x 2 scenarios, {len(fl)} flows...")
        result = run_backtest(md, cfg, d0, d1, fl)
        n_rows, out_dir = save_backtest(engine, cfg, result)
    except (LookupError, ValueError) as exc:
        typer.secho(f"backtest failed: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from exc
    finally:
        engine.dispose()

    if result.synthetic:
        typer.secho("SYNTHETIC DATA - offline mode, not real market prices", fg=typer.colors.YELLOW)
    typer.echo(
        f"Backtest {result.run_id[:8]}: {result.start} -> {result.end}, "
        f"execution lag {cfg.workflow.execution_lag_days} day(s)"
    )
    with pd.option_context("display.width", 120, "display.max_columns", 5):
        typer.echo(result.summary.to_string(float_format=lambda x: f"{x:,.4f}"))
    typer.echo(f"Saved {n_rows} daily rows to backtest_daily and CSVs to {out_dir}")


@app.command()
def demo(
    skip_backtest: Annotated[
        bool, typer.Option("--skip-backtest", help="Do not run the (1 minute) backtest.")
    ] = False,
    snapshot_out: Annotated[
        Path | None,
        typer.Option(help="Build a fresh demo and save it here (e.g. data/demo/demo.db)."),
    ] = None,
    config: ConfigOption = None,
) -> None:
    """Build an offline demo database: synthetic data, ~6 weeks of run-day, backtest."""
    from pit.demo import DEMO_BACKTEST, build_demo, write_snapshot

    if snapshot_out is not None:
        report = write_snapshot(
            load_config(config), snapshot_out, lambda msg, _f: typer.echo(f"  {msg}")
        )
        typer.echo(f"Snapshot ready: {'; '.join(report.steps)}")
        return

    cfg, engine = _engine(config)
    try:
        report = build_demo(
            engine,
            cfg,
            progress=lambda msg, _f: typer.echo(f"  {msg}"),
            backtest=None if skip_backtest else DEMO_BACKTEST,
        )
    finally:
        engine.dispose()
    done = "; ".join(report.steps) or "nothing to do (data already present)"
    typer.echo(f"Demo ready: {done}. Open it with: streamlit run app/dashboard.py")


if __name__ == "__main__":  # pragma: no cover
    app()
