# 15. A trade is a position episode, reconstructed on demand

**Status:** Accepted · 2026-08-19

## Context

Requirement #6 asks what a strategy actually did: which trades it took, what
they made, and why each one ended. Nothing in the platform stored a *trade*. It
stored orders and fills, which are what a venue deals in, and a human does not
reason in orders — an entry that filled in four prints across two sessions and
was exited in two pieces is one decision that made or lost one number.

So there is a reconstruction to do, and two questions to settle before writing
it. Neither has an obviously right answer, and both change every figure the
analytics layer reports.

**What counts as one trade?** Three defensible units:

1. **A tax lot.** Each entry fill is a lot; each exit closes lots FIFO. This is
   what a broker's statement shows and what a tax authority wants.
2. **A position episode.** Flat, through however many scale-ins and partial
   exits, back to flat. One row per "we were long SPY from Tuesday to Friday".
3. **An order pair.** Each exit order matched to whichever entry order it
   closes.

**Who computes it, and when?** ADR 0007 settled the equivalent question for the
live book in the opposite direction: the *worker* computes it once and the API
serves that verbatim, because two processes computing "what do we hold" at two
instants disagree. Following that pattern would mean the runner maintaining a
trade list and publishing it. Not following it needs a reason.

## Decision

**A trade is a position episode — flat to flat — and it is reconstructed from
stored orders at request time, in the API process.**

### Why the episode

Option 3 is out on its own: an entry that filled in two orders and exited in one
has no natural pairing, and any rule invented for it is arbitrary.

Between a tax lot and an episode, the deciding argument is what the numbers are
*for*. This platform's analytics exist to answer whether a strategy is worth
running, and three of the four things worth knowing are only defined on an
episode:

- **`exit_reason`.** A lot closed by a partial take-profit and later by a stop
  has two exit reasons and no single answer. The episode has one — the exit that
  returned it to flat — and exit-reason attribution is the most actionable table
  the analytics layer produces.
- **The holding period.** "How long did we hold this?" means from first entry to
  final exit, not from a lot's entry to the fill that happened to consume it.
- **MAE/MFE.** An excursion is measured over a window, and the episode is the
  window a person means by "during the trade".

The fourth, per-trade P&L, is the one a tax lot answers better — and it is
answered better for an accounting question this platform does not ask.

A consequence worth stating plainly: **the FIFO-vs-LIFO choice mostly stops
mattering under this grouping.** Within one episode entries and exits are
aggregated, so both conventions produce identical per-trade P&L. The convention
binds in exactly one place — a fill that carries a position *through* zero, where
some of its quantity closes the old episode and the rest opens the opposite one.
That split is FIFO, and the fill's fee is divided pro rata by quantity. There is
no better answer available for the fee: the venue charged one commission for one
execution and nothing in the print says which part belonged to closing.

### Why reconstruct rather than publish

ADR 0007's argument does not transfer, and the reason it does not is the whole
of this half of the decision.

That ADR is about **a quantity that is still moving**. Equity, exposure and
unrealised P&L change with every tick, so two processes computing them a second
apart genuinely disagree, and the disagreement is invisible to the reader. A
*closed* round trip is finished. Its entry price, exit price and P&L will never
change again, and two processes reconstructing it from the same stored fills
cannot disagree about anything.

With no disagreement to prevent, the costs decide, and they point the other way:

- The runner has a one-minute tick and a documented six-step ordering it must
  complete inside it. Adding report generation nobody is reading between polls
  spends that budget on the wrong thing.
- A published trade list is a *cache*, and one whose correctness depends on the
  worker having been running. A trade taken during an outage would be missing
  from it and present in the orders table — the report and the source of truth
  disagreeing, which is the failure ADR 0007 exists to prevent, arrived at from
  the other side.
- Reconstruction is a pure function of stored orders, so it is testable without
  a running worker and identical whoever calls it.

### The read has no start bound

Round trips are matched from flat, so an exit can only be paired with its entry
if that entry is in the same list. `OrderRepository.filled_orders` therefore
takes `until` and no `start`, and a caller wanting a period filters the *trades*
that come out. A window applied to the orders going in would present every
position opened before it as an exit with no entry — and the tempting reading of
that is a short that was never opened, which inverts the sign of its P&L.

## Consequences

- Every analytics request reads every filled order in the account's history.
  That grows without bound. It is affordable now — one operator, one strategy,
  a paper week — and `docs/ANALYTICS.md` records the threshold and the fix: a
  stored trade table, reconstructed once and appended to. **Not** a truncated
  read, because a truncated read does not get slower, it gets wrong.
- A position still open is not a trade and does not appear. Realised figures
  only.
- `max_drawdown` from `/analytics/performance` is the drawdown of *realised*
  P&L, which is shallower than what the account experienced.
  `/dashboard/equity-curve` answers the other question.
- The `orders` table gained a `purpose` column (migration `c3f8b2d5e714`) and
  `Order` gained the matching field. `purpose` already existed and was already
  load-bearing — it is part of the `client_order_id` derivation — but it was
  consumed by that derivation and dropped, and a SHA-256 digest cannot be read
  backwards. Without it, all three engine-side exits store identically.
- `OrderRouter.flatten` gained a `purpose` parameter for the same reason, which
  changes the idempotency key of an engine-side exit. That is a correction: a
  stop and a time exit firing on the same bar are two decisions, and one key for
  both would silently drop the second.

## Alternatives considered

**Store a trade row when a position goes flat.** The runner knows the moment it
happens, and it would make every read cheap. Rejected for now on the same
grounds as the published list: it is a second source of truth for something
derivable, and a trade taken while the runner was down would be missing from it
forever. It becomes the right answer when the reconstruction read stops being
affordable, and at that point it should be built as a *cache* that can be
rebuilt from the orders table rather than as the record itself.

**Tax-lot trades, with episodes derived as a grouping on top.** More
information, and strictly more work at every layer — the API, the view models,
the screen. Deferred rather than refused: nothing has asked for lot-level detail
yet, and `build_trades` is the only place that would change.

**Make the matching convention configurable.** Rejected. Under episode grouping
there is almost nothing for it to configure, and a setting that changes reported
P&L is a setting that makes two reports incomparable without either of them
saying so.
