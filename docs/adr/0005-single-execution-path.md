# 5. A single order submission path

**Status:** Accepted · 2026-08-14

## Context
Orders originate from several places: strategy signals, protective stops after a
fill, manual dashboard orders, and emergency exits. Each could plausibly call
the broker adapter directly.

## Decision
Every order goes through `OrderRouter.submit()`, which calls
`RiskEngine.validate()` before any broker call. No exceptions — including
manual orders and protective stops.

## Consequences
- Risk limits are a guarantee rather than a convention. There is one place to
  audit and one place to enforce.
- One audit trail; every order has a recorded decision.
- Slight awkwardness: emergency paths go through validation too. Handled by
  letting exits bypass entry-blocking rules while still passing through the
  engine — a rule that blocks an exit traps you in a losing position.
- A reviewer can reject "just this once" bypasses by pointing here.

## Alternatives
**Direct broker calls for "trusted" paths** — the trusted path is always the one
that later turns out to be the bug. Manual orders in particular are the most
common reason to need limits, not a reason to skip them.
