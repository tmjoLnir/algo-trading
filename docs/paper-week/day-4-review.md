# Paper Week — Day 4 Review

**Session:** 2026-09-11 · **Log window:** 12:10:11Z → 23:01:15Z · **RTH:** 13:30–20:00Z
**Source:** 14,916 docker log records across 6 containers, cross-checked against the repository at `849c461`
**Config:** `sma_crossover`, 20 symbols, Alpaca paper, IEX feed, `run_mode=paper`, config revision 6
**Predecessor:** `day-3-review.md` (2026-09-09). 12 commits landed between the two sessions, carrying the day-3 fixes for B1 (host sleep, via ADR 0033), the B2 exception classification, the crash-loop guard, missed-fill recovery, and an operator entry point for `adopt_broker_state`.

---

## 1. The verdict

**Day 4 ran.** That is new, and everything below is downstream of it.

The host stayed awake for the entire 10.85-hour capture — **zero dark minutes**, against day 3's
8.8 hours. One worker process served the whole session. Reconciliation ran 78 times and came back
clean 78 times, against a book that grew to 14 positions. There were no halts, no staleness
detections, no stream reconnects, no 5xx responses, and no crash loop. The session summary and
the daily report both ran on time, for the first time in the paper week. Three of day 3's four
blocking fixes are confirmed working in production.

**And the first session that actually functioned measured one thing: this platform cannot exit a
position.**

The strategy generated **39 exit signals. One reached the venue.** The other 38 were refused,
every one of them with the same Alpaca error:

```
{"available":"0","code":40310000,"existing_qty":"6","held_for_orders":"6",
 "message":"insufficient qty available for order (requested: 6, available: 0)"}
```

The protective stop is holding the shares. `submit_protective_orders` places a GTC sell stop for
the whole position the moment the entry fills; the venue then reserves that inventory against the
working stop, so the market sell that would close the position has nothing to sell.
`_disarm_if_flat` is the only thing that cancels the stop, and it waits for the position to go
flat — which cannot happen, because the order that would flatten it is the one being refused.
It is a deadlock, and `runner.py:1878` states its own half of it without noticing:

> **The position going flat is the moment, not the exit being accepted.** [...] Waiting for flat
> costs the window between the exit filling and this line, which is the same event handler.

The exit never fills. There is no window.

**So every position that closed on day 4 closed by being stopped out**, and the result is what
that arithmetic makes it:

| | |
|---|---|
| Round trips closed | **41** |
| Winners | **0** |
| Scratches | 1 (PFE, exactly flat) |
| Losers | **40** |
| Realised P&L on day-4 trades | **−$251.60** |

Zero winners is not a verdict on `sma_crossover`. It is arithmetic: the only exit the platform
can execute is the stop, the stop is below the entry, and a position that only ever leaves at its
stop only ever leaves at a loss. **The strategy's exit rule was never tested.** It fired 39 times
and was silenced 38 of them.

**The one exit that worked proves the mechanism.** Day 3's orphaned MSFT position was recovered
and booked at 14:04:41; its protective stop was refused by the risk chain one millisecond later
(`stale_data` — the market-data stream had not connected yet), so nothing was holding the shares.
At 14:21:48 the strategy signalled an exit on MSFT and it went straight through, filling at
494.95 against a 493.232 entry: **+$17.18, the day's only profitable close, on the only position
with no stop on it.**

**Three numbers describe the session:**

| | |
|---|---|
| Orders the venue accepted | **102** of 209 attempts |
| Orders the venue refused | **107** (69 wash-trade, 38 insufficient-qty) |
| Exit signals executed | **1 of 39** |

**And 68 CRITICAL alerts about unprotected positions reached nobody.** `order.position_unprotected`
fired 70 times at `critical`. Exactly **two** protection alerts were sent all day — one at
14:04:43 and an all-clear at 14:22:49. Day 3's F1 asked for deduplication because 129 identical
pages went to one human; the deduplication landed and now swallows everything after the first
all-clear. The platform went from paging 129 times about one problem to paging zero times about
sixty-eight.

**Recommendation: fix the exit path before day 5, and do not count day 4 as a measured session.**
The infrastructure is finally sound enough to trade a full day, which is real progress and should
be said plainly. What it traded was a strategy with its exit rule disconnected, which is not the
strategy anybody intends to run.

---

## 2. Timeline

