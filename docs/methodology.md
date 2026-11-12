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
| Idiosyncratic volatility | uniform 10-22 % per year (typical for S&P 500 mega caps) |
| Betas | uniform 0.75-1.25 |
| Market caps (last date) | Zipf-like by size rank, so the top name is ~7 % of the benchmark |
| Daily volume | 0.4 % of shares outstanding with noise |
| SPX level | cap-weighted return of the 100 members **minus q/252** (a price index) |

The synthetic SPX is built from its members and, like the real index, excludes
dividends. Stock prices are total-return series. This keeps everything
consistent: benchmark total return = SPX + q, and futures priced at
S x exp((r - q)T) replicate it. With these settings the 55-name model has an
ex-ante tracking error of ~4 % and a beta of ~1.02, typical of a core
large-cap portfolio.

The seed is fixed (`data.synthetic_seed`), and shocks are drawn one day at a
time, so extending the end date never changes history already generated.

## Portfolio valuation

| Quantity | Definition |
|---|---|
| Equity market value | sum of shares x close |
| Futures notional | contracts x futures price x multiplier |
| NAV | equity MV + cash + unsettled futures P&L |
| Total exposure | (equity MV + futures notional) / NAV |
| Cash % | physical cash / NAV (what the cash limits apply to) |
| Unequitized cash | cash - futures notional (cash truly out of the market) |

A futures position has no market value of its own: gains and losses are paid
every day as **variation margin**. The toolkit keeps the P&L since the last
settlement as "unsettled" and moves it into cash at the daily settlement, so
NAV is unchanged by the settlement itself.

**Inception:** the fund invests NAV x (1 - cash buffer) at the model weights.
Shares are rounded **down**, so the purchase never needs more cash than the
fund has; the rounding residue stays in cash.

## Index futures

**Contracts.** E-mini S&P 500 (ES, USD 50 x index) and Micro E-mini (MES,
USD 5 x index). They expire on the third Friday of March, June, September and
December. Codes follow CME notation: `ESZ6` = December 2026.

**Fair value (cost of carry).**

F = S x exp((r - q) x T)

- S: S&P 500 level (SPX close)
- r: 3-month T-Bill (FRED DTB3), used as a proxy for the financing rate
- q: index dividend yield, 1.3 % (config)
- T: years to expiry, ACT/365

Buying the future instead of the stocks means you keep the cash (and earn r)
but give up the dividends (q). With r > q the future trades above spot.

*Approximation:* production would use the traded contract price. The real
basis also reflects the market's financing spread, which makes rolls cheap or
rich relative to fair value.

**Roll.** A contract is flagged for roll when it is 5 business days or fewer
from expiry (`futures.roll_window_business_days`). Inside that window new
exposure goes straight into the next contract, and existing positions are
closed and reopened in it. The roll spread is not simulated.

## Equitization

**Goal:** keep the fund fully exposed to the market while cash is waiting to
be invested (subscriptions) or waiting to be paid out (redemptions).

**One rule for every case, the futures overlay.** On any day the fund
should hold futures against the cash it does not need to keep:

    cash to equitize = max(cash - buffer% x NAV, 0)
    target notional  = cash to equitize x beta_target
    N_ES  = floor(target notional / (F x 50))
    N_MES = round((target notional - N_ES x F x 50) / (F x 5))

- **ES is floored** so the big contract never adds exposure beyond the cash.
- **MES is rounded** to fill the gap. What is left is at most half a micro
  contract (about USD 15k when F = 6,000), i.e. 0.003 % of a USD 500 m fund.
  It can be slightly negative (a tiny over-exposure).
- **Cash buffer (0.5 % of NAV)** is never equitized. It pays futures margin,
  fees and small redemptions without forcing trades. The cost is exposure of
  about 99.5 % instead of 100 %.
- **beta_target** (default 1) lets the PM size the hedge to the portfolio's
  beta instead of the index's.

Worked example (no buffer): USD 25 m subscription, F = 6,030

| Step | Value |
|---|---|
| ES contract notional | 6,030 x 50 = 301,500 |
| N_ES | floor(25,000,000 / 301,500) = floor(82.92) = **82**, USD 24,723,000 |
| Residual | 277,000 |
| MES contract notional | 6,030 x 5 = 30,150 |
| N_MES | round(277,000 / 30,150) = round(9.19) = **9**, USD 271,350 |
| Cash left un-equitized | **USD 5,650** |

**Subscription:** cash rises, so the overlay buys futures the same day.

**De-equitization:** when the stock basket is bought, cash falls by the
amount invested, so the overlay sells about USD invested / (F x multiplier)
contracts. Futures are unwound in proportion to the dollars that went into
stocks, and total exposure stays the same.

**Redemption:**
1. Cash needed = redemption + buffer on the post-redemption NAV.
2. If cash covers it, pay from cash. The futures that were equitizing that
   cash are sold.
3. If not, sell stocks **pro rata** for the shortfall. Selling the same
   fraction of every position keeps the weights, and so the active risk,
   unchanged. Shares are rounded up so that enough cash is raised.
