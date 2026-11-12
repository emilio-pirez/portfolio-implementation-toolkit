# Methodology

This document explains the financial logic behind each module: formulas,
assumptions and simplifications. It is filled in phase by phase.

## Configuration and governance

All limits and parameters live in `config.yaml` and are validated on load
(`src/pit/config.py`). Unknown keys, inverted ranges (min > max) or invalid
severities make the program fail immediately instead of running with a wrong limit.

Each compliance rule carries a severity:

- **HARD**: a breach blocks the basket (status `REJECTED`).
- **SOFT**: a breach raises an alert; the basket stays `PROPOSED` and needs manual approval.

### Key parameters (defaults)

| Parameter | Value | Why |
|---|---|---|
| Initial NAV | USD 500 m | Realistic size for a single U.S. large-cap sleeve |
| ES / MES multiplier | 50 / 5 USD per index point | CME contract specifications |
| Index dividend yield (q) | 1.3 % | Approximate S&P 500 trailing yield, used in futures fair value |
| Cash buffer | 0.5 % of NAV | Left un-equitized to cover futures margin and expenses |
| No-trade band | 5 bp | Avoids micro-trades whose cost exceeds their benefit |
| Estimated cost | 1 bp commission + 2 bp half-spread | Simple linear cost model for liquid U.S. large caps |

## Data layer

### Sources

| Data | Online source | Offline source |
|---|---|---|
| S&P 500 members and GICS sectors | FMP `sp500-constituent` | `data/inputs/offline_universe.csv` (100 large S&P 500 names) |
| Benchmark weights | FMP `market-capitalization-batch`: w_i = cap_i / sum(cap) | synthetic market caps |
| Daily stock prices and volume | FMP `historical-price-eod/dividend-adjusted` | synthetic GBM |
| S&P 500 index level (stored as `SPX`) | FMP `historical-price-eod/full`, symbol `^GSPC` | synthetic market factor |
| Risk-free rate | FRED `DTB3` (3M T-Bill), stored as a decimal | flat 4.0 % |

Prices are **dividend- and split-adjusted**, because returns, betas and
covariances must reflect total return; an unadjusted series would show a fake
loss on every ex-dividend date and a crash on every split.

### Which prices are downloaded and why

Only the model portfolio's tickers plus the index are downloaded (~56 series),
not all 500 constituents. Ex-ante tracking error will be computed against the
index itself, treating the benchmark as one asset:

TE^2 = w' S w - 2 w' s_b + s_b^2

where S is the covariance of the stocks held, s_b the vector of their
covariances with the index and s_b^2 the index variance. This is exact for "fund vs.
index" and needs no covariance for the 450 names we never hold. Sector
weights of the benchmark still use all constituents (market caps only, one
batched call per 100 names). An online load is about 60 API calls, inside the
FMP free-tier daily limit.

### Caching

The SQLite database is the cache. For each ticker the loader looks up the
last cached date and requests only the days after it, so a daily refresh costs
one call per ticker for one day of data. Upserts (`INSERT ... ON CONFLICT DO
UPDATE`) make re-loading the same range safe.

### Data integrity rules

- Every market-data row has a `source` column: `FMP`, `FRED` or `SYNTHETIC`.
- Real and synthetic data never coexist: each load first deletes the other
  mode's market data. The cache lookup only counts rows from real sources.
- Online data is fully downloaded **before** anything is written; a failed
  call leaves the database untouched.
- If keys are missing or a provider fails, the loader falls back to offline
  data (disable with `--no-fallback`); the failure is recorded in `audit_log`
  as `DATA_SOURCE_ERROR`.
- Model weights are validated on load: unique tickers, no negative weights
  (long-only fund) and a sum of 1.0.
- CHECK constraints in the schema reject impossible values (non-positive
  prices, unknown order sides or statuses, negative flow amounts).

### Simplifications

- **Benchmark weights are a single snapshot** (today's market caps). Historical
  index weights are a paid dataset; for a large-cap index the weights drift
  slowly, so a snapshot is a reasonable approximation for a backtest.
- **DTB3 is a discount rate**, not a bond-equivalent yield. At a 4-5 % rate
  level the difference is a few basis points and it is ignored.
- **Calendar:** offline data uses Monday-Friday business days and ignores exchange holidays.

### Synthetic market (offline mode)

Daily log returns follow a one-factor model with sector factors, i.e. a
correlated geometric Brownian motion:

r_i,t = beta_i r_m,t + r_sector(i),t + e_i,t - 1/2 var_i dt

| Parameter | Value |
|---|---|
| Market drift / volatility | 8 % / 16 % per year |
| Sector factor volatility | 8 % per year |
| Idiosyncratic volatility | uniform 15-30 % per year |
| Betas | uniform 0.75-1.25 |
| Market caps (last date) | Zipf-like by size rank, so the top name is ~7 % of the benchmark |
| Daily volume | 0.4 % of shares outstanding with noise |

The seed is fixed (`data.synthetic_seed`), and shocks are drawn one day at a
time, so extending the end date never changes history already generated.

## Sections to be added

- Futures fair value and equitization (Phase 3)
- Basket construction and transaction costs (Phase 4)
- Compliance checks (Phase 4)
- Beta, ex-ante tracking error and cash drag (Phase 5)
