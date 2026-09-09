# Paper Week — Day 3 Review

**Session:** 2026-09-09 · **Log window:** 11:08:42Z → 21:41:05Z · **RTH:** 13:30–20:00Z
**Source:** 9,566 docker log records across 5 containers, cross-checked against the repository at `9800a92`
**Config:** `sma_crossover`, 20 symbols, Alpaca paper, IEX feed, `run_mode=paper`, config revision 6
**Predecessor:** `day-2-review.md` (2026-09-08). 12 commits landed between the two sessions, carrying the day-2 fixes for B1, F3, F4, F7 and F8.

---

## 1. The verdict

**Day 3 did not run. It rebooted 131 times and left a naked position at the venue.**

Two independent failures, either of which alone would void the session.

**The host was switched off for most of the day.** Every container in the stack goes silent
together — the API's healthcheck, nginx's, Redis's background saves — sixteen separate times,
for a total of **8.8 hours out of a 10.5-hour capture**. Inside regular trading hours the stack
was dark for **311.3 of 390 minutes: 79.8%**. This is not a feed outage and not a crash; it is
the machine underneath asleep. `docs/adr/0021` chose that machine and wrote the condition down:

> **Sleep is now a configuration item, and it is the whole risk.** [...] a paper week
> interrupted by sleep is not a paper week, so the four weeks `docs/SAFETY.md` asks for are
> gated on this being handled rather than intended.

It was intended. It was not handled.

**And the one order the platform managed to place destroyed it.** At 14:44:33 the strategy
bought 10 MSFT at market. Four shares filled. The router submitted the protective stop 238 ms
later, while the parent order was still working, and Alpaca refused it:

```
{"code":40310000,"message":"potential wash trade detected. use complex orders",
 "reject_reason":"opposite side market/stop order exists"}
```

The adapter classified that as `InsufficientFundsError`, the exception escaped the
`trade_updates` consumer, and the worker died. It has not run since. Every subsequent boot
reads its own stored book (`cash=100090.06`, `positions=[]`), asks the broker to agree, is told
the broker holds 10 MSFT and $4,932.32 less cash, and refuses to start:

```
ExecutionError: refusing to start: the book does not match the broker's
  — missing_position MSFT: the broker holds a position we do not;
    cash account: cash differs by 4932.32000000, beyond the 1.00 tolerance
```

That guard is correct. It is also unconditional, and the process exits when it trips, so
Docker restarts it into the same failure. **129 boots died on that one line.** The service
carries `restart: unless-stopped` with no attempt cap, so the loop has no backoff of its own,
no ceiling and no escape hatch. The only thing that ever interrupted it was the host going to
sleep.

**Three numbers describe the whole session:**

| | |
|---|---|
| Time the platform was permitted to trade | **16 min 56 s** (14:27:48 → 14:44:44) |
| Orders submitted | **1** |
| Positions closed | **0** |

**A 10-share MSFT position has been open, unprotected, since 14:44:34** — through the close and
into after-hours, 6 h 56 m at the end of the capture, and still open when the log stops. There
is no broker-side stop, because the stop was rejected. There is no engine-side stop, because
the engine refuses to start. `docs/SAFETY.md:125`'s go-live gate — *"there are no unprotected
positions"* — is violated for the second session running, and this time the position is still
there.

**The day-2 fixes worked.** That needs saying, because it is easy to lose in the wreckage.
Zero sub-penny rejections (B1 fixed). Corporate actions skipped the seed tickers and completed
(F7 fixed). Warmup refused stale bars from before the session boundary (F8 fixed). The
reconciler re-read before halting (F4 fixed). Protection failure raised a CRITICAL (F3 wired,
though not on the path that fired — see F10). Every one of them behaved as designed. Day 3
failed *past* them.

**And day 2 predicted it.** Its closing section warned that the protection paths had never been
exercised because every stop was rejected up front, and that fixing B1 would route fills into
code *"neither of which this session touched even once"*. The first fill that reached that code
crashed the worker.

**Recommendation: stop the paper week. Do not run day 4 on this host.** Fix the sleep
configuration or move the stack; fix the crash loop; then restart the count at day 1. Days 1, 2
and 3 have now each been voided by a different defect, and none of them measured a strategy.

---

## 2. Timeline

