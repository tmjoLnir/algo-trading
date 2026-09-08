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
| 11:43:41 | Stream + trade-updates reconnect after a **28m27s** gap (`gap_seconds=1707.4`). Pre-market |
| 11:43:41 | `data.stream.backfill_truncated` — *"the rest is left to the nightly gap sweep."* The gap is **not** fully backfilled |
| 12:01:00 | First bar ingested |
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
`feed incidents NOT MEASURED`. It says nothing about protection, because it has no field for
it. On the one day when every position in the book ran naked, the operator's end-of-day
artifact reads as a normal day.

**Fix.** Add `positions_unprotected` and `protective_orders_rejected` to the report, and make
a non-zero value the headline rather than a footnote.

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
session-level "N symbols unprotected" rollup so 85 events become one actionable page.

### F4 — The reconciler races in-flight orders and halts on the result `high`

```
17:00:51.429  job_starting reconcile_with_broker        ← broker snapshot taken
17:00:51.681  QQQ stop_triggered → exit submitted
17:00:51.967  INTC stop_triggered → exit submitted
17:00:52.219  QQQ partial fill (1 of 6)
17:00:52.451  INTC fills 47 → position closes
17:00:52.799  reconcile.mismatch → killswitch.engaged (global)
```

`missing_position: INTC` — closed on the platform after the broker snapshot.
`orphan_order: QQQ` + `position_qty: QQQ` — a live partial exit inside the comparison window.
Three "discrepancies", all of them the same 1.4-second window.

**Proof it is a false positive:** the next scheduled run at 17:05:55 was `execution.reconcile.clean
open_orders=0 positions=5`, with no operator action in between. The 17:10:56 escalation is the
same race against PEP's exit, submitted at 17:10:55.135, 0.5s before that run started.

Cost: **2h59m07s of RTH** with entries refused, and a halt that outlived the close by 1h38m.

**Fix.** Take both books at one instant, or require quiescence — no fills and no working
orders for N seconds — before declaring a divergence. Failing that, re-check once before
halting: a divergence that clears on immediate re-read is a race, not a break.

### F5 — `from_reason=None` on an escalation that had a reason `medium`

`risk.killswitch.escalated ... from_reason=None reason=reconciliation_mismatch`. The halt
being escalated was engaged 10 minutes earlier with `reason=reconciliation_mismatch`, not
`None`. The field that exists to show what changed reports the previous state as absent.

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

### F8 — The strategy is running parameters it was not configured for `medium`

`runner.timeframe_mismatch asked_for=1d serving=1m`. `sma_crossover` defaults to
`fast_period=20`, `slow_period=50` (`examples/sma_crossover.py:37-38`). Declared against daily
bars, that is a 20-day/50-day trend system. Served 1-minute bars, it is a 20-minute/50-minute
scalper — which is what actually traded, 38 round trips in one session.

Day 1's fix made the reader agree with the writer, which was right. It did not make the
*strategy* agree with either: the platform silently substitutes a series the strategy did not
ask for, warns **once** at 13:33, and runs 385 more evaluations in silence.

`runner.warmed_up bars=1020` = 20 × 51 = 20 × (`slow_period` + 1). At 13:32 the 50-bar slow
average therefore spans the 50 minutes *before* the open — thin pre-market IEX prints.

**Fix.** Refuse to boot on a mismatch, or require an explicit `timeframe` in the strategy
config that must equal the worker's. Warning-and-continuing on a disagreement about *what data
the strategy trades* is the class of silent substitution CLAUDE.md §5 exists to prevent.
Whatever day 3 measures, it is not the configured strategy until this is closed.

### F9 — The tape is 91.4% complete and nothing says so `medium`

7,130 bars ingested during RTH against 7,800 expected (390 min × 20 symbols) = **91.4%**. Only
78 of 388 minutes carried all 20 symbols; the mode is 18–19. Two RTH minutes have no bars at
all: **13:30 — the opening minute** — and 15:57.

This is the IEX partial tape doing exactly what ADR 0026 says it does. But an `SMA(20)` over a
gappy series is not a 20-minute average; it is an average of "the last 20 bars that happened to
print on IEX", spanning a different amount of wall-clock time per symbol and per hour.
*(Inference, not measurement: the size of the resulting signal distortion is not quantified
here.)*

**Fix.** Record per-symbol bar coverage per session and surface it in the daily report. A
strategy result computed over an unmeasured 91% tape is not reproducible.

### F10 — The pre-market reconnect gap was never backfilled `low`

