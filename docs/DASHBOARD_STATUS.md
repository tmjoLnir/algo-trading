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
(`OrderStatus`, `SignalAction`, `StrategyKind`, the four backtest statuses in
`atp_core.backtest.ports`, and the five audit verbs in `atp_core.audit.ports`).
So what follows describes **what the front end renders for a given contract**,
and says nothing whatever about whether the server can produce that contract
from a real database. Phase 5's *Verifiable:* line — a book a real worker
published — is still unshown, and nothing here is evidence toward it.

Read this as: *this is the screen*. Not: *this is the system working*.

The screenshots this was written from are not committed. Rendered PNGs of seven
tabs weigh several megabytes, they go stale the first time a component changes,
and git keeps them for good — so the descriptions below carry the findings
instead, and anyone who wants the pictures can point a browser at a running
stack.

---

## The headline

**The one acting control in the entire dashboard is wired to a stub.** The
`HALT TRADING` button posts to `/api/v1/risk/halt`, and that handler is
`raise NotImplementedError` (`apps/api/src/atp_api/routers/risk.py:90`). Nothing
converts that into a 501, so pressing it returns an unhandled 500 and trading
does not stop. `scripts/halt.py` still says it is "the *only* operator path to
the kill switch" — and it still is. No web test covers the button.

Everything else on every tab is a read, and the reads are real.

---

## 1. Dashboard — `/`

Account summary, equity curve, open positions, signal feed and working orders,
from the single `/dashboard/live` aggregate plus `/dashboard/equity-curve`. The
MSFT row is the case the screen is built around: unmarked, so every
mark-dependent field is `—` rather than zero, and the account banner says the
totals understate exposure.

**Outstanding**

- `HALT TRADING` calls a stubbed endpoint (above). This is the whole of the
  tab's write surface.
- No way to clear a halt. `KillSwitchButton.tsx:6` says resuming is "done from
  the risk page" — **there is no risk page**; the nav has seven tabs and none is
  Risk. `/risk/resume` is stubbed too (`risk.py:107`), so clearing is
  `scripts/halt.py clear` only.
- `/dashboard/health` is stubbed (`dashboard.py:567`). Nothing calls it —
  `FeedStatus` reads `data_feed_healthy` off the aggregate instead — so it is a
  dead route rather than a missing feature.
- The book has never been served from a real worker's Redis publication.

## 2. Strategies — `/strategies`

The stored rows against the registered classes, which is the point of the
screen: `donchian_breakout` and `opening_range_breakout` exist in code and have
never run.

**Outstanding**

- **Entirely read-only, and every write endpoint behind it is a stub**: `POST
  /strategies`, `PATCH /strategies/{id}`, `POST /{id}/promote`, `POST
  /{id}/pause`, `GET /{id}`, `GET /strategies/available`
  (`strategies.py:225,238,249,257,275,282`). The page says so and explains why —
  the promotion ratchet's preconditions cannot be checked yet.
- **The lifecycle vocabulary has drifted three ways.** `STRATEGY_STATES` in
  `useStrategies.ts:27` offers `draft, backtest, paper, active, paused`. The
  domain enum `StrategyState` has `draft, backtesting, paper, live, paused,
  halted`. And `StrategyRepository.ensure` writes the literal `"active"`
  (`persistence/strategies.py:65`), which is not a member of that enum at all —
  the column is a plain `String(20)`, so nothing rejects it. Net effect: the
  filter can never match `backtesting`, `live` or `halted`, and `draft` and
  `paused` are unreachable because nothing writes them. Only `active` and
  `paper` can ever appear.
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

**Outstanding**

- **`/analytics/live-vs-backtest/{run_id}` is implemented and has no screen.**
  It is referenced only in a comment (`Backtests.tsx:25`) and in the generated
  schema; nothing in `apps/web/src` calls it. The run picker it needs is this
  page's list.
- No way to cancel or delete a run — by design, per `backtest/ports.py`: arq
  cannot interrupt a running job, so no cancel endpoint exists.

## 4. Positions — `/positions`

The stored book — the copy the worker wrote to Postgres, with its age stated
separately from the tab's own refresh.

**Outstanding**

- No position actions. `GET /positions/{symbol}`, `POST /{symbol}/close` and
  `PATCH /{symbol}/stop` are all stubs (`positions.py:133,139,153`), so closing
  a position or moving a stop is a broker-UI action today.
- No flatten-all. `/risk/flatten-all` is stubbed (`risk.py:127`);
  `docs/RUNBOOK.md` sends you to the broker's own UI.