| Time (UTC) | Event |
|---|---|
| *(10:26:34)* | **A reconciliation halt is engaged, before the capture opens.** Day 3 inherits it |
| 11:08:42 | Day 2's worker shuts down cleanly — `'signal received — shut down cleanly, nothing halted'` |
| 11:08:43 | Boot 1. `restored_book cash=100094.20`, then `fees.settled owed=4.14` → **100090.06** |
| 11:08:43 | `worker.ready halted=True` — `halts=['global by reconciler (reconciliation_mismatch) at 10:26:34']` |
| 11:08:45 | `runner.market_closed sleeping_seconds=8474` |
| 11:14:51 | **Host dark — 49.1 min** |
| 12:04:11 | **Host dark — 71.7 min** |
| 13:20:09 | Corporate actions: `skipped_seed_symbols ZVZZT ZWZZT ZXZZT` — **F7 fixed** |
| 13:20:16 | **`corporate_actions.inconsistent BAC`** — 386 of 1066 bars agree. Prices stored anyway (F5) |
| 13:30:00 | **Market open.** Still halted |
| 13:32:54 | Boot 2 (host had slept through the open) |
| 13:32:56 | `warmed_up bars=20 needed=51 short=20` — **every symbol short.** Evaluation 1 |
| 13:56:10 | Host dark — 2.9 min |
| 13:59:14 | **Host dark — 15.4 min** |
| 14:14:33 | `staleness.detected silent_for=914.1s` → **global halt**, reason `data_feed_lost` |
| 14:14:39 | `staleness.recovered` — 6 seconds later. The halt stays |
| 14:15:40 | `halt_reminder` #1 — the only one all day |
| 14:25:45 | Host dark — 2.2 min |
| 14:27:45 | `staleness.detected silent_for=110.3s` → halted again |
| 14:27:48 | **Operator clears the halt** — 1 s after loading the dashboard, 3 s into the second outage |
| 14:28:00 | Data recovers. Operator reads `/orders` at 14:28:01 and `/positions` at 14:28:02 — *after* clearing |
| 14:44:33 | **Evaluation 53. One signal. `order.submitted MSFT buy 10 market`** |
| 14:44:34.189 | `trade_update.filled qty=4 price=493.23 status=partially_filled` |
| 14:44:34.427 | **`runner.position_unprotected reason='protective submission raised' qty=4`** |
| 14:44:44.443 | **`responsibility_ended trade_updates` — wash-trade rejection, unhandled.** Worker dies |
| 14:44:45.391 | `worker.halted`; Telegram alerted |
| 14:44:47 | Boot 3. Refuses to start on the book mismatch. **The loop begins** |
| 14:44:52 | `reconcile.mismatch` → `killswitch.escalated unproven=['MSFT']` |
| 14:44–15:09 | **88 boots in 25 minutes**, one every ~17 s |
| 15:09:09 | `staleness.detected silent_for=369.1s` |
| 15:09:02 | **Host dark — 50.2 min.** From here the day is bursts of 2–7 boots separated by hours |
| 17:09:05 | `staleness.detected silent_for=7145.2s`; reconnect backfill writes **2,169 bars** |
| 20:00:00 | **Market close. No `session_summary` — the host was asleep** |
| 20:30:00 | **No `daily_report` — the host was asleep** |
| 21:38:25 | **`POST /api/v1/risk/resume` → 401.** Twice more, 21:38:29 and 21:38:37. All 401 |
| 21:39:47 | The dashboard itself starts returning 401 |
| 21:40:06 | `auth.login_failed`, then `auth.login` at 21:40:17 |
| 21:40:48 | Last `reconcile.mismatch`. **Resume is never retried** |
| 21:41:05 | Capture ends. Halted, crash-looping, 10 MSFT live at the venue |

131 boots. 130 halts. 142 alerts. 1 order.

---

## 3. The blockers

### B1 — The host sleeps, so there is no session to measure `blocker`

**Evidence.** Container healthchecks are the clock. `atp/api` polls `/healthz` and `atp/web-prod`
is polled by `wget`, together every ~15 s all day. Sixteen times they stop together and resume
together:

| Window (UTC) | Dark |
|---|---|
| 11:14:51 → 12:03:57 | 49.1 min |
| 12:04:11 → 13:15:53 | 71.7 min |
| 13:29:01 → 13:31:21 | 2.3 min |
| 13:56:10 → 13:59:06 | 2.9 min |
| 13:59:14 → 14:14:37 | **15.4 min** |
| 14:25:45 → 14:27:54 | 2.2 min |
| 15:09:02 → 15:59:12 | 50.2 min |
| 15:59:16 → 17:08:35 | 69.3 min |
| 17:09:10 → 18:09:34 | 60.4 min |
| 18:09:38 → 18:27:48 | 18.2 min |
| 18:27:53 → 19:10:50 | 42.9 min |
| 19:11:24 → 19:15:07 | 3.7 min |
| 19:15:12 → 20:11:48 | 56.6 min |
| 20:12:23 → 21:12:46 | 60.4 min |
| 21:13:22 → 21:18:56 | 5.6 min |
| 21:19:01 → 21:37:59 | 19.0 min |

**Total 529.8 minutes — 8.8 hours. Inside RTH: 311.3 of 390 minutes, 79.8%.**

This is not the worker crashing: during these windows *no container logs anything*, including
Redis, whose own periodic-save timer only fires when the host is running. It is not a network
partition either — the stack is loopback-bound. The machine is asleep.

**Everything else on the day decodes from this.** The four staleness detections
(914 s, 110 s, 369 s, 7145 s) are not feed incidents, they are the monitor measuring wall-clock
across a sleep, exactly as ADR 0021 said it would:

> A Mac that sleeps mid-session drops the Alpaca stream, stops the five-minute reconcile and
> the one-minute snapshot, and comes back with a wall clock that has jumped — against
> `StalenessMonitor`, which measures silence in wall-clock seconds.

The reconnect backfill then does its job correctly and invisibly: 2,169 bars recovered in one
call at 17:09, 134 at 15:09, 294 at 14:14. **The tape is not the casualty — the session is.**
The 20:00 session summary and the 20:30 daily report both fall inside dark windows and never
ran, which is why day 3 has no end-of-day artifact of any kind.

`docs/LOCAL_HOSTING.md` already carries the mechanics, including the trap that most likely
applies here (`LOCAL_HOSTING.md:66`):

