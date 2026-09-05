# Paper Week — Day 1 Review

**Session:** 2026-09-03 · **Log window:** 11:16:26Z → 21:23:51Z · **RTH:** 13:30–20:00Z
**Source:** 13,727 docker log records across 9 containers, cross-checked against the repository at `1119110`
**Config:** `sma_crossover`, 20 symbols, Alpaca paper, IEX feed, `run_mode=paper`

---

## 1. The verdict

**Day 1 is void as a paper-trading day.** The strategy was never asked for an opinion — not
once, in ten hours.

`apps/worker/src/atp_worker/trading.py:205` builds the `StrategyRunner` with a hard-coded
`timeframe=Timeframe.D1`. The ingestor writes **1-minute** bars. The bar repository filters
strictly on timeframe. So every 60 seconds the runner asked Postgres for the newest **daily**
bar, got back the same one it loaded at warmup, and returned an empty `just_closed` list.
`strategy.on_bar()` was invoked **zero times**. The 6,887 minute bars the platform ingested
went into a series the runner never reads.

This is structural, not statistical. On this wiring `sma_crossover` produces zero signals
every day of the week.

Three consequences follow:

1. **Nothing traded, and nothing could have.** No signal, no order, no fill, no risk
   evaluation. `RiskEngine.validate()` ran **0 times** all day.
2. **Nothing said so.** The strategy loop emits no log line per evaluation, and its only
   instrumentation — `atp_strategy_evaluations_total` — never exported, because
   `METRICS_TOKEN` was unset on all six boots. ~390 silent evaluations across the session.
3. **A global kill switch sat engaged for 2h37m**, covering the last 74 minutes of the
   session, cleared by hand at 21:23 — and the worker announced *"trading sma_crossover with
   paper money"* three times while it was engaged.

The safety machinery is sound. The wiring and the observability are not.

**Recommendation: hold day 2.** Fix the two blockers in §3 and the heartbeat in §4, verify
with one SQL query, then re-run day 1. Re-running as-is produces another day of zeros.

---

## 2. Timeline

| Time (UTC) | Event |
|---|---|
| 11:16:26 | All three images built (cached, ~1s). No later rebuild all day |
| 11:18:14 | **Start #1.** config rev 2, `sizing=fixed_qty 1`. `worker.metrics_disabled` |
| 11:18:16 | `runner.warmed_up bars=1020` — 51 **daily** seed bars × 20 symbols |
| 12:11–12:52 | 5 stream + 5 trade-update disconnects, all recovering in ~1s |
| 12:13:30 | Operator logs in — the only login all day |
| 12:30:00 | `apply_corporate_actions` → **not implemented**, marked dormant |
| 13:25:00 | `rollover_daily_counters` → **not implemented**, marked dormant |
| 13:30:00 | **Market open** |
| 13:52:42 | `PUT /api/v1/worker/config` → revision 3 |
| 13:53:04 | **Full-stack bounce during RTH** — postgres, redis, queue, worker all SIGTERM'd |
| 13:53:11 | **Start #2.** Revision 3 changed *nothing* in any logged field |
| 14:36:23 | `PUT /api/v1/worker/config` → revision 4 (`sizing` → `risk_pct 0.0015`) |
| 14:36:43 | **Start #3** |
| 14:40:16 | **Operator's last request. Nobody watches for the next 6h42m** |
| 18:44:00 | Last bar before the outage (18 bars that minute) |
| 18:45:22 | Trade-updates socket drops; data stream drops at 18:45:36 |
| 18:46:03 | `data.staleness.detected` 64.4s → **`risk.killswitch.engaged` (global)** |
| 18:46:04 | `alert.sent` telegram — the only *critical* alert of the day |
| 18:46:03 | Dashboard receives the push and refetches. Nobody is at the screen |
| 18:49:17 | **Crash #1** — trade_updates, 8 attempts exhausted |
| 18:50:30 | **Crash #2** — strategy_runner, `GET /v2/positions`, 5 attempts |
| 18:51:56 | **Crash #3** — same |
| 18:52:20 | **Start #4.** Alpaca reachable again. Backfill recovers *one minute* of seven |
| 20:00:34 | Market close. Runner sleeps 62,965 s until 2026-09-04T13:30 |
| 20:30:00 | `generate_daily_report` → **not implemented** |
| 21:22:07 | Operator returns |
| 21:23:16 | **`risk.killswitch.cleared`** `was_engaged=True` — 2h37m13s after it engaged |

