# The stop fills that never reached the book

**Investigated:** 2026-09-23 · **Incident:** book frozen 2026-09-11 19:59Z; halted from 2026-09-14 through 2026-09-23
**Evidence:** the venue's own order history; `orders`, `fills`, `position_snapshots`,
`equity_snapshots`, `audit_log`; the halt record; `runner.quarantined` at every boot since

---

## Summary

Ten protective stops armed on Friday 2026-09-11 filled at the venue over the next six days — DIA
twenty-seven seconds after Friday's last book snapshot, four at Monday's open, the other five
between Monday midday and Thursday — and **not one of those fills ever reached the persisted
position book**. The book is frozen at that snapshot, 2026-09-11 19:59:05Z, still holding all ten.
Against the venue it carries $617.99 of realised loss it never booked; its last recorded equity,
100,138.58 at Friday's marks, is $902.39 above what the account is now worth.

Three things make it worth a page:

1. **Every fill reached `orders` and `fills`. None reached the book.** Five came in on the live
   trade-updates stream and five were rebuilt by the REST catch-up — two different routes into
   `on_fill_event`, one outcome. No position or equity snapshot of any kind has been written since
   Friday 19:59:05.
2. **Once that happens, nothing can repair it.** `on_fill_event` persists the order terminal and
   drops it from the working set *before* it writes the book, and the catch-up only asks the venue
   about orders that are not terminal. An order whose fill landed without its book write is out of
   reach of every later catch-up, for good.
3. **The divergence grew while the platform was halted.** When the reconciler first halted —
   Monday 09-14 14:37:10Z — the venue still held five of the ten. Their stops kept working through
   the halt, as protective stops are meant to, and each fill widened a gap the book could no longer
   absorb.

## 1. The state as found

`scripts/status.py`, 2026-09-23:

```
halts      HALTED (1 active)
           global  reconciliation_mismatch  by reconciler  since 2026-09-22T22:17:28Z
           ^ will NOT close: AMZN, CSCO, DIA, INTC, IWM, KO, MSFT, PEP, WMT, XOM
positions  0 at the venue
orders     0 working
           equity 99236.19   cash 99236.19
```

`execution.reconcile.mismatch`, at every boot (12:40:48Z and 12:47:13Z on 09-23):

```
unknown_position: AMZN, CSCO, DIA, INTC, IWM, KO, MSFT, PEP, WMT, XOM
cash: ours 50843.48 · theirs 99236.19 · differs by -48392.71, beyond the 1.00 tolerance
```

Ten `unknown_position` findings — "we hold a position the broker does not" — and a cash gap the
size of the book.

## 2. What the evidence rules out

Three queries, each returning the answer that removes a hypothesis.

**The order table is balanced.** Every symbol nets to zero across all paper orders:

```sql
select symbol, sum(case when side='buy' then filled_qty else -filled_qty end) as net_qty
  from orders where run_mode='paper' and filled_qty > 0
 group by symbol having sum(case when side='buy' then filled_qty else -filled_qty end) <> 0;
-- (0 rows)
```

So there are no phantom buys, and the order path did not invent anything.

**Every filled order has its fills.** No order is marked filled with nothing behind it:

```sql
select o.symbol, o.filled_qty, coalesce(sum(f.qty),0)
  from orders o left join fills f on f.order_id = o.id
 where o.run_mode='paper' and o.status in ('filled','partially_filled')
 group by o.id having coalesce(sum(f.qty),0) <> o.filled_qty;
-- (0 rows)
```

So fill persistence is not the gap either.

**The snapshot is internally consistent and correctly restored.** `PostgresPortfolioRepository
._positions_at` selects `PositionSnapshotRow.ts == ts` against the newest `equity_snapshots.ts`,
so a restore reads one coherent instant. There is no per-symbol "latest row" resurrection bug.

What is left is the position book.

## 3. The book froze holding each symbol's last buy

The newest snapshot is **2026-09-11 19:59:05Z**, fifty-five seconds before Friday's close, and
there has been no snapshot since:

```
              ts               | positions |       cost_basis
-------------------------------+-----------+------------------
 2026-09-11 19:59:05.49915+00  |        10 |        49010.70
 2026-09-11 19:56:04.050162+00 |        11 |        53963.21
```