> Note that `-s` asserts only while on AC power — a MacBook on battery will still sleep. Ctrl-C
> ends the assertion, and so does closing the terminal.

**Fix.** Before anything else runs: `sudo pmset -c disablesleep 1`, verified with `pmset -g`,
and `pmset -g log | grep -i "sleep\|wake"` checked after the session rather than assumed.
`LOCAL_HOSTING.md:423` already prescribes exactly this and it was not done.

**And make the platform able to say it happened.** A session that loses 80% of its wall clock
should not require a reviewer to correlate healthcheck timestamps across four containers to
discover it. The staleness monitor already detects the symptom; it reports it as lost market
data, which is the wrong diagnosis and sends the operator to Alpaca's status page. A silence
that stops *every* container is a host event, and the daily report should carry
`host_dark_minutes` as a first-class number.

**If sleep discipline cannot be made reliable, take ADR 0021's own fallback.** It named one:
*"A commodity US-East VPS [...] the fallback for paper if the sleep discipline proves
unworkable in practice."* Two sessions of evidence now say it is unworkable — day 2 recorded a
129.6-second whole-host stall and the review filed it under papercuts. That was the same
failure, small.

### B2 — A broker rejection the adapter cannot classify kills the worker, and the restart guard makes it permanent `blocker`

Three defects in series. Each is survivable alone.

**First: protection is submitted against a working parent order.** The entry was a market BUY
of 10 MSFT. It filled 4, the fill event fired, and `_protect` submitted a SELL STOP for the
filled quantity 238 ms later — while the BUY still had 6 shares working. Alpaca's wash-trade
check refuses an opposite-side stop against a live market order in the same symbol. This is not
an edge case; it is what a partial fill on a market order looks like, and CLAUDE.md §5 lists
partial fills as a thing this codebase is expected to get right.

There is no parent-order check, and that is deliberate. `router.py:416-443`:

> *Submit this immediately on fill, before anything else. The window between "we own it" and
> "we have a stop on it" is unprotected exposure* [...] *An entry that fills in pieces gets a
> stop per piece.*

"A stop per piece" is the wash-trade generator, because `partial_fill` is a first-class fill
event (`alpaca.py:204`). `entry_order.is_complete` is consulted exactly once in the method, at
`router.py:611`, and only for bookkeeping *after* a successful submit. The trade-off — close the
unprotected window at the cost of overlapping orders — was chosen knowingly; what nothing
anticipated is a venue refusing the resulting pair. The module docstring already names the
missing primitive (`router.py:33-41`): *"`BrokerPort` has no bracket, so a stop and a target for
the same shares are two independent orders"* — which is what Alpaca means by `use complex
orders`.

**Second: the rejection is classified as the wrong exception, and the router cannot catch it.**
`alpaca.py:429-432` inspects the response body for one integer and returns unconditionally:

```python
# libs/core/src/atp_core/brokers/alpaca.py:427-432
# 403 on an order submit is Alpaca's buying-power refusal; it carries a
# distinct code so a caller can tell "no money" from "no permission".
with contextlib.suppress(ValueError):
    payload = json.loads(response.text)
    if isinstance(payload, dict) and payload.get("code") == 40310000:
        return InsufficientFundsError(f"Alpaca refused {method} {path}: {detail}")
```

The comment states the belief that produced the bug. Alpaca reuses `40310000` as a generic
403 "order not permitted" bucket, and the wash-trade refusal is one of its members. **It is the
only Alpaca error code the platform knows** — there is no other numeric code, and no `wash` or
`opposite side` string, anywhere in the tree. The check runs *before* the status-code branch, so
it pre-empts the `OrderRejectedError` that every other 403 would have produced.

That distinction is everything, because `InsufficientFundsError` and `OrderRejectedError` are
**siblings** under `BrokerError` (`errors.py:90-101`), not parent and child — and `_route`
catches only two of the three:

```python
# libs/core/src/atp_core/execution/router.py:1074-1091
try:
    acknowledged = await self.broker.submit_order(order)
except OrderRejectedError as exc:          # ← would have logged order.broker_rejected
    ...
    return SubmitResult(order=order, decision=decision, submitted=False)
except BrokerConnectionError as exc:
    return await self._resolve_indeterminate(order, decision, exc)
```

Had `_refusal` returned `OrderRejectedError`, line 1076 catches it, the router returns
`submitted=False`, and `submit_protective_orders` takes its ordinary refusal branch: a CRITICAL,
a metric, and a normal return. **No crash.** `router.py:64` even imports `BrokerError` and never
catches it.

The call stack shows the exception propagating untouched:

```
trading.py:396   consume_trade_updates
runner.py:1579   on_fill_event
runner.py:1654   _protect
router.py:570    submit_protective_orders
router.py:1075   _route
alpaca.py:587    submit_order
alpaca.py:402    _request        → InsufficientFundsError
```

Note what is *missing* from the day's log: **zero `order.broker_rejected` events.** Day 2
logged 85 of them. That log line lives inside the `except OrderRejectedError` arm above, so it
is silent here for exactly the reason the crash happened. A protective order the venue refuses
is a known, expected outcome; it must not be able to kill the consumer that books fills.

**On the ten-second gap between the CRITICAL and the death, a correction to the obvious
reading.** It is not a retry and not a second attempt. `runner.py:1653-1682` catches everything,
writes the fact down, and re-raises deliberately — the log call and the `raise` are consecutive
statements:

```python
try:
    result = await self.router.submit_protective_orders(...)