Only **4** `runner.warmed_up` for **6** `worker.starting`: two boots died before warming up.
Process downtime per crash was ~0.73 s; the *strategy runner* was absent
18:49:17.389 → 18:52:20.162 = **2m52.8s**.

---

## 3. The two blockers

### B1 — The runner reads a bar series nothing writes `critical`

**Evidence chain, all verified:**

| File | Line | What it says |
|---|---|---|
| `apps/worker/src/atp_worker/trading.py` | 205 | `timeframe=Timeframe.D1,` — a literal, no config behind it |
| `apps/worker/src/atp_worker/main.py` | 232 | `StreamIngestor(...)` constructed with no `bar_timeframe` |
| `libs/core/src/atp_core/data/stream.py` | 104 | `bar_timeframe: Timeframe = Timeframe.M1` — the default |
| `libs/core/src/atp_core/persistence/bars.py` | 163 | `.where(..., BarRow.timeframe == timeframe.value)` — strict filter |
| `apps/worker/src/atp_worker/runner.py` | 687, 692 | polls `self.timeframe`; skips when `bar.ts <= held[-1].ts` |
| `apps/worker/src/atp_worker/runner.py` | 856 | `_poll_strategy` iterates `just_closed` — always empty |

**Log corroboration.** `runner.warmed_up bars=1020 symbols=20` is exactly 20 × 51
(`SmaCrossover.warmup_bars = slow_period + 1`) and is **identical on all four warmups** —
11:18:16, 13:53:14, 14:36:45 and 18:52:20 — including the last, after five hours of live
minute bars had accumulated. Daily seed data does not move during a session. That constancy
is the fingerprint.

Every `data.alpaca.bars_fetched`, every `data.backfill.*` and all six `data.stream.started`
lines carry `timeframe=1m`. Zero daily bars were written all day.

**`WorkerConfig` has no `timeframe` field**, so no dashboard edit can correct this.

**Zero is a bug, not a market outcome.** A Monte Carlo over 2,000 driftless GBM paths of 390
one-minute bars gives a mean of **7.65** SMA(20)/SMA(50) crossings per symbol per session
(median 8; zero occurrences of zero). At the observed 342 bars: mean 6.56, again 0/2000.
P(zero across 20 symbols) < 1e-20. *(Model estimate, labelled inference — not market data.)*

**Confirm before day 2, one query:**
```sql
select timeframe, count(*), max(ts) from bars group by 1 order by 1;
-- expect: 1m with max(ts) at today's close, 1d with max(ts) before today
```

**Fix.** Add a `timeframe` field to `WorkerConfig`, thread it into **both**
`build_runner`'s `StrategyRunner(timeframe=...)` and `StreamIngestor(bar_timeframe=...)`
from the same value so writer and reader cannot disagree, and surface it in
`worker.config_loaded`. Add a boot-time check that the newest stored bar in the runner's
timeframe is younger than one bar-interval during RTH.

---

### B2 — A market entry into a flat symbol cannot be priced `high`

**Fixing B1 alone still produces zero orders.**

`OrderRouter._size` prices every entry with `reference_price(signal.symbol, portfolio,
signal.limit_price)`: the limit price if present, else `position.last_price`, else `None`.
`sma_crossover` emits a **market** `ENTER_LONG` (no limit price) and only when
`position.is_flat`.

Live, nothing ever sets `last_price` on a flat symbol: `StrategyRunner._mark` builds its
symbol list from `portfolio.open_positions`. A flat symbol has no open position, so it is
never marked, so the entry cannot be priced — a wall of SIZING refusals rather than orders.

**Fix.** Mark the whole configured watchlist, not just open positions. `_mark` should set
`portfolio.position(symbol).last_price` for every symbol in `self.symbols` from the quote
cache — the runner already fetches the full watchlist's quotes for the snapshot
(`runner.py:656`).

---

## 4. Findings

### F1 — The strategy loop is unobservable `high`