| Time (UTC) | Event |
|---|---|
| *(before 12:10)* | A worker is already running — ingestor and scheduler. Its boot predates the capture |
| 12:10:11 | Capture opens. `data.bars.upserted`, Redis background saves, healthchecks every ~15 s |
| 12:30:00 | Corporate actions: `skipped_seed_symbols ZVZZT ZWZZT ZXZZT`, then `done series=68 adjustments=0`. **No `inconsistent` warning — BAC is clean this time** |
| 13:25:00 | `rollover_daily_counters` |
| 13:30:00 | **Market open.** `execution.fees.settled owed=0.01 cash=95157.73` — the pre-restart worker holds MSFT and $95,157.73 |
| 13:30:01 | `execution.reconcile.clean open_orders=0 positions=1`. Clean every five minutes from here |
| 13:30–14:04 | **34.7 minutes of RTH with no strategy evaluation.** Zero `runner.evaluated`. The capture cannot say why (F11) |
| 14:04:33 | **Operator bounces the whole stack.** SIGTERM to queue, fast shutdown to db, Redis saves and exits. The worker logs *nothing* (F10) |
| 14:04:38 | db, redis back. 5.1 s of downtime |
| 14:04:39.413 | **Boot 1 — the only boot of the day.** `run_mode=paper` |
| 14:04:39.780 | `restored_book cash=100090.06 positions=[]` — **day 3's closing book, not the one this stack held four minutes ago** (B2) |
| 14:04:39.88–40.59 | `warmup_short_history` ×20 — `have=34 needed=51` for 14 symbols, 24–31 for the rest. **Every symbol short. It starts anyway** (F6) |
| 14:04:40.599 | `restored_open_orders count=1` — day 3's `atp-e82c4109cc2c0c8886b0e0b5` |
| 14:04:41.331 | **`execution.recovery.missed_events fills=1`** — the day-3 fix works: the missed fill is *booked*, not reported. No crash loop |
| 14:04:41.332 | `order.risk_denied rule=stale_data` — the protective stop for the recovered 10 MSFT is refused: *'no market data has arrived for MSFT'*. **CRITICAL, and the day's first alert** (F9) |
| 14:04:41.342 | `caught_up_on_orders applied=1 cash=95157.74 positions=['MSFT']` |
| 14:04:42.555 | `stream_connected feed=iex` — **1.2 s too late for the stop** |
| 14:04:42.557 | `warmed_up bars=645 needed=51 short=20` |
| 14:04:43.537 | Evaluation 1 |
| 14:21:48 | **`order.submitted MSFT sell 10 market`** → filled 494.95. The day's only executed exit, on the only unprotected position |
| 14:22:49 | `alert.sent key=protection.restored` — **the last protection alert of the day** |
| 14:27:50 | **First entry.** 3 signals, 4 orders. SPY, AAPL, KO |
| 14:27:51.693 | First wash-trade rejection → `position_unprotected KO qty=27`, **`refusals=['']`** (F2) |
| 14:27:52.494 | First `protective_stop_placed` — SPY, 6 shares, after the parent completed |
| 14:31:52 | First stop-out. SPY 765.99 → 765.09, **−$5.39**, held 4 minutes |
| 15:17:06 | **First refused exit.** `signal_refused action=exit reason= rule= symbol=PFE` (F3) |
| 16:07:25 | SPY exit refused: `available=0 held_for_orders=6`. The pattern for the next four hours |
| 19:59:05 | Evaluation 353, the last. `open_positions=10 refused=38 submitted=51` |
| 19:59:33 | DIA stopped out — 27 s before the close, after the final evaluation |
| 20:00:00 | **`worker.session_summary evaluations=353 halted=False orders_submitted=51`** — on time, for the first time in the paper week |
| 20:00:05 | `market_closed sleeping_seconds=235794` |
| 20:30:00 | **`worker.daily_report`** — `'209 submitted, 93 filled, 0 refused'`, 9 halts, no P&L (F4) |
| 23:01:15 | Capture ends. 9 positions held overnight, $44,277 of exposure, **all with working GTC stops** |

1 boot. 0 halts. 4 alerts. 209 orders.

---

## 3. The blockers

### B1 — The protective stop reserves the position, so the strategy can never exit it `blocker`

**Evidence.** 38 of 39 exit signals were refused, and all 38 carried the same venue error —
a one-to-one mapping with no exceptions:

| | |
|---|---|
| Exit signals generated | 39 |
| Submitted and accepted | **1** (MSFT, 14:21:48 — no stop was holding it) |
| Refused `insufficient qty available` | **38** |
| Refused for any other reason | 0 |

Refused exits, by symbol: INTC 5, BAC 4, XOM 4, QQQ 3, SPY 3, JPM 3, KO 3, PFE 2, PEP 2, GE 2,
AMZN 2, WMT 1, DIA 1, AAPL 1, IWM 1, MSFT 1. Sixteen of the twenty names in the universe.

**The mechanism, end to end.** Take SPY's second cycle:

```
15:18:07.502  order.submitted      buy 6 SPY market
15:18:08.265  filled               5 of 6 @ 765.32        (partial)
15:18:08.508  order.broker_rejected  wash trade — opposite side market/stop order exists
15:18:08.508  position_unprotected   qty=5   refusals=['']
15:18:09.055  filled               6 of 6 @ 765.32        (complete)
15:18:09.325  protective_stop_placed qty=6   ← the venue now holds all 6 shares
16:07:25.795  order.broker_rejected  insufficient qty (requested 6, available 0, held_for_orders 6)
16:07:25.799  signal_refused       action=exit reason= rule= symbol=SPY
17:56:10.434  order.broker_rejected  insufficient qty — again
19:09:52.974  filled               stop 6 @ 764.76  → −$3.36
```

The strategy asked to leave at 16:07 and at 17:56. It left at 19:09, at its stop, 112 minutes
later and $3.36 worse off. Multiply by 41.

**Why the code does this.** Two call sites, and neither cancels protection before submitting a
close.

`OrderRouter.submit()` handles a strategy exit (`router.py:314-320`) by sizing the order to
`abs(position.qty)` and submitting it. **There is no `cancel_protection` anywhere on that path.**

`OrderRouter.flatten()` does call it — and in the wrong order (`router.py:835-837`):

```python
result = await self.submit(request, portfolio)
if result.submitted:
    await self.cancel_protection(symbol)
```

Cancel-after-submit cannot work: the submit is what the reservation blocks.

And `StrategyRunner._disarm_if_flat` (`runner.py:1878-1908`) is deliberately the only other
cancel, gated on the position already being flat. Its docstring argues the case carefully and
correctly against cancelling at acknowledgement:

> `flatten` pins `MARKET`/`GTC`, while `submit_signal` builds an exit that is `LIMIT` when the
> signal carries a price and `DAY` either way. So a cancel at acknowledgement de-arms a position
> whose exit may never fill — a limit that never trades, or a partial that leaves shares behind —
> and the remainder sits with no venue stop, no engine-side watch [...] **That is the state this
> file calls the worst one in the system, reached while trying to leave it.**

Every word of that is right, and it reasons past the case that actually happens: the venue
refuses the exit outright, so waiting for flat waits forever. The reasoning assumed the only
failure mode was a *partial* exit. The real one is a *rejected* exit.

**The corroborating fingerprint** is `order.protection_cancelled`, logged 42 times, every single
one of them `cancelled=0 still_live=0`. The cancel only ever runs once the stop has already
filled, at which point there is nothing left to cancel. A method whose counter is zero 42 times
out of 42 is never doing its job.

**Fix.** Cancel first, confirm, then close — and re-arm if the close does not complete:

1. **Cancel protection before submitting an exit**, on both paths (`submit()`'s EXIT branch and
   `flatten`). Wait for the venue's cancel acknowledgement, not just the request.
2. **Re-arm on any remainder.** This is the hole `_disarm_if_flat` was protecting against, and it
   is a real one — so handle it rather than avoid it: if the exit is refused, partially fills, or
   expires, place a stop over whatever quantity is still held, and alert if that fails. The
   engine-side armed level is the cover for the gap in between, which is what it is for.
