# Paper Week — Day 2 Review

**Session:** 2026-09-08 · **Log window:** 11:16:47Z → 21:39:07Z · **RTH:** 13:30–20:00Z
**Source:** 14,989 docker log records across 5 containers, cross-checked against the repository at `e288351`
**Config:** `sma_crossover`, 20 symbols, Alpaca paper, IEX feed, `run_mode=paper`
**Predecessor:** `day-1-review.md` (2026-09-03, void). 43 commits landed between the two sessions.

---

## 1. The verdict

**Day 2 traded. Day 2 was never protected.**

Day 1's blocker is fixed: the strategy evaluated 386 times, produced 97 signals, and the
platform turned them into 76 orders that all filled. The machinery ran end to end for the
first time.

And in ten hours it did not place a single working stop-loss.

Every one of the 85 protective stop orders the platform tried to place was rejected by
Alpaca, all 85 with the identical error:

```
invalid stop_price 88.21881579149739452.
sub-penny increment does not fulfill minimum pricing criteria
```

`libs/core/src/atp_core/risk/stops.py:141` computes an ATR stop as
`level = entry_price + direction * multiplier * atr_value`. The ATR arrives from
`runner.py:1321` as `Decimal(str(numpy_float))` — seventeen significant digits.
`execution/router.py:895` passes the result through untouched as `stop_price=level`.
`brokers/alpaca.py:838` sends `str(order.stop_price)`. Nothing rounds it to the penny.

The rejected price reproduces exactly:

```python
>>> Decimal('88.39') - Decimal(2) * Decimal(str(0.08559210425130274))
Decimal('88.21881579149739452')      # byte-for-byte what Alpaca refused
```

The function that would have fixed this already exists:

```python
# libs/core/src/atp_core/domain/market.py:43
def round_price(self, price: Decimal) -> Decimal:
    """Snap to the venue tick. Sending an off-tick price is a rejection."""
    return (price / self.tick_size).quantize(Decimal("1")) * self.tick_size
```

It has **zero callers in the repository**. The codebase wrote down the exact failure mode in
a docstring and never wired the guard up. `order.protective_stop_placed` appears **0 times**
in the entire day.

Three consequences follow:

1. **Every position the platform opened ran with no broker-side protection** — 38 positions,
   **86,470 position-seconds (24.0 position-hours)**, median 1,031 s each, longest 7,866 s
   (INTC). An engine-side stop did fire 19 times and did close positions — but it only exists
   while the worker process is alive, and it acts on a completed bar at a median 60.2 s
   cadence. The GTC broker stop is the one that survives a crash, a restart, or the overnight
   gap, and `router.py:869-882` says so in its own docstring. None existed.
2. **Nobody was told.** 85 `CRITICAL` log lines, and **zero alerts**. All 17 alerts sent all
   day were halt lifecycle or scheduled reports. The most dangerous condition of the session
   never reached a human.
3. **The end-of-day report counts the failures as successes.** `orders_submitted=201` is
   76 real orders + 85 rejected stops + 40 risk refusals, added together as one number.

**This is the one condition the paper week exists to test.** `docs/SAFETY.md:125` lists as a
go-live gate: *"Every strategy has a stop loss configured; there are no unprotected
positions."* And `analytics/paper_run.py:261` carries a clause written for exactly this:

> *SAFETY.md's go-live condition is that `runner.position_unprotected` never happens. A paper
> week is the first and only place that condition can be observed at all.*

Day 2 violated it **85 times across 38 positions**. Layer 5 did not hold.

Worse, the platform's own verdict tool cannot see it: `_stops_on_every_position` returns
*"no durable record — an unprotected position is a CRITICAL log line, not a row"* and tells the
operator to `docker compose logs worker | grep runner.position_unprotected`. The go-live gate
is checked by grepping container logs, which is precisely how this review found it. There is no
metric either — `metrics/registry.py` declares 19 metric names covering halts, risk, orders,
stream, strategy, alerts and the API, and **none** for protection.

Separately and independently: a **global halt fired at 17:00:52 on a false positive** and
covered the last 2h59m of RTH. The reconciler compares two book snapshots taken 1.4 seconds
apart while orders are in flight. It is not a real divergence — the very next run, five
minutes later, was clean, with no intervention.

**Recommendation: hold day 3.** Fix B1 (one line, plus a test), fix the alerting gap in F3,
and put a quiescence guard on the reconciler (F4). Day 2's trades are not evidence about the
strategy; they are evidence about the plumbing.

---

## 2. Timeline

