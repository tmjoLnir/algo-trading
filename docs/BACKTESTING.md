# Backtesting

Requirement #2. This document is mostly about how backtests lie, because a
backtest that has not been defended against these is worse than no backtest —
it produces false confidence, and false confidence is what gets deployed.

---

## The invariant

> A strategy may only use information that existed at its decision time.

Enforced structurally: `BacktestContext` cannot address a bar past the current
index, and orders generated on bar *i* fill on bar *i+1*. If a test fails
because of this, the test is telling you something true.

## The biases, in order of how often they ruin a backtest

### 1. Lookahead bias

Using data the strategy could not have had. It is rarely as obvious as reading
tomorrow's close:

- **The current bar's close** as an entry price when the signal comes from that
  same close. You cannot trade at a price you only know once the bar is over.
  Fill at the next bar's open. This one alone can turn a losing strategy into a
  spectacular one.
- **Restated fundamentals.** Earnings get revised. A database showing today's
  restated figure for a 2019 quarter is showing you something no one had in
  2019. Use point-in-time data.
- **Index membership as of today.** See survivorship, below.
- **Adjusted prices for signal generation** where the adjustment factor depends
  on a split that had not happened yet. Its mirror — raw prices, where the split
  *has* happened — is bias 6 below, and is the one that has actually bitten this
  platform.

### 2. Survivorship bias

A universe built from today's S&P 500 excludes every company that was in it and
went to zero. Backtesting "the S&P 500" over 2000–2024 using current membership
quietly removes Lehman, Enron, Bear Stearns, and hundreds of quieter failures.
Returns are inflated, drawdowns understated, and the effect is largest exactly
in the crisis periods you most wanted to test.

Use point-in-time constituent lists, or accept the bias explicitly and discount
the result.

### 3. Overfitting

The big one, and the hardest to see because it feels like work.

Test 200 parameter combinations, pick the best, and you have found the luckiest
one. With 200 trials, a Sharpe of 2 on noise is unremarkable. The backtest is
now a description of one specific historical path, not a strategy.

Defences:

- **Walk-forward analysis.** Optimise on window *n*, test on window *n+1*, roll
  forward. Only out-of-sample results count.
- **Hold out data you never look at** until the very end. Once you look, it is
  in-sample.
- **Count your trials.** If you tested 50 variants, say so — the reported Sharpe
  needs discounting for it.
- **Prefer fewer parameters.** A 2-parameter strategy that works is worth more
  than a 9-parameter one that works better in sample.
- **Distrust cliffs.** If SMA(50) is excellent and SMA(48) and SMA(52) are
  terrible, you have fitted noise. Real edges are smooth in their parameters.

### 4. Unrealistic fills

- Market orders do not fill at the last price. Cross the spread and add impact.
- Limit orders do not always fill when touched — you may be last in the queue.
- You cannot buy more than the market traded. Cap participation
  (`max_volume_participation`, default 10%).
- Stops fill worse than the trigger, especially on the gaps where they matter.
- When a bar's range spans both stop and target, assume the stop filled. The
  bar cannot tell you which came first, and the pessimistic reading is the only
  honest one at that resolution.

### 5. Ignoring costs

Commission, spread, borrow on shorts, slippage, taxes. A strategy edging 3bps
per trade at 20 trades a day needs to clear ~250% a year in costs. Never
evaluate with `ZeroCostModel` — it exists for testing engine mechanics only.

### 6. Unadjusted corporate actions

Rarer than the five above and more total when it happens. A split changes the
price and the share count on the same morning; raw prices carry only half of
that, so a backtest holding a fixed quantity through one reads the price change
as performance.

The engine handles this for you: `BacktestEngine.run` converts every bar into
adjusted space before the first mark, scaling open, high, low and close by
`adj_close / close` and volume by its inverse (`Bar.adjusted`). Bars with no
`adj_close` — what a `--raw-only` backfill leaves behind — refuse the run with
`UnadjustedDataError` rather than being priced raw. See
`docs/adr/0017-backtests-price-off-adjusted-closes.md`.

What that prevents, from the run that motivated it: a `buy_and_hold` over
twenty large caps priced off raw closes reported 331.7% over six years, of
which a single day was **+51.16%** — GE's 1:8 reverse split on 2021-08-02,
booked as an 8x gain, twenty-eight standard deviations of that run's own daily
volatility. A 4:1 forward split is the same defect pointing the other way and
is easier to miss, because a position that appears to lose 75% overnight looks
like a bad day rather than an impossible one.

