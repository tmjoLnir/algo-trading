# The API — REST and WebSocket surface, and the conventions that hold across it

What `apps/api` serves, what it refuses, and the handful of rules that apply to
every route rather than to one. Read this before writing a client or adding an
endpoint.

**This page is not a reference for individual endpoints.** The API's own
OpenAPI document is, it is generated from the code rather than written beside
it, and it is served at `/docs` on any running instance and dumped to a file by
`make gen-types`. A hand-written parameter table would be a second source of
truth that drifts, which is the failure this page exists to describe rather than
to join.

What a generated schema cannot tell you is which of those routes actually work,
what a `"100.50"` in a response means, or why a 403 arrives when you expected a
401. That is what follows.

---

## 1. The shape of it

The API is deliberately thin: validate input, call into `atp_core`, serialise
the result. No trading logic lives here (`apps/api/src/atp_api/main.py`). If you
find yourself computing a position size in a router, it belongs in core — where
it is testable without HTTP and shared with the backtest path.

**The API does not place orders.** There is exactly one execution path —
`execution.router.OrderRouter.submit()`, in the worker — and adding a second
from here is CLAUDE.md §1.5's blocking review comment. What this process does is
record an intention the worker acts on. Two consequences you will meet:

- `PUT /worker/config` writes a row; the worker reads it. The API never reaches
  a broker on that path at all (ADR 0023).
- `POST /positions/{symbol}/close` does reach one, through the same
  `OrderRouter` core exposes — not around it. Its risk chain is built per
  request, so a ceiling saved a moment ago binds it (`deps.get_effective_risk_limits`).

**Read models are read-only here.** The worker is the only writer of the book,
of orders, of signals and of the live snapshot (ADR 0007), so "what do we hold"
has one answer. The three narrow exceptions are stated where they are made:
`backtest_runs` on create, `strategies` on create, and the worker-config row.

---

## 2. Versioning, and the four routes that have none

Business routes are under `/api/v1`. Four are deliberately unversioned:

| Route | Why it is not versioned |
|---|---|
| `GET /healthz` | Liveness. The Docker `HEALTHCHECK` and compose `depends_on` gates hit it |
| `GET /readyz` | Readiness. A load balancer hits it |
| `GET /metrics` | A Prometheus scrape target (ADR 0013) |
| `WS /ws` | The dashboard's socket |

A probe URL that moves when the API version bumps is a probe that fails for no
real reason, and re-pointing an orchestrator is not a migration anybody planned
for. `GET /` is also unversioned and is excluded from the schema; it returns the
name, version and **run mode**, which is what lets the dashboard paint an
unmissable banner before anyone has signed in.

---

## 3. Money on the wire is a string

**Every quantity and every price is a JSON string, never a JSON number.**

```json
{ "symbol": "AAPL", "qty": "100", "avg_price": "182.4150", "unrealized_pnl": "-31.20" }
```

This is CLAUDE.md §1.1 reaching the wire. Money is `decimal.Decimal` throughout
the platform because binary floats cannot represent `0.1`, and Pydantic v2
serialises a `Decimal` to a string in JSON mode precisely so that the value
survives the trip. A client that does `parseFloat(position.qty)` has undone the
guarantee at the last hop and reintroduced the bug the whole ledger is built to
avoid — quietly, because the first few trades round correctly.

Use a decimal type on the client, or keep the string and let a formatter handle
display. The dashboard's generated types say `string` for these fields, which is
the compiler telling you the same thing.

**Requests are more forgiving than responses.** A body field that is a `Decimal`
accepts a number *or* a string (`ManualOrderRequest.qty` is `number | string`),
because a client that sends `100` means one hundred and refusing it would be
pedantry. Responses are always strings.

**Statistics are ordinary numbers,** and that is not an inconsistency. Sharpe,
correlations, `holding_period_hours` — nothing accumulates a balance in them, so
§1.1 exempts them explicitly and a float is the honest type. The rule is about
ledgers, not about arithmetic.

**Timestamps are timezone-aware UTC**, ISO-8601, and every field naming one ends
in `_at` or `_ts`. Naive datetimes are rejected at the domain boundary; convert
to exchange-local time for display only.

---

## 4. Authentication

One operator, one bcrypt hash, one signed cookie. The whole design is
[ADR 0008](adr/0008-one-operator-and-a-session-cookie.md); there is no users
table, deliberately.

