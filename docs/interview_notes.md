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