| Time (UTC) | Event |
|---|---|
| *(before 11:16)* | **Worker already running — the boot is not in this capture.** First worker line is a reconnect whose `gap_since` is 11:15:12 |
| 11:16:47 | Log window opens |
| 11:43:40 | Stream + trade-updates drop and reconnect. **Real downtime 1.13s**; the log calls it `gap_seconds=1707.4`, which is the ingestor's age, not a gap (F10) |
| 11:43:41 | `data.stream.backfill_truncated` → a 6-hour window lying entirely before IEX opens, so all 20 symbols return empty. Correct, and unreadable |
| 11:43:47 | A **129.6-second whole-host stall**, mid-backfill. Pre-market |
| 12:00:00 | First bar-minute of the day — IEX publishes nothing earlier |
| 12:30:01 | **`apply_corporate_actions` raises `DataGapError` on `ZWZZT`** and dies. Never re-runs |
| 13:12:11 | Postgres: `the "timescaledb" extension is not up-to-date` (2.15.2 installed, 2.30.0 current) |
| 13:30:00 | **Market open.** No bars this minute |
| 13:32:16 | `runner.warmed_up bars=1020 symbols=20` — the only warmup all day |
| 13:33:17 | **`runner.timeframe_mismatch asked_for=1d serving=1m`** — logged once, never again |
| 14:01:24 | First order (KO buy 56). **First stop rejection, 0.77s later** |
| 14:23:54 | `auth.login` — the only login of the day |
| 16:57:51 | Last stop rejection. **85 attempted, 85 rejected, 0 placed** |
| 17:00:51.4 | `reconcile_with_broker` starts — snapshots the broker book |
| 17:00:51.7 | QQQ `stop_triggered` → exit submitted |
| 17:00:52.0 | INTC `stop_triggered` → exit submitted |
| 17:00:52.5 | INTC fills 47 — position closes |
| 17:00:52.8 | **`reconcile.mismatch` → `risk.killswitch.engaged` (global)**, 1.4s after the snapshot |
| 17:00:54 | `alert.sent` telegram, critical |
| 17:05:55 | **Next reconcile run is clean, `positions=5`. No intervention.** The mismatch was transient |
| 17:10:56 | Second mismatch (`position_qty: PEP`) → escalated. Same race |
| 17:15–19:45 | `worker.halt_reminder` × 11, every 15 min, each one alerting |
| 17:58:11 | Operator's last dashboard request. **Nobody watches for 3h39m** |
| 20:00:00 | `worker.session_summary evaluations=386 halted=True orders_submitted=57` |
| 20:00:36 | Market close. Runner sleeps 62,963 s |
| 20:30:00 | `worker.daily_report` — *"201 submitted, 76 filled, 40 refused"* |
| 21:38:31 | `POST /api/v1/risk/resume` → **halt cleared, 4h37m38s after it engaged** |
| 21:38:52 | Operator's session ends. Total visit: **49 seconds** |

One `runner.warmed_up`, **zero crashes, zero restarts, zero config changes** — against day 1's
six boots, three crashes and three revisions.

---

## 3. The blocker

### B1 — Every protective stop price is off-tick, so every protective stop is rejected `critical`

**Evidence chain, all verified:**

| File | Line | What it says |
|---|---|---|
| `apps/worker/src/atp_worker/runner.py` | 1321 | `Decimal(str(value))` — a numpy float becomes a 17-digit `Decimal` |
| `libs/core/src/atp_core/risk/stops.py` | 141 | `level = entry_price + direction * multiplier * atr_value` — never quantised |
| `libs/core/src/atp_core/execution/router.py` | 895 | `stop_price=level` — passed through untouched |
| `libs/core/src/atp_core/brokers/alpaca.py` | 838 | `body["stop_price"] = str(order.stop_price)` — serialised raw |
| `libs/core/src/atp_core/domain/market.py` | 43 | `round_price()` — **the fix, with zero callers** |
| `libs/core/src/atp_core/domain/market.py` | 32 | `tick_size: Decimal = Decimal("0.01")` — the quantum, already declared |

**Log corroboration.** 85 `order.broker_rejected`, 85 of them matching
`invalid stop_price <N>. sub-penny increment` — a single distinct message shape after
normalising the price. `order.protective_stop_placed`: **0 occurrences**.
`order.protection_cancelled` fires 57 times and every one reads `cancelled=0 still_live=0` —
the platform repeatedly cancelling protection that was never there.

Rejections by symbol: WMT 14, KO 8, PEP 6, JPM 6, INTC 6, GOOGL 5, BAC 5, QQQ 4, PFE 4,
DIA 4, AMZN 4, SPY 3, MSFT 3, JNJ 3, AAPL 3, IWM 2, GE 2, XOM 1, IBM 1, CSCO 1. **All 20
symbols. No symbol was ever protected.**

**Why 85 rejections for 76 orders.** A protective stop is attempted per *fill event*, not per
order — and `router.py:562` advances `self._covered[entry_order.id]` only on success. Because
every attempt failed, `covered_from` stayed at zero and each new partial re-attempted the whole
**cumulative** quantity. INTC's 49-share entry at 14:18:30 filled in four tranches and produced
four rejections at `qty=31, 36, 45, 49` against the same level `101.4772122294475936`; KO's
15:51 entry drew five, at `qty=20, 48, 53, 55, 56`. The 85 rejections are exactly the 85 fill
events on the 38 buy orders. Attempts per entry: 14 entries × 1, 11 × 2, 7 × 3, 3 × 4, 2 × 5,
1 × 6. The escalation is a consequence of the failure, not a second defect.

