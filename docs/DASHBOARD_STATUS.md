# Dashboard — what each tab does, and what is still missing

An audit of every tab in `apps/web`: what the screen shows, and what remains
unbuilt behind it. Scope is the dashboard and the endpoints it consumes; the
rest of the platform's outstanding work is tracked in `docs/ROADMAP.md`.

## What this was checked against

Each tab was rendered in a browser — `vite` serving `apps/web`, driven with
Playwright — and read. **The API behind it was a fixture server, not the real
one**: there is no Docker daemon in the environment this was done in, so
`make up` could not run.

That bounds what the reading is worth. Every fixture response was shaped from
`apps/web/src/api/schema.d.ts` — the types generated from the app's own OpenAPI
document — and every enum value checked against the domain rather than guessed
(`OrderStatus`, `SignalAction`, `StrategyState`, the four backtest statuses in
`atp_core.backtest.ports`, and the twelve audit verbs in `atp_core.audit.ports`).
So what follows describes **what the front end renders for a given contract**,
and says nothing whatever about whether the server can produce that contract
from a real database. Phase 5's *Verifiable:* line — a book a real worker
published — is still unshown, and nothing here is evidence toward it.

Read this as: *this is the screen*. Not: *this is the system working*.

The screenshots this was written from are not committed. Rendered PNGs of the
seven tabs that existed then weigh several megabytes, they go stale the first
time a component changes, and git keeps them for good — so the descriptions
below carry the findings instead, and anyone who wants the pictures can point a
browser at a running stack.

---

## The headline