If you load bars yourself rather than through `scripts/run_backtest.py`, the
conversion still happens inside the engine — but the bars you supply have to
carry `adj_close`.

## Running one

```bash
# CLI
uv run python scripts/run_backtest.py \
  --strategy sma_crossover --symbols SPY,QQQ \
  --start 2020-01-01 --end 2024-12-31 --timeframe 1d \
  --qty 100 --out results.json

# ...and the same run with the protection a live worker would give it
uv run python scripts/run_backtest.py \
  --strategy sma_crossover --symbols SPY,QQQ \
  --start 2020-01-01 --end 2024-12-31 --timeframe 1d \
  --sizing risk_pct --sizing-value 0.003 \
  --stop atr --stop-value 2 --stop-period 14 --out results.json
```

`--out` writes the whole result as JSON — the metrics, the money, the equity
curve — and, under `spec`, the request that produced them. That last part is
exactly what the two commands above differ by, and without it their files are
indistinguishable: the second run's return reads as a fact about
`sma_crossover` when it is a fact about `sma_crossover` sized at 0.3% risk
behind a 2×ATR stop. The block carries every field of the request — cost model,
strategy params, sizing method and value, stop type and its parameters — so a
`--zero-cost` run is legible as one rather than passing for evidence.

It is written by `atp_core.backtest.ports.spec_to_json`, which is the same
writer that fills `backtest_runs.config` for a queued run and reaches the
Backtests tab from there. A CLI export and a run exported from the tab
therefore describe themselves identically, and a field added to the spec lands
in both files or neither.

`warnings` carries both kinds: what the run did — coverage shortfalls, orders
refused — and the notes derived from the metrics about how far to trust them,
through the same `runner.all_warnings` the API serves. Read them first; the
"Reading the result" section below is what they are pointing at. They are also
printed to the terminal as the run finishes, but the file is the copy that
survives, and a result is usually read long after the scrollback is gone.

That includes the three the run itself earns — the zero-cost note, the
fixed-qty note, and the refusal summary — because the CLI runs through
`runner.run_spec`, which is what attaches them. It used to state all three on
screen and record none of them, so a `--zero-cost` export named the cost model
in its `spec` block and said nothing about it in its `warnings`: a debugging
run, on disk, reading as a result.

The terminal still says each of those three in its own place — two above the
run, the refusal summary below the table — so the metric table's warning block
skips what has already been said rather than repeating it. The file is not
filtered; it carries everything.

Or from the dashboard's **Backtests** tab, over `POST /api/v1/backtests`. Both
run through the same function (`atp_core.backtest.runner.run_spec`, which
assembles the engine with `build_engine` and attaches the caveats above), so the
same parameters produce the same result *and the same warnings* either way — two
call sites wiring their own engines would eventually disagree, and a dashboard
reporting a different Sharpe from the terminal is worse than either being wrong.

The API route is **queued, not inline**: the request records a row, hands the job
to arq, and answers `202` with a run id. A separate process executes it — the
`queue` container, one run at a time. Why it is a third process rather than part
of the trading worker, and every other decision behind the queue, is ADR 0016.
The short version: a backtest is minutes of solid synchronous Python, and running
one inside the process that manages open positions would stall the market-data
stream until the staleness watchdog halted trading.

A queued run moves `queued → running → done | failed`, and the row says which. It
carries three timestamps, because a queue puts real time between being asked and
starting: `queued_at`, `started_at` (null while it waits), `finished_at`. While it
runs it publishes bar counts, which is what the progress bar on the screen reads.

There is no cancel. arq cannot interrupt a job that is already executing, and an
endpoint reporting a cancellation the worker went on ignoring would be worse than
no endpoint; a bounded job timeout is what stops a mistaken run holding the queue.

A run left behind by a worker that was killed is marked **interrupted** the next
time one starts, rather than sitting at `running` forever — the worst outcome this
path can produce, and the one nothing else would ever correct.

**On a clean database the queued path needs nothing but bars.**
`backtest_runs.strategy_id` is a foreign key onto `strategies`, and for a long
time the only writer of that table was `StrategyRunner.warmup` at a live session
open — so the tab's picker was empty, `POST /backtests` answered 409, and
queueing a backtest meant configuring a *trading* worker with broker credentials
that a backtest does not need. `make seed` writes those rows for development
(plus some fabricated bars under reserved test tickers, see docs/DATA.md), and
`POST /backtests` now writes one itself for any registered class it is queueing
the first run of: the row an author would have created, at `draft`, with the
class's declared defaults and no universe. A registry the API can see is a
strategy the API can backtest. The CLI never needed any of it: it stores no run,
so it has no foreign key to satisfy.