3. **Treat `insufficient qty available` as its own outcome, not a generic rejection.** It means
   "your own resting order holds this inventory", which is a platform bug every time — never a
   market condition. It should be loud, and it should name the order doing the holding:
   the rejection body already carries `held_for_orders`.
4. **Assert the invariant in a test at the router level**: an exit signal on a protected position
   must reach the venue. Day 4 had 353 chances to notice and no test covers it, because every
   unit test uses a fake broker that does not reserve inventory (`tests/fakes.py`). The fake
   should model the reservation — that is the behaviour that broke production.

**The smallest change that would have turned this day into a real measurement** is moving
`cancel_protection` from after the submit to before it, on both paths.

### B2 — The restored book was two days old, because the only thing that writes it is the evaluate loop `blocker`

**Evidence.** At 14:00:10 the running worker reconciled clean with `positions=1`, and at 13:30:00
it reported `cash=95157.73`. Four and a half minutes later the stack was bounced, and the new
process read this:

```
14:04:39.780  worker.restored_book  cash=100090.06000000  positions=[]
              msg='starting from our own stored book — the broker is about to be asked to agree'
```

`100090.06` is **day 3's closing cash**, to the cent (`day-3-review.md` §5). `positions=[]` is
day 3's position list. The book that the pre-restart worker held — one MSFT position, $4,932 of
cash moved — was never written down. Two days of state were missing from what the platform calls
"our own stored book".

What saved it was the day-3 recovery fix doing its job: `execution.recovery.missed_events`
re-read the venue, booked the missed fill, and arrived at `cash=95157.74`. The platform
reconstructed the truth from Alpaca rather than from storage.

**Why.** `PortfolioRepository.snapshot` has exactly one caller in the worker —
`StrategyRunner._persist` (`runner.py:1062`) — and `_persist` is **step 6 of `evaluate`**
(`runner.py:987`). A worker that is not evaluating never writes a book. The pre-restart worker
logged 7,000 bar upserts, eight scheduled reconciles and a fee settlement across two hours, and
**zero `runner.evaluated`** — so it wrote zero snapshots, for two hours, while holding a position.

This is the third appearance of one shape. Day 3's F10 found the unprotected-position alert
hanging off the evaluate loop, so it could not fire on the path that killed the worker. Day 4's
B2 is the book hanging off the same loop. **Anything reachable only from inside `evaluate` does
not exist for a worker that is halted, pre-open, or running without a strategy** — which are all
normal states.

**This is the demonstration the roadmap was waiting for, and it failed.** `docs/ROADMAP.md:1591`,
on *Order and position persistence*:

> Unticked. The SQL is exercised by 15 integration tests against a real Postgres in CI, but
> **nothing here has survived an actual restart of a running worker** — which is the
> demonstration, and it is one the paper week produces for free the first time the process is
> bounced.

The process was bounced at 14:04:33. It did not survive. The same roadmap item explains exactly
what this costs:

> Before it, the runner's book lived only in memory, so every boot adopted the broker's
> wholesale — which made reconciliation across a restart clean *by construction* and therefore
> **worthless as evidence**.

Day 4's 78 clean reconciles are the strongest result in this review, and B2 is the reason they
are not yet the evidence Phase 4 needs: the book the broker agreed with was reconstructed *from
the broker* ninety minutes into the session.

**Fix.**
1. **Snapshot the book on every change, not once per evaluation.** A fill is the event, and
   `_apply_to_portfolio` is where it lands. At minimum, snapshot from the reconcile job too —
   it runs every five minutes regardless of whether the strategy is evaluating.
2. **Snapshot on shutdown**, which requires F10's SIGTERM handler to work.
3. **Make a stale restore visible.** `restored_book` should carry the snapshot's age. A book
   written 50 hours ago is not "our own stored book" in any useful sense, and one log field would
   have made this self-evident rather than something a reviewer has to subtract two cash figures
   to find.
4. **Leave the roadmap item unticked and say why** — not "no restart yet", but "restarted, and
   the book did not survive it".

---

## 4. Findings

### F1 — 68 of 70 CRITICAL unprotected-position events reached nobody `high`

| | |
|---|---|
| `order.position_unprotected` at `critical` | **70** |
| `runner.position_unprotected` at `error` | 70 |
| Protection alerts actually sent | **2** |

The two were `protection.unprotected` at 14:04:43 (MSFT, the boot race) and
`protection.restored` at 14:22:49. Every unprotected event from 14:27:51 onward — 68 of them,
across 18 of the 20 symbols — produced no alert.

Day 3's F1 asked for deduplication by key at the transport, because 129 identical pages had gone
to one human. What landed over-corrected: after the all-clear at 14:22:49, the dedup state
treats each new unprotected set as already-announced and nothing else gets through for five and a
half hours. The symbol sets were *not* the same — KO, AAPL, SPY, QQQ, BAC, PFE, AMZN, JPM, PEP,
IBM, CSCO, GE, IWM, GOOGL, INTC, WMT, JNJ and MSFT each went unprotected at different times.

In fairness, the exposure behind most of them was brief: the wash-trade rejections were followed
by an accepted stop within 300–800 ms once the parent order completed. But the platform does not
know that when it suppresses the alert, and one of the 70 was 17 minutes long (F9).

**Fix.** Dedup with a cooldown *and a floor*: suppress repeats of the same symbol set inside a
window, but never suppress a set that contains a symbol not in the last announcement, and always
emit a rollup at a fixed cadence while anything is unprotected ("4 positions unprotected,
longest 11 s"). Day 3 was paged 129 times about one dead task; day 4 was paged zero times about
sixty-eight naked positions. Neither is alerting.

### F2 — The unprotected alert's reason is empty, while the line above it carries the full text `high`

Every one of the 69 wash-trade cases logged this pair, two microseconds apart:

```
[critical] order.position_unprotected   detail='Alpaca refused POST /v2/orders: {"code":40310000,
             "message":"potential wash trade detected. use complex orders",
             "reject_reason":"opposite side market/stop order exists"}'  symbol=KO  qty=27
[error   ] runner.position_unprotected  refusals=['']  symbol=KO  unprotected_qty=27
```

The router's line has the venue's exact words. The runner's line — **the one the alert is built
from** — has `['']`. A human paged by this gets "KO, 27 shares, unprotected, reason: blank".

This is day-2's F6 (*"unprotected alert has empty reason"*), recorded as not-fixed on day 3 and
still not fixed. Day 3's note said the reason *"now doesn't reach a log line at all — the
rejection raises instead"*. The rejection no longer raises (that fix landed, and it is why day 4
has a session at all) so the line is back — and it is still empty. The reason has now failed to
arrive by two different routes across three sessions.

