# Dashboard

Requirement #7: review live trades selected by preset rules, auto-refreshed
every 5 minutes.

## The refresh model

Two paths, deliberately:

| | Cadence | Role |
|---|---|---|
| Poll `/dashboard/live` | 5 min | authoritative, consistent snapshot |
| WebSocket | live | ticks, fills, signals and halts between polls |

**One aggregate endpoint, not six parallel requests.** Six fetches produce a
screen assembled from six different instants; on a fast-moving position, a P&L
computed from one snapshot and a price from another simply disagree, and the
reader cannot tell which to trust. One query, one picture.

**The WebSocket is an enhancement, never the source of truth.** A dropped socket
degrades the dashboard to 5-minute freshness rather than leaving it stale
forever. That is why the poll exists even though push is "better".

Implemented in `useLiveDashboard.ts`:

- interval comes from the server's `refresh_seconds` — one place to change it
- no polling in a hidden tab
- refetch on window focus, so alt-tabbing back does not show 5-minute-old data
- `ageSeconds` is displayed, and warns visibly past 1.5× the interval

## Where the numbers come from

The response has two halves, and the split is the subject of ADR 0007.

**The book** — account, positions, signals, working orders — is computed **once,
by the worker**, at the end of the evaluation it just acted on, published to
Redis, and served verbatim. The API does not recompute it. It could: it can
reach the order table, the position snapshots and the quote cache. But it would
be computing equity at its own instant while the runner simultaneously held a
different number, which is the same "two instants" problem the aggregate
endpoint exists to prevent, moved somewhere harder to see.

**The run mode, whether the market is open, and the active halts** are answered
by the API on every request, from configuration, the exchange calendar and the
kill switch directly. Each of those must still be correct when the worker is
dead — a halt banner sourced from a snapshot nobody is publishing would say "not
halted" at exactly the moment that matters most.

So the response carries **two timestamps**:

- `as_of` — when the API assembled the reply;
- `book_as_of` — when the worker built the book half, with `book_age_seconds`
  beside it.

That is more honest than one, not less. The concern behind "one `as_of`" is six
parts of the *book* from six instants; the book still has exactly one.

**Nothing published is not an empty book.** A worker that is not trading — the
default, since `WORKER_STRATEGY` is empty — publishes nothing, and the endpoint
reports that as itself: `book_as_of`, `account` and `data_feed_healthy` are all
null, and the banners, the halt list and the kill switch still render. "You hold
nothing" and "nobody has said what you hold" are different sentences and only one
of them is safe to act on.

**An unreadable store is a 503, not an empty book.** A dashboard that rendered
"no positions" because Redis blinked would be telling its reader they are flat.
The client keeps its last good data on screen, labelled stale.

## Layout priority

Top to bottom, in the order a person needs it:

1. **Run mode banner** — backtest, paper or live. Loud, permanent, not
   dismissible. An unrecognised mode falls through to the loudest branch.
2. **Halt banner** — if trading is stopped, nothing else matters first.
3. **Account summary** — equity, day P&L, exposure, leverage.
4. **Equity curve.**
5. **Open positions** — with distance-to-stop.
6. **Signal feed** — what the rules decided and *why*.
7. **Working orders.**
8. **Kill switch** — always visible, never behind a menu.

Positions before signals: what you are exposed to matters more than what the
system is thinking about.

## Rules for this UI

- **Never show a price without its age.** Grey out anything stale. The book's
  age is displayed next to the tab's own refresh age, because a tab that just
  refreshed against a worker that stopped publishing an hour ago is fresh by one
  measure and useless by the other.
- **Never render a monetary value from a float.** The API sends Decimals as
  strings. `src/lib/money.ts` formats them *as strings* — grouping, padding and
  truncating — and there is no decimal library because the dashboard does no
  arithmetic on money at all: every derived figure arrives computed. `parseFloat`
  on a balance is a bug. The single exception is `toChartNumber`, for chart
  geometry, named so that any other use of it reads as a mistake.
- **A figure we do not know renders as `—`, never as `0`.** The API sends null
  for an unmarked position, for leverage against zero equity, and for day P&L
  with no session anchor. Zero is a value a reader acts on.
- **Colour is not the only signal.** Red/green needs a sign or an arrow too.
- **Show `reason` on every signal** — including refused ones, with the rule that
  refused them named. A strategy blocked by a risk limit on every bar looks,
  from anywhere else in the system, exactly like a strategy with no ideas.
- **Distance-to-stop, not just the stop price.** The server sends the fraction
  of the entry-to-stop distance still standing: 1.0 at the entry, 0.0 at the
  stop, above 1.0 in profit. It is **signed** — negative means price is already
  through an unfired stop, and clamping that to zero would render the most
  alarming row on the screen as an ordinary one.
- **A live tick does not overwrite the book.** Quotes arriving over the socket
  are shown beside the mark, labelled live, rather than written over it. The P&L
  in the same row was computed from the mark; swapping the price would put two
  instants in one row, and recomputing the P&L in the browser would mean doing
  arithmetic on money in IEEE 754.
- **Errors keep the last good data on screen**, clearly labelled stale. A blank
  dashboard during an API blip is worse — the user still needs to see what they
  hold.
- **Destructive actions confirm.** Except the kill switch, which must not.

## The WebSocket

`apps/api/src/atp_api/ws.py` holds sockets open and forwards what the worker
publishes to Redis (`atp_core.channels`). Producers: the ingestor for quotes and
bars, the strategy runner for fills and signals, the kill switch for halts.

- **Halts reach every client**, subscribed or not. A trading halt is not
  something to opt into.
- **Market data is filtered by symbol**; execution events are not. A fill on a
  symbol you did not subscribe to is still your money.
- **A client that stops reading is dropped** on a short deadline rather than
  buffered for. Unbounded buffering for one slow reader costs every other
  client, and the poll recovers whatever it misses.
- **The bridge retries forever.** Nothing here is traded on, so a dead bridge
  costs live updates and never data a decision is made from.

## Conventions

TanStack Query owns all server state — no `useEffect` fetch loops. API types are
**generated** from the OpenAPI schema and committed:

```bash
make gen-types    # dumps the schema from the app, then openapi-typescript
```

No running server is needed. `src/api/types.ts` is nothing but aliases over the
generated `schema.d.ts`; if one stops compiling, the server contract changed and
the components reading it need to change too. That is the alarm working.

## Not built yet

Stated here rather than left to be discovered:

- **The signal feed does not survive a restart.** It is a bounded in-memory ring
  on the strategy runner. `persistence.models.SignalRow` is the durable home and
  nothing writes it — that belongs with the roadmap's trade-reconstruction item.
- **`/dashboard/health` is a stub.** It would report worker heartbeat, broker
  reachability and the last reconciliation result, none of which the worker
  publishes anywhere. Building it means giving the worker a health key to write.
- **Per-position strategy attribution is absent.** `PostgresOrderRepository`
  stores `strategy_id` as null (a strategies row must exist first, and nothing
  writes one), so a position cannot be traced to the strategy that opened it.
  The snapshot names the strategy running the whole book instead.
- **There is no authentication.** docs/SAFETY.md's access-control item is Phase
  6 and blocking for any deployment. The socket is read-only, but everything it
  carries is disclosure.
