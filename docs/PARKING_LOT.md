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

---

## The backtest equity curve is stamped a day late

**Status:** open · found 2026-08-26 · `libs/core/backtest`

### What is wrong

`BacktestEngine.run` marks equity with `clock.now()`
(`engine.py:640-641`), and the clock is set to `ts + step` at the top of each
iteration (`engine.py:591`), where `step` is the timeframe's wall-clock duration
(`engine.py:546`). The comment there is right about its own intent:

> The clock stands at the bar's CLOSE: `Bar.ts` is its open, and a decision
> taken on a completed bar is taken once it has ended.

For an intraday bar that is exact. For a **daily** bar it is not: a daily bar is
stamped at exchange-local midnight, so `ts + 24h` is the *next* midnight rather
than the session close at 21:00 UTC — a different calendar day. Friday's session
is therefore recorded at Saturday's date, and Monday never appears at all.

The engine holds both conventions at once. Thirteen lines below the clock, the
session anchor keys on `ts.date()` — the true session date (`engine.py:604`) —
and its comment justifies that choice by noting a daily bar "is stamped at
exchange-local midnight, so neither straddles a UTC day boundary". The equity
curve's own timestamps are the thing that does straddle it.

`Bar.close_ts` (`market.py:79`) computes the identical `ts + timeframe.seconds`,
so the convention exists in two places and would have to move in both.

### How it was found

A weekday histogram of a real 1,525-point curve over 2020-07-27 → 2026-08-21:
**304 Saturdays and zero Mondays.** Shift every point back one calendar day and
the series becomes a clean Mon–Fri NYSE calendar — no weekends, holidays
correctly absent, Mondays the sparsest weekday exactly as US market holidays
predict. Corporate-action dates in that run also lined up one day late, which is
how the offset first drew attention.

### What it costs while it stays

**No metric is wrong.** Each session still maps to a distinct date, so
`daily_returns` (`performance.py:467`) yields the right *set* of returns and
Sharpe, volatility, drawdown and CAGR are unaffected. The defect is entirely in
the labels, and it is pervasive:

- Every exported curve carries dates the market was not open on. A chart drawn
  from one has a Saturday on its axis and no Mondays.
- Joining a backtest curve against a live equity series, or against a benchmark,
  **by date** is silently off by one for every point.
- Reconciling a specific day's move against the market fails: the day named is
  not the day that moved.

It is the kind of defect that reads as a rendering quirk until somebody lines
two series up and cannot work out why they disagree.

### What fixing it would take

The work is small; the decision is not, and that asymmetry is why it is here.

1. **Decide what a daily bar's close instant is.** Either the curve is stamped
   at `ts` — the session date, matching what the session anchor already uses —
   or `close_ts` and `step` become exchange-aware and resolve to the real
   session close. The first is nearly a one-line change; the second is a
   calendar dependency in the domain layer.
2. **Both copies move together**, or the engine and `Bar.close_ts` start
   disagreeing about the same question.
3. **Every stored equity curve's labels change.** `backtest_runs.equity_curve`
   is not migrated by either option, so old rows keep the old dates and a chart
   comparing an old run with a new one would be comparing two conventions.
   Whether to migrate, and how to tell the two apart in a row, is the real
   question.

### Why it was deferred

Nothing it touches is wrong *numerically*, so it competes badly against defects
that are — and it was found alongside two of those (the raw-close pricing fixed
in #103, and the dropped warnings in #104), both of which changed reported
figures. Parking it keeps it from being fixed casually inside an unrelated diff,
which is the outcome that would leave stored curves in two conventions with no
decision recorded.

---

## mypy does not check the tests

**Status:** open · found 2026-08-26 · tooling

### What is wrong

`make typecheck` runs `uv run mypy libs apps` (`Makefile:190`), and CI's Type
check gate runs the same command. `mypy_path` names the three source roots and
no test path (`pyproject.toml`).

So a test that calls into `libs` with the wrong signature **type-checks
nowhere**. If that test is a unit test it at least fails when run; if it is an
integration test it also *executes* nowhere locally, because those skip without
Postgres and Redis. The two gaps compose: a broken call site in an integration
test is invisible to every check a contributor can run.

### How it was found

#104 gave `BacktestRunRepository.finish` a required `warnings` argument. Eleven
call sites were updated and a twelfth was missed — a multi-line call whose shape
did not match the others. Local lint, `mypy libs apps` and the whole unit suite
were green. CI was not:

```
TypeError: PostgresBacktestRunRepository.finish() missing 1 required
keyword-only argument: 'warnings'
FAILED tests/integration/test_backtest_runs.py::TestTheResultRoundTrips
```

Running `mypy` over that one file by hand reported the same defect statically,
in the form that would have caught it before the push:

```
error: Missing named argument "warnings" for "finish" of
"PostgresBacktestRunRepository"  [call-arg]
```

### What it costs while it stays

Every signature change to a core API can leave a broken test call site that no
local gate sees. The cost is a CI round trip at best, and at worst a test that
looks maintained and cannot run — the failure mode #96 and #104 both had in a
different layer, where what one side computed and what the other expected came
apart with nothing failing.

### What fixing it would take

Measured rather than guessed. `uv run mypy tests` today:

```
Found 166 errors in 41 files (checked 90 source files)
```

By category: 65 `type-arg` (bare `list` / `dict` annotations), 28 `arg-type`,
25 `unused-ignore`, 9 `object`, 9 `attr-defined`, 8 `no-untyped-def`, 8
`import-untyped`, the rest scattered. Two files hold 75 of the 166
(`test_decision_record.py` and `test_worker_main.py`).

**None of them is `call-arg`** — the class this exists to catch is already
clean. So the gate would be correct the day the noise is cleared, and most of
the noise is mechanical: filling in generic parameters accounts for well over a
third of it.

The change is then one line in the `Makefile` and one in `.github/workflows/ci.yml`,
plus whatever per-module relaxations the test tree warrants — `tests` does not
need the `disallow_untyped_defs` strictness `atp_core` is held to, and an
override saying so may be the difference between a day's cleanup and a week's.

### Why it was deferred

166 errors is a real, if mechanical, cleanup that touches 41 files and belongs
to no feature. Folding it into the bug fix that exposed it would have buried a
reviewable fix under unrelated churn, and doing it alone is a change worth
reviewing on its own terms.