`_evaluate_once` increments `self.stats.evaluations` and logs nothing on the success path.
Its only export is the Prometheus counter `atp_strategy_evaluations_total`, and the metrics
server refused to start on all six boots (`worker.metrics_disabled`, `METRICS_TOKEN` unset;
`startup.no_metrics_token` on the API at 13:53:11.662). `RunnerStats` is not in the
dashboard snapshot either. `engine_tick_interval_seconds` defaults to 60, so roughly **390
evaluations ran across the session and emitted nothing**.

`runner.signal_refused` — **0 occurrences** in 13,727 records, confirming no signal ever
reached the router. `runner.evaluation_failed` — also 0, so the loop never raised.

Worse: `atp_halt_active` (`apps/api/src/atp_api/routers/metrics.py:87`) — the one continuous
halt signal, and the number `docs/RUNBOOK.md:61` tells the operator to check — was
uncollectable for the entire 2h37m. Zero requests to `/metrics` all day.

**Fix.** Set `METRICS_TOKEN`, and — independently, because metrics can always be down — emit
one line per evaluation:
`log.info("runner.evaluated", evaluations=…, bars_closed=…, signals=…, refused=…)`.

---

### F2 — The engine-side stop fallback is unreachable `critical`

`StrategyRunner._exit_reason` short-circuits on the **static** `stop_config.broker_side`
flag rather than asking the router what is actually protected. A position whose broker-side
stop was *refused* therefore has no stop at all — neither broker-resident nor engine-managed.

Untested on day 1 because the book was empty throughout. It fires the first time a
protective-stop submission is rejected.

---

### F3 — The kill switch has no exit carve-out `high`

`KillSwitchRule` refuses **everything** with no exemption for closing trades. During those
2h37m, every flatten, every take-profit exit and every protective-stop child order would
also have been denied.

The book was empty all day (`positions=[]` at every `worker.restored_book`, `open_orders=0
positions=0` at every reconcile), so exposure was **exactly zero**. With an open book, a
data-feed halt would have frozen the platform's ability to *reduce* risk while leaving the
position on.

---

### F4 — The worker never reads halt state at boot `high`

`worker.halted` → restart → `worker.ready ... 'trading sma_crossover with paper money'`,
three times, while globally halted. There is no `is_engaged` or `active_halts` call anywhere
in the boot path.

**This is an observability defect, not a safety hole.** Every order reaches the venue only
through `OrderRouter._route` → `RiskEngine.validate` → `KillSwitchRule`
(`router.py:892`, `engine.py:241`), which reads Redis per order and **fails closed**. The
restarted worker would not have traded through the halt — it simply had no idea it was halted
and said the opposite, at INFO, three times.

`preflight.check_not_halted` exists and is exactly the right check. Its only caller is
`scripts/preflight.py`, not the worker boot path.

**Fix.** Before `worker.ready`, call `kill_switch.active_halts()`, fold the result into the
`worker.ready` line, and log a CRITICAL when it is non-empty.

---

### F5 — Six minutes of market data were lost, and reported as recovered `high`

Bars upserted per minute across the outage:

```
18:41→19   18:42→19   18:43→18   18:44→18
18:45→0    18:46→0    18:47→0    18:48→0    18:49→0    18:50→0    18:51→0
18:52→1    18:53→15   18:54→17
```

The recovery backfill at 18:52:26:
```
data.backfill.window_done  bars_written=16 start=18:51:00 end=18:52:00
data.stream.reconnected    attempts=3 gap_seconds=23.316 gap_since=18:51:57.056005
```

`gap_since` is the **current process's** stream-start time. Worker #4 began at 18:51:56.9
(`data.staleness.watching` at 18:51:57.059 — a 3 ms match). Each crash reset the gap origin,
so the fourth worker believed the gap was 23 seconds rather than 8 minutes and asked for a
one-minute window. The same mechanism is visible benignly at 12:11:04, where
`gap_since=11:18:15.943515` is exactly the first process's `stream_subscribed` timestamp.

~108 bars are permanently absent. `data.stream.backfill_failed`, `backfill_skipped` and
`backfill_truncated` all exist in the code and **none fired** — the backfill succeeded by its
own definition. `backfill_missing_bars`, the sweep that would repair this, is scheduled for
2026-09-04T02:00 and never ran on day 1.

