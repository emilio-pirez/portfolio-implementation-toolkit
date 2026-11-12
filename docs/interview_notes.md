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