4. Resize the overlay.

In the simulation the stock sales execute at the close on the same day, with
no market impact. In production they would be a basket that goes through
pre-trade compliance (Phase 4), and settlement timing (T+1) would be matched
to the redemption's payment date.

## Basket construction

    equity budget   = NAV x (1 - cash buffer)
    target shares_i = round_to_lot(w_i x equity budget / price_i)
    order_i         = target shares_i - current shares_i      (> 0 BUY, < 0 SELL)

- NAV is measured **after** the day's flow, so the basket invests a
  subscription or funds a redemption.
- Names held but not in the model get a target of zero (full exit).
- **No-trade band (5 bp of NAV).** Smaller orders are dropped: their cost
  (about 3 bp) is close to the tracking error they would fix. Full exits are
  always kept so no stub positions are left behind. A side effect is useful:
  on a subscription, small names below the band are not bought, and that cash
  stays equitized with futures until the next rebalance.
- **Cash constraint.** Buys may use at most cash + sale proceeds - costs -
  minimum buffer. If they would use more, every buy is scaled down pro rata. The
  same check runs again at execution, because the book may have changed in
  between (e.g. a redemption). Without it the no-trade band is one-sided:
  small overweights are never sold while underweights are bought, so the fund
  slowly runs out of cash.
- **Estimated cost** per order = |notional| x (commission 1 bp + half spread
  2 bp). Linear, with no market impact. A production model would add impact
  that grows with order size / ADV (e.g. a square-root model).
- Every basket gets a `batch_id` (UUID). Its orders, compliance checks and
  audit events share that ID.
- **Desk file:** `ticker, side, quantity, est_price, est_notional`.

## Pre-trade compliance

Checks run on the **pro-forma** portfolio: the current book with the basket
executed at estimated prices and costs, and the futures overlay resized
(de-equitized). That is the portfolio the fund would actually own at the close.

| # | Check | Value | Limit | Severity |
|---|---|---|---|---|
| 1 | Max issuer weight | largest position / NAV | 8 % | HARD |
| 2 | Max sector weight | largest GICS sector / NAV | 30 % | HARD |
| 3 | Sector active weight | max abs(sector weight - benchmark sector weight) | 10 pts | SOFT |
| 4 | Cash range | **net** cash (cash - futures notional) / NAV | 0.25 % - 3 % | HARD |
| 5 | Total exposure | (equities + futures notional) / NAV | 98 % - 102 % | HARD |
| 6 | Tracking error | ex-ante, annualised (risk model, Phase 5) | 6 % | SOFT |
| 7 | Beta | ex-ante vs S&P 500 (risk model, Phase 5) | 0.85 - 1.15 | SOFT |
| 8 | Holdings count | names held | 30 - 80 | SOFT |
| 9 | Restricted list | restricted names held or **bought** | none | HARD |
| 10 | Order vs ADV | largest order / 20-day average volume | 10 % | SOFT |

Design choices:

- **Cash is measured net of futures.** On a subscription day physical
  cash can be 5 % of NAV while futures cover almost all of it. That cash is
  not out of the market, so it should not trip the 3 % maximum. Physical cash is
  never below net cash while the fund is long futures, so the 0.25 % minimum
  still protects margin and expenses.
- **Futures are not allocated to sectors.** Index futures add exposure to
  every sector in benchmark proportions, so they do not change active sector
  weights.
- **Selling a restricted name passes check 9.** Selling is how a breach is cured.
- **Missing volume data fails check 10.** A check that cannot be evaluated
  is not a pass.
- The tracking error and beta checks take a pluggable risk model. Without
  one they are recorded as passed with an empty value and the message "not
  computed (no risk model supplied)", so the gap is visible in the
  results. Phase 5 supplies the risk model.

**Decision:**

| Result | Basket status | What happens |
|---|---|---|
| Any HARD breach | `REJECTED` | Basket blocked; reasons in `compliance_checks` and `audit_log` |
| SOFT breaches only | `PROPOSED` | Needs manual approval **with a written reason** |
| All pass | `APPROVED` | Can go to the desk |

Allowed status transitions: `PROPOSED -> APPROVED | REJECTED`,
`APPROVED -> FILLED`. Anything else raises an error. Every transition writes an
audit event with the actor and the reason.

Audit trail of one basket: `BASKET_GENERATED`, then one `COMPLIANCE_BREACH`
per failed check, then `BASKET_APPROVED`, `BASKET_PENDING_APPROVAL` or
`BASKET_REJECTED`, and later a manual `BASKET_APPROVED` / `BASKET_REJECTED`
and `BASKET_FILLED`.

## Risk model

**Beta** of stock i: cov(r_i, r_SPX) / var(r_SPX), from 252 daily returns.
Portfolio beta = sum of w_i x beta_i + futures notional / NAV. Weights are
fractions of NAV, cash has beta 0 and index futures have beta 1.

