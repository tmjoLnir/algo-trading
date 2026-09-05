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

## Live against the backtest

`GET /analytics/live-vs-backtest/{run_id}` — the most important report here, and
the one most easily read into saying something it does not.

**It is keyed on a backtest run, not on a strategy.** A strategy accumulates any
number of stored runs over different windows, cost models and share counts, and
comparing live against an arbitrary one — the newest, say — reports a divergence
against a backtest nobody used to approve anything. So the caller names the run,
and the strategy is *read off it* rather than passed alongside: the two halves of
the comparison cannot be about different strategies, because only one of them was
ever specified. Running a backtest inside the request would be worse again — it
would compare live against whatever parameters that request happened to pass.

**What it still cannot do** is verify that the named run is the one the promotion
was granted against. Nothing in the platform records that; it is the audit
trail's lifecycle verbs (ADR 0010) and is not built. Naming an unrepresentative
run produces an answer that is arithmetically correct and worthless, and the
response cannot detect it.

**The live window is open at the start by default**, unlike every other endpoint
on this router. The others describe a period a reader chose, and a month is the
period an operator asks about without thinking. This one asks whether a strategy
has held up, and the denominator for that is its whole live record — a default
that compared the last 30 days of a three-month paper run against a five-year
backtest would answer a narrower question in a way nothing in the response
distinguished from the broader one.

### Why two thirds of the response is context

The arithmetic is one subtraction per metric. Everything else is there because
the two sides are rarely measuring the same thing, and every one of these
differences produces a divergence that is correct and misleading:

- **The annualisation bases differ.** The engine annualises a backtest by its bar
  spacing — 252 for dailies, 252×390 for minutes (`metrics.periods_per_year_for`,
  which the engine and this endpoint now share so they cannot drift). The live
  side is inferred from a curve that steps once per *closed trade*, which for a
  six-trade paper month is around 20. Every annualised ratio differs by that
  factor before the strategy has done anything, and a live Sharpe that reads
  *better* than the backtest is the usual result. Pass `periods_per_year` to put
  both on one basis; the warning names both numbers and disappears when they
  agree.
- **The windows are different lengths.** `total_return`, `max_drawdown`,
  `num_trades` and `turnover` all scale with the window. A live month against a
  backtested five years gives a `total_return` divergence of very nearly minus
  the backtest's return, whatever the strategy did.
- **The sizing rules differ.** A backtest sizes every entry at a flat share count
  (`spec.qty`, a placeholder that says so); live sizing is risk-based
  (docs/RISK.md). The money-denominated metrics are partly a difference between
  two sizing rules.
- **The symbol sets can differ.** Live trades are **not** filtered to the run's
  symbols, deliberately. Dropping them would tidy the numbers and hide a strategy
  trading names it was never approved on — which is a finding, not noise. The
  warning names them instead. The reverse case matters too: a backtested symbol
  live has never touched is a refusal or a data gap, and from the trade count
  alone it is indistinguishable from underperformance.

So each metric carries a `comparability` of `per_trade`, `annualised` or
`window` (`backtest.metrics.METRIC_BASIS`), saying which of the above reaches it.
The per-trade statistics — win rate, profit factor, expectancy, the win and loss
sizes — are the ones two runs of different shapes can be compared on directly,
and they are where a real divergence shows up first.

The warnings are computed server-side for the same reason `suspicious` attaches
a backtest's own to it: a number a human has already read is a number they have
already believed.

### Nulls

A divergence is `null` where either side does not have the number, and that means
**not available**, never zero. Zero is the strongest claim this report can make —
live matched the backtest exactly on this metric — and it is the last thing an
absent value should render as.

Absences are routine rather than exotic. `backtest.runner.jsonable` nulls every
non-finite metric on the way into the row because `Infinity` is not legal JSON,
and an infinite `profit_factor` means the backtest had no losing trade — which is
precisely the run somebody wants to hold a live record up against. A metric
missing from a run stored before the metric set grew a field gets the same
treatment, so adding one metric does not retire every backtest on record.

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
| `GET /analytics/live-vs-backtest/{run_id}` | live against one stored backtest run, metric by metric |
| `GET /analytics/reports/daily` | one session, section by section, and what nothing counts |

Every monetary value crosses the wire as a **string** and nothing downstream
parses one back (docs/DASHBOARD.md).

## The screen

`/analytics` in the dashboard, and it is the only consumer of the endpoints
above. It is a fold over stored fills and adds no server capability of its own.

**Four panels, and the fourth is shaped unlike the other three.** Performance,
attribution and the trade list describe a period the reader chose. Live-vs-
backtest is keyed on a *run*, and three consequences follow that are the panel's
whole design rather than its presentation:

- **The picker starts empty.** Which backtest a live record is judged against is
  the substance of the comparison, so defaulting to the newest run would be the
  screen making exactly the choice the endpoint refuses to make. Only completed
  runs are offered — the endpoint answers 400 for any other, so a picker that
  listed them would offer a choice the server refuses.
- **The page's date range does not reach it.** The live window is open at the
  start by default and that default is load-bearing; forwarding the selected
  month would compare a month of live against a five-year backtest while looking
  like it had compared everything.
- **`comparability` renders beside every row and the warnings above them**, and
  a null divergence renders as a dash. Nothing is coloured good or bad: on most
  rows the sign is a difference rather than a verdict — more volatility, more
  round trips or a longer hold is not a failure — so the screen states the
  difference and leaves the reading to the operator, the same refusal
  `/backtests`' comparison table makes about marking a winner.

The annualisation warning has an answer on screen rather than only a
description: a control pins both sides to the backtest's own basis, using the
`periods_per_year` the response reports for that run. The client never computes
that number itself — a second copy of `periods_per_year_for` would drift from the
one the engine used and then disagree with the figure it is supposed to
explain.

**Three requests, one window.** docs/DASHBOARD.md refuses to assemble the live
screen from six fetches, because a P&L computed at one instant beside a price
fetched at another disagree and the reader cannot tell which to trust. That
argument does not reach here, for the same reason these endpoints reconstruct on
request rather than serving something the worker published: a round trip that
finished last Tuesday has finished, and its P&L is the same number in all three
responses. The page sends `start` and `end` explicitly on all three so they are
demonstrably describing one period rather than three that coincide, and each
panel renders or fails on its own — a reader who can still see their trades is
better served than one looking at an error page.

**Nothing polls** — and since ADR 0022 nothing anywhere does, so this stopped
being the exception it was written as. The reason these reads never polled is the
stronger one: a finished period cannot change, and re-reading would repeat a
reconstruction over the whole order history to produce an identical answer.

Four things the screen refuses to do, each of which is the tempting version:

- **A period with no closed trades is a sentence, not a grid of zeros.**
  `compute_all` legitimately returns 0.0 for every ratio it cannot compute, and
  nineteen of them on screen read as a flat month rather than as an empty one.
  The same rule the dashboard applies to a single figure — a number we do not
  know is `—`, never `0` — applied to a whole panel.
- **The annualisation basis is on screen beside the ratios it scales.** A reader
  who disagrees with a Sharpe should be able to check `periods_per_year` before
  doubting the arithmetic.
- **An unmeasured excursion is a dash; a measured zero is a zero.** Straight
  through from the null this module is careful to send.
- **The money-shaped statistics are labelled as statistics.** `expectancy`,
  `avg_win`, `avg_loss`, `largest_win` and `largest_loss` are floats — computed
  in float space by `backtest/metrics.py`, deliberately shared with the backtest
  — and formatting them with the ledger's formatter would claim a precision the
  response does not carry. `src/lib/money.ts` accepts only strings, so the
  compiler refuses the confusion rather than relying on anyone remembering it;
  `src/lib/stats.ts` is the other half and says where the boundary runs. Exact
  per-trade P&L is in the trade table, as a string, all the way to the pixels.

One thing it adds that the API does not: **the hour grouping is labelled UTC.**
`by=hour` keys on the entry's UTC hour, while every timestamp on the screen
renders in the reader's local time, and a bare `14` beside a local-time trade
list invites a comparison between two different clocks. The weekday needs no
such treatment — a US equity session never straddles UTC midnight, so its UTC
day is its session day.

## Not built yet

Stated here rather than left to be discovered:

- **Feed incidents are still nowhere queryable**, and the daily report says so
  rather than working around it. Reconnects, gaps and staleness are log lines —
  `data.stream.reconnected`, `data.stream.gap_widened_from_storage`,
  `data.staleness.detected` — with no table behind any of them, so that section
  of the report reports **absent** and carries the grep that answers it. The
  other two gaps this list used to name have closed: refused orders became rows,
  and halts a person engaged became audit entries. The ones the risk layer
  engages on its own still write no row, which the halts section states on the
  face of its own count.
- **No period comparison.** The screen reports one window at a time. "Is this
  month worse than last?" is answered by changing the dates and remembering,
  which is the shape of question a stored trade table would make cheap.
- **A trade opened by one strategy and closed by another is attributed to the
  first.** The entry names the strategy. Two strategies trading one symbol
  cannot be told apart per position, and nothing in the platform stops that
  configuration — it just is not attributable.
