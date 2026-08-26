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

## The audit page

`audit_log` had been in the schema since the first migration with nothing
writing it and nowhere to read it. It is written now, and it is on a screen —
the second half being the point, because a record nobody can see is a record
nobody checks.

Two rules it inherits from the rest of this document:

- **An unreadable trail is not an empty one.** A 503 renders as "the audit trail
  could not be read… nothing can be concluded from this screen", never as
  "nothing recorded yet". Same distinction as "nothing published is not an empty
  book" — the reader is looking at this page *because* something went wrong, and
  telling them nothing happened is the one answer that is actively misleading.
- **An action with no target renders `—`.** Signing out is not done *to*
  anything, and that should read as "no object" rather than as missing data.

Colour is an accent, never the signal: every row names its action in words, and
`login_failed`, `rate_limited` and `forbidden` are merely tinted on top of text
that already says so.

Paging is by cursor (`before_id`), not offset. Rows arrive while the page is
being read — most of all during whatever is being investigated — and an offset
shifts under the reader every time one does.

What is recorded today is authentication and refusals. Order flow and
kill-switch changes are not, because those handlers are stubs; see ADR 0010.

## The analytics page

`/analytics`, over the three endpoints in docs/ANALYTICS.md — the metric set,
the attribution breakdown and the closed-trade list. Same shape of gap as the
audit page: the endpoints had been built and tested since #58 with nothing
reading them, and a report nobody can open is a report nobody reads.

It is the one screen in this app that is deliberately **not** one aggregate
request, and the exception proves the rule stated at the top of this document.
Six fetches are refused for the live book because a P&L computed at one instant
beside a price fetched at another disagree. A finished period has no such
problem — a round trip that closed last Tuesday is the same number in every
response — so the three panels are three requests that name one explicit window
and fail independently. Nothing here polls, either: a finished period does not
move, and a poll would re-run a reconstruction over the whole order history to
produce an identical answer.

Two rules from this document reach it unchanged, and one is new:

- **A figure we do not know renders as `—`, never as `0`** — applied to a whole
  panel rather than a cell. A period with no closed trades reports that in
  words; `compute_all` returns 0.0 for every ratio it cannot compute, and
  nineteen zeros on screen read as a flat month rather than an empty one.
- **Never render a monetary value from a float.** Every per-trade figure is a
  decimal string through `money.ts` as usual. The metric set is not: it is a bag
  of JSON numbers, five of which are money-shaped statistics computed in float
  space by the same functions the backtest uses. `src/lib/stats.ts` formats
  those and says so on screen — `money.ts` takes only strings, so the compiler
  keeps the two apart.
- **New here: a grouping in UTC says UTC.** Attribution by hour keys on the
  entry's UTC hour while every timestamp on the screen is local, and an
  unlabelled `14` invites a comparison between two clocks.

## The orders page

`/orders`, over `GET /api/v1/orders` — the order table, newest first, with
filters for status, symbol, strategy and a start date.

**It exists for the orders that never filled.** The dashboard's working-orders
panel shows what is live at the venue right now, and `/analytics` shows round
trips that completed. An order that was *refused* is in neither, and in nothing
else either: it moved no quantity, so `filled_orders` excludes it by design, no
reconstructed trade contains it, the book never held it and the equity curve
never moved for it. Before this screen, a strategy whose every order was refused
looked — from every other view in this platform — exactly like a strategy that
never placed one. Those two call for opposite responses.

That is also why the two refusals are worded apart rather than both saying
"rejected". `rejected_risk` is our own engine declining to send an order, which
is a limit doing its job. `rejected` is the venue declining one we sent, which is
a problem with the account or the instrument. One bucket for both would hide
which happened.

Three things it refuses to do:

- **A refusal with no recorded reason says so**, rather than rendering the dash
  that means "nothing refused this order". The dash and the missing reason are
  different facts and the second one is alarming; collapsing them into one glyph
  states the opposite of the truth. Same family as the rule for signals — show
  `reason` on every one, refused ones included.