except Exception:
    ...
    log.critical("runner.position_unprotected", ..., reason="protective submission raised")
    raise
```

`submit_protective_orders`, `_protect` and `on_fill_event` have exactly one production caller
each, a 403 is not in `_RETRY_STATUSES`, and the raise unwinds the trade-updates generator so no
second fill event can arrive. The ten seconds is the shutdown path: `supervise` cancels the
sibling tasks, then makes two blocking alert sends through a synchronous `httpx.Client` whose
timeout is `alert_timeout_seconds = 5.0` (`config.py:143`). Two unreachable sends at 5 s each is
10.0 s against 10.016 s observed. That last step is inference from the timeout value rather than
from a log line, but a retry, a second fill and a second call site are all ruled out by the code.

**Third: the start-up guard has no way out.** `runner.py:562` refuses to start when the book
disagrees with the broker. That is the right instinct, and — contrary to the obvious guess — it
is not a day-2 fix: it has been there since `eadac93` (2026-08-26). What day 2 added at that
call site is the F4 re-read. But the failure is *terminal by construction*: the divergence is
real, nothing in the loop can repair it, `supervise` re-raises (`main.py:490`), `asyncio.run`
exits 1, and `docker-compose.yml:237` says `restart: unless-stopped` with no attempt cap
anywhere in either compose file. 129 consecutive boots died on that line. Median time between
boots: **16.9 seconds.**

The compose file predicted this in a comment about a different failure
(`docker-compose.yml:202-208`): the worker was once kept behind a profile precisely because an
always-raising `main` *"which `restart: unless-stopped` would have turned into an endless
crash-loop — and a red container everyone learns to ignore."*

**And the documented remedy has no operator entry point.** `docs/RUNBOOK.md:182` says *"Once you
know why, `adopt_broker_state()` to resync"*. That method has exactly one production caller —
`trading.py:342`, inside `restore_or_adopt` — reached **only when no stored book exists at all**.
A crash-looping worker that *has* a stored snapshot can never take that branch, and there is no
`scripts/` entry point for it. `execution.reconcile.adopted_broker_state` appears **0 times** in
the log, for the second session running. The RUNBOOK is prescribing a method call the operator
has no way to make.

**Fix, in order:**
1. **Do not place protection while the parent is working.** Arm the level, place the venue-side
   stop when the entry is terminal (filled or cancelled), and keep the engine-side stop as the
   cover in between — which is what it is for. If protection must go earlier, use the bracket
   or OCO form Alpaca's own message points at (`use complex orders`).
2. **Map broker rejections by code, not by guess, and never let one escape a fill consumer.**
   40310000 is a rejection, not an account error. Add a `BrokerRejectedError` class, log
   `order.broker_rejected` for it as day 2 did, raise the unprotected alert, and continue.
   A catch-all that maps unknown codes to `InsufficientFundsError` is worse than one that maps
   them to a generic rejection.
3. **A guard that cannot be satisfied must not be a restart loop.** Refusing to trade on a
   divergence is right; exiting the process is not. Stay up, stay halted, keep serving the
   dashboard and metrics, and tell the operator what one command fixes it. Failing that, cap
   the restarts and stop.
4. **Give `adopt_broker_state` an operator entry point** — a `scripts/` command, or a
   step-up-authenticated endpoint — so the RUNBOOK's own instruction is executable.

**The smallest change that would have prevented all of this is one line:** have `_route` catch
`BrokerError` rather than only its two named subclasses, or have `_refusal` stop treating
`40310000` as proof of a buying-power refusal. Everything downstream already handles a refusal
correctly.

---

## 4. Findings

### F1 — 129 identical CRITICAL pages went to one human `high`

142 alerts were sent, 139 of them CRITICAL, all to Telegram:

| Count | Key |
|---|---|
| **129** | `worker.died.strategy_runner` |
| 3 | `halt.global.all.escalated.reconciliation_mismatch.MSFT` |
| 2 | `staleness.recovered` |
| 2 | `halt.global.all.reconciliation_mismatch` |
| 1 each | `corporate_action.inconsistent.BAC`, `halt.global.all.data_feed_lost`, `halt.reminder.1`, `halt.global.all.cleared`, `halt.global.all.unhandled_exception`, `worker.died.trade_updates` |

Day 2's F3 found that `Alert.key` is computed carefully and never reaches the wire. Day 3 is
what that costs: the same key, 129 times, at a peak of 64 in one hour. The operator's phone
received a critical alert roughly every 17 seconds for 25 minutes.

This is worse than silence. An operator who muted the channel after the twentieth page — a
rational response — then missed `corporate_action.inconsistent.BAC` and every subsequent halt.
And the one alert that would have explained everything, the host going dark, does not exist.

**Fix.** Deduplicate on `key` at the transport with a rollup ("worker has died 129 times since
14:44"), and rate-limit per key. The machinery to do this is already designed; it is the wire
format that drops the field.

### F2 — The strategy traded on its slow average's first two values `high`

`runner.warmed_up bars=20 needed=51 short=20` — at 13:32:56, warmup found **one bar per symbol**
and declared all 20 short. This is the day-2 F8 fix working correctly: `not_before=
2026-09-09T13:30:00+00:00` refuses to reach back across the session boundary, so the previous
session's bars no longer pad the average. Good.

But the runner started anyway and accumulated bars live. It evaluated 53 times. Evaluations 1
through 52 produced **zero** signals. Evaluation 53 produced the day's only signal.

Each evaluation closes one bar per symbol, so by evaluation 53 each symbol held roughly 53 bars
against a `slow_period` of 50. **The first moment SMA(50) was computable at all is within about
two bars of the moment it fired.** That is the same shape as day 2's F8 — where the day's entire
profit came from the one trade priced against a slow average stitched across a four-day
weekend. Two sessions, two trades that matter, both taken at the exact point the indicator was
least defined.

`runner.warmup_short_history` fired **520 times** (26 boots × 20 symbols), every one
`have=0 needed=51`. The platform said, loudly and 520 times, that it did not have the history
its strategy needs. It traded anyway.

**Fix.** `warmup_short_history` should gate, not narrate. A symbol whose series is shorter than
the strategy's declared lookback is not tradeable — exclude it from evaluation until it is, and
say how many symbols are held back. Combined with B1's sleep, this is the difference between
"the strategy had 53 bars" and "the strategy had 53 bars, 21 minutes of which were backfilled
after the host woke up".

### F3 — The book's equity was right by coincidence and its positions were wrong all day `high`

The platform's stored book never changed: `cash=100090.06`, `positions=[]`, on all 131 boots.
The broker's book, read 130 times, never changed either: `cash=95157.74`, MSFT 10.

Equity reconciles by accident. 95,157.74 + (10 × 493.232) = 100,090.04, against a claimed
100,090.06 — two cents apart. An operator or a dashboard checking *equity* would see a flat,
healthy day. The composition is completely wrong: the platform believes it holds 100% cash and
the venue holds a $4,932 equity position.

This matters beyond bookkeeping. Day 2's F2a found the dashboard renders `stop_loss_price` from
the armed level rather than the working order. Day 3 adds a second axis: a book that agrees on
the total and disagrees on every constituent. **Reconciliation caught it. The dashboard would
not have.**

### F4 — The operator cleared a halt three seconds into the outage that caused it `high`

```
14:27:45.160  staleness.detected silent_for=110.3s   → halted
14:27:48.xxx  GET  /api/v1/dashboard/live
14:27:49.xxx  POST /api/v1/risk/resume               ← halt cleared
14:28:00.188  staleness.recovered
14:28:01      GET  /api/v1/orders                    ← read AFTER clearing
14:28:02      GET  /api/v1/positions                 ← read AFTER clearing
```

The clear landed **12 seconds before** the data feed recovered, and the book was read *after*
the decision rather than before it. This is day 2's F13 recurring verbatim — same operator,
same ordering, same platform indifference. Day 2's recommendation was to *"require a fresh clean
reconcile before a reconciliation halt can be cleared"*; that fix has not landed, and a
staleness halt needs the equivalent: proof that data is flowing now, not a password.

Sixteen minutes later the single order this permitted became the day's blocker. That is not
causation — a correctly-warmed strategy would have been free to trade too — but it is the
sequence, and the platform accepted an assertion it could have checked in one call.

### F5 — BAC's adjusted history is known-corrupt, stored, and still traded `high`

```
worker.corporate_actions.inconsistent  symbol=BAC  bars_agreeing=386  bars_compared=1066
                                       consistent=False  factor=1.005136436597110754414125201
