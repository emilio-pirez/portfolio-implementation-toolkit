"""Command-line interface (``pit``).

Commands: ``version``, ``show-config``, ``load-data``, ``db-schema``.
``run-day`` and ``backtest`` are added in Phase 5.
"""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path
from typing import Annotated

import typer

from pit import __version__
from pit.config import get_settings, load_config
from pit.data import DataSourceError, load_data
from pit.db import make_engine, render_schema_sql

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


if __name__ == "__main__":  # pragma: no cover
    app()