- **A partial fill is shown as a proportion**, filled against asked, not left to
  the status word. `cancelled` covers an order that never traded and one that
  filled 90% before the cancel landed, and those are different positions.
- **A full page says it is full.** A list that stops at exactly the row limit
  looks identical to a list that ended, and only one of them means "this is
  everything".

The page names the run mode the rows belong to, because paper and live orders
share a table.

**Only the read is built.** `POST /orders`, `DELETE /orders/{id}` and
`/orders/cancel-all` are still stubs, and deliberately: they place things, and
there is exactly one path from an intent to a venue (rule §1.5, ADR 0005). That
path also carries the audit writes ADR 0010 is waiting on. A read needs none of
it.

## The positions page

`/positions`, over `GET /api/v1/positions` — the book the worker last *wrote to
Postgres*, with the age of that record.

**The dashboard shows the same book from a different place, and the difference
is the whole point.** The dashboard reads what the worker published to Redis;
when the worker stops there is nothing to read, and it correctly reports no book
at all. The same book is written to the snapshot tables at every evaluation, and
that copy outlives the process. So this screen answers "what am I holding?" at
the moment the live one cannot — which is usually the moment somebody is asking.

That is **not** the recomputation ADR 0007 refuses. Nothing here adds a position
up from orders and quotes; it is the worker's own computation, read back from
the table the worker wrote it to. What it *is* is possibly old, and that changes
what the screen owes the reader:

- **The age leads.** It is the first thing on the page, not a footnote, and past
  ten minutes — several missed evaluations — the whole header becomes a warning
  telling the reader to treat the figures as history rather than as their
  current exposure. A stored book rendered as though it were current would be
  ADR 0007's failure moved from a cache to a table.
- **Two ages, not one**, for the same reason the dashboard shows two: a tab that
  refreshed a second ago against a worker that stopped an hour ago is fresh by
  one measure and useless by the other.
- **No live quotes.** The dashboard overlays socket ticks beside the mark. Here
  every figure is as of one past instant, and a live price beside a P&L computed
  hours ago would put two instants in one row.
- **Never written is not empty.** A worker that has never traded has stored
  nothing, and that reads as itself rather than as "you hold nothing" — the same
  distinction the live endpoint makes.

Every derived figure — market value, unrealised P&L, leverage, the
distance-to-stop fraction — comes from `atp_core.dashboard`'s own
`position_summary` and `account_summary`, the functions the live book is built
from, and the rows are the same `PositionView` rendered by the same
`PositionsTable`. One expression per figure, two screens: a distance-to-stop
that disagreed between them would be a bug invisible from either.

Day P&L is **absent** rather than zero. It is this equity against the session's
first recorded one, which is a question about the equity history rather than
about one snapshot, and the dashboard is where it is answered.

**Only the read is built.** Closing a position and moving a stop both place
orders — rule §1.5 again — so `POST /positions/{symbol}/close` and
`PATCH /positions/{symbol}/stop` are still stubs. So is
`GET /positions/{symbol}`, for a different reason: nothing consumes it. The
screen reads the whole book in one request, and an endpoint built, tested and
documented with no caller is the gap the analytics endpoints sat in for a phase.

## The strategies page

`/strategies`, over `GET /api/v1/strategies` — the rows a worker has written,
and the strategy classes the code registers, in one response.

**The gap between those two lists is why the page exists.** A class is
registered at import time; a `strategies` row is written by the runner at its
first session open — or by a create, by `scripts/seed.py`, or by the first
backtest queued for the class, which is why the registry table says *stored*
rather than "a worker has run this". The absence of a row is the half that still
means exactly one thing. `WORKER_STRATEGY` is empty by default, so a platform with
strategies in it and nothing running is the ordinary state of a fresh install —
and no screen could say so. "I wrote a strategy and nothing is happening" had no
answer anywhere in this UI. The response carries `never_run` computed
server-side, because every client would otherwise diff the two lists identically
and the diff *is* the answer.

Two labels here are deliberately not the column names behind them, because the
columns mean less than they say:

- **`state` is not "is it running now".** `StrategyRepository.ensure` writes
  `draft` when it creates a row and never touches it again, so a strategy a
  worker has been running for a month still reads `draft` — that is the
  ratchet's first rung and the endpoints that would promote it off are stubs.
  The screen shows it as the configured state and puts the liveness question on
  the timestamp instead. It wrote `active` until #76, which `StrategyState` has
  never contained; a CHECK constraint now refuses anything outside the enum.
- **`updated_at` is not "last edited".** The same asymmetry — a later boot bumps
  only the timestamp — so the API serves it as `last_started_at` and the column
  header reads "a worker last started this". Under its own name it would invite
  every reader to conclude somebody edited the strategy this morning.

One trap worth recording because it was nearly shipped: **the registry is
populated by an import side effect.** `@register` runs when a strategy module is
imported, so a process that has never imported one has an empty registry — and
would report, with total confidence, that this platform has no strategies. The
worker and the backtest script already import `atp_core.strategy.examples` for
exactly this reason; the API had never needed to, because nothing here read the
registry until this endpoint.

A filtered request still reports every available class, and `never_run` is still
computed against the **unfiltered** table. Otherwise filtering to `paused` would
report every active strategy as one nothing has ever loaded.

**Creating is built; the rest of the ratchet is not.** `POST /api/v1/strategies`
stores a strategy at `draft`, which is the rung a booting worker and
`scripts/seed.py` already write and which authorises nothing. Every rung above it
is a promotion, and promotion to `live` wants a completed backtest on record — now
answerable, since `backtest_runs` has a reader — a minimum paper-trading period,
which nothing yet records the start of, and a verb per transition. An endpoint
that promoted while skipping a check it could not perform would be the ratchet
with its pawl removed, which is worse than no endpoint, so edit, promote and
pause stay stubs. `GET /strategies/{id}` and `GET /strategies/available` stay
stubs for the duller reason: the list above already carries both, and nothing
calls either.

**There is no authoring form yet**, so the create endpoint is reached from a
client rather than from this screen. What it is for is the declarative half of
the platform: a rule set had no way into the database at all, and
`POST /api/v1/backtests` has been able to run a stored one since #96. A creation
is written to the audit trail against the session's user (`strategy_created`),
because this is where a strategy's name — the key every later signal and order
carries — comes into existence.

## The backtests page

`/backtests`, over `POST /api/v1/backtests` and its four reads — the last of the
seven tabs, and **the only screen in this app that starts work**. Everything else
either reads something already computed or halts trading.

It is also the only screen with a form, and most of that form's design is about
staying out of the way of the server's validation:

- **Money is typed as text.** `<input type="number">` hands back a JavaScript
  number, and a starting cash that had been through IEEE 754 would propagate into
  every figure the run reports. The API takes a decimal string.
- **The dates are sent with an explicit `Z`.** A date input yields a bare
  `YYYY-MM-DD`, and the API refuses a naive datetime at the boundary rather than
  assuming a zone (rule §1.2).
- **The server's refusal is shown verbatim.** The important one is missing
  history: the API answers 400 with the exact `scripts/backfill_bars.py` command
  that fixes it, and paraphrasing the one actionable message on this screen would
  turn it into a dead end. Duplicating the server's rules here would give two
  answers to one question, and the client's is the one that drifts.
- **The zero-cost option is labelled with what it costs you.** docs/BACKTESTING.md
  is unambiguous that a zero-cost result is not evidence about a strategy, so it
  reads as that rather than as the quicker choice — and a run that used it is
  flagged on its row, because it invalidates everything else on it.

- **A blank strategy id is refused here, not by the server.** The strategy is the
  only value on this form nobody types — it is derived from the strategies list —
  so it is the only one that can be empty without anybody having done anything. A
  `strategies` row carries whatever `Strategy.name` the worker booted with, so a
  blank name makes a row that shows a label in the picker and carries no id. The
  server's own refusal for that, `strategy_id is empty`, is correct and unreadable
  beside a picker visibly showing a strategy, because it describes the request
  when the fault is in the row. The id is also trimmed before it is sent, because
  the server strips it before both the registry lookup and the spec it stores —
  sending the raw value is accepted at the door and then misses the foreign key
  onto `strategies.id`, surfacing as a 409 about a row that could not be found.

