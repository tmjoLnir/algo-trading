# The stop fills that never reached the book

**Investigated:** 2026-09-23 · **Incident:** 2026-09-11 19:59Z → 2026-09-23, twelve days halted
**Evidence:** the venue's own order history; `orders`, `fills`, `position_snapshots`,
`equity_snapshots`, `audit_log`; the halt record; `runner.quarantined` at every boot since

---

## Summary

Ten protective stops armed on Friday 2026-09-11 filled at the venue — one that evening, the rest
into Monday's open and after — and **not one of those fills was ever persisted to the runner's
position book**. The book is frozen at Friday's close, holding ten positions the venue has since
closed, overstating the account by $902 and carrying $617.99 of realised loss it never booked.

The reconciler caught it on the next session — Monday 2026-09-14 14:37:10Z, an hour after the
open — and halted. It was right, and it has been right on every boot since.

Two things make this worth a page rather than a line:

1. **The repair ran, and then threw itself away.** Every boot from 09-14 onward re-read the venue,
   booked some of the missing fills into `orders` and `fills` — and never wrote the position book
   that reflects them. The booked order then goes terminal and leaves the working set, so the next
   boot's catch-up cannot see it any more.
2. **So the quarantines were not identical.** Each one silently converted orders from recoverable
   into unrecoverable while persisting none of the repair. The system spent twelve days consuming
   its own recovery path.

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

`execution.reconcile.mismatch`, every five minutes and at every boot:

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

What is left is the live `Portfolio` itself.

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

`sma_crossover` emits `ENTER_LONG` only when the position is flat. So the first exit a symbol lost
was also its last trade: the book believed it still held the position, and never touched that name
again.

The stop for each was **armed seconds after its entry filled** — that is `_protect` doing its job,
and it is why `orders` shows a `stop_loss` sell submitted one to six seconds after each buy. It is
not a round trip. What each stop then did is a separate question with a separate timestamp.

```
symbol   entry order       stop level   venue execution times we hold
KO       09-11 15:35:14    87.91        none — booked 09-16 by rest_recovery
DIA      09-11 15:38:15    525.71       09-11 19:59:32 -> 19:59:33
XOM      09-11 15:38:15    164.88       none — booked 09-14 by rest_recovery
AMZN     09-11 17:02:47    255.57       09-14 13:30:54
INTC     09-11 17:10:52    102.33       09-14 13:30:54 -> 13:35:22  (6 tranches)
WMT      09-11 17:44:05    106.53       none — booked 09-17 by rest_recovery
MSFT     09-11 18:01:12    495.15       none — booked 09-16 by rest_recovery
PEP      09-11 19:11:42    136.24       none — booked 09-14 by rest_recovery
CSCO     09-11 19:49:59    111.59       09-14 13:31:14 -> 13:34:35  (4 tranches)
IWM      09-11 19:51:00    288.85       09-14 13:32:24 -> 13:35:10  (5 tranches)
```

**Friday's book was correct.** Ten positions open, ten stops resting at the venue, and a snapshot
at 19:59:05 that said exactly that. Nothing was wrong at the close except DIA, whose stop filled at
19:59:32 — twenty-seven seconds after the last snapshot this platform has ever written.

The rest went over the weekend and were taken out at **Monday's open**, 09-14 13:30:00Z, in
tranches: six for INTC across four and a half minutes, five for IWM, four for CSCO. A stop held
through a weekend gap fills in pieces, well below its level, and that is what these are.

The five marked `rest_recovery` carry no venue execution time of their own — that id is minted by
`execution.recovery._reconstruct_fill`, whose `ts` is when the catch-up ran, not when the venue
filled. For those five the venue's own fill time is not in our data; only the date we finally
booked it is.
## 5. The mechanism

The fills reached `orders` and `fills`. They never reached a persisted position book.

`fills.venue_fill_id` is the evidence, because only two code paths mint one:

- a **real UUID** comes from the venue's own execution id, applied through
  `trade_updates._apply_fill`
- **`rest_recovery:<broker_order_id>:<cumulative_qty>`** is minted by
  `execution.recovery._reconstruct_fill` on a catch-up

Both are present across the ten. So `on_fill_event` was reached, `apply_trade_update` returned
True, `order.apply_fill` ran, and `order_repo.save` persisted the result. Every one of those steps
worked.

What did not happen is the write at the end of the same method:

```python
if order.is_complete:
    # phase 1 — lands, and takes the recovery key with it
    await self.order_repo.save(order, run_mode=self.run_mode)
    self._open_orders.pop(order.client_order_id, None)
if booked:
    # phase 2 — has not landed since 2026-09-11 19:59:05
    await self._checkpoint(portfolio, "fill")
```

`position_snapshots` has no row after **2026-09-11 19:59:05**. `orders.filled_at` for these ten
runs **09-14, 09-16 and 09-17**. Phase one landed on three separate days; phase two never landed at
all.

`_checkpoint` cannot report that. It swallows a failed write and logs at ERROR, on the reasoning
that *"the next write retries it: the evaluate loop writes every pass."* On these boots there is no
next write — `warmup` runs the catch-up, then reconciles, then quarantines, and the evaluate loop
is never reached. The retry the comment depends on is downstream of the thing that stops.
## 6. Why it never healed