**The one acting control in the dashboard now works.** It did not when this
document was first written: `HALT TRADING` posted to `/api/v1/risk/halt`, which
was `raise NotImplementedError`, and nothing converted that into a 501 — so the
button returned an unhandled 500 and trading did not stop. The endpoint is
implemented, covered by tests, and writes an audit row (#70). `scripts/halt.py`
is no longer the only operator path to the kill switch, though it remains the
one that works when the API does not.

**Clearing a halt is a screen now too.** It was command-line only when this was
written: `/risk/resume` was a stub demanding a step-up password no screen asked
for, which left the asymmetry docs/RISK.md wants — stopping reflexive,
restarting deliberate — enforced by what existed rather than by design. The
endpoint is implemented and the halt banner carries a `Resume…` control per halt
(#75), so the asymmetry is now enforced where it was meant to be: the halt
button asks nothing, the resume asks for the account password, and a read-only
session may press one and not the other. `scripts/halt.py clear` remains the
path that works when the API does not.

**The resume control has never been rendered against a real API**, and neither
had the halt button when this document was first written. It is covered by
component tests against a stubbed `fetch` and by handler tests against a fake
switch; nothing has cleared a halt in a real Redis from a real browser.

Everything else on every tab is a read, and the reads are real.

---

## 1. Dashboard — `/`

Account summary, equity curve, open positions, signal feed and working orders,
from the single `/dashboard/live` aggregate plus `/dashboard/equity-curve`. The
MSFT row is the case the screen is built around: unmarked, so every
mark-dependent field is `—` rather than zero, and the account banner says the
totals understate exposure.

**Outstanding**

- `HALT TRADING` is the whole of *this tab's* write surface. It stopped being the
  whole of the app's when the Config tab arrived (ADR 0023, then ADR 0025) — see
  §8 and the cross-cutting note below. It works now (#70), including the 503 it answers when the switch
  cannot be written — a state that is neither "stopped" nor "trading", because
  the switch fails closed but records nothing, so trading resumes when the store
  recovers. The button renders that message rather than swallowing it.
- Clearing a halt is on the halt banner (#75), not on this tab's own controls:
  one `Resume…` per halt, because a halt is keyed on (scope, target) and a
  single button could only ever clear one of the halts on screen while looking
  like it cleared the lot. The nav has eight tabs and none is Risk, which
  is not where the risk reads ended up. `/risk/status` and `/risk/limits` are
  built and read by a panel on the **Strategies** tab (#75) rather than by a
  tab of their own — `/status` is what a person checks before promoting a
  strategy, so it sits on the screen that decision is made on and no eighth tab
  was added for it. (The eighth is Config, which arrived later and for another
  reason — ADR 0023, then ADR 0025.) `/risk/rejections` joined them there
  (#77), reading `signals` rather than `orders` for the reason below.
- `/dashboard/health` is stubbed (`dashboard.py:567`). Nothing calls it —
  `FeedStatus` reads `data_feed_healthy` off the aggregate instead — so it is a
  dead route rather than a missing feature.
- The book has never been served from a real worker's Redis publication.

## 2. Strategies — `/strategies`

The stored rows against the registered classes, which is the point of the
screen: `donchian_breakout` and `opening_range_breakout` exist in code and have
never run.

**The risk limits panel sits above them** (#75), from `/risk/status`: each
account-wide ceiling with the book's current standing against it, the rule name
that would refuse, and a word — `at limit`, `ok`, `not observable`, `no reading`
— beside every bar, because colour alone is not a signal every reader has. It
is on this tab rather than a Risk tab because it is the pre-promotion check and
this is the pre-promotion screen.

Three states that deliberately do not look alike: a published book gives real
readings; **no published book gives the ceilings with every reading `—` and no
bar drawn at all**, since an empty bar and a bar at zero are the same picture
and one of them means nobody knows what the book holds; and a `/risk/status`
that fails falls back to `/risk/limits`, so the ceilings still render with the
reason for the missing readings stated above them. **That fallback is narrower
than it was.** It read `Settings` and touched no store when this was written;
since ADR 0025 the ceilings are a `worker_config` row, so `/risk/limits` needs
Postgres too — it survives a broker or Redis outage, not a database one.

**Outstanding**

- **Still entirely read-only as a screen**, though the API is no longer:
  `POST /strategies` is built and stores a strategy at `draft`. What is missing
  here is the form — nothing on this tab posts one, so a rule set is authored
  from a client. `PATCH /strategies/{id}`, `POST /{id}/promote`, `POST
  /{id}/pause`, `GET /{id}` and `GET /strategies/available` remain stubs, the
  first three because the promotion ratchet's preconditions still cannot be
  checked and the last two because the list above already carries what they
  would serve.
- **A row no longer implies a worker ran it.** `has_run` on a registered class
  and `last_started_at` on a stored row both read the same two facts — a row
  exists, and `updated_at` — and an authored row has both from the moment it is
  created. That was already true of every row `scripts/seed.py` writes; creation
  makes it ordinary. Telling the two apart needs a `last_started_at` column that
  only `ensure` bumps; docs/ROADMAP.md carries it.
- ~~**The lifecycle vocabulary has drifted three ways.**~~ **Fixed (#76.)**
  `STRATEGY_STATES` offered `draft, backtest, paper, active, paused`; the domain
  enum `StrategyState` had `draft, backtesting, paper, live, paused, halted`;
  and `StrategyRepository.ensure` wrote the literal `"active"`, which was not a
  member of that enum at all, into a plain `String(20)` nothing checked.

  This audit understated it. `ensure` is the **only** writer of a state value in
  the platform — the strategy write endpoints are all stubs — so `active` was
  not one possible value among several, it was the only value any row could ever
  hold. Of the filter's five options, four could not match by construction and
  the fifth matched a state the domain did not recognise.

  Now: `ensure` writes `draft`, the ratchet's first rung; a CHECK constraint
  refuses anything outside the enum (migration `e2b6d1a70f93`, which rewrites
  the existing rows first); the API's `state` filter is the enum, so a typo is a
  422 rather than an empty 200; and the screen's filter and tint map are both
  `Record`s over the generated union, so a rung added on the server fails `tsc`
  here until somebody labels it.
- Stored descriptions are always blank — `ensure` writes `description=""` and
  no write path exists to fill it, so the description column renders empty for
  every row a worker created. The registry descriptions beneath it are the only
  ones with text.

## 3. Backtests — `/backtests`

The one screen that starts work. Queue form, run list with live progress, and
the four real statuses — `queued`, `running`, `done`, `failed`. The failed row
carries the server's own `backfill_bars.py` command verbatim.

Opening a run gives metrics, equity curve and per-trade inspection.

Ticking two finished runs compares them, with the overfitting warning above the
table and deliberately no "winner" column.

The picker offers strategies that have a `strategies` row, which used to mean
"that a trading worker has booted". On a clean database that was nothing, so the
form had nothing to offer and the endpoint answered 409 — a screen whose one
action was unreachable until you configured a worker with broker credentials the
backtest does not use. `make seed` writes those rows now, so a migrated database
plus backfilled bars is the whole prerequisite.

**Outstanding**

- ~~**`/analytics/live-vs-backtest/{run_id}` is implemented and has no
  screen.**~~ **Built**, on the Analytics tab rather than this one: it is a
  report about live performance, and the run picker it needs is a hook rather
  than a page. This list is still where a run id comes from.
- No way to cancel or delete a run — by design, per `backtest/ports.py`: arq
  cannot interrupt a running job, so no cancel endpoint exists.

## 4. Positions — `/positions`

The stored book — the copy the worker wrote to Postgres, with its age stated
separately from the tab's own. Both count up on their own clock, and the book's
is shown as its age now rather than when it was read (ADR 0022).

**Outstanding**

- No position actions **on the screen**. `POST /{symbol}/close` is built and
  goes through the risk chain, but nothing renders a control for it, so closing
  a position is a `curl` or a broker-UI action today. `GET /positions/{symbol}`
  and `PATCH /{symbol}/stop` are still stubs (`positions.py`), the second
  deliberately: replacing a stop is two orders, and a half-built version leaves
  positions unprotected in a way the operator cannot see.
- Flatten-all is built, and still has no control. `/risk/flatten-all` cancels
  resting orders and closes the book, behind the confirmation phrase and a
  step-up password; `docs/RUNBOOK.md` carries the `curl`. A button for it is a
  deliberate open question rather than an oversight — the one control on the
  dashboard today is HALT, and putting an irreversible liquidation next to it is
  a design decision nobody has made.

## 5. Orders — `/orders`

Order history with filters, fill progress per row, and the distinction the table
exists for: `refused by risk` (our own engine) is a different word from
`refused by venue`.

**It had never rendered a `rejected_risk` row, and could not have** (fixed in
#78). This endpoint's docstring has always said the orders that matter most are
the ones that never filled, and that "a rejection appears in no other read in
the platform"; `OrderHistoryTable` tints `rejected_risk` and shows the reason
beside it. The whole read path was built and complete. Nothing ever wrote one —
the runner dropped a refused order at all four places it can be refused, so the
table's most important category of row could not exist. The write path exists
now.

**A refused row now names its refuser as well as its reason** (#79). The row #78
finally produced still could not say *which* rule refused it: `RiskDecision`
carries `rule` beside `reason` and the router passed only the reason into
`transition()`, so the rule reached the structured log and never the table. A
reason alone does not identify a limit — three separate rules refuse with "no
price available for SPY" — and the rule name is the one string that gets a
reader from a refusal to the ceiling that predicted it, which is exactly the
cross-reference the risk limits panel is laid out for. `orders.rejected_by`
(migration `b8e3f01c7d24`) holds a rule name, the pre-rule stage `routing`, or
the broker when the venue was the refuser; `status` says which of the two
vocabularies to read it in, which keeps refusals countable by rule while one
column answers "who refused this order".

Unlike `signals.rejected_by`, **it could not be backfilled**. There the rule had
been packed into the reason as `"[rule] reason"` and the migration parsed it
back out. An order's reason never carried it, so every refusal stored before the
column says its refuser was not recorded — permanently, not until some
straggling writer catches up. The table states that rather than leaving the line
blank, the same admission `purpose` makes on rows that predate its own column.

**Outstanding**

- Read-only **on the screen**. `DELETE /orders/{id}` and
  `POST /orders/cancel-all` are built — cancelling withdraws intent and consults
  no risk rule, which is why they landed ahead of `POST /orders` — but the table
  renders no control for either, so a cancel is a `curl` today.
- `POST /orders` is still a stub, and `ManualOrderRequest` is still in the
  OpenAPI document with nothing serving it. The wiring is no longer what is
  missing: `atp_api.execution` assembles the router and
  `POST /positions/{symbol}/close` places orders through it. What is missing is
  a manual order's own decisions — what a hand-typed quantity is sized against,
  and what a stop attached to it means when no strategy owns the position.

## 6. Analytics — `/analytics`

Performance, attribution and closed round trips, each panel failing
independently. Money is decimal strings; the float statistics are separated and
labelled as such.

**Outstanding**

- `/analytics/reports/daily` is built and has **no screen**. It is computed on
  demand from the durable records rather than stored, so a panel would be a
  fetch and a table. What it cannot answer it reports as absent rather than as
  zero — feed incidents have no table behind them — and a screen for it has to
  render that third state distinctly or it will be read as "none happened".
- No price charts anywhere in the UI. `/market-data/bars/{symbol}`,
  `/quote/{symbol}` and `/search` are stubs (`marketdata.py:34,41,46`), and
  `/market-data/calendar` is implemented with no consumer at all. The equity
  curve is the only chart in the app.
- Everything here has only ever run against fixtures and test databases, never
  against a database holding a real strategy's history.

## 7. Audit — `/audit`

**The screen was very nearly the finding.** The audit trail records twelve
verbs: five about who was signed in — `login`, `login_failed`, `logout`,
`rate_limited`, `forbidden` — two about the kill switch, `halt_engaged` (#70)
and `halt_cleared` (#75), one about authoring, `strategy_created`, three about
closing out — `order_cancelled`, `position_closed` and `flatten_all` — and one
about configuration, `worker_config_updated` (#124), which also carries every
risk-ceiling change since ADR 0025. Each
arrived with the endpoint that emits it, because the class docstring's rule is
that a constant for an event nothing emits is a claim the record does not
support. `POST /orders` and strategy promotion are still stubs, so there is
still no verb for either.

So the tab is a sign-in log with the kill switch's two events and the close-out
verbs in it. "Who stopped trading", "who started it again" and "who liquidated
the book" are all answerable — and only for the API's own actions:
`scripts/halt.py` has no session to attribute a row to, and
the automated triggers inside the risk layer announce themselves through alerts
and logs instead. An absent row means "not halted *from the dashboard*", never
"not halted". "Who promoted this strategy to live" and "which order did we send
at 14:30" remain unanswerable, and ADR 0010's lifecycle verbs remain unwired.

`forbidden` also covers a failed step-up now, carrying `step_up_failed` in its
detail (#75). It had always claimed to — ADR 0009 says so and the constant's own
docstring says so — but only `require_write_scope` ever wrote one, so a wrong
password against `/resume` or `/flatten-all` left no trace at all. That was the
gap worth closing: `rate_limited` counts attempts at the *login form*, so a
stolen cookie being used to guess at the step-up was invisible to every part of
the record.

**Outstanding**

- The order-flow and strategy-lifecycle verb families, each blocked on its own
  handler. Clearing a halt was the nearest one and has landed; the rest wait on
  `/orders`, `/positions/{symbol}/close` and the strategy writes.
- The halts this log cannot see — the CLI's and the risk layer's own. Attributing
  those means giving each an identity the record can stand behind, which is a
  larger question than adding a constant.
- ~~`/risk/rejections` is stubbed~~ **Built (#77)**, on the Strategies tab. "A
  strategy blocked on every order looks identical to a strategy with no
  signals" was previously answerable only from the dashboard's signal feed, and
  only for the current book; it is now a durable, filterable query.

  **It surfaced a gap, and #78 closed it.** The runner records a row for every
  *signal* whatever its fate, which is what makes this endpoint possible. Its
  other refusal paths recorded nothing: a **stop exit**, a **protective stop**
  and a **shutdown flatten** the risk chain denied were logged and dropped —
  none is a signal, so `_record_signal` never saw them, and none of their orders
  was ever tracked, so `_persist` never saved them either.

  Those are the *worse* refusals: a refused entry is a trade that did not
  happen, a refused stop exit is a position that should have closed and did not,
  and a refused protective stop is one that never had a stop at all — the two
  ends of docs/SAFETY.md's layer 5. They are stored as rejected orders now, so
  they appear on the **Orders** tab. The blind-spot list this endpoint sends is
  consequently a signpost rather than an apology: it still has to say that an
  empty table here does not mean nothing was refused, but it can now say where
  the rest is.

---

## 8. Config — `/worker`

**Read from source, not rendered.** Every section above describes a screen that
was driven in a browser against a fixture server; this one was not. The tab did
not exist when that reading was done — it arrived with ADR 0023 (#124) and was
renamed from Worker to Config by ADR 0025 (#132) — and the constraint that
stopped the original reading using a real API has not lifted. What follows is
read off `apps/web/src/pages/Worker.tsx`, `components/WorkerConfigPanel.tsx`
and `apps/api/src/atp_api/routers/worker.py`, so it says what the code builds
and claims nothing about what a browser shows. Treat it as a weaker warrant
than §§1–7, not an equal one.

The screen is one panel over one endpoint. `GET /worker/config` returns the
whole of it in a single query — the saved row, the running worker's report of
what it booted with, and the option lists the form renders from — so the form
cannot show a strategy the server would reject.

- **Two halves, one save.** The worker settings (watchlist, strategy and its
  parameters, sizing, protective stop, feed watchdog) and the eight account-wide
  risk ceilings are edited together and written by one `PUT`, producing one
  revision and one audit row. That is the ADR 0025 decision showing through: a
  tightened position limit and a widened stop are one operator decision and are
  recorded as one.
- **The risk section is rendered from the server's field list**, not from a list
  the front end keeps — so a ceiling added to `RiskLimits` appears on the screen
  without a front-end change, and cannot appear with a different label than the
  API validates against.
- **One field asks for a password.** `allow_live_orders` is the third of the
  three live locks (CLAUDE.md §1.8). Ticking it reveals a password box and the
  save carries the operator's password; the ceilings and the worker settings do
  not ask, and turning the lock back off does not either. A read-only session is
  refused before the handler runs.
- **Saved is not running.** The worker binds its configuration at start, so the
  panel renders the saved revision against the revision the running process
  reported and says a restart is owed when they differ. A worker that booted
  before anything was ever saved reports a distinct sentinel rather than
  revision 1, so "nothing is stored" and "stored, and it happens to equal the
  defaults" are not shown as the same state.
- **Nothing is *stored* for the worker half.** `WorkerConfig` does carry defaults —
  `risk_pct` sizing at 1%, a 2×ATR stop, a 60-second feed watchdog — but the
  watchlist and strategy default to empty, so a fresh install trades nothing
  until somebody opens this tab, which is the same posture an empty
  `WORKER_STRATEGY` always had. The migration does backfill the eight ceilings
  with the values `.env.example` used to ship.
- **This screen has never been driven against a real API**, and nor has any
  other. It is covered by component tests against a stubbed `fetch`
  (`components/workerconfig.test.tsx`) and by handler tests
  (`tests/unit/test_worker_config_api.py`).

---

## Endpoint coverage

| Endpoint | Server | Screen |
|---|---|---|
| `/auth/{login,logout,me,context}` | built | Login, nav |
| `/dashboard/live`, `/equity-curve` | built | Dashboard |
| `/dashboard/health` | **stub** | none (unused) |
| `/positions` | built | Positions |
| `/positions/{symbol}/close` | built | none |
| `/positions/{symbol}`, `/{symbol}/stop` | **stub** | none |
| `/orders` (GET) | built | Orders |
| `/orders/{id}` (DELETE), `/cancel-all` | built | none |
| `/orders` (POST) | **stub** | none |
| `/strategies` (GET) | built | Strategies |
| `/strategies` (POST) | built | none — no authoring form yet |
| `/strategies` other writes, `/available`, `/{id}` | **stub** | none |
| `/analytics/{performance,trades,attribution}` | built | Analytics |
| `/analytics/live-vs-backtest/{run_id}` | built | Analytics |
| `/analytics/reports/daily` | built | none |
| `/backtests` + `/compare` + `/{id}/*` | built | Backtests |
| `/audit` | built | Audit |
| `/market-data/calendar` | built | **none** |
| `/market-data/{bars,quote,search}` | **stub** | none |
| `/risk/halt` | built (#70) | Kill switch, on the pinned tab bar (#131) |
| `/risk/resume` | built (#75) | Halt banner |
| `/risk/limits`, `/risk/status` | built (#75) | Strategies (read) |
| `/risk/rejections` | built (#77) | Strategies |
| `/risk/flatten-all` | built | none |
| `/worker/config` (GET) | built (#124) | Config — one query: saved row, running worker's report, options |
| `/worker/config` (PUT) | built (#124, #132) | Config — **the app's one write into trading** |

## Cross-cutting

- **Three live write paths now, and the third is the widest.** Of five mutations
  in the whole app — halt, resume, save the configuration, queue a backtest,
  log in/out — the kill switch touches trading in both directions (#70, #75),
  and `PUT /worker/config` decides what trading *is*: the watchlist, the
  strategy, the sizing, the stop, the eight account-wide ceilings (#132) and
  `allow_live_orders`, which is the third of the three live locks. Arming that
  one costs the operator's password and is refused to a read-only session; the
  rest of the row is an ordinary save.

  The narrower claim this bullet used to make still holds, and is worth keeping
  separate from the one above: nothing the browser renders can place an order,
  close a position, or move a stop. It can decide what the worker will do, and
  it cannot do it itself.

  It is no longer a statement about the *API*. Closing one position, cancelling
  orders and flattening the book are all built and all reachable by `curl`, so
  the asymmetry has moved rather than gone: the limits panel shows a breach it
  offers no control to act on beyond halting, while the controls that would act
  on it exist one layer down with no screen in front of them.
- **One built endpoint has no reader** (`market-data/calendar`), and has been
  reader-less for phases. `live-vs-backtest` was the other and now has the
  Analytics tab's fourth panel. `/risk/limits` is a
  third only in the ordinary case: it is fetched by the Strategies panel solely
  when `/risk/status` has failed, which is the case that route exists for.
- **The Strategies tab is now three panels**: the risk limits, the refused
  decisions, and the strategies themselves. That is deliberate rather than
  accretion — all three answer one question, "why is this strategy not
  trading", from the three places the answer can be: a limit it is against, a
  refusal it has already hit, or nothing having run it at all.
- **Nothing here has met real data.** Every test drives fixtures or an ASGI
  transport, and so did the reading behind this document. The gap between "the
  screen renders this correctly" and "the screen agrees with what the worker
  holds" is exactly Phase 5's unshown *Verifiable:* line.
- No roadmap tick is warranted by this document, and none by the resume work
  either — it is fakes and an ASGI transport, and Phase 5's line asks for a
  browser agreeing with a running worker. Phase 5's dashboard item is
  already unticked for the reason above, and the ticked "Redis kill switch" item
  (#32) claims only the core mechanism, which does work — it never claimed the
  HTTP endpoint or the button.