**Fix.** Derive the gap window from the last bar actually in storage. A restart must not be
able to shrink a gap.

---

### F6 — The three crashes were self-inflicted `high`

Alpaca was unreachable ~18:45:22 → 18:52:19 (**~7 minutes**). Both retry budgets expire
sooner, so the worker killed itself, restarted, reset the budget, and died again.

Trade-updates websocket (8 attempts, disconnect 18:45:22):
```
1 → 18:45:32.3   2 → 18:45:43.2   3 → 18:45:54.9   4 → 18:46:07.9
5 → 18:46:23.8   6 → 18:46:46.4   7 → 18:47:14.1   8 → 18:48:18.5   gave up 18:49:17.4
```
Total budget **235 s (3m55s)** — intervals are ~10 s handshake timeout plus a roughly
doubling backoff. The shape is right; the ceiling is not.

`GET /v2/positions` (5 attempts): 18:47:59.9 → 18:49:06.9 = **67 s**.

**Consequence.** Three process deaths, three `runner.stopped positions_left_open=True`, and
three destroyed gap markers — the direct cause of F5.

**Fix.** Cap per-attempt backoff (~30 s) and bound the budget by **elapsed time**, not
attempt count, so it survives a 10–15 minute venue outage. Add jitter.

---

### F7 — A crash-looping worker cannot halt itself `high`

`data.staleness.watching` restarts its `max_silence_seconds` clock at process start:

```
18:49:18.272  data.staleness.watching     (start #4)
18:50:18.391  data.staleness.detected     silent_for_seconds=60.1   ← exactly 60 s later
18:50:31.241  data.staleness.watching     (start #5)
18:51:31.355  data.staleness.detected     silent_for_seconds=60.1   ← exactly 60 s later
```

A worker that dies faster than `max_silence_seconds` **never halts at all**. Today the halt
engaged only because worker #1 survived long enough to notice.

Compounding it: **`data.staleness.recovered` is declared in the code and never fired.** Data
resumed at 18:52:26 and nothing observed it.

**Fix.** Seed the staleness clock from the last bar timestamp in storage. Emit and alert on
`data.staleness.recovered`.

---

### F8 — Nothing repeated the halt for 2h37m `high`

Two `alert.sent` in the whole day:
- 18:46:04.750 · worker · `key=halt.global.all.data_feed_lost` · **critical** · telegram
- 21:23:17.488 · api · `key=halt.global.all.cleared` · **info** · after the operator acted

Nothing alerted on `worker.halted` (×3), `worker.responsibility_ended` (×3),
`execution.reconcile.broker_unreachable` (×3), `worker.scheduler.job_failed`, or the crash
loop. Three process deaths in 158 seconds produced **zero** alerts.

**The mechanism matters:** the "still halted" reminder is **per-process state that the crash
loop destroyed**, and the one continuous halt signal — the `atp_halt_active` metric — was
uncollectable (F1). Both escalation paths failed for the same underlying reason.

Six `KillSwitch.engage()` calls fired during the incident (3 from `StalenessMonitor._halt`,
3 from `main._halt`) and correctly collapsed to **one** halt record and **one** alert — the
dedup at `killswitch.py:214-216` held exactly as designed.

**Fix.** Make the halt reminder durable (it is already in Redis). Alert on `worker.halted`.
Add an end-of-session summary alert — it would have said *"0 orders, halted for 74 minutes"*
at 20:00.

---

### F9 — `scripts/halt.py` clears a halt with no password and no audit row `high`

Exactly two callers of `KillSwitch.clear` exist: `apps/api/src/atp_api/routers/risk.py:777`
and `scripts/halt.py:132`. Through the dashboard, resume is properly gated. The shell path is
not — and the RUNBOOK says otherwise.

Related: **the audit trail records the resume but not the halt**, and carries no
`correlation_id`, so the incident cannot be joined end to end from the audit log. `GET
/api/v1/audit` was hit twice all day, both times before the incident (12:13:45 and 14:40:13).

---

### F10 — Three scheduled jobs are stubs that go dormant after one attempt `medium`