```http
POST /api/v1/auth/login
{"username": "operator", "password": "…", "read_only": false}
→ 200  {"user": "operator", "scope": "full"}
   Set-Cookie: atp_session=…; HttpOnly; SameSite=Strict; Path=/
```

The cookie is `atp_session`, `HttpOnly`, `SameSite=Strict`, and `Secure` when
the request arrived over TLS — decided from `X-Forwarded-Proto`, because nginx
terminates TLS and forwards plain HTTP, so this process's own `request.url.scheme`
would say `http` behind a perfectly good certificate. It expires after
`API_SESSION_HOURS` (default 12).

**Why a cookie and not a bearer token.** A browser cannot set `Authorization` on
a WebSocket handshake. Bearer would therefore force the token into a query
string — where nginx writes it to the access log in plain text on every
reconnect — or into an abuse of `Sec-WebSocket-Protocol`. A cookie is sent on
the handshake by itself, and `HttpOnly` puts it beyond reach of any script on
the page, which `localStorage` cannot do.

Everything not on a short allow-list requires that cookie. The list is
`health`, `auth`, `metrics` and `ws`, and it is stated as an allow-list on
purpose: a router added to the application and to nothing else comes out
**authenticated**, which is the safe direction for the mistake.
`tests/unit/test_api_contract.py` holds the same line from outside, against the
generated schema, so neither the list nor the test can drift alone.

### Scopes: what a session may do, not who holds it

`scope` is `read` or `full`, chosen at sign-in via `read_only` and carried in
the **signed** payload — a scope the browser could edit would be a suggestion,
and the point of a read-only session is that its holder cannot decide to stop
being one.

A read-only session may call any safe method (`GET`, `HEAD`, `OPTIONS`) and
exactly one mutating route:

> **`POST /api/v1/risk/halt`.** Stopping trading is not an exception grudgingly
> made. `docs/RISK.md` says engaging needs no confirmation because "hesitation is
> the expensive part", and someone watching the book from a phone is exactly the
> case where the ability to stop matters most and the ability to place an order
> matters least. **Clearing a halt is deliberately not on that list** — stopping
> is reflexive, restarting is a decision (ADR 0009).

Anything else mutating from a read-only session is **403 `this session is
read-only`**, and the attempt is written to the audit trail before it is
refused. 403 and not 401: the caller is authenticated and the credential is
fine, so re-presenting it would change nothing, and a 401 would send the
dashboard to a login screen to solve a problem logging in cannot solve.

The check is made once from the request's method and path
(`deps.require_write_scope`) rather than route by route, for the reason every
cross-cutting rule here is: a rule applied per handler is a rule someone adds a
handler without.

### Step-up: three acts that ask for the password again

A session cookie proves somebody logged in within the last twelve hours. It does
not prove anybody is at the keyboard now. Three acts require the password to
travel with the call:

| Act | Route |
|---|---|
| Clear a halt | `POST /api/v1/risk/resume` |
| Liquidate the book | `POST /api/v1/risk/flatten-all` |
| **Arm** `allow_live_orders` | `PUT /api/v1/worker/config` |

Each is irreversible or puts money at risk. Note the asymmetry in the third:
arming asks, and turning it off asks for nothing — stopping is never the harder
direction. A save that leaves the lock where it already was asks for nothing
either, and the demand is made regardless of run mode, because the value
persists and a paper platform later switched to live would otherwise arrive
already armed by a save nobody proved.

**There is deliberately no elevation window.** A "recently authenticated" period
of a few minutes is a few minutes during which a walked-away laptop can flatten
the book, which is the precise situation this exists to prevent. A failed
step-up is 403 `password required for this action` and is recorded as
`forbidden` with `reason: step_up_failed` — distinguishable on the audit screen
from a read-only session's refusal, which shares the verb. One is a session in
the wrong mode; the other is a credential that did not check out.

`flatten-all` wants **two** proofs, not either:

```http
POST /api/v1/risk/flatten-all
{"confirm": "FLATTEN ALL POSITIONS", "password": "…"}
```

The phrase shows you know what this does; the password shows you are entitled to
do it. A wrong phrase is refused with `the book is untouched` in the message,
because the one thing a caller needs to know after a refused flatten is whether
anything happened.

---

## 5. Errors

Every error body is FastAPI's envelope — one key:

```json
{ "detail": "this session is read-only" }
```

Validation failures are the exception and are FastAPI's own `422` shape, a list
of per-field errors under the same key.