```

**36% of bars agree.** `corporate_actions.py:98` requires *all* compared bars to agree before
calling an adjustment consistent, so this is a series that moved in a way no single corporate
action explains. `scheduler.py:600` writes the incoming bars **before** `_report_adjustment`
runs, and the alert body says so plainly: *"The refreshed prices are stored; the cause is not a
split this can name."*

So the bar store now holds a BAC series the platform has formally declared it cannot explain,
and BAC remained in the 20-symbol universe for all 53 evaluations. CLAUDE.md §5 lists corporate
actions as one of the things that quietly corrupts a backtest; this is that, in the live store,
flagged and then ignored.

The alerting is right and the honesty is right. What is missing is a consequence. **Fix:**
quarantine an inconsistent series — refuse to trade the symbol and refuse to backtest on it —
until a human clears it, rather than storing it and sending one Telegram message into a stream
of 129 identical ones.

### F6 — The resume button failed three times without saying so `high`

```
21:38:25  POST /api/v1/risk/resume  → 401
21:38:29  POST /api/v1/risk/resume  → 401
21:38:37  POST /api/v1/risk/resume  → 401
21:39:47  GET  /api/v1/dashboard/live → 401   (the dashboard notices at last)
21:40:06  POST /api/v1/auth/login → 401       (wrong password)
21:40:17  POST /api/v1/auth/login → 200
21:40:44  GET  /api/v1/dashboard/live → 200
   —      resume is never retried. Capture ends 21 seconds later.