`data.stream.reconnected gap_seconds=1707.4` (28m27s, 11:15:14 → 11:43:41), followed by
`data.stream.backfill_truncated` — *"the rest is left to the nightly gap sweep"* — and 20
`data.stream.backfill_empty`. Under CLAUDE.md §5 the gap should be backfilled before resuming.
It was not. **Impact this session: none** — the gap is entirely pre-market and the runner did
not warm up until 13:32. It matters the day it happens at 15:00.

### F11 — `worker.starting` is absent, so boot-time state is unverifiable `low`

The first worker line in the capture is a reconnect at 11:43:40 whose `gap_since` is 11:15:12 —
before the window opened at 11:16:47. The worker was already running. **This review cannot
confirm from the log** whether `METRICS_TOKEN` was set, what the live-mode banner said, or what
config revision was loaded. Day 1 could confirm all three. Start the capture before the stack.

### F12 — Operational papercuts `low`

- **TimescaleDB is 15 minor versions behind** — `2.15.2` installed, `2.30.0` current (13:12:11).
- **Redis is churning**: `10000 changes in 60 seconds` throughout RTH, and an AOF rewrite
  triggered at `1553588% growth`. 2,500 log lines for one session.
- **7,237 `data.bars.upserted` lines, every one `batches=1 rows=1`** — one database round trip
  per bar, and one log line per bar. Batch the writes; log the batch.

### F13 — A reconciliation halt clears in 49 seconds with no evidence trail `medium`

The operator's entire final visit was 21:38:03 → 21:38:52. One `POST /api/v1/risk/resume` at
21:38:31 cleared a global halt naming three unproven symbols, 1h38m after the close.

CLAUDE.md §1.8 is deliberate that arming `allow_live_orders` costs a password and is audited,
while turning it *off* asks for nothing. That asymmetry is right for the kill direction. But
clearing a **reconciliation** halt is not a kill — it is an assertion that the books now agree,
and nothing in the log shows that assertion was checked. Day 1 raised the same shape of issue
as F9 (`scripts/halt.py` clears with no password and no audit row).

**Fix.** Require a fresh clean reconcile before a reconciliation halt can be cleared, and
record who cleared it against what evidence.

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
- **The halt reminder works.** 11 reminders, every 15 minutes, each one alerting. Day 1's F8 —
  *"nothing repeated the halt for 2h37m"* — is closed.
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
| **F5** market data lost and reported as recovered | **Partly fixed** — the truncation is now *stated* (F10), still not backfilled |
| **F6** crashes self-inflicted | **Fixed** — zero crashes |
| **F7** crash-looping worker cannot halt itself | **Fixed** — no crash loop |
| **F8** nothing repeated the halt | **Fixed** — 11 reminders, all alerting |
| **F9** halt cleared with no password/audit | **Not fixed** — cleared in a 49s visit (F13) |
| **F10** three scheduled jobs are dormant stubs | **Partly fixed** — `generate_daily_report` and `rollover_daily_counters` now run; `apply_corporate_actions` runs and **crashes** (F7) |
| **F11** sizing not survivable on the intended timeframe | **Open** — folded into F8 |
| **F12** full-stack restart during RTH | **Fixed** — zero restarts |
| **F13** the feed is structurally thin | **Confirmed, now quantified** — 91.4% (F9) |
| **F14** `no_action` inflates the rejection counter | **Superseded** — the counter is now wrong in a bigger way (F1) |

Day 1 recommended verifying with one SQL query before re-running. **No evidence in the log or
the repository that it was run.**

---

## 8. Before day 3 runs

1. **B1 — quantise every price sent to a broker.** One call to the `round_price` that already
   exists, rounded conservatively by side, plus a test asserting two decimal places in the
   submitted body. Nothing else on this list matters as much.
2. **F3 — alert on `order.position_unprotected`.** The platform must be able to wake someone
   for a loss of protection, not only for a halt.
3. **F4 — stop the reconciler racing itself.** Quiescence check, or re-read once before halting.
4. **F2/F1 — make the daily report say what happened.** Add protection counts; label the
   denominators.
5. **F7 — exclude `RESERVED_TEST_SYMBOLS`** and make corporate actions survive one bad symbol.
6. **F8 — decide the timeframe question.** Either configure the strategy for `1m` and re-tune
   its periods, or serve it `1d`. Until then day 3 measures plumbing, not strategy.

**Verify B1 with one query after the first fill of day 3:**
```sql
select id, symbol, purpose, stop_price, status from orders
where purpose = 'stop_loss' and created_at::date = current_date;
-- expect: stop_price with exactly 2 decimal places, status not 'rejected'
```

Day 2 is **readable but not conclusive**. The platform is sound in its safety design — the
halt, the carve-out, the reminders and the engine-side fallback all did their jobs. What it
lacks is the last inch of wiring on three guards that were already written: `round_price`,
`RESERVED_TEST_SYMBOLS`, and an alert route for the one condition that most needed one.