**Fix.** `ProtectionResult` should carry the refusal text from the `SubmitResult` that produced
it. The data is in scope at the call site; it is being dropped on assignment, not lost.

### F3 — A venue rejection is reported as a risk refusal, with the risk fields blank `high`

All 38 refused exits logged:

```
runner.signal_refused  action=exit  correlation_id=...  reason=  rule=  symbol=SPY
```

`reason` and `rule` are empty because `runner.py:1518-1519` logs `result.decision.rule` and
`result.decision.reason` — the *risk chain's* decision. The risk chain approved these orders.
What refused them was Alpaca. The log line is reporting the wrong layer's verdict and, having
nothing to report, reports nothing.

It is also counted wrong. The same branch increments `stats.orders_rejected_by_risk`
(`runner.py:1512`), so `runner.evaluated` ends the day claiming `refused=38` — 38 refusals by a
risk chain that refused nothing. Meanwhile the daily report, reading order rows rather than the
counter, says `orders_refused=0`. **Two numbers, both wrong, disagreeing with each other about
the same 38 events.**

A reviewer reading only these lines would conclude the risk config was too tight. The truth is
the opposite: risk approved every exit and the platform's own resting orders blocked them.

**Fix.** Distinguish "refused before submission" from "refused by the venue" in the log event,
the counter and the order status. A `SubmitResult` that failed at the broker should not be
narrated with `RiskDecision` fields.

### F4 — The daily report ran, and four of its five numbers are wrong `high`

The first daily report of the paper week. It is worth having, and it is worth reading closely:

```
2026-09-11 — 209 submitted, 93 filled, 0 refused
  symbols        AAPL, AMZN, BAC, CSCO, DIA, GE, GOOGL, IBM, INTC, IWM, JNJ, JPM, KO, MSFT,
                 PEP, PFE, QQQ, SPY, WMT, XOM
  trades         93  (93 filled of 209 submitted)
  risk rejections 0  (nothing was refused by the risk chain)
  halts          9  (9 recorded — operator halts only; ...)
  feed incidents NOT MEASURED — ...
```

| Claim | Truth |
|---|---|
| `209 submitted` | **102 reached the venue.** The other 107 were refused by it. `orders_submitted=len(orders)` (`daily.py:168`) counts every order *row*, and `_record_refusal` writes a row for a rejection. This is day-2's **F1, verbatim, still open** |
| `93 filled` | Defensible, and it counts an order with one partial fill as filled. 142 of 236 fill events were partials |
| `0 refused` | **38 exits were refused.** `refused` filters `OrderStatus.REJECTED_RISK` (`daily.py:148`), and venue rejections are not that status. The parenthetical *"nothing was refused by the risk chain"* is literally true and the headline built from it is not |
| `9 halts` | **There were no halts on day 4.** `session_summary` says `halted=False`; no halt event appears anywhere in 14,916 records. The 9 rows come from `session_start = now - timedelta(days=1)` (`scheduler.py:426`) — a rolling 24-hour window labelled `day=2026-09-11`. Day-2's **F14**, still open |
| `feed incidents NOT MEASURED` | Correct, honest, and the right way to report it |

**And the report contains no P&L.** On a day with 41 round trips and −$251.60 realised, the only
end-of-day artifact says nothing about money. `DailyReport` has `realised_pnl`, `starting_equity`
and `ending_equity` fields, and `summarise()` accepts the last two — but `scheduler.py:448` calls
`summarise(now.date(), orders, audit=audit)` and passes neither, so `pnl_change` is `None` and
`headline()` silently drops the equity clause. The snapshots it needs are in
`PortfolioRepository`, which the same job already has a session factory for.

`realised_pnl` is computed and then not logged, which is as well: it is
`sum(avg_fill_price * filled_qty * side.sign)` (`daily.py:155`), the net cash flow of the day's
fills, not a P&L. With 50 buys and 42 sells of different quantities it is a number with no
meaning.

**Fix.** Three separate changes, smallest first: pass the equity snapshots; window the report on
the trading day rather than on 24 rolling hours; and split "submitted" from "accepted by the
venue" everywhere the word appears. Then compute realised P&L by pairing entry and exit legs, or
report it as absent — `_feed_incidents` is already the model for how this report says "nobody
counts this".

### F5 — Protection is still submitted against a working parent: 69 wash-trade rejections `high`

Day 3's B2 fix list opened with *"Do not place protection while the parent is working."* It was
not done, and `submit_protective_orders` still documents the opposite as intent
(`router.py:441-443`):

> **Covers what has filled since the last call**, capped at the exposure actually held. An entry
> that fills in pieces gets a stop per piece.

"A stop per piece" met a venue that refuses an opposite-side stop against a working market order
**69 times**, across 18 symbols. The shape is always the same, and KO's first cycle shows it
worst — four rejections on one entry as it filled in tranches of 27, 22, 2 and 1:

```
14:27:51.693  position_unprotected KO qty=27
14:27:52.735  position_unprotected KO qty=49
14:27:53.456  position_unprotected KO qty=51
14:27:53.694  position_unprotected KO qty=52
```

What changed from day 3 is that this is no longer fatal: `InsufficientFundsError` is now a
subclass of `OrderRejectedError` (`errors.py:97`), `_route` catches it, and the day logged 107
`order.broker_rejected` events with `classified=True` and zero exceptions. **The crash is fixed.
The cause of the crash is not.**

It is not harmless either. Every rejected tranche is a real unprotected window — short (300–800
ms) but real — and it is what exhausts F1's alert budget in the first thirty seconds of trading.
And it costs one wasted `POST /v2/orders` per partial fill against a 30/minute rate limit.