```

The operator's session had expired. They pressed resume three times in twelve seconds against a
dead token and got no feedback that would distinguish "rejected" from "nothing happened". The
UI only surfaced the problem 70 seconds later, when a *read* also 401'd. By the time they had
logged back in, they did not press it again.

Day 2's F12 recorded *"a 22-minute burst of 401s [...] the session expired while the dashboard
kept refetching, and nothing told the operator until they looked"*, filed as a papercut with
"no trading impact". On day 3 the same papercut sits on the single control that would have
ended the incident.

**Fix.** A 401 on a mutating request must raise a visible, blocking error, and the client must
re-authenticate before a control that changes platform state is offered at all. A halt-clear
that silently no-ops is the worst failure mode this dashboard has.

### F7 — Nothing recorded the session, because the reports need the host awake `medium`

No `worker.session_summary` and no `worker.daily_report` exist for 2026-09-09. Both are
scheduled — 20:00 and 20:30 — and both fall inside host-dark windows (19:15:12→20:11:48 and
20:12:23→21:12:46). Day 3 has no end-of-day artifact at all.

`LOCAL_HOSTING.md:272` describes launchd's catch-up behaviour for jobs whose time passes during
sleep; the in-process scheduler has no equivalent. Its `next_due` is recomputed from the
current time on every boot, so a boot at 21:37 schedules `summarise_the_session` for
2026-09-10T20:00 and the 2026-09-09 summary is simply never produced.

**Fix.** A session summary is a report *about a period*, not an event at an instant. If the
scheduled time was missed, run it late and label it late. Day 2's F14 already found the daily
report's rolling window sweeping in rows from outside the day it describes; the same
period-versus-instant confusion produces both.

### F8 — The staleness monitor's diagnosis sends the operator to the wrong place `medium`

Four detections, reported as lost market data:

| Time | `silent_for_seconds` | Actually |
|---|---|---|
| 14:14:33 | 914.1 | Host asleep 13:59:14 → 14:14:37 |
| 14:27:45 | 110.3 | Host asleep 14:25:45 → 14:27:54 |
| 15:09:09 | 369.1 | Host asleep, worker crash-looping |
| 17:09:05 | 7145.2 | Host asleep 15:59:16 → 17:08:35 |

Each is arithmetically correct and diagnostically wrong. `data_feed_lost` names Alpaca; the
cause was the machine. Day 2's F10 found the mirror image — a reported 28-minute outage that
was 1.13 seconds of real downtime — and the fix proposed there (report the gap only once a
message has been seen) does not address this one.

**Fix.** Distinguish "no messages arrived" from "no time passed here". A monotonic clock
alongside the wall clock separates them: if the wall clock advanced 914 s and the monotonic
clock did not, the host slept and the message should say so. CLAUDE.md §5's warning about
wall-clock reads is exactly this failure, and `Clock` is the right place for it.

### F9 — `runner.restored_open_orders` re-adopts a filled order 131 times `medium`

Every boot logs `runner.restored_open_orders client_order_ids=['atp-e82c4109cc2c0c8886b0e0b5']
count=1`. That order filled completely at the venue at 14:44. The platform re-adopts it as
working on every restart because it never saw the terminal fill — the consumer that would have
booked it is the one that died.

It is benign here only because the worker never gets far enough to act on it. Had the guard in
B2 not fired first, the platform would have resumed with a working-order set containing an
order that no longer exists, which is the state day 2's F4 showed the reconciler halting on.

**Fix.** Reconcile the restored order set against the broker's open orders before adopting it,
not after. The data to do it is fetched three lines later.

### F10 — The new unprotected-position alert cannot fire on the crash path `high`

Day 2's F3 asked for `runner.position_unprotected` to reach a human. It was implemented:
`_announce_unprotected` (`runner.py:942-1010`) does dedup-by-symbol-set, a cooldown, and an
all-clear, and its docstring says plainly that this route did not exist before.

Its only caller is `_mark_broker_protection` (`runner.py:940`), whose only caller is `_persist`
(`runner.py:898`) — **step 6 of the `evaluate` loop.** The fill path never reaches it.

So on day 3: `runner.py:1676` logged the CRITICAL and re-raised; the process died before the
next `evaluate` pass; **the unprotected-position alert never fired.** An alert *was* delivered a
second later, but it was `_announce_death` from `main.py:487`, titled *"Worker stopping —
trade_updates ended"*. It names a dead task, not a naked position. The operator's phone was told
the worker had a problem. Nothing told them 10 MSFT were sitting at the venue with no stop.

`docs/FIRST_PAPER_RUN.md:246` already documents this exact hole: *"`reason="protective
submission raised"` is the CRITICAL variant: ... there is no next attempt, and the fact lives
only in this line."*

**Fix.** Alert from the fill path, not only from the persist path. The one variant of lost
protection that is guaranteed to have no next evaluation is the one variant that currently
depends on there being one.

### F11 — The F4 re-read was sized for a five-minute job and runs on the boot path `medium`

`execution.reconcile.rereading` fired **131 times**, each with `settle_seconds=2.0`. The guard
is right and its own docstring is honest about the cost (`reconciliation.py:326-328`): *"the
cost of the re-read is `settle_seconds` on a job that runs every five minutes."*

It is not a five-minute job here. `warmup` calls the same `reconcile`, so every boot pays 2.0 s
of sleep **plus a second complete `_compare`** — three more sequential broker REST calls — before
raising the error it was always going to raise. Against a divergence that cannot clear, that is
roughly 4–6 seconds of every 17-second boot cycle spent re-asking a question already answered.

Not a defect in F4. A note that a guard shared between a scheduled job and a start-up path needs
its cost sized against both.

### F12 — Operational papercuts `low`