**It offers every strategy the platform has**, stored or merely registered, and
for most of this screen's life it did not. `backtest_runs.strategy_id` is a
foreign key onto `strategies`, and the picker was built from that table alone —
so a class that existed in the code and had never been loaded was left out,
because queueing it produced a 409 for a choice the screen had invited. What
that actually asked of somebody wanting to backtest `buy_and_hold` was to
configure a *trading* worker with broker credentials a backtest does not need,
or to run the development seed script; the list was then an accident of which
strategies had been through one of those, usually one, on the tab whose subject
is comparing strategies.

`POST /api/v1/backtests` writes the row itself now, for a registered class it is
queueing the first run of — the same row an author would have created, at
`draft`, carrying the class's declared defaults and claiming no universe. So the
picker is the union of the stored rows and the registry, and a choice with no
row says "no worker has run this yet" beneath the control rather than being
hidden by it. Four things write that table now — a runner at its first session
open, `POST /api/v1/strategies`, `scripts/seed.py` and a queued backtest — so
"has a row" does not imply "a worker has run it", and the strategies page says
*stored* where it used to say a worker had run something. The gap that page
exists to show is unchanged: no row still means nothing has ever loaded it.

**A run has four states and each renders as itself.** A queued one says it is
waiting rather than showing an elapsed time counted from a timestamp nobody
wrote — `started_at` is genuinely null until a worker claims it. A running one
shows a bar built from the server's own `fraction`, with the bar counts beside it,
because a percentage alone cannot tell a slow run from one whose range turned out
to hold forty bars; a running run that has not reported yet says so rather than
showing 0%, which reads as stalled. A failed one shows its reason in words. A done
one shows its headline figures.

**This screen polls only while something is in flight.** Every other tab either
polls on a fixed cadence or not at all; a backtest changes state within seconds
and then never again, so the interval is derived from the data — once every run is
terminal the timer stops entirely and a tab left open makes no requests. Same
principle as not polling a hidden tab, on a different axis: do not ask a question
whose answer cannot have changed.

**The caveats are above the numbers, not below them.** A run's `warnings` come
from the server, because a number a reader has already seen is a number they
have already believed. Two sources, concatenated in that order:

- **Derived on every read** from the metric set — too few trades, an implausible
  Sharpe, in docs/BACKTESTING.md's own words. Recomputed rather than stored, so
  a threshold this project revises applies to runs already on record.
- **Recorded by the run itself** and stored on the row: orders refused before
  reaching the market, symbols whose history started late, costs switched off,
  a flat share count. None of these is a function of the metrics, and the first
  is invisible in them — a run refused everything and a run that never signalled
  report the same zeros. Serving only the derived half is what made a fully
  refused run indistinguishable from an idle one on this screen.

The run's own list ends with the refusal summary, so that line sits nearest the
numbers it qualifies. A run stored before the column existed has `null` rather
than `[]` and serves only the derived half — it never recorded the rest, and an
empty list would claim it finished clean.

The metric set goes through `src/lib/stats.ts`, not `money.ts`, including the five
metrics denominated in account currency. `compute_all` computes those in float
space, so the precision is gone before serialisation and formatting them with the
ledger formatter would claim a precision the response does not carry; the panel
says so in as many words. Everything on a *trade* — price, quantity, fee, P&L — is
a decimal string and goes through `money.ts` untouched. The equity curve's single
float conversion is `toChartNumber`, for geometry.

**The trade table is what makes the page worth opening.**
docs/BACKTESTING.md's pre-belief checklist asks for individual trades to be
inspected for impossible fills, and nothing else in this platform can answer it —
a metric set cannot show you the one fill that made the number. Exit reasons come
from the order that closed each position, so a stop-out and a signal exit are told
apart; that required the backtest engine to start setting `Order.purpose`, which
it never had (ADR 0016).