| Status | Means | Notes |
|---|---|---|
| `401` | No session, expired, or a bad signature | **One message for all three.** Distinguishing them tells a prober which half they solved; the client's response is identical — log in again |
| `403` | Authenticated, not permitted | Read-only session on a write, or a failed step-up. Never retry by logging in |
| `422` | The request body or query did not validate | |
| `429` | Too many sign-in attempts | Carries `Retry-After`. Only `/auth/login` can produce it |
| `500` | A bug — **or an unimplemented route** | See §7 |
| `502` | The venue answered, and not with success | Only `flatten-all` raises it, and it means **some positions may still be open**. Read the book from the broker before retrying |
| `503` | A dependency is unreachable, or this deployment has none | |

**`503` is the one worth reading carefully**, because it is three different
sentences:

- **`the database is unreachable`** — with `Retry-After: 5`. Registered once as
  an exception handler rather than caught per router. FastAPI's default for an
  unhandled exception is 500, and for an unreachable Postgres that is a false
  confession: the API is fine, and 500 says the opposite to the one person
  trying to work out which of the two to restart. The body deliberately carries
  no exception text — a driver's connection error is free to quote the DSN it
  failed to connect with, and the DSN carries the password (CLAUDE.md §1.6). The
  reason goes to the log, where a shell can read it and a browser cannot.
- **`ALPACA_API_KEY is unset…`** — from a route that reaches for a broker in a
  deployment that has none. Configuration, not a bug; every route that does not
  need a venue keeps working.
- **`… is not available — the API started without one`** — the application was
  built without its lifespan having run. Normal in a unit test driving the app
  over ASGI, a misconfiguration anywhere else.

There is no `404` for an absent session — that is the `401` above, and always
the same one.

---

## 6. Rate limiting, correlation ids, CORS

**Rate limiting is scoped to sign-in and nothing else.** `POST /auth/login`
allows `API_LOGIN_ATTEMPTS` (default 10) per `API_LOGIN_WINDOW_SECONDS` (default
300) per client address, counting *attempts* rather than failures — so the guess
that happens to be right is refused too once the limit is reached — and keyed on
the address rather than the username, because counting per username lets anyone
who knows the operator's name lock them out of their own platform.

That narrowness is a decision, not an omission (ADR 0010). The threat is
somebody guessing the one password, which happens before any session exists.
Throttling the authenticated surface would be defending against an operator
abusing their own platform, at the cost of a limit that misfires on the
dashboard's own reads — which since ADR 0022 arrive in bursts of several
endpoints at once, from however many windows are open.

> **`/risk/halt` must never be rate limited**, whatever is added later. It is the
> one endpoint whose purpose is to work in the worst moment, and a limiter that
> refuses a halt has chosen the wrong thing to protect.

The limiter **fails open**: an unreachable Redis allows the attempt and logs
`CRITICAL`. Failing closed would lock an operator out of their own platform
during an outage, and the degraded state is not "no protection" but "bcrypt
alone" — a cost-12 hash is roughly a quarter-second per guess.

**Correlation id.** Every response carries `X-Request-ID`, echoing the request's
own if it sent one and minting one otherwise, and every log line the request
writes is bound to it. An inbound value is sanitised rather than trusted — it is
about to appear on every log line, and a caller-supplied newline under the
console renderer writes its own. The middleware is outermost, so a request
refused by authentication or by CORS still gets an id and is still counted.

**CORS** allows `API_CORS_ORIGINS` (default `http://localhost:5173`, the Vite
dev server) with credentials. In the deployed arrangement the dashboard is
same-origin behind nginx, so CORS matters in development and not in production.

**Metrics authenticate differently.** `GET /metrics` accepts either a
`Authorization: Bearer <METRICS_TOKEN>` header or a valid session cookie — a
scraper holds a token and cannot hold a cookie; an operator holds a cookie and
should not have to find a token to look at a number. Both absent is the same 401
either way, so neither tells a prober which half they got right. With
`METRICS_TOKEN` unset the endpoint still answers a signed-in operator and no
scraper can collect; startup says so.

**Pagination is a cursor where rows keep arriving.** `GET /audit` takes `limit`
(≤500) and `before_id`, and returns `next_before_id` — null when the page is the
end of the record. A page number would shift under a reader as rows land. The
list endpoints that are snapshots rather than streams take a bare `limit`:
orders ≤500, backtests ≤200, analytics trades ≤1000, rejections ≤500.

---

## 7. The route table — and which routes are not built