That cost basis is not approximately anything. It is **the sum of each symbol's last buy of the
day, to the cent**:

| Symbol | Qty | Last buy | Cost |
|---|---:|---:|---:|
| KO | 56 | 88.03 | 4,929.68 |
| DIA | 9 | 526.00 | 4,734.00 |
| XOM | 30 | 165.30 | 4,959.00 |
| AMZN | 19 | 255.90 | 4,862.10 |
| INTC | 48 | 102.70375 | 4,929.78 |
| WMT | 46 | 106.60 | 4,903.60 |
| MSFT | 10 | 495.39 | 4,953.90 |
| PEP | 36 | 136.35 | 4,908.60 |
| CSCO | 44 | 111.7625 | 4,917.55 |
| IWM | 17 | 288.97 | 4,912.49 |
| | | | **49,010.70** |

Specifically the *last* buy, not the first — AMZN priced at its 15:15 fill instead of its 17:02
one gives 49,013.55, which does not fit.

And the venue's proceeds for those same ten exits reproduce the reconciler's cash gap
independently:

```
cost basis the book still carries : 49,010.70
proceeds the venue actually got   : 48,392.71   <- reported as the cash discrepancy
unbooked realised loss            :   -617.99

reconciler's "ours" cash          : 50,843.48
+ proceeds of the ten exits       : 48,392.71
                                  = 99,236.19   = reconciler's "theirs" cash
```

The 48,392.71 is computed from ten venue fill prices that appear in neither cash figure. It lands
on the discrepancy the reconciler derived from the account. The book is short exactly ten closing
fills.

## 4. Friday's book was correct — the stops were still resting

The stop for each position was **armed seconds after its entry filled** — `_protect` doing its
job, and the reason `orders` shows a `stop_loss` sell submitted one to six seconds after each buy.
That is not a round trip. When each stop then *filled* is a separate fact, and it is in the data:

```
sym   stop     avg fill      vs stop  venue fill (UTC)             booked via
DIA   525.71   525.66555556   -0.01%  Fri 19:59:32 -> 19:59:33     stream, 2 tranches
AMZN  255.57   250.94         -1.81%  Mon 13:30:54                 stream, 1
INTC  102.33   95.0925        -7.07%  Mon 13:30:54 -> 13:35:22     stream, 6 tranches
CSCO  111.59   109.9025       -1.51%  Mon 13:31:14 -> 13:34:35     stream, 4 tranches
IWM   288.85   287.91588235   -0.32%  Mon 13:32:24 -> 13:35:10     stream, 5 tranches
PEP   136.24   136.22         -0.01%  Mon 15:39:23                 rest_recovery
XOM   164.88   164.857        -0.01%  Mon 16:48:56                 rest_recovery
MSFT  495.15   492.719        -0.49%  Wed 13:31:54                 rest_recovery
KO    87.91    87.90          -0.01%  Wed 19:33:33                 rest_recovery
WMT   106.53   106.52         -0.01%  Thu 14:33:05                 rest_recovery
```

For the stream rows the time is each tranche's own execution timestamp. For the `rest_recovery`
rows it is the venue's `filled_at`: `recovery.missed_updates` stamps a reconstructed fill with
`theirs.filled_at or now`, and all five fall inside regular hours on weekdays, which a fallback to
boot time would not do.

**Friday's book was correct.** At the 19:59:05 snapshot all ten positions were open at the venue
with their stops resting, and the book said exactly that. DIA's stop filled twenty-seven seconds
later. Every other stop outlived the weekend.

Two shapes of fill follow. Four stops were taken out **through a gap at Monday's open**, in
tranches, well under their levels — INTC's first tranche 6.86% under. MSFT, two minutes after
Wednesday's open and 0.49% through, is a small fifth. The other five — DIA on Friday, then PEP,
XOM, KO and WMT between Monday midday and Thursday — filled **within a few cents of their stop**
in ordinary trading, which is a stop doing exactly what it says.

## 5. The mechanism

The fills reached `orders` and `fills`. None reached a persisted position book.

