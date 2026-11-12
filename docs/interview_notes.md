# Interview notes

Short answers to the questions this project is designed to support. Updated
at the end of each phase.

## Phase 1 - Skeleton

**Why keep every limit in `config.yaml` instead of in code?**
Limits are business decisions, not implementation details. Keeping them in one
validated, version-controlled file means a risk or compliance reviewer can see
exactly what is enforced, every change leaves a Git history entry, and a typo
fails loudly instead of silently disabling a check.

**Why HARD vs. SOFT severity?**
Not every breach should stop trading. A position above the issuer limit or a
name on the restricted list must block the basket; a tracking error slightly
above target should alert the PM, who can approve it with a documented reason.

## Phase 2 - Data

**Where does the data come from, and what would change in production?**
Prices and constituents from Financial Modeling Prep, the T-Bill rate from
FRED. In production these would come from licensed vendors (Bloomberg,
FactSet, the index provider for official benchmark weights) and from the
firm's security master, and the book of record would be reconciled daily
against the custodian.

**How do you avoid mixing bad or fake data with real data?**
Every row carries its source. Real and synthetic data are never stored
together, downloads finish before anything is written, and failures are logged
in the audit trail. The dashboard (Phase 6) will show a banner whenever data is synthetic.

**Why adjusted prices?**
Risk numbers are built from returns. Unadjusted prices turn every dividend and
split into a fake return, which would distort betas, volatilities and
tracking error.

**Why only download the model's stocks?**
Tracking error is computed against the index return directly, so the
covariance matrix only needs the names the fund can hold plus the index. That
cuts the download from 500 series to about 56.

## Phase 3 - Portfolio and equitization

**Why equitize with futures and not with an ETF like SPY?**
- *Cost:* no management fee (SPY ~9 bp a year) and a tighter effective
  spread on very large size; ES is one of the most liquid contracts in the world.
- *Cash:* a future needs only margin (a few % of notional), so most of the
  cash stays available for the stock basket or a redemption. An ETF uses the
  full amount of cash.
- *Speed and size:* billions trade in minutes, nearly 24 hours a day.
- *Clean unwind:* no ETF position to sell (and no capital gain on it) when the
  stocks are bought.
- *Trade-offs:* futures must be rolled every quarter (roll cost), carry basis
  risk versus the portfolio, and need margin management. Some mandates or
  smaller accounts cannot use derivatives, and then an ETF is the fallback.

**How do you calculate how many contracts to buy? What happens with the residual?**
Divide the cash to equitize (above the buffer, times the target beta) by the
contract notional F x multiplier. Floor the ES contracts so the big contract
never over-exposes the fund, then cover the rest with micro contracts
(MES = 1/10 of ES), rounded to the nearest one. The residual is at most half
a micro contract, about USD 15k, which is immaterial on a USD 500 m fund.
Worked example: USD 25 m at F = 6,030 gives 82 ES + 9 MES and leaves USD 5,650 uncovered.

**How do you handle a large redemption?**
Cash first: use the cash (keeping the minimum buffer on the smaller NAV) and
sell the futures that were equitizing it. If that is not enough, sell stocks
pro rata for the shortfall so the weights and tracking error do not change.
The sale is a basket that goes through pre-trade compliance. For a very large
redemption I would also consider an in-kind transfer, or using futures to
stay exposed while the stock sale is worked over several days to limit market impact.

**Why keep a cash buffer at all?**
Futures need margin, fees must be paid, and small redemptions should not force
trades. 0.5 % is a balance: big enough for daily margin swings on the futures,
small enough that the drag on returns is limited.

## Phase 4 - Orders and compliance

**What controls do you run before sending a basket, and which ones block it?**
I run them on the pro-forma portfolio, i.e. the fund as it will look after the
trades and the futures adjustment, not on today's book.
- *Blocking (HARD):* issuer above 8 %, sector above 30 %, cash outside
  0.25-3 %, total exposure outside 98-102 %, and any restricted name held or
  bought. These are regulatory, prospectus or firm-policy limits, and a
  breach must never reach the market.
