# Portfolio Implementation Toolkit

[![CI](https://github.com/emilio-pirez/portfolio-implementation-toolkit/actions/workflows/ci.yml/badge.svg)](https://github.com/emilio-pirez/portfolio-implementation-toolkit/actions/workflows/ci.yml)

A simplified simulation of a core portfolio manager's daily implementation workflow:
cash equitization with index futures, basket generation, pre-trade compliance and monitoring.

> **Status:** work in progress (Phase 6 of 7: dashboard). See the roadmap below.

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
streamlit run app/dashboard.py       # first launch builds an offline demo (~1 min)
```

Or step by step from the command line:

```bash
pit load-data --offline              # synthetic data, no API keys needed
pit run-day --date 2026-03-16        # USD 25 m subscription: equitize, basket, compliance
pit run-day --date 2026-03-17        # basket filled, futures unwound
pit backtest                         # with vs without equitization, ~1 minute
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
| `pit run-day --date D` | Process one day: mark to market, fill yesterday's basket, flows, futures, new basket, compliance, audit log |
| `pit approve / reject --batch-id ID --reason ...` | Manual decision on a basket with SOFT alerts |
| `pit export-basket --batch-id ID --out file.csv` | Trading-desk file for a stored basket |
| `pit backtest [--start --end --flows --lag]` | Same workflow over a period, with vs without equitization |
| `pit demo [--skip-backtest]` | Build the offline demo database used by the dashboard |

## Roadmap

| Phase | Scope | Status |
|---|---|---|
| 1 | Skeleton: packaging, config, lint, CI | done |
| 2 | Data layer: SQLite schema, FMP / FRED clients, offline synthetic data | done |
| 3 | Portfolio valuation, NAV, futures fair value, equitization | done |
| 4 | Trade basket generation and pre-trade compliance | done |
| 5 | Risk metrics, daily workflow, backtest | done |
| 6 | Streamlit dashboard | done |
| 7 | Documentation and polish | planned |

## What it does

1. **Equitizes cash with index futures.** Subscriptions are covered the same
   day with E-mini / Micro E-mini S&P 500 futures (sized with floor ES +
   rounded MES). The futures are unwound as the stocks are bought.
2. **Builds the trade basket** toward the PM's model weights, with a no-trade
   band, lot rounding and estimated costs, and exports the trading-desk CSV.
3. **Runs 10 pre-trade compliance checks** on the pro-forma portfolio.
   HARD breaches block the basket; SOFT breaches need a documented manual approval.
4. **Measures risk ex-ante** (beta, tracking error with a Ledoit-Wolf
   covariance) and **runs the whole day** (`pit run-day`). Every step goes to
   an append-only audit log in SQLite.
5. **Backtests** the workflow with and without equitization to measure cash
   drag and limit breaches.

| Check | Limit | Severity |
|---|---|---|
| Max issuer weight | 8 % | HARD |
| Max sector weight | 30 % | HARD |
| Sector active weight vs benchmark | +/- 10 pts | SOFT |
| Cash, net of futures | 0.25 % - 3 % of NAV | HARD |
| Total exposure (stocks + futures) | 98 % - 102 % | HARD |
| Ex-ante tracking error | 6 % | SOFT |
| Ex-ante beta | 0.85 - 1.15 | SOFT |
| Number of holdings | 30 - 80 | SOFT |
| Restricted list | none held or bought | HARD |
| Order size vs ADV | 10 % | SOFT |

## Dashboard

`streamlit run app/dashboard.py` opens six pages. If the database is empty,
the first launch builds an offline demo by itself: synthetic data, six weeks of
`run-day` with the example flows, and the backtest. A banner is shown on every
page whenever the data is synthetic.

| Page | What it shows |
|---|---|
| Overview | NAV, total exposure vs. the 98-102 % limits, cash and net cash, futures, active alerts |
| Positions vs. Model | Weights vs. target vs. benchmark, by stock and by sector; largest deviations |
| Order Blotter | Every basket with its status, orders and costs; desk CSV download |
| Compliance | Pre-trade checks per basket, post-trade checks per day, breach history |
| Cash & Equitization | Backtest: idle cash, cumulative cash drag and exposure, with vs. without futures |
| Audit Log | Every event, filterable by type, date and text, with the full JSON details |

## Key results (synthetic data)

Backtest from 2-Jan-2025 to 30-Oct-2026 (477 trading days, 45 random client
flows of 0.5-5 % of NAV). The stock basket is filled one day after the flow
(lag 1) or three days after (lag 3).

| | Lag 1: with equitization | Lag 1: without | Lag 3: with | Lag 3: without |
|---|---|---|---|---|
| Max idle cash (% NAV) | 0.50 % | 5.61 % | 0.50 % | 7.79 % |
| Avg total exposure | 99.50 % | 99.13 % | 99.50 % | 98.65 % |
| Cash drag, realized (bp/yr) | 2.1 | 2.2 | 2.1 | 12.1 |
| Days breaking a HARD limit | **0** | **26** | **0** | **95** |

Equitization keeps the fund fully exposed and inside its exposure and cash
limits every day. The return benefit is small when cash is invested the next
day, and grows the longer cash would otherwise wait (about +8 bp/yr at lag 3).
Details: [docs/methodology.md](docs/methodology.md#backtest-pit-backtest).

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
app/dashboard.py     # Streamlit dashboard (6 pages)
docs/                # methodology and design notes
tests/               # pytest suite
```

## Author

Emilio Pirez