Every fill in this platform is booked through `on_fill_event`, which has exactly two callers: the
live trade-updates consumer (`apps/worker/src/atp_worker/trading.py`) and the REST catch-up
(`runner.py`, `catch_up_on_orders`). `fills.venue_fill_id` says which one booked each fill. The
stream carries the venue's own execution id; the catch-up mints
`rest_recovery:<broker_order_id>:<cumulative_qty>` in `recovery._reconstruct_fill`. Five of each
are present.

So both routes reached `apply_trade_update`, both ran `order.apply_fill`, and both got the order
persisted. And from neither route has a book write landed:

```
newest position_snapshots row : 2026-09-11 19:59:05
newest equity_snapshots row   : 2026-09-11 19:59:05
earliest of the ten fills     : 2026-09-11 19:59:32
```

The two writes sit in this order at the end of `on_fill_event`:

```python
if order.is_complete:
    # the order: persisted terminal, and removed from the working set
    await self.order_repo.save(order, run_mode=self.run_mode)
    self._open_orders.pop(order.client_order_id, None)
if booked:
    # the book: nothing has landed from here since 2026-09-11 19:59:05
    await self._checkpoint(portfolio, "fill")
```

And `_checkpoint` can fail without anyone finding out. It catches the exception, logs
`runner.book_unwritten` at ERROR, and returns, on the reasoning that *"the next write retries it:
the evaluate loop writes every pass."* Whether the book write here was attempted and failed every
time, or was never reached, the surviving evidence does not say (§10). Either way, one silent miss
at this point is enough — because of §6.

## 6. Why it never healed

The order write removes the handle the book write would have been retried by.

```python
# runner.py — the catch-up, run at warmup and on every trade-updates reconnect
if not self._open_orders:
    return 0
updates = await self.reconciler.missed_order_updates(list(self._open_orders.values()))
```

`_open_orders` is rebuilt on boot from `order_repo.open_orders(run_mode)` — *"every non-terminal
order for this run mode"*, `status.notin_(terminal)`. `recovery.read_missed_updates` states the
same scope: *"Ask the venue about every order we believe is working."*

So once an order has been written `filled`, no catch-up will ever ask about it again. If the book
write that should have followed it never landed, the position it was closing stays open in the
book indefinitely, and the one mechanism built to notice has no way to see it.

**What the data shows of this is the end state, not the steps.** The dates in §4 are when the venue
filled each order, not when this platform booked it: `Order.apply_fill` sets `filled_at` from the
fill's own timestamp, and neither `orders` nor `fills` records when a row was written. So there is
no evidence here of *when* each order went terminal — only that all ten did, that all ten
positions are still in the book, and that the code gives a terminal order no road back.

**And the halt did not freeze the divergence.** At 09-14 14:37:10, when the reconciler first
halted, PEP, XOM, MSFT, KO and WMT were still open at the venue. Their stops were resting orders at
Alpaca, outside anything the kill switch governs, and they kept working — which is correct:
a venue-side stop is meant to outlive the platform. But each one filled into a book that could not
take it. At most five names had diverged when the halt was engaged — its own findings went with the
record cleared on 09-22 — and all ten had by 09-17.

## 7. What it cost

**$617.99** of unbooked realised loss, and eight trading days halted (09-14 through 09-23).

Of that, **INTC alone is $365.34.** The stop was armed Friday 09-11 17:10 at 102.33, against an
entry of 102.70375. It did not fill that day. Monday's open took it out in six tranches between
13:30:54 and 13:35:22:

```
95.31   95.18   95.11   94.62   94.55   94.79      avg 95.0925
```

The first tranche is **6.86% under the stop level**. That is a weekend gap, and filling well through
the level is what a stop does in one — not slippage, not a bad quote, and not something better data
would have prevented. A stop cannot defend a position while the market is shut.

AMZN (1.81% under its stop), CSCO (1.51%) and MSFT (0.49%) are the same shape, smaller. DIA, PEP,
XOM, KO and WMT cost almost nothing beyond the stop distance itself.

**A correction this page has to carry**, because the first version of it got this wrong: the
`Quote` validation gap in §8a is real, and it did **not** cause this loss.

