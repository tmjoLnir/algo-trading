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
- **`OrderRouter.flatten()` therefore builds a market order and submits it,
  rather than calling `BrokerPort.close_position()`.** That method reaches the
  venue without the chain, which is exactly the bypass this ADR refuses.
- **The carve-out, stated so it is not mistaken for one.** `close_position` and
  `close_all_positions` exist for the runbook's emergency flatten
  (`POST /api/v1/risk/flatten-all`), which is a human acting *around* a platform
  they have already halted. That is not a code path choosing to skip the limits;
  it is the case where our own view of the book is the thing you have lost
  confidence in, and so the thing you cannot build a correct `OrderRequest`
  from. It requires a typed confirmation and it is audit-logged. No automated
  path may call either method.
- Six of the nine default rules can refuse an exit. Four judge the order rather
  than whether it reduces a position — the kill switch, trading hours, the rate
  limit and stale data — and two more refuse whenever any holding is unmarked.
  Passing through the engine therefore means a flatten
  or a protective stop *can* be refused. The answer is not an exemption: it is
  that the refusal names the rule, loudly, so the human who pressed the button
  reads "refused by kill_switch" instead of believing a position closed.

## Alternatives
**Direct broker calls for "trusted" paths** — the trusted path is always the one
that later turns out to be the bug. Manual orders in particular are the most
common reason to need limits, not a reason to skip them.