| Job | Started | Done | Status |
|---|---|---|---|
| `reconcile_with_broker` | 76 | 75 | 1 failed at 18:49:06 (broker unreachable) |
| `apply_corporate_actions` | 1 | 0 | `raise NotImplementedError` · 12:30 |
| `rollover_daily_counters` | 1 | 0 | `raise NotImplementedError` · 13:25 |
| `generate_daily_report` | 1 | 0 | `raise NotImplementedError` · 20:30 |
| `backfill_missing_bars` | 0 | 0 | due 2026-09-04T02:00 — never ran on day 1 |

The driver marks each dormant after its first attempt (`scheduler.py:310-313`), so they never
retry. `rollover_daily_counters` is the reset for the daily-loss and order-rate counters — a
real-money guardrail that does not exist. The queue container hosts a matching
`generate_report_task` which is *also* a stub, so the report has two unimplemented halves.

`apply_corporate_actions` matters more than it looks: a strategy reading **daily** bars is
exactly the one an unapplied split corrupts.

**Roadmap check (CLAUDE.md §6): the roadmap is honest.** Phase 4 at **0/11**, Phase 5 at
**0/12**, Phase 4's *Verifiable:* line is "a week of paper trading." Nothing falsely ticked.

---

### F11 — Sizing is not survivable on the timeframe you intend to trade `medium`

`position_size` for `risk_pct` is `equity × risk_pct / |entry − stop|`, floored ROUND_DOWN
(`risk/rules.py:529`), with the stop derived as 2 × ATR(14).

At $100,000 equity, `risk_pct 0.0015` = **$150 risk per trade**. On **daily** bars — what
actually ran — SPY's ATR(14) is ~$5, stop distance ~$10, order ~15 shares ≈ $8,250 ≈ 8.25% of
equity, just under the 10% `max_position_pct` ceiling. On **1-minute** bars a minute-scale ATR
makes the same formula ask for roughly **165% of equity**, refused wholesale.

Revision 4's sizing row is only survivable *because* of the B1 bug. Re-derive it for the
timeframe you actually choose. Sizing could not have floored to zero either way — that needs
2 × ATR > $150, i.e. ATR > $75.

---

### F12 — A full-stack restart during market hours, for a config change that changed nothing `medium`

At 13:53:04 — 23 minutes after the open — postgres logged `received fast shutdown request`
with 8 `FATAL: terminating connection due to administrator command`, redis logged `Received
SIGTERM`, and the queue logged `shutdown on SIGTERM`. All back up by 13:53:11.

Diffing all six `worker.config_loaded` lines: rev 2 → 3 changed **no logged field at all**;
rev 3 → 4 changed `sizing` only. `worker.config_loaded` does not log the whole config, so an
operator cannot tell from the log what a revision changed.

---

### F13 — The feed is structurally thin `medium`

RTH coverage **6,830 of 7,800 expected bars (87.6%)**. Rows per RTH minute:

| bars in the minute | 1 | 14 | 15 | 16 | 17 | 18 | 19 | 20 |
|---|---|---|---|---|---|---|---|---|
| minutes | 2 | 5 | 18 | 43 | 70 | **106** | 93 | 46 |

Median 18 of 20; only 46 of 390 minutes complete. The **only** zero-bar RTH minutes are
18:45–18:51 — every other shortfall is per-symbol sparseness, which is what `feed=iex`
produces: a symbol with no IEX print in a minute yields no bar.

Not a bug, but it changes what the paper week measures. Decide IEX or SIP deliberately and
record it in an ADR.

---

### F14 — `no_action` inflates the rejection counter it was written to protect `low`

`SubmitResult.no_action` builds an *approved* decision specifically so a HOLD-shaped outcome
does not inflate `RunnerStats.orders_rejected_by_risk` — but it sets `submitted=False`, and
`_submit` increments that exact counter on `if not result.submitted`. An exit signal on an
already-flat position is counted as a risk rejection.

**Fix.** Gate the counter on `result.decision.approved` being False, not on `submitted`.

---

## 5. What worked