**49 routes, and 12 of them are stubs.** They appear in the OpenAPI schema
exactly like the working ones, because FastAPI documents a handler by its
signature and a `raise NotImplementedError` has the same signature as an
implementation. Calling one is a `500`.

This is the single most useful thing this page can tell a client author, and it
is why generating a client from the schema and trusting it is a mistake here.

**The table is checked against the source on every test run.**
`tests/unit/test_api_doc_routes.py` takes the paths from the app's own OpenAPI
document and the stub marks from an AST walk of the routers, and fails if a row
is missing, invented, or marked wrongly — so implementing one of these without
moving its mark breaks the build rather than leaving a page that quietly
understates the platform. It cannot check the third column, which is prose.

Legend: **✅** implemented · **🔲** stub, raises `NotImplementedError` → 500.

### Auth

| | Route | Notes |
|---|---|---|
| ✅ | `POST /api/v1/auth/login` | Rate limited. `429` with `Retry-After` |
| ✅ | `POST /api/v1/auth/logout` | `204` |
| ✅ | `GET /api/v1/auth/me` | `{user, scope}` |
| ✅ | `GET /api/v1/auth/context` | Pre-session. Run mode only — what the login screen may know |

### The book

| | Route | Notes |
|---|---|---|
| ✅ | `GET /api/v1/positions` | |
| 🔲 | `GET /api/v1/positions/{symbol}` | |
| ✅ | `POST /api/v1/positions/{symbol}/close` | Through `OrderRouter`. **Can be refused** — a refusal is `200` with `submitted: false` and the rule that said no. Read it; do not assume the position closed |
| 🔲 | `PATCH /api/v1/positions/{symbol}/stop` | |
| ✅ | `GET /api/v1/orders` | `limit` ≤500 |
| 🔲 | `POST /api/v1/orders` | Manual order entry. Not built |
| ✅ | `DELETE /api/v1/orders/{order_id}` | |
| ✅ | `POST /api/v1/orders/cancel-all` | Continues past a failure and reports per order |

### Risk

| | Route | Notes |
|---|---|---|
| ✅ | `GET /api/v1/risk/status` | |
| ✅ | `GET /api/v1/risk/limits` | The effective ceilings, from the saved row |
| ✅ | `POST /api/v1/risk/halt` | **The one write a read-only session may make.** Never rate limited |
| ✅ | `POST /api/v1/risk/resume` | Step-up |
| ✅ | `POST /api/v1/risk/flatten-all` | Step-up **and** the confirmation phrase. A `502` means it did **not** complete and positions may still be open |
| ✅ | `GET /api/v1/risk/rejections` | Why nothing is happening — the signals the chain refused |

### Strategies — the thinnest area

| | Route | Notes |
|---|---|---|
| ✅ | `GET /api/v1/strategies` | |
| ✅ | `POST /api/v1/strategies` | `201`, and **no `Location` header** — pointing a client at the stub below would be worse than pointing it nowhere |
| 🔲 | `GET /api/v1/strategies/available` | |
| 🔲 | `GET /api/v1/strategies/{strategy_id}` | |
| 🔲 | `PATCH /api/v1/strategies/{strategy_id}` | |
| 🔲 | `POST /api/v1/strategies/{strategy_id}/promote` | |
| 🔲 | `POST /api/v1/strategies/{strategy_id}/pause` | |

Five of seven. A strategy is authored in code and registered
(`docs/STRATEGY_AUTHORING.md`); editing one over HTTP is what ADR 0007's
argument refuses while a worker holds a view of it.

### Backtests

| | Route | Notes |
|---|---|---|
| ✅ | `POST /api/v1/backtests` | `202` — queued to a third process (ADR 0016). Checks the history exists **before** queueing, because the alternative is a job that fails four minutes in from somewhere else |
| ✅ | `GET /api/v1/backtests` | `limit` ≤200 |
| ✅ | `GET /api/v1/backtests/{run_id}` | |
| ✅ | `GET /api/v1/backtests/{run_id}/trades` | |
| ✅ | `GET /api/v1/backtests/{run_id}/equity-curve` | |
| ✅ | `GET /api/v1/backtests/compare` | |

Run status is `queued` → `running` → `done` \| `failed`. There is no
`cancelled`.

### Dashboard, analytics, audit, market data, worker