**Fix.** As day 3 said: arm the level, place the venue-side stop when the entry is terminal, and
let the engine-side stop cover the gap. The parent's state is already on the object —
`entry_order.is_complete` is consulted at `router.py:611`, for bookkeeping, *after* the submit
that needed it.

### F6 — The strategy started trading six bars after its slow average became computable `high`

Third session running. Warmup was honest and then irrelevant:

```
runner.warmup_short_history  have=34  needed=51  ×14 symbols
runner.warmup_short_history  have=24–31  needed=51  ×6 symbols   (DIA 24, IBM 25, XOM 29, JNJ 30, GE 30, PEP 31)
runner.warmed_up  bars=645  needed=51  short=20
```

All twenty symbols short, and it started anyway. The first entry came at 14:27:50, 23 minutes
and ~23 bars later — so `SMA(50)` had existed for about **six bars** when the day's first trade
was taken, and for about two minutes when the first three were.

Day 3's F2 found the same thing (53 bars against a 50-period average) and day 2's F8 found it
with the average stitched across a four-day weekend. The recommendation each time was to gate
rather than warn. `warmup_short_history` has now fired 540 times across three sessions without
once preventing a trade.

**Fix.** A symbol whose series is shorter than the strategy's declared lookback is not tradeable.
Exclude it from evaluation until it is, log how many symbols are held back, and let the daily
report carry the number. The `not_before` session boundary from day 2's F8 is correct and should
stay — the answer is to wait for real bars, not to reach back for stale ones.

### F7 — The strategy acts on bars that are 60 to 120 seconds old, on a one-minute timeframe `high`

`newest_bar_age_seconds` across 353 evaluations: **median 96.5 s, range 60.4–120.4 s.** It never
once dropped below 60.

The evaluation loop runs every 60.2 s and is not aligned to the minute boundary, so it sawtooths:
the age climbs by a fraction of a second per pass until a newer bar arrives and it drops ~60 s.
Roughly half of all evaluations are acting on a bar that closed **two bars ago**.

On a 1-minute strategy with stops 0.10% wide (F8), that is not a detail. Every signal is computed
against a close up to two minutes stale and then filled at market. The backtest engine fills at
the *next* bar's open and enforces it (`CLAUDE.md` §5); live, the fill lands one to two bars later
than that. **The live loop and the backtest are not modelling the same strategy**, which is the
comparison Phase 5's live-vs-backtest item exists to make.

**Fix.** Drive evaluation from bar arrival rather than from a 60-second timer, and make the lag a
first-class metric with a threshold — a 1-minute strategy acting on 2-minute-old data should say
so, not leave it in a log field nobody aggregates.

### F8 — An ATR(14)×2 stop on one-minute bars is about 0.10% wide `high`

`stop='atr x2 period=14'` against a `1m` timeframe. The realised distance between entry and
stop-out, across 41 round trips:

| | |
|---|---|
| Median | **0.104%** of entry |
| Range | 0.000% – 0.613% |
| Median loss | **−$5.00** on a median $4,910 position |
| Median hold | **11.5 minutes** (range 0.2 – 289.3) |

Two ATRs of a one-minute bar is a handful of basis points. One trade was stopped out **12
seconds** after entry. Even with B1 fixed, a stop this tight converts ordinary intrabar noise
into a closed loss, and the strategy's edge — whatever it is — has to clear the spread twice plus
that distance to show up at all.

This is not a bug; it is a parameter set nobody has chosen. Day 3's F12 recorded that the
timeframe question was settled by declaring `1m`, and that *"the strategy is running 20/50-minute
periods and its parameters have still never been tuned for that horizon."* The stop inherited the
same non-decision.

**Fix.** Pick the horizon deliberately and size the stop to it. If the intent is a 1-minute
strategy, the ATR multiplier and the SMA periods both need to come from a backtest on 1-minute
bars — which `backtest/` can run. Right now the platform is live-testing a configuration that has
never been backtested at its own timeframe.

### F9 — A recovered position's stop is refused because the market-data stream has not connected yet `high`

```
14:04:41.332  order.risk_denied  rule=stale_data  side=sell qty=10 symbol=MSFT
              reason='no market data has arrived for MSFT'
14:04:41.332  order.position_unprotected  qty=10  rule=stale_data        ← CRITICAL
14:04:42.555  data.alpaca.stream_connected  feed=iex  symbols=20         ← 1.2 s later
```

Boot ordering. `execution.recovery.missed_events` books the missed fill and `_protect` fires
immediately, 1.2 seconds before the stream connects, so `RiskEngine`'s `quote_age=30s` rule sees
no quote at all and refuses the protective order. The rule is right — a stop priced off no data
is worse than no stop — but the sequencing makes it unwinnable.

The position then sat unprotected from 14:04:41 to 14:21:48: **17 minutes and 7 seconds, $4,950
of exposure, no venue stop and no engine-side level.** It ended only because the strategy happened
to signal an exit.

There is a sharp irony here, and it is the single most useful sentence in this review: *that
failure is the only reason any exit worked all day.* The bug in F9 is what exposed the blocker in
B1.

**Fix.** Connect and subscribe the stream before recovery runs, or retry protection on the first
quote. The runner already has a tick handler (`_on_tick`) and an `_unprotected` set; a position
recorded as unprotected should be re-armed when data arrives, rather than waiting for a strategy
signal. And `docs/SAFETY.md:125`'s gate — *"there are no unprotected positions"* — is violated for
17 minutes of day 4, which is an improvement on day 3's 6 h 56 m and still a violation.

### F10 — The worker logged nothing when it was shut down `medium`

At 14:04:33 the stack was bounced. `atp/queue` logged `shutdown on SIGTERM` and `queue.stopped`;
`atp/db` logged a fast shutdown and a clean checkpoint; `atp/redis` saved its final RDB and said
goodbye; `atp/api` logged `Shutting down` / `shutdown` / `Application shutdown complete`.

**`atp/worker` logged nothing at all.** Its last line is the 14:00:10 reconcile. Day 3's worker
managed `worker.stopped  msg='signal received — shut down cleanly, nothing halted'`; day 4's did
not, so the capture cannot say whether the SIGTERM handler ran, timed out, or never fired.

