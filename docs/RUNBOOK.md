# Runbook

For when something is wrong during market hours. Read the first section now, not
during the incident.

## First move, always

**HALT.** Diagnose second — a halt costs missed opportunity; hesitation costs
money.

> **The dashboard button does not exist yet.** It is Phase 5, and
> `POST /api/v1/risk/halt` still raises `NotImplementedError`. Until both land,
> the halt is a command:
>
> ```bash
> uv run python scripts/halt.py engage --by "<your name>" --detail "why"
> ```
>
> Run it before you need it once, so the first time is not during an incident.
> `scripts/halt.py status` says what is halted and exits 2 when anything is;
> `clear --by <name>` resumes. Stopping the worker
> (`docker compose stop worker`) is **not** the same thing: it leaves other
> processes free to trade and deliberately does not halt.
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

## Worker crash-looping

1. Halt (the API is independent of the worker).
2. `docker compose logs worker --tail=200`.
3. Positions are safe if broker-side stops are in place — verify.
4. Fix, then restart. `warmup()` will reconcile and adopt open positions.

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