- **The kill switch is durably correct.** Redis key `atp:halt:global`, **no TTL**
  (`killswitch.py:158-163, 211-224`), on an `--appendonly yes` volume-backed Redis. Nothing
  auto-clears it — exactly two callers of `clear()` exist, both requiring a human, no timer,
  no restart path. `is_engaged` **fails closed** on a Redis error. `was_engaged=True` at the
  21:23 clear proves it survived all three crashes.
- **The halt chain fired correctly and in order:** detect (64.4 s) → engage global → alert →
  halt, in 1.0 s. Six `engage()` calls collapsed to one record and one alert.
- **Halt enforcement is correct in code** — per-order, fails closed — though day 1 never
  observed it working.
- **The dashboard push path works.** 15 ms after the halt the browser refetched
  `/api/v1/dashboard/live`. The WebSocket client stayed connected 14:40 → 21:22.
- **`allow_live_orders=False` did not cause the silence.** It is read as `if settings.is_live
  and not config.allow_live_orders` (`trading.py:137`) — inert in paper, exactly as
  `WorkerConfig`'s own comment says. Leave it off.
- **Reconciliation ran 76 times and never disagreed.** 85 `execution.reconcile.clean` = 75
  scheduled + 4 warmups + 6 trade-update reconnects — correct by design.
- **The stack was clean.** Postgres 101/101 checkpoints. Redis 404 saves, 34 AOF rewrites, no
  evictions. One login, three benign 401s at 12:13:21, no other non-2xx all day.
- **Money rendering is Decimal throughout** — `cash=100000.00000000`. No float artifacts.
- **Real-money exposure during the incident was exactly zero.** The book was empty at every
  checkpoint.

---

## 6. The silence census

103 of the 157 structlog event names declared in the codebase never appeared. Most are error
paths that correctly stayed quiet. These are the ones that matter:

| Never emitted | What it tells us |
|---|---|
| `order.submitted`, `order.filled_qty`, `order.protective_stop_placed`, `execution.trade_update.filled`, `runner.stop_triggered` | The entire order lifecycle is untouched |
| `runner.signal_refused`, `order.risk_denied` | No signal reached the router; `RiskEngine.validate()` ran 0 times |
| `data.staleness.recovered` | Nothing noticed the feed came back (F7) |
| `worker.backfill_missing_bars.*` | The gap-repair sweep never ran (F5) |
| `data.stream.backfill_failed` / `_skipped` / `_truncated` | The truncated backfill reported success (F5) |
| `worker.metrics_serving` | Metrics off all day (F1) |
| `runner.warmup_short_history`, `worker.not_trading`, `execution.reconcile.mismatch` | Good news: warmup was complete, trading was armed, the book never diverged |

Eight of the nine default risk rules — position size, gross exposure, daily loss,
orders/minute, open positions, quote age, trading hours, buying power — have never seen an
order in production. The configured ceilings are untested numbers.

---

## 7. Before day 2 runs