**Ex-ante tracking error**, with the benchmark treated as one asset:

    a  = [w_1, ..., w_n, w_SPX],   w_SPX = futures notional / NAV - 1
    TE = sqrt(a' S a) x sqrt(252)

S is the daily covariance of the stocks plus SPX over 252 days. Checks:
a fund 100 % in futures has TE = 0; a fund with 5 % idle cash has
TE = 5 % x index volatility.

**Covariance:** Ledoit-Wolf shrinkage (`sklearn.covariance.ledoit_wolf`). With
~57 assets and 252 observations the sample covariance is noisy (1,653
parameters). Shrinking toward a scaled identity gives a stable, well
conditioned matrix. The choice is set by `risk.covariance_shrinkage`
(`ledoit_wolf` or `none`).

The model is re-estimated every day, and the pre-trade check uses it on the
**pro-forma** portfolio.

## Daily workflow (`pit run-day`)

At the close of day T:

1. **Mark to market.** Re-price stocks, re-mark futures at fair value,
   settle variation margin into cash, and accrue interest on cash at the
   T-Bill rate (ACT/365).
2. **Execute the pending basket** generated `workflow.execution_lag_days`
   ago (default 1). APPROVED: fill at today's close, capping sells at the
   shares held and scaling buys to the available cash, then de-equitize.
   PROPOSED: it was not approved in time, so it expires (REJECTED, with reason).
3. **Client flows.** Subscriptions are equitized at once; redemptions are
   paid from cash, then from pro-rata stock sales.
4. **Rolls.** Contracts within 5 business days of expiry are flagged
   (`ROLL_REQUIRED`) and moved to the next contract (`FUTURES_ROLLED`).
5. **New basket** toward the model, with the pre-trade review (10 checks,
   including TE and beta).
6. **Overlay resize, post-trade checks** on the actual book (stored as batch
   `POST-<date>`), positions saved, desk CSV exported, `DAY_COMPLETED`.

**Why a one-day execution lag?** The futures trade at the close of T (they
trade almost 24 hours), while the stock basket needs the order to be built,
checked and worked by the desk. It is filled at the next close. This is the
gap equitization exists to cover. With same-day execution the futures would be
bought and sold within the same day and equitization would show almost no effect.

**Governance:** each day is processed **once and in order**, so a re-run or an
out-of-order date is refused. Every step writes to `audit_log`.

**Futures replicate total return.** Cash earns r, and the future returns
about dS/S - (r - q) dt as F converges to S. Together they earn dS/S + q dt,
the index total return. That is why equitized cash behaves like invested cash.

## Cash drag

    drag_t = idle cash %_(t-1) x (r_benchmark,t - r_f,t)

- r_benchmark is the total return (SPX + q/252), and r_f is the T-Bill
  accrual for the day.
- Idle cash is cash net of futures.
- **Realized** drag (the sum of daily drags) is noisy, because it depends on
  what the market did on the specific days the cash was idle.
- **Expected** drag = average idle cash x (benchmark return - T-Bill) over
  the period. It shows the structural cost.

## Backtest (`pit backtest`)

Two scenarios run on the same market data, flows and basket logic. The only
difference is `equitization.enabled`. Flows are random but seeded (10 % of
days, 0.5-5 % of NAV, 60 % subscriptions), or read from a CSV. SOFT alerts are
auto-approved and counted. Daily results go to the `backtest_daily` table and
to CSVs in `data/outputs/`.

Results on **synthetic data**, 2-Jan-2025 to 30-Oct-2026 (477 days, 45 flows):

| Metric | Lag 1: with | Lag 1: without | Lag 3: with | Lag 3: without |
|---|---|---|---|---|
| Avg idle cash (% NAV) | 0.50 % | 0.87 % | 0.50 % | 1.35 % |
| Max idle cash (% NAV) | 0.50 % | 5.61 % | 0.50 % | 7.79 % |
| Avg total exposure | 99.50 % | 99.13 % | 99.50 % | 98.65 % |
| Cash drag, realized (bp/yr) | 2.1 | 2.2 | 2.1 | 12.1 |
| Cash drag, expected (bp/yr) | 1.6 | 2.8 | 1.6 | 4.3 |
| Tracking difference vs benchmark (bp/yr) | +37.1 | +39.2 | +33.4 | +25.0 |
| Days with a HARD post-trade breach | **0** | **26** | **0** | **95** |

How to read it:

- The **remaining drag with equitization is the 0.5 % buffer**, which is
  never equitized by design.
- With a 1-day lag the return benefit is small (~1 bp/yr expected), and in
  this sample it is lost in the noise. The value of equitization here is
  **control**: exposure is 99.5 % every day, and the fund never breaks its
  98 % exposure or 3 % cash limits. Without it, every large subscription
  breaks them.
- The longer cash takes to invest, the bigger the benefit. With a 3-day lag
  equitization adds about +8 bp/yr and removes 95 breach days. Real-world
  cases with longer lags include large flows relative to liquidity,
  transitions and holidays.
- The tracking difference versus the benchmark is mostly stock selection
  (55 names vs 100), which is the same in both scenarios. The difference
  between the scenarios is the equitization effect.
