# 28. An exposure bound is not a book

**Status:** Accepted · 2026-09-08

Completes what ADR 0027 recorded under *"what this does not fix"*. ADR 0020's
three properties all survive; none is superseded.

## Context

`project_pending` decides what to project by asking `reduces_position`, which is
quantity-blind by design. So a working `SELL 300` against a settled long of 100
is dropped **entirely** — and the committed book goes on saying long 100 when
that fill would leave a short of 200. A batch of flip orders is invisible to
`max_position_size` and `max_gross_exposure`, the two rules whose whole job is
capping what an order leaves behind.

ADR 0027 recorded this as a known hole rather than fixing it, because the
obvious fix — filter on `closes_without_reversing` so the reversal is projected
— is not obviously right: projecting that order also moves cash.

**It is worse than not obviously right. It is impossible.**

`Portfolio` carries one cash balance and one mark per symbol, so a projected
reversal moves quantity, market value, cash and equity together. Any book that
*shows* the reversal on a settled long of 100 at a mark of 100 carries −200,
whose market value is −20,000, so equity is pinned to `cash − 20,000`. That
leaves exactly one free variable, and the two guarantees pull it in opposite
directions:

```
keeping BuyingPowerRule no looser than today   requires   cash <= 50,000
keeping equity from inventing a drawdown       requires   cash >= 80,000
```

Swept through the real rules, every candidate fails:

```
cash 50,000  equity 30,000   buying power denied     daily loss DENIED -50.00%
cash 60,000  equity 40,000   buying power LOOSER     daily loss DENIED -33.33%
cash 76,400  equity 56,400   buying power LOOSER     daily loss DENIED  -6.00%
cash 80,000  equity 60,000   buying power LOOSER     daily loss approved
cash 86,000  equity 66,000   buying power LOOSER     daily loss approved
```

The gap is `|Δq| × mark` and no price, predicate or filter closes it. A
manufactured drawdown is not a cosmetic error either: `StrategyRunner._escalate`
turns a `daily_loss_limit` denial into a **global halt**.

Two earlier attempts each picked a row and were caught. Pricing the projection's
cash at the mark under-charges a buy limited above it; pricing at the limit
drifts equity by `Δq × (mark − limit)`, and a passive `SELL 300 @120` against a
mark of 100 takes equity from 60,000 to 66,000 — a phantom +10% that loosens the
daily loss limit and both ceiling denominators.

## Decision

**The projection stays exactly as ADR 0020 wrote it. The bound moves out of the
book.**

`project_pending` is unchanged, byte for byte. `RiskBooks` gains a third field:

```python
#: symbol -> (working BUY quantity, working SELL quantity)
in_flight: Mapping[str, tuple[Decimal, Decimal]] = field(default_factory=dict)
```

and `rules.worst_resulting_qty(order, books)` turns it into the largest position
an order can leave in its symbol, over every outcome the orders in flight can
produce. The two ceilings measure that instead of the committed quantity.

Three properties, each chosen against the alternative:

- **A bound, not a book.** The conservative direction differs per axis —
  quantity wants the largest magnitude, cash wants the smallest credit — and a
  `Portfolio` has one value for each. A bound has no cash, so it has no
  direction to get wrong. This is why the answer is not a better projection.
- **Every other rule is an *identical function*, not merely no-looser.** Cash,
  equity, `unmarked_symbols`, `gross_exposure` and `open_positions` on the
  committed book are bit-identical to before, so `kill_switch`, `trading_hours`,
  `rate_limit`, `stale_data`, `max_open_positions`, `daily_loss_limit` and
  `buying_power` cannot have moved at all. Only two rules changed, and both
  changed in one direction.
- **`MaxExposureRule` adds an increment rather than replacing its number**, so
  the non-negativity is visible in the code and not only here. It also preserves
  the price-mixing quirk already in `without` rather than quietly correcting it,
  because that correction would have been a loosening.

**Three candidate outcomes suffice.** The reachable quantities are the interval
`[held − sells, held + buys]`, `|x + q|` is convex, and a convex function's
maximum over an interval is at an endpoint. The third candidate is `held`
itself — the outcome where **nothing** fills: a cancel, a reject, an expiry, a
DAY limit dying at the close. That is the one outcome always available, and it
is the one a magnitude filter can otherwise leave uncovered.

**ADR 0020's asymmetry is unchanged**, restated as a property of the outcome
rather than of a predicate: an outcome showing less of the symbol than the
account holds is not considered, so a resting protective stop still cannot lower
a ceiling on the strength of an exit that has not happened.

## Consequences

**The invariant, verified by execution rather than argued.** Over 4,000
randomised books × 2 ceilings: **0 looser, 50 tighter, 7,950 unchanged**, with
513 working reversals where the bound exceeds what the committed book reports.
No rule approves what it refused before this change.

**The literal instruction was refused, and this is what replaced it.** The ask
was that `project_pending` project working reversals. It still does not. What
was delivered is the consequence that ask names — the ceilings stop being blind
to them — because the mechanism as stated cannot be built without weakening
`buying_power` or the daily loss limit, which `CLAUDE.md` §7 forbids shipping
quietly.

**A discipline hazard, stated plainly.** The committed book still shows long 100
while a `SELL 300` works, and that is now load-bearing rather than incidental. A
future rule that measures a magnitude must call `worst_resulting_qty`; one that
reads `books.committed.position(s).qty` directly gets the old answer and the
flip is invisible to it again. This is the same hazard ADR 0027 records one
layer down, and the same answer applies: the bound is one function, called from
both places that need it, so there is one place left to get it wrong.

**What is still not fixed.** The mark-versus-limit inconsistency in the
projection is real, reachable and untouched: `project_pending` moves cash at
`reference_price` (the limit price when there is one) while market value moves
at the mark, so ten flat symbols with ten working `LIMIT BUY 100 @105` take
committed equity to 95,000 and the daily loss limit denies "down −5.00%" off
orders that have not filled. It predates this change and this change does not
make it worse — but it is now the largest known defect in the projection, and it
wants its own ADR.

## Alternatives considered

**Filter `project_pending` on `closes_without_reversing`.** The obvious fix, and
the one the impossibility above rules out. It was implemented twice and caught
twice.

**Price the projection's cash consistently with market value.** Both readings
fail. At the mark, `BuyingPowerRule` is handed proceeds the account has not been
paid and buys limited above the mark are under-charged. At the limit, equity
drifts signed by `Δq × (mark − limit)`.

**Give each rule its own conservative book.** "Worst case" is not well defined
when one book feeds nine rules with opposite conservative directions, and it
multiplies the projection this ADR exists to keep singular.

**Split the reversal into a closing half and an opening half.** Coherent on
quantity and no better on cash: the closing half still credits proceeds, which
is the whole problem.