- **`worker.stopped` at 11:08:42 reads `'signal received — shut down cleanly, nothing halted'`
  while a global halt from 10:26:34 was engaged** and was inherited by the very next boot. The
  message means "this shutdown halted nothing", but on the line immediately before a boot that
  logs `halted=True`, it reads as a claim about platform state and is false.
- **`execution.fees.settled` ran twice for the same $4.14** (11:08:45 and 13:32:55), each time
  reporting `cash=100090.06`. The idempotency holds — cash is unchanged and the intervening
  `fees.level` lines say *"the book already reflects every fee the venue has charged"* — so the
  day-3 fee fix works. But an event named `settled` that fires for an already-settled amount
  should be `fees.level`, or the two names mean nothing.
- **The nginx duplicate-`Date`-header warning is still on every WebSocket upgrade**, unchanged
  from day 2's F12.
- **Redis logged 485 lines** for a session with one order, mostly background saves. Its
  `1 changes in 3600 seconds` timer firing at the *end* of each dark window is, incidentally,
  the cleanest single proof that the host was asleep rather than the containers stopped.
- **The `queue` container is absent again**, as on day 2. Four containers logged; `arq` is
  either idle or not running, and the log cannot tell which.
- **The timeframe mismatch is settled, and it was settled by configuration.** `706cde2` makes a
  worker refuse to boot when a strategy's declared timeframe differs from the one it serves, and
  `sma_crossover` ships declaring `1d` against a `WorkerConfig` default of `1m` — a pairing that
  would refuse to start. It did not: `worker.config_loaded` carries
  `strategy_params={'timeframe': '1m'}` on all 131 boots, and every one of the 130 halts
  decodes to either the wash-trade exception (1) or the book-mismatch `ExecutionError` (129).
  **Zero `ConfigError` boots.** Day 2's F8 timeframe question was closed by declaring `1m`, which
  means the strategy is running 20/50-minute periods and its parameters have still never been
  tuned for that horizon.

---

## 5. The day's result

There isn't one.

| | |
|---|---|
| Signals | 1 |
| Orders submitted | 1 |
| Orders filled (platform-visible) | 1 partial, 4 of 10 shares |
| Orders filled (venue truth) | 1 complete, 10 shares @ 493.2320 |
| Round trips closed | **0** |
| Realised P&L | **$0.00** — nothing was closed |
| Fees | $4.14, all carried over from day 2 |
| Starting cash | $100,094.20 → $100,090.06 after day-2 fees settled |
| Platform's closing book | $100,090.06 cash, no positions |
| Venue's closing book | $95,157.74 cash, **10 MSFT** |

The strategy produced one signal in 53 evaluations across 74 minutes of a 390-minute session.
There is no sample here, and there would not be one even if the plumbing had held: with the
host awake for 20% of RTH and the slow average defined for the last two minutes of the run,
the strategy was never asked a question it could answer.

**The open position is the deliverable, and it is a liability.** 10 MSFT, entered at 493.2320,
$4,932.32 of exposure, no stop of any kind, held across the close. Whoever picks this up should
deal with that before anything else:

```sql
-- what the platform thinks it holds
select symbol, qty, avg_entry_price, stop_loss_price from positions;
-- expect: zero rows. The broker holds 10 MSFT.
```

The remedy is `docs/RUNBOOK.md`'s reconciliation procedure: compare the books, then
`adopt_broker_state()`, then decide whether to close MSFT or protect it. Adopting is what breaks
the restart loop; nothing else in the log will.

---

## 6. What worked

Worth recording, because five separate day-2 fixes landed and every one of them did its job.

- **B1 (tick rounding).** `Instrument.round_price` went from zero callers to three
  (`router.py:526-528` for the armed level, `router.py:1001-1009` inside `_on_tick`, applied
  before `RiskEngine.validate`), rounded conservatively per side. Zero sub-penny rejections. The
  stop that failed on day 3 failed Alpaca's *wash-trade* check, which means it passed price
  validation — it got further into the venue than any stop on day 2 did.
- **F3 (alert on lost protection).** The route was built and `position_unprotected` fired
  CRITICAL where day 2 was silent 85 times. It did not page for the naked position, because the
  alert hangs off the evaluate loop rather than the fill path (F10) — but the log line day 2
  found unrouted now has a route.
- **F4 (reconciler race).** `execution.reconcile.rereading — 'books disagree — re-reading once
  before halting (F4)'` ran 131 times with `settle_seconds=2.0`. Every divergence was real and
  survived the re-read, which is the correct outcome — the guard did not soften a true halt.
  Day 3 had no race to catch; see F11 for what it cost on the boot path.
- **F7 (seed tickers).** `corporate_actions.skipped_seed_symbols ZVZZT ZWZZT ZXZZT`, and the
  job completed — `series=60 adjustments=2` — where day 2's died on the first one.
- **F8 (warmup recency).** `not_before=2026-09-09T13:30:00` and 520 honest
  `warmup_short_history` warnings. The platform no longer stitches a moving average across a
  closure. It does still trade on the short series, which is F2.
- **Fee settlement** (`34da457`) carried day 2's $4.14 across the restart correctly and did not
  double-count it across 131 boots.
- **The kill switch failed closed** through 131 restarts, `allow_live_orders=False` on every
  one, and the `ready_while_halted` CRITICAL fired every time. No order reached the venue after
  the halt. CLAUDE.md §1.8's three locks held.

The safety design keeps being the strong part of this platform. What keeps failing is
everything around it.