This matters more than a missing log line, because B2's fix depends on it: a book snapshot
written on shutdown is only as reliable as the shutdown path, and right now there is no evidence
the shutdown path executes. It also means a reviewer cannot distinguish an operator restart from
a crash without reading four other containers.

**Fix.** Log `worker.stopping` on signal receipt and `worker.stopped` after the last write, and
check the container's `stop_grace_period` against how long the drain actually takes.

### F11 — 34.7 minutes of regular trading hours passed with no strategy evaluation, and nothing says why `medium`

The market opened at 13:30:00. The first `runner.evaluated` is at 14:04:43 — after the restart.
The worker that was running across the open logged bars, scheduled reconciles and a fee
settlement, and **zero evaluations**. The strategy was not asked anything for the first 8.9% of
RTH.

The capture cannot explain it: that worker's `worker.ready` line predates the log window, so its
`responsibilities` list is unknown. It may not have had `strategy_runner`; it may have had it and
been in a state that skipped the loop. Either way the platform has no self-description a reviewer
can read after the fact.

**Fix.** Re-announce `worker.ready`'s responsibilities periodically — hourly, or at each
reconcile — so a log window that opens mid-process can still say what the process was doing. And
give the daily report a coverage number: **minutes of RTH with an evaluating runner, against
minutes of RTH.** Day 4 would read `355 / 390`. Day 3 would have read `74 / 390`. That single
field would have made day 3's blocker visible from the report rather than from healthcheck
archaeology, and it is the number the paper week is actually trying to accumulate.

### F12 — The dashboard WebSocket reconnected 80 times `low`

`ws.connected` and `ws.disconnected` each fired 80 times between 14:04:12 and 19:59:33, a median
of 181 seconds apart. The browser was open and reconnecting roughly every three minutes for six
hours. One `ws.dropping_client` at 15:27:13 carries `error=` — another empty reason field (F2's
shape, benign here).

No trading impact, and the dashboard stayed available (1,335 × 200, 78 × 101 upgrade, zero 5xx,
zero 401s). But TanStack Query owns server state by convention and a WS that drops every three
minutes is either an idle timeout nobody configured or a keepalive nobody sends.

**Fix.** Find the timeout — nginx `proxy_read_timeout`, or no ping frame — and either extend it or
send heartbeats. Then log reconnects at a level that aggregates, because 80 reconnects and 1
reconnect look identical in this log.

### F13 — Operational papercuts `low`

- **`execution.fees.settled` fired twice for the same one cent** (13:30:00 and 14:04:41), both
  reporting `cash=95157.73`, with `total_seen=4.15 owed=0.01`. Day 3's F12 recorded the same
  thing for $4.14 and asked for the second one to be named `fees.level`. It still is not — and
  day 4 adds a twist: the double-settle produced the right answer **because** the first one was
  lost in B2's missing snapshot. The idempotency was not tested; it was bypassed.
- **The bar store takes one round trip per bar per symbol.** 7,244 `data.bars.upserted` calls
  wrote 35,948 rows, and **7,176 of those calls wrote exactly one row.** Six calls carried the
  backfills (842–1,595 rows each) and 48 carried three. Redis logged `10000 changes in 60
  seconds` continuously and `atp/redis` alone produced 2,468 log lines. Batching the live stream
  per tick-flush rather than per bar is a one-line change to the ingestor's buffer.
- **`runner.warmed_up bars=645`** sums bars across symbols, so the headline number looks healthy
  (645) while every constituent is short (24–34 against 51). Report the minimum, or the count of
  short symbols — which the very next field already does, as `short=20`.
- **The nginx duplicate-`Date`-header warning is still on every WebSocket upgrade**, 79 times,
  unchanged from day 2's F12 and day 3's F12. Three sessions is long enough to either fix the
  upstream header or silence the warning.
- **The `queue` container logged 5 lines and is working.** It appeared for the first time in the
  paper week: `queue.ready max_jobs=1 queue=atp:jobs`, 3 functions registered, `0 jobs complete`.
  Day 2 and day 3 could not tell whether `arq` was idle or absent; it is idle, and now says so.