> **Status.** The four P0 items are fixed in code, and so are **F3** and **F9**
> from the P1 list. The sections above are left as they were written — they
> describe what day 1 did, and that does not change. `METRICS_TOKEN` in item 3 is
> the one part no commit can discharge: it is a deployment secret, and it still
> has to be set on the host before day 2. The rest of P1, and all of P2, are
> open.
>
> **F3** took the first of the two options it offered. `KillSwitchRule` now
> permits an order that can only make a holding smaller — a flatten, a
> take-profit exit, a protective stop — and still refuses one that would reverse
> a position, which would be new risk taken while the platform is stopped.
> docs/SAFETY.md's own incident response is what decided it: "Halting stops new
> risk; flattening realises existing P&L." Refusing exits did both. The
> data-outage half of that sentence is unaffected — `stale_data` refuses every
> order including exits, on the same chain, so a feed halt still cannot dump the
> book into a market nobody can see.
>
> **F4 through F8 are fixed too**, and they are one causal chain rather than
> five findings: F6 (both retry budgets expired in about four minutes against a
> seven-minute venue outage) caused the three process deaths, and those deaths
> caused F5 (each restart reset the feed's gap origin), F7 (each restart reset
> the staleness clock) and half of F8 (the escalation state was per-process).
>
> **F6.** Both streams are now bounded by elapsed time — fifteen minutes — in
> place of an attempt ceiling, and the per-attempt cap is halved to 30s so a long
> outage is retried more often rather than less. Each has a regression test that
> plays the day-1 outage: seven minutes of failures, then the venue returns, and
> nothing raises.
>
> **F5.** The reconnect window now starts at the earlier of the feed's gap
> marker and the last bar in storage, and says so as
> `data.stream.gap_widened_from_storage` when the two disagree. A restart cannot
> shrink a gap any more. `FakeBarRepo` answered every read with "no bars", which
> is why no test caught this: a fake that cannot express the failure cannot catch
> it, so it now holds bars.
>
> **F7.** `connected_since` is demoted from a peer of `last_message_at` to the
> fallback of last resort. The baseline is the storage watermark, which a restart
> cannot reset — so a worker that dies inside `max_silence_seconds` now still
> reports the outage it was born into. `data.staleness.recovered` also reaches a
> phone now, and says that the halt it engaged is still engaged, because a
> CRITICAL followed by silence cannot be told from "fixed itself, waiting for
> you".
>
> **F4.** `worker.ready` reads `active_halts()` first, carries `halted`, and is
> followed by a CRITICAL naming every scope and the command that clears it.
> Still an observability fix and not a safety one — every order passed
> `KillSwitchRule` throughout — but it is the line an operator believes.
>
> **F8.** The halt reminder is a scheduler job reading Redis every fifteen
> minutes in session, so the crash loop that destroyed the old per-process state
> cannot suppress it. A worker responsibility dying now alerts on its own key,
> because the halt's alert is deduplicated by design and the second and third
> deaths reached nobody. And the session closes with a summary — the message
> worth sending precisely when nothing happened, since nothing happening is
> indistinguishable from working perfectly until somebody says so.
>
> What is **not** fixed here: `backfill_missing_bars` still has to run to repair
> the ~108 bars day 1 lost, and the `METRICS_TOKEN` in item 3 remains a
> deployment secret no commit can discharge.
>
> **F10 through F14 are fixed**, and two of them not as written — the review was
> cross-checked against `1119110` and three things it named have since been
> built, so verifying first was most of the work.
>
> **F10.** Two of `rollover_daily_counters`'s three clauses were already being
> done: the runner re-runs `warmup()` at every open and anchors the daily-loss
> baseline there, and the rate-limit "counter" is a trailing 60-second deque that
> prunes itself on every read. Filling it in as written would have re-anchored a
> second time from a job, which `anchor_session` names as the mistake that grants
> a drawn-down day a second allowance. The third clause could not be built
> because **nothing ever engaged a daily-loss halt to clear** — and that turned
> out to be the real finding here. docs/RISK.md has always said the kill switch
> "auto-engages on: daily loss limit breach, ... a rate-limit storm", and both
> were `HaltReason` values with no writer. `StrategyRunner._escalate` now writes
> them, and the rollover releases the daily-loss halt at the next open, narrowly:
> that reason only, engaged by the risk chain only, from a previous session only.
> `apply_corporate_actions` and `generate_daily_report` are still stubs and still
> dormant — see below.
>
> **F11.** Already fixed, by `preflight.check_sizing_is_reachable`, which prices
> the first entry off the *configured* timeframe's bars and fails when it would
> breach the position cap. Its fix message had a bug in exactly this finding's
> case, though: the fraction that fits on a minute series is about 0.000036, and
> `:.4f` rendered that as `about 0.0000 fits` — an operator following the hint
> would enter 0, which `position_size` refuses outright. Below one basis point it
> now says the timeframe is the thing to re-decide, which is what this finding
> concluded.
>
> **F12.** The diff was already computed on the save path and went only to the
> audit table, so `docker compose logs` could not answer "what did revision 3
> change". It is logged now, and a revision that changed *nothing* — day 1's rev
> 2 → 3, which cost a full-stack restart 23 minutes into the session — says so on
> its own line. The worker cannot close this end: the configuration is a single
> upserted row with no history, so a worker has nothing to diff against.
>
> **F13.** ADR 0026. The week runs on IEX deliberately, and 87.6% RTH coverage is
> recorded as the baseline rather than as an incident — a symbol with no IEX
> print in a minute yields no bar, which is the feed answering the question it
> was asked. What that costs is written down: the week can prove the *platform*
> and cannot prove the *strategy*, because signal timing and fill quality both
> need a complete tape.
>
> **F14.** Fixed, one condition. `no_action` carries an approved decision and is
> no longer counted as a risk rejection, nor escalated.
>
> **`METRICS_TOKEN` now has a check.** It was set on the host after day 1, and
> `scripts/preflight.py` — the command that answers "is this configuration ready
> to spend a week trading paper?" — did not ask about the one setting whose
> absence caused F1. It warns rather than fails: metrics are not a safety layer,
> and a platform with no scraper still halts, still refuses and still alerts.
>
> **Still open after all of this:** `apply_corporate_actions` and
> `generate_daily_report` remain stubs (the first matters more than it looks for
> a daily-bar strategy, per F10 above), and the ~108 bars day 1 lost still need
> `backfill_missing_bars` to actually run.
>
> **F9** is gated and recorded. `scripts/halt.py clear` now prompts for the
> account password and checks it against the same hash `POST /risk/resume` does,
> so docs/RUNBOOK.md's "clearing asks for the password, wherever you do it" is
> true rather than aspirational. Both commands write an audit row, best-effort:
> the halt is attributed to the script rather than to `--by`, because nothing
> authenticated that name and ADR 0008 keeps unverified names out of the `actor`
> column; the resume may name the operator account, because the password proved
> one. Every row carries the `correlation_id` of its run, and a resume carries
> the `engaged_at` of the halt it ended — the two joins the finding said were
> missing. The risk layer's own automated triggers still write no row, which is
> the remaining half of the gap and is not this change.

