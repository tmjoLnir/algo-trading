# F8 — What timeframe is `sma_crossover` actually for?

**Measured:** 2026-09-13 · **Raised by:** `day-4-review.md`, F8
**Data:** 346,643 real Alpaca bars over the 20-name paper watchlist — 170,542 × `1m`
(2026-08-10 → 09-12), 147,546 × `5m` (2026-05-01 → 09-12), 28,555 × `1d`
(2021-01-01 → 2026-09-12)

---

## The question

Day 4 of the paper week ran `sma_crossover` with `timeframe=1m`, SMA 20/50 and an
`atr x2 period=14` stop. It closed 41 round trips, **all 41 at their stop, none
profitable**, and the review's F8 observed that the stop looked about 0.10% wide
and that no part of that configuration had ever been backtested at the timeframe
it was running on.

F8's own proposed fix was *"pick the horizon deliberately and size the stop to
it."* The obvious reading — the stop is too tight, widen it — turns out to be
wrong, and the measurement below is why.

## How this was measured, and what that does not support

Bars were fetched straight from `AlpacaHistoricalProvider` and fed to
`backtest.runner.run_spec` in process. Same provider, same engine entry point the
CLI uses, `alpaca_equities` cost model, full risk chain. **Not** through
`scripts/run_backtest.py`, because that reads bars from Postgres and the machine
this ran on had none — so these numbers are reproducible from the script in the
scratchpad, not from a `backfill_bars.py` + `run_backtest.py` pair. Anyone
repeating this should do it the documented way and expect small differences from
vendor restatements.

**Trial count: 26 configurations** — three timeframes × three period pairs ×
three ATR multipliers, less one 1m cell that did not finish. The PR template asks
for this number because it is what separates a result from a search, and 26 is
easily enough to find a flattering cell by accident. Nothing below is a
recommendation to use the best-performing cell.

**The windows differ per timeframe** — a month of `1m`, four and a half months of
`5m`, five and three-quarter years of `1d` — because that is what is available
and affordable at each resolution. `backtest/metrics.py` already draws the
distinction this needs: `total_return` and `max_drawdown` are `BASIS_WINDOW` and
are **not** comparable between these runs, while `win_rate` is `BASIS_PER_TRADE`
— *"two runs of different lengths over different windows can be compared on
these directly."* So win rate and order count carry the argument; the returns are
context.

`sharpe` is present for the daily block and absent for the intraday one: the run
that produced these tables read a key that does not exist (`sharpe_ratio` for
`sharpe`), and the daily grid was cheap enough to repeat while the 1m grid was
not. It is `BASIS_ANNUALISED` and so not comparable *between* the blocks anyway —
but it is comparable within the daily block, and it is the number that stops the
daily column being read as a triumph. See the last paragraph of **What to do**.

---

## 1. The same stop config is 34× tighter at 1m than at 1d

`atr x2 period=14`, as a percentage of price, computed through
`indicators.dispatch` — the same call the live runner makes:

| Timeframe | Median | Min | Max |
|---|---|---|---|
| `1m` | **0.121%** | 0.027% | 0.255% |
| `5m` | 0.205% | 0.064% | 0.505% |
| `1d` | **4.082%** | 1.618% | 10.472% |

A stop 0.12% from entry is inside the noise on a one-minute bar. It cross-checks
against production: day 4's realised exit-versus-entry distance, measured from
actual fills, had a median of 0.104%.

**The configuration string is identical in every case.** `stop='atr x2
period=14'` appears in `worker.config_loaded` and says nothing about which of
these two stops is in force. That is now fixed — `runner.warmed_up` carries
`stop_width_bps`, computed from the bars warmup just loaded, so the number that
took a week and a bar fetch to establish is on the boot line.

## 2. Widening the stop does not help. The timeframe is the whole problem.

`sma_crossover`, `alpaca_equities` costs, full risk chain, `equity_pct 0.05`
sizing:

| tf | fast/slow | stop | orders | fills | return | sharpe | maxDD | **win%** |
|---|---|---|---|---|---|---|---|---|
| `1m` | 20/50 | ×2 | 2,667 | 1,415 | −58.23% | – | −58.23% | **0.86%** |
| `1m` | 20/50 | ×4 | 2,705 | 1,481 | −60.05% | – | −60.07% | 0.97% |
| `1m` | 20/50 | ×8 | 2,682 | 1,465 | −60.23% | – | −60.24% | 0.97% |
| `1m` | 10/30 | ×2 | 4,088 | 1,538 | −59.19% | – | −59.19% | 1.85% |
| `1m` | 10/30 | ×4 | 4,047 | 1,574 | −60.49% | – | −60.49% | 1.69% |
| `1m` | 10/30 | ×8 | 4,045 | 1,589 | −60.66% | – | −60.66% | 1.80% |
| `1m` | 50/200 | ×2 | 1,092 | 1,048 | −49.56% | – | −49.56% | 0.60% |
| `1m` | 50/200 | ×4 | 1,080 | 1,037 | −49.59% | – | −49.59% | 0.60% |
| `5m` | 20/50 | ×2 | 3,389 | 3,389 | −63.03% | – | −63.03% | 10.01% |
| `5m` | 20/50 | ×4 | 3,347 | 3,347 | −63.80% | – | −63.81% | 12.92% |
| `5m` | 20/50 | ×8 | 3,169 | 3,169 | −62.02% | – | −62.02% | 13.40% |
| `5m` | 10/30 | ×2 | 5,571 | 5,556 | −76.94% | – | −76.94% | 8.31% |
| `5m` | 10/30 | ×4 | 4,888 | 4,874 | −75.12% | – | −75.13% | 9.06% |
| `5m` | 10/30 | ×8 | 5,043 | 5,023 | −75.97% | – | −75.98% | 9.74% |
| `5m` | 50/200 | ×2 | 898 | 898 | −25.03% | – | −25.05% | 9.22% |
| `5m` | 50/200 | ×4 | 893 | 893 | −24.34% | – | −24.36% | 14.69% |
| `5m` | 50/200 | ×8 | 845 | 845 | −22.46% | – | −22.49% | 17.20% |
| `1d` | 20/50 | ×2 | 605 | 605 | +23.63% | 0.59 | −14.11% | **31.21%** |
| `1d` | 20/50 | ×4 | 596 | 595 | +21.62% | 0.49 | −16.84% | 38.14% |
| `1d` | 20/50 | ×8 | 590 | 589 | +18.04% | 0.41 | −16.71% | 39.58% |
| `1d` | 10/30 | ×2 | 994 | 991 | +26.39% | 0.70 | −9.30% | 34.48% |
| `1d` | 10/30 | ×4 | 942 | 938 | +25.91% | 0.61 | −14.38% | 38.71% |
| `1d` | 10/30 | ×8 | 1,021 | 1,014 | +38.37% | 0.74 | −15.11% | 39.56% |
| `1d` | 50/200 | ×2 | 109 | 109 | +62.77% | 1.21 | −10.26% | 18.75% |
| `1d` | 50/200 | ×4 | 104 | 98 | +89.96% | 1.21 | −15.02% | 32.56% |
| `1d` | 50/200 | ×8 | 102 | 96 | +72.30% | 1.09 | −16.33% | 41.46% |

**Every one of the 17 intraday cells loses money. Every one of the 9 daily cells
makes money.** That is not a margin a search could manufacture — it is unanimous
across three period pairs and three stop multipliers on each side.

**Within the 1m block the stop multiplier is irrelevant.** ×2 → ×8 moves the
return from −58.23% to −60.23% and the win rate from 0.86% to 0.97%. The naive
F8 fix — widen the stop — does nothing, because the stop was never the binding
constraint.

**What is binding is the trade rate.** 20/50 on one-minute bars produces 2,667
orders in one month; the same pair on daily bars produces 605 in five and
three-quarter years. That is roughly two orders per hundred bars either way — the
strategy is behaving identically — but one of those rates pays the spread 2,667
times a month. A 0.86% win rate is not a strategy with a bad stop. It is a
machine for paying costs.

The 1m block also shows the platform resisting: 1,415 fills from 2,667 orders,
against 605 from 605 at daily. The rest were refused by the risk chain or capped
by volume participation.

---

## What to do

**Set the live `WorkerConfig` timeframe back to `1d`** — which is what
`SmaCrossover` declares as its own default, and what every cell of this
measurement prefers. This is a configuration row an operator edits on the
dashboard, not something code can change, so it is a recommendation rather than
part of this diff.

**Leave the stop at `atr x2 period=14`.** At `1d` that is a ~4% stop, which is a
sane distance for a daily swing position, and the multiplier is second-order in
every block above.

**Do not take the periods from this table.** 50/200 ×4 tops it at +89.96%, and
picking it on this evidence would be exactly the overfitting the trial-count
disclosure exists to flag: one dataset, 26 cells, no out-of-sample window, no
walk-forward. If period tuning is wanted it needs its own work with a held-out
period. The 20/50 the strategy ships with is not the best cell here and is
perfectly serviceable.

**And do not read the daily column as a triumph.** The shipped 20/50 ×2 config
returns +23.63% over five and three-quarter years at a **Sharpe of 0.59** — which
is a mediocre risk-adjusted result, roughly what holding the index would have
given with less work, on a 20-name universe over a period that was mostly up. The
1.21 Sharpe cells are the 50/200 pair, which is the one this document refuses to
recommend. The honest summary of the daily block is *"not obviously broken"*, not
*"good"*. It is the difference between a configuration worth spending a paper week
on and one that is not, and nothing more than that.

**A 1-minute strategy is not impossible — this one is not it.** An intraday
crossover would need a cost model it can clear, which means either far fewer
signals or an edge per trade several times the spread. Nothing in this table
suggests `sma_crossover` has one.

## What this changes about the paper week

Days 1–4 have each been voided by a different defect. If the config had been
right, day 4's session would still not have measured anything useful: it was
running a configuration that loses ~60% a month in backtest. **The first paper
session worth reading is one run at `1d`** — which also means one bar per symbol
per day, and a week of it is five decisions per symbol, not 2,000. That is worth
knowing before anyone reads a week of daily-bar paper trading as a verdict on
anything.