**This is a CLAUDE.md §1 rule 1 story with a twist.** `runner.py:1315-1320` carries this
docstring:

> *Float in, `Decimal` out via `str` — never `Decimal(float)`, which inherits the binary
> rounding error rule §1.1 exists to avoid. ATR is a statistic so computing it in float is
> fine; the moment it becomes a price distance it stops being one.*

That reasoning is correct, and following it is what produced the off-tick price:
`Decimal(str(0.08559210425130274))` faithfully preserves every digit the float had. **Obeying
rule §1.1 properly is what broke the order.** Decimal exactness and venue tick conformance are
two different constraints, and the rule only names the first. It needs the second half written
down: a price that leaves the platform is quantised to the instrument's tick.

**Fix.** Quantise at the boundary, in `router._stop_order`, using the instrument's own tick:

```python
stop_price=instrument.round_price(level),
```

Round *conservatively*, not to nearest: a long's stop rounds **down** and a short's **up**, so
quantising can never tighten a stop into the market. Add the same at the take-profit and any
other price the router sends. Then a test that submits a stop at a price with more than two
decimal places and asserts the body sent to the broker carries exactly two.

**Confirm before day 3, one query:**
```sql
select purpose, status, count(*) from orders
where created_at::date = current_date group by 1,2;
-- expect: stop_loss orders with status NOT IN ('rejected')
```

---

## 4. Findings

### F1 — The daily report counts failed safety devices as submitted orders `high`

Three numbers describe the same day, and all three are internally correct under three
different unstated definitions:

| Source | Number | What it actually counts |
|---|---|---|
| `worker.session_summary` | `orders_submitted=57` | Orders the **runner** initiated (52 pre-halt + 5 exit carve-outs) |
| `order.submitted` log lines | **76** | Orders that **reached the broker** (57 runner + 19 stop-manager exits) |
| `worker.daily_report` | `orders_submitted=201` | **Every order record created** = 76 + 85 rejected + 40 refused |

The arithmetic is exact: `76 + 85 + 40 = 201`, and `76 − 19 = 57`.

An operator reads *"201 submitted, 76 filled"* and infers a 38% fill rate on a busy day. The
truth is a **100% fill rate on 76 orders**, and the number is inflated precisely by the day's
worst defect. The report is most wrong exactly where it most matters.

**Fix.** Report the funnel, not a total: signals → orders sent → filled → *protective orders
rejected*. Name the denominator in the field. And see F2 — the report must state the
unprotected count, which it currently cannot see at all.

### F2 — The daily report cannot see that every position was unprotected `high`

`worker.daily_report` names symbols, trades, risk rejections, halts, and honestly declares
`feed incidents NOT MEASURED`. It says nothing about protection, because `DailyReport`
(`analytics/daily.py:72-86`) has **no field for it** — the dataclass carries
`orders_submitted`, `orders_filled`, `orders_refused`, `refusals_by_rule`, `symbols`,
`realised_pnl`, `starting_equity` and `ending_equity`, and nothing about stops. On the one day
when every position in the book ran naked, the operator's end-of-day artifact reads as a
normal day.

It also has `realised_pnl`, `starting_equity` and `ending_equity` — and **printed none of
them**, so all three were `None`. The day's realised P&L was computable from the fill stream
this whole time (§5: +$94.20 over 38 round trips). A report with a P&L field that silently
renders nothing is worse than one without: the operator cannot tell "flat" from "not
measured".

**Fix.** Add `positions_unprotected` and `protective_orders_rejected` to `DailyReport`, and
make a non-zero value the headline rather than a footnote. Populate `realised_pnl` from the
fills, or say `NOT MEASURED` the way the feed-incidents section already honestly does — the
machinery for admitting a gap is right there and unused.

### F3 — 85 CRITICAL lines produced zero alerts `high`

Every alert sent on 2026-09-08:

| Count | Key |
|---|---|
| 11 | `halt.reminder.1` … `halt.reminder.11` |
| 3 | `halt.global.all.reconciliation_mismatch`, `…escalated…INTC-QQQ`, `…escalated…INTC-PEP-QQQ` |
| 1 | `halt.global.all.cleared` |
| 2 | `daily.report.2026-09-08`, `session.summary.2026-09-08` |
| **17** | **total — none of them about unprotected positions** |

`order.position_unprotected` fires `log.critical` 85 times and reaches no transport. The halt
machinery alerts well; the protection machinery does not alert at all. Note the asymmetry:
the platform woke a human 11 times for a **false-positive halt**, and zero times for a **real
loss of protection**.

**Fix.** Route `order.position_unprotected` to the alert port, deduplicated by symbol with a
session-level "N symbols unprotected" rollup so 85 events become one actionable page. Add
`atp_positions_unprotected` to `metrics/registry.py` — there is no metric for it today — and
persist it, so SAFETY.md's go-live gate can be evaluated from the database rather than from
`docker compose logs`.

### F4 — The reconciler reads its two books at different times, and halts on the difference `critical`