Phase one destroys the key that phase two would have been retried by.

```python
# runner.py:2392 — the warmup catch-up
if not self._open_orders:
    return 0
updates = await self.reconciler.missed_order_updates(list(self._open_orders.values()))
```

`_open_orders` is rebuilt on boot from `order_repo.open_orders(run_mode)` — *"every non-terminal
order for this run mode"*, `status.notin_(terminal)`. `recovery.read_missed_updates` says the same:
*"Ask the venue about every order we believe is working."*

So once phase one has written an order `filled`, that order is invisible to every future catch-up.
The position it was closing stays open in the book forever, and the fill that would close it can
never be asked for again.

**This is a ratchet, and the dates prove it ran.** Each boot took whichever stops were still
non-terminal, booked them, and quarantined before persisting the book:

```
booked into orders/fills on   symbols            (* = rest_recovery id)
2026-09-11                    DIA
2026-09-14                    AMZN, INTC, CSCO, IWM, PEP*, XOM*
2026-09-16                    MSFT*, KO*
2026-09-17                    WMT*
```

Three separate days of the platform correctly reading the venue, correctly booking what it found,
and then throwing the result away — each time leaving one fewer order that a later boot could ask
about. By 09-17 there was nothing left to ask about and the divergence was permanent.

The twelve quarantines were not twelve identical readings of one frozen state. They were the
mechanism by which the state became unrecoverable.
## 7. What it cost

**$617.99** of unbooked realised loss, and twelve trading days halted.

Of that, **INTC alone is $365.34** — and it is not what an earlier draft of this page claimed. The
stop was armed Friday 09-11 17:10 at 102.33, against an entry of 102.70375. It did not fill that
day. Monday's open took it out in six tranches between 13:30:54 and 13:35:22:

```
95.31   95.18   95.11   94.62   94.55   94.79      avg 95.0925
```

The first tranche is **6.86% below the stop level**. That is a weekend gap, and filling well
through the level is what a stop does in one — not slippage, not a bad quote, and not something
better data would have prevented. A stop cannot defend a position while the market is shut.

AMZN (-1.94%) and CSCO (-1.66%) are the same shape, smaller.

**A correction this page has to carry**, because the first version of it got this wrong: the
`Quote` validation gap below is real, and it did **not** cause this loss.
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

**Which step between phase one and phase two failed.** §5 shows the order was persisted and the
book was not, on three separate days. It does not show whether `_checkpoint` was reached and its
write failed (swallowed, ERROR-logged), or whether something between `_apply_to_portfolio` and
`_checkpoint` raised and took the runner into `WarmupBlockedError` first. `runner.py`'s own
catch-up comment names `_protect` as the usual suspect for that second case.

The worker log cannot answer it. The container was created 2026-09-23T12:40:40Z with
`RestartCount=0` and holds 1011 lines, all from today; nothing from 09-11 through 09-17 survives.
A grep for `runner.fill_for_unknown_order` over it returns nothing, and that result carries no
information either way — it is a grep over a log that does not reach the incident.

**What is established** is the shape, from data that does survive: phase one landed on 09-14, 09-16
and 09-17; phase two has not landed since 09-11 19:59:05; and each landing of phase one removed an
order from the set any later catch-up can see. Both fixes in §11 follow from that and do not depend
on which step failed.

**A falsified hypothesis, recorded so nobody re-runs it.** An earlier version of this page put the
mechanism at `on_fill_event`'s unknown-order branch — a fill arriving for an order no longer in
`_open_orders`, discarded with a warning. `fills.venue_fill_id` disproves it: the ids are there, so
`apply_trade_update` ran, so that branch was not taken.
## 11. What to fix

In priority order. None of these are done; this page is the diagnosis, not the repair.

1. **Persist the book and the order atomically, or persist the book first.** Today phase one writes
   the order terminal and drops it from `_open_orders` before phase two writes the position book,
   and phase two's failure is swallowed. That ordering means any failure between them is both
   silent and permanent. The comment on it cites `_persist`'s ordering (B2) as the reason; whatever
   B2 needed, it cannot be worth making the repair path unreachable.
2. **A catch-up must not be able to leave the book behind.** `missed_order_updates` should reach
   orders that are terminal in our book but whose position is still open — the exact set that
   §6 shows is unreachable today. This is what turns the failure from permanent into self-healing.
3. **`_checkpoint` must not swallow a write failure it cannot retry.** Its own justification is
   "the next write retries it"; on the `warmup` path there is no next write. Either it retries, or
   it refuses to be silent there.
4. **`Quote` must reject one-sided and crossed quotes at the domain boundary** (§8a). Unrelated to
   this incident, live today.
5. **A resume interlock.** Clearing a `reconciliation_mismatch` halt should re-run the comparison
   and refuse, or demand explicit confirmation, while the divergence still stands (§9).
## 12. Recovery from this incident

The ten positions have not existed at the venue since Friday 2026-09-11, and the book's numbers
for them are stale by exactly -617.99. `scripts/adopt_broker_state.py` discards nothing real.

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
