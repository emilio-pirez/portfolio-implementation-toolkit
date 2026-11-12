# Portfolio Implementation Toolkit

[![CI](https://github.com/emilio-pirez/portfolio-implementation-toolkit/actions/workflows/ci.yml/badge.svg)](https://github.com/emilio-pirez/portfolio-implementation-toolkit/actions/workflows/ci.yml)

A simplified simulation of a core portfolio manager's daily implementation workflow:
cash equitization with index futures, basket generation, pre-trade compliance and monitoring.

> **Status:** work in progress (Phase 3 of 7: portfolio and equitization). See the roadmap below.

## What this is (and is not)

This project models how a portfolio manager *implements* a portfolio: receiving
cash flows, keeping the fund fully invested with index futures, generating the
trade basket toward model weights, and checking every basket against risk and
compliance limits before it goes to the trading desk. All of it is logged for audit.

It is **not** an alpha model or a trading strategy. Target weights are an input
(the "PM's model"), read from `data/inputs/target_weights.csv`.

## Quickstart

```bash
git clone https://github.com/emilio-pirez/portfolio-implementation-toolkit.git
cd portfolio-implementation-toolkit
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
pit load-data --offline     # synthetic data, no API keys needed
pytest
```

Market data API keys are optional. To use live data, copy `.env.example` to
`.env`, add free keys from [Financial Modeling Prep](https://site.financialmodelingprep.com/developer/docs)
and [FRED](https://fred.stlouisfed.org/docs/api/api_key.html), and run
`pit load-data`. Without keys, or if a provider fails, the toolkit falls back to
offline mode on clearly labelled synthetic data.

## CLI

| Command | What it does |
|---|---|
| `pit load-data [--offline]` | Load prices, benchmark weights, T-Bill rate and model weights into SQLite |
| `pit show-config` | Validate `config.yaml` and print key parameters |
| `pit db-schema` | Print the database DDL (SQLite or Postgres) |

## Roadmap

| Phase | Scope | Status |
|---|---|---|
| 1 | Skeleton: packaging, config, lint, CI | done |
| 2 | Data layer: SQLite schema, FMP / FRED clients, offline synthetic data | done |
| 3 | Portfolio valuation, NAV, futures fair value, equitization | done |
| 4 | Trade basket generation and pre-trade compliance | planned |
| 5 | Risk metrics, daily workflow, backtest | planned |
| 6 | Streamlit dashboard | planned |
| 7 | Documentation and polish | planned |

## Example: equitizing a USD 25 m subscription

```python
from pit.equitization import size_futures

size_futures(25_000_000, futures_price=6_030)
# -> 82 ES (USD 24,723,000) + 9 MES (USD 271,350); USD 5,650 left un-equitized
```

See [docs/methodology.md](docs/methodology.md) for the formulas and
[docs/interview_notes.md](docs/interview_notes.md) for the reasoning behind them.

## Project layout

```
config.yaml          # every limit and assumption, in one reviewable file
src/pit/             # the package (db, data, portfolio, equitization, orders, compliance, risk, workflow)
app/dashboard.py     # Streamlit dashboard
docs/                # methodology and design notes
tests/               # pytest suite
```

## Author

Emilio Pirez
