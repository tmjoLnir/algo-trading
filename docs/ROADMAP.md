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
- [ ] `BrokerPort` + Alpaca adapter (paper first) — @claude (wip #36).
  `AlpacaBroker` is implemented over REST: account, submit, cancel, order and
  position reads, the venue clock, and both flatten paths. Paper and live are
  the same adapter on different hosts, which is the whole of requirement #5 at
  this layer — there is no `if paper:` anywhere in it.

  Three translations carry the risk, and each is pinned by tests rather than
  trusted:

  - **Status.** Alpaca has more order states than we do, and the map is total:
    an unrecognised one raises rather than defaulting. The plausible default is
    `SUBMITTED`, and an order reported as working when the venue has killed it
    is a position nobody is watching.
  - **Money.** Every number crosses the wire as a string in both directions.
    Responses parse with `parse_float=Decimal`, and requests stringify, because
    `json.dumps` cannot serialise a `Decimal` and both fallbacks lose exactness
    on exactly the fields where it matters (rule §1.1).
  - **A submit that dies in transport.** The adapter does *not* resubmit. It
    looks the `client_order_id` up, and adopts the order if the venue has it —
    the case rule §1.4 exists for, and the difference between a network blip
    and a duplicate position.

  One gap is stated rather than left to be discovered: REST reports
  `filled_qty` and `filled_avg_price` as running totals, so a fill read this
  way is **one** synthetic `Fill` for the whole quantity. That is right for
  P&L and wrong for anything inspecting the sequence, and it is the reason the
  trade-updates item below exists. That item has since landed (#37), so the
  sequence is available on the streamed path; a fill learned from REST — a
  reconnect catch-up, a reconciliation sweep — is still the collapsed one.

  Unticked. Every test is against recorded responses through `respx`; nothing
  in this item has yet been pointed at Alpaca's paper endpoint, and the phase's
  *Verifiable:* line is a week of paper trading.
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
  trading reconciling clean; two of the six items are unstarted — that was four
  until #36 implemented both brokers — and nothing calls this router in
  production yet. One dependency is worth naming now
  because it blocks that line: `risk_pct` sizing with an ATR stop is the
  documented default pair, and no `Signal` carries an ATR-derived level, so
  every entry from a default-configured strategy is refused at sizing.
  `StrategyRunner` holds the `BarRepository` and owns closing that.
- [ ] `SimulatedBroker` — @claude (wip #36).
  Implemented, both halves: `on_bar` for bar-driven fills and `on_quote` for
  tick-level ones, plus the whole `BrokerPort` surface over a local book.

  The decision worth reviewing is ADR 0006. The touch rule — when a resting
  order fills and at what price — was a private method on `BacktestEngine`,
  and is now `execution.matching.intended_price`, called by both. Two copies
  would not have diverged loudly: they would have diverged by a `<` becoming a
  `<=` on a limit touched exactly at a bar's low, and the symptom would be a
  paper run whose fills quietly disagree with the backtest that approved the
  strategy. `TestAgreementWithTheBacktestEngine` drives a real engine run and
  the broker over the same bars and compares the fills that come out, so the
  agreement is pinned by behaviour rather than by an import — verified by
  making the simulator fill at the close instead, which fails it.

  The extraction is behaviour-preserving: the engine's hand-computed 20-bar
  fixture, written precisely to catch a fill-timing change, passes unchanged.

  Three things it refuses to pretend about, recorded because the tempting
  version is the flattering one:

  - **Latency is not modelled at bar granularity.** 50ms against a 60,000ms
    bar is below the resolution of the data, and faking it there moves fills
    by a whole bar. It applies in `on_quote`, where it stops a strategy
    trading on the very quote it reacted to.
  - **Buying power is not re-checked.** `BuyingPowerRule` already ran — every
    order reaching a broker has passed the chain (rule §1.5). A second opinion
    here would refuse in paper what the platform approves in live, which is
    the one disagreement that makes paper trading actively misleading.
  - **A close is an order.** `close_position` rests and fills on the next bar
    like anything else, rather than flattening instantly at a price of its own
    choosing.

  Unticked, and the reason is the same as the item above: the phase's
  *Verifiable:* line is a paper week, and this has only ever met synthetic
  bars.
- [ ] Reconciliation — @claude (wip #38).
  `Reconciler.reconcile` and `adopt_broker_state` are implemented — docs/SAFETY
  .md's layer 7. All four documented checks: every broker position matched on
  **signed** quantity, no local position the broker does not have, every open
  broker order accounted for, and cash within a tolerance.

  Signed rather than absolute is the one to read twice. A long we believe is a
  short matches on magnitude and is the disagreement that *doubles* the loss
  when acted on, because every exit is then sized in the wrong direction. Pinned
  by a test, and verified by making the comparison `abs()` — which reports that
  book as clean.

  Four decisions worth naming:

  - **A broker we cannot read halts.** That table names layer 7's failure mode
    as "reconciliation itself is not running", so an unreachable broker is not
    a reason to skip the check and carry on — it engages `BROKER_UNREACHABLE`
    and re-raises. An unverified book and a book known to be wrong are the same
    thing to anyone about to size an order against it.
  - **Equity is deliberately not compared, only cash.** Equity is cash plus
    marks, and our marks come from our feed while the broker's come from
    theirs; two feeds a tick apart on an open position is not a book
    discrepancy, and reporting it as one would make layer 7 fire on every
    volatile day. Cash is arithmetic on fills, so drift there means a fill one
    of us does not know about — which is the thing worth catching.
  - **An orphan order is reported, never cancelled.** It is most often a
    protective stop placed before a restart, and cancelling it blindly leaves
    the position it guards naked — a worse state than the one being reported.
  - **`known_orders` has no default.** Defaulting it to empty would report
    every real order as an orphan and halt on the first healthy run; defaulting
    it to "skip check 3" would silently disable a documented safety check. The
    caller is the only thing that knows what it submitted.

  An adopted position carries **no** protective levels. The broker knows a
  position exists; it does not know the stop we intended for it, and inventing
  one from the venue's average entry price would arm a level no strategy chose.
  So a position adopted after a mismatch is unprotected until something re-arms
  it — which is the honest state, and why docs/RUNBOOK.md has the operator
  reconcile *before* clearing the halt.

  Unticked, and the blocker is worth stating precisely because it is not the
  usual one. `scheduler.reconcile_with_broker` is already on the 5-minute
  schedule layer 7 asks for and stays a `NotImplementedError` stub: it can
  build a broker and a kill switch from settings, but it has nothing to
  *compare them against*. That is now a wiring gap rather than a missing
  component: #44 gives `PositionSnapshotRow` a reader, so a scheduled job could
  load the last snapshot — but a snapshot is not the live book, and reconciling
  the venue against a minute-old copy would report every fill in between as a
  mismatch. It needs the runner's own portfolio, which means the runner
  handing it over rather than the scheduler fetching it. Reconciling against an
  empty portfolio would report every real position as `missing_position` and
  halt on the first run, so the job is left honestly unbuilt; the scheduler
  already treats a stub as unbuilt rather than failed.

  #39 builds the `StrategyRunner` that owns a live `Portfolio` and the set of
  orders we believe are working, so the missing half now exists as an object —
  but nothing constructs one in the worker yet, so the scheduled job still has
  nothing to reach for.
- [ ] `StrategyRunner` live loop — @claude (wip #39).
  Implemented: `warmup`, `run`, `evaluate`, `on_fill_event` and `shutdown`, plus
  `LiveContext` — the live counterpart of `BacktestContext`, serving a strategy
  from a rolling in-memory window of completed bars.

  The loop runs the documented six-step ordering, and **stops before signals**
  is the part that matters: it is the mirror of the backtest engine, and a
  divergence makes every backtest a claim about a system that does not exist.
  Pinned by a test that reads the order the router was called in, and verified
  by swapping the two steps — which fails it.

  `indicators/dispatch.py` is new and holds the name→`ta` lookup that the
  engine and the runner both need. ADR 0006's reasoning a third time: a
  strategy computing a different SMA(20) live than the one its backtest
  approved is the one divergence this platform's premise cannot survive. The
  engine delegates to it and its tests pass unchanged.

  Decisions worth reading:

  - **A mismatched book refuses to start.** `warmup` reconciles and raises on a
    dirty report rather than continuing. The kill switch is already engaged by
    then, so the chain would refuse every order anyway — raising means the
    operator sees why at startup instead of finding a process that is up and
    silently not trading.
  - **The strategy decides on bars; the book is marked on quotes.** `LiveContext
    .last_price` is the last *completed* bar's close, because a decision taken
    on a mid-quote inside an unfinished bar is one the backtest can never
    reproduce. Marking is the opposite case and uses the quote, because every
    percentage risk limit is denominated in the mark and a stale one makes a
    breached limit look compliant.
  - **A fill is booked and protected the instant it arrives**, not on the next
    pass — the window between owning a position and having a stop on it is
    unprotected exposure. What waits for the pass is the *strategy's* reaction
    (`on_fill`), which belongs inside the ordering like any other signal source.
  - **A fill for an order the runner does not know is refused**, not invented.
    Hanging it on a fabricated order would apply the quantity twice when the
    real one turns up; reconciliation reports it as an orphan instead.
  - **Bars are compared on timestamp, not count.** A repository re-serving the
    same bar — an idempotent upsert re-running, a restatement landing — must
    not read as a fresh close and re-trigger a strategy.

  This closes the dependency #33 recorded against the router: `risk_pct` sizing
  with an ATR stop is the documented default pair, no `Signal` carries an
  ATR-derived level, and so every entry from a default-configured strategy was
  refused at sizing. `_with_stop` derives one from the runner's own bar history
  — and leaves a signal that already names a stop exactly as it is, because the
  strategy's choice outranks a configured default.

  It also gives #38's `Reconciler` the `known_orders` set it had no source for:
  the runner tracks what it submitted, which is what makes orphan detection
  mean anything.

  **Step 6 of the ordering is complete as of #45.** The durable half landed in
  #44 — every pass saves the working orders and snapshots the book, and `warmup`
  restores what was working before reconciling. The publishing half is now
  there too: the runner builds a `LiveSnapshot` from the same portfolio and
  order set the pass just used, puts it where the API reads it, and announces
  each signal and each fill on the channels `atp_core.channels` names. All of
  it is best-effort and swallowed — an unreachable Redis must not fail an
  evaluation, because three failures in a row halt trading and stopping a
  strategy to protect a screen is a cure worse than the disease.

  The other gap recorded here — that `atp_worker.main` did not construct a
  runner — is closed by #40, which wires it behind an opt-in.

  Unticked. Phase 4's *Verifiable:* line is a paper week, and this loop has
  never been pointed at a venue — every test drives it off fakes.
- [ ] Trade-updates WS with reconnect — @claude (wip #37).
  `AlpacaBroker.stream_trade_updates` is implemented, and so is the consumer
  that makes it mean anything. Both gaps this item existed to close are closed:

  - **The fill sequence.** REST reports running totals, so a fill read that way
    is one synthetic `Fill` for the whole quantity. The stream carries the
    individual print, and `TradeUpdate` carries it as a `Fill` with the venue's
    `execution_id` on it.
  - **The state-machine hole.** `execution/state.py` named it and said the
    guard belonged "with whatever consumes the trade-updates stream". That
    consumer is now `execution/trade_updates.py`, which refuses a fill from a
    status the table would not have allowed to fill — deriving that set from
    `TRANSITIONS` rather than restating it. `state.py` is updated in the same
    diff to point at the guard rather than describe the gap.

  Three refusals in the applier, each one silent in the version that just calls
  `apply_fill`. A redelivered fill is discarded on the venue's execution id —
  without it a re-sent event doubles the position, and an id-less fill is
  deliberately *not* treated as a duplicate because two prints of the same size
  at the same price are ordinary. A fill against an order our book has already
  killed raises `ReconciliationError` rather than picking a side: applying it
  resurrects a dead order, dropping it leaves us disagreeing with the venue,
  and neither is recoverable in code. And a fill arriving before we recorded
  the submit walks the order forward through `SUBMITTED` instead — the event is
  proof the venue has it, so refusing on a technicality would leave a real
  position unrecorded.

  The reconnect signal is carried *in* the stream as `TradeUpdatesReconnected`,
  the same shape as `data.ports.FeedReconnected` and for the same reason: one
  `async for` body runs to completion before the next event, so the consumer's
  REST catch-up provably happens before it handles anything from the new
  connection. The adapter deliberately does not re-read open orders itself — it
  holds no book to correct.

  The account handshake is **not** the market-data one (`authenticate` with
  nested `key_id`/`secret_key`, versus `auth` with flat `key`/`secret`), and
  that is pinned by a test, because sending the wrong frame authenticates
  nothing and the server simply never answers.

  Unticked. Every frame here is shaped from Alpaca's documentation and nothing
  has been pointed at a live account stream — which is exactly the caveat #34
  turned into a finding when the market-data wire disagreed with the docs three
  ways at once. What is tested is our handling, not the vendor's shape.
- [ ] Worker wired to trade — @claude (wip #40).
  `atp_worker.main` now constructs a `StrategyRunner`, a `Reconciler` and an
  `OrderRouter` over the Alpaca adapter, and supervises two new
  responsibilities: the strategy loop and the trade-updates consumer. The
  startup log no longer says no runner exists, because one does.

  **It is off by default and stays off until somebody turns it on.** Three
  locks, in `atp_worker.trading.decide`, which is a pure function so the answer
  to "does this configuration place orders?" lives in one readable place rather
  than being inferred from a wiring block:

  1. `WORKER_STRATEGY` names a strategy; empty means no orders. Same posture as
     an empty `WORKER_SYMBOLS` — a worker that starts trading because it was
     *deployed* rather than because somebody chose to is the accident this
     prevents.
  2. Rule §1.8's existing pair, enforced in `Settings` itself.
  3. `WORKER_ALLOW_LIVE_ORDERS`, live only. The first two say this process may
     trade real money; the third says this unattended loop may place the
     orders. Different decisions, made by different people at different times.
     Paper ignores it deliberately — requiring it there would train operators
     to set it, which is how a lock stops working.

  Every branch is tested in both directions, and the third lock was verified by
  removing it, which lets a live configuration through and fails the test.

  Two things worth a reviewer's eye. **Boot adopts the broker's book**, in
  `trading.adopt_broker_state` rather than inside `warmup`: at boot we hold no
  book, so there is nothing for the broker's to disagree with, and without this
  a worker restarted holding positions would refuse to start forever
  (docs/RUNBOOK.md says `warmup()` adopts on restart; docs/SAFETY.md's checklist
  requires a restart to adopt rather than double). Mid-session drift keeps the
  old behaviour — it halts. With no persistence, *every* boot takes the adopt
  path, which means a bug that lost our state is indistinguishable from a clean
  restart. That is one of the reasons the repositories matter.

  And `StreamIngestor` now tracks **per-symbol** last-tick times, because
  `StaleDataRule` needs them and `last_message_at` is the feed's pulse rather
  than the symbol's: on a watchlist where one name is halted and the rest are
  busy, the feed looks healthy while that symbol has not traded for an hour.

  Unticked, and the reason is now only the demonstration. Nothing here has been
  run against the paper account — no credentials and no session from this
  environment — so what is shown is that the wiring assembles and the locks
  hold, not that a strategy traded.
- [ ] Order and position persistence — @claude (wip #44).
  `OrderRepository` and `PortfolioRepository` in `execution/ports.py`, with
  PostgreSQL adapters over the `orders`, `fills`, `position_snapshots` and
  `equity_snapshots` tables that had been sitting there with no reader.

  **What this is actually for.** Before it, the runner's book lived only in
  memory, so every boot adopted the broker's wholesale — which made
  reconciliation across a restart clean *by construction* and therefore
  worthless as evidence. `trading.restore_or_adopt` now tells the two
  situations apart: a stored book is used and the broker is allowed to disagree
  (a real check, since the views were formed independently), and only a
  first-ever boot adopts. docs/FIRST_PAPER_RUN.md's caveat about what a paper
  week cannot prove is narrowed to that first boot rather than deleted.

  A read failure raises rather than falling back to adoption. Adopting because
  the database blinked would silently discard our own book, which is worse than
  refusing to start.

  Two decisions worth review:

  - **The repositories are required on `StrategyRunner`, not optional.** A
    defaultable one would make the in-memory-only hole reachable by omission,
    which is the hole this closes.
  - **An order is upserted on its primary key, not on `client_order_id`.** The
    unique constraint on that key is the database half of rule §1.4 and stays
    exactly where it is; but the same key legitimately arrives many times as an
    order fills in pieces, so it is the wrong conflict target for a repeated
    save. Fills get a deterministic id derived from the order and their index,
    so re-saving an order inserts nothing new — a double-counted fill is a
    double-counted position.

  Migration `a1c4e77b91d2` adds `take_profit_price`, `high_water_mark`,
  `opened_at` and `fees_paid` to `position_snapshots`. The table stored
  `stop_loss_price` and nothing else a position protects itself with, and the
  first of those is the dangerous one: a trailing stop reloaded without its
  high-water mark re-anchors on the current bar, so `update_trailing`'s
  monotonicity guarantee holds around a mark that has moved *down*.

  Unticked. The SQL is exercised by 15 integration tests against a real
  Postgres in CI, but nothing here has survived an actual restart of a running
  worker — which is the demonstration, and it is one the paper week produces
  for free the first time the process is bounced.

*Verifiable:* a strategy trades the paper account for a week and reconciles clean.

*Verifiable (broker layer, proposed):* the same strategy over the same bars
produces identical fills through `BacktestEngine` and through `SimulatedBroker`,
and `AlpacaBroker` round-trips one order against the **paper** endpoint —
submitted, read back by `client_order_id`, cancelled — with every price and
quantity a `Decimal`.
**Partly shown** — @claude (#36). Proposed because the two broker items above
have no line they can be ticked against short of a full paper week, which is
the same trap three Phase 2 items fell into. The first clause is shown, by
`TestAgreementWithTheBacktestEngine`. The second is not: nothing in #36 has
been pointed at a real endpoint, and until it has, "the adapter works" rests
entirely on fixtures written from Alpaca's documentation — which #34 already
demonstrated can disagree with the wire in three ways at once. Adjust the
wording if it is not the demonstration you want.

## Phase 5 — Dashboard & analytics (requirements #6, #7)
- [ ] `/dashboard/live` aggregate endpoint — @claude (wip #45).
  Implemented, along with `/dashboard/equity-curve` and the WebSocket that
  carries events between polls. The decision is ADR 0007 and it is the part to
  review: the **worker** computes the book once, at the end of the evaluation it
  just acted on, and the API serves it verbatim rather than recomputing it from
  the order table and the quote cache. It could recompute it — and that version
  would be computed at a different instant from the one the trading loop used,
  which is the same "two instants" problem the aggregate endpoint exists to
  prevent, moved somewhere harder to see.

  The run mode, whether the market is open, and the active halts are
  deliberately *not* in the published book. Each must still be correct when the
  worker is dead, and a halt banner sourced from a snapshot nobody is
  publishing would say "not halted" at exactly the moment that matters most.

  Three refusals worth recording, because the tempting version of each is the
  flattering one:

  - **A number we cannot know is null, never zero.** An unmarked position is
    not a position worth nothing; leverage against no equity is undefined, not
    unlevered. `PositionView`'s mark-dependent fields became nullable here —
    the skeleton had them required, and that was the model that was wrong.
  - **"Nothing published" is not "you hold nothing".** A worker that is not
    trading publishes nothing, which is the default posture, and that is
    reported as itself with the banners and halts still rendering.
  - **An unreadable store is a 503.** A dashboard rendering "no positions"
    because Redis blinked would be telling its reader they are flat.

  `buying_power` was dropped from the account view: it is the venue's number
  and reading it costs a broker call per poll on the process placing orders
  against the same rate limit, while `BuyingPowerRule` constrains against cash.

  Unticked. Every test drives fakes or an ASGI transport; nothing here has
  served a book that a real worker put into a real Redis.
- [ ] `/market-data/calendar` sessions endpoint — @claude (#16).
  Implemented and tested: sessions, holidays and early closes over a range,
  straight from the exchange rules. Still unticked, and the reason has narrowed
  rather than gone away: this phase has a *Verifiable:* line now (#45), but
  that line is about the live book, and nothing consumes this endpoint even
  with the dashboard built — `market_open` is answered from the same
  `TradingCalendar` in-process, without an HTTP round trip. It is for the
  charting views that will grey out non-trading days, and it wants a
  *Verifiable:* line of its own when one of those exists.
- [ ] React dashboard, 5-min refresh + WS — @claude (wip #45).
  The six stub components render the book now, and `src/api/types.ts` stopped
  being a hand-written copy of a server contract: `make gen-types` dumps the
  OpenAPI document straight from the app — no running server, which is what
  made the old target something people worked around — and the file is aliases
  over the generated `schema.d.ts`.

  There is no decimal library on the front end and there does not need to be:
  the dashboard performs no arithmetic on money at all, because every derived
  figure is computed server-side, so `src/lib/money.ts` formats decimal
  *strings* and never parses one. The single float conversion is
  `toChartNumber`, for chart geometry, named so any other use reads as a
  mistake.

  Test infrastructure grew for this. There was no way to test a component — no
  jsdom, no testing-library, no vitest config — so the suite could only ever
  have asserted on the props a component was handed, and every rule in
  docs/DASHBOARD.md lives in what a person reads instead. A `.prettierrc.json`
  came with it, because prettier's defaults disagreed with every file in the
  app and `make fmt` would have rewritten 27 of them the first time anyone ran
  it; `make lint` and CI now gate web formatting the way they always did
  Python's.

  Unticked, and the gap is the demonstration rather than the code. It has
  narrowed without closing: a browser has now rendered this dashboard from the
  built bundle, through the proxy, against a live API — so the front end, the
  serving layer and the endpoint are shown to work as one. What is missing is
  the subject of this phase's *Verifiable:* line, a book that a **worker**
  published. With nothing trading, the screen correctly reports "No book
  published", which demonstrates the null-book path and says nothing whatever
  about agreement between the worker's numbers and the screen's — which is the
  only property that makes the screen worth reading.
- [ ] Trade reconstruction, attribution, MAE/MFE.
  Two prerequisites are worth naming here because #45 found them and did not
  fix them. `SignalRow` exists in the schema with **no writer** — the
  dashboard's signal feed is a bounded in-memory ring on the runner, so a
  restart empties it — and writing one needs a `strategies` row to satisfy its
  foreign key, which nothing creates either. `PostgresOrderRepository` stores
  `strategy_id` and `signal_id` as null for the same reason, so an order cannot
  currently be traced back to the decision that caused it. Attribution is that
  chain.
- [ ] Live-vs-backtest comparison
- [ ] Daily report

*Verifiable:* with the stack up and a worker trading paper, a browser opened at
any moment shows the same positions, cash and equity the worker's own log
reports for its latest evaluation; the age of that book is on the screen and
advances; halting from the dashboard makes the banner appear without a reload
and the next order is refused; and every monetary value is a string from the
`Decimal` in the runner to the pixels, with no `parseFloat` between them.

**Proposed** — @claude (#45). This phase had no *Verifiable:* line at all,
which is why #16's calendar endpoint has sat implemented and untickable since
Phase 1. It is written from the dashboard's side because that is what
requirement #7 asks for; the analytics items above will want their own, and
"the dashboard renders" is deliberately not enough — the line asks for
agreement between what the worker believes and what the screen says, which is
the only property that makes the screen worth reading.

## Phase 6 — Production readiness
- [x] **Authentication** — @claude. One operator, `API_USER` and a bcrypt
  `API_PASSWORD_HASH`, with the session in a signed `HttpOnly`,
  `SameSite=Strict` cookie. Everything under `/api/v1` requires it and so does
  the WebSocket, which refuses the handshake with close code 1008 rather than
  accepting a socket it would then send the whole book down. Design, and the
  four choices worth arguing with, are ADR 0008.

  The change that matters most is not the login screen. `actor` was a **query
  parameter the caller filled in themselves** on `/risk/halt`, `/risk/resume`,
  `/risk/flatten-all`, `/orders`, `/positions/{symbol}/close` and
  `/positions/{symbol}/stop`. It now comes from the session. An audit trail with
  a name box on it was not one, and it was going to be wrong on exactly the day
  those handlers stopped being stubs.

  Two things found on the way, both by running it rather than by reading it.
  `passlib[bcrypt]`, which this package declared from the start, **does not
  run**: passlib 1.7.4 reads `bcrypt.__about__.__version__`, bcrypt removed it,
  and every `hash()` raises on bcrypt 5. It is replaced by `bcrypt` directly
  rather than pinned to a 2020 release. And the login screen's run-mode banner
  was broken in a way its unit test could not see — it read the API's root `/`,
  which nginx serves the dashboard from, so the browser got `index.html` and the
  banner silently never rendered. A stub matching paths by suffix made `'/'`
  match every request in the suite. Both the endpoint and the stub are fixed.

  **Not** ready for a public address, and this item should not be read as
  saying so: no rate limit on the login endpoint, no revocation before a
  session expires, no TLS of our own, no secrets manager. The items below are
  the difference.
- [x] **Authorisation** — @claude. Not roles: this platform still has one
  account, and the note that stood here — that a role column with one value in
  it would describe a permission model rather than enforce one — is still true.
  What was wrong about it was the conclusion that there was therefore nothing to
  build. docs/RISK.md already stated an authorisation rule that nothing enforced:
  *"engaging needs no confirmation — hesitation is the expensive part. Clearing
  requires a named human."* That is about two acts, not two kinds of person, and
  it is the shape authority takes everywhere in this codebase.

  So: **session scopes** and **step-up**. A session is `full` or `read`, chosen
  at sign-in and carried in the signed token, so its holder cannot promote it. A
  read-only session reads everything and is refused every write with 403, with
  one exception — `/risk/halt`, which it may still call. That exception is a
  domain rule rather than a convenience: someone watching the book from a phone
  on the LAN is exactly who most needs to stop trading and least needs to place
  an order. Clearing a halt is deliberately not on the list, which is the
  asymmetry docs/RISK.md asks for, enforced instead of described.

  `/risk/resume` and `/risk/flatten-all` additionally require the account
  password in the request body — never a query parameter, which nginx logs
  verbatim. There is no elevation window on purpose: a "recently authenticated"
  period is a stretch of minutes during which a walked-away laptop can flatten
  the book. Design and the four rejected alternatives are ADR 0009.

  One thing found by testing rather than reading, and worth recording because it
  had been true since the previous PR: the 401-ends-your-session rule lived in
  `main.tsx`, which no test loads, so every web test ran against a query client
  that did not have it and the rule was asserted nowhere. It is
  `api/queryClient.ts` now, and both the 401 and the 403 behaviours are pinned.
- [x] **Rate limiting, audit log surfaced in UI** — @claude. Two halves,
  related because the second is how anyone finds out the first mattered.

  Sign-in is limited to ten attempts per five minutes per client address —
  address rather than username, because counting per username hands anyone who
  knows the operator's name a way to lock them out of their own platform.
  Attempts rather than failures, because otherwise the guess that happens to be
  right is the one that gets through. It fails open on an unreachable Redis: the
  degraded state is bcrypt alone, and failing closed locks the operator out
  during the outage they most need to look at. `/risk/halt` is never limited.

  The audit trail is the more interesting half. `AuditLogRow` has been in the
  schema and the initial migration since the first commit with **nothing writing
  it and nothing reading it** — the state `SignalRow` is still in — while
  `killswitch.py` claimed clearing a halt was "always audit-logged" and
  `models.py` promised "every consequential human action". What "audit-logged"
  actually meant was a structlog line: an operational stream, rotated away, in a
  format nobody promised. There is a port, a Postgres adapter, a read endpoint
  and an **Audit** page now.

  It records less than that docstring implies, deliberately: signing in and out,
  failed attempts, lockouts, and actions refused to a read-only session. Order
  flow and kill-switch changes are **not** wired, because every one of those
  handlers is still a `NotImplementedError` stub and a write behind a stub is
  dead code. They land with their handlers. The table's docstring is now the
  optimistic document and this item is the honest one.

  Two rules the design turns on. A failed audit *write* never fails the action —
  the actions worth auditing include halting trading, and refusing to stop
  because Postgres is down has the failure modes inverted. A failed audit *read*
  is a 503 and never an empty page, which is ADR 0007's "nothing published is not
  an empty book" applied to the record instead of the book. Full reasoning and
  five rejected alternatives: ADR 0010.
- [ ] Alerting to a phone (feed loss, halt, reconciliation failure)
- [ ] Metrics/tracing
- [ ] Backups and a tested restore
- [ ] Deployment target chosen; secrets manager — @claude (wip).
  **The target is chosen and recorded; nothing is deployed.** ADR 0011: one
  always-on VM per run mode in a US-East region, the existing compose stack,
  reached over Tailscale, deployed by an explicit operator action, with paper
  and live on separate hosts so that docs/SAFETY.md layer 3 is structural
  rather than conventional.

  Two constraints decided it, and both are in the code rather than in anyone's
  preference. Alpaca refuses a second stream connection per key with code 406,
  which `AlpacaRealtimeFeed` treats as permanent — so the worker is a singleton,
  and every mainstream orchestrator's default rollout overlaps instances, which
  is the duplicate-position incident in docs/RUNBOOK.md. And HA is not wanted:
  broker-side stops hold positions while the platform is down, so the posture is
  fail-stopped and restart cleanly rather than multi-node.

  What landed with it is `docker-compose.prod.yml`, which is the more useful
  half today. `docker-compose.yml` is a development stack — that is deliberate
  and documented — and deploying it as-is would have been three quiet faults at
  once: `db`, `redis` and `api` carry no restart policy while `worker` does, so
  a reboot brought back the worker alone, whose kill switch fails closed against
  an unreachable Redis and which therefore came up halted while looking alive;
  `api` and `worker` bind-mount `./libs` and `./apps/*` over the code baked into
  the images, so what runs is the checkout rather than what was built and
  tested; and `api` runs uvicorn with `--reload`, which makes a `git pull` a hot
  deploy mid-session. The overlay corrects all three, `make deploy` applies it,
  and docs/DEPLOYMENT.md is the procedure — the page README.md has linked to
  since the skeleton and which did not exist.

  `scripts/check_port_bindings.py` now checks **both** configurations. It read
  only the development one, so the deployed file — where a wrong bind matters
  most — would have been the one thing nothing looked at. It also asserts the
  deployed *shape*: the overlay strips those mounts and `--reload` with
  compose's `!reset`, and a compose that does not know the tag leaves them in
  place silently, so the resolved configuration is checked rather than the file
  trusted. Verified by removing the tag and watching the check fail.

  What was actually run, since a compose file that only parses is not a compose
  file that works. `db` and `redis` were started **through the overlay** and
  inspected as containers rather than as YAML: both come up with
  `RestartPolicy=unless-stopped` and a rotating log driver, which the base file
  gives neither. The database password is enforced on the path the API and
  worker use — over the compose network the base file's `atp` is refused
  `password authentication failed` and the configured one succeeds. (In-container
  loopback is `trust` in this image, so a `docker exec psql` test proves nothing
  and was not what was used.) The `api` and `worker` images could **not** be
  built here: both Dockerfiles pull `uv` from `ghcr.io`, and this environment's
  egress policy answers 403 for its blob storage. CI has that access and builds
  both in the `stack` job on every push.

  **Unticked, and not close.** Nothing has been provisioned and nothing has been
  deployed: this is a decision, a compose file and a runbook, none of which is a
  running host. The secrets-manager half is unstarted — ADR 0011 chooses SOPS +
  age and no tooling is written, so `.env` at `0600` is still the secret store.
  Alerting and backups, the two items above, are what a live host would need
  next, and docs/DEPLOYMENT.md says so rather than implying a host is ready.

  One thing found on the way and recorded rather than fixed: `tailscale serve`
  is the documented way to get HTTPS, but TLS terminates at Tailscale and nginx
  sets `X-Forwarded-Proto` from its own `$scheme`, which is `http` — so the
  session cookie `_is_https()` guards is **not** marked `Secure` behind exactly
  the TLS this recommends. Fixing it means deciding which proxy's headers to
  trust, which is a security change rather than a deployment one. SECURITY.md
  lists it and docs/DEPLOYMENT.md explains it.
- [x] Dashboard served as a built bundle rather than a dev server — @claude (#46).
  `infra/docker/web.Dockerfile` gained a `prod` stage: the `npm run build`
  output served by nginx, with `/api`, `/ws` and the health probes proxied so
  the browser only ever sees one origin (`make up-prod`, port 8080). The
  dashboard addresses the API with relative paths now and derives the socket
  scheme from the page's, so the bundle carries no hostname and one image is
  correct on localhost, on a LAN address and behind TLS.

  This closed a trap rather than only adding a target. Vite inlines
  `VITE_API_BASE_URL` into the bundle at **build** time, and docker-compose was
  setting it as **runtime** container environment — which is invisible, because
  it works for the dev server it was actually pointed at and does nothing
  whatsoever to a built bundle. A production build made under that arrangement
  had `http://localhost:8000` compiled into it, which was confirmed by building
  one before the change.

  Ticked against the serving-layer line proposed at the end of this phase. What
  that rests on is worth stating precisely, because the two halves were shown
  by different means and neither is the other. CI's `stack` job builds the
  image, starts it, and asserts with curl that the bundle carries no
  compiled-in host, that `/healthz` answers through nginx on the dashboard's
  own origin, and that a client-side route survives a hard refresh — first
  green on the #46 merge, run 32099581786, and re-run on every push since.
  Separately, headless Chromium rendered the dashboard from the real config and
  the real bundle against a live API, which is what showed the SPA fallback,
  the cache headers, gzip, the `/api/v1` prefix and query string arriving
  unstripped, and the WebSocket upgrade headers forwarded. CI drives the image
  but no browser; the browser run served the config directly rather than from
  the image. The line is written to what CI mechanically re-checks, so it does
  not claim the half only a human has watched.

  The caveat that stood here — that the image had never been built and nothing
  had ever been served from the container — is no longer true, and is corrected
  in place rather than left reading as current. The `stack` job built the image
  and served from it on the #46 merge commit (CI run 32099581786): the bundle
  carried no compiled-in host, `/healthz` answered through nginx on the
  dashboard's own origin, and a client-side route survived a hard refresh. Every
  push re-checks it, which is the point of putting it there rather than in a
  deployment runbook.

  Exposure is a decision now rather than a default. Every port in
  `docker-compose.yml` binds `127.0.0.1`: the API, the `atp`/`atp` Postgres and
  the passwordless Redis holding the kill-switch state were all published on
  `0.0.0.0`, which contradicted docs/SAFETY.md's own "bind to localhost only"
  rule for as long as both have existed. The dashboard's port is the single
  deliberate exception, through `ATP_WEB_BIND_ADDR`, and `make check-bindings`
  refuses both a wildcard and a publicly routable address — in CI, and as a
  pre-flight on `make up-prod` itself, before anything starts. Moving that one
  port is safe only because nginx reaches the API across the compose network, so
  putting the dashboard on a LAN does not put an unauthenticated API on it too.

  The public-address half of that check was added after someone set
  `ATP_WEB_BIND_ADDR` to the address `what is my IP` returned — their router's
  public one. A wildcard check waves that through: it is a specific address, and
  it is the worst available value for it. The test is `is_global` and not
  `is_private`, because Tailscale allocates from 100.64.0.0/10, which reports as
  not-private while being unroutable from the internet — an `is_private` check
  would have refused precisely the route docs/DASHBOARD.md recommends.

  It still does not make the platform deployable. The item above this is
  unstarted and the authentication item at the top of the phase is still
  blocking: this serves the dashboard to a private network and nowhere else.

*Verifiable (rate limiting and audit, proposed):* a run of wrong passwords from
one address ends in 429 with a `Retry-After`, a correct password from that
address is refused too while the window holds, and the same password from
another address is not; and every one of those events, plus a write refused to a
read-only session, is readable afterwards on the Audit page — with an unreadable
trail saying so rather than rendering as an empty one.
**Shown** — @claude. Driven end to end against a real PostgreSQL and a real
Redis through the real nginx config: four wrong passwords, a fifth answered 429
with `Retry-After: 299`, a read-only sign-in from a second address that was
unaffected, and a `POST /api/v1/orders` refused 403. All seven events were then
read back from the table and rendered on the page — `login_failed` x4,
`rate_limited` with its retry, `login` with `scope=read`, and `forbidden`
carrying the path and method — with the action filter narrowing to four. The
storage behaviour has its own integration tests against a real database,
including that a write which cannot land returns rather than raising.

Scoped to these two: it says nothing about what is *not* audited, which is most
of what the table's docstring anticipates and all of which is still stubbed.
*Proposed and ticked by the same hand; it wants a reviewer's eye.*

*Verifiable (authorisation, proposed):* a read-only session is refused every
mutating route with 403 and is permitted `/risk/halt`; a full session is refused
none of them on scope; clearing a halt and flattening the book require the
account password and are refused without it; and being refused does not end the
session.
**Shown** — @claude. The two sweeps are exhaustive over the generated schema,
walking every mutating route as a read-only session and again as a full one, so
a route added later is refused by default and it is *adding one to the exception
list* that this notices. The exception itself is pinned separately, along with
the halt/resume asymmetry. Step-up is checked at all three outcomes: refused by
the schema with no password, 403 with the wrong one, past the gate with the
right one — "past the gate" being a 500 into a stub, which is the honest
assertion while the handler is unbuilt.

Then driven in a real browser through the real nginx config: the read-only badge
renders, `/dashboard/live` returns 200, `/risk/halt` is not refused, `/orders`
and `/positions/{symbol}/close` and `/risk/resume` are each 403 — and after four
consecutive refusals the operator is **still signed in**, which is the property
the 401/403 distinction exists for and the one a naive implementation gets
wrong. Scoped to authorisation: it says nothing about rate limiting or
revocation, which remain unbuilt. *Proposed and ticked by the same hand; it
wants a reviewer's eye.*

*Verifiable (sign-in, proposed):* every route under `/api/v1` and the WebSocket
refuse a caller with no session; a correct username and password return a cookie
that admits both; a wrong password and an unknown user are refused with the same
message; and `actor`, wherever a handler records one, comes from that session
rather than from the request.
**Shown** — @claude. The refusals are held by an exhaustive contract test that
walks every path in the generated schema, so a route added later is covered
without anyone remembering to cover it. The rest was shown by driving a real
browser against the real nginx config and the real built bundle: the login
screen renders unauthenticated with no dashboard behind it, a wrong password
gives the one message, a correct one lands on the book, the cookie arrives
`HttpOnly` and `SameSite=Strict` through the proxy, **the WebSocket opens and
stays open** — which is the whole argument for a cookie over a bearer token,
since a browser cannot put a header on a handshake — and signing out clears it.

What that did not cover: it ran nginx and the API as host processes rather than
from the compose stack, and it has never met TLS, so `Secure` on the cookie is
correct-by-construction rather than observed. Scoped to sign-in on purpose — it
says nothing about rate limiting, revocation or authorisation, which are
unstarted. *Proposed and ticked by the same hand; it wants a reviewer's eye.*

*Verifiable (serving layer, proposed):* the dashboard is served from the built
image with no hostname compiled into its bundle; the API answers on the
dashboard's own origin through the proxy; a client-side route survives a hard
refresh; and no service in the compose file publishes a port on a wildcard or a
publicly routable address — all of it re-checked on every push, and on every
`make up-prod`, rather than discovered at deploy time.
**Shown** — @claude. Proposed because this phase states no line at all, which
is the same defect #45 named in Phase 5 and that left #16's calendar endpoint
implemented and untickable through the whole of Phase 1: a phase without a line
cannot tick anything, however much of it is built. It is scoped to *serving* on
purpose. It says nothing about authentication, rate limiting, alerting,
metrics, backups or a secrets manager — the other six items here, which are
genuinely unstarted — and it must not be read as evidence for any of them.
*Proposed and ticked by the same hand, so it wants a reviewer's eye rather than
mine.*

*Verifiable (deployment, proposed):* the stack runs unattended on the chosen
host across a full trading week — a deliberate reboot brings every service back
without a hand on it, a deploy mid-week never has two workers alive at once, the
dashboard is reachable over the tailnet and from nowhere else, and no plaintext
secret sits on the host outside the `0600` runtime file.
**Not yet shown** — nothing has been provisioned. Proposed here because this
item had no line it could ever be ticked against, which is the defect #45 and
#46 both named: a phase without a line cannot tick anything, however much of it
is built. It is scoped to *deployment* on purpose and says nothing about
alerting, metrics or backups, which are separately unstarted.

That line and Phase 4's are the same week of uptime seen from two ends, which is
the argument for doing this now rather than at go-live: Phase 4 has eight built
items held against "a strategy trades the paper account for a week", Phase 5
needs "a worker trading paper", and Phase 1's streaming line needs a forced
disconnect during a session. None of that is demonstrable on a laptop that
sleeps.

The phase still needs a line covering production readiness as a whole. The
reason previously given for not writing one — that nothing is deployable until
authentication lands — no longer holds, since it has; what stands in its way now
is alerting, backups and metrics, so a line written today would still describe a
system docs/SAFETY.md's own go-live checklist refuses.

## Later
Declarative rule builder UI · walk-forward optimisation · sector/factor exposure
limits · multi-broker · options · portfolio-level strategy allocation · IBKR
adapter for SG/HK markets

## Explicitly out of scope
HFT or latency-sensitive strategies (wrong architecture and wrong language) ·
market making · anything requiring co-location · ML model training (train
elsewhere, serve the signal here)
