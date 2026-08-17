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
  on a split that had not happened yet.

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

## Running one

```bash
# CLI
uv run python scripts/run_backtest.py \
  --strategy sma_crossover --symbols SPY,QQQ \
  --start 2020-01-01 --end 2024-12-31 --timeframe 1d \
  --qty 100 --out results.json

# API (queued to the worker) — not wired yet
POST /api/v1/backtests
```

Bars come from the database, not the vendor: a backtest has to be reproducible,
and re-fetching means today's answer can differ from yesterday's because the
vendor restated something. Run `scripts/backfill_bars.py` first — the CLI names
the exact command if the range is empty.

`--qty` is a placeholder. It sizes every entry at the same share count, so the
reported return is a property of that number as much as of the strategy; real
sizing is risk-based (docs/RISK.md 'Position sizing'). Until the rule chain
exists, no pre-trade check refuses anything either — orders are routed through
`RiskEngine`, but it is holding an empty chain. The CLI says both on every run.

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

## Before believing a result

- [ ] Ran with realistic costs
- [ ] ≥ 2 years, including a drawdown period
- [ ] ≥ 100 trades
- [ ] Walk-forward, not one in-sample fit
- [ ] Trial count known and disclosed
- [ ] Individual trades inspected — no impossible fills
- [ ] Equity curve is not one lucky trade with noise around it
- [ ] Results survive ±20% parameter perturbation
- [ ] Data checked for gaps and unadjusted splits

## Then paper trade it

A passed backtest earns a paper deployment, not a live one. Four weeks minimum,
then compare with `GET /api/v1/analytics/live-vs-backtest/{id}`. Divergence
there is the cheapest lesson this platform can teach you.
