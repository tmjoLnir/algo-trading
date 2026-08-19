# Analytics

Requirement #6: what did each strategy actually do, and was it worth running?

The live dashboard (docs/DASHBOARD.md) answers *what do we hold right now*. This
answers *what happened*, over a period that has already finished. They are
different questions with different failure modes, and the split runs through
every design choice below.

## What a trade is

**One position episode: flat, through however many scale-ins and partial exits,
back to flat.** Not one order, and not one tax lot. ADR 0015 has the argument;
the short version is that three of the four things worth knowing about a trade
are only defined on an episode — its exit reason, its holding period, and the
window an excursion is measured over.

Worked through, so the convention is unambiguous:

| Fills | Trades reported |
|---|---|
| buy 100 @ 50, sell 100 @ 55 | one long, entry 50, exit 55 |
| buy 100 @ 100, buy 100 @ 102, sell 200 @ 110 | **one** long, entry 101 (VWAP), qty 200 |
| buy 200 @ 100, sell 100 @ 110, sell 100 @ 120 | **one** long, exit 115 (VWAP), closed at the second sell |
| buy 100 @ 100, **sell 300 @ 110**, buy 200 @ 105 | **two**: a long 100→110, then a short 110→105 |
| buy 100 @ 50 (still held) | **none** — a position, not a round trip |

The fourth row is the one that matters. A single fill can carry a position
*through* zero, and it is two facts at once: the long closed, and a short
opened. Handled as either alone, one whole round trip disappears — and the
missing one surfaces much later, as an exit with no entry, which reads as a
short that was never opened and inverts the sign of its P&L.

### The matching convention

**FIFO**, and it binds in exactly one place. Within an episode, entries and
exits are aggregated, so FIFO and LIFO give identical per-trade P&L — there is
nothing to choose between. The only split is on a flipping fill: the closing
part is whatever the old episode still had open, the remainder opens the new
one.

That fill's **fee is divided pro rata by quantity**. There is no better answer
available: the venue charged one commission for one execution and nothing in the
print says which part belonged to closing.

### Exit reasons

Read off the closing order's `purpose` (`atp_core.execution.idempotency`), which
is stored on the order:

| `purpose` | Reported as | Set by |
|---|---|---|
| `exit` | `signal` | a strategy's `EXIT` signal |
| `entry` | `signal` | an order that reversed the position through zero |
| `stop_loss` | `stop_loss` | the runner's step 2, or a broker-side stop |
| `take_profit` | `take_profit` | the runner's step 2 |
| `time_exit` | `time` | a `StopType.TIME` config |
| `flatten` / `manual` | `manual` | an operator, or the runbook |
| *(null)* | `unknown` | stored before migration `c3f8b2d5e714` |

`unknown` is reachable for real and is deliberately **not** guessed into a
bucket. A wrong exit reason is worse than a missing one — it is the number that
decides whether a strategy's stops are misplaced.

This is the most actionable grouping the platform produces. A strategy whose
profit comes entirely from its take-profits while its stops bleed has a
stop-placement problem, not a signal problem, and no other table says so.

## MAE and MFE

Maximum adverse and favourable excursion: the worst and best *unrealised P&L*
during a trade (docs/GLOSSARY.md). Both are scaled by the position's quantity,
so they are money rather than a distance. MFE is ≥ 0 and MAE is ≤ 0 by
construction.

Three things to know before trusting one:

- **They are bounds at bar resolution, not measurements.** The bar covering the
  entry has a high and a low that may have printed before we were filled, so its
  extremes are attributed to a position that did not yet exist. The error is one
  bar wide at each end and always in the direction of a *larger* excursion — so
  MAE is the pessimistic end of the range, which is the right direction for a
  number used to decide whether a stop sits too close. Ask for minute bars to
  narrow it.
- **No bars means `null`, never zero.** Zero says the trade never went against
  us, which is the most flattering possible reading of "we did not measure".
  The same rule the dashboard follows for an unmarked position.
- **Zero on one side is a measurement.** A trade that only ever went one way has
  a genuine zero on the other. A gap that opened past the entry and never came
  back would make the raw favourable number negative, and a negative *maximum
  favourable* excursion is not a quantity anyone can read, so it is clamped.

## Attribution

`GET /analytics/attribution?by=…` over `strategy | symbol | hour | weekday |
exit_reason`.

`hour` and `weekday` group on the **entry**, because the question they answer is
when a strategy finds trades worth taking. Grouping on the exit would answer
when its stops happen to fire, which is a fact about the market's schedule.

`contribution_pct` is denominated in the total **absolute** P&L, not the net.
Net is the intuitive denominator and it misbehaves exactly when a reader most
needs the number: a period whose winners and losers nearly cancel has a
near-zero net, and a share of it reads as +900% for one strategy and −800% for
another. Against the absolute total every contribution lands inside ±100% and
the signs still say who helped and who hurt.

An unknown dimension is a **422**, not an empty list. A report silently grouped
by nothing looks like a period with no trades.

## Statistics