| | Route | Notes |
|---|---|---|
| ✅ | `GET /api/v1/dashboard/live` | The authoritative aggregate. The WebSocket is never the source of truth |
| ✅ | `GET /api/v1/dashboard/equity-curve` | |
| 🔲 | `GET /api/v1/dashboard/health` | |
| ✅ | `GET /api/v1/analytics/performance` | |
| ✅ | `GET /api/v1/analytics/trades` | `limit` ≤1000 |
| ✅ | `GET /api/v1/analytics/attribution` | |
| ✅ | `GET /api/v1/analytics/live-vs-backtest/{run_id}` | |
| ✅ | `GET /api/v1/analytics/reports/daily` | Reports a section it could not read rather than pretending it was empty |
| ✅ | `GET /api/v1/audit` | Cursor paginated |
| ✅ | `GET /api/v1/market-data/calendar` | |
| 🔲 | `GET /api/v1/market-data/bars/{symbol}` | |
| 🔲 | `GET /api/v1/market-data/quote/{symbol}` | |
| 🔲 | `GET /api/v1/market-data/search` | |
| ✅ | `GET /api/v1/worker/config` | |
| ✅ | `PUT /api/v1/worker/config` | Step-up **when arming** `allow_live_orders` |

### Probes

| | Route | Notes |
|---|---|---|
| ✅ | `GET /healthz` | Liveness. **Touches no dependency** — a slow database must not get a healthy API killed |
| ✅ | `GET /readyz` | Readiness. `{status, checks: {database, redis}}`, and **`503` with that same body** when either is down. The body is what turns "not ready" into "which one" |
| ✅ | `GET /metrics` | Prometheus text. Bearer token **or** session |

The broker is deliberately not checked by `/readyz`: an API with no venue can
still serve the book, the halts and the audit trail, and failing readiness would
take it out of the load balancer for a fault it can work around.

### What audits

**The API writes twelve of the thirteen verbs in `atp_core.audit.ports`** —
`login`, `login_failed`, `logout`, `rate_limited`, `forbidden`,
`strategy_created`, `order_cancelled`, `position_closed`, `flatten_all`,
`halt_engaged`, `halt_cleared` and `worker_config_updated`. The thirteenth,
`book_adopted`, is not an HTTP act at all: it belongs to
`scripts/adopt_broker_state.py`.

Two of those are not written by a route. `forbidden` comes from the two
cross-cutting refusal paths — `deps.require_write_scope` for a read-only session
attempting a write, and `stepup.require_step_up` for a password that did not
check out — which is why the verb is worth reading with its `detail`, and why
only one of the two carries `reason: step_up_failed`. And `strategy_created` has
two writers: `POST /strategies`, and `POST /backtests` when the run names a
registered class that has no row yet.

A failed audit write never fails the action (`atp_core.audit.ports`); it logs
`CRITICAL` instead, because the actions being audited include halting trading.

---

## 8. The WebSocket

`WS /ws`, unversioned, and an **enhancement rather than a source of truth**. A
dropped socket costs the dashboard its liveness between reads and not its
correctness — the next aggregate read is a whole consistent snapshot. Everything
about the implementation follows from that: nothing retries a delivery, nothing
queues, and a client the server cannot keep up with is dropped rather than
allowed to slow anything down.

### Handshake

The session cookie arrives on the handshake by itself; there is nothing to send.
An unauthenticated socket is closed **before `accept()`**, so nothing is ever
delivered to one — not even the halt broadcast every other client gets
unconditionally.

**The close code is the protocol.** `1008` means "your session is not valid,
stop retrying, show the login screen". Every other code means reconnect. In
particular a client dropped for reading too slowly gets **`1013 Try Again
Later`** and must never get `1008` — signing an operator out of a working
platform because their connection was slow is the failure that distinction
exists to prevent.

### Client → server

```jsonc
{"type": "subscribe",   "channels": ["quotes", "bars"], "symbols": ["AAPL"]}
{"type": "unsubscribe", "symbols": ["AAPL"]}
{"type": "ping"}
```

Answered `{"type":"subscribed"}`, `{"type":"unsubscribed"}`, `{"type":"pong"}`.
An unparseable or unknown frame is answered `{"type":"error","detail":…}` and
**the socket stays open** — closing on a bad message would let one buggy client
version disconnect itself in a loop, and a browser's reconnect ladder turns that
into a storm against an API that is working perfectly.

Channels are `quotes`, `bars`, `fills`, `signals`, `halts`.