Not a book divergence. A **read-ordering race inside the reconciler**, and the asymmetry is
visible in one line:

```python
# apps/worker/src/atp_worker/scheduler.py:289-290
report = await session.reconciler.reconcile(
    session.portfolio, known_orders=session.open_orders()
)
```

`session.open_orders()` is a **call**, evaluated as an argument expression *before* the
coroutine runs. `session.portfolio` is a **live object reference**, whose state is read inside
at `reconciliation.py:200`. Between them sit three sequential broker awaits
(`get_positions`, `get_open_orders`, `get_account`, `reconciliation.py:182-184`).

So the local order set is read **too early**, the local portfolio **too late**, and the
broker's three snapshots are scattered in between — about 1.4 seconds end to end. For any
order in flight, both errors point the same way.

```
17:00:51.429  job_starting                       ← known_orders snapshotted here
17:00:51.681  QQQ exit submitted                 ← 252 ms after that snapshot
17:00:51.967  INTC exit submitted
17:00:52.219  QQQ fills 1 of 6
17:00:52.451  INTC fills 47 → booked locally
17:00:52.799  mismatch → killswitch.engaged      ← portfolio read here
```

Every field decodes to a read artifact:

- `orphan_order: QQQ`, `orphan orders: atp-8c1a369c511562feff193f54` — **the platform's own QQQ
  exit**, submitted 1.12 s after the `known_orders` snapshot that could not contain it.
- `missing_position: INTC` — `MISSING_POSITION` means "the broker holds a position we do not"
  (`reconciliation.py:281`). Ours = 0 because the fill was booked at 52.451; theirs = 47 from a
  `get_positions()` call made before it.
- `position_qty: QQQ` — ours 5, theirs 6, mid-exit.

**The proof is statistical and total.** Of the 78 scheduled reconcile runs, **exactly 2 had any
`order.submitted` or `execution.trade_update.filled` inside their job window — and those are
exactly the 2 that halted.** All 76 quiet windows were clean. Perfect precision and
specificity. The 17:05:55 run, five minutes later and quiet, was `clean positions=5` with no
intervention.

**Correction to a tempting story:** this is only *partly* downstream of B1. Run 1's exits were
`runner.stop_triggered` — engine-side stops that exist only because the broker stops were
rejected. Run 2's PEP exit carries no `stop_triggered`; it was an ordinary signal exit. So the
rejections supplied the order flow for one of the two halts, and the mechanism for neither. F4
would fire on a fully protected platform too.

Cost: **2h59m07s of RTH** with entries refused, and a halt that outlived the close by 1h38m.

**Fix.** Read both books at one instant — snapshot the portfolio at the same point as
`open_orders()`, inside the reconciler — or require quiescence (no fills, no working orders for
N seconds) before declaring a divergence. Failing either, re-read once before halting: a
divergence that clears on immediate re-read is a race.

### F4a — The exit carve-out held by about two seconds `high`

The halt's impugnment named **INTC, QQQ and PEP** — and an impugned symbol is the *void* in the
exit carve-out (ADR 0029): the halt says it cannot prove those positions, so it will not let
them out.

It cost nothing only because all three were going flat at the exact moments they were
impugned. INTC's 47 filled at 17:00:52.451, 0.35 s before the halt named it. QQQ finished at
17:00:54.329. PEP's exit was already submitted when the escalation named it.

Had any of the three still held size, the platform would have refused to close it for **4h37m,
through the close, with no broker-side stop anywhere** — the exact trap the carve-out exists to
prevent, produced by a halt that was a false positive to begin with.

**Fix.** An impugnment raised by a *transient* reconciliation artifact must not void the exit
carve-out. Re-read before impugning (F4), and let a symbol out on a confirmed flat.

### F4b — The reconciler's halt writes no audit row `medium`

The 4h37m global halt exists only as a log line and a Redis key. `HALT_ENGAGED` is written at
four sites — `api/routers/risk.py:762,872` and `scripts/halt.py:312,352` — and
`risk/killswitch.py` imports no audit sink. See F14 for what that does to the daily report.

### F4c — The halt reminder goes silent exactly when it is needed most `high`

`remind_about_halts` is RTH-gated: 26 firings from 13:30 to 19:45, then nothing. The halt ran
until 21:38. The reminder was silent for the final **1h53m** — the longest unattended stretch
of the incident, and the only part of it after the operator might plausibly have finished their
day.

**Fix.** An engaged halt is a state, not a market-hours event. Remind until it clears.

### F5 — `from_reason=None` reads as a bug and is not one `low`

`risk.killswitch.escalated ... from_reason=None reason=reconciliation_mismatch` looks like a
lost field: the halt being escalated was engaged 10 minutes earlier *with* that reason. It is
in fact intended — `from_reason` is emitted only when the reason **changes**, and here it did
not. But a field that renders `None` for "unchanged" is indistinguishable from one that lost
its value, on the single most-read line of the worst incident of the day. Omit the key, or
render it `unchanged`.

### F6 — The unprotected alert carries an empty reason `medium`

