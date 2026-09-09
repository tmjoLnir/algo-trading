# 31. A fee is settled when the cash says so, not when a ledger says so

**Status:** Accepted · 2026-09-09

Amends ADR 0030. Everything 0030 decided about *reading* fees stands — the port,
the sign convention, the narrow activity-type list, settling inside `reconcile`.
What changes is where the claim "this charge is in the book" is kept, and how it
is arrived at.

## Context

ADR 0030 made `broker_fees` the record of what had been **applied**, keyed on
the venue's activity id, and argued:

> `record_unseen` inserts and returns in one `ON CONFLICT DO NOTHING …
> RETURNING`, so applying a charge twice is impossible rather than merely
> unlikely.

That was true, and it defended the wrong side. Applying a charge twice was never
the likely failure. Applying it **zero** times while the ledger insisted
otherwise was, because the two halves of "apply" were two writes with a gap
between them:

```
record_unseen(...)            # committed, durably, immediately
portfolio.cash -= total       # in memory
...                           # cash becomes durable at the next snapshot
```

On 2026-09-09 a worker did the first, did the second, and ended before the
third. `broker_fees` held all three of the day's charges stamped
`applied_at = 10:25:29`; no durable balance had ever reflected them. Every
restart afterwards read the un-corrected book, was told by the ledger that
everything was applied, found nothing to do, and halted on the $4.14 those
charges explained — for eleven minutes, in complete silence, because the
already-applied branch logged at DEBUG.

**The loss was permanent.** Nothing in the schema could distinguish "applied"
from "recorded and then lost", so no later run could repair it. Recovery
required an operator deleting rows by hand.

The deeper fault is that the ledger made a claim about a balance it did not
own. A claim about a number, one transaction away from that number, is a claim
that can be wrong — and when it is, it is wrong silently and forever.

## Decision

**The claim moves onto the row that carries the cash, and the correction is
derived rather than marked.**

1. `Portfolio.fees_settled` is how much of the venue's fee take the book's
   `cash` already reflects. It is persisted in `equity_snapshots`, by the same
   `INSERT` as `cash`. The two cannot disagree across a crash because they are
   one write.
2. `broker_fees` goes back to recording only what it can vouch for: the charges
   the venue has told us about. `applied_at` becomes `seen_at`. The ledger no
   longer has an opinion about the book.
3. `FeeLedger.record_seen` returns the total over **every** charge ever recorded
   for the run mode — not over the batch offered, and not over the lookback
   window. As old charges age out of the caller's window a windowed total would
   fall, and the difference would read as a credit owed back to cash.
4. Every settling pass computes

   ```
   owed = total_seen - portfolio.fees_settled
   ```

   applies it to cash, and sets `fees_settled = total_seen`. Both operands are
   durable before the pass and both move together after it.

## Why derived beats exactly-once

Exactly-once is a promise about a sequence of events. Derived state is a
property of the data, and it holds regardless of what happened.

- **A crash anywhere is recoverable.** Both operands survive independently, so
  the next pass computes the same `owed` and applies it. The interrupted
  settlement is not lost, it is simply redone.
- **The state 2026-09-09 left behind heals on its own.** A ledger holding
  charges against a book that never settled them is not a special case needing
  a repair script; it is a book whose `fees_settled` is behind, which is the one
  condition this module exists to close. No operator deletes a row.
- **The empty sweep stops being a dead end.** ADR 0030's settlement returned
  early when the venue reported nothing, which is exactly when a stuck book most
  needs the ledger consulted — the charges are days old and no longer in the
  window. The ledger is now asked on every pass, whatever the feed says.
- **A reversal works through the same arithmetic.** A charge reversed at the
  venue, or a row an operator removes, leaves the book having settled more than
  the venue charged; `owed` goes negative and the money comes back. Under 0030
  that was unrepresentable.

## What was rejected

**One transaction spanning the fee rows and the portfolio snapshot.** The
obvious repair, and it does close the window. It needs the persistence layer to
share a session across two repositories, which it cannot currently do, and it
buys only atomicity — a crash still loses the pass, it just loses all of it
cleanly. Derived state gives atomicity *and* recovery, for a column.

**Persisting the portfolio immediately after settling.** Narrows the window and
does not close it. A narrower race on a money balance is not a fix, it is the
same bug at a lower rate, and it would have been reported as fixed.

**Keeping `applied_at` and adding a `settled` flag.** Same disease: a second
claim about the book, kept somewhere the book is not.

## Consequences

- One extra column, and one number to keep true. `fees_settled` is meaningless
  on its own; it is only ever read against the ledger's total.
- `equity_snapshots` rows written before this migration carry `fees_settled = 0`,
  which is not a default standing in for the truth — it *is* the truth, since no
  book has ever had a fee correction durably applied. The first reconcile after
  the migration therefore settles the entire ledger in one pass, which is the
  $4.14 outstanding since 10:25:29.
- `FeeSettlement.total` now means the derived correction rather than the sum of
  the charges in the sweep, and `is_empty` is keyed on it: a pass can see a
  hundred charges and owe nothing, which is the steady state.
- Reported P&L is still gross of regulatory fees at the *position* level on
  Alpaca, exactly as ADR 0030 says. That has not changed and cannot from this
  feed.
