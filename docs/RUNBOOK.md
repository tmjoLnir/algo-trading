# Runbook

For when something is wrong during market hours. Read the first section now, not
during the incident.

## First move, always

**HALT.** Diagnose second — a halt costs missed opportunity; hesitation costs
money.

> **Two ways, and either is fine.** `HALT TRADING` on the dashboard, top right,
> which asks nothing further and stops everything; or the command, which is the
> one that still works when the dashboard does not:
>
> ```bash
> uv run python scripts/halt.py engage --by "<your name>" --detail "why"
> ```
>
> Run the command once before you need it, so the first time is not during an
> incident — and because it is what you fall back to when the page will not
> load.
>
> **Clearing asks for the password, wherever you do it.** The halt banner now
> carries a `Resume…` control per halt, which posts to `POST /risk/resume` and
> demands the account password in the body — stopping stays reflexive, restarting
> stays a decision, and a read-only session may halt but may not resume.
> `clear --by <name>` does the same job from the shell and is what you fall back
> to when the page will not load. `scripts/halt.py status` says what is halted
> and exits 2 when anything is.
>
> **Clearing one halt is not clearing all of them.** A halt is keyed on scope
> and target, so resuming the global halt leaves a symbol halt standing. The
> banner is the answer to what remains: it re-reads every active halt after a
> resume and stays up if any are left.
>
> Stopping the worker (`docker compose stop worker`) is **not** the same thing:
> it leaves other processes free to trade and deliberately does not halt.
>
> If the button reports that the halt was **not recorded**, believe it and read
> the message: the switch fails closed, so nothing is trading while the store is
> unreachable, but nothing was written either — trading resumes on its own when
> the store recovers. Halt again once it is back, or stop the worker meanwhile,
> and confirm with `scripts/halt.py status`.
>
> `scripts/status.py` is the read-only companion — halts, quote freshness, the
> latest stored bars, and the venue's account, positions and working orders.
> Safe to run during an incident.

Halting is *not* flattening. Halting stops new risk. Flattening realises
existing P&L and is a separate decision.

## Reading the numbers

`scripts/status.py` first — it is the operator's view and it needs no token.
`/metrics` is the second look, and it answers questions status cannot: *how long
has this been happening*, and *is it happening on both processes*.

```bash
# what the platform thinks is true right now
curl -sH "Authorization: Bearer $METRICS_TOKEN" http://127.0.0.1:8000/metrics \
  | grep -E 'atp_halt_(active|state_readable)|atp_orders_rejected|atp_alerts_failed'
```

Three readings worth knowing before you need them:

- **`atp_halt_state_readable 0`** — the kill-switch state cannot be read.
  It fails closed, so *every order is being refused* whether or not anything is
  halted. This is a Redis problem, not a trading one.
- **`atp_orders_rejected_total{stage="indeterminate"}` rising** — a submit
  failed in transport and could not be resolved against the venue. Go to
  "Indeterminate submit" below; it always comes with a halt.
- **`atp_alerts_failed_total` rising** — halts are happening and *no phone is
  ringing*. Whatever you are reading about here, assume nobody else knows.

If a scrape fails entirely, that is information: the worker's exporter dies with
the worker, which is the point of it being a separate target (ADR 0013). Both
endpoints also accept your dashboard session, so a browser works if you do not
have the token to hand. Full reference: [OBSERVABILITY.md](OBSERVABILITY.md).

Every log line inside one unit of work carries a `correlation_id` — one per API
request, per scheduled job, per pass of the strategy loop. Once you have found
one interesting line, that field is how you get the rest of what it was doing:

```bash
docker compose logs worker | grep '"correlation_id": "<the id>"'
```

## The phone buzzed

An alert means the platform **has already stopped trading** — every one of them
is sent from the kill switch after the halt is durable (ADR 0012). So there is
nothing to do in the first instant; the reflex this runbook opens with has
already happened by itself.