What that row is *not* is a record of configuration. The run's params, symbols
and timeframe stay on the run — sweeping params over one strategy is what a
backtest's `params` are for, and a row claiming the strategy trades what the
last run happened to ask for would be a statement about live configuration that
nobody made. An existing row is never touched, so a worker's universe, params
and `last_started_at` are safe from anybody queueing a backtest.

Bars come from the database, not the vendor: a backtest has to be reproducible,
and re-fetching means today's answer can differ from yesterday's because the
vendor restated something. Run `scripts/backfill_bars.py` first — both the CLI and
the API name the exact command if the range is empty. The API checks coverage
*before* queueing, because a run that dies four minutes in for want of history is
a worse answer than a refusal.

**An empty range is the easy case. A partial one used to be silent.** Asking for
2017–2025 and holding bars only from late 2020 produced a run that reported the
window it was asked for and measured a different one, with no error and no
warning. Two checks in `BacktestEngine._validate` close that:

- **A hole inside a symbol's history is refused** — bars either side of a stretch
  longer than `max_gap_days` (10 by default) and none inside. There is no benign
  reading of one: `BacktestContext.closes` slices by *position, not by date*, so
  a 50-bar average spanning a hole silently averages prices from either side of
  it, and a bar-counting stop measures the hole as one bar. The refusal names the
  exact `backfill_bars.py` range to re-fetch. Calendar days against a flat
  threshold rather than sessions against an exchange calendar, because the engine
  is handed bars rather than a venue and one run's symbols can trade on exchanges
  whose holidays disagree; the longest US equity closure this century was six
  days, so the default clears every weekend and holiday comfortably. Raise it if
  a symbol genuinely stopped trading for a month — not to quiet a real hole.
- **A window shorter than the one requested warns.** A series starting after
  `--start` or ending before `--end` has legitimate causes — an ETF's inception,
  a delisting, a backfill that has not caught up — so it cannot refuse. One
  aggregated line per shortfall covers the whole universe rather than one per
  symbol, and it is placed ahead of everything the run appends, so it survives a
  caller that prints only the first handful of warnings.

## Sizing, and what the chain refuses

Both used to be caveats here and neither is any more.

**Sizing goes through `risk.rules.position_size`** — the same function the live
router calls, with the same arguments. `--sizing` picks the method and
`--sizing-value` supplies what it reads; `fixed_qty` remains the default so a
run stored before this existed reproduces exactly, and it still prints the
warning it always did, because sizing every entry identically still makes the
return a property of that share count. `risk_pct` is what docs/RISK.md calls
real sizing and it needs the strategy to emit a stop: a signal without one is
booked as a **refused order** naming the sizing stage, not silently dropped.

**The rule chain is live in a backtest**, as `risk.engine.backtest_rules()` —
five of the nine. The other four are excluded by decision rather than omission,
and `backtest_rules`' docstring gives each reason; the short version is that a
kill switch, a session calendar, a rate limit and a feed-staleness check are all
measuring something a replay over bars does not have. `trading_hours` is the
sharpest case: a daily bar is stamped at exchange-local midnight, so the
calendar says closed at every one of them and the rule would refuse every order
in every daily backtest.

## Stops, and what a run without them is measuring

**A backtest arms nothing you did not ask it to.** Until `--stop` (or the form's
**Stop** field) exists on a run, the engine watches only the levels a `Signal`
itself carries — and none of the shipped strategies emits one, so every run this
platform had produced was an unprotected one. If that strategy is configured
live behind `WORKER_STOP_TYPE=atr`, the backtest and the live worker are running
two different strategies, and the number on the screen belongs to the one you
are not going to trade. That is CLAUDE.md §5's divergence in its purest form: no
error, no warning, just a result about something else.

The default is still "no stop", and deliberately. Defaulting a stored spec to
`atr` would change what every historical run reports, and a spec is a record of
what was asked for. The CLI says so out loud on every run that omits it.

| `--stop` | Reads `--stop-value` as | Also arms a target | Trails |
|---|---|---|---|
| `atr` | a multiple of ATR | no | no |
| `chandelier` | a multiple of ATR | no | yes |
| `fixed_pct` | a fraction of entry | yes | no |
| `fixed_amount` | a price distance | yes | no |
| `trailing_pct` | a fraction | no | yes |
| `time` | — (uses `--stop-bars`) | no | — |