---

## 7. Day-2 scorecard

| Day-2 item | Status on day 3 |
|---|---|
| B1 — off-tick stop prices | **Fixed.** Zero sub-penny rejections |
| F1 — daily report counts rejects as submissions | **Untested.** No daily report was produced (F7) |
| F2 — report cannot see unprotected positions | **Untested.** Same |
| F2a — dashboard shows armed stops as working | **Not fixed, and now worse** (F3) |
| F3 — CRITICALs produce no alerts | **Partly fixed.** Route built, overcorrected on volume (F1), and blind to the crash path (F10) |
| F4 — reconciler read-ordering race | **Fixed.** Re-read ran 131 times; mis-sized on the boot path (F11) |
| F4a — exit carve-out vs. transient impugnment | **Untested.** No exit was attempted |
| F4b — reconciler halt writes no audit row | **Not fixed.** The 14:44 halt has no row |
| F4c — halt reminder is RTH-gated | **Not fixed.** One reminder all day |
| F5 — `from_reason=None` | **Not fixed.** Still emitted at 14:44:52 |
| F6 — unprotected alert has empty reason | **Regressed.** The reason now doesn't reach a log line at all — the rejection raises instead (B2) |
| F7 — corporate actions die on seed tickers | **Fixed** |
| F8 — warmup straddles a closure | **Fixed**, both halves. New problem in its place (F2) |
| F9 — tape completeness unmeasured | **Not fixed**, and unmeasurable this session |
| F10 — false gap reporting | **Not fixed**, inverted (F8) |
| F13 — clearing a halt proves who, not what | **Not fixed.** Recurred verbatim (F4) |
| F15 — nothing scrapes the metrics | **Not fixed.** `metrics_serving` on all 131 boots, no scraper |

Six fixed, and every one of them confirmed working in production. That is a good ratio. The
problem is that day 3 found its blockers in the two places nobody had looked: the machine, and
the protection path that day 2's own closing section said had never been exercised.

---

## 8. Before day 4 runs

**Do not run day 4 until the first two are done. Nothing else on this list matters if the host
sleeps.**

1. **Stop the host sleeping, and verify it.** `sudo pmset -c disablesleep 1`; confirm with
   `pmset -g`; check `pmset -g log` after the session. If the machine is a laptop that leaves
   the desk, or runs on battery, `caffeinate` is not sufficient — take ADR 0021's own fallback
   and put the paper stack on a VPS. Two sessions have now been lost to this.
2. **Break the crash loop.** The one-line version: have `_route` catch `BrokerError` rather
   than only `OrderRejectedError` and `BrokerConnectionError`, so a venue refusal cannot escape
   a fill consumer. Then the real fixes, in order: stop treating `40310000` as proof of a
   buying-power refusal; don't submit protection while the parent order is still working; make
   the book-mismatch guard halt-and-stay-up instead of exit-and-restart; and give
   `adopt_broker_state` an operator entry point so the RUNBOOK's instruction is executable.
3. **Deal with the open MSFT position** before the stack comes back, or the first boot of day 4
   crashes exactly as the last 129 of day 3 did. `adopt_broker_state()`, then close or protect.
4. **Deduplicate alerts by key at the transport** (F1), and **alert on lost protection from the
   fill path** (F10). 129 identical pages about a dead task, and none about the naked position,
   is the wrong way round in both directions.
5. **Gate evaluation on warmup, don't just warn about it** (F2). A symbol with 53 of the 51
   bars its slow average needs is not warmed up in any useful sense.
6. **Make a 401 on a mutating request visible** (F6). The resume button must never silently
   do nothing.
7. **Quarantine an inconsistent corporate-action series** (F5) instead of storing it and
   trading it.
8. **Separate wall-clock silence from host sleep** (F8), and make the session summary run late
   rather than not at all (F7).

**The roadmap needs no change, and that is the point.** Phase 4's *Verifiable:* line is *"a
strategy trades the paper account for a week and reconciles clean"*. Day 3 reconciled dirty for
6 h 56 m and has not stopped. Phase 6 wants *"a week of unattended uptime"*; the stack was
unattended and down for 8.8 hours. Phase 4 stays `0 / 11` and Phase 5 stays `0 / 12`, correctly.

**The paper week's day count should reset.** Day 1 was void (blocked strategy). Day 2 was void
(no working stops). Day 3 is void twice over. Three sessions have produced 38 round trips, all
on day 2, all unprotected, and a single unclosed position. Calling tomorrow "day 4" implies
three days of evidence exist. They do not.

**One pattern, stated plainly.** Day 2's review closed by naming a recurring shape: guards that
exist, are correct, are documented, and are never called — *"What is missing is not judgement.
It is the call site."* Day 3's shape is different and worse. Every guard here **was** called,
and each one did precisely what it was written to do. The wash-trade rejection was caught by
the venue. The book divergence was caught by the reconciler. The short warmup was caught and
reported 520 times. The halt held through 131 restarts.

The platform detected everything and recovered from nothing. Each guard failed closed, in
isolation, correctly — and composed into a machine that shut itself down permanently on the
first partial fill of the day and paged a human 129 times about it. **Day 3's lesson is that
correct local failure handling is not a recovery story.** Somewhere above all these guards,
something has to be responsible for getting the platform back to a state where it can trade,
and right now nothing is.