**Every run can be taken away as a file.** Each row carries a `JSON` button that
writes that one run to `backtest-<strategy>-<queued date>-<run id>.json`: the run
as the API served it — spec, metrics, warnings, all three timestamps — plus its
equity curve and every trade. Four decisions in it are worth stating:

- **Per run, not per list.** What a reader keeps, diffs or hands to a notebook is
  a *result*. Forty of them with the curves attached is not a file anybody opens,
  and a minute run's curve alone is hundreds of thousands of points.
- **Assembled in the browser from the reads that already exist**, rather than
  through a new export endpoint. Everything in the file is already served by the
  list and the two the detail panel makes; a fifth endpoint would thicken an
  `apps/api` that is meant to stay thin, and would still have to be fetched with
  the session cookie and turned into a blob here — a plain `<a href>` does not
  carry a credentialed cross-origin request. It goes through the same query keys
  as the detail panel, so an open run's curve is reused and a click during its
  load joins that request instead of making a second one.
- **Nothing in it is parsed.** Every monetary figure — `starting_cash`, each
  point on the curve, each price, fee and P&L on a trade — is a decimal string on
  the wire and is copied untouched, so what lands on disk is what the engine
  computed. The metric set stays float, because that is what it is.
- **A missing result is `null`, an empty one is `[]`.** A queued or running run
  has not produced a curve, and `RunRepository.fail` *clears* the curve and the
  trades on failure — a partial curve under a failed status is a chart of two of
  the five years somebody asked about. Those export as `null`, and the two
  endpoints are not called at all. A finished run that closed no round trip
  exports `[]`, because taking no trades is a result.

The button is offered on unfinished runs too — that file is the spec and the
status, which is still the record of exactly what was asked for — and it is not
gated on write scope: reading a result and writing it to disk performs no act
(ADR 0009).

**Comparison marks no winner**, deliberately. Highlighting the best value per row
would be this screen making exactly the choice its own overfitting warning asks a
reader not to make on those numbers alone. It is a GET, so a read-only session can
use it: comparing performs no act (ADR 0009).

**A read-only session cannot queue a run**, and is told so rather than finding out
from a 403. Occupying the shared queue for minutes is an act.

## Serving it

Two ways, and they resolve the API identically on purpose.

| | Command | Port | What runs |
|---|---|---|---|
| Development | `make up`, or `make dev-web` | 5173 | Vite dev server, HMR, source bind-mounted |
| Deployed | `make up-prod` | 8080 | `npm run build` output served by nginx |

**Everything is same-origin.** The dashboard addresses the API with relative
paths — `/api/v1/dashboard/live`, `/ws` — and whatever served the page routes
those through to FastAPI: the dev-server proxy in `vite.config.ts`, and
`infra/docker/web.nginx.conf` in production. The browser only ever sees one
origin, so CORS is not part of the deployment and `API_CORS_ORIGINS` goes unread.

That is not for convenience. It is so the two environments resolve the API the
*same way* rather than merely both working. A dev server talking cross-origin to
:8000 while production talks same-origin hides every CORS and mixed-content
problem until the deploy, and puts the correct value of one variable in two
places that are never exercised together.

**`VITE_API_BASE_URL` and `VITE_WS_URL` are build-time, not runtime.** Vite
inlines `import.meta.env.VITE_*` into the bundle when it is built, so setting
either on the container *serving* a built bundle does nothing at all — the value
compiled in is the one the browser uses. `docker-compose.yml` used to set them on
the `web` service, which worked only because that service runs the dev server.
They reach a build through the build args in `infra/docker/web.Dockerfile`.

Both default to empty, which means "same origin as the page", and that is what
makes one image correct on localhost, on a LAN address and behind a hostname. Set
them only to point at an API on a genuinely different origin — which means taking
on `API_CORS_ORIGINS` and giving up a bundle that travels.