**P0 — blocking**

1. **B1** — add `timeframe` to `WorkerConfig`; thread it into both `StrategyRunner` and
   `StreamIngestor` from one value. Verify with the SQL query in §3.
2. **B2** — mark the whole watchlist in `_mark`, not just open positions.
3. **F1** — set `METRICS_TOKEN` and add a per-evaluation `runner.evaluated` line.
4. **F2** — make `_exit_reason` ask the router what is actually protected.

**P1 — this week**

5. ~~**F5** — derive the reconnect gap from the last stored bar.~~ **Done** — the
   earlier of the feed's marker and storage wins, and a disagreement is logged.
6. ~~**F6** — bound retry budgets by elapsed time; survive 10–15 minutes.~~
   **Done** — 15 minutes of elapsed time on both streams, per-attempt cap 30s.
7. ~~**F7** — seed the staleness clock from the last bar; emit `staleness.recovered`.~~
   **Done** — and the all-clear alerts, saying the halt is still engaged.
8. ~~**F4/F8** — read halt state at boot; make the halt reminder durable; alert on
   `worker.halted`; add an end-of-session summary.~~ **Done** — all four.
9. ~~**F3** — give `KillSwitchRule` an exit carve-out, or state explicitly that a halt freezes
   exits too.~~ **Done** — carve-out, narrowed to orders that cannot reverse a position.
10. ~~**F9** — gate `scripts/halt.py --clear` behind the same checks as the API, and audit it.~~
    **Done** — step-up password on `clear`, audit rows on both halves, correlation ids on each.
11. ~~**F10** — implement `rollover_daily_counters` (a risk guardrail), then the daily report.~~
    **Partly done** — the rollover is real, and the guardrail it needed (nothing
    engaged a daily-loss halt) is built. The daily report is still a stub.

**P2 — decide and document**

12. ~~**F11** — re-derive the sizing row for the timeframe you actually trade.~~
    **Done** — `preflight.check_sizing_is_reachable` already caught it; its fix
    message could not express this case and now can.
13. ~~**F13** — IEX vs SIP, in an ADR.~~ **Done** — ADR 0026, and the week is IEX.
14. ~~**F12** — no mid-session restarts; log which fields a config revision changed.~~
    **Done** for the logging half. "No mid-session restarts" is an operating
    practice, not a change: docs/RUNBOOK.md says it.
15. Someone watches during RTH, or the alerting in (8) substitutes for a human. Day 1 had
    neither for the last 5h20m.

---

*13,727 records from `dockerlogs20260903T212403.606Z_day_1.csv`, cross-checked against the
repository at `1119110`. Two independent analyses converged on B1 with the same evidence
chain.*
