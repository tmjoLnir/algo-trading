# 34. The daily decision is taken at the open, on the previous session's bar

**Status:** Accepted · 2026-09-22

Widens the guard [ADR 0033](0033-the-mac-stays-and-the-condition-becomes-a-number.md)'s
paper week ran into and that PR #163 installed: a worker could be configured for
`1d` and would then trade nothing, silently. That guard's own docstring said the
diff that writes daily bars is the one allowed to widen it. This is that diff.

It does not reopen [ADR 0017](0017-backtests-price-off-adjusted-closes.md) or
[ADR 0023](0023-worker-configuration-is-a-row-the-dashboard-writes.md). The
timeframe is still a row the dashboard writes; what changes is that one more
value of it now works.

## Context

**The live runner could not run a daily strategy at all, and said nothing about
it.** `strategy.on_bar` is reachable from exactly one place — `_poll_strategy`,
fed by `_refresh_bars`, which yields a bar that *newly closed since the last
pass*. A daily bar closes when the session ends. `warmup` loads the newest stored
bar at every session open, so by the time the first evaluation runs there is
nothing left to close: `bar.ts <= held[-1].ts` on every pass, for ever. The
strategy is never asked, the worker reports healthy, and the session looks
exactly like one in which a crossover strategy found no crossing.

Nothing could have supplied the missing bar either. `MarketDataFeed.subscribe`
takes no timeframe — Alpaca's websocket carries minute bars and nothing else —
and the adapter stamps every streamed bar `STREAMED_BAR_TIMEFRAME`. Every writer
of a daily bar in the tree is a REST or batch path, and none of them runs during
a session.

**This mattered because the platform could only run the configuration its own
backtest says is worthless.** `docs/paper-week/f8-timeframe-and-stop-sizing.md`
measured 26 configurations over 346,643 real bars: every intraday cell loses
money, every daily cell makes money, and the shipped `sma_crossover` 20/50 ×2 at
`1m` returns −58.23% with a 0.86% win rate. Four sessions of the paper week have
been run on it and none of them could mean anything about the strategy.

Three earlier documents told an operator to fix this by setting the timeframe to
`1d` — `docs/paper-week/day-5-readiness.md` §3.2 among them, until it was
corrected. That edit would have produced a session with **zero** evaluations,
which is worse than `1m`, because `1m` at least trades.

## Decision

**A daily worker takes its decision at the session open, on the previous
session's closed bar, and the resulting order fills during today's session.**

That is `BacktestEngine.run` exactly. For each bar the backtest sets the clock to
the bar's close, marks, checks stops, fills pending orders, calls `on_bar`, and
turns any signal into an order that fills on the *next* bar. Decide on the close
of bar N, fill at bar N+1. For a daily series that is: decide on yesterday's
close, fill today.

Four mechanisms implement it, and each exists because leaving it out reintroduces
a failure this platform has already had.

1. **`TradingCalendar.previous_session` names which bar a decision is about.**
   The last session to have *closed* at or before an instant — at or before,
   because a session's close is the moment its bar becomes complete. Naming it by
   session rather than by `now - 1 day` is the whole point: subtracting a day
   lands on a Sunday one week in five.

2. **A session-open warmup stops one bar short.** It fetches one row more than it
   keeps and withholds the newest, so the existing `_refresh_bars` trigger closes
   it into the loop on the first evaluation. No second call site for `on_bar`, no
   second evaluation path to reason about.

   The arithmetic is the part that has to be right. `_poll_strategy` admits a
   signal at `have > warm_after`, measured *after* the bar is appended, so the
   window must hold `warmup_bars` bars **before** the decision bar arrives. Load
   one too few and the daily runner asks the strategy every session, gets a real
   signal, and discards every one of them as cold — a healthy-looking zero-trade
   session reached by a different route.

3. **A bar whose own session has not closed is refused, on both sides.** Alpaca
   publishes a partial daily bar for the session in progress, and
   `apply_corporate_actions` fetches with `end = now` an hour before the open and
   upserts whatever comes back. So the hazard is reachable rather than
   theoretical, and deciding on such a bar is lookahead — the one error class
   `CLAUDE.md` §5 says invents a profitable strategy that loses money in
   production. `refresh_session_bars` bounds its fetch at the previous session's
   close; `warmup` and `_refresh_bars` drop anything dated later. Neither side is
   the only thing standing between the platform and a bar that has not happened.

