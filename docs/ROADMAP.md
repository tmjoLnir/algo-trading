# Roadmap

Build order matters: each phase produces something verifiable, and the risk
layer lands before anything can place an order.

## How this file is maintained

This is the only record of what is built. It is updated by the PR that does the
work, in the same diff — see `CLAUDE.md` §6. Conventions:

| Line | Means |
|---|---|
| `- [ ] item` | Not started, unclaimed |
| `- [ ] item — @who (wip #12)` | Claimed and in progress, so nobody duplicates it |
| `- [x] item — @who (#12)` | Done, demonstrated, and merged in that PR |

A box is ticked only when the phase's *Verifiable:* line has actually been shown
— not when the code compiles, and not when the tests pass in isolation. If an
item turns out to be wrong, in the wrong phase, or ticked when it should not be,
fix it here in the PR that discovered it.

## Phase 0 — Foundations (skeleton is here)
- [x] Repo structure, tooling, docs, CI (#1, #2)
- [x] `make install` and `make up` work end to end — @claude (#7, #35).
  Demonstrated by the `stack` CI job, which runs both on a clean checkout.
  `make up` starts db, redis, api, web **and the worker**: the compose profile
  that held it back is gone, because the condition stated here for removing it —
  that its entry point stop raising `NotImplementedError` — is met (#35). On a
  clean checkout the worker comes up in backtest mode with no watchlist, reports
  that it is ingesting nothing, and runs its schedule; the `stack` job's
  "none is restarting" step now covers it without needing a new check.
- [x] Alembic initial migration; TimescaleDB hypertable created — @claude (#6)
- [x] `Position.apply_fill` + `Order.apply_fill` implemented and property-tested — @claude (#4)

**Do these two first.** Everything downstream computes P&L from them; a bug here
is invisible and poisons every number the platform produces.

## Phase 1 — Data (requirement #4)

**The *Verifiable:* line has been shown.** With egress open to
`data.alpaca.markets` and credentials supplied,
`scripts/backfill_bars.py --symbols SPY --start 2021-08-17 --verify` ran against
the real Alpaca feed into a real TimescaleDB hypertable and exited 0:

```
1,254 bars written for 1 symbol(s) across 1 window(s) in 1 request(s).
Verified against the NYSE calendar: every session in 2021-08-17 → 2026-08-17
has a bar for all 1 symbol(s).
```

`find_gaps` — SQL read included this time, not just the pure scan — reported
`expected=1254 missing=0 windows=0`. In the database: 1,254 rows across 262
hypertable chunks, `adj_close` populated on every one, every price and quantity
column `numeric`. Re-running left the row count at 1,254, so the idempotence the
backfiller claims is now observed rather than asserted. The full integration
suite (52 tests) passes against that hypertable and a real Redis.

Three things previously pinned only by tests are also now seen on live data:

- Daily bars **are** stamped 00:00 New York — 05:00Z in winter, 04:00Z in
  summer — so the attribution rule in `atp_core.data.gaps` holds across DST.
- Pagination **does** cross real `next_page_token` boundaries: 22,727 one-minute
  bars over 3 pages, ascending and duplicate-free across both seams. The daily
  backfill fits in one page, so it never exercises this on its own.
- The calendar handles a one-off closure the vendor also skips (2025-01-09, the
  national day of mourning), so the clean scan is not passing vacuously.

The last two items stay unticked. This *Verifiable:* line is entirely a
historical-data statement — it never opens a socket and never reads a quote — so
it cannot demonstrate them, which is a gap in the roadmap rather than in them.
A line they can be ticked against is proposed below.

- [x] Alpaca historical provider with pagination — @claude (#23).
  `AlpacaHistoricalProvider`, unit-tested against recorded responses and now
  exercised against the live feed including multi-page pagination.
- [x] `BarRepository` + backfill script — @claude (#23).
  `PostgresBarRepository` (#9) and `scripts/backfill_bars.py` over
  `atp_core.data.backfill`, run end to end against the live feed into the
  hypertable: windowing, batching, rate limiting, empty-window detection, and
  an upsert that re-runs clean.
- [x] Gap detection, calendar-aware — @claude (#15, #16, #23).
  `TradingCalendar` over `pandas_market_calendars` (sessions, holidays, early
  closes, `next_open`, `minutes_to_close`), `find_gaps` on top of it, and
  `--verify` (#15). The nightly sweep that consumes it — `backfill_missing_bars`
  over every stored series for the last 7 days, re-checking and naming what it
  could not fill — landed in #16. Verified end to end against five years of
  stored SPY dailies. `1h`/`4h` gap detection stays deliberately refused rather
  than guessed (docs/DATA.md 'Gaps').
- [ ] Real-time WS ingestor, reconnect + gap backfill — @claude (wip).
  Both halves are built and unit-tested against scripted fakes.
  `AlpacaRealtimeFeed` owns the socket — auth handshake, subscription replay,
  exponential backoff with jitter, and a refusal to loop on an error another
  connection would not fix (bad credentials, plan, or the one-connection limit).
  `StreamIngestor` owns the fan-out and the data gap: it caches quotes,
  persists bars, and on `FeedReconnected` re-fetches `[last message, last
  completed bar)` before handling anything from the new connection. A gap it
  cannot close engages the kill switch instead of trading across the hole.

  A socket has now been held open to Alpaca *during the session*, and live data
  parsed from it: 5,284 SPY quotes and a complete one-minute bar over ~80s on
  2026-08-17, plus a second bar in a follow-up capture. Every price and size
  arrived a `Decimal`, each bar's OHLC sat inside its own range, and each was
  stamped at its open. The handshake — and with it the stream entitlement, a
  separate grant from the REST API — is confirmed (`stream_connected`,
  `stream_subscribed bars=1 quotes=1`). The note that stood here said no bar or
  quote had ever been parsed from the live stream; that was true when written
  and is now not, so it is corrected in place rather than left reading as
  current.

  That run also closed a blind spot no scripted fake could have found. Every
  wire fixture in `test_alpaca_realtime_feed.py` was written from Alpaca's
  documentation, and the wire disagrees with the documentation in three ways:
  quotes and trades are stamped to the **nanosecond** — nine fractional digits,
  where the fixtures stopped at six — real prices carry sub-cent precision
  (`775.425`, and a VWAP to six decimals), and every message arrives padded
  with vendor fields the parser ignores (`bx`, `ax`, `z`, `i`, `x`). The adapter
  handles all three correctly, so no bug was found; but nothing held it to
  them. A `strptime` parser accepting an optional six-digit fraction rejects
  every real quote and trade on the feed and still passes all 648 other unit
  tests. `TestRealWireFormat` now carries the captured frames verbatim, and was
  verified by making exactly that change: the other 648 stayed green and only
  those 4 failed.

  Still unticked, and the *reason* has moved twice — recorded each time so the
  next person does not re-check what is already known. Egress is not the blocker:
  the policy permits the `wss://` upgrade to `stream.data.alpaca.markets`
  (`101 Switching Protocols`, then real frames), and a shut exchange is not the
  blocker either. Nor, as of #35, is the missing process: `atp_worker.main` now
  wires this ingestor to a real Redis and a real hypertable, supervises it, and
  halts if it dies.

  What is left is simply to run it through a disconnect and watch. The reconnect
  ladder and the `FeedReconnected` gap backfill — the half of this item that
  decides whether a dropped connection trades across a hole — are still pinned by
  tests alone, because nothing has yet dropped the socket on purpose during a
  session with the stack up.
- [ ] Redis quote cache, pub/sub publisher, staleness monitor — @claude (wip).
  All three are built. `RedisQuoteCache` (one key per symbol, `MGET` for a
  watchlist, every number stored as a string, TTL as garbage collection rather
  than as freshness) and `RedisEventPublisher` (which refuses to publish a
  float) are unit-tested against a fake and integration-tested against a real
  Redis — TTL expiry, `MGET` hole alignment and a genuine cross-connection
  publish/subscribe are behaviours of Redis rather than of Python, and a fake
  agreeing with us about them would prove nothing. `StalenessMonitor` is
  calendar-aware and measures silence from the latest of last-message,
  connect-time and session-open; it halts once per outage and never clears.
  Those tests were re-run green as part of the 52-test suite above. Unticked
  because the historical *Verifiable:* line does not exercise any of it —
  nothing in a bar backfill writes a quote to the cache or publishes an event —
  so there is no demonstration to tick against yet, only passing tests.

  All three now have a production caller: `atp_worker.main` builds the cache, the
  publisher and the watchdog and hands them to the ingestor (#35). That closes
  the "nothing constructs these" gap but not the item — a caller is not a
  demonstration, and the streaming line below is still what they need.

*Verifiable:* backfill 5 years of SPY dailies; no gaps outside holidays.
**Shown** — @claude (#23); see the run above.

*Verifiable (streaming):* hold the live stream through a forced disconnect
during market hours; every session bar lands in the hypertable, the reconnect
backfills the gap, and the latest quote is readable from Redis.
**Not yet shown** — proposed here because the two items above have no line of
their own, and were being held against a historical-data line that cannot
demonstrate them. Adjust the wording if it is not the demonstration you want.

It is no longer blocked on egress or on the clock, which is the one thing this
line's status has learned since it was written. Checked on 2026-08-17 with the
market open: the egress policy permits the WebSocket upgrade to
`stream.data.alpaca.markets`, the credentials carry the stream entitlement, and
live quotes and bars parse through `AlpacaRealtimeFeed` end to end. So access is
settled and only wiring is left. The line has four clauses — a held socket
through a *forced* disconnect, session bars in the hypertable, the reconnect
backfilling the gap, the latest quote readable from Redis — and holding a socket
is the only one demonstrated.

That wiring landed in #35: `atp_worker.main` binds the ingestor to a real Redis
and a real hypertable, so nothing structural stands in the way any more. What
this line now needs is an *act*, not a component — `make up` with a watchlist
during a session, kill the socket, and check all four clauses. Nobody has done
that yet, so it stays **not shown**.

## Phase 2 — Backtesting (requirement #2)
- [x] Indicators (`ema`, `rsi`, `atr`, …) — @claude (#24, #27).
  `ema`, `rsi`, `atr`, `bollinger`, `macd` and `stddev` are implemented, along
  with the `*_series` variants the module docstring promised the engine, plus
  `true_range` and `sma_series`. The series form is the primitive and the
  scalar is its last element: Wilder's smoothing is recursive, and two
  implementations of it are two chances to get it subtly wrong. Warmup is
  `nan` rather than a partial average, which is what stops a run opening on an
  SMA(200) computed from six bars. Conventions that reasonable implementations
  disagree on — SMA-seeded EMA, Wilder's alpha for RSI and ATR, population
  `stddev` for Bollinger — are stated at the top of `ta.py`, and each is
  cross-checked to 1e-9 against pandas' independent `ewm`/`rolling`. Ticked on
  the end-to-end line below, where `SmaCrossover`'s `ta.sma` calls drive 23
  fills over five years of real SPY dailies — the phase's first *Verifiable:*
  line runs a scripted strategy that computes nothing from prices, so it never
  could.
- [x] Backtest engine event loop, next-bar fills — @claude (#25).
  `BacktestEngine.run` walks a merged timeline over all symbols in the
  documented order (mark → stops → fills → decide → size → risk → queue for the
  next bar), and `BacktestContext` is the lookahead guarantee: a per-symbol
  cursor that every accessor slices behind, monotonic, so there is no code path
  that returns a later bar. Orders rest for one bar and fill against the next —
  market at its open, limit only if the range reached the price, stop at the
  open when the bar gapped through the trigger. Fills are capped at
  `max_volume_participation` of the bar's volume, and a DAY order that cannot
  fill expires rather than resting into an unrelated later bar. When a bar's
  range spans both stop and target the stop is taken, since the bar cannot say
  which came first.
- [x] Cost and slippage models — @claude (#26, #27).
  `PerShareCostModel.commission` and `SpreadSlippageModel.slippage` are
  implemented; `ZeroCostModel`, `CompositeCostModel` and
  `alpaca_equities_default` already wired them together. Commission is
  per-share over a minimum, with SEC Section 31 and FINRA TAF on sells only —
  Alpaca charges no commission but the regulators still do. The minimum and the
  TAF cap belong to the *order* rather than to each fill, so both are charged as
  the difference between the order's running total after a fill and before it;
  otherwise the engine's volume cap, which makes partial fills ordinary, would
  quietly make a split order cost more than a whole one. Slippage crosses half
  the spread and adds an impact term in `√(qty / volume)`, sized on what we try
  to execute rather than on what fills, and is adverse on both sides by
  construction. Ticked on the end-to-end line below, which charges them: the
  same SPY run scores +11.74% costless and +10.41% with
  `alpaca_equities_default()`, of which only $15.04 is fees — the rest is
  spread and impact inside the fill prices.
- [x] Metrics — @claude (#27).
  Every function in `backtest/metrics.py`, plus the engine wiring that makes
  them real: `BacktestResult.metrics` was a dead field until now. Sample
  standard deviation, annual `risk_free_rate`/`target` divided down by
  `periods_per_year`, and downside deviation over every period rather than only
  the losing ones — all stated at the top of the module, because reasonable
  implementations differ and a number that disagrees with the reader's own
  arithmetic reads as a bug. Ratios return `inf` where the denominator is
  legitimately zero rather than a sentinel, which per docs/BACKTESTING.md
  nearly always means too few trades rather than a perfect strategy. The three
  metrics an equity curve cannot answer — holding period, exposure, turnover —
  are supplied by the caller from what it watched happen; the engine tracks
  round trips as positions return to flat, so it needs none of the FIFO
  reconstruction the analytics layer will.
- [x] `SmaCrossover` runs end to end — @claude (#27, #28).
  `scripts/run_backtest.py` runs it from the command line: bars out of the
  hypertable, strategy resolved from the registry by name, engine, realistic
  costs, metrics, a formatted report and optional JSON. Bars come from the
  database rather than the vendor, because a backtest has to be reproducible
  and re-fetching means today's answer can differ from yesterday's over a
  restatement.

  Two caveats the CLI prints on every run rather than burying here, because a
  number a human has already read is a number they have already believed.
  Sizing is a fixed share count (`--qty`), so the return is a property of that
  share count — real sizing is risk-based and equalises risk per trade. And no
  pre-trade rule refuses anything: orders are routed through `RiskEngine`, but
  the chain is empty. Both are Phase 3, which the build order puts after this.

  `POST /api/v1/backtests` and its worker task are still stubs. They are not
  Phase 2 items and the dashboard that would consume them is Phase 5; the CLI
  is what docs/BACKTESTING.md 'Running one' documents as the way to run one,
  and it works.

*Verifiable:* a hand-computed 20-bar fixture matches the engine exactly.
**Shown** — @claude (#25). `TestAgainstKnownFixture` in
`tests/unit/test_backtest_engine.py`: 20 bars, entry signalled on bar 5 and
filled at bar 6's open (110), exit signalled on bar 12 and filled at bar 13's
open (130), for a realised 2,000 on 100 shares and a total return of 0.02 —
every number worked out by hand in the docstring. The path is deliberately not
a straight ramp: on a linear one, filling at the signal bar's close gives the
same P&L as filling at the next open, so the headline number would pass under
exactly the bug the fixture exists to catch. Here the wrong reading returns
5,000 instead of 2,000.

Separately smoke-tested over the 1,254 real SPY dailies in the hypertable with
`SmaCrossover`: 23 signals, 23 fills, every fill price equal to the next bar's
open and inside that bar's range, one equity point per bar. That is engine
mechanics only — it ran on `ZeroCostModel` and a fixed share count, so its
return is not a strategy result and must not be read as one.

*Verifiable (end to end):* `SmaCrossover` over real stored bars with
`alpaca_equities_default()` costs and a full metrics report, where the fills,
the fees and the reported metrics all reconcile against the equity curve.
**Shown** — @claude (#27). Proposed in #26 because the line above is a
hand-computed fixture on a scripted strategy and `ZeroCostModel`, which is
exactly what makes it a good test of fill timing and a useless one for
indicators, costs or metrics; those three were being held against a line none
of them could ever satisfy. `SmaCrossover(20, 50)` over the 1,254 stored SPY
dailies, with `alpaca_equities_default()`:

```
equity 100,000 → 110,411.07   total_return 0.1041   fees $15.04
sharpe 0.358   sortino 0.489   calmar 0.140   volatility 0.061
max_drawdown -14.39% over 477 days
11 round trips   win_rate 0.455   profit_factor 1.232   expectancy $358.16
exposure 65.2%   turnover 11.3×   avg hold 2,365h
```

Each number was recomputed from the run's own artefacts rather than trusted:
`total_return` against the curve's endpoints, `max_drawdown` and `sharpe`
re-derived from the equity array, one equity point per bar, fees non-zero,
win-rate consistent with the trade count. The per-trade-P&L identity is
reported as *not applicable* rather than passed, because the book is still long
SPY on the last bar — an open position means trade P&L cannot account for the
whole equity change, and asserting it anyway would be the check lying.

A Sharpe of 0.36 is the point. docs/BACKTESTING.md says anything above 3 on a
simple strategy is a bug until proven otherwise, and a 20/50 SMA crossover on
one index ETF scoring modestly is what a working engine looks like.

Costs bite as expected: the same run scores +11.74% on `ZeroCostModel`, so a
strategy evaluated without them is flattered by 1.3 points over five years on
11 round trips — and far more at any real turnover.

## Phase 3 — Risk (requirement #3)
- [x] `RiskEngine` + full rule chain — @claude (#28, #29, #32).
  All nine rules and `default_rules()` are implemented, with 35 tests covering
  both directions for each — a rule that fails to block is an obvious bug, but
  a rule that blocks something it should have allowed is the one that traps a
  losing position. The chain runner landed in #28.

  Four decisions `docs/RISK_IMPLEMENTATION_NOTES.md` said to settle *before*
  writing any rule were settled first, and that file is annotated in place:
  `Portfolio.unmarked_symbols` (an unmarked holding valued at zero made every
  percentage limit come out too small and approve what it should refuse),
  module-level `reduces_position(order, portfolio)` (a sell is an exit only if
  you are long), `DailyLossLimitRule.anchor()` for the day's starting equity,
  and `RiskDecision.shrink()` to complete a path that was specified but had no
  constructor.

  Four of the nine cannot evaluate without a halt state, a clock, a calendar or
  a feed timestamp, so `default_rules()` takes them as arguments. There is no
  no-argument version: a chain that quietly dropped them would stop enforcing
  four things without anyone deciding to. `RiskEngine(limits)` with no rules now
  raises rather than defaulting.

  Ticked against the *Verifiable:* line below, now shown. The caveat that stood
  here — **nothing routes live orders through this chain** — is closed by
  `OrderRouter` (#33): every path through it validates, and there is no way to
  reach a broker adapter around it. The backtest CLI still passes an explicit
  empty chain, and nothing calls the router in production until
  `StrategyRunner` exists, so the claim has moved one link down rather than
  being discharged.
- [ ] Position sizing, all methods — @claude (wip #30).
  All five implemented: `fixed_qty`, `fixed_notional`, `equity_pct`, `risk_pct`
  and `volatility_target`. `risk_pct` and `volatility_target` each refuse the
  input they are defined by — a stop, an instrument volatility — rather than
  defaulting it, because a default there sizes every trade as though its stop
  were somewhere it is not. Sizes round *down*: a sizing function must never
  hand back more risk than it was asked for, and docs/RISK.md's own worked
  example rounds that way too.

  `PositionSizeSpec.value` is now bounded, type-aware — 500 is an ordinary share
  count and an absurd risk fraction — with a `risk_pct` backstop set an order of
  magnitude past the documented 0.5–2%, because the mistake worth catching at
  config time is a misplaced decimal point rather than a deliberate 3%.

  docs/RISK.md's worked example reproduces exactly: $50 entry with a $48 stop
  gives 500 shares ($25,000), with a $35 stop gives 66 shares ($3,300), and both
  lose about $1,000 if stopped. Across 40 stop distances the loss-if-stopped
  never exceeds the target and is never more than one share short of it.

  Unticked because Phase 3 still has no *Verifiable:* line that covers sizing —
  the one proposed in #29 is about the rule chain. Rather than propose a second
  line and tick against it in the same PR, this is left for whoever reviews it:
  the demonstration above is the obvious candidate.

  It now has a production caller: `OrderRouter.submit_signal` sizes through it
  (#33), and `test_order_router.py` reproduces the docs/RISK.md worked example
  end to end rather than against `position_size` alone. Still unticked — a
  caller is not a demonstration, and the missing line is the reason.
- [ ] `StopManager` — fixed, ATR, trailing, chandelier, time — @claude (wip #31).
  All six `StopType`s, including `fixed_amount`, which was in the enum but
  missing from docs/RISK.md's table — the row is added rather than the member
  dropped, because it is a real stop type. 41 tests, every level checked long
  *and* short: a sign error there is silent and either stops the position out
  on the bar it opens or never at all.

  The monotonicity invariant is a hypothesis property, as docs/TESTING.md asks:
  over any price path of up to 40 bars, in any order, a long's trailing stop
  never decreases. `update_trailing` returns None rather than a level when
  nothing ratchets, so a caller cannot assign a widened stop by accident, and
  it tracks the bar's *extreme* rather than its close — a spike that closed
  back down still locked in its gain.

  `should_trigger` compares against the low for a long and the high for a
  short. A bar that dipped through the stop and recovered did hit it, and using
  the close would inflate every backtest that uses stops.

  Two refusals worth naming. A `time` stop is refused a price level rather than
  given a made-up one — it is not a level, so it lives in `time_exit_due`. And
  a take-profit that is not a fixed distance from entry raises instead of
  returning None, because a take-profit that quietly does not exist is a
  position with no upside exit.

  `OrderRouter.submit_protective_orders` is the first caller (#33): it derives
  the level from a `StopConfig` against the entry's *actual* average fill price,
  arms it on the position, and sends a broker-side stop. So "nothing arms these"
  no longer holds.

  Unticked all the same, and for a sharper reason than before: broker-side stops
  are docs/SAFETY.md's layer 5, and a layer is only demonstrated by watching it
  hold. Nothing has yet placed one of these against a real venue, and four of
  the nine rules can refuse a protective stop — which the router reports rather
  than hides, but which no *Verifiable:* line yet exercises. Phase 4's paper
  week is the demonstration.
- [x] Redis kill switch — @claude (#32).
  `RedisKillSwitch` over the three halt scopes, tested against a real Redis as
  well as in memory. Ticked on docs/SAFETY.md's own go-live checklist rather
  than on anything proposed here: *"Kill switch tested end to end — engage it
  and confirm orders are actually refused"* — engaged, the chain refuses and
  names `kill_switch`; cleared, the same order passes.

  **It fails closed.** docs/SAFETY.md is explicit that layer 6 fails "Redis
  unreachable — fail closed", so an unreachable Redis reports engaged and
  trading stops. Shown against a genuinely dead port, not a fake that raises.
  The reasoning is one-sided: a false halt costs missed opportunity, a false
  clear trades the account through whatever broke Redis.

  Engaging is idempotent and keeps the *original* record — a halt that
  re-stamps itself erases the only evidence of when trading actually stopped.
  Clearing refuses an empty `cleared_by`, because "who decided it was safe to
  trade again" is the question anyone asks afterwards.

  Takes a client rather than the stub's `redis_url`, matching `RedisQuoteCache`
  — core does not open sockets on its own behalf (CLAUDE.md §1.3). Synchronous,
  unlike the quote cache, because the risk chain is.

  Still open, and not this item: most of the documented auto-engage triggers
  still never call `engage()`. Each belongs to the subsystem that detects it —
  `DATA_FEED_LOST` is wired from the stream consumer and `BROKER_UNREACHABLE`
  from the order router (#33), leaving the daily loss limit, reconciliation
  mismatch, rate-limit storm and repeated unhandled exceptions to the runner and
  the reconciler. `RISK.md`'s `flatten_all_positions` remains a stub,
  deliberately separate because halting stops new risk while flattening realises
  P&L into a market you may not be able to see.

**Before any live order path exists.** Not after.

*Verifiable (proposed):* a strategy that tries to breach every limit is refused
by the *right* rule each time — the reason a human reads names the most
fundamental breach, not merely the first one checked — and no configuration of
the chain can refuse an exit.
**Shown** — @claude (#32). Proposed in #29 because this phase stated no line at
all, which is how three Phase 2 items ended up held against a line none of them
could satisfy. *Proposed and ticked by the same hand, so it is worth a
reviewer's eye rather than mine.*

`test_each_limit_is_refused_by_its_own_rule` breaches each limit in isolation
and asserts the rejection names the rule that owns it — a rejection blamed on
the wrong rule sends whoever reads it to the wrong config. The kill-switch half
runs against a real Redis in `tests/integration/test_kill_switch.py`.

The second clause is the one worth failing a build over, and
`test_no_configuration_of_the_chain_can_refuse_an_exit` states it directly:
against a book breaching every limit at once — down 40% on the day, no cash, an
oversized position — the order that closes it still passes, while an entry
against that same book is refused.

That work turned up something worth recording. At the default 100% gross cap, a
long-only book's equity is its cash plus its positions, so the headroom under
the gross cap *is* the cash — `BuyingPowerRule` and `MaxExposureRule` bind
identically and gross always answers first. Buying power only becomes the
operative limit under margin or with shorts. Pinned by a test, because it looks
like a gap and is not.

I earlier wrote that this line needed a live order path. On reflection that
conflated two claims: the line describes the chain refusing correctly, which is
shown, whereas wiring it to real orders is Phase 4 and is tracked on the item
above.

## Phase 4 — Execution & paper trading (requirements #1, #5)
- [ ] `BrokerPort` + Alpaca adapter (paper first)
- [ ] `OrderRouter`, order state machine — @claude (wip #33).
  Both halves are implemented, and Phase 3's standing caveat — "nothing routes
  live orders through this chain" — is closed at the router: `RiskEngine
  .validate()` gates every path through it (entries, exits, protective stops,
  flattens) and there is no way to reach a broker adapter around it.
  `StopManager` and `RedisKillSwitch` gained their first callers outside tests
  in the same change.

  The state machine gained the piece it was missing: `state.transition()`, which
  moves an order *through* the table. `TRANSITIONS` was already correct and is
  unchanged — but nothing consulted it, every status in the repo being set by
  plain assignment, which is precisely how a replayed "submitted" overwrites a
  "filled".

  `client_order_id` is now derived rather than random
  (`execution/idempotency.py`), closing `docs/RISK_IMPLEMENTATION_NOTES.md`
  item 4. Two collisions that note did not anticipate are pinned by tests, and
  both were live in the first draft of this work: a reversal on one bar (exit
  the long, open the short) produces two SELLs agreeing on strategy, symbol,
  side and timestamp, so `purpose` is part of the key; and an entry filling
  100 + 100 produces two stops the venue would treat as one unless the child key
  names the *range* it covers, which would leave the second tranche naked while
  the router reported it protected.

  A third class of bug, found by review and fixed here, is worth recording
  because it will recur in every later Phase 4 item: **a fill that flips a
  position through zero resets nothing.** `Position.apply_fill` clears
  protective levels only at exactly flat, and a flip never passes through flat —
  so the old side's stops stay working, where a sell stop under what is now a
  short *adds* to the short when it fires. Protection is therefore tracked and
  counted by side, stale-side stops are cancelled before new ones are placed,
  and a level the market has already passed is refused rather than armed. Each
  of those failed silently before: the result object reported the position
  fully protected.

  Three deliberate refusals. A submit that fails in transport gets one lookup
  and then stops — it does not resubmit, because the venue may already hold the
  order, and it halts on `BROKER_UNREACHABLE` (the second auto-engage trigger to
  be wired, after `DATA_FEED_LOST`). Only the stop goes to the venue; a
  take-profit is armed on the position, because `BrokerPort` has no bracket and
  the fill handler that would cancel the losing leg is a separate item — placing
  both would ship the trap before the guard. And `SCALE_IN`/`SCALE_OUT` are
  refused exactly as the backtest engine refuses them.

  **Unticked, and not close.** Phase 4's *Verifiable:* line is a week of paper
  trading reconciling clean; four of the six items are unstarted and nothing
  calls this router in production yet. One dependency is worth naming now
  because it blocks that line: `risk_pct` sizing with an ATR stop is the
  documented default pair, and no `Signal` carries an ATR-derived level, so
  every entry from a default-configured strategy is refused at sizing.
  `StrategyRunner` holds the `BarRepository` and owns closing that.
- [ ] `SimulatedBroker`
- [ ] Reconciliation
- [ ] `StrategyRunner` live loop — @claude (wip).
  Untouched: `warmup`, `run`, `evaluate`, `on_fill_event` and `shutdown` all
  still raise. It needs a `BrokerPort` adapter and reconciliation, neither
  started, so there is nothing to run a loop against yet.

  The second half of this item — drop the `worker` compose profile so the worker
  rejoins the default stack "once it can actually start" — **is done** (#35), and
  is worth separating from the runner because it turned out not to depend on it.
  `atp_worker.main` is implemented: it wires the ingestor, the staleness watchdog
  and the scheduler to a real Redis and a real hypertable, supervises them, and
  halts on `UNHANDLED_EXCEPTION` if one of them ends instead of running until
  cancelled. SIGTERM is an ordinary shutdown and deliberately does not halt —
  otherwise every deploy would leave a halt for a human to clear.

  So the worker starts, ingests and runs scheduled jobs. **It does not trade**,
  and the startup log says so on every boot rather than leaving "the worker is
  up" and "the worker is trading" as the same observation. Unticked on that
  basis: this item is the live loop, and the live loop does not exist.
- [ ] Trade-updates WS with reconnect

*Verifiable:* a strategy trades the paper account for a week and reconciles clean.

## Phase 5 — Dashboard & analytics (requirements #6, #7)
- [ ] `/dashboard/live` aggregate endpoint
- [ ] `/market-data/calendar` sessions endpoint — @claude (#16).
  Implemented and tested: sessions, holidays and early closes over a range,
  straight from the exchange rules. Unticked because this phase states no
  *Verifiable:* line to tick against and nothing consumes it yet — the
  dashboard that would is not built. Worth a *Verifiable:* line of its own when
  someone writes one.
- [ ] React dashboard, 5-min refresh + WS
- [ ] Trade reconstruction, attribution, MAE/MFE
- [ ] Live-vs-backtest comparison
- [ ] Daily report

## Phase 6 — Production readiness
- [ ] **Authentication and authorisation** — blocking for any deployment
- [ ] Rate limiting, audit log surfaced in UI
- [ ] Alerting to a phone (feed loss, halt, reconciliation failure)
- [ ] Metrics/tracing
- [ ] Backups and a tested restore
- [ ] Deployment target chosen; secrets manager

## Later
Declarative rule builder UI · walk-forward optimisation · sector/factor exposure
limits · multi-broker · options · portfolio-level strategy allocation · IBKR
adapter for SG/HK markets

## Explicitly out of scope
HFT or latency-sensitive strategies (wrong architecture and wrong language) ·
market making · anything requiring co-location · ML model training (train
elsewhere, serve the signal here)