- **BAC's corporate-action history is clean this session.** `corporate_actions.done series=68
  adjustments=0 split_like=0`, no `inconsistent` warning. Day 3's F5 (36% of BAC bars agreeing)
  did not recur — but nothing was fixed, so it is unreproduced rather than resolved, and the
  quarantine day 3 asked for still does not exist.
- **The strategy still declares `timeframe: 1m` with 20/50 periods and an ATR(14)×2 stop**, none
  of which has been backtested at that timeframe (F8). Unchanged from day 3's F12.

---

## 5. The day's result

There is one, for the first time in the paper week. It is not a measurement of the strategy.

| | |
|---|---|
| Evaluations | **353** (14:04:43 → 19:59:05, every 60.2 s) |
| Signals generated | **89** — 50 entries, 39 exits |
| Entries submitted | 50 of 50 |
| Exits submitted | **1 of 39** |
| Orders attempted at the venue | **209** |
| Accepted | 102 — 50 buy market, 1 sell market, 51 sell stop |
| Refused | **107** — 69 wash-trade, 38 insufficient-qty |
| Fill events | 236 — 142 partial, 94 terminal |
| Protective stops placed | 51 (GTC) |
| Protective stops filled | **42** |
| Round trips closed (day-4 entries) | **41** |
| Winners / scratches / losers | **0 / 1 / 40** |
| Realised P&L (day-4 trades) | **−$251.60** |
| Day-3 MSFT carryover, closed | **+$17.18** |
| Net realised | **−$234.42** |
| Median trade | −$5.00 on $4,910, held 11.5 min |
| Worst trade | INTC, **−$30.36** |
| Best trade | PFE, **$0.00** |
| Notional deployed | $241,340 across 50 entries |
| Fees | $0.01 new; $4.15 total seen |
| Reconciles | **78, all clean. Zero mismatches** |
| Halts | **0** |
| Positions held overnight | **9** — KO 56, XOM 30, AMZN 19, INTC 48, WMT 46, MSFT 10, PEP 36, CSCO 44, IWM 17 |
| Overnight exposure | ~$44,277, **all with working GTC stops** |

**Every symbol that closed a trade lost money.** Nineteen symbols, nineteen negative totals:
AAPL −32.27, INTC −30.36, GE −22.22, AMZN −18.15, PFE −16.32, MSFT −15.54, IWM −13.14,
QQQ −12.24, CSCO −11.08, SPY −10.36, IBM −10.35, JNJ −10.32, PEP −10.27, BAC −8.69,
GOOGL −7.96, KO −7.80, JPM −7.76, WMT −3.76, DIA −3.01.

A uniform result across twenty uncorrelated names is not a market observation. It is a
mechanism, and B1 is the mechanism.

**What day 4 did measure, and it is worth having:**

- The stack runs a full session on this host without sleeping.
- One worker process survives six hours of trading, 209 orders and 236 fills without dying.
- The book agrees with Alpaca 78 times out of 78, across a position count that reached 14.
- Protective stops are GTC and survive the close — the nine overnight positions are genuinely
  protected, which no previous session could claim.
- Both scheduled end-of-day jobs run and report.

**What nobody should read into it:** that `sma_crossover` loses money. The strategy was asked 39
times to close a position and was permitted to do so once. Until B1 is fixed, every paper session
measures the exit path, not the strategy.

---

## 6. What worked

Three of day 3's four blocking fixes are confirmed working in production, and the fourth is
confirmed *present*.

- **B1 (the host sleeps) — fixed, and the decision was reversed on evidence.** Zero dark minutes
  in 10.85 hours; no gap over two minutes in any container's heartbeat, and none in the whole
  stack's combined log. ADR 0033 superseded ADR 0032's move to Oracle and kept the Mac, on the
  grounds that the sleep condition had finally been applied and read back rather than assumed.
  Day 4 is that ADR's evidence, and it holds. The four staleness detections, 16 dark windows and
  131 boots of day 3 are all absent.
- **B2 (the crash loop) — fixed, all four parts.** `InsufficientFundsError` is now a *subclass* of
  `OrderRejectedError` (`errors.py:97`), and its docstring names day 3 as the reason — so the 107
  venue refusals of day 4 were caught, logged as `order.broker_rejected classified=True`, and
  continued. Zero exceptions, zero `responsibility_ended`, zero restarts. Day 3's 129 identical
  boots became one.
- **The missed fill is booked, not reported.** `execution.recovery.missed_events ... fills=1
  detail='the venue moved these orders while we were not listening — booking them now'` followed
  by `caught_up_on_orders applied=1 cash=95157.74 positions=['MSFT']`. Day 3's orphan position —
  the thing that crash-looped 129 boots — was absorbed in 11 milliseconds. **This is the fix that
  made day 4 possible.**
- **`scripts/adopt_broker_state.py` exists**, closing day 3's complaint that `docs/RUNBOOK.md`
  prescribed a method call with no operator entry point. Day 4 never needed it, because recovery
  handled the case automatically — which is the better outcome.
- **Protective stops are GTC, deliberately.** `router.py:926` explains why a DAY stop would
  falsify `StrategyRunner.shutdown`'s contract through the overnight gap. Nine positions are
  carried into 2026-09-14 with venue-side protection that will survive the weekend.
- **The kill switch and the three locks held.** `allow_live_orders=False` on the single boot,
  `run_mode=paper`, every order to `paper-api.alpaca.markets`. No live order was possible.
- **Corporate actions completed clean** — 68 series, seed tickers skipped, no inconsistency.
- **Both end-of-day jobs ran on time**, at 20:00:00.001 and 20:30:00.061, for the first time in
  the paper week. Their contents need work (F4); their existence does not.
- **The audit of what is *not* measured is still the best thing in this codebase.** The daily
  report's `feed incidents NOT MEASURED — not recorded anywhere queryable` with the grep command
  attached is how every one of F4's wrong numbers should have been reported.

---

## 7. Day-3 scorecard

| Day-3 item | Status on day 4 |
|---|---|
| B1 — the host sleeps | **Fixed.** Zero dark minutes. ADR 0033 |
| B2 — rejection kills the worker, guard makes it permanent | **Fixed.** 107 refusals absorbed, 1 boot, no crash loop |
| B2 fix 1 — don't protect while the parent is working | **Not fixed.** 69 wash-trade rejections (F5) |
| B2 fix 2 — map rejections by code, never escape a fill consumer | **Fixed.** `classified=True` ×107, zero escapes |
| B2 fix 3 — a guard that cannot be satisfied must not restart-loop | **Fixed**, and untested — nothing tripped it |
| B2 fix 4 — `adopt_broker_state` entry point | **Fixed.** `scripts/adopt_broker_state.py`. Unused: recovery handled it |
| Pre-day-4 item 3 — deal with the open MSFT position | **Handled automatically.** Recovered, booked, and exited at 14:21 for +$17.18 |
| F1 — 129 identical pages to one human | **Over-corrected.** 70 CRITICALs, 2 alerts (F1) |
| F2 — trades on the slow average's first values | **Not fixed.** Six bars past computable (F6) |
| F3 — book right by coincidence, positions wrong | **Fixed.** 78 clean reconciles, 0 mismatches |
| F4 — operator cleared a halt 3 s into the outage | **Untested.** Zero halts, zero mutating API calls |
| F5 — BAC's adjusted history corrupt and traded | **Unreproduced.** BAC clean this session; no quarantine built |
| F6 — the resume button failed silently | **Untested.** Zero 401s, zero resume attempts |
| F7 — no session summary, because the host slept | **Fixed.** Both jobs ran on time |
| F8 — staleness blames the feed for a host event | **Untested.** Zero staleness detections |
| F9 — `restored_open_orders` re-adopts a filled order | **Fixed.** Adopted once, then recovered and booked correctly |
| F10 — unprotected alert cannot fire on the crash path | **Fixed.** `order.position_unprotected` now fires from the fill path, ×70. The alert behind it does not (F1) |
| F11 — F4 re-read mis-sized on the boot path | **Untested.** One boot, no divergence, no re-read |
| F12 — `fees.settled` fires for an already-settled amount | **Not fixed.** Twice for $0.01 (F13) |
| F12 — nginx duplicate `Date` header | **Not fixed.** 79 occurrences (F13) |
| F12 — `queue` container absent | **Resolved.** It logs, and it is idle (F13) |
| Day-2 F1 — report counts rejects as submissions | **Not fixed**, and now demonstrated: 209 vs 102 (F4) |
| Day-2 F2a — dashboard shows armed stops as working | **Untestable** from logs; `_mark_broker_protection` is wired |
| Day-2 F6 — unprotected alert has empty reason | **Not fixed.** `refusals=['']` ×69 (F2) |
| Day-2 F14 — report's rolling window crosses days | **Not fixed.** 9 halts on a day with none (F4) |
| Day-2 F15 — nothing scrapes the metrics | **Not fixed.** `metrics_serving port=9101`, no scraper |

Six day-3 items fixed and confirmed in production, including both blockers. That is the best
ratio of the week. The cost is that day 4 got far enough to find the next blocker, which had been
invisible behind the previous two: **days 1–3 never closed a position by signal, so nothing ever
tried to cancel a stop.**

---

## 8. Before day 5 runs

**The nine overnight positions make this urgent rather than theoretical.** They are protected —
the GTC stops are live — but every one of them has an exit the platform cannot execute. Day 5
opens holding $44,277 of inventory that can only leave at its stop.

1. **Fix the exit path (B1).** Cancel protection before submitting a close, on both
   `submit()`'s EXIT branch and `flatten`; re-arm over any remainder; treat `insufficient qty
   available` as a platform bug, loudly. Then make `tests/fakes.py` reserve inventory against
   working orders, so a unit test can fail on this.
2. **Decide what to do with the nine positions before the open.** They are the first real test of
   the fix. Either flatten them by hand with the stops cancelled first, or leave them and watch
   the first exit signal on each — but decide deliberately, because "the strategy will handle it"
   is the assumption day 4 disproved.
3. **Persist the book outside the evaluate loop (B2).** Snapshot on fill and from the reconcile
   job; snapshot on shutdown once F10's signal handler is proven; and put the snapshot's age in
   `restored_book`. Until this is done, every clean reconcile is worth less than it looks,
   because the book may have been reconstructed from the broker it is being checked against.
4. **Make the unprotected alert work in both directions (F1, F2).** Never suppress a symbol set
   containing a symbol not in the last announcement; emit a rollup while anything is unprotected;
   and carry the refusal text into `refusals=[...]` instead of `['']`.
5. **Gate evaluation on warmup (F6).** Three sessions, 540 honest warnings, zero symbols held
   back. A symbol below its strategy's lookback is not tradeable.
6. **Stop submitting protection against a working parent (F5).** Arm the level, place the stop
   when the entry is terminal. This removes 69 rejections, 69 unprotected windows and the noise
   that exhausts the alert budget in the first thirty seconds.
7. **Fix the daily report's four wrong numbers (F4)** — submitted vs accepted, refused vs
   refused-by-risk, the 24-hour window, and the missing P&L. It is the only end-of-day artifact,
   it finally runs, and right now a reader who trusts it learns four false things.
8. **Add RTH coverage to the report (F11)** — minutes with an evaluating runner against minutes
   of RTH. Day 4 is `355 / 390`; day 3 would have been `74 / 390`. It is the number the paper week
   is accumulating, and nothing currently computes it.
9. **Then tune the parameters (F7, F8)** — bar-arrival-driven evaluation, and an ATR multiplier
   and SMA periods backtested at the timeframe actually being traded. Do this *after* B1, not
   before: tuning a strategy that cannot exit measures nothing.

**The roadmap needs no tick, and one item needs a correction.** Phase 4's *Verifiable:* line is
*"a strategy trades the paper account for a week and reconciles clean"*. Day 4 reconciled clean
for one session — genuinely, 78 times, against a book of up to 14 positions — which is one fifth
of that line and the first fifth the platform has produced. Phase 4 stays **0 / 11** and Phase 5
stays **0 / 12**, correctly.

The correction, made in this diff, is to *Order and position persistence*, which was waiting on a
restart it has now had:

> nothing here has survived an actual restart of a running worker — which is the demonstration,
> and it is one the paper week produces for free the first time the process is bounced.

It was bounced, and it did not survive. The item now says so, and names the cause: the snapshot
is written only from inside the evaluate loop. "Not yet demonstrated" and "demonstrated and
failed" are different sentences, and a reader planning day 5 needs the second one.

**On the day count.** Day 3's review asked for the count to reset, on the grounds that calling a
session "day 4" implies three days of evidence that do not exist. That argument still holds, and
day 4 does not change it: a session whose exit rule was disconnected is not a day of the paper
week either. **What day 4 *is*, and what no previous session was, is a working platform.** The
week should start when the first session ends with a result that means something about the
strategy. That session has not happened yet, but for the first time it is the only thing missing.

**One pattern, stated plainly.** Day 2's lesson was guards that exist, are correct, and are never
called — *"What is missing is not judgement. It is the call site."* Day 3's was that every guard
was called, each did exactly what it was written to do, and they composed into a machine that
shut itself down permanently — *"correct local failure handling is not a recovery story."*

Day 4's is narrower and more specific, and it is visible in three separate places in this review.
**The protective stop is treated as a thing you add, and never as a thing you must remove.**
`submit_protective_orders` is 200 lines of careful reasoning about when and how to place a stop —
tick-rounded, side-aware, capped at held exposure, keyed per tranche, GTC so it survives the
close. `cancel_protection` is called from two places, and both of them are after the moment that
needed it: `flatten` cancels after its submit, and `_disarm_if_flat` cancels after the position
is already gone. The result is 42 cancels that cancelled nothing and 38 exits that could not
happen.

The same asymmetry produced B2 — a book written only where it is convenient to write it, never
where it is needed — and F9, where a stop is armed 1.2 seconds before the platform can price one
and never re-armed. **Day 4's lesson is that a lifecycle is not complete when the thing is
created.** Every guarantee this platform establishes needs a documented moment where it is
withdrawn, and the withdrawal needs the same care as the arming. Right now the arming has all of
it.
