# Parking lot

Known defects in things that are **already built**, deliberately deferred, with
enough context to pick each one up cold.

## How this file is maintained

This is not a third roadmap. `docs/ROADMAP.md` records what has and has not been
built; its *Later* section lists features nobody has started, and *Explicitly out
of scope* lists features this platform will not have. Neither has a place for
"this shipped, it is wrong in a known way, and we chose not to fix it yet" —
which is what lands here.

An entry earns its place by being **discovered and diagnosed**, not merely
suspected. If nobody has established that the defect is real, it is not parked;
it is a hunch, and it belongs in an issue. Each entry states what is wrong, how
it was found, what it costs while it stays, and what fixing it would take —
because the whole value of parking something is that the next person does not
have to rediscover it.

Conventions:

- **An entry leaves when the work lands**, deleted in the same diff as the fix,
  the way a roadmap box is ticked in the same diff as the work (`CLAUDE.md` §6).
  An entry describing a defect that no longer exists is worse than no entry.
- **An entry that turns out not to be a defect leaves too**, with the correction
  written into the commit message rather than left as a tombstone here.
- **Parking is a decision, not a backlog.** Anything here was deferred for a
  stated reason. If the reason has expired, say so in the entry rather than
  leaving the original justification standing.

---

## A backtest's money never reaches the dashboard

**Status:** open · found 2026-08-26 · `apps/api`, `libs/core/persistence`,
`libs/core/backtest`

### What is wrong

`BacktestResult.to_report()` produces nine fields that describe the money a run
made and the orders it took:

`ending_equity` · `total_return` (as a decimal string) · `realized_pnl` ·
`unrealized_pnl` · `open_positions` · `orders` · `filled_orders` · `signals` ·
`fees`

**None of them is stored, and none reaches the dashboard.**
`runner.result_to_storage` returns metrics, the equity curve, the trades and the
warnings; `persistence.models.BacktestRunRow` has no column for any money field;
and `BacktestOut` exposes `metrics: dict[str, float | None]` and nothing else
numeric. The CLI's `--out` JSON has all nine. A queued run has none of them, at
any layer.

The design tension underneath is worth stating, because both halves of it are
correct:

- `metrics` is a bag of **floats** by contract, and it has to be — these are
  statistics over a return series, not balances. Money must not be float
  (`CLAUDE.md` §1.1), so `to_report()` deliberately keeps the money fields *out*
  of `metrics` and reports them as decimal strings beside it.
- The queued path stores **only** `metrics`, the curve and the trades.

Each decision is right on its own. Together they drop every money figure a run
produced.

### How it was found

A `buy_and_hold` run over twenty symbols, exported from the Backtests tab and
compared field-by-field against the same run's CLI report. The metric set and all
1,525 equity points were byte-identical — `build_engine` holding both call sites
to the same numbers, exactly as ADR 0006 intends. The nine fields above were
simply absent from the export.

### What it costs while it stays

That run reported a **total return of 202.8%**, of which **none was realised**:
`realized_pnl` was zero, all 202.8% was unrealised mark-to-market, and twenty
positions were still open at the end. On the dashboard it reads as "202.8%
return" with nothing to say otherwise. `to_report()`'s own docstring names the
hazard precisely:

> `realized_pnl` and `unrealized_pnl` are reported separately because
> `ending_equity` alone is readable two ways and only one of them is a track
> record.

The persisted path keeps *neither* reading. The nearest available hint is
`num_trades: 0` in the metric set, which says something different — that the
trade statistics rest on no closed round trips — and is easy to read as "this
strategy does not trade much" rather than "you are still holding all of it".

This bites hardest on exactly the strategies a reader is most likely to
misjudge: anything that ends the window still holding, where the difference
between a banked return and an open position is the difference between a track
record and a bet that has not been settled.

### What fixing it would take

Bigger than it looks, and the reason it is parked rather than done:

1. **A schema decision first.** Money crosses this API as decimal strings today —
   `starting_cash`, every equity-curve point, every price and fee on a trade —
   and the new fields must do the same rather than joining the float bag. Whether
   that is a set of `MONEY` columns or one JSON blob of decimal strings is the
   open question; the equity curve is the precedent for the latter, and
   `positions`/`orders` are the precedent for the former.
2. **A migration**, additive and nullable, with no backfill available: runs
   already on record computed these figures and discarded them, so old rows
   would carry `null` and mean it. Same shape as `a9f37c14e6b2`, which parked
   the equivalent problem for `warnings`.
3. **`StoredBacktestRun`, `BacktestOut` and the OpenAPI schema**, which means
   `make gen-types` and a change to `apps/web/src/api/types.ts` — the first
   change in this area that actually moves the TypeScript surface.
4. **The run panel**, which is where the "still holding N positions carrying X
   unrealised" sentence has to appear for any of it to matter. The CLI already
   prints that sentence; nothing on screen does.

### Why it was deferred

Split off from the `warnings` column (#104) deliberately. That fix was contained
— one JSON column, one merge function, no API schema change, no TypeScript — and
bundling a money schema change into it would have turned a reviewable diff into
a speculative one. The two gaps have the same root (the queued path stores less
than the run produced) and very different sizes.

The warnings fix also removes the sharper edge: a run whose orders were all
refused is no longer silent. This one remains, and it is a misreading rather
than a blind spot.
