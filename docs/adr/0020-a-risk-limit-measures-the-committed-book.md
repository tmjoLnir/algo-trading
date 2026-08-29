# 20. A risk limit measures the committed book, not the settled one

**Status:** Accepted · 2026-08-29

## Context

`RiskEngine.validate(order, portfolio)` runs a chain of rules against
`Portfolio`, and `Portfolio` moves when a **fill** lands: `cash` is debited in
`BacktestEngine._execute`, `positions` are advanced by `Position.apply_fill`.
Nothing about an order that has been approved and sent — but not yet filled —
appears anywhere in it.

Orders are approved one at a time. So a caller that submits several before any
of them settles has each one judged against a book containing none of the
others.

That is not a corner case; it is what a multi-symbol universe does on every
bar. `buy_and_hold` emits an entry for all forty of its symbols on the first
bar. `StrategyRunner._submit` loops signals through the router against one
`portfolio` object that only moves when `_drain_fills` runs. Both submit a batch
against a book frozen at the start of it.

The measurable cost: a 40-symbol `buy_and_hold` replay at `equity_pct 0.05`
filled **all forty** orders, ended at **1.97x gross exposure** with cash at
**−97,046** against a `max_gross_exposure_pct` of 1.00, and the chain refused
nothing. Each order was 5% of equity against a 100% ceiling and passed honestly;
together they were 200%. The engine also charges no financing on negative cash,
so the run was levered two-to-one for free and reported 542% where the
unlevered version reports 269%.

**Four of the nine rules have this hole, not two.** It is not only the rules
that price the book:

| Rule | What it reads | How a batch defeats it |
|---|---|---|
| `max_gross_exposure` | `portfolio.gross_exposure` | forty at 5% each |
| `buying_power` | `portfolio.cash` | every order priced against the opening balance |
| `max_open_positions` | `portfolio.open_positions` | a batch submitted at nineteen open |
| `max_position_pct` | one symbol's `qty` | two orders in one name at 6% of a 10% cap |

`CLAUDE.md` §1.5 makes the chain the one thing between a strategy and the
account. The chain ran on every order. It measured the wrong book.

## Decision

**`RiskEngine.validate` takes the orders the caller has committed and not yet
seen settle, and projects the book forward before the chain reads it.**

```python
def validate(self, order, portfolio, pending: Iterable[Order] = ()) -> RiskDecision:
    book = project_pending(portfolio, pending)
    for rule in self.rules:
        decision = rule.check(order, book, self.limits)
```

`rules.project_pending` returns a `Portfolio` with cash and positions advanced
as if everything in flight had filled at its reference price — the same
`reference_price` the rules and the position sizer already use, so a projection
and a check cannot disagree about what an order is worth.

Three properties, each chosen against a plausible alternative:

- **One projection, not nine rule changes.** Every rule keeps
  `check(order, portfolio, limits)` and none of them knows in-flight orders
  exist. A rule added later inherits the fix.
- **Reductions are never credited.** A resting protective stop, counted as
  filled, would *lower* projected exposure and license a position the limits
  would otherwise refuse — a rule reasoning from an exit that has not happened.
  `reduces_position` already draws that line for `DailyLossLimitRule`; it draws
  it here. The asymmetry is the default-closed posture this module is built on.
- **An unpriceable in-flight order still consumes its quantity.** With no limit
  price and no mark there is no notional to add, so the projected position
  carries the quantity and no price, lands in `Portfolio.unmarked_symbols`, and
  `_unpriced_book` refuses on behalf of every rule that prices the book.
  Skipping it would under-count, and under-counting approves what it should
  refuse.

The two callers both pass it: `BacktestEngine` its resting orders,
`StrategyRunner` its `_open_orders` — what it believes is working at the venue,
restored from the database at warmup and cleared on a terminal state, so an
order still working from an earlier bar counts too.

## Consequences

The 40-symbol replay now fills twenty and refuses twenty at
`max_gross_exposure`, landing at 1.00x. A batch that fits — forty at 2.5%, which
is exactly 100% — is untouched.

**`pending` defaults to empty, and that is a real edge.** A caller that has
orders outstanding and omits it gets the old behaviour, silently. The default
exists so that the many call sites with nothing in flight (tests, one-off
submissions, `flatten`) read cleanly, and the two that matter are covered by
tests asserting the runner passes `open_orders` and the engine passes
`_pending`. A stricter design would make the argument required; that would
change every test double in the repository to buy a guarantee those two tests
already give.

**A limit is still not an invariant of the settled book.** The chain prices an
in-flight order at the last mark and the fill then crosses the spread, so a book
can settle a hair above a ceiling it was approved under — tens of dollars on a
100,000 account in the replay above. That residual is identical for a single
order, predates this change, and is inherent to any pre-trade check. What is
gone is the 97,046 version of it.

The projection is a read-only view for the chain. `avg_entry_price` and the P&L
fields are left untouched, because no rule reads them and inventing a cost basis
for a fill that has not happened would be a worse answer than an untouched one.
A future rule that reads them must project them first.

## Alternatives considered

**A reservation ledger inside `RiskEngine`.** The engine tracks what it has
approved and releases on fill or cancellation. Rejected: it makes the engine
stateful and couples it to order lifecycle events it has no other reason to
know about. Worse, a release that never arrives — a dropped fill event, a
crash between submit and reconcile — leaks reservations that progressively
refuse everything. Fail-closed, but failing closed by slow strangulation is not
a good failure. The caller already knows what is in flight; asking it is
cheaper and cannot drift.

**Passing `pending` to every rule.** Change `RiskRule.check` to take a fourth
argument. Rejected: nine rules would each re-derive "what does in-flight mean
for my limit", four of them would need it, and the fifth kind of drift this
codebase keeps finding is exactly that — the same question answered slightly
differently in several places.

**Refreshing the portfolio from the broker between orders.** Correct in
principle and unusable in practice: an API round trip per order inside a
forty-signal loop, against a rate limit, to learn about fills that have not
happened yet.

**Sizing the batch as a batch, upstream of risk.** Have the strategy runner
allocate across its signals before submitting any. That is a real improvement
and orthogonal to this one — it would make the *sizing* coherent, while this
makes the *limit* coherent. A limit that only holds when sizing is well behaved
is not a limit.
