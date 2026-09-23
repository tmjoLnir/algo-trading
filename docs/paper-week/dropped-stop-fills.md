# The stop fills that never reached the book

**Investigated:** 2026-09-23 · **Incident:** 2026-09-11 19:59Z → 2026-09-23, twelve days halted
**Evidence:** the venue's own order history; `orders`, `fills`, `position_snapshots`,
`equity_snapshots`, `audit_log`; the halt record; `runner.quarantined` at every boot since

---

## Summary

On Friday 2026-09-11, ten protective stop orders filled at the venue and **none of those ten
fills reached the runner's in-memory `Portfolio`**. The session ended with a book holding ten
positions the venue had already closed, overstating the account by $902 and carrying $617.99 of
realised loss it had never booked.

The reconciler caught it on the next session — Monday 2026-09-14 14:37:10Z, an hour after the
open — and halted. It was right, and it has been right on every boot since.

Two things make this worth a page rather than a line:

1. **Nothing was lost.** `orders` and `fills` are complete and agree with the venue to the cent.
   The order path, the fill persistence and the snapshot restore are all correct. Only the live
   position book diverged.
2. **The divergence cannot heal itself.** `Reconciler.missed_order_updates` exists to repair
   exactly this, and it is structurally unable to see the orders that caused it. That is why
   twelve days and a dozen restarts changed nothing.

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

## 4. The watchlist bled out one name at a time

`sma_crossover` emits `ENTER_LONG` only when the position is flat. So the first exit a symbol
lost was also its last trade: the book believed it still held the position, and never touched
that name again.

```
KO    15:35:14   <- stuck from here
DIA   15:38:15
XOM   15:38:15
AMZN  17:02:47
INTC  17:10:52
WMT   17:44:05
MSFT  18:01:12
PEP   19:11:42
CSCO  19:49:59
IWM   19:51:00   <- the last order the venue ever saw
```

Four hours and sixteen minutes, ten names, one at a time. By 19:59:05 the whole watchlist was
frozen, gross exposure read 49,290.64 against a venue that was flat, and equity read 100,138.58
on an account worth 99,236.19.

The snapshot holding **eleven** positions at 19:52–19:56 is an eleventh name closing correctly at
about 19:56:30. **The failure is intermittent, not total** — which matters for reproducing it.

## 5. The mechanism

`apps/worker/src/atp_worker/runner.py:2491`, the first thing `on_fill_event` does:

```python
order = self._open_orders.get(update.client_order_id)
if order is None:
    log.warning("runner.fill_for_unknown_order", client_order_id=..., symbol=...)
    return          # the position is never touched
```

A fill for an order that is not in `_open_orders` is logged at **warning** and discarded. The
`return` fires before anything else in the method: `_apply_to_portfolio`, `_disarm_if_flat`,
`_protect`, `_announce` and `_checkpoint` all sit below it and none of them run.

That one `return` produces every field of the frozen snapshot simultaneously:

| Observed in `position_snapshots` | Because |
|---|---|
| position still open at full qty | `_apply_to_portfolio` never called |
| `broker_protected_qty` still 56.00 / 9.00 / 30.00 … | `_disarm_if_flat` never called |
| `stop_loss_price` still armed | same |
| cash never credited | `portfolio.cash -=` never ran |

All ten rows claim a full-size protective stop resting at a venue that reports **0 working
orders**. The stops did not vanish; they filled, and the book was never told.

## 6. Why it never healed

```python
# runner.py:2392 — the warmup catch-up
if not self._open_orders:
    return 0
updates = await self.reconciler.missed_order_updates(list(self._open_orders.values()))
```

`_open_orders` is rebuilt on boot from `order_repo.open_orders(run_mode)`, whose docstring reads
*"Every non-terminal order for this run mode"* and whose query is
`OrderRow.status.notin_(terminal)`. `recovery.read_missed_updates` states the same scope:
*"Ask the venue about every order we believe is working."*

Those ten stop orders are `filled` in `orders` — terminal. So:

- they are not restored into `_open_orders`,
- so they are not in the working set,
- so the venue is never asked about them,
- so the fills are never recovered,
- on every boot, forever.