The socket's scheme is derived from the page's rather than hardcoded
(`src/api/origin.ts`): a browser refuses a `ws://` socket from an `https://` page
as mixed content, so a hardcoded scheme is a dashboard that loses every live
update the day it goes behind TLS — while the 5-minute poll keeps working and
hides that it has.

### What nginx does

- Serves `dist/` with an SPA fallback, so a hard refresh on `/positions` returns
  `index.html` rather than a 404.
- Caches fingerprinted `/assets/*` for a year, and `index.html` not at all. A
  cached `index.html` names asset hashes that stop existing at the next deploy,
  which is a blank dashboard that a reload does not fix.
- Proxies `/api/`, `/healthz`, `/readyz` and `/ws`, passing the path through
  unstripped, and forwards the WebSocket upgrade with a read timeout long enough
  that a quiet market does not look like a broken feed. The health probes keep
  their unversioned names, so the pair answers "is the whole chain up" from the
  same browser that is showing the problem: both `502` means the API is not
  running, `200` on `/healthz` with `503` on `/readyz` means it is up and a
  dependency behind it is not, and `/readyz`'s body names which. That is the
  ladder in docs/RUNBOOK.md, "Dashboard shows 502 Bad Gateway" — the one symptom
  this arrangement produces and the one it exists to make legible.
- Resolves the API's address per request via Docker's DNS. nginx otherwise
  resolves an upstream once at startup and caches it forever, and goes on
  proxying to a dead address after the api container restarts on a new one.

### Reaching it from another machine

Every port in `docker-compose.yml` is bound to `127.0.0.1`. One of them is meant
to be moved off it — the dashboard's — and it is the only one that is
configurable:

```bash
ATP_WEB_BIND_ADDR=192.168.1.50   # in .env, then: make up-prod
```

The others are pinned. The API does not need publishing for the dashboard to
work, because nginx reaches it across the compose network; Postgres and Redis
are published only so `make migrate`, `seed` and `backfill` can run from the
host. Putting the dashboard on a LAN therefore does not put an unauthenticated
API, a `atp`/`atp` Postgres, or a passwordless Redis holding the kill-switch
state there with it. That separation is what makes exposing one port defensible.

Three ways to reach it, in descending order of how much they protect you:

| | How | Cost |
|---|---|---|
| **SSH tunnel** | leave the default, `ssh -L 8080:127.0.0.1:8080 you@host` | nothing on the network at all; every viewer needs an SSH account, and it is awkward from a phone |
| **VPN interface** | `ATP_WEB_BIND_ADDR` = a Tailscale (`tailscale ip -4`) or WireGuard address | reachable from anywhere you are on the VPN, encrypted in transit; needs the VPN |
| **LAN interface** | `ATP_WEB_BIND_ADDR` = the address from `ip -4 -brief addr` | simplest; "the LAN" usually includes guest wifi and IoT unless segmented, DHCP can move the address, and it is plain HTTP so the book crosses the wire in clear text |

The VPN route is the one to prefer if you want to look at positions away from
the machine. With Tailscale, `tailscale serve` additionally fronts it with a
real HTTPS certificate — which is what the `wss://` derivation above is waiting
for — and can gate on Tailscale identity, the closest thing to authentication
available before Phase 6.

**Do not use a firewall instead of a bind address.** Docker publishes ports with
rules that are traversed before the chain ufw and firewalld write into, so
`ufw deny 8080` reports itself applied and blocks nothing at all. Restricting a
published Docker port means writing into the `DOCKER-USER` chain. The bind
address is the control that actually holds; a firewall is defence in depth
behind it, never instead of it.

**The address must be a private one.** `make check-bindings` refuses two values,
and the second is the one people actually reach for: `0.0.0.0`, and any publicly
routable address. Looking up "what is my IP" returns the address your *router*
presents to the internet, not your machine's — setting that here asks Docker to
serve the book to anyone who connects. Take the value from `ip -4 -brief addr`
(192.168.x, 10.x) or `tailscale ip -4` (100.x) instead.

The test is `is_global` rather than `is_private`, which matters for the route
recommended above: Tailscale allocates from the shared CGNAT range
100.64.0.0/10, which reports as *not* private while being unroutable from the
internet. An `is_private` check would have refused exactly the option worth
preferring.

