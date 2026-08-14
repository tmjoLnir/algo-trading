# Runbook

For when something is wrong during market hours. Read the first section now, not
during the incident.

## First move, always

**HALT.** Dashboard, top right. No confirmation needed.

Diagnose second. A halt costs missed opportunity; hesitation costs money.

Halting is *not* flattening. Halting stops new risk. Flattening realises
existing P&L and is a separate decision.

---

## Data feed disconnected

*Symptom:* `feed.disconnected` in logs, stale banner on the dashboard, no ticks.

1. It should have auto-halted. Confirm it did.
2. Check Alpaca status. Check our network. Check the key has not been revoked.
3. Positions keep their **broker-side** stops — those are unaffected by our
   downtime. This is why they exist.
4. On recovery: confirm backfill ran, verify no gap, reconcile, then clear.

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