```
order.position_unprotected  detail= entry_order_id=... qty=56 rule= symbol=KO
```

`router.py:550-551` populates these from `outcome.decision.rule` and `.reason` — fields set on
a **risk denial**. This order was not risk-denied; it was **broker-rejected**, and the decision
object is empty. The surrounding comment reasons entirely about risk rules ("a transient rule
declined", "`kill_switch` *can* reach here now") — the broker-rejection path was never
considered. The single most important alert of the day says nothing about why.

**Fix.** Branch on the failure kind and carry the broker's own rejection message.

### F7 — `apply_corporate_actions` dies on a synthetic test ticker `medium`

`scheduler.py:515` builds its work list from `repository.stored_series()` — **every** symbol in
the bar store, which includes the synthetic seed tickers `data/seed.py:64` writes
(`ZVZZT`, `ZWZZT`, `ZXZZT`). Alpaca has no data for a NASDAQ test ticker, so `get_bars` raises
`DataGapError` at `providers/alpaca.py:329` and the job dies **before processing any real
symbol**. It does not retry.

`RESERVED_TEST_SYMBOLS` already exists at `seed.py:59` and is not consulted here. **This is the
same shape of defect as B1: the guard is written, and not wired up.** Under CLAUDE.md §5,
unapplied splits corrupt the price history a backtest is later judged on.

**Fix.** Exclude `RESERVED_TEST_SYMBOLS` from the work list, and make one symbol's gap skip
that symbol rather than abort the job.

### F8 — The slow average at the open straddles a four-day weekend `high`

Two defects, and the second is the serious one.

**First, the declared timeframe is still wrong.** `runner.timeframe_mismatch asked_for=1d
serving=1m`. `sma_crossover` defaults to `fast_period=20`, `slow_period=50`
(`examples/sma_crossover.py:37-38`). Declared against daily bars that is a 20-day/50-day trend
system; served 1-minute bars it is a 20-minute/50-minute scalper — which is what actually
traded, 38 round trips in one session. The platform silently substitutes a series the strategy
did not ask for, warns **once** at 13:33, and runs 385 more evaluations in silence.

**Second, the warmup is stitched across a market closure.** `runner.warmed_up bars=1020
symbols=20` is 51 bars per symbol — `slow_period + 1`. `runner.py:449` gets them from
`bar_repo.get_last_n_bars(symbol, self.timeframe, needed)`, and `get_last_n_bars`
(`persistence/bars.py:150`) takes the newest `n` rows **with no recency bound at all**.

Count what was actually available. This session had ingested **118 bars in total** before the
13:32:16 warmup — 5.9 per symbol — of which only 58 were pre-market (2.9 per symbol; IEX
pre-market is nearly dead). Yet every symbol received its full 51, and
`runner.warmup_short_history` fired **zero** times.

So at least **45 of each symbol's 51 bars came from storage that predates this session** — and
`data.stream.gap_widened_from_storage storage_says_from=2026-09-04T20:57` names the newest
stored bar as **Friday 4 September, post-market**. Monday 7 September was Labor Day.

The SMA(50) that priced the open was therefore computed over roughly 45 one-minute bars from
the previous Friday, concatenated directly onto Tuesday's first few, with a **four-day closure
treated as though no time had passed**. The fast SMA(20) refreshed within about twenty minutes
of the open while the slow average stayed half-stale — and the first order of the day landed at
**14:01**, thirty-one minutes in. *(That the stale slow average manufactured that particular
crossover is an inference, not established; the discontinuity itself is arithmetic.)*

**This is the survivorship/lookahead family of error** that CLAUDE.md §5 exists to catch: a
number that looks plausible and is meaningless. A moving average is only defined over a
contiguous series.

**Fix.** Three things, and the third matters most:
1. Require an explicit `timeframe` in the strategy config that must equal the worker's; refuse
   to boot on a mismatch rather than substituting.
2. Give `get_last_n_bars` a recency bound, or have warmup refuse bars older than a session
   boundary — and make `warmup_short_history` the loud path rather than silently reaching back
   across a holiday.
3. Re-tune the periods for whatever timeframe is chosen. Until then day 3 measures plumbing,
   not strategy.

### F9 — The tape is 91.67% complete and nothing says so `medium`

7,150 bars ingested during RTH against 7,800 expected (390 min × 20 symbols) = **91.67%**.
Median 18 symbols per minute; only 79 of 389 minutes carried all 20. Exactly one RTH minute has
no bars at all: **15:56**.

*(A bar for minute M is written just after M closes, so the arrival timestamp must be shifted
back one minute before bucketing. Doing this naively makes 13:30 look empty and shifts the
whole distribution — the corrected figures are above.)*

**Day 2 is better than day 1 on this axis**: 91.67% against day 1's 87.6% (6,830/7,800), and no
multi-minute blackout — day 1 lost 18:45–18:51 entirely.

This is the IEX partial tape doing exactly what ADR 0026 says it does. But an `SMA(20)` over a
gappy series is not a 20-minute average; it is an average of "the last 20 bars that happened to
print on IEX", spanning a different amount of wall-clock time per symbol and per hour.
*(Inference, not measurement: the size of the resulting signal distortion is not quantified
here.)*

**Fix.** Record per-symbol bar coverage per session and surface it in the daily report. A
strategy result computed over an unmeasured 91% tape is not reproducible.

### F10 — The log reported a 28-minute feed outage that did not happen `medium`

`data.stream.reconnected gap_seconds=1707.439 gap_since=2026-09-08T11:15:14` reads as a
28m27s loss of market data. It was not one. The real data-socket downtime was **1.13 seconds**:
`data.alpaca.stream_disconnected` at 11:43:40.744, `data.alpaca.stream_connected feed=iex
symbols=20` at 11:43:41.872.

`providers/alpaca.py:592` seeds `gap_since = self._clock.now()` when the stream generator
starts, and only advances it from `self._last_message_at`. No frame had ever arrived — IEX
publishes nothing before 12:00Z, which the tape's own first bar-minute of 12:00 corroborates —
so `gap_since` never moved off the process start time. **1707 seconds is the ingestor's age,
not a gap.**

Everything downstream then behaved correctly on a false premise: `gap_widened_from_storage
storage_says_from=2026-09-04T20:57` is right (Friday 4 Sep post-market was the last stored bar;
Monday 7 Sep was Labor Day); `backfill_truncated` clipped 3.6 days to the 6-hour
`MAX_RECONNECT_BACKFILL` (`stream.py:74`), producing a 05:43–11:43Z window lying **entirely
before IEX opens**; and so all 20 symbols came back empty (`backfill_empty` × 20,
`data.backfill.done bars=0 empty_windows=20 requests=21`).

So CLAUDE.md §5's reconnect-gap rule was honoured — the backfill ran and correctly recovered
nothing, because there was nothing to recover. The defect is the telemetry: a reviewer reading
this log would spend an hour on an outage that lasted a second, and the same line will
under-report a real gap whose first message arrives late.

**Fix.** Report the gap only once a message has been seen; before that, say `no data yet` and
report process age separately. Make the reconnect backfill calendar-aware so a window spanning
a closure says "the venue was shut", not "the venue had nothing".

### F10a — `get_bars` is all-or-nothing, and this caller skipped both guards `medium`

`AlpacaHistoricalProvider.get_bars` (`providers/alpaca.py:326-333`) raises `DataGapError` on
the **first** symbol that returns empty, discarding the whole batch — the failure mode behind
F7. Both request passes (`symbols=41`, raw and adjusted) had already been paid for. The
codebase has guards for this contract elsewhere; `apply_corporate_actions` uses neither.

**Fix.** Return per-symbol results and let the caller decide, or give the batch path a
`skip_empty` mode. A whole-batch abort on one dead ticker is not a data-integrity guarantee, it
is a single point of failure.

### F11 — `worker.starting` is absent, so boot-time state is unverifiable `low`

The first worker line in the capture is a reconnect at 11:43:40 whose `gap_since` is 11:15:12 —
before the window opened at 11:16:47. The worker was already running. **This review cannot
confirm from the log** whether `METRICS_TOKEN` was set, what the live-mode banner said, or what
config revision was loaded. Day 1 could confirm all three. Start the capture before the stack.

### F12 — Operational papercuts `low`

- **TimescaleDB is 15 minor versions behind** — `2.15.2` installed, `2.30.0` current (13:12:11).
- **Redis is churning**: `10000 changes in 60 seconds` throughout RTH, and an AOF rewrite
  triggered at `1553588% growth`. 2,500 log lines for one session.
- **7,237 `data.bars.upserted` lines, every one `batches=1 rows=1`** — `stream.py:331` calls
  `upsert_bars([bar])`, so it is one database round trip and one log line per bar. Batch the
  writes; log the batch. The line also carries **no `symbol`** (`bars.py:118`), so ADR 0026's
  own per-symbol coverage baseline cannot be reproduced from the log at all.
- **A 129.6-second whole-host stall at 11:43:47**, mid-backfill and pre-market. It explains why
  `stream_subscribed` lands 2m17s after `stream_connected`. No trading impact, but a stall that
  long during RTH would be a staleness halt.

### F13 — Clearing a reconciliation halt proves who, never what `medium`

The operator's entire final visit was 21:38:03 → 21:38:52. One `POST /api/v1/risk/resume` at
21:38:31 cleared a global halt naming three unproven symbols, 1h38m after the close — **28
seconds after loading the dashboard**.

The order of requests is the finding:

```
21:38:03  dashboard loaded
21:38:31  POST /api/v1/risk/resume     ← halt cleared
21:38:43  GET  /api/v1/positions       ← the book read 12s AFTER clearing
21:38:47  GET  /api/v1/orders
```

**Authentication was not the gap — day 1's F9 is properly fixed.** `POST /api/v1/risk/resume`
calls `require_step_up(payload.password, ...)` (`api/routers/risk.py:831`) and writes an audit
row, and `scripts/halt.py` now demands the same. The operator entered their password.

What nothing demanded was **evidence**. `docs/RUNBOOK.md` "Reconciliation mismatch" step 1 is
*compare `GET /api/v1/positions` with the broker's own UI*; step 3 is `adopt_broker_state()`.
The positions read happened **after** the clear, and
`execution.reconcile.adopted_broker_state` appears **0 times** all day. Clearing a
reconciliation halt is an assertion that the two books now agree, and the platform accepted
that assertion without checking it — or asking the operator whether they had.

Human-shaped API navigation stops at **14:29:19** and does not resume until **21:38:03** — a
**7h09m gap** that swallows the entire incident.

CLAUDE.md §1.8 is deliberate that arming `allow_live_orders` costs a password and is audited,
while turning it *off* asks for nothing. That asymmetry is right for the kill direction. But
clearing a **reconciliation** halt is not a kill — it is an assertion that the books now agree,
and nothing in the log shows that assertion was checked. Day 1 raised the same shape of issue
as F9 (`scripts/halt.py` clears with no password and no audit row).

CLAUDE.md §1.8's asymmetry — arming costs a password, disarming asks nothing — is right for
the *kill* direction. But a reconciliation halt is not a kill switch an operator is turning
off; it is a claim about the state of the book. Those need different gates, and today they
share one.

**Fix.** Require a fresh clean reconcile before a reconciliation halt can be cleared, and
record the halt against the evidence that cleared it, not only the person.

### F14 — The daily report's halt count is a stale row, and this day's halt counts zero `medium`

`worker.daily_report` says `halts 1 (1 recorded — operator halts only; the risk layer's own
triggers write no audit row)`. The parenthetical is truthful: `HALT_ENGAGED` / `HALT_CLEARED`
audit rows are written at exactly four sites — `api/routers/risk.py:762,872` and
`scripts/halt.py:312,352` — and `risk/killswitch.py` imports no audit sink at all.

But the consequence is worse than the caveat admits. The halt that engaged at 17:00:52 was
`engaged_by=reconciler`, so it wrote **no** row. The operator's clear came at 21:38:31 — 68
minutes *after* the 20:30 report was generated. The only mutating HTTP requests in the entire
capture are the 14:23 login and that 21:38 resume. So the "1 recorded" halt is a row from
**outside this session**, swept in by the report's rolling `now - timedelta(days=1)` window at
`scheduler.py:418`.

The halt that cost three hours of RTH, 40 refusals and 11 CRITICAL reminders contributes
**zero** to the day's halt count, and a halt from a previous day is reported in its place.

**Fix.** Give the risk layer an audit sink, and bound the report's window to the session it
names rather than a rolling 24 hours.

---

## 5. The day's result

Stated for completeness, and **not** as evidence about the strategy — see F8 and B1.

FIFO-matched from the 174 fill events using per-order VWAP: **38 completed round trips**, zero
residual, **gross realised +$94.20** on $185,177.17 bought against $185,271.37 sold.
**15 winners, 23 losers**; best INTC +$121.05, worst −$15.02. Corroborated independently by
`runner.evaluated ... open_positions=0` at 19:59:36.

Costs are not modelled here and no benchmark is attached, so this is a bookkeeping figure, not
a performance claim. On a day when 40 entry signals were refused for three hours and every
position ran unprotected, +$94.20 on $100,000 of equity is noise.

The 19 engine-side stop exits filled at a **net +$2.58** against their armed levels — a wash.
The software fallback did not cost money on this tape. What it cannot do is survive the worker.

---

## 6. What worked

Worth stating plainly, because day 2 fixed real things:

- **The strategy loop runs and is observable.** 386 evaluations, one per minute, each line
  carrying `bars_closed`, `signals`, `open_positions`, `working_orders` and cumulative
  counters. Day 1's B1 and F1 are closed.
- **The signal funnel closes exactly.** 97 signals = 57 submitted + 40 refused. Every signal
  is accounted for.
- **The halt does what it claims.** All 40 denials are `side=buy`; all 5 post-halt submissions
  are `side=sell`. The exit carve-out (commit `8154201`) let the book flatten completely while
  refusing every new entry. **The platform ended the day flat.**
- **The halt reminder works, and the alerts really were delivered.** 11 reminders, every 15
  minutes, each one alerting. `alert.sent` is emitted only after Telegram returns HTTP 200 with
  `ok:true` (`alerts/sinks.py:311-335`), and there were **0** `alert.send_failed` all day. Day
  1's F8 — *"nothing repeated the halt for 2h37m"* — is closed. The alerting worked; the
  attention did not (F4c, F13).
- **Stability.** One warmup, zero crashes, zero restarts, zero config changes, no full-stack
  bounce during RTH. Day 1's F6, F7 and F12 are closed.
- **The engine-side stop fallback is reachable and fired 19 times.** Day 1's F2 is closed. It
  is now the *only* protection, which is B1's point — but it works.
- **No secrets anywhere in 14,989 records.** CLAUDE.md §1.6 clean.
- **The daily report is honest about what it cannot measure** (`feed incidents NOT MEASURED`,
  with the grep to run). That is the right instinct; F2 asks it to extend the same honesty to
  protection.
- **A non-bug, documented so nobody chases it:** `execution.reconcile.clean` (78) exceeds
  `worker.reconcile.clean` (76) because two runs are ad-hoc — the 11:43 reconnect and the 13:32
  warmup — and carry no `correlation_id`. 78 scheduled runs = 76 clean + 2 mismatch. Consistent.

---

## 7. Day-1 scorecard

| Day-1 item | Day-2 status |
|---|---|
| **B1** runner reads a series nothing writes | **Partly fixed** — reader and writer agree on `1m`; the *strategy* still declares `1d` (F8) |
| **B2** market entry into a flat symbol cannot be priced | **Fixed** — 76 entries priced and filled |
| **F1** strategy loop unobservable | **Fixed** — 386 `runner.evaluated` |
| **F2** engine-side stop fallback unreachable | **Fixed** — 19 `runner.stop_triggered` |
| **F3** kill switch has no exit carve-out | **Fixed** — 5 exits allowed, 40 entries refused |
| **F4** worker never reads halt state at boot | **Untestable** — no boot in this capture (F11) |
| **F5** market data lost and reported as recovered | **Fixed, with a new twist** — no data was lost this time; the log now over-reports a gap instead (F10) |
| **F6** crashes self-inflicted | **Fixed** — zero crashes |
| **F7** crash-looping worker cannot halt itself | **Fixed** — no crash loop |
| **F8** nothing repeated the halt | **Fixed** — 11 reminders, all alerting |
| **F9** halt cleared with no password/audit | **Fixed** — `scripts/halt.py` and `POST /risk/resume` both demand the password (`risk.py:831`) and write an audit row |
| **F10** three scheduled jobs are dormant stubs | **Partly fixed** — `generate_daily_report` and `rollover_daily_counters` now run; `apply_corporate_actions` runs and **crashes** (F7) |
| **F11** sizing not survivable on the intended timeframe | **Open, and worse than thought** — see F8: the slow average also straddles a market closure |
| **F12** full-stack restart during RTH | **Fixed** — zero restarts |
| **F13** the feed is structurally thin | **Confirmed, quantified, improving** — 91.67%, up from 87.6% (F9) |
| **F14** `no_action` inflates the rejection counter | **Superseded** — the counter is now wrong in a bigger way (F1) |

Day 1 recommended verifying with one SQL query before re-running. **No evidence in the log or
the repository that it was run.**

---

## 8. Before day 3 runs

1. **B1 — quantise every price sent to a broker.** One call to the `round_price` that already
   exists, rounded conservatively by side, plus a test asserting two decimal places in the
   submitted body. Nothing else on this list matters as much.
2. **F3 — alert on `order.position_unprotected`, and give it a metric and a row.** The
   platform must be able to wake someone for a loss of protection, not only for a halt — and
   SAFETY.md's go-live gate must be answerable from the database, not from a log grep.
3. **F4 — stop the reconciler racing itself.** Quiescence check, or re-read once before halting.
4. **F2/F1 — make the daily report say what happened.** Add protection counts; label the
   denominators.
5. **F7 — exclude `RESERVED_TEST_SYMBOLS`** and make corporate actions survive one bad symbol.
6. **F8 — decide the timeframe question, and bound the warmup.** Configure the strategy for
   `1m` and re-tune its periods, or serve it `1d` — and stop `get_last_n_bars` reaching back
   across a four-day closure to fill a 50-period average. Until then day 3 measures plumbing,
   not strategy.

**The roadmap needs no change.** Phase 4's *Verifiable:* line is *"a strategy trades the paper
account for a week and reconciles clean"*. Day 2 did neither — the week is not complete and the
reconciler diverged twice — so Phase 4 correctly stays `0 / 11`. The one ticked item day 2
bears on, *"Alerting to a phone (feed loss, halt, reconciliation failure)"*, **holds**: all 17
alerts were confirmed delivered. It does not claim to cover unprotected positions, and F3 is a
gap in the platform rather than a lie in the roadmap.

**Verify B1 with one query after the first fill of day 3:**
```sql
select id, symbol, purpose, stop_price, status from orders
where purpose = 'stop_loss' and created_at::date = current_date;
-- expect: stop_price with exactly 2 decimal places, status not 'rejected'
```

Day 2 is **readable but not conclusive**. The platform is sound in its safety design — the
halt, the carve-out, the reminders and the engine-side fallback all did their jobs, and the
book ended flat. What it lacks is the last inch of wiring on three guards that were already
written: `round_price`, `RESERVED_TEST_SYMBOLS`, and an alert route for the one condition that
most needed one.

The pattern is worth naming, because it is the same pattern three times. This codebase reasons
carefully — the docstrings on `round_price`, on `Decimal(str(...))`, on the GTC stop, on the
exit carve-out are all correct, and several of them predict the exact failure that then
occurred. What is missing is not judgement. It is the call site.