CI runs the check before the stack starts, so a compose file that puts any of
this on a wildcard or a public address fails the build.

### It should still not face the public internet

Signing in is required now (below), which is a floor rather than a finish. What
is still absent is everything around it: no rate limit on the login endpoint, no
way to revoke a session before it expires, no TLS of our own, and no secrets
manager. A password is the only thing between a reachable port and the whole
book, and there is nothing slowing down guesses at it but bcrypt.

On a LAN or a VPN that is a reasonable place to be. On a public address it is
not, and `make check-bindings` refuses to start the stack bound to one. The
remaining Phase 6 items are the difference — see docs/SAFETY.md.

## Not built yet

Stated here rather than left to be discovered:

- **All seven tabs are built.** Backtests was the last and the largest; it is
  above. What it unblocked elsewhere is still being built separately:
  `/analytics/live-vs-backtest` is now an endpoint and has no screen — it wants a
  *run picker* rather than a date range, because the choice it turns on is which
  backtest, which is a different shape from the three date-ranged panels the
  analytics page is (docs/ANALYTICS.md). The promotion ratchet on the strategies
  page could now check "a completed backtest on record" and still cannot write
  the audit entry naming a human (ADR 0010).
- **A divergence table needs its labels to be worth rendering.** Whenever the
  comparison does get a screen: the response carries a `comparability` per metric
  and warnings above them, and a table that dropped either would reintroduce
  exactly the misreading they exist to prevent — most often a live Sharpe that
  looks better than the backtest because the two were annualised differently.
- **Strategy parameters cannot be edited per run.** The backtests form queues a
  run with the strategy's configured parameters. Building the editor means
  rendering a form from a JSON Schema, and one that silently dropped the fields it
  could not render would report a result for parameters nobody chose.
- **No screen places an order.** The three tabs that are built are all reads.
  Every write handler across `orders.py` and `positions.py` is still a stub,
  because they place things and there is one path from an intent to a venue
  (rule §1.5, ADR 0005) — the path that also carries the audit events ADR 0010
  is waiting on. The kill switch remains the only acting control in this UI.
- **The signal feed on *this screen* does not survive a restart**, though the
  signals themselves now do. The feed is still a bounded in-memory ring on the
  strategy runner, so a deploy empties what the dashboard shows;
  `persistence.models.SignalRow` gained a writer in #58 and holds the durable
  record, including the refusals. What is left is for this screen to fall back
  to `SignalRepository.recent` when the ring is cold.
- **`/dashboard/health` is a stub.** It would report worker heartbeat, broker
  reachability and the last reconciliation result, none of which the worker
  publishes anywhere. Building it means giving the worker a health key to write.
- **Per-position strategy attribution is absent *from this snapshot*.** The
  underlying gap is closed — `PostgresOrderRepository` stores a real
  `strategy_id` and `signal_id` now that `strategies` and `signals` have writers
  (#58), so an order can be traced to the decision that caused it, and
  `/analytics/attribution?by=strategy` reads that join. What this screen still
  does is name the strategy running the whole book, because the published
  snapshot is built from the runner's live `Portfolio` and a `Position` carries
  no strategy of its own.
- **Sign-in and scopes exist; rate limiting and revocation do not.** Signing in
  is one operator against a bcrypt hash, with the session in an `HttpOnly` cookie
  the WebSocket handshake carries by itself (ADR 0008). Sessions are `full` or
  `read`, chosen at sign-in (ADR 0009): a read-only session reads everything, may
  still hit the kill switch, and is refused every other write with 403. Note what
  that does **not** change on this screen — the kill switch is currently the only
  acting control here, so a read-only session looks almost identical; the badge
  in the nav is there because otherwise the difference would be invisible until
  something was refused. What it changes is what the API permits, so a stolen
  cookie cannot trade. What is *not* built: any rate limit on the login endpoint,
  revocation before a session expires, and any notion of roles — with one account
  there is nothing to distinguish.