- *Alerts (SOFT):* tracking error above 6 %, beta outside 0.85-1.15, sector
  active weight above 10 pts, fewer than 30 or more than 80 names, and an
  order above 10 % of ADV. These are risk-budget or liquidity signals where
  the PM can make an informed exception. The approval needs a written reason
  and is logged with who approved it.

**Why a no-trade band?**
Every trade costs a few basis points. A trade that moves a weight by 2 bp
costs about as much as the tracking error it removes, and it adds operational
load. The band keeps the basket focused on the trades that matter. Full exits
are always traded.

**What happens with a basket that breaks a limit?**
It is stored as REJECTED with the failing checks and their values. Nothing
goes to the desk. The PM adjusts (for example caps the name, or splits the trade
over several days for liquidity) and generates a new batch, so the history of
both attempts stays in the audit log.

**What would you do differently in production?**
Use the firm's OMS/EMS and its compliance engine (rules coded from the
prospectus and regulations, e.g. the 1940 Act diversification test). Add a
market-impact cost model. Add four-eyes approval for exceptions, and reconcile
fills against the custodian.

## Phase 5 - Risk and workflow

**What is cash drag and how much does it cost in a bull year?**
It is the return lost on cash that is not invested: idle cash % x (equity
return - T-Bill). With 3 % idle cash in a year where stocks return 25 % and
T-Bills 5 %, that is 0.03 x 20 % = about 60 bp. For an index-like core fund
whose tracking error budget is a few percent, that is a large share of the budget.
In a falling market the sign flips and cash helps. That is why it shows up as
tracking error, not just as lost return: the fund's beta drifts below 1.

**What did your backtest show?** (synthetic data)
With a one-day lag between a flow and the stock basket, equitization keeps
idle cash at the 0.5 % buffer (vs. up to 5.6 %) and exposure at 99.5 % every day.
Without it, the fund broke its 98 % exposure / 3 % cash limits on 26 days.
The return benefit with a 1-day lag is small (~1 bp/yr expected) and within the
noise. With a 3-day lag it grows to about +8 bp/yr and 95 avoided breach days.
The honest conclusion: for liquid large caps the main value is risk control
and staying within limits every day. The return benefit grows with how long
cash would otherwise sit.

**How do you measure tracking error ex-ante?**
TE = sqrt(a' S a) x sqrt(252). Here a holds the active weights, with
the index as one more asset: futures exposure - 100 %. S is a Ledoit-Wolf
shrunk covariance of 252 daily returns. A fund fully in futures has TE = 0, and
5 % idle cash adds about 5 % x index volatility. I run it on the pro-forma
portfolio before every basket.

**Why does the fill re-check cash?**
Because the book can change between generating a basket and executing it.
In the backtest, a redemption paid in between made a pending basket overdraw
the fund. The fix is what a desk does: re-check available cash at execution
and scale the buys.

**What would you do differently in production?**
- Use real futures prices and roll calendars instead of fair value, and
  model roll cost.
- Intraday execution with a market-impact model, instead of fills at the close.
- An OMS/EMS and the firm's compliance engine, with four-eyes approval.
- Daily reconciliation of positions and cash with the custodian and the
  futures clearing broker, including initial and variation margin.
- A commercial risk model (factor-based, e.g. Barra or Axioma) instead of a
  sample covariance.
- Settlement timing (T+1 for stocks) matched to the payment date of each flow.

## Phase 6 - Dashboard

**What would you look at first every morning?**
1. Total exposure and net cash against their limits: is the fund fully
   invested?
2. Alerts: post-trade breaches, baskets waiting for approval, futures that
   need to roll.
3. Today's flows and the basket they generate, with its pre-trade results.
4. The positions furthest from the model.

The Overview page is built in that order. Everything else is one click away,
and the audit log answers "what happened and who decided it" for any day.

**Why build the dashboard on top of the database instead of recomputing?**
The dashboard only reads what the workflow wrote. What the PM sees is exactly
what was decided and logged that day, which is the point of an audit trail.