## 8. The churn underneath is F8, and it is already fixed

Separately from the divergence, `sma_crossover` at `timeframe=1m` was stopping itself out all day.
`f8-timeframe-and-stop-sizing.md` measured `atr x2 period=14` at `1m` as a median **0.121%** of
price and recorded day 4 closing *"41 round trips, all 41 at their stop, none profitable"*.

Friday 09-11 is that again. The stop levels in §4 sit 0.04%–0.36% from their entries, and the
same-day round trips closed at their stops within minutes: AMZN entered 16:15:30 and stopped out
16:26:50, CSCO entered 16:29:35 and stopped out 16:30:03, IWM entered 16:41:40 and stopped out
17:04:39.

The `worker_config` change of 2026-09-22 21:56:29Z (revision 9, `1m` -> `1d`) is the fix; at `1d`
the same stop is 4.08% wide.

F8 explains why there were ten small positions with tight stops going into a weekend. It does not
explain the divergence, and a wider stop would not have prevented it.

**A second correction.** An earlier version of this page read `orders.submitted_at` as a fill time
and reported nineteen round trips "buy and sell seconds apart" totalling -$552.20. That pairing was
wrong: the sell submitted seconds after each buy is the protective stop being *armed*, not an exit.
Holding periods were minutes for the same-day round trips and days for the ten that stuck.

## 8a. And the `Quote` gap, which is real and unrelated

`ALPACA_DATA_FEED` is `iex` — roughly 2-3% of consolidated volume — and `Quote.__post_init__`
(`libs/core/src/atp_core/domain/market.py`) validates only that the timestamp is UTC. It does not
require `ask > 0`, `bid > 0`, or `bid <= ask`. `Bar` rejects `low > high`; `Quote` rejects nothing.
At the time of writing, six watchlist symbols are quoting `ask 0`, and `runner.py` marks the book
with `quote.mid`, which for a one-sided quote is half price.

That is a live defect worth fixing on its own terms — a halved mark feeds equity, the daily-loss
anchor and every percentage limit. It is recorded here because it was found during this
investigation, not because it caused any part of this incident.

## 9. The halt was cleared over a live divergence

From `audit_log`:

```
2026-09-22 22:11:36  operator  halt_cleared
  {"scope":"global","via":"scripts/halt.py","was_halted":true,
   "original_reason":"reconciliation_mismatch","originally_engaged_by":"reconciler",
   "originally_engaged_at":"2026-09-14T14:37:10.611014+00:00"}
2026-09-22 22:17:28  reconciler re-halts
```

Eight days, seven hours and thirty-four minutes of halt, cleared without the book being resynced
— there is no `adopt_broker_state` row — and re-engaged five minutes and fifty-two seconds later.

`docs/RUNBOOK.md` says *"Do not resume until it is understood."* The asymmetry that allowed this
is worth naming: **`adopt_broker_state.py` refuses to run unless trading is halted, but nothing
refuses to clear a `reconciliation_mismatch` halt while the mismatch still stands.** The interlock
exists in one direction only. The operator's only feedback was a re-halt six minutes later.

This cost confusion, not money — the book was already frozen and the venue already flat.

## 10. What is not established

**Why no book write has landed since 2026-09-11 19:59:05.** Ten fills were booked into `orders`
by two different routes across six days, and no position or equity snapshot followed any of them.
Once the order write has landed, only two things stand between it and the book write: the
`pop` and the `if booked:` test. So either the book write was attempted and failed every time —
`_save_book` raised and `_checkpoint` swallowed it as `runner.book_unwritten` — or `_checkpoint`
returned before trying, which it does silently whenever `_book_bound` is false. (`_protect` is not
a candidate here: it returns early for a fill that reduces a position, and every one of these
did.) Nothing surviving distinguishes the two.

**When each fill was booked.** `orders.filled_at` and `fills.ts` are the venue's times (§6). No
table stores when a row was written. Postgres does record the writing transaction, which gives a
relative order against rows whose times *are* known:

```sql
select 'fill' as kind, o.symbol as what, f.ts, f.xmin::text::bigint as txid
  from fills f join orders o on o.id = f.order_id
 where o.run_mode = 'paper' and o.side = 'sell' and f.ts >= '2026-09-11 19:59'
union all
select 'audit', action, ts, xmin::text::bigint from audit_log where ts >= '2026-09-11'
union all
select 'book', 'equity_snapshot', ts, xmin::text::bigint
  from equity_snapshots where ts >= '2026-09-11 19:50'
order by txid;
```

Treat it as ordering, not timing, and note that an upsert rewrites `xmin` — if `order_repo.save`
re-upserts fill rows, their `xmin` is the last save, not the first.

**The worker log cannot answer either question.** The container was created 2026-09-23T12:40:40Z
with `RestartCount=0` and holds 1011 lines, all from today. A grep over it for any of the booking
or refusal lines returns nothing, and that result carries no information — it is a grep over a log
that does not reach the incident. For next time: `runner.book_unwritten` is the line that would have
answered the first question on the day it happened.

**Falsified along the way, recorded so nobody re-runs them:**

- *The fill was dropped by `on_fill_event`'s unknown-order branch.* The first version of this page
  put the mechanism there. `fills.venue_fill_id` is populated on every one of the ten, so
  `apply_trade_update` ran and that branch was not taken.
- *The catch-up booked these on 09-14, 09-16 and 09-17.* An intermediate version read
  `orders.filled_at` as a booking date. It is the venue's fill time (§6).
- *The five `rest_recovery` timestamps are when the catch-up ran.* Same version, same error:
  `missed_updates` stamps them with the venue's `filled_at`.

## 11. What to fix

In priority order. None of these are done; this page is the diagnosis, not the repair. None of them
depend on which of §10's two explanations is the true one.

1. **Keep the write order, and make the stale book repairable.** `_persist` explains why the
   order is written before the book: if the process dies between them, a restart reads a stale
   book beside a current order set, *"which reconciliation notices and halts on"* — whereas the
   reverse would restore a book claiming fills whose orders were never recorded, which nothing
   could detect. That reasoning holds, and reconciliation did notice. What B2 did not provide is a
   way back: once halted, the only repair is a human running `adopt_broker_state.py`. But the fills
   the stale book is missing are already in `fills`. On restore, any fill on an order for a
   symbol the book holds, dated after the snapshot's instant, can be replayed from our own tables
   — no venue call — before the first reconcile.
2. **A catch-up must be able to see what it left behind.** `missed_order_updates` should also reach
   orders that are terminal in our book while the position they were closing is still open — the
   exact set §6 shows is unreachable. This is what turns the failure from permanent into
   self-healing.
3. **A book write that fails must not be silent.** `_checkpoint` swallows the failure on the
   grounds that the evaluate loop retries it. A book that has not been written for twelve days
   says the retry is not a safety net. At minimum it is a metric and an alert; on the `warmup` path,
   where there is no next write, it should refuse to continue. Its early return on an unbound book
   logs nothing at all, and should say so too.
4. **`Quote` must reject one-sided and crossed quotes at the domain boundary** (§8a). Unrelated to
   this incident, live today.
5. **A resume interlock.** Clearing a `reconciliation_mismatch` halt should re-run the comparison
   and refuse, or demand explicit confirmation, while the divergence still stands (§9).

## 12. Recovery from this incident

The last of the ten closed at the venue on Thursday 2026-09-17 (WMT), and the book's numbers for
them are stale by exactly -617.99. `scripts/adopt_broker_state.py` discards nothing real.

```bash
# 1. preserve the evidence first — adopt overwrites the only record of what we believed we held
docker compose exec -T db pg_dump -U atp -d atp \
  -t orders -t fills -t position_snapshots -t equity_snapshots -t audit_log -t broker_fees \
  > atp-incident-2026-09-11.sql

# 2. see the difference, change nothing
uv run python scripts/adopt_broker_state.py --by <you> --dry-run

# 3. adopt, then resume as a separate decision
uv run python scripts/adopt_broker_state.py --by <you>
uv run python scripts/halt.py clear --by <you>
```

Every adopted position is unprotected until something re-arms it. Here the venue is flat, so
there is nothing to leave unprotected — the one piece of luck in this incident.