The repair mechanism's candidate set is defined by order status; the damage is defined by a
position that is open while its closing order is terminal. Those two sets do not intersect. That
is the whole reason twelve days of restarts produced twelve identical quarantines.

`docs/ROADMAP.md`'s Reconciliation item says `missed_order_updates` "closes" the day-3 gap. It
closes it for an order still working in our book. It does not close it for an order our book has
already retired while still holding the position that order was closing.

## 7. What it cost

**$617.99** of unbooked realised loss, and twelve trading days halted.

Of that loss, **INTC alone is $365.34** — a stop armed at 102.33 that filled at 95.092499, 7.1%
through its own level, in a 3.6-second window, on Intel:

```
INTC buy  17:10:53  @ 102.70375
INTC sell 17:10:56  @  95.092499     -7.41%,  -365.34
```

That is not a market move. It is a market-on-stop order filling against a one-sided book.
`ALPACA_DATA_FEED` is `iex` — roughly 2–3% of consolidated volume — and `Quote.__post_init__`
(`libs/core/src/atp_core/domain/market.py`) validates only that the timestamp is UTC. It does not
require `ask > 0`, `bid > 0`, or `bid <= ask`. `Bar` rejects `low > high`; `Quote` rejects
nothing. At the time of writing, six watchlist symbols are quoting `ask 0`, and
`runner.py` marks the book with `quote.mid`, which for a one-sided quote is half price.

AMZN (-1.94%) and CSCO (-1.66%) are smaller instances of the same thing. In paper this is an
accounting annoyance. In live it is the most expensive line on this page.

## 8. The churn underneath is F8, and it is already fixed

Every venue order that day is a buy followed by a sell seconds later, and all nineteen visible
round trips lose money, median -0.10%:

```
19 round trips, ALL losing. total -552.20
```

`f8-timeframe-and-stop-sizing.md` measured `atr x2 period=14` at `1m` as a median **0.121%** of
price and recorded day 4 closing "41 round trips, all 41 at their stop, none profitable". This is
the same machine still running. The `worker_config` change of 2026-09-22 21:56:29Z
(revision 9, `1m` → `1d`) is the fix; at `1d` the same stop is 4.08% wide.

F8 is the reason the losses were small. It is not the reason the book diverged — a wider stop
would have produced fewer dropped fills, not zero.

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

**Why each stop order left `_open_orders` before its own fill arrived.** `runner.py:2536` pops an
order when `order.is_complete`; something marked these complete first. Commit `1038f12`
("retry a refused stop, confirm a cancel before closing, watch a stop lost to a refused close")
is working the same area, which suggests a known and not-fully-closed race.

That is the *trigger*. Sections 5 and 6 — why the loss was silent and why it was permanent — hold
regardless of what the trigger turns out to be.

**The falsifiable check, not yet run.** If §5 is right, Friday's worker log contains exactly ten
`runner.fill_for_unknown_order` warnings, at the symbols and times in §4:

```bash
docker compose logs worker | grep -E "fill_for_unknown_order" | head -20
```

Ten hits at those timestamps closes the chain end to end. Anything else means §5 is wrong and the
fill was lost somewhere earlier.

## 11. What to fix

In priority order. None of these are done; this page is the diagnosis, not the repair.

1. **`on_fill_event` must not silently drop a fill.** A fill on an order we have forgotten, for a
   position we still hold, is a divergence — it belongs in the halt path or a repair path, not in
   a `log.warning` followed by `return`. This is the bug.
2. **Widen the catch-up set.** `missed_order_updates` should reach orders that are terminal in our
   book but whose position is still open. This is what turns the failure from permanent into
   self-healing, and it is the difference between one bad afternoon and twelve days.
3. **`Quote` must reject one-sided and crossed quotes at the domain boundary.** `Bar` already
   rejects `low > high`. A quote with `ask == 0` currently marks the book at half price and prices
   orders against nothing. This is the $365 of §7 and the only item here that is catastrophic
   rather than merely wrong in live.
4. **A resume interlock.** Clearing a `reconciliation_mismatch` halt should re-run the comparison
   and refuse, or demand explicit confirmation, while the divergence still stands.

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
