# 18. A curve point is labelled by its bar, not by the clock

**Status:** Accepted · 2026-08-26

## Context

`BacktestEngine.run` stamped each equity point with `clock.now()`, and the
clock is set to `ts + step` at the top of every iteration, where `step` is the
timeframe's wall-clock duration. The comment there is right about its own
intent:

> The clock stands at the bar's CLOSE: `Bar.ts` is its open, and a decision
> taken on a completed bar is taken once it has ended.

That is exactly correct for the clock. It is what stops the engine stamping an
order at an instant before the information that produced it existed, which is
the lookahead `CLAUDE.md` §5 and `docs/BACKTESTING.md` are about.

It is wrong as a *label*. For an intraday bar `ts + step` is the real close. For
a **daily** bar it is not: a daily bar is stamped at exchange-local midnight, so
`ts + 24h` is the next midnight rather than the session close at 21:00 UTC — a
different calendar day. Friday's session was recorded at Saturday's date, and
Monday never appeared at all.

A weekday histogram of a real 1,525-point curve over 2020-07-27 → 2026-08-21
had **304 Saturdays and zero Mondays**. Shifting every point back one day gave
a clean Mon–Fri NYSE calendar: no weekends, holidays correctly absent, Mondays
the sparsest weekday exactly as US market holidays predict. A 40-bar synthetic
series reproduces it in miniature — eight Saturdays, no Mondays.

No metric was ever wrong. Each session still mapped to a distinct date, so the
return series was the right set of returns and Sharpe, volatility, drawdown and
CAGR were unaffected; `_duration_days` is the only metric that reads a
timestamp at all and it takes a *difference*, which a uniform shift leaves
alone. The defect was entirely in the labels, and pervasive there: every
exported curve carried dates the market was not open on, and joining a backtest
curve to a live equity series or a benchmark **by date** was silently off by one
for every point.

The engine already held the other convention thirteen lines away. The session
anchor keys on `ts.date()` — the true session date — and its comment justifies
that by noting a daily bar "is stamped at exchange-local midnight, so neither
straddles a UTC day boundary". The curve's own timestamps were the thing that
did straddle it.

## Decision

**An equity-curve point is stamped with its bar's `ts`. The clock is not
moved.**

`Bar.ts` is already this platform's identity for a bar: the timeline is built
from it, the per-symbol index is keyed on it, and the session anchor reads its
date. A curve point names the session whose equity it reports, so it takes that
same name. A benchmark series is labelled the same way, which is what makes
joining one to the other by date land.

The clock keeps standing at `ts + step`. It answers a different question — the
earliest instant a decision on this bar could be taken — and it is what stamps
orders and fills. Moving it to `ts` to fix a label would have stamped every
order at its bar's *open*, which is the lookahead bug this engine exists to
avoid.

`Bar.close_ts` computes the same `ts + timeframe.seconds` and keeps it. It is
the clock's question, not the curve's, and for a daily bar it is an upper bound
on the session close rather than the close itself — safe for "the earliest a
strategy may act", wrong as a label. Its docstring now says so, because a
reader who took it as exact would reintroduce this bug somewhere else.

Stored curves are migrated (`c5e9a03b1f47`): every point in
`backtest_runs.equity_curve` moves back by its own run's timeframe. The shift
is a deterministic function of data each row already carries — `config.timeframe`
— so unlike the `warnings` backfill this is a correction that can be checked
rather than a value that has to be invented.

## Consequences

- Curve dates are session dates. A chart drawn from an export has no weekend on
  its axis, and a date named in one is a date the market was open.
- A backtest curve, a live equity series and a benchmark all key on the same
  thing, so a date join is a date join.
- Old rows and new rows are in one convention, so a chart comparing two runs is
  comparing two runs. Leaving them mixed was the alternative that would have
  made the defect permanent and invisible.
- No stored metric changes, so no run's reported performance moves. A migration
  that alters figures a human has already read would be a different and much
  larger decision; this one alters none.
- Anything that wants the instant a bar's decision could be taken still asks the
  clock, and the two questions now have two answers instead of one.

## Alternatives considered

**Make `step` and `Bar.close_ts` exchange-aware**, resolving a daily bar to its
real 21:00 UTC close. Rejected on the dependency rule: a `Bar` carries no
exchange, so it cannot resolve its own session close, and `domain` imports
nothing from its siblings — a calendar there would be the first exception. It
would also make reading a stored curve require a calendar, and it fixes the
label by making the clock's answer more precise in a place where precision was
never the problem. The label wanted the session's *name*, which `ts` already is.

**Leave stored curves alone and mark the convention per row.** Cheaper, and it
leaves the table holding two conventions for as long as the old rows exist,
with every reader obliged to know which is which. The correction is mechanical
and checkable here, which is what makes migrating the better trade.

**Leave it.** It was parked once already, on the grounds that nothing it touches
is numerically wrong and it was found alongside two defects that were (#103,
#104). That reasoning has expired: both of those have landed, and what is left
is a defect that reads as a rendering quirk right up until somebody lines two
series up and cannot work out why they disagree.