`atp_core.risk.StopManager` computes every one of those levels, and it is the
same object `StrategyRunner` uses live — one implementation, two callers (ADR
0006). So is `should_trigger`, and so is `target_hit`, which had a private copy
in each until this landed.

Four things the engine does with them that are worth knowing before reading a
result:

1. **The stop is derived before the size, not after.** `risk_pct` sizing is
   *defined* off `|entry − stop|`, so a stop that arrives at fill time arrives
   too late to size anything. This is why `--sizing risk_pct` over a stopless
   strategy books a refusal per entry, and why adding `--stop` fixes it.
2. **A strategy's own level always wins.** A configured stop fills gaps; it
   never overwrites a level the signal named.
3. **A trailing stop ratchets before the bar is tested against it.** A bar that
   both extends the move and retraces into the level *that bar* justified is
   stopped out, which is what a venue-side trailing stop would have done.
4. **A time exit fills at that bar's close.** It has no level to fill at, and
   the close is where the clock stands when the engine asks. It is labelled
   `time_exit` rather than folded into `exit`, because exit-reason attribution
   is how a strategy's stops get judged.

**The ATR that places the stop is computed from bars that had closed.** It goes
through the same cursor a strategy reads, so it cannot see the volatility it is
about to be measured against. An ATR over the whole series would place stops
using a future that had not happened — the quietest possible way to make a
backtest fictional, and one that flatters it.

**A stop that cannot be derived leaves the position openly unprotected.** An ATR
stop during warmup has no ATR, and `StopManager` refuses to default one. The
entry still happens and carries no stop, which is worse than it sounds and much
better than the alternative: a position that *looks* guarded at an invented
level is a position whose risk you have mismeasured.

**`broker_side` is False for every backtest stop, and that is not a preference.**
Live, the initial stop rests at the venue and fires without us (docs/SAFETY.md
layer 5). In a replay there is no venue — the engine *is* the fill model — so a
config claiming the level was resting elsewhere would describe a protection the
run does not provide.

**Expect the position cap to bite when you turn stops on.** docs/RISK.md's
recommended pair is `risk_pct` at 1% with a 2×ATR stop, and on a ~$97 stock with
an ATR near $1.64 that asks for 305 shares — 29.5% of a $100,000 account against
a 10% `RISK_MAX_POSITION_PCT`, so `max_position_size` refuses every entry. The
refusal line says so. It is not a bug in either the sizer or the cap; it is the
two limits meeting, and the run above uses 0.3% because that is what fits.

**Expect refusals, and read them.** A run's warnings end with one line counting
what was refused and by which rule. That line matters more than the return above
it: a backtest whose entries were mostly refused reports what the survivors did,
which is a statement about the limits rather than about the strategy. The
default `--qty 100` on a ~$100 stock is $10,000 against a $100,000 account —
right at `RISK_MAX_POSITION_PCT` — so runs that used to fill will now be
partly refused. That is the correction, not a regression: those positions were
always over the limit, and nothing said so.

## Reading the result

| Metric | Look for | Suspicion |
|---|---|---|
| Sharpe | > 1.0 | > 3.0 usually means a bug or lookahead |
| Max drawdown | Could you actually sit through it? | < 5% over years is implausible |
| Trade count | ≥ 100 | < 30 and the statistics mean nothing |
| Profit factor | > 1.3 | ∞ means too few trades, not perfection |
| Win rate | Any | On its own it tells you nothing — see expectancy |
| Expectancy | > 0 after costs | The number that decides go/no-go |

**A Sharpe above 3 on a simple strategy is a bug until proven otherwise.** Check
fill timing first, then data alignment. It is almost never a discovery.

**Every metric in that table counts closed round trips. `ending_equity` does
not.** A trade's P&L is only known when it closes, so `num_trades`, `win_rate`,
`profit_factor` and `expectancy` are computed from completed trips — while
ending equity marks the still-open ones to the last bar. A run that finishes
holding winners therefore reports a gain its closed trades never made, and a
`profit_factor` below 1 can sit directly beneath a positive total return without
either being wrong.

So the report states both halves. `realized_pnl` is what the closed trips made,
net of fees; `unrealized_pnl` is the mark on whatever was still open at the end;
`open_positions` counts them. They add back to `ending_equity - starting_equity`
exactly, because the realised half is computed as the remainder rather than as a
second sum that could drift from it. The CLI prints the split under ending equity
and says how much of the return is a mark.

Read `expectancy` and `realized_pnl` together as the track record. A large
`unrealized_pnl` is not a result — it is a position, and it is a statement about
one arbitrary day.