## 5. Orders — `/orders`

Order history with filters, fill progress per row, and the distinction the table
exists for: `refused by risk` (our own engine) is a different word from
`refused by venue`.

**Outstanding**

- Read-only. `POST /orders`, `DELETE /orders/{id}` and `POST /orders/cancel-all`
  are stubs (`orders.py:213,218,223`) — no manual order, no cancel from the UI.
  `ManualOrderRequest` is in the OpenAPI document with nothing serving it.

## 6. Analytics — `/analytics`

Performance, attribution and closed round trips, each panel failing
independently. Money is decimal strings; the float statistics are separated and
labelled as such.

**Outstanding**

- `/analytics/reports/daily` is a stub (`analytics.py:722`) and has no screen.
  The roadmap explains why it is hard rather than merely undone: rejections live
  in `signals`, halts in the kill switch's records, and feed incidents only in
  the worker's logs — no one query reaches all three.
- No price charts anywhere in the UI. `/market-data/bars/{symbol}`,
  `/quote/{symbol}` and `/search` are stubs (`marketdata.py:34,41,46`), and
  `/market-data/calendar` is implemented with no consumer at all. The equity
  curve is the only chart in the app.
- Everything here has only ever run against fixtures and test databases, never
  against a database holding a real strategy's history.

## 7. Audit — `/audit`

**The screen is the finding.** The audit trail records five verbs and only
five — `login`, `login_failed`, `logout`, `rate_limited`, `forbidden`
(`atp_core/audit/ports.py:82-91`). There is no order-flow, kill-switch or
strategy-lifecycle entry because nothing emits one, which the class docstring
states outright: constants "land with their handlers", and those handlers are
still stubs.

So the tab is an accurate, complete sign-in log — and calling it an *audit
trail* over-promises. "Who halted trading", "who promoted this strategy to live"
and "which order did we send at 14:30" are not answerable here, and ADR 0010's
lifecycle verbs remain unwired.

**Outstanding**

- The four lifecycle/order/risk verb families, each blocked on its own handler.
- `/risk/rejections` is stubbed (`risk.py:138`) — "a strategy blocked on every
  order looks identical to a strategy with no signals" is currently answered
  only by the dashboard's signal feed, and only for the current book.

---

## Endpoint coverage

| Endpoint | Server | Screen |
|---|---|---|
| `/auth/{login,logout,me,context}` | built | Login, nav |
| `/dashboard/live`, `/equity-curve` | built | Dashboard |
| `/dashboard/health` | **stub** | none (unused) |
| `/positions` | built | Positions |
| `/positions/{symbol}`, `/close`, `/stop` | **stub** | none |
| `/orders` (GET) | built | Orders |
| `/orders` (POST), `/{id}` (DELETE), `/cancel-all` | **stub** | none |
| `/strategies` (GET) | built | Strategies |
| `/strategies` writes, `/available`, `/{id}` | **stub** | none |
| `/analytics/{performance,trades,attribution}` | built | Analytics |
| `/analytics/live-vs-backtest/{run_id}` | built | **none** |
| `/analytics/reports/daily` | **stub** | none |
| `/backtests` + `/compare` + `/{id}/*` | built | Backtests |
| `/audit` | built | Audit |
| `/market-data/calendar` | built | **none** |
| `/market-data/{bars,quote,search}` | **stub** | none |
| `/risk/halt` | **stub** | **Dashboard button calls it** |
| `/risk/{resume,flatten-all,limits,status,rejections}` | **stub** | none |

## Cross-cutting

- **One live write path, and it is broken.** Of three mutations in the whole app
  — halt, queue a backtest, log in/out — only the kill switch touches trading,
  and its endpoint is a stub. A reader of this UI cannot stop trading from it.
- **Two built endpoints have no reader** (`live-vs-backtest`,
  `market-data/calendar`), and one screen control points at a page that does not
  exist (the "risk page" in `KillSwitchButton.tsx`).
- **Nothing here has met real data.** Every test drives fixtures or an ASGI
  transport; these screenshots do the same. The gap between "the screen renders
  this correctly" and "the screen agrees with what the worker holds" is exactly
  Phase 5's unshown *Verifiable:* line.
- No roadmap tick is warranted by this document. Phase 5's dashboard item is
  already unticked for the reason above, and the ticked "Redis kill switch" item
  (#32) claims only the core mechanism, which does work — it never claimed the
  HTTP endpoint or the button.
