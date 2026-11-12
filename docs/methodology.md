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

## Sections to be added

- Futures fair value and equitization (Phase 3)
- Basket construction and transaction costs (Phase 4)
- Compliance checks (Phase 4)
- Beta, ex-ante tracking error and cash drag (Phase 5)