**The dashboard says it too, in the same words.** All nine figures are stored on
a queued run (`backtest_runs.totals`) and the run panel carries a *Money* block
above the metric grid, with the same "N positions still open at the end,
carrying X of unrealised" sentence the CLI prints. Until they were stored, the
queued path kept neither reading — the screen showed a return with nothing to
say how much of it had been banked, and the nearest hint was `num_trades: 0`,
which says something different (ADR 0019). A run recorded before that column
existed shows no split at all rather than zeros: those figures were computed and
thrown away, and there is nothing to reconstruct them from.

**An equity point is dated by its bar, not by the clock** (ADR 0018). The two
are not the same instant: the engine's clock stands at `ts + step`, which is
when the bar's decision could first be taken and what stamps its orders, while a
curve point names the *session* whose equity it reports and so carries the bar's
own `ts`. For a daily bar the difference is a calendar day — a daily bar is
stamped at exchange-local midnight, so `ts + 24h` is the next midnight rather
than the 21:00 UTC close — and a curve built from the clock filed Friday's
session under Saturday and had no Mondays at all.

That matters when you line a curve up against something else. A backtest curve,
a live equity series and a benchmark are all keyed on the session date, so
joining them by date lands. It never mattered to a metric: no metric reads these
labels except `max_drawdown_duration_days`, which takes a difference between
two of them.

## Before believing a result

- [ ] Ran with realistic costs
- [ ] Beat `buy_and_hold` over the same bars, costs and sizing
- [ ] ≥ 2 years, including a drawdown period
- [ ] ≥ 100 trades
- [ ] Walk-forward, not one in-sample fit
- [ ] Trial count known and disclosed
- [ ] Individual trades inspected — no impossible fills
      (`GET /api/v1/backtests/{id}/trades`, or open the run on the Backtests tab)
- [ ] Equity curve is not one lucky trade with noise around it
- [ ] Results survive ±20% parameter perturbation
- [ ] Data checked for gaps — an unadjusted split is refused by the engine
      rather than left for this checklist (bias 6 above)

## Comparing runs

`GET /api/v1/backtests/compare?run_ids=a&run_ids=b` puts metric sets side by
side, and the Backtests tab does it from checkboxes. Two things about it are
deliberate and both are about the *Overfitting* section above:

- It is capped at a handful of runs. That is the argument expressed as a limit
  rather than as advice — an endpoint that cheerfully ranked fifty runs would be
  tooling for the mistake.
- It marks no winner. Highlighting the best value in each row would be the
  interface making the choice that section asks you not to make on these numbers
  alone.

Every comparison carries the warning in its own response, because the person
reading a comparison table is the person about to promote something.

**The comparison worth drawing first is against the benchmark.** `buy_and_hold`
is a registered strategy like any other — run it over the same bars, the same
costs and the same sizing, and compare the two runs. A return with nothing
beside it is not evidence: 18% over a year is skill against a flat market and a
bad year against one that returned 30%, and the number alone cannot tell you
which. The benchmark is not exempt from next-bar fills, so it is a price you
could actually have paid rather than a paper one — see
`docs/STRATEGY_AUTHORING.md` on why that matters more for the baseline than for
anything measured against it.

## Then paper trade it

A passed backtest earns a paper deployment, not a live one. Four weeks minimum,
then compare with `GET /api/v1/analytics/live-vs-backtest/{run_id}`. Divergence
there is the cheapest lesson this platform can teach you.

**The `{run_id}` is a backtest run, not a strategy**, and that is the substance
of the endpoint rather than a URL detail. A strategy accumulates any number of
stored runs over different windows, cost models and share counts; comparing live
against an arbitrary one — the newest, say — reports a divergence against a
backtest nobody used to approve anything. So you name the run, and the strategy
is read off it: the two halves cannot be about different strategies, because only
one of them was ever specified. Running a backtest inside the request would be
worse still — it would compare live against whatever parameters that request
happened to pass.

What the endpoint cannot do is check that the run you named is the one the
promotion was granted against. Nothing records that yet (ADR 0010's lifecycle
verbs), so choosing an unrepresentative run gives an answer that is
arithmetically correct and worthless. Note the run id in the promotion checklist
and compare against that one.

Read the warnings before the numbers. `docs/ANALYTICS.md` has the full argument;
the short version is that a live paper month and a backtested five years are
measured over different windows, annualised on different bases and sized by
different rules, and several rows of the divergence table move for those reasons
before the strategy has done anything.