4. **A session-series signal is stamped from its bar, not from the wall clock.**
   `Signal.ts` becomes `OrderRequest.decided_at` and from there part of
   `client_order_id`, which is rule §1.4's whole mechanism. A strategy stamps
   `ctx.now`; in a backtest that *is* the bar's close, so the key is a function of
   the bar. Live it is wall-clock. On an intraday series that is nearly harmless,
   because the same bar does not close twice. **On a daily series it is a
   duplicate position**: a worker restarted mid-session warms up again, withholds
   the same bar and decides on it again — correctly, having no memory that it
   already did. Stamping from the bar makes the second decision the *same*
   decision, and the venue returns the order it already holds.

**And a dedicated job writes the bar.** `refresh_session_bars` runs at open−30
with a second attempt at open−15, over the runner's own watchlist and series.

The two jobs either side of it sweep `stored_series()` — whatever the bar store
already holds — which is right for their purposes and can never bootstrap a
symbol with no daily history. That hole is why `scripts/backfill_bars.py` has
been the only way a daily series came into being. Relying on
`apply_corporate_actions` instead would have been worse than the hole: it is a
corporate-actions sweep, its failure is logged and retried tomorrow, and
tomorrow is too late for a decision taken this morning. Coupling the trading path
to a job whose purpose is something else is the specific failure this codebase
keeps finding in itself.

## Consequences

**`1d` joins `1m` as a deliverable timeframe, and nothing else does.**
`DELIVERABLE_TIMEFRAMES` is the pair, and `5m` through `4h` are still refused at
assembly and at preflight, because nothing aggregates them either. Widening it
again means writing the writer first.

**A daily session has one decision in it.** That is the intent, and it has a cost
worth stating plainly: `_check_stops` is fed by the same `closed` list, so
engine-side stop checks, trailing ratchets and time exits are evaluated **once
per session** on a daily worker. The venue-side GTC stop is unaffected and
remains the protection that matters — `docs/SAFETY.md` layer 5 — but a strategy
relying on an engine-side trailing stop should not be run at `1d` until the
ratchet has somewhere else to live.

**The absence of a decision is loud, per symbol, in two places.**
`refresh_session_bars` alerts before the open with the backfill command ready to
run, and `warmup` alerts again if the bar still is not there. Both are `WARNING`
rather than `CRITICAL`: no position is endangered — venue stops are live — but
the day's decision is, and "produced nothing" is indistinguishable from a correct
quiet session unless somebody says so.

`warmup`'s announcement runs *after* reconciliation, so a runner about to
quarantine on a book mismatch does not also page about which bar it would have
decided on. The alert budget has been exhausted on the wrong message twice
already (`docs/paper-week/day-4-review.md`, F1).

**Nothing changes for an intraday worker.** The session bound, the withhold and
the stamp are all behind one switch — `_decision_session`, set only by a
session-open warmup on a session series — and a `1m` runner takes none of them.
That is asserted rather than assumed.

**The paper week can now run the strategy it was always configured to describe**,
at one decision per symbol per day. F8's caveat stands and is worth repeating:
one daily session is one bar per symbol, so a week of `1d` is five decisions per
symbol, not two thousand. This does not make a week conclusive. It makes a week
worth recording.

## Alternatives considered

**Leave warmup alone and add an explicit session-open evaluation step.** A named
`_decide_on_previous_session_bar()` reads more honestly than logic spread across
`warmup` and `_refresh_bars`, and that criticism of the chosen design is fair.
It was rejected on the arithmetic: deciding on a bar already inside the warmup
window adds nothing to `have`, so `51 > 51` is false and every daily signal is
discarded as cold, every session, for the life of the process — a runner that
asks the strategy, gets an answer, and can never act on it. Fixing that means
loading an extra bar anyway, at which point the "warmup is untouched" argument
has gone and a second `on_bar` call site remains.

**Make the live loop consume a timeline, as the backtest does.** The deepest
framing, and the one that would stop this class of bug recurring: a bar becomes
decidable when its close has passed, regardless of how it arrived. It is also a
rewrite of the evaluation loop, on a platform that has not yet completed one
measured session. Recorded here as the direction rather than dismissed — if a
third timeframe ever needs support, this is the change to make instead of a
third special case.

**Depend on `apply_corporate_actions` for the bar.** It already re-fetches and
upserts a seven-day window for every stored series at open−60, so for an
already-bootstrapped symbol the bar arrives without any new job. Rejected above:
wrong scope, wrong failure handling, wrong purpose.

**Set the live timeframe to `1d` and fix nothing.** This is what three documents
in this repository recommended, and what the operator would have done. It is
recorded here because the reason it fails is not obvious from any one file: the
runner, the feed adapter and the scheduler each behave correctly on their own,
and the silence only exists between them.