**`subscribe` is additive, and the two empty cases are not the same.** A
dashboard subscribes as each panel mounts, so a second call never drops the
first panel's symbols. Naming no symbols leaves the filter alone — subscribing
to a channel without naming a symbol asks for all of it. But `unsubscribe`-ing
your last symbol leaves an **empty** filter, which means *nothing*, not
everything: conflating those once promoted a panel that unmounted from five
symbols to every tick in the universe, and the only symptom was a tab that got
slow.

Only `quotes` and `bars` are symbol-filtered. Execution events are not — a fill
on a symbol you did not subscribe to is still your money.

### Server → client

```jsonc
{"type": "quote",  "symbol": "AAPL", "bid": …, "ask": …, "ts": …}
{"type": "bar",    "symbol": "AAPL", …}
{"type": "fill",   "order_id": …, "symbol": …, "qty": …, "price": …}
{"type": "signal", "strategy": …, "symbol": …, "action": …, "reason": …}
{"type": "halt",   "scope": …, "reason": …}
{"type": "gap",    "seconds": …}
```

**`halt` and `gap` arrive whether you subscribed or not.** A trading halt is not
something to opt into — a dashboard that filtered one out would show a green
screen while nothing was trading.

**`gap` is the one message the API originates rather than forwards.** It means
this process's own subscription to the worker dropped and came back: everything
published in between reached no browser, and Redis pub/sub has no replay. The
browsers noticed nothing, because their sockets to *this* process stayed up
throughout — which is why the server has to say so. It carries no news of its
own. **Re-read the book; that read is the complete repair**, because the
aggregate read *is* the current state of everything the socket carries.

`seconds` is a **lower bound**, not a measurement. The clock starts when the
failure was noticed, and a connection that stopped carrying data without closing
is noticed up to 30 seconds after it actually died. Reporting it as exact would
be the same mistake as a frozen book age (ADR 0022) — a number describing the
detection rather than the outage.

### What the server does about silence

A quiet channel and a dead connection look identical from the outside, and that
is the failure this codebase is least able to notice (CLAUDE.md §5). The bridge
polls with an explicit 1-second deadline rather than blocking, so "nothing was
published" is an answer instead of a timeout; after 20 seconds of total silence
it pings Redis and waits for the pong, and after 30 it treats the subscription
as dead and rebuilds it — announcing the gap when it returns.

Client-side, one message gets 2 seconds to be accepted before that client is
dropped and hung up on. A slow reader with no deadline is unbounded buffering on
the server, paid for by every other client.

---

## 9. Generated types, and not hand-writing them

The dashboard's API types are **generated** from the OpenAPI document:

```bash
make gen-types      # scripts/dump_openapi.py → apps/web/openapi.json → apps/web/src/api/schema.d.ts
```

Both outputs are build products — `apps/web/openapi.json` is gitignored and
`schema.d.ts` is regenerated, so neither is a file to edit.

It needs neither a running server nor a database, which is the difference
between a generation step people run and one they work around by hand-editing
the types instead. Do not hand-write them and let them drift (CLAUDE.md §4).

Generating also forces FastAPI to resolve every handler's annotations, so a
handler whose `datetime` sits behind `if TYPE_CHECKING` fails there rather than
on the first request. `tests/unit/test_api_contract.py` asserts the same thing,
and also holds the authentication allow-list against the generated schema from
the outside.

---

## 10. Exposure

**Do not put this on a public address.** `docs/SAFETY.md` says so and the rate
limiter's own reasoning depends on it: `X-Forwarded-For` is caller-supplied and
trivially spoofed, and it is only trustworthy because this stack always sits
behind its own nginx (`infra/docker/web.nginx.conf`), which overwrites it.
Exposed without that proxy, an attacker rotates the header and the login limit
is gone.

Reach it over a private network — see `docs/LOCAL_HOSTING.md` for the deployed
arrangement and `docs/DEPLOYMENT.md` for the stack.

---

## Related

| | |
|---|---|
| `docs/ARCHITECTURE.md` | The module map this API is the thin edge of |
| `docs/DASHBOARD.md` | The client, and why it refreshes when asked (ADR 0022) |
| `docs/OBSERVABILITY.md` | What `/metrics` exposes and how it is scraped |
| `docs/RUNBOOK.md` | When one of these endpoints is the thing that is broken |
| `docs/SAFETY.md` | The layers that stand between this API and a live order |
| `adr/0008` · `0009` · `0010` | The session, the authorisation model, the limiter and the audit trail |
