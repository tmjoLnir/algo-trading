# 30. A venue fee is a movement of cash, not drift for a tolerance to absorb

**Status:** Accepted · 2026-09-09

Narrows a premise ADR 0011 and `Reconciler` both rest on. The reconciliation
design is otherwise unchanged and this depends on it.

## Context

`Reconciler._cash_discrepancies` compares our cash with the venue's and states
the assumption underneath the check:

> Cash is arithmetic on fills, so a drift beyond the tolerance means a fill one
> of us does not know about — which is exactly what this exists to catch.

That is true of a venue which charges nothing outside the fill. Alpaca is not
one. It charges three regulatory fees on equities — CAT, REG and TAF — and books
each as an **account activity**, never on the fill. `AlpacaBroker` therefore
built every `Fill` with `fee=0`, at both sites, each carrying a comment saying
the number lives on the activities feed and calling it "a known gap the
activities endpoint closes".

Nothing called that endpoint. The only two occurrences of `activities` in the
repository were those two comments.

So our cash was a fills-only total and the venue's was not. The consequence is
not noise that settles in both directions; it is a **ratchet**. Every session
adds its fee take to a gap that nothing ever gives back. The account feed for
2026-09-08 — a single session — held exactly three rows:

```
FEE / CAT  -0.01   "CAT fee for proceed of 174 trades"
FEE / REG  -3.82   "REG fee for proceed of $185271.37"
FEE / TAF  -0.31   "TAF fee for proceed of 1572 shares (89 trades)"
```

$4.14, against a `DEFAULT_CASH_TOLERANCE` of $1.00. The next morning the worker
read its own stored book (cash 100094.20), asked the venue (cash 100090.06),
halted global trading and crash-looped on `restart: unless-stopped`.

`fees_paid` was zero on every position for the same reason, so reported P&L was
overstated by the same amount. This was never only a reconciliation problem.

## Decision

**A fee the venue has charged is money that has already left the account, and
our book is brought up to date with it before the two are compared.**

1. `BrokerPort.get_fee_activities(since)` — on the port, not on the Alpaca
   adapter. Any venue that charges a fee it does not put on a fill drifts our
   cash the same way, and the reconciler must be able to ask without knowing
   which venue it is talking to. `SimulatedBroker` returns an empty list, which
   is a real answer: its cost model charges the fill itself.
2. `FeeActivity.amount` is **positive for money leaving the account**, whatever
   the venue's own sign convention. Every caller settles with `cash -= amount`.
3. `broker_fees` is a ledger keyed on the venue's own activity id, and
   `record_unseen` inserts and returns in one `ON CONFLICT DO NOTHING …
   RETURNING`. Applying a charge twice is therefore impossible rather than
   merely unlikely.
4. Settlement happens **inside `Reconciler.reconcile`**, before the comparison.
   One place, so `warmup` and the five-minute job cannot diverge about whether
   fees were applied, and the re-read cannot double-charge.

## What was rejected, and why

**Widening `DEFAULT_CASH_TOLERANCE`.** The obvious fix and the wrong one. A
tolerance absorbs noise; this is a monotonic accumulator, so any ceiling is
breached on a schedule set by how much the platform trades. It also blinds the
one check that catches a genuinely missed fill — the thing layer 7 exists for —
by exactly the amount it is widened.

**`adopt_broker_state`, per docs/RUNBOOK.md.** The documented recovery, and it
works for one session. At $4.14 a day against a $1.00 tolerance it buys back
less than a trading day, which makes it a morning ritual rather than a fix.

**Estimating the fee from a schedule.** The rates are public and the arithmetic
is easy. It is still a guessed number in a P&L ledger, which is the reason both
`Fill` sites hold zero rather than an estimate, and it would drift from the
venue's own rounding on a cadence nobody could predict.

**Attributing an account-level charge back to individual fills.** A REG fee is
charged on a day's *proceeds*, so it belongs to no order and no position.
Dividing it across a session's symbols would put a number nobody chose into
per-position P&L. Fees settle against cash and `Position.fees_paid` stays the
fill-level field it was designed as — which means it stays zero on Alpaca, and
`docs/RUNBOOK.md` says so rather than leaving it to be discovered.

**A date watermark instead of a ledger.** Cheaper, and it silently under-counts:
Alpaca stamped 2026-09-08's fees with `created_at` after midnight UTC on the
9th, so a fee booked late slips behind a cursor and is never applied — the same
ratchet, arrived at more slowly.

## Consequences

- The reconciler's cash check means what its docstring always claimed. A cash
  discrepancy that survives settlement really is a fill one of us does not know
  about.
- One extra REST call per reconcile. On a five-minute schedule that is well
  inside Alpaca's 200 req/min.
- **A fee-feed outage is not a halt.** `settle_broker_fees` logs and returns
  empty, the unapplied charge stays visible as drift, and the worst case is
  exactly the behaviour that existed before this ADR.
- **A fee type nobody listed fails safely.** `_FEE_ACTIVITY_TYPES` is narrow —
  `FEE` only, because `REG` and `TAF` are sub-types under it. A charge outside
  the list is not applied, so it shows up as drift and halts, rather than being
  quietly folded into the book by a rule nobody wrote.
- Reported P&L is still gross of fees at the *position* level on Alpaca. The
  account-level total is right; `Position.fees_paid` is not, and cannot be from
  this feed.
