# 26. The paper week runs on IEX, and the week is scoped to what IEX can prove

**Status:** Accepted · 2026-09-05

## Context

`ALPACA_DATA_FEED` has always been `iex`, and it has always been a default
nobody chose. Day 1 of the paper week measured what that default actually
delivers (docs/paper-week/day-1-review.md, F13):

```
RTH coverage        6,830 of 7,800 expected bars   (87.6%)

bars in the minute    1   14   15   16   17    18    19   20
minutes               2    5   18   43   70   106    93   46
```

Median 18 symbols of 20 per minute. **Only 46 of 390 RTH minutes carried a bar
for every name on the watchlist.** The single run of zero-bar minutes — 18:45
through 18:51 — was the venue outage F5 and F6 describe; every other shortfall
is a different thing entirely, and the difference is the whole of this decision.

**IEX is one exchange, not the tape.** It runs a low single-digit percentage of
US equity volume. Alpaca's `iex` feed reports trades and quotes that printed *on
IEX*, so a symbol that traded elsewhere in a given minute — which, for most
symbols in most minutes, is where it traded — yields no bar at all. The 12.4%
that is missing is not loss, packet drop, or a bug in the ingestor. It is the
feed answering the question it was asked.

That is not a defect to fix, and treating it as one would be the expensive
mistake here: an engineer chasing "missing" bars on IEX is chasing the design of
the product. But it is also not nothing, because it silently changes what a week
of paper trading is evidence *of*.

`sip` is the alternative: the consolidated tape, every venue, effectively
complete. It requires a paid Alpaca market-data subscription, and it is one
setting away.

## Decision

**The paper week runs on `iex`, deliberately, and the week's conclusions are
scoped to what an incomplete tape can support.**

Concretely, what this week may and may not conclude:

**It can prove the platform.** Does the strategy loop evaluate every bar. Does a
signal become a sized order that passes the risk chain. Does a fill book
correctly and get a stop attached. Does reconciliation agree with the venue. Do
the halts fire, alert, and clear. Does the platform survive a venue outage, a
restart and a weekend. **None of these depend on the tape being complete** —
they depend on the platform doing the right thing with whatever arrives, and a
sparse feed exercises that more honestly than a dense one.

**It cannot prove the strategy.** Any number that depends on seeing every print
is not measured this week:

- **Signal timing.** `sma_crossover` closes on the bar it is handed. On IEX a
  symbol's "1-minute close" is its last IEX print in that minute, which may be
  stale by most of the minute, and the minute may be absent entirely. A crossover
  can therefore be detected late, early, or not at all.
- **Fill quality and slippage.** Alpaca paper fills against the same feed, so a
  measured slippage is slippage against IEX, not against the market.
- **Anything comparative.** A backtest run on adjusted daily bars from the full
  tape and a paper week run on sparse IEX minutes are not the same experiment.
  docs/BACKTESTING.md's warning about comparing a backtest to a live run applies
  with extra force here, and the difference is the data source rather than the
  code.

**The 87.6% is recorded as the baseline, not as an incident.** A future week
that reports the same number is behaving normally. A future week that reports a
markedly *lower* number has something wrong with it, and now has something to be
lower than.

## Consequences

- `ALPACA_DATA_FEED=iex` stays the default, and is now a decision with a
  document behind it rather than an unexamined default.
- **The paper week's own report must not present per-signal or per-fill quality
  as a finding.** It is measuring the platform. Saying so once, here, is cheaper
  than re-litigating it every time a number looks surprising.
- Switching to `sip` is one environment variable and a paid subscription. It
  should be a deliberate move made *between* evaluation periods and never during
  one: the two feeds produce different bars for the same minute, so a week
  spanning a switch is two experiments reported as one.
- Sparse minutes are ordinary, so nothing in the ingest path should treat an
  empty window as an error. `data.stream.backfill_empty` already logs a symbol
  with no bars in a gap at INFO and says it cannot tell the two apart — that
  restraint is correct and this ADR is why.
- Gap detection has to keep measuring against *the feed's own* expectation.
  A gap sweep that assumed a bar per symbol per minute would report tens of
  thousands of phantom gaps on IEX and bury the real ones.

## Alternatives considered

**Move to SIP for the paper week.** It would make the week measure the strategy
as well as the platform, which is more. Rejected for now on sequencing rather
than on cost: the platform has not yet completed a clean week, day 1 was void
for reasons that had nothing to do with the feed, and changing two variables at
once — the fixes in #135 through #137, and the data source — would leave any
surprising result with two candidate explanations. Move to SIP when the platform
is boring, and treat that as the start of a new evaluation period.

**Reduce the watchlist to liquid names to raise coverage.** Twenty large-cap
names already are the liquid case; the sparseness is IEX's share of volume, not
the symbols' liquidity. It would raise the percentage slightly and change
nothing about what the week proves, while making the universe less
representative and inviting exactly the survivorship-flavoured reasoning
docs/BACKTESTING.md warns about.

**Treat missing minutes as gaps and backfill them from REST.** The historical
endpoint would answer with consolidated data, so this would silently splice two
different data sources into one series — dense where the sweep ran, sparse where
it did not. Every indicator computed across the seam would be reading a
discontinuity that no log records. This is the worst of the options and is the
one most likely to be reached for by accident, which is why it is written down.
