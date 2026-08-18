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
  their unversioned names, so `/healthz` through the proxy answers "is the whole
  chain up".
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