The title carries the reason. Find it below — `data_feed_lost` is "Data feed
disconnected", `reconciliation_mismatch` is "Reconciliation mismatch",
`broker_unreachable` is "Broker unreachable" — and work that section.

Deliberately absent from the notification: balances, positions, P&L. Open the
dashboard for those. A "Trading resumed" alert is somebody clearing the halt; if
that was not you and you do not know who, treat it as an incident.

**Silence is not health.** Alerts go out from the worker; a worker that is dead,
partitioned or was never configured with a topic sends nothing at all, and the
absence of alerts and a working platform look identical from a phone.

---

## Data feed disconnected

*Symptom:* `feed.disconnected` in logs, stale banner on the dashboard, no ticks.

1. It should have auto-halted. Confirm it did.
2. Check Alpaca status. Check our network. Check the key has not been revoked.
3. Positions keep their **broker-side** stops — those are unaffected by our
   downtime. This is why they exist.
4. On recovery: confirm backfill ran, verify no gap, reconcile, then clear.
   Do not wait for the nightly sweep to prove it — run the check yourself over
   the outage window:
   `uv run python scripts/backfill_bars.py --symbols SPY,... --start <outage day> --verify`

**Do not** clear the halt to "keep trading" on the last known price.

## Reconciliation mismatch

*Symptom:* `ReconciliationError`, trading auto-halted.

Our book disagrees with the broker's. **Do not resume until it is understood.**

1. Compare `GET /api/v1/positions` with the broker's own UI.
2. Usual causes: a fill during a restart, a missed WS event, a corporate action,
   a manual trade placed outside the platform.
3. Once you know *why*, `adopt_broker_state()` to resync. Not before —
   adopting silently hides the bug, and if the cause is duplicate submission you
   will do it again tomorrow.
4. Clear the halt.

## Duplicate positions

*Symptom:* position roughly double what it should be.

Likely a retry without `client_order_id` reuse, or two workers running.

1. Halt.
2. `ps` / check the orchestrator — **is a second worker running?** Kill it.
3. Manually flatten the excess through the broker.
4. Fix the retry path before resuming. This is a code bug, not an ops incident.

## Runaway order submission

*Symptom:* order count climbing fast, rate-limit rejections.

1. Halt. The rate limiter should already have caught it.
2. `POST /api/v1/orders/cancel-all`.
3. Identify the strategy; pause it specifically.
4. Almost always a signal re-firing because a crossover was tested as a level
   rather than a transition.

## Stop did not fire

*Symptom:* position past its stop, still open.

1. Was it broker-side or engine-side? Check the broker for a resting stop order.
2. If engine-side and the worker was down — that is the known limitation, and
   the reason broker-side is the default for live.
3. Decide immediately: close manually or accept the position. Do not leave it.
4. Afterwards: why was there no broker-side stop?

## Dashboard shows "502 Bad Gateway"

*Symptom:* the dashboard renders, and then, where the numbers should be,
`Failed to load dashboard: Error: 502: Bad Gateway`. Under `make up` the dev
server says `500` for the same fault.

**Read this line before diagnosing: you have not lost control of the book.** A
502 is the server that sent you the *page* reporting that it could not reach the
API. Nothing about positions, stops or the halt state has changed, and none of
the three tools that matter go through the dashboard — `scripts/halt.py` engages
a halt, `scripts/status.py` reads halts, quote freshness and the venue's account
and positions, and broker-side stops are held by the venue regardless. Halt
first if you would have halted anyway; the dashboard is not a prerequisite.

The API never answers 502 itself: everything it has an opinion about comes back
as JSON, including the 503 it returns when it cannot read the halt state. So the
status came from nginx (`web-prod`, port 8080), and it means one hop failed —
nginx to the `api` container.

Three causes look identical on screen. The two probes separate them, and both
are proxied onto the dashboard's own origin, so they answer in the same browser
with no shell on the host:

| `/healthz` | `/readyz` | What it is | Where to look |
|---|---|---|---|
| 502 | 502 | the API is not running | `docker compose ps`, then `logs api` |
| 200 | 503 | API up, a dependency is not | the `/readyz` body names `database` or `redis` |
| 200 | 200 | API and dependencies are fine | reload; then the browser console |

`/readyz` returns `{"status": ..., "checks": {...}}` and marks each dependency
`ok`, `unreachable` or `absent`. It deliberately does not say *why* — it is open
without a session, and a driver's connection error quotes the DSN. The reason is
in the API's log: `docker compose logs api | grep readyz`.

`db`, `redis` and `api` carry `restart: unless-stopped`, so a single exit — an
OOM kill, a host reboot, a laptop suspended with the stack up — recovers on its
own and this symptom should clear within seconds. It did not always: nginx
restarted forever while the API did not, so one exit produced a dashboard that
rendered perfectly and 502'd permanently. If you are on a checkout without that
policy, `docker compose up -d api` is the immediate fix.

A container that keeps restarting is the other shape of this, and `docker
compose ps` distinguishes them — a climbing restart count is a crash loop, not a
recovery. Read `logs api` for the traceback; a `Settings` validation error at
startup is the usual cause on a machine where `.env` was edited.

### Before you sign in: "Cannot reach the API"

The same fault, one screen earlier. With no session yet, the dashboard shows

> Cannot reach the API. This is not a sign-in problem — the server did not
> answer. It will retry on its own.

in place of the login form. That substitution is deliberate: a password box in
front of a server that cannot check a password invites the operator to read an
outage as their own typing. The `/healthz` and `/readyz` ladder above applies
unchanged — both are open without a session, so they answer from the same
browser showing this screen.

**First, check the API is not crash-looping.** `docker compose ps api` — a
climbing restart count means it is exiting at startup, and then no amount of
waiting helps because there is nothing to reach. `docker compose logs api` has
the reason. The one that produced this symptom in the past was a `Settings`
validation error: an empty `ALPACA_API_KEY` in `paper` or `live` mode refused to
validate, and since the API builds its app at import it could not start at all.
That is fixed — a missing broker credential is now a CRITICAL log line
(`startup.no_broker_credentials`) and a broker that will not build, not a
process that will not run — but any *other* `Settings` error still exits the
same way, and `.env` is where to look.

**Otherwise it clears itself.** The screen re-asks every five seconds and renders
the login form as soon as the API answers, so a stack that is merely still
starting needs no reload. It did not always, and the sentence on screen was the giveaway: the
session probe was configured never to retry, so the first refused connection —
the seconds between the dev server accepting requests and the API being ready to
answer, which `make up` produces on every cold start — stuck until the tab was
refocused or reloaded, under a message promising it would retry on its own. If
this screen persists for more than a few seconds, the API genuinely is not
answering: work the table above.

## Worker crash-looping

1. Halt (the API is independent of the worker).
2. `docker compose logs worker --tail=200`.
3. Positions are safe if broker-side stops are in place — verify.
4. Fix, then restart. `warmup()` will reconcile and adopt open positions.

## A backtest is stuck, or the queue is not running

**Nothing here touches trading.** Backtests run in the `queue` container, which
places no orders — its broker is simulated and lives inside the run. A dead queue
worker means research is stalled, not that the book is at risk, so this is never a
reason to halt.

*Symptom: a run sits at `queued` and nothing picks it up.*

1. `docker compose ps queue` — is it running? `docker compose logs queue --tail=100`.
2. It logs `queue.ready` on startup with the queue name. No such line means it
   never got past `on_startup`; the usual cause is Redis.
3. `docker compose restart queue`. The job is still on the queue — arq holds it in
   Redis — so a restart picks it up rather than losing it.

*Symptom: a run sits at `running` and never finishes.*

