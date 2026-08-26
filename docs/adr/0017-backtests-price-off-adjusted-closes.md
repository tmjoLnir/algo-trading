# 17. Backtests price off adjusted closes, and refuse to run without them

**Status:** Accepted · 2026-08-26

## Context

`CLAUDE.md` §5 has said since the first commit that splits and dividends change
historical prices, that both a raw and an adjusted close are stored, and that a
backtest runs on adjusted while an order is placed at raw. `docs/DATA.md` says
it. `Bar`'s own docstring says it. The Alpaca provider does its half correctly:
`get_bars(adjusted=True)` costs two passes over the window precisely so that
`close` can hold the price as traded and `adj_close` the adjusted one, and
`BarRepository.upsert_bars` COALESCEs the column so a later raw-only refetch
cannot erase it.

Nothing read it. `BacktestEngine` marked positions, filled orders, sized them,
derived stops and computed indicators from `Bar.close` — the raw price — and a
grep for `adj_close` across `atp_core` returned the provider, the persistence
layer and the domain model, none of them a consumer.

The symptom is not subtle once you look for it, and invisible until you do. A
`buy_and_hold` run over twenty large-cap symbols from 2020-07-27 to 2026-08-21
reported a total return of 331.7% and a CAGR of 27.4% against roughly 14% for
the index over the same window. Its equity curve contains a single **+51.16%**
bar — twenty-eight standard deviations of that run's own daily volatility, and
four times the largest genuine move in six years. The date is 2021-08-02, when
GE began trading on a split-adjusted basis after a **1:8 reverse split**. The
price octupled overnight; the held share count did not divide. The engine booked
it as an 8x gain and reported it as performance.

Every corporate action in that universe is in the curve on its own date, in both
directions: AAPL's 4:1 as −5.26%, GOOGL's 20:1 as −4.17%, GE's two spinoffs as
−6.50% and −11.82%. Neutralising just the reverse-split bar takes the run from
331.7% to 185.6%. That number is not the answer either, because from August 2021
the GE position is carried at eight times its true weight for the remaining five
years, so every subsequent day's return is mis-weighted. There is no patching a
curve like this; it has to be re-run.

## Decision

**The backtest engine converts every bar into adjusted space at the top of
`run()`, and refuses the run outright when a bar has no `adj_close`.**

### The whole candle moves, not just the close

`Bar.adjusted()` scales open, high, low and close by the single factor
`adj_close / close`, and divides volume by it.

The alternative — swapping `close` for `adj_close` at each read site — is what
the bug report first suggests and it is worse than doing nothing. The engine
reads six price fields across marking, stop resolution, fill matching, sizing
and the indicator window. Converting five of them leaves a run that marks
positions at adjusted closes and fills them at raw opens: two numbers in
different currencies, differing by the split factor, in an expression that
subtracts one from the other. A conversion applied at the boundary cannot be
partially applied.

Volume moves the other way because a split changes the share count as well as
the price: 4:1 quarters the price and quadruples the shares. Dividing by the
same factor holds the bar's traded notional fixed, which matters because the
engine caps fills at `max_volume_participation` of bar volume — a participation
limit measured in pre-split shares against post-split prices is wrong by exactly
the split ratio.

The conversion is idempotent, marked by `adj_close == close` on the result. A
symbol with no corporate action already satisfies that and is returned
unchanged, which is the ordinary case for most symbols in most windows.

### A missing adjusted close refuses the run

A raw-only backfill (`--raw-only`) leaves the column unset. The engine raises
`UnadjustedDataError` naming the symbols and the backfill that fixes them,
rather than falling back to the raw close for those series.

Falling back is the more helpful-looking option and it is the one that caused
this. It completes. It reports. The only trace is a number in an equity curve
that looks like a very good day. `_refuse_holes` already takes this position
about a gap in stored history, for the same reason and with the same shape of
message, and this is the same class of defect: input that produces a plausible
but wrong result rather than an obviously broken one.

One unadjusted symbol refuses the whole run rather than its own series. A
twenty-symbol result that is correct about nineteen of them is not distinguished
in any output the platform produces from one that is correct about all twenty.

### Live still trades raw

Only `BacktestEngine.run` converts. `OrderRouter`, the broker adapters and
`SimulatedBroker` continue to see raw prices, because an order is placed into a
book quoted in prices as traded — which is the second half of the §5 rule and
not a compromise in it.

This does mean the engine and `SimulatedBroker` would fill at different prices
if driven over a window containing a corporate action, which is a narrowing of
ADR 0006's "one fill rule, two callers". The rule is still one function; the
inputs are deliberately in different spaces. It does not bite in practice: the
simulator is driven with recent bars, where no split has yet happened and raw
and adjusted are the same number. `TestAgreementWithTheBacktestEngine` pins the
agreement on exactly that kind of series.

## Consequences

- Backtest results over any window containing a corporate action change, and
  every such result produced before this commit is wrong. The reverse-split
  case overstates; the ordinary forward-split case understates.
- Indicators computed inside a backtest are now continuous across splits. An
  SMA(200) spanning a 4:1 split was previously averaging two price regimes.
- A raw-only backfill can no longer be backtested at all. This is intended, and
  the error names the re-fetch.
- `--raw-only` remains correct for its actual purpose — the realtime path, which
  fills gaps at raw prices and does not backtest.
- **The live warmup has the same defect and is not fixed here.**
  `StrategyRunner` builds its indicator window from `b.close` on stored bars, so
  a live SMA(200) spanning a split is computed across the discontinuity for the
  length of its lookback. It is a smaller and slower-moving bug than this one —
  it decays out of the window rather than compounding into a P&L figure — and
  fixing it means deciding how a live loop holds two price spaces at once, which
  is a different decision from this one and deserves its own ADR.

## Alternatives considered

**A `use_adjusted` flag on `BacktestConfig`, defaulting to true.** Rejected. The
flag's only honest use is reproducing a known-wrong historical run, and
`CLAUDE.md` §1 is explicit that a guardrail is not to be widened to make
something pass. A switch that turns this off would be reached for the first time
by whoever hits the refusal in a hurry.

**Falling back to `close` with a warning on the result.** Rejected. The run that
produced this ADR already carried warnings the reader did not act on, and a
result with twenty per-order refusals in it still reported `total_return: 0.0`
in a field that reads like a measurement. A warning is the mechanism that
already failed here.

**Adjusting during backfill, storing only adjusted prices.** Rejected. It
discards the raw price, which live order placement needs, and it is wrong on
its own terms: adjustment factors change whenever a *new* corporate action
happens, so a value adjusted at write time is stale from the next split onward.
Storing both and converting at read time is what makes a re-fetch enough to
correct history.
