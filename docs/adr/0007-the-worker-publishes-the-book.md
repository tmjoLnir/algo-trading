# 7. The worker publishes the book; the API serves it

**Status:** Accepted · 2026-08-18

## Context

Requirement #7 asks for one screen showing what the platform holds, what it has
decided, and why — refreshed every five minutes. `docs/DASHBOARD.md` already
fixed the shape of the answer: **one aggregate endpoint, not six parallel
requests**, because six fetches produce a screen assembled from six different
instants and a reader cannot tell which number to trust.

That settles how many *requests* there are. It does not settle who *computes*
them, and by the time Phase 5 started there were two candidates, both viable:

1. **The API assembles the screen.** It can already reach everything it would
   need: `PostgresOrderRepository`, `PostgresPortfolioRepository`, the Redis
   quote cache, the kill switch and the trading calendar. One handler, one
   transaction, no new moving parts.
2. **The worker publishes a snapshot and the API serves it.** The strategy
   runner already holds a live `Portfolio`, the set of orders it believes are
   working, and the marks it just valued the book at. Step 6 of its documented
   evaluation ordering is "persist state and publish updates", and only the
   persisting was built.

Option 1 is less code and fewer failure modes. It is also wrong, and the reason
is not obvious until you write down what the two processes would each be doing
at the same moment.

## Decision

**The worker computes the book once, at the end of the evaluation it just
acted on, and publishes it. The API serves that verbatim and computes nothing
about the book itself.**

The split is not total. Three things are deliberately *not* in the published
snapshot and are answered by the API on every request:

| | Source | Why not the snapshot |
|---|---|---|
| `run_mode` | API configuration | The banner saying whether this is real money must not depend on a process that can die |
| `market_open` | `TradingCalendar` | A pure function of the clock; no I/O, no staleness |
| `active_halts` | `KillSwitch.active_halts()` | A halt banner sourced from a snapshot nobody is publishing says "not halted" at exactly the moment that matters most |

Day P&L is a fourth case and sits with the API for a different reason: its
anchor is the first `equity_snapshots` row of the current session, which is a
*historical* fact rather than a concurrent one. Reading it in the API keeps a
display query out of the trading loop.

## Consequences

**One answer to "what is my equity".** Under option 1 the API would recompute
equity from its own reads, at its own instant, while the runner simultaneously
held a different number computed from its own book. Two answers to that question
is precisely the failure the single aggregate endpoint exists to prevent — it
would simply have moved the inconsistency from between six fetches to between
two processes, where it is harder to see.

**The response carries two timestamps.** `as_of` is when the API assembled the
reply; `book_as_of` is when the worker built the book half. That is more honest
than one, not less: the concern behind "one `as_of`" is six parts of the *book*
from six instants, and the book still has exactly one. The dashboard displays
both ages and warns when the book stops advancing, which is what makes a dead
worker visible rather than merely quiet.

**A dashboard with no worker still works, and says so.** `book_as_of` is null,
the account is null, and the banners, halts and kill switch all render. "You
hold nothing" and "nobody has said what you hold" are different sentences and
the API never conflates them.

**The freshness of the book is bounded by the runner's tick**, not by the
poll — one minute by default. That is well inside the five-minute cadence
requirement #7 asks for, and the WebSocket carries fills, signals and halts in
between.

**A worker that is not trading publishes nothing.** `WORKER_STRATEGY` is empty
by default, so the ordinary state of a fresh deployment is a dashboard with no
book. This is reported as itself rather than as an empty account.

**The snapshot is a second copy of the book, and copies can disagree.** It is
mitigated rather than eliminated: it is written *after* the durable writes in
the same step, from the same objects the loop just used, and it carries the
instant those writes shared. A failure to publish is swallowed and logged — an
unreachable Redis must not fail an evaluation, because three failed evaluations
halt trading and stopping a strategy to protect a screen is a cure worse than
the disease.

**Adding a producer is a two-file change**, not a two-process one: publish to a
channel in `atp_core.channels` and the API's WebSocket bridge forwards it.

## Alternatives

**The API assembles the screen (option 1).** Rejected for the two-answers
problem above. It also needs `buying_power` from the broker to fill the account
view, which is a venue call per dashboard poll on the same rate limit the
trading process is placing orders against.

**Publish over pub/sub instead of a key.** Rejected: pub/sub is fire-and-forget,
so a browser opened at any moment would see nothing until the next tick. The
dashboard's premise is that opening it *now* shows the current picture, which is
a read of the latest value rather than a replay of the last message. Both paths
exist and each is right for its job — `atp_core.channels` carries the
announcements, `SnapshotStore` carries the state.

**Put the halts in the snapshot too.** Rejected, and this is the one worth
remembering: it would have made the halt banner an artefact of the worker being
alive. The single most important thing on the screen would have been silently
wrong in the single most likely incident.