1. If the worker is alive and the run is legitimately long, leave it. A multi-year
   minute-bar run is minutes; the job times out after an hour.
2. If the worker was restarted or killed mid-run, that row is orphaned: the job is
   gone from Redis and no retry is coming. **A worker starting is what corrects
   it** — `queue.on_startup` sweeps rows left `running` past twice the job timeout
   and marks them *interrupted*, with a message saying to queue it again. So
   `docker compose restart queue` is the fix, and the row will say what happened
   rather than staying stuck.
3. There is deliberately no cancel. arq cannot interrupt a job already executing
   (ADR 0016), so a run you no longer want is waited out or the container is
   restarted — which orphans the row, which the sweep then labels.

*Symptom: every run fails immediately with "unknown strategy".*

The queue worker's strategy registry is populated by an import side effect, and
the API's is populated separately — so a request the API accepts can be refused
by the worker. `atp_worker.queue` imports `atp_core.strategy.examples` for exactly
this reason; if a strategy module stopped being imported there, this is the
symptom, and `tests/unit/test_backtest_queue.py` is what should have caught it.

*Symptom: a run fails naming `backfill_bars.py`.*

Not a fault. The history it needs is not stored, and the message is the command
that fixes it. The API checks coverage before queueing, so seeing this from the
worker means the bars were there at request time and are not now — a restored
database, or a symbol whose bars were deleted.

## Broker unreachable

1. Auto-halts. Confirm.
2. Check Alpaca status; check whether it is only us.
3. **Positions with broker-side stops remain protected** — the venue holds them.
4. If it is prolonged and you want out, use the broker's own UI. Do not wait for
   the platform.

## Indeterminate submit (`order.submit_indeterminate`)

*Symptom:* a `CRITICAL` log with that event name, and trading halted with reason
`broker_unreachable`.

The transport failed *after* an order was sent, so the venue either never saw
it, has it resting, or has already filled it. The router deliberately did not
resubmit — that is how one intended position becomes two — and left the order at
`pending_submit`, which is the honest status for "approved, not yet
acknowledged".

1. Take the `client_order_id` from the log line. It is deterministic, so it is
   the same key the venue would have recorded.
2. Look it up in the broker's own UI or API. This is the whole question: does
   the venue have that order, and did it fill?
3. **Do not resubmit by hand.** If the order needs to go again, replay the same
   request through the platform — the key is derived from the decision, so the
   venue deduplicates. A hand-built order gets a new key and no such protection.
4. Reconcile before clearing the halt. Trading on against a book you have not
   confirmed is the thing the halt bought you time to avoid.

## Position open with no stop (`order.position_unprotected`)

*Symptom:* a `CRITICAL` log with that event name.

An entry filled and its protective stop was refused, or there was no stop to
place. The position is live and the venue holds nothing against it. An
engine-side level may be armed, which protects you only while the worker is up —
that is not the guarantee a broker-side stop gives.

1. Read `rule` in the log line. A transient refusal (`stale_data`,
   `trading_hours`, `rate_limit`) clears on its own and the runner's next
   attempt places the stop; `kill_switch` will not clear until someone clears it.
2. If it will not clear promptly, place the stop through the broker's own UI.
3. `no stop level was requested and no stop_config was supplied` is a strategy
   configuration bug, not an incident: the strategy is trading without a stop.
   docs/SAFETY.md makes that a go-live blocker.

## Emergency flatten

`POST /api/v1/risk/flatten-all` with `confirm: "FLATTEN ALL POSITIONS"`.

Irreversible: realises every open P&L at whatever the market offers. Correct
when you have lost confidence in the system's state. Wrong when you have simply
lost visibility — you would be dumping the book into a market you cannot see.

---

## After any incident

Write it down the same day: what happened, what was seen first, what was done,
what would have caught it. The post-mortem is how the next guardrail gets added.
Add the regression test.