`PerformanceAnalyzer.metrics` runs through `backtest/metrics.py` — the same
functions the backtest engine uses, deliberately (ADR 0006's reasoning). A live
Sharpe computed by different code from the backtested one cannot be compared to
it, and comparing them is the point of running paper first.

**Annualisation is the one way to make every ratio wrong while all of them still
look plausible.** `periods_per_year` is inferred from the equity curve's own
median gap unless a caller pins it, because the caller most likely to be wrong
is the one that does not think about it: the runner writes an equity point per
evaluation — once a minute — and annualising a minute-sampled series as though
it were daily understates volatility by about twenty times. The endpoint returns
the value it used, so a reader who disagrees with a Sharpe can check this before
doubting the arithmetic.

The median rather than the mean, because a curve has a 16-hour hole at every
overnight and a mean over those lands nowhere near either sampling rate.
Inference has two regimes — sub-daily gaps divide into the 6.5-hour session,
daily-or-longer gaps divide into 252 — and is exact at one sample a minute and
at one a day. A weekly series reads 36 rather than the 50 its five-trading-day
week deserves; pass `periods_per_year` explicitly for one.

**`max_drawdown` here is the drawdown of *realised* P&L.** The curve
`/analytics/performance` builds steps only when a round trip closes, so it is
shallower than what the account actually experienced. That is the right curve
for a statistic about closed trades and the wrong one for "how bad did it get" —
`/dashboard/equity-curve` answers that.

`daily_returns` groups by **UTC calendar day**, and for US equities that is the
session: the cash market runs 13:30–21:00 UTC at the widest, so a session never
straddles UTC midnight. A market that does — an overnight future — would need
the exchange's own trading day and this would silently split one session in two.
Named rather than handled, because nothing here trades one. The first day has no
prior close and is **absent** rather than zero; absence says the series started
there, zero would claim the account was flat.

## The read, and what it costs

Reconstruction reads **every filled order in the account's history** up to the
window's end, and then filters the *trades* by the window.
`OrderRepository.filled_orders` takes `until` and no `start`, and that asymmetry
is load-bearing: round trips are matched from flat, so a window applied to the
orders going in would present every position opened before it as an exit with no
entry.

The cost grows with the lifetime of the account. It is affordable now — one
operator, one strategy, a paper week — and the threshold is roughly when a
request stops returning inside a second on the production box.

**When it does, the fix is a stored trade table, not a truncated read.** A
truncated read does not get slower, it gets wrong. The trade table should be
built as a *cache* — reconstructed once, appended to as positions go flat,
rebuildable from `orders` — rather than as the record itself, so that a trade
taken while the worker was down is not missing from it forever.

## The decision record

Attribution is a join, and until #58 two of its three sides did not exist.

- `strategies` — one row per strategy, written by
  `StrategyRepository.ensure` at every session open. The id is the strategy's
  *name*, because that is what `Signal.strategy_id` carries everywhere in the
  platform.
- `signals` — every decision and its fate, written by `SignalRepository.save`
  as the runner routes it. **Refusals are kept**, and that is the point rather
  than an edge case: from the orders table alone, a strategy whose every idea
  was refused is indistinguishable from a strategy that had no ideas, and those
  two call for opposite responses.
- `orders.strategy_id` / `orders.signal_id` — foreign keys to the two above.
  They were stored as literal `None` because their targets did not exist.

The foreign keys mean the write ordering is not advisory: ensure the strategy,
record the signal, save the order. A caller that skips one gets an integrity
error rather than a null, which is the intended outcome — a null is how this gap
stayed invisible for four phases.

`Signal.indicators` are stored as **strings**, not JSON numbers. An indicator
value is usually a price — an SMA of closes is denominated in dollars — and JSON
has only binary floats to carry it (rule §1.1). They are not parsed back on the
way out, because the column holds values of several kinds and this layer cannot
know which is which.

## Endpoints

| | Returns |
|---|---|
| `GET /analytics/performance` | the full metric set over a period |
| `GET /analytics/trades` | completed round trips, newest first, with MAE/MFE |
| `GET /analytics/attribution` | P&L grouped by one dimension |
| `GET /analytics/live-vs-backtest/{id}` | **not built** — see below |
| `GET /analytics/reports/daily` | **not built** — see below |

Every monetary value crosses the wire as a **string** and nothing downstream
parses one back (docs/DASHBOARD.md).

## Not built yet

Stated here rather than left to be discovered:

- **Live-vs-backtest comparison.** Half of it exists:
  `PerformanceAnalyzer.compare_to_backtest` computes the divergence metric by
  metric, live minus backtest. What is missing is the other operand — there is
  no stored backtest result to compare against, because `backtest_runs` has no
  reader and `/backtests` is a stub. Running a backtest on the fly inside the
  request would compare live against whatever parameters that request happened
  to pass, rather than against the backtest that approved the strategy, which is
  the only comparison worth making.
- **The daily report.** Trades and P&L are available from this module now. The
  other three things the report wants are not gathered anywhere one query can
  reach: rejections are in `signals`, halts are in the kill switch's records,
  and feed incidents exist only in the worker's logs.
- **No UI.** These endpoints have no screen. The dashboard renders the live book
  and nothing here.
- **A trade opened by one strategy and closed by another is attributed to the
  first.** The entry names the strategy. Two strategies trading one symbol
  cannot be told apart per position, and nothing in the platform stops that
  configuration — it just is not attributable.
