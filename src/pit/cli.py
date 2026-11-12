"""Command-line interface (``pit``).

Only ``version`` and ``show-config`` exist in Phase 1; ``load-data``,
``run-day`` and ``backtest`` are added in later phases.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

from pit import __version__
from pit.config import get_settings, load_config

app = typer.Typer(
    name="pit",
    help="Portfolio Implementation Toolkit - daily PM implementation workflow.",
    no_args_is_help=True,
)


@app.command()
def version() -> None:
    """Print the toolkit version."""
    typer.echo(f"portfolio-implementation-toolkit {__version__}")


@app.command("show-config")
def show_config(
    config: Annotated[
        Path | None, typer.Option("--config", "-c", help="Path to config.yaml.")
    ] = None,
) -> None:
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


if __name__ == "__main__":  # pragma: no cover
    app()
