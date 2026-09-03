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
| `- [ ] item — @who` | Built and unticked: waiting on the phase's *Verifiable:* line, not on code |
| `- [x] item — @who (#12)` | Done, demonstrated, and merged in that PR |

The third row was added after six Phase 5 items had been sitting in that state
with no way to say so. It is not a softer tick and must not be used as one: it
means the work is finished and the demonstration is not, which is a different
sentence from "nearly done".

A box is ticked only when the phase's *Verifiable:* line has actually been shown
— not when the code compiles, and not when the tests pass in isolation. If an
item turns out to be wrong, in the wrong phase, or ticked when it should not be,
fix it here in the PR that discovered it.

## Where this stands

**21 of 51 items ticked — and the other 30 are not a measure of what is
unbuilt.** Reading them as one is the specific mistake this section exists to
prevent. Twenty-three of the thirty sit in Phases 4 and 5, whose *Verifiable:*
lines both come down to the same event: a strategy trading the paper account for
a week. The code is written and tested. The week has not happened.

| Phase | Ticked | Open | What the open items are waiting on |
|---|---:|---:|---|
| 0 — Foundations | 4 / 4 | 0 | — |
| 1 — Data | 3 / 5 | 2 | A forced disconnect against the live stream, and a quote proving its own age |
| 2 — Backtesting | 7 / 7 | 0 | — |
| 3 — Risk | 2 / 4 | 2 | A strategy that tries to breach every limit, refused by name |
| 4 — Execution & paper trading | 0 / 11 | 11 | **The paper week.** A strategy trades the paper account for a week and reconciles clean |
| 5 — Dashboard & analytics | 0 / 12 | 12 | **The same paper week**, read back through the screens and the analytics |
| 6 — Production readiness | 5 / 8 | 3 | A scrape from a real deployment, a restore actually performed, the chosen host with the stack actually on it |
| **Total** | **21 / 51** | **30** | |

The thirty open items are in three different states, and the difference
matters more than the count:

| State | Count | Means |
|---|---:|---|
| Claimed, in progress (`wip`) | 0 | Nobody has an open PR against an item here right now |
| Built, awaiting the phase line | 28 | Phases 1, 3, 4, 5 and 6. Code merged and tested, nothing left but the demonstration |
| Unclaimed | 2 | **Daily report** (Phase 5) and **strategy lifecycle verbs** (Phase 4) — nobody has started either |

Two things a reader should take from this rather than from the counts:

- **Phase 4 and Phase 5 being at zero is one fact, not twenty.** Both phases
  hinge on the paper week; neither has an item blocked on anything else. A
  reader who wants to know what is genuinely missing should look at the three
  Phase 6 items and the two unclaimed ones, which is the whole of it.
- **A tick here is expensive on purpose.** Phase 1 is the only phase whose
  *Verifiable:* line has been shown against live data, and it took real egress,
  real credentials and a real hypertable to earn three boxes. That is the
  standard the other phases are held to, and it is why the count looks worse
  than the codebase is.

This section is derived from the boxes below and will lie the moment it drifts
from them, which is the failure `CLAUDE.md` §6 is about.
`tests/unit/test_roadmap_summary.py` parses both and fails when they disagree,
so updating an item without updating this table breaks the build rather than
the record.

That test can only compare this file against itself, and one thing here is a
claim about the outside world: `wip #12` says a pull request is still open.
Every one of the twenty-one `wip` markers in this file was false on 2026-09-02
— one of them naming a PR that had never merged at all — and the summary test
passed on every one of those days, because "is somebody still working on this?"
is not a question about the document (#125). `scripts/check_roadmap_wip.py`
asks GitHub instead: its format half runs offline in the unit suite, and CI
runs the half that reads PR state. A state it cannot read is a note rather than
a failure, so the check is red only when it can name a finished PR.

## Phase 0 — Foundations (skeleton is here)
- [x] Repo structure, tooling, docs, CI (#1, #2)
- [x] `make install` and `make up` work end to end — @claude (#7, #35).
  Demonstrated by the `stack` CI job, which runs both on a clean checkout.
  `make up` starts db, redis, api, web **and the worker**: the compose profile
  that held it back is gone, because the condition stated here for removing it —
  that its entry point stop raising `NotImplementedError` — is met (#35). On a
  clean checkout the worker comes up in backtest mode with no watchlist, reports
  that it is ingesting nothing, and runs its schedule.

  **Both halves of that stopped being true and are repaired here (#126).** #124
  gave the worker a `worker_config` row to read at boot, and refusing to start
  without one is deliberate (ADR 0023) — but nothing in `make up` had ever run a
  migration, so on a clean checkout that table did not exist and the worker
  crash-looped instead of idling. `make up` now applies the schema before it
  starts anything; `make deploy` deliberately still does not, because on a host
  that is an operator's decision inside a halt window (ADR 0024).

  The sentence that used to end this entry — that the `stack` job's "none is
  restarting" step covered the worker "without needing a new check" — is the
  more expensive half to have got wrong, so it is removed rather than reworded.
  That step sampled container state once, and a crash-looping container reads
  `running` for most of each cycle: the same tree passed on #124 and on its merge,
  then failed twice on #125. It now asserts Docker's restart counter across a
  settling window, which is a fact about an interval rather than a guess taken at
  an instant.
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
- [ ] Real-time WS ingestor, reconnect + gap backfill — @claude (#18).
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
- [ ] Redis quote cache, pub/sub publisher, staleness monitor — @claude (#21).
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
- [x] Backtests price off adjusted closes — @claude (#103).
  `BacktestEngine.run` converts every bar into adjusted space before the first
  mark, and refuses a run whose bars carry no `adj_close` instead of pricing it
  raw. Missing from this file until now, which is its own small failure: the
  rule has been in `CLAUDE.md` §5, `docs/DATA.md` and `Bar`'s docstring since
  the beginning, the Alpaca provider has always paid for a second pass to fill
  the column, and `upsert_bars` has always COALESCEd it so a raw refetch cannot
  erase it — and nothing read it. A roadmap with no line for a rule that is
  stated three times and implemented nowhere is how that survives.

  Found by running the `buy_and_hold` item below over real bars. That run
  reported 331.7% over six years and contained a single **+51.16%** day: GE's
  1:8 reverse split on 2021-08-02, where the raw price octupled overnight and
  the held share count did not divide. Twenty-eight standard deviations of the
  run's own daily volatility, four times the largest genuine move in the
  window, and nothing in the result said so.

  The conversion scales the whole candle by `adj_close / close`, not just the
  close, because the engine reads six price fields and a run that marks at an
  adjusted close while filling at a raw open is wrong by the split factor;
  volume divides by the same factor so the participation cap stays a fraction
  of the bar's real notional. A missing `adj_close` refuses the whole run —
  `--raw-only` leaves the column unset, and the silent fallback is what
  produced the bug. Reasoning, and the live-warmup defect this does *not*
  fix, in `docs/adr/0017-backtests-price-off-adjusted-closes.md`.

  Ticked against this phase's *Verifiable:* line, which is what it is:
  hand-computed fixtures in `TestCorporateActions` put a 1:8 reverse split and
  a 4:1 forward split mid-window and pin the run to the market's own return
  (0.008, or `qty x (last close - entry open) / equity`) rather than to the
  split factor — 0.096375 on the same bars priced raw, twelve times the truth.
  Six of the seven fail with the conversion removed.

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
  number a human has already read is a number they have already believed. **Both
  narrowed in #81, and this paragraph went on denying it until #129.** It used to
  say that sizing was a fixed share count and the rule chain was empty, and that
  both were Phase 3 work still to come. By 2026-09-02 forty-four commits had
  edited this file since #81 and eighteen since `AUDIT.md` finding 33 named the
  sentence, and none of them touched it. It is corrected rather than deleted
  because the caveats did not disappear — they narrowed, and what is left is
  worth a reader's attention.

  **Sizing goes through `risk.rules.position_size`**, the function the live
  router calls, with the same arguments: `--sizing` picks the method and
  `--sizing-value` supplies what it reads. `fixed_qty` is still the default, so a
  spec stored before those fields existed reproduces exactly — and a run using it
  still prints the caveat it always did, because sizing every entry identically
  really does make the return a property of that share count. What changed is
  that the warning is now a statement about a choice rather than about the
  platform. `risk_pct` needs a stop, and a signal without one is booked as a
  **refused order** naming the sizing stage rather than dropped silently.

  **The rule chain is live**, as `risk.engine.backtest_rules()` — five of the
  nine. The four that are absent are absent by decision: a kill switch, a session
  calendar, a rate limit and a feed-staleness check each measure something a
  replay over stored bars does not have. That is itself a caveat, and it is
  attached to every run that keeps its chain rather than to none of them, because
  all four only ever refuse — a live account would be stopped more often than a
  backtest was, never less. A run that deliberately asks for no rules says that
  instead.

  Neither is Phase 3 work still to come. Both are built, and the Phase 3 boxes
  they belong to sit in the third row of the conventions table at the top of this
  file — built and unticked, waiting on a demonstration rather than on code. The
  sizing item is waiting on a reviewer to accept the *Verifiable:* line it
  proposes; the phase's own line is waiting on a strategy that tries to breach
  every limit and is refused by name. "Unticked" and "not built" are different
  sentences, and confusing them is precisely what this paragraph used to do.
  docs/BACKTESTING.md's "Sizing, and what the chain refuses" is the account in
  full.

  `POST /api/v1/backtests` and its worker task are built as of Phase 5's
  backtests tab (#67), which is where they belonged — they were never Phase 2
  items, and the dashboard that consumes them is that phase. Both paths now run
  through `atp_core.backtest.runner.run_spec` — which assembles the engine with
  `build_engine` and attaches the caveats the run earns — so a queued run and
  this CLI cannot report different numbers, or different warnings, for the same
  parameters. The CLI reached that function last, in #110; until then it
  assembled the same engine and attached none of the caveats.

- [x] `buy_and_hold` benchmark — @claude (#94, #109).
  The second registered strategy, and the one that makes the first one's number
  mean something. Every item above this line produces a return; none of them
  produces anything to read it against, and a return with nothing beside it is
  not evidence — 18% over a year is skill against a flat market and a bad year
  against one that returned 30%. `docs/BACKTESTING.md`'s "Before believing a
  result" checklist was missing that comparison entirely; this diff adds the
  line and the strategy that satisfies it.

  Three decisions, each the version one line longer than the tempting one, and
  each wrong in a way no result would show:

  - **It fills at the second bar's open**, like every other strategy. A baseline
    exempted from next-bar fills is measured at a price nobody could have paid,
    and since it is what everything else is compared *against*, flattering it by
    one bar's move understates the whole platform by that amount.
  - **One attempt per symbol, not "enter whenever flat".** The shorter version
    becomes buy → stopped out → buy again as soon as a stop is configured: a
    re-entry system whose results depend on the stop, which is the opposite of a
    fixed baseline.
  - **It reads the position, not its own signals.** A signal is a request that
    fills a bar later and can be refused. Counting emitted signals would have a
    restarted runner double a position it already holds — and, in the case that
    actually bites, buy back in after a restart followed by a stop-out.

  Two properties are pinned by hand-computed arithmetic rather than by the code
  agreeing with itself: the fill lands on the bar *after* the decision, and the
  run earns exactly `qty × (last close − entry open) / starting equity` — the
  market's return over the window it was actually in, which is the whole claim a
  benchmark makes.

  **Ticked (#109): run over real stored bars and reconciled.** Twenty symbols
  (four index ETFs and sixteen large caps), 1,525 daily bars from 2020-07-27 to
  2026-08-20, `alpaca_equities_default()` costs, `--sizing equity_pct 0.05`:

  ```
  equity 100,000 → 302,809.33   total_return 2.0281   fees $0.00
  cagr 0.2011   sharpe 1.276   sortino 1.860   calmar 0.921
  max_drawdown -21.83% over 260 days   volatility 0.153
  0 round trips   20 signals / 20 orders / 20 fills / 20 open
  exposure 99.9%   turnover 0.99×
  ```

  Run twice — once through `scripts/run_backtest.py --out`, once queued and
  exported from the Backtests tab — and the two files agree on all 1,525 equity
  points and all nineteen metrics exactly, which is the invariant the
  `build_engine` note above claims and the first time it has been observed
  rather than asserted. All nineteen recompute from the exported curve through
  `metrics.compute_all` to the last digit.

  Three of the strategy's decisions are now seen on real bars rather than
  fixtures. The first equity point is exactly the starting cash and the second
  has moved, so the entry filled at the bar *after* the decision. Twenty signals
  produced twenty fills and twenty still-open positions — one attempt per
  symbol, not re-entry. And `realized_pnl` is 0 with the whole 202,809.33
  unrealised, so nothing was ever sold.

  `fees $0.00` is the cost model being right, not off: Alpaca's equity
  commission is zero and the SEC/TAF fees `PerShareCostModel` keeps are
  sell-side, so a strategy that never sells pays none. Slippage was charged —
  it lands in the fill price, not the fee line.

  The conversion recorded under "Backtests price off adjusted closes" (#103) is
  confirmed by the same run, on the universe that found it: 2021-08-02, the GE
  1:8 reverse split that once showed as a **+51.16%** day, is now −0.26%. The
  largest move in the window is 2025-04-09's +8.22%, which is the tariff-pause
  rally and belongs there.

  **Two caveats that are properties of this run, not of the platform.** The
  universe is twenty names that all still exist in 2026 — textbook survivorship
  bias (`CLAUDE.md` §5), so 20.1% CAGR is a property of that basket and not of
  the market; it is a baseline to compare strategies against on the *same*
  universe, and nothing else. And `num_trades` is 0 by construction, so the nine
  per-trade metrics are placeholder zeros rather than measurements — which is
  what the run's own warning says, and, until #109, what only one of the two
  export paths recorded.

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
  reach a broker adapter around it.

  The half that was left — "the backtest CLI still passes an explicit empty
  chain" — is closed too. `backtest_rules()` is the chain a replay over bars can
  evaluate: five of the nine, with the four it cannot named and justified rather
  than passed stubs that always approve. `trading_hours` is the one worth
  knowing about, because including it looks harmless and is not: a daily bar is
  stamped at exchange-local midnight, so the calendar reports closed at every
  one and the rule would refuse every order in every daily backtest.

  **And one rule in the live chain had never been reachable.**
  `DailyLossLimitRule` denies every entry until something calls `anchor`, which
  is correct — it will not assume the day began flat — and nothing in this
  platform ever called it. So `apps/worker`'s chain was configured to refuse
  every entry it would ever produce. It survived because the failure is
  invisible from outside: a chain refusing everything and a chain nothing has
  reached look identical, and nothing has traded paper. `RiskEngine
  .anchor_session` is the named seam, `StrategyRunner.warmup` calls it at each
  session open and the backtest engine at each session in the replay.

  **And every one of those nine rules measured the wrong book** (#112). A limit
  is checked per order and the book only moves on a fill, so a caller submitting
  several orders before any settles had each judged against a book holding none
  of the others. Forty entries at 5% of equity each pass a 100% gross cap alone
  and are 200% together — which a 40-symbol `buy_and_hold` replay did: forty
  filled, 1.97x gross exposure, cash at −97,046, and the cap refused nothing.

  It is not only the two rules that price the book. `max_open_positions` counts
  positions, so a batch submitted at nineteen open all passes; `max_position_pct`
  reads one symbol's quantity, so two orders in the same name at 6% each pass a
  10% cap. Four of the five rules a replay can evaluate, and the same four live:
  `StrategyRunner._submit` loops signals through the router against a portfolio
  that only moves when `_drain_fills` runs.

  `RiskEngine.validate` now takes what the caller has committed and not yet seen
  settle, and `rules.project_pending` advances the book before the chain reads
  it — one projection, so no rule knows in-flight orders exist. Found by
  checking a benchmark export rather than by reading the code: the run said 542%
  and the turnover said it had borrowed to get there. ADR 0020.
- [ ] Position sizing, all methods — @claude (#30).
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
  end to end rather than against `position_size` alone.

  **And a second caller, which is what closes this.** `build_engine` sized every
  backtest at a flat share count — `FixedQtySizer` — so none of the five methods
  had ever decided a quantity in a run anybody read. `RiskBasedSizer` delegates
  to the same `position_size` the router calls, with the same arguments, so the
  two cannot drift; `BacktestRunSpec` carries the method and its value, and a
  spec stored before those fields existed still resolves to the `fixed_qty` run
  it was. The inputs the function refuses to default surface as a **refused
  order** naming the sizing stage rather than as a silent zero — `sma_crossover`
  emits no stop, so a `risk_pct` run over it books one refusal per entry and
  says so, instead of returning an empty result indistinguishable from a
  strategy that never signalled.

  *Verifiable (proposed, and shown):* the docs/RISK.md worked example reproduces
  through the path a backtest actually uses. $100,000 at 1%, a $50 entry against
  a $48 stop is 500 shares and against a $35 stop is 66; both lose within $15 of
  $1,000 if stopped. Asserted through `RiskBasedSizer` rather than against
  `position_size` directly, because the thing that was missing was never the
  arithmetic. Proposed here because the item's own text asked whoever reviewed
  it to settle a line, and this is that line.

  **Still unticked, and deliberately.** The reason above has not gone away, it
  has only got closer: this PR proposes the line and shows it, in one diff,
  which is exactly what the earlier text declined to do. That refusal was right
  and is not mine to overturn — proposing your own bar and declaring you cleared
  it is not a demonstration, it is a decision about what counts as one, and that
  belongs to a reviewer. The box is one review away rather than one PR away.
- [ ] `StopManager` — fixed, ATR, trailing, chandelier, time — @claude (#31).
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

  **And a second caller, which is the gap this item did not name.** Every
  `StopManager` method above had exactly one production caller — the live
  runner. `BacktestEngine` watched only the levels a `Signal` happened to carry,
  and no shipped strategy emits one, so a strategy configured behind
  `WORKER_STOP_TYPE=atr` was backtested naked. Not a missing feature: a
  *divergence*, of the kind CLAUDE.md §5 calls the hardest here to notice,
  because the backtest reported a number belonging to a strategy nobody was
  going to run. `BacktestRunSpec` now carries the stop, `--stop` and the
  Backtests tab set it, and a spec stored without one still resolves to the
  unprotected run it was.

  Three things moved rather than being reimplemented, which is the whole point.
  `target_hit` had a private copy in the engine and another in the runner and
  now lives in `risk/stops.py` with both calling it. `should_trigger` replaces
  the engine's own inline comparison. And the ordering matches live: the stop is
  derived before sizing, because `risk_pct` is defined off the distance to it —
  which is why a `risk_pct` run over a stopless strategy books a refusal per
  entry, and why `--stop atr` is what fixes that rather than a sizing default.

  The ATR that places the level goes through the same cursor a strategy reads,
  so it cannot see the volatility it is about to be measured against; a stop it
  cannot derive leaves the position openly unprotected rather than armed at an
  invented level. `broker_side` is False on every backtest stop, because there
  is no venue in a replay and a config claiming otherwise would report a
  protection the run does not provide.

  Unticked all the same, and for a sharper reason than before: broker-side stops
  are docs/SAFETY.md's layer 5, and a layer is only demonstrated by watching it
  hold. Nothing has yet placed one of these against a real venue, and four of
  the nine rules can refuse a protective stop — which the router reports rather
  than hides, but which no *Verifiable:* line yet exercises. Phase 4's paper
  week is the demonstration.

  That reason is untouched by the above, and this PR does not weaken it. A
  backtest places no broker-side stop by construction, so watching one hold at a
  venue is still the thing that has not happened. What changed is the scope of
  what is left: the engine-side half now has two callers and is exercised on
  every level type, long and short, and Phase 4's paper week is the only
  outstanding demonstration rather than one of two.
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
  the reconciler. Reconciliation mismatch is wired as of the reconciliation item
  in Phase 4 — the 5-minute job now exists and engages through the reconciler —
  leaving three.

  `RISK.md`'s `flatten_all_positions` is **no longer a stub and no longer a
  function**. The act it described exists as `POST /api/v1/risk/flatten-all`,
  which is where ADR 0005 puts it: the carve-out that ADR defends is a *human*
  calling `BrokerPort.close_all_positions()` behind a typed confirmation, a
  step-up password and an audit row, and it ends "no automated path may call
  either method". A module-level function in the risk layer is reachable by
  every automated path there is. What kept it separate from `engage()` is
  unchanged and still true — halting stops new risk, flattening realises P&L
  into a market you may not be able to see — which is why the endpoint reports
  whether the platform was halted when it ran rather than assuming it was.

  **The operator path was its own gap and this file did not name it.** This item
  built the mechanism and Phase 5 built the screen; nobody owned the wire
  between them, so `POST /api/v1/risk/halt` raised `NotImplementedError` while
  the dashboard shipped a `HALT TRADING` button that called it — a safety
  control that looked live and returned a 500. Closed by #70, which is not
  ticked anywhere on purpose: it is covered by unit tests against a fake switch
  and has never engaged a halt in a real Redis from a real browser, which is the
  demonstration this file asks for. Clearing from the UI was still not built at
  that point — `/risk/resume` was a stub wanting a step-up password no screen
  asked for. It is built now (#75), with the same caveat and for the same
  reason: fakes and an ASGI transport, never a real Redis from a real browser.

  **The same gap held the close-out path, and this file did not name that
  either.** `POST /risk/flatten-all` authenticated, demanded the confirmation
  phrase and the step-up password, and then raised — so the runbook's "Emergency
  flatten" sent an operator at an endpoint that could not flatten anything, and
  said nothing about it. docs/FIRST_PAPER_RUN.md was the only document honest
  about it. Closed now, together with `POST /positions/{symbol}/close`,
  `DELETE /orders/{id}` and `POST /orders/cancel-all`, and the split between
  them is ADR 0005's: the three ordinary ones go through `OrderRouter` and can
  be refused by a rule, and only `/flatten-all` reaches the venue around the
  chain. Both operator documents now describe what the endpoints actually do,
  including that a refused close is a `200` an operator has to read. Same
  caveat as the halt path above and for the same reason: fakes and an ASGI
  transport, never a real venue.

  **The read half followed in the same PR.** `/risk/limits` serves the
  configured ceilings from config alone — no store, so it answers during the
  incident when an operator most wants to know what the limits are — and
  `/risk/status` reports usage against each of them from the worker's published
  book, mirroring every rule's own comparison including the boundaries the
  rules deliberately disagree on. Both are read by a panel on the **Strategies**
  tab rather than by a Risk tab of their own: `/status` is what a person checks
  before promoting a strategy, so it belongs on the screen that decision is made
  on, and the nav stays at seven tabs. `/limits` is fetched only when `/status`
  has failed, which is the case it exists for — it touches no store, so the
  ceilings still render when the book cannot be read.

  One limit is reported as **structurally unobservable** rather than as a
  number, and it is worth recording why here because the same shape blocks the
  daily report below. `RateLimitRule` keeps its window in the worker's own
  process and counts orders on the *attempt*, before the rules after it vote —
  and a refused attempt is never persisted as an order, because
  `runner._persist` walks the open-order set that a refusal never enters. Its
  record is a **signal** instead. So a rate taken from the `orders` table would
  read as calm during precisely the runaway the limit exists to catch, and
  understating it is the direction that makes a breached limit look compliant.

  **`/risk/rejections` is that same fact read from the other side** (#77). The
  record exists; it is kept as a decision rather than as an order, so the
  endpoint reads `signals`. Three things had to be true for it to be worth
  having. It filters in SQL, because taking the newest hundred signals and
  keeping the refused ones answers "were any of the last hundred decisions
  refused" — which is "no" for a strategy blocked all week that has since
  emitted one HOLD. It excludes `no_action`, which the router marks *approved*
  precisely so holds do not inflate the count an operator reads to judge
  whether risk is too tight. And `rejected_by` became a real column
  (`f4d2e8b1a075`): it had been packed into `rejection_reason` as
  `"[rule] reason"` on the grounds that a column was "not worth a migration for
  one string", which was true only while nothing queried it — against the
  packed form, excluding one rule is a `LIKE` on a bracketed prefix that also
  matches any reason text beginning with a bracket.

  **A gap that endpoint could not close, and #78 closed.** The runner has four
  refusal paths and recorded one. `_record_signal` writes a row for every signal
  whatever its fate; a **stop exit**, a **protective stop** and a **shutdown
  flatten** the risk chain denied were logged
  (`runner.stop_exit_refused`, `runner.position_unprotected`,
  `runner.shutdown_flatten_refused`) and stored nowhere — none is a signal, and
  none of their orders was ever tracked, so `_persist` never saw them. These are
  the more serious three: a refused entry is a trade that did not happen, a
  refused stop exit is a position that should have closed and did not, and a
  refused protective stop is one that never had a stop — docs/SAFETY.md's layer
  5 failing at both ends, silently.

  All four now store the order they refused, which `GET /orders` was already
  built to render and had never once been given. The endpoint's docstring says
  "a rejection appears in no other read in the platform" and
  `OrderHistoryTable` tints `rejected_risk`; the read path was complete and the
  write path did not exist, so the table's most important category of row could
  not occur. The write is deliberately swallowed on failure rather than raised:
  it happens on the way *out*, about something that already happened, and three
  failed evaluations halt trading — so raising would let the record of a refused
  stop become the thing that stops the platform.

  **And the row could not name its refuser until #79.** #78 gave `/orders` its
  first refused row; the rule that refused it was still dropped one layer down.
  `RiskDecision` carries `rule` beside `reason` and `OrderRouter._route` passed
  only the reason into `transition()`, so the rule reached the structured log
  and never the table. A reason on its own does not identify a limit — three
  rules refuse with "no price available for SPY" — and the rule name is what
  gets a reader from a refusal to the ceiling that predicted it, which the risk
  limits panel is laid out for and had no counterpart to be read against.

  `orders.rejected_by` (`b8e3f01c7d24`) is set inside `transition()` beside
  `reject_reason`, on the same condition and from the same call, because the two
  drifting apart is how the rule came to be logged and the reason stored. It
  holds a rule name, the pre-rule stage `routing` where the chain approved and
  left nothing to trade, or the broker where the venue refused — `status` says
  which vocabulary applies, which is what keeps refusals countable by rule while
  one column answers "who refused this order". All three venue paths fill it,
  including the pushed one: the name travels on `TradeUpdate` because the runner
  consumes that stream and reaches a broker only through the router (rule §1.5).

  Unlike `signals.rejected_by`, **there was nothing to backfill**. That column
  was reconstructed by unpacking `"[rule] reason"`; an order's reason never
  carried the rule, so rows written before this one are null permanently and
  the table says so rather than leaving the line blank.

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
- [ ] `BrokerPort` + Alpaca adapter (paper first) — @claude (#36).
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
- [ ] `OrderRouter`, order state machine — @claude (#33).
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
- [ ] `SimulatedBroker` — @claude (#36).
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
- [ ] Reconciliation — @claude (#38).
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

  **The schedule is wired now**, and it was the last structural gap in layer 7.
  This module's own docstring has always said reconciliation runs "at startup,
  on a schedule, and after any reconnect"; two of those three were built — the
  runner reconciles in `warmup`, and `trading.consume_trade_updates` does it
  again whenever the trade-updates socket returns — and the middle one was a
  `NotImplementedError` stub. Between a clean start and the next reconnect,
  nothing checked the book at all, which is precisely the failure mode
  docs/SAFETY.md names for this layer: "reconciliation itself is not running".

  The blocker was never a missing component; it was that the job had nothing to
  *compare against*. A snapshot loaded from Postgres is not the live book, and
  reconciling the venue against a minute-old copy reports every fill in between
  as a mismatch — a halt caused by reading rather than by drift. It needed the
  runner's own portfolio, handed over rather than fetched. That is
  `scheduler.SessionJobs`: the runner's `Portfolio` by reference, and
  `open_orders` as a *callable* so each run asks what is working now rather than
  reporting every order placed since the wiring as an orphan. `build_schedule`
  adds the entry only when this worker is actually trading, so a worker with no
  strategy configured no longer carries a job that would fail every five minutes
  for want of a book.

  Still unticked, and this does not change why: Phase 4 has no line short of the
  paper week, and nothing here has met a real venue. What a mismatch does when
  it finds one is now pinned by tests over the real `Reconciler` — a divergence
  halts, and it halts without raising, because a job that reported its own
  success as a failure would put a traceback in the log every five minutes for
  as long as the halt stood.
- [ ] Declarative rule sets compile and run — @claude (#93).
  `compile_ruleset` and `RuleSet.required_warmup` were the two
  `NotImplementedError` stubs standing between a fully specified, fully
  validated rule DSL and one that could execute. The spec models had shipped;
  nothing could turn one into a `Strategy`, so no rule set had ever been
  backtested. Both are implemented, and what comes out is an ordinary
  `Strategy` — the engine, the runner and live drive it through the path they
  drive `SmaCrossover` through, with no branch anywhere downstream.

  Three decisions are worth a reviewer's eye, because in each case the easier
  version is the one that fails quietly rather than loudly:

  - **Warmup is asked of `dispatch`, not assumed to be the period.** `rsi` and
    `atr` need `period + 1` bars — Wilder's smoothing averages differences
    between consecutive closes. Assuming the period is not an off-by-one in a
    warmup count: the compiled strategy sizes its history window off the same
    number, and a window fixed one bar short never grows, so `compute` answers
    None for the *entire run* and the rule never fires. An empty result, from a
    spec that reads like it should trade. `dispatch.min_bars` states the
    minimum where a caller can read it rather than putting a second copy of
    `ta`'s lengths in `rules.py` (ADR 0006). `StopSpec.period` goes through the
    same call, so an ATR(50) stop asks for 51 bars rather than 50.
  - **Conditions are three-valued.** An operand that cannot be computed yet is
    unknown, not false. `none: [rsi(14) < 30]` means "not oversold"; collapsed
    to two values it holds on the first bar of every run, on the grounds that a
    number which does not exist is not below 30.
  - **Anything unrunnable is refused at compile time.** An unknown indicator, a
    missing period, `field:` other than `close`, extra indicator `params`, an
    empty condition group, `flatten_at_close`. None of these crashes if waved
    through — they produce a plausible equity curve answering a different
    question, which is the one failure a backtest cannot survive.

  Two gaps are stated rather than left to be found. The `risk` block is read
  only for warmup: a compiled rule set emits no stop level of its own, because
  the run's `stop_config` already derives one and a single level with two
  sources is the divergence ADR 0006 exists to prevent — so wiring `spec.risk`
  into a run's configuration is still manual, and a rule set sized by
  `risk_pct` needs its stop passed to the run explicitly. And `flatten_at_close`
  is refused rather than modelled, since a strategy cannot read the clock
  (§1.5).

  Unticked, and the reasons have moved on since #93 wrote this. A reference
  spec now does ship — `strategy/examples/rsi_mean_reversion.py` carries the
  authoring guide's worked example as the YAML a reader would paste, and
  `tests/unit/test_rsi_mean_reversion.py` runs it end to end: a 200-bar warmup,
  an entry on the dip inside an uptrend, an exit on the bounce, and silence on
  a downtrend where the RSI leg alone would have bought. So the sentence this
  paragraph used to carry — that no rule set ships and only test fixtures have
  executed — is no longer true.

  Two things still stand between that and a tick, and the first is new
  information rather than a restatement:

  - ~~**Nothing resolves a stored rule set into a run.**~~ Closed by #96.
    `BacktestRunSpec` gained a `ruleset` field, `build_engine` compiles it when
    present, and `POST /api/v1/backtests` copies a `kind="ruleset"` row's rules
    onto the run. A **snapshot, not a reference**, and that is the decision to
    review: a rule set is editable in the UI — that is what it is for — so a run
    recording only `strategy_id` would replay differently the day somebody moved
    a threshold, silently, with both numbers filed under one name. The id keeps
    the foreign key and answers "which strategy is this a run of"; the snapshot
    answers "what rules actually ran", and those stop being one question the
    first time a rule set is edited.

    Four refusals land at the door rather than minutes later on a row that says
    `failed`: a declarative row with no rules, params sent alongside one, rules
    that no longer compile, and a run whose symbols do not meet the rule set's
    `universe`. The last is the valuable one — a compiled rule set ignores
    symbols outside its universe, so such a run completes, takes no trades, and
    reports a flat curve indistinguishable from a strategy that never signalled.

    Two things this did **not** close. ~~**Nothing can create a rule-set row.**~~
    Closed by #97, the strategy-creation item below: `POST /api/v1/strategies`
    stores one, through a `NewStrategy` write type that carries the whole row
    rather than the thin record `ensure` accepts. So the path from a rule set to a
    result is complete at both ends for the first time. Second, the `risk` block
    is still read only for warmup, so a run of a rule set must be configured with
    the ATR stop its own spec asks for or `risk_pct` refuses every entry at
    sizing.
  - **Nothing has run a rule set on the paper endpoint**, which is this phase's
    *Verifiable:* line and unchanged. That is what still holds the tick.

  A run of this spec also has to be configured with the ATR stop the spec asks
  for, because a compiled rule set emits no protective level of its own — the
  gap #93 named, now with a concrete case: without that stop, `risk_pct` refuses
  every entry at sizing, which `test_without_that_stop_every_entry_is_refused_at_sizing`
  pins so the failure is a documented refusal rather than a surprise.
- [ ] Strategy creation endpoint — @claude (#97).
  **An item this phase was missing**, added in the PR that built it. Requirement
  #1 is "strategy CRUD and lifecycle"; the roadmap tracked the listing screen
  (Phase 5) and the rule-set compiler (above) and nothing tracked the write half
  — which is how its absence came to be recorded as a *blocker* on another item
  rather than as work.

  `POST /api/v1/strategies` stores a coded strategy or a declarative rule set at
  `draft`. `StrategyRepository` gained `create`, and `strategy/ports.py` a third
  type over one table: `NewStrategy` is the whole row **minus the columns the
  platform decides**, beside `StrategyRecord` (what a booting worker knows) and
  `StoredStrategy` (what a reader needs). The split is by who is the authority
  on which column, and it is what makes the ratchet a property of the type
  rather than a check: there is no `state` field anywhere on the write path, so
  nothing between the request and the INSERT could carry a rung above the first.

  **Two writers on one table, and they do not fight.** A worker's `ensure` is an
  upsert that touches only `updated_at` on a row it finds, so running an
  authored strategy cannot overwrite what was authored — which matters most for
  a rule set, since a compiled one reaches the runner as an ordinary `Strategy`
  and the row it would ensure says `kind="coded"` and carries no rules. Pinned
  against a real PostgreSQL rather than asserted.

  **Most of the endpoint is refusals**, each of them a failure that otherwise
  arrives later from another process. The three worth review: a spec whose name
  disagrees with its row (the compiled class stamps `spec.name` on every signal,
  so every decision it recorded would fail its foreign key); a rule set taking a
  registered class's name (both would file signals under one `strategy_id`, with
  their attribution silently merged); and a universe that is not uppercase
  (matched against `bar.symbol` exactly, so it compiles, runs, takes no trade and
  reports a flat curve). `risk_config` is refused rather than stored, because
  nothing in the platform reads that column and a limit on screen that no order
  is checked against is worse than no limit shown.

  `strategy_created` is the audit trail's first lifecycle verb, landing with its
  handler as ADR 0010 says these do. That removes one of the two reasons the
  promote stub gives for staying a stub — the mechanism for an entry naming a
  human is now wired and demonstrated — and leaves its own work: a verb per
  transition, and a paper-trading period nothing yet records the start of.

  **A gap this makes ordinary rather than introduces.** `last_started_at` on the
  Strategies tab is `strategies.updated_at`, and a create writes it equal to
  `created_at` — as does a worker's first boot, so one column cannot tell "never
  started" from "started once, when it was made". Every row `scripts/seed.py`
  writes has had that property all along; authoring makes it the common case,
  and it also reaches `available[].has_run`, which is the field the Strategies
  tab exists for. The fix is a `last_started_at` column only `ensure` bumps,
  nullable, backfilled from `updated_at` — a migration, not a rename — and it is
  deliberately not in this change: it touches the read model, the screen and the
  generated types, and folding it into the authoring PR would put two reviews in
  one diff.

  Unticked, and not close. This phase's *Verifiable:* line is a week of paper
  trading, and nothing here trades. What has been shown is 20 API tests over
  ASGI and 4 against a real PostgreSQL in CI; what has not is a strategy created
  through this endpoint being picked up by a worker, which is the whole point of
  storing one and needs a running stack to demonstrate.

  **The rest of the lifecycle is now its own item, below.** This entry used to
  end by calling the four stubs and the missing authoring form "the next piece
  of this item rather than a separate one", and carried `wip #97` on the
  strength of it — one item describing a built endpoint and an unbuilt half at
  the same time, so no single marker could be true of it. #97 merged, which
  makes `wip` false; the endpoint is built, which makes "not started" false.
  Splitting it lets both halves say what they are: this one is built and waiting
  on the phase's *Verifiable:* line, and the work that is genuinely unbuilt is
  unclaimed rather than dressed as in progress (#127).
- [ ] Strategy lifecycle verbs and an authoring form.
  Split out of the item above, where it had been described as its next piece.
  Nobody is on it.

  `PATCH /strategies/{id}` (editing a live strategy needs pausing it first, and
  pausing is a stub), `POST /{id}/promote`, `POST /{id}/pause` and `GET /{id}`
  are all still stubs, and there is **no authoring form** — the Strategies tab
  is read-only, so a rule set is posted from a client rather than written on the
  screen that lists it.

  Promotion is the one with a prerequisite rather than just work: ADR 0010 wants
  an audit entry naming a human for a lifecycle transition, and #97 wired and
  demonstrated that mechanism, which removed one of the two reasons the stub
  gives for staying a stub. What it still needs is a verb per transition and a
  paper-trading period nothing yet records the start of.
- [ ] `StrategyRunner` live loop — @claude (#39).
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

  **The mirror was not complete, and #58 found the hole while building
  attribution on top of it.** The claim above — that the loop runs the
  documented ordering and that a divergence from the engine makes every backtest
  a claim about a system that does not exist — was pinned by a test on the
  *order* of the steps, and step 2's *content* had drifted:
  `BacktestEngine._check_stops` resolves a take-profit against the bar and names
  `take_profit` as an exit reason, while `_exit_reason` here only ever returned
  `stop_loss` or `time_exit`. So an armed target was never acted on live. The
  level was real and persisted — the router arms one from any signal or
  `StopConfig` that carries it, and migration `a1c4e77b91d2` added the column so
  it survives a restart — and nothing looked at it. A strategy backtested with a
  target and run live without one is not the same strategy.

  Closed in #58, with the engine's pessimistic tie-break carried across
  verbatim: when one bar's range spans both levels the stop is assumed to have
  filled first, because the bar cannot say which came first and assuming the
  target would make every live report and every backtest that agreed with it
  flatter than the truth. Verified by mutation — removing the take-profit check
  fails one test, and flipping the tie-break fails another.

  Worth naming as a class rather than as an incident: this drifted because the
  parity test read the *sequence* of the steps and not what each step did. The
  ordering test is still the right test; it is just not the whole of the
  property it appears to guarantee.

  Unticked. Phase 4's *Verifiable:* line is a paper week, and this loop has
  never been pointed at a venue — every test drives it off fakes.
- [ ] Trade-updates WS with reconnect — @claude (#37).
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
- [ ] Worker wired to trade — @claude (#41).
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

  *(Locks 1 and 3 are no longer environment variables — they are columns on the
  `worker_config` row the dashboard writes, and arming the third costs the
  operator's password. See "Worker configuration endpoint and screen" in Phase
  5. The checks, their order and their reasoning are unchanged; only where the
  values come from is.)*

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

  **The PR number on this line was #40, which never merged.** It was closed at
  01:21:07 and #41 — same branch, same title — merged at 01:57:46. The work
  landed; the marker pointed at the abandoned attempt, which reads as work
  dropped rather than work done. Corrected to #41.
- [ ] Order and position persistence — @claude (#44).
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

**Not shown, and the two tools that would let it be are built** — @claude (#83).
Nothing in Phase 4 has met Alpaca; this PR does not change that and could not.
What it changes is what happens on either side of the week, because the input
this line needs and cannot re-run is calendar time.

`scripts/preflight.py` (`make preflight`) checks eleven preconditions in about
two seconds — every one already stated in docs/FIRST_PAPER_RUN.md, in prose or
in its "most likely to break first" list. The two that matter are the two that
produce a *silent* week, which that document warns is indistinguishable from a
strategy that correctly never signalled: warmup history shorter than the
strategy needs, and a size the position cap refuses. The second is the
interaction #82 surfaced — `risk_pct` at 1% against a 2×ATR stop asks for ~30%
of a $100k account against a 10% `RISK_MAX_POSITION_PCT` — priced through
`position_size`, the same call the router makes, so the prediction is about this
platform rather than a similar one. The decisions are pure functions in
`atp_worker.preflight`; the script is the I/O.

`scripts/paper_report.py` (`make paper-report`) answers this line's four clauses
from the record, with the counts behind each. It exists because the recording
section of docs/FIRST_PAPER_RUN.md asks for numbers rather than a conclusion and
nothing produced them, so the tick after a week would have been somebody's
recollection of a log tail.

**Two of the four clauses have no store behind them, which this file did not
record.** `execution.reconcile.clean` and `runner.position_unprotected` are log
lines: no table, no metric, no audit row — and the audit log is the wrong home,
since that record attributes an action to a *person* (ADR 0008) and a
reconciliation has no actor. So the report renders those clauses `[?]`, prints
the grep, and exits non-zero; `--logs <file>` counts them and answers all four.
`[?]` is deliberately not `[ ]`: unshown is not shown-false, and a report that
rendered "no unprotected positions found" out of a store that never held them
would be believed. Making them durable is a real gap and a separate change — it
wants a decision about where system events live, not a column bolted onto a
table that is about something else.

One more thing it refuses to flatter: when nothing filled, the stop clause is
`[?]` rather than `[x]`. No position was ever held, so SAFETY.md's layer 5 was
never asked to hold.

Three corrections landed with it, each a doc that had drifted in the direction
that makes the platform look *less* safe or *more* proven than it is.
FIRST_PAPER_RUN.md's "how to stop" said the dashboard HALT button did not exist
and `/risk/halt` raised `NotImplementedError` — both closed in #70 and #75, so
an operator reading it under pressure believed they had one stopping mechanism
when they had two. `status.py` said there was no order or position repository
(#44). `/risk/flatten-all` really was still a stub at that point, and the text
was corrected to say exactly how far it got before raising — the honest state
then. It is built now (see the kill-switch item in Phase 3), and both operator
documents were corrected again in the same diff that built it.

And one real defect, found by running the new script: `ALERT_NTFY_TOPIC`,
`ALERT_NTFY_TOKEN` and `ALERT_TELEGRAM_TOKEN` were plain `str` while every other
credential in `Settings` is `SecretStr`. `repr(Settings)` therefore rendered
them in full, and SQLAlchemy puts that repr into an `ArgumentError` message — so
a mistyped database URL printed a live bot token to the terminal. config.py's
own prose already said "the topic **is** a credential" and "the bot token **is**
the bot"; the types now agree with it. `scripts/preflight.py` also renders an
exception's *type* and never its message, for the same reason.


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
- [ ] `/dashboard/live` aggregate endpoint — @claude (#45).
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
- [ ] React dashboard, on-demand refresh + WS — @claude (#45).
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
  published.

  **The 5-minute cadence is gone** (ADR 0022): the reader reloads, or uses the
  button on the indicator. The item is retitled rather than re-scoped — what
  changed is the requirement, not the amount built. The cadence had been
  carrying something nobody had written down, which is why this was more than a
  deletion: every age on the screen was computed during render, so the poll was
  what made it advance. Both ages now run on their own ticker, and the book's is
  shown as its age *now* — the server's number plus the time since the read —
  because a frozen one could not warn about an outage that started after the
  read. That is the tab-left-open-across-a-sleeping-laptop case, which on this
  host (ADR 0021) is the failure mode that decides a paper week.

  **This phase's *Verifiable:* line is unaffected and was the constraint.** It
  asks that the age of the book be on the screen *and advance*, and that halting
  make the banner appear without a reload. A naive removal would have broken
  both; halts stayed on the socket's push path and the ticker keeps the age
  moving, so the line stands as written and this item stays unticked for the
  reason it already was — no worker has published a book to agree with. With nothing trading, the screen correctly reports "No book
  published", which demonstrates the null-book path and says nothing whatever
  about agreement between the worker's numbers and the screen's — which is the
  only property that makes the screen worth reading.

  **That push path was narrower than the sentence above implies, and no longer
  is.** The socket was opened by the dashboard *screen*, so React Router closed
  it on navigation: `HaltBanner` sits above the nav on all seven routes and was
  fed on exactly one, and the API logged `clients=0` for the other six. A halt
  reached the banner without a reload only while somebody happened to be looking
  at `/`. It is held for the signed-in session now, and a reconnect re-reads the
  book, because pub/sub has no replay and with nothing polling a gap nobody
  repairs is permanent. Neither changes what this line asks for, or whether it
  has been shown — the worker still has not published a book.
- [ ] Trade reconstruction, attribution, MAE/MFE — @claude (#58).
  Built: `PerformanceAnalyzer` folds stored fills into round trips, measures
  MAE/MFE against bars, and groups P&L five ways; `/analytics/performance`,
  `/analytics/trades` and `/analytics/attribution` serve it. docs/ANALYTICS.md
  and ADR 0015.

  **Both prerequisites #45 named are closed.** `SignalRow` has a writer
  (`SignalRepository`), a `strategies` row is created by the runner at every
  session open, and `PostgresOrderRepository` stores real `strategy_id` and
  `signal_id` instead of the literal `None` it had been storing since #44 — so
  an order can be traced to the decision that caused it, which is the whole of
  attribution. The foreign keys make that non-optional rather than
  best-effort: a runner that skips either write now gets an integrity error
  where it used to get a null, and a null is how the gap stayed invisible for
  four phases.

  Two things this had to add before it could report anything honest, both
  found by building it:

  - **`orders.purpose`** (migration `c3f8b2d5e714`). `OrderRequest.purpose`
    existed and was already load-bearing — it is part of the `client_order_id`
    derivation — but it was consumed by that derivation and dropped, and a
    SHA-256 digest cannot be read backwards. Every engine-side exit reaches the
    venue as `router.flatten`, so without it a stop, a target and a time exit
    stored identically and the most actionable table in the platform would have
    had one bucket. `flatten` gained a `purpose` parameter to match, which
    changes the idempotency key of an engine-side exit — a correction, since a
    stop and a time exit firing on the same bar are two decisions and one key
    for both would silently drop the second.
  - **A live-vs-backtest divergence in the runner**, recorded against
    `StrategyRunner` in Phase 4 as well. The engine resolved take-profits and
    the live loop never looked at one.

  A decision worth a reviewer's eye, argued in ADR 0015: **a trade is a
  position episode** — flat, through any number of scale-ins and partial exits,
  back to flat — rather than a tax lot. That is what makes `exit_reason` a
  single answer and the holding period a well-defined window, and it narrows
  the FIFO/LIFO question to one place: a fill that carries a position *through*
  zero, where the split is FIFO and the fee is pro rata. The invariant that
  matters is a hypothesis property, as docs/TESTING.md asks — over any
  generated fill sequence, the reconstructed trades sum to the P&L the fills
  themselves produce.

  The second decision is that these endpoints **reconstruct on request** rather
  than serving something the worker published, which is the opposite of ADR
  0007 and needs its reason stated: that ADR is about a quantity still moving,
  and two processes computing a *closed* round trip from the same stored fills
  cannot disagree. The cost is a read that grows with the account's lifetime;
  ADR 0015 names the threshold and says the fix is a stored trade table, not a
  truncated read — a truncated read does not get slower, it gets wrong.

  The SQL is exercised against a real PostgreSQL 16 with TimescaleDB 2.15.2 —
  the version `docker-compose.yml` pins — by 21 integration tests, and the whole
  suite is green with nothing skipped (1,467 Python, 81 web). The three that
  earn their keep are the refusals: an order naming a signal nobody recorded is
  rejected by the foreign key, a signal naming an unregistered strategy is
  rejected by the other one, and a second `ensure` does not overwrite the row a
  first boot wrote. Each was verified by mutation — restoring the hardcoded
  `None` fails the first two, and a naive upsert fails the third.

  **These endpoints have a screen now** (#63). `/analytics` in the dashboard renders
  all three — the metric set, the attribution breakdown with its dimension
  selector, and the closed-trade list with MAE/MFE — and it is a front-end
  change only: no handler, model or query was touched to put it there.
  docs/ANALYTICS.md had listed "No UI" under *Not built yet*, which was the
  same shape of gap the audit page closed in #57: a report nobody can open is a
  report nobody reads, and these had been built and tested with no consumer
  since this item landed.

  The screen is the one place in the app that is deliberately not a single
  aggregate request, and the reason is the same one ADR 0015 gives for
  reconstructing on demand: three reads of a *finished* period cannot disagree.
  Two refusals are worth a reviewer's eye, because the flattering version of
  each is the one that writes itself — a period with no closed trades reports
  that in a sentence rather than as a grid of the zeros `compute_all` correctly
  returns, and the five money-shaped metrics are labelled as the float
  statistics they are rather than formatted with the ledger's formatter, which
  would claim a precision the response does not carry. 17 web tests cover those
  and the rest of the rules; `src/lib/stats.ts` holds the float/decimal
  boundary and `money.ts` accepts only strings, so the compiler enforces it.

  It changes nothing about what is ticked below. The proposed analytics
  *Verifiable:* line asks whether the reconstruction agrees with the broker's
  own statement after a paper week, which is a claim about the numbers and not
  about the screen showing them.

  Unticked all the same, and the reason has narrowed to exactly one thing.
  This phase's *Verifiable:* line is about the dashboard and cannot demonstrate
  any of it; a line these items can be held against is proposed below. What has
  been shown is that the schema, the constraints and the fold behave against a
  real database on fixtures. What has **not** been shown is any of it against a
  database holding a real strategy's history, which is what the proposed line
  asks for and what the paper week produces.
- [ ] Order history endpoint and screen — @claude.
  **An item this phase was missing.** Phase 5 tracked the live dashboard and the
  analytics endpoints; the nav has seven tabs and nothing here covered the other
  five. Added rather than folded into "React dashboard", which is about the live
  book and has its own *Verifiable:* line.

  `GET /api/v1/orders` and the `/orders` tab. The endpoint was one of four
  routers whose every handler raised `NotImplementedError`; this builds the read
  and leaves the writes alone, because a write places something and there is
  exactly one path from an intent to a venue (rule §1.5, ADR 0005) — which is
  also the path carrying the audit events ADR 0010 is waiting on. That ADR's
  statement that the order-flow handlers are stubs is unchanged: a read is not
  one of the verbs it names, and it decided against recording every request.

  `OrderRepository` gained `recent_orders`, and its shape is the opposite of
  `filled_orders` in two ways that are worth a reviewer's eye because the port's
  own docstring warns against both. It is **newest-first**, where the other is
  oldest-first so FIFO matching can pair an exit with its entry. It is
  **bounded**, where the other refuses a limit because a truncated read of a
  reconstruction does not get slower, it gets wrong. Neither applies to a
  display: nothing here is matched against anything, and dropping the oldest
  rows loses rows off the bottom of a screen rather than inverting the sign of a
  P&L.

  What it is for: **an order that was refused appears in no other read in this
  platform.** It moved no quantity, so `filled_orders` excludes it, no round
  trip contains it, the book never held it and the equity curve never moved for
  it. A strategy refused every morning for a month was, from the dashboard and
  from analytics alike, indistinguishable from one that never placed an order.

  Unticked. The SQL is covered by ten integration tests against a real
  PostgreSQL in CI, and the handler and screen by 12 API and 18 web tests — but
  every row in all of them is a fixture. Nothing here has yet displayed an order
  a *worker* placed and a real risk rule refused, which is what the line below
  asks for. #79 narrowed the gap by one step without closing it: the screen can
  now name the rule that refused a row, so what the paper week has to show is a
  refusal displayed under the name of the rule that actually made it — but the
  rows demonstrating it are still fixtures.

- [ ] Stored-book positions endpoint and screen — @claude.
  `GET /api/v1/positions` and the `/positions` tab. Third of the five stub tabs,
  after `/analytics` (#63) and `/orders` (#64), and the same posture: the read is
  built and the writes are left alone.

  **The decision worth reviewing is the source.** The dashboard reads the book
  the worker publishes to Redis; when the worker stops, there is nothing to read
  and `/dashboard/live` correctly reports no book. The same book is written to
  `equity_snapshots` and `position_snapshots` at every evaluation, and that copy
  survives the process — so this screen answers "what am I holding?" at the
  moment the live one cannot, which is the moment it is asked.

  That is not the recomputation ADR 0007 refuses, and the distinction is the
  argument: nothing here adds a position up from orders and quotes. It is the
  worker's own computation, read back from the table the worker wrote it to. The
  cost is that the answer can be arbitrarily old, so `PortfolioRepository` gained
  `latest_snapshot`, which returns the book *with the instant it was written*.
  `latest` stays as it was and now delegates to it: its caller is a runner
  adopting its own last state at boot, for which the age is not a question.

  The age is therefore the first thing on the screen rather than a footnote, and
  past ten minutes — several missed evaluations — the header becomes a warning.
  A stored book rendered as though it were current would be ADR 0007's failure
  moved from a cache to a table, by the screen built to avoid it.

  Two pieces of reuse rather than reimplementation, and the second found a bug.
  `atp_core.dashboard`'s `position_summary` and `account_summary` were made
  public and are now the single source of every derived figure on both screens —
  `_distance_to_stop`'s own docstring says writing it per side would be two
  chances to get a sign wrong, and writing it per *screen* is the same mistake.
  Reusing `PositionsTable` then surfaced a defect in it: `distance_to_stop_pct`
  is null both when there is no stop and when there is a stop but no mark, and
  the component rendered both as "no stop" — putting those words in the same row
  as a stop price. The second case is the more alarming of the two and now says
  so. A web test asserted the old behaviour and was corrected rather than
  deleted.

  Unticked. The stored-book path has three new integration tests against a real
  PostgreSQL in CI and the endpoint and screen have 10 API and 11 web tests, but
  every book in all of them is a fixture. Nothing here has yet shown a real
  worker's book outliving the worker, which is what the line below asks for.

- [ ] Strategy listing endpoint and screen — @claude.
  `GET /api/v1/strategies` and the `/strategies` tab. Fourth of the five stub
  tabs, after `/analytics` (#63), `/orders` (#64) and `/positions` (#65), and the
  same posture: the reads are built and the writes are left alone.

  **What it answers that nothing else could.** A strategy class is registered at
  import time; a `strategies` row is written by the runner at its first session
  open. Those two sets can differ, and with no strategy configured by default
  the ordinary state of a fresh install is a platform with strategies in it and
  nothing running. No screen could say so — "I wrote a strategy and nothing is
  happening" had no answer anywhere in this UI. The endpoint returns both halves
  and the difference, because every client would otherwise compute that diff
  identically and the diff is the whole answer.

  `StrategyRepository` gained `list_all`, returning a new `StoredStrategy`
  rather than the existing `StrategyRecord`. That is the read/write split rather
  than duplication: `StrategyRecord`'s own docstring explains why it stays thin —
  a booting worker must not invent values for fields it is not an authority on —
  and a reader needs the whole row.

  Two columns are served under names that say what they record, because their
  own names do not. `state` is not "is it running now": `ensure` writes it once
  and never revisits it, so a strategy a worker has been running for a month
  still reads whatever the first boot set. `updated_at` is not "last edited": a
  later boot bumps only the timestamp, so it is served as `last_started_at`.
  Both are the same asymmetry in `ensure`, seen from the reading side for the
  first time.

  What that first boot wrote was `"active"` — a string `StrategyState` has never
  contained, into a `String(20)` nothing checked, and the only value any row
  could hold because `ensure` is the platform's only writer of one. So the
  screen's filter offered five options of which four could not match and the
  fifth was not a real rung. Fixed in #76: `draft` on a first boot, a CHECK
  constraint over the enum (migration `e2b6d1a70f93`), the API's filter typed as
  the enum, and the front end's list derived from the generated union so the
  drift cannot silently return.

  **A trap this nearly shipped into.** `@register` runs at import time, so a
  process that has never imported a strategy module has an empty registry — and
  this endpoint would have reported, with total confidence, that the platform has
  no strategies. The worker and the backtest CLI already import
  `atp_core.strategy.examples` for exactly this reason; the API never had to,
  because nothing here read the registry until now. A unit test asserts the
  registry is populated in this process rather than trusting the import to stay.

  Unticked. Four new integration tests against a real PostgreSQL in CI, 11 API
  and 13 web — but every row in all of them is a fixture, and the property this
  screen exists for is about a *real* worker having or not having booted a
  strategy.

- [ ] Worker configuration endpoint and screen — @claude (#124).
  **An item this phase was missing**, added in the PR that built it. `GET` and
  `PUT /api/v1/worker/config`, a `worker_config` table, and the `/worker` tab —
  the newest of the eight tabs, and the first one that *writes* something a
  running process will act on. ADR 0023 has the decision and the four alternatives it rejected.

  **Ten environment variables became a row.** `WORKER_SYMBOLS`,
  `WORKER_MAX_SILENCE_SECONDS`, `WORKER_STRATEGY`, `WORKER_STRATEGY_PARAMS`, the
  sizing pair, the stop triple and `WORKER_ALLOW_LIVE_ORDERS` are gone from
  `.env.example` and from `Settings`. They were the only settings in that class
  an operator changed while the platform was *running*, and an environment
  variable is the wrong home for such a thing three times over: changing one
  needed shell access to the host, nothing recorded who changed it or when, and
  the API could not read them — so every screen that wanted to explain why
  nothing was trading had to say "go and look at a file". `WORKER_METRICS_PORT`
  and `_ADDR` stayed, because a listener's address is what the process is.

  **Two configurations, and the screen shows both.** A worker reads this row
  once, at start — rebuilding a strategy, a stop manager and a market-data
  subscription underneath a half-finished evaluation is not a thing to do while
  holding positions — so saved and running can differ until somebody restarts.
  The worker publishes what it actually loaded (`WorkerStatusStore`, Redis,
  the same shape and reasoning as the book's snapshot store), the API serves it
  beside the saved row, and the tab states the difference. A settings page
  showing only what it can edit would report a stop multiplier no process is
  using, which is the same class of lie as a dashboard rendering an empty book
  because Redis blinked.

  **The third live lock is now armable from a browser, and that is the decision
  to review.** `allow_live_orders` is `docs/SAFETY.md` layer 2a — added to that
  table in this PR, where it had been missing since it was written. Moving it out
  of `.env` moved it within reach of anything holding a session cookie, so it
  arrives with ADR 0009's answer attached: a read-only session cannot touch it,
  arming it demands the operator's password *with the request* (the same
  `require_step_up` `/resume` and `/flatten-all` use, lifted into
  `atp_api.stepup` when it acquired a second caller), and the change is audited
  with its before and after. Turning it **off** asks for nothing, which is the
  asymmetry `/risk/halt` already has and the property that makes the widening
  safe. `ATP_RUN_MODE` and `ATP_ALLOW_LIVE_TRADING` deliberately did not move.

  **Validation lives in one place**, `WorkerConfig.__post_init__`, because the
  API refuses a bad edit and the worker refuses to boot on a bad row and those
  two must agree — a value the API accepts and the worker rejects saves cleanly
  and then kills the process at its next restart, discovered by an operator who
  has just been told the save worked. The rules it enforces are the ones that
  were previously nowhere: a `fixed_pct` stop of 2 is refused as a stop 200%
  below entry, and a `risk_pct` above 0.10 is refused as the misplaced decimal
  point docs/RISK.md's 0.5–2% range makes it.

  Unticked. 31 unit tests on the value object, 21 on the API including every
  branch of the step-up, and 11 on the panel — but every one of them is a
  fixture, and the property this screen exists for is an operator changing a
  setting and a *real* worker booting on it.

- [ ] The risk ceilings join that row, and the tab becomes Config — @claude.
  **An item this phase was missing**, added in the PR that built it. ADR 0025
  has the decision; it is the companion to ADR 0023 above, finishing the job that
  one started.

  **Eight more environment variables became columns.** `RISK_MAX_POSITION_PCT`,
  `RISK_MAX_GROSS_EXPOSURE_PCT`, `RISK_MAX_DAILY_LOSS_PCT`,
  `RISK_MAX_ORDERS_PER_MINUTE`, `RISK_MAX_OPEN_POSITIONS`,
  `RISK_MAX_QUOTE_AGE_SECONDS` and the two `RISK_DEFAULT_*` fallbacks are gone
  from `.env.example` and from `Settings`, which no longer has a `risk` field at
  all. They are nested on `WorkerConfig` as `RiskLimits`, saved by the same
  `PUT /worker/config`, and edited in a risk section under the worker settings
  on the same screen. One row, so one revision, one audit entry and one restart
  notice cover a decision an operator made once.

  **Why they were the worse half to leave behind.** Every argument ADR 0023 made
  applies, and one applies harder: a position limit is *learned* — tuned against
  a book, in response to what trading did — and `.env` is the one place in this
  system a book cannot be seen from. The API also returns the refusals these
  ceilings cause and could not read the number that caused one.

  **The bounds are new, and they close AUDIT.md finding #42.** `RiskLimits` was
  eight bare annotations: `RISK_MAX_POSITION_PCT=10` loaded cleanly and set the
  single-position cap to 1000% of equity, and `RISK_MAX_ORDERS_PER_MINUTE=0`
  denied every order forever. Neither raised, logged, nor appeared in
  `config_problems()`. `RiskLimits.__post_init__` now refuses both, in the one
  place the API and the worker both get their rules from — plus the cross-field
  rule that a single symbol may not be allowed to exceed the whole book's
  ceiling, which is not unsafe but means the operator has a limit they do not
  have.

  **They do not ask for the password, and that is deliberate.**
  `allow_live_orders` grants a capability to an unattended loop; these bound
  orders that are already permitted. A step-up in front of them would make
  *tightening* a ceiling harder than leaving it alone — the direction `/halt`
  never takes. The audit row carrying both numbers and the operator's name is
  what makes a loosening answerable instead.

  **The tab is renamed Worker → Config**, because the screen stopped being about
  the worker: these ceilings bind a manual order typed into the dashboard while
  no worker is running. Every operator-facing pointer at "the Worker tab" moved
  with it — the runbook, the preflight fixes, the worker's own startup hints.

  Unticked, for the same reason as the item above: 47 unit tests on the value
  object, 10 on the published-status round trip, 13 on the API and 7 on the
  panel, and every one of them is a fixture. The property this exists for is an
  operator tightening a ceiling and a *real* order being refused by it.

- [ ] Backtest queue, endpoints and screen — @claude.
  **An item this phase was missing**, added in the PR that built it. `POST
  /api/v1/backtests` with the full arq queue behind it, the four reads, and the
  `/backtests` tab — the last of the seven and the largest. Phase 5 tracked the
  live dashboard and the analytics endpoints; the five stub tabs were added one
  per PR as each landed (#63, #64, #65, #66) and this is the fifth.

  **A third process, and that is the decision to review** (ADR 0016). A backtest
  is minutes of solid synchronous Python. Run inside `apps/worker` it would hold
  that process's event loop for the whole run — no ticks consumed, no bars
  stored, and `StalenessMonitor` eventually halting trading because the feed
  looked dead. It is not dead; the process is busy being a calculator. So
  `apps/worker/queue.py` runs in its own container, one job at a time, and the
  engine runs in a thread even there so arq can keep answering its own health
  check.

  Four decisions worth a reviewer's eye, because in each case the easier version
  is the one that produces a state nobody can act on:

  - **The row is written before the job is enqueued.** A row with no job is a run
    that shows as queued and never progresses — visible and re-queueable. A job
    with no row is a worker that cannot find what it was asked to do and has
    nowhere to write the failure. If the enqueue then fails, the run is marked
    failed and the request answers 503, because "queued" when nothing accepted it
    is the one status a reader cannot act on.
  - **A queued run has not started.** `backtest_runs.started_at` was `NOT NULL`,
    so the only value the API could have written was the current time — making
    every run's reported duration include its queue wait. Migration
    `d7a1c9f4b208` adds `queued_at` and makes `started_at` nullable.
  - **One attempt, plus a startup sweep.** A backtest is deterministic over
    stored bars, so a retry spends the same minutes to reach the same failure —
    arq's default of five would do it four more times. What that leaves uncovered
    is a worker killed mid-run: no retry is coming and the row says `running`
    forever, which the stub's own docstring named as the worst outcome for a user.
    The queue worker sweeps those at startup and records them as *interrupted*
    rather than *failed*, because the run did not fail — the process running it
    stopped existing.
  - **`GET /backtests/compare`, where the skeleton specified a POST.** ADR 0009's
    reason: the scope gate keys off the method, so as a POST a pure read would be
    refused to exactly the read-only session it is for. The alternative was
    widening `deps.READ_ONLY_MAY_CALL`, whose one entry is there for a domain rule
    about halting.

  **Two things found by building it**, both live-vs-backtest divergences:

  - **The engine never set `Order.purpose`.** It defaults to `"entry"`, and the
    trade reconstruction this reuses — the same `build_trades` the live analytics
    use, so a backtested trade and a live one are comparable — reads it to label
    an exit. Every exit the engine produced therefore reconstructed as an exit "by
    signal", stop-outs and targets included: a *wrong* label rather than a missing
    one, on the field that decides whether a strategy's stops are misplaced. Same
    family as the take-profit divergence recorded against `StrategyRunner` in #58.
  - **The queue worker had an empty strategy registry.** `@register` runs at
    import time, and nothing in the queue process imported `strategy.examples` —
    so it would have failed *every* queued run with "unknown strategy" while the
    API, which does import them, accepted the request at the door. The least
    debuggable shape this failure has. Caught by a unit test that drives a real
    engine through the task rather than a fake one.

  Unticked. 24 unit tests on the queued task and the sweep, 38 on the endpoints,
  9 more on the engine's two new behaviours, 20 web tests, and 14 integration
  tests against a real PostgreSQL in CI — but every run in all of them is a
  fixture or a synthetic series. Nothing here has yet queued a backtest over real
  backfilled history from a browser and read the result, which is what this
  phase's *Verifiable:* line asks of a screen and what the proposed line below
  asks of the numbers.

  **One thing standing between this and that line is gone as of the seed
  script.** The 409 above — "a backtest needs a row in `strategies`, which a
  worker writes the first time it loads the strategy" — was not only a confusing
  state, it was a *prerequisite*: on a clean database the picker was empty and
  the only way to fill it was to configure a trading worker with broker
  credentials that a backtest does not need. `make seed` now writes those rows
  (`scripts/seed.py`), so queueing a run from a browser needs a migrated database
  and backfilled bars and nothing else. Still unticked, and deliberately: the
  *Verifiable:* line asks for **real backfilled** history, and the bars this seed
  writes are fabricated ones under reserved test tickers. It removes an obstacle
  to showing the line; it does not show it.

  **And the rest of it is gone as of `POST /backtests` writing that row
  itself.** The seed script closed the clean-database case for development; what
  it could not close was the shape of the picker anywhere else. A strategy was
  offered only if something had already written its row, so the list was an
  accident of deployment — on the tab whose subject is comparing strategies,
  typically one strategy — and `buy_and_hold`, the baseline the phase's own
  numbers are read against, was usually not on it. The endpoint now writes the
  row for a registered class when it queues that class's first run: the same row
  an author would create through `POST /strategies` and the same one the seed
  writes, at `draft`, on the class's declared defaults, claiming no universe, and
  never touching a row that already exists. The picker is the union of the stored
  rows and the registry. A `strategies` row is therefore no longer evidence that
  a worker ran anything — the Strategies tab says *stored* where it said "a
  worker has run this" — while the absence of one still means exactly what it
  did, which is what that tab exists to show.

  **Until #96 a queued run ignored the sizing method and the stop the request
  chose.** `_spec_to_json` wrote nine of `BacktestRunSpec`'s fifteen fields, and
  the six it dropped were `sizing_method`, `sizing_value` and the four `stop_*`
  fields. Since the worker is handed a `run_id` and rebuilds the spec from that
  column and nothing else, a run submitted from the Backtests tab as `risk_pct`
  with an ATR stop *executed* as `fixed_qty` with no stop — the API validated
  the stop config and then discarded it. Nothing failed: the result looked
  exactly like a correct one, which is the same class of error as a backtest run
  with no costs.

  The CLI was never affected — `scripts/run_backtest.py` builds a spec in
  process and hands it straight to `build_engine`, so it never crosses this
  seam. Two things hid it from the tests: `FakeBacktestRunRepository` stores the
  run object in memory, so every unit test of the queue bypassed the serialiser,
  and the integration case named `test_the_spec_survives_the_config_column`
  asserted five fields, all of which happened to be among the nine that
  survived. Both are addressed, and the round trip is now asserted against
  `dataclasses.fields`, so the next field added to the spec cannot be forgotten.

  Runs queued before that fix still read as `fixed_qty` with no stop, which is
  what they ran as — the ask was never recorded and cannot be recovered.

  **And until #104 the screen could not say what a run had been refused.**
  `result_to_storage` returned metrics, the curve and the trades;
  `BacktestResult.warnings` — every per-order refusal the engine booked, the
  coverage shortfalls `_validate` returns, `refusal_summary`'s counting line,
  and the cost and sizing caveats `run_spec` appends — was assembled on every
  queued run and then dropped. The API filled the hole by recomputing warnings
  at read time from the metric set (`suspicious`), which catches the two
  thresholds docs/BACKTESTING.md names and nothing that is not a function of the
  metrics.

  Same seam as #96 and the same shape of failure: what the worker computed and
  what the row keeps had come apart, and nothing failed. A `buy_and_hold` run
  over twenty symbols whose every entry was refused at sizing reported
  `total_return` 0.0, `max_drawdown` 0.0 and `num_trades` 0 — an all-zero metric
  set that is identical whether a strategy was refused everything or never
  signalled — and the tab showed one line, "only 0 trades". The refusals were
  invisible by construction, because they are not in the metrics.

  A `warnings` column now takes them, written in the same `UPDATE` as the rest
  of the result and cleared with it on failure, and `all_warnings` serves the
  derived caveats ahead of the stored ones so `run_spec`'s refusal summary is
  still the last line. Runs stored before it hold `null` rather than `[]` and
  serve exactly what they served before: the warnings were computed and dropped,
  and an empty list would claim they finished clean.

  **The other half of that seam is closed too.** The nine money and count fields
  `to_report()` produces — ending equity, the realised/unrealised split, open
  positions, the fill counts, fees — are stored on the run
  (`backtest_runs.totals`), served on `BacktestOut`, and shown in the run
  panel's *Money* block above the metric grid. This tab used to show a
  `buy_and_hold` return of 202.8% with no way to say that none of it was
  realised and twenty positions were still open; it now carries the CLI's own
  sentence saying exactly that. Split off from #104 deliberately, because it
  needed a schema decision about how money crosses this API, a migration, an
  OpenAPI change and `make gen-types` where the warnings fix needed one JSON
  column. Reasoning in ADR 0019.

  **And until #107 the CLI's own `--out` file could not say what it was a run
  of.** The mirror image of #96, on the other path. Execution was never
  affected — the CLI hands a spec straight to `build_engine` — but the file it
  wrote recorded the strategy, the universe and the window and stopped. The
  cost model, the strategy params, the sizing method and its value and all four
  stop fields were reachable from the command line and preserved nowhere, so
  two exports differing only in `--sizing` were indistinguishable on disk and a
  `--zero-cost` run read as evidence. Where the queued path lost the ask on the
  way *in* and ran the wrong thing, the CLI ran the right thing and lost the
  ask on the way *out*: the numbers were correct and unattributable.
  `_spec_to_json` is now `ports.spec_to_json`, one writer for the `config`
  column and the `--out` file alike with the `dataclasses.fields` assertion
  covering both, so a CLI export and a run exported from this tab carry the
  same spec block.

  **And #109 closed the last of that seam: the `--out` file said what it was a
  run of, and still not how far to trust it.** `to_report()`'s warnings are what
  the run *did* — coverage shortfalls, refusals — and the derived notes from
  `suspicious` were computed, printed to the terminal, and dropped on the way to
  disk, where this tab served them on every read through `all_warnings`. Found
  by diffing the two exports of the `buy_and_hold` run ticked in Phase 2: two
  files identical in all 1,525 equity points and all nineteen metrics, one
  saying `only 0 trades — the statistics mean very little` and the other saying
  `"warnings": []`. The caveat was never missing from an operator's screen, only
  from the artifact that outlives it, which is the half that gets read six
  months later. `build_report` now derives them through the same
  `runner.all_warnings` the API calls.

  **And #110 closed the rest of it.** #109 fixed the half derived from the
  metrics; the other half was that the CLI called `build_engine(...).run(bars)`
  directly rather than `runner.run_spec`, so the three caveats `run_spec`
  attaches — zero-cost, fixed-qty, the refusal summary — reached the terminal
  through the CLI's own prints and never reached `--out` at all. Invisible on
  the `buy_and_hold` run, which earns none of the three, and live on every
  `--zero-cost` export: a file that named the cost model in its `spec` block and
  said nothing about it in its `warnings`, which is a debugging run on disk
  reading as a result.

  The CLI now runs through `run_spec`, so those three ride on the result and
  reach both readers of it. What made this a separate diff from #109 was the
  terminal rather than the file: the CLI states all three itself, two above the
  run — deliberately, because a number a human has already read is a number they
  have already believed — and the refusal summary below the table, where the
  ten-warning cap on the table's own block cannot swallow it. Attaching them to
  the result would have said each thing twice. `stated_separately` is the
  reconciliation: it decides what the table declines to repeat, never what the
  run records, and is matched against `runner`'s constants so a drifting wording
  duplicates a line rather than dropping one.

  Both halves had the same shape and the same root: `to_report()` is the file's
  only account of itself, and the CLI kept saying things to the screen that it
  never put there.

  **And #111 found three more of them, by reading an export rather than the
  code.** #110's own sentence — "the CLI states all three itself" — was true of
  the three it moved and quietly false of the rest: `main` prints six notes, and
  three were still terminal-only. The risk-chain note ("five of the nine rules
  apply to a replay"), the no-stop note, and the open-position note. A CLI export
  of `sma_crossover` over forty symbols carried 131 refusals from three rules and
  no indication anywhere in the file that four more rules were never consulted —
  a chain that reads as complete because the part of it that is missing cannot
  refuse anything and so leaves no trace. The same file ended holding twelve
  positions worth 3,768.70 of unrealised mark, 8% of its total return, which
  `open_positions` and `unrealized_pnl` both recorded and no warning mentioned:
  ADR 0019 put the money on the run and left the sentence about it on screen.

  The risk-chain line is the one caveat attached to **every** run, which is a
  deliberate exception to the rule the fixed-qty note follows. A caveat should
  describe a choice, and there is no choice here to describe: the four absent
  rules need a calendar, a feed clock, a halt state and a runaway loop, and no
  replay over stored bars has any of them. Because all four only ever deny, their
  absence flatters every backtest this platform has ever produced — a live
  account is stopped more often, never less — which is precisely the direction a
  reader will not assume unprompted.

  **The fourth was a constant that had never been attached to anything.**
  `NO_RISK_RULES_WARNING` was defined, documented at length, and referenced by
  nothing; `build_engine`'s docstring promised "the warning goes back on the
  result when they do" and nothing put it back. `with_rules` stopped at
  `build_engine`, so the only way to obtain an engine that refuses nothing went
  around the function that attaches caveats. `run_spec` takes the flag now, which
  makes the switch that removes the rules the switch that says so. A guarantee
  documented and unkept is worse than one never made, because it reads as kept.

  `REPLAY_BLIND_RULES` moved to `risk.engine` beside the function that omits
  those four, so the sentence naming them cannot go on naming four rules after
  the chain starts omitting a different set — pinned against the difference
  between the two chains rather than against a second literal.

- [ ] Live-vs-backtest comparison — @claude.
  Built as of #68: `GET /analytics/live-vs-backtest/{run_id}` serves the live
  metric set, the stored backtest's, the divergence between them, and the reasons
  not to read that divergence as performance.

  **The blocker recorded here was the *choice of which run*, and it is answered
  by making the caller name it.** The path takes a backtest run id, and the
  strategy is read off that run's spec rather than passed alongside — so the two
  halves cannot be about different strategies, because only one of them is ever
  specified. Nothing is guessed, and the alternative that was actually wrong
  (comparing against the newest run, or running a backtest inside the request)
  is not reachable from this shape.

  What that does **not** answer is whether the named run is the one the promotion
  was granted against. That still needs the ratchet to record it, and it is still
  blocked on the audit trail's lifecycle verbs (ADR 0010). The endpoint cannot
  detect an unrepresentative run, so it says so in its own docstring and in
  docs/ANALYTICS.md rather than implying an authority it does not have.

  **Three things found by building it:**

  - **`compare_to_backtest` raised on the runs most worth comparing.** It did
    `float(theirs[name])`, and a stored run's metrics come back from a JSON
    column with every non-finite value nulled by `runner.jsonable` — `Infinity`
    is not legal JSON. An infinite `profit_factor` means the backtest had no
    losing trade, which is exactly the result somebody holds a live record up
    against. Now either side may be a mapping and either may have a hole in it;
    the divergence is `None` there, meaning *not available* and never zero.
  - **The two sides annualise on different bases, and the artefact flatters
    live.** The engine scales a backtest by its bar spacing (252 for dailies,
    252×390 for minutes); the live curve steps once per closed trade, which for a
    paper month infers to around 20. Every annualised ratio differs by that
    factor before the strategy has done anything, and the usual result is a live
    Sharpe that reads *better* than the backtest — a divergence a reader acts on
    in the wrong direction. The engine's `_periods_per_year` was private and is
    now `metrics.periods_per_year_for`, read by both, so the number the endpoint
    reports is the number the engine used rather than a second copy of the idea.
    Each metric carries a `comparability` of `per_trade` / `annualised` /
    `window` saying which of these reaches it.
  - **Filtering live trades to the run's symbols was the tempting version and is
    wrong.** It would tidy the numbers and hide a strategy trading names it was
    never approved on, which is a finding rather than noise. Both directions are
    warned about instead: the reverse — a backtested symbol live has never
    touched — is a refusal or a data gap, and from the trade count alone it is
    indistinguishable from underperformance.

  **The screen is built**, as the Analytics tab's fourth panel. It sits there
  rather than on `/backtests` because it is a report about live performance; the
  run picker it needs is a hook over the same list, not that page. Three things
  in it are the endpoint's own reasoning carried to the last step, where a
  plainer table would have undone it: the picker starts **empty**, because
  defaulting to the newest run would be the screen making the choice this
  endpoint exists to refuse; the page's date range is **not forwarded**, because
  the open-at-the-start live window is the honest denominator for "has this held
  up"; and every row carries its `comparability` with the warnings above it,
  with a null divergence rendered as a dash. Nothing is coloured good or bad —
  on most rows the sign is a difference rather than a verdict, the same refusal
  the backtest comparison table makes about marking a winner. The annualisation
  warning also gained an answer rather than only a description: a control pins
  both sides to the basis the response reports for that run, so the client never
  computes a second copy of `periods_per_year_for`.

  Unticked. 18 unit tests on the endpoint, 14 on the core it added and 9 web
  tests on the panel, and `make check` is green — but every run in them is a
  fixture. Nothing has yet compared a real paper record against a real
  backfilled backtest, which is what the line below asks for, and no amount of
  screen closes that.

*Verifiable (live-vs-backtest, proposed):* with a strategy that has traded paper
for a fortnight and a completed backtest of the same strategy on record, the
endpoint answers for that run id, and: the live half's `num_trades` and
`total_return` match what `/analytics/performance` reports for the same strategy
over the same window, to the digit; the backtest half's metrics match what
`/backtests/{run_id}` serves for that run; and asking again with
`periods_per_year` pinned to the backtest's own basis moves every annualised
metric and no per-trade one, with the annualisation warning gone from the
response. Proposed because the numbers being *arithmetically* right is what the
unit tests already hold, and the thing that would actually be wrong in
production is the two halves describing different periods or different runs.
- [ ] Daily report.
  Trades and P&L are available from `analytics/` as of #58. The other three
  things the report wants are not gathered anywhere one query can reach:
  rejections are in `signals`, halts are in the kill switch's records, and feed
  incidents exist only in the worker's logs.

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

*Verifiable (strategy listing, proposed):* on a clean stack with no strategy
configured, the `/strategies` tab lists `sma_crossover` as a class that exists
and has never run, and the stored table is empty. Choose `sma_crossover` on the
`/worker` tab, restart the worker, and the same screen moves it across: a row
appears with the universe and parameters the worker was configured with, and "a
worker last started this" matches the runner's own session-open log line.
Restart again and only that timestamp moves.
**Not yet shown** — @claude. Proposed because no line above distinguishes a
strategy that exists from one that has run, which is the single thing this
screen is for.

*Verifiable (stored book, proposed):* with a worker trading paper, stop the
worker. The dashboard correctly reports no book published; the `/positions` tab
still shows the positions, cash and equity the worker's own log reports for its
final evaluation, and the age on it advances past the staleness threshold and
turns the header into a warning. Restart the worker and the two screens agree
again.
**Not yet shown** — @claude. Proposed because this phase's line is about a
browser agreeing with a *running* worker, and the property this screen exists
for is what happens when there is not one.

*Verifiable (order history, proposed):* with a worker trading paper and a risk
limit tightened enough to refuse something, the `/orders` tab shows that refusal
— the right symbol, the rule that refused it, and the reason in words — beside
the orders that did reach the venue, and the same order is absent from
`/analytics/trades` and from the book. That absence is the point: it is what
makes this screen the only place the refusal can be seen.
**Not yet shown** — @claude. Proposed because neither line above reaches it: one
is about the live book, the other about reconstruction, and a refused order is
in neither by construction.

*Verifiable (analytics, proposed):* after a paper week, `/analytics/trades`
reconstructs every round trip the worker actually took — the count and the
summed net P&L agree with the broker's own statement for the period, not merely
with our order table — each trade names the exit that closed it, and
`/analytics/attribution?by=exit_reason` accounts for the whole of that P&L
across its buckets.
**Not yet shown** — @claude (#58). Proposed because the line above is entirely a
dashboard statement and cannot demonstrate anything in the three analytics
items, which is the same trap that left #16's calendar endpoint untickable for
four phases.

The clause doing the work is *"agree with the broker's own statement"*. Checking
the reconstruction against our own orders table only proves the fold is
self-consistent — and a fold that drops a position on a reversal is perfectly
self-consistent, it just reports a smaller loss than the account took. The
venue's own numbers are the only independent check available, and the paper week
produces them for free.

It needs the paper week and nothing else: every part is built, and none of it
has met a database holding a real strategy's history.

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

  A third, found the same way, after the fact: the `API_PASSWORD_HASH` line
  `scripts/hash_password.py` printed **could not be pasted into `.env`**.
  Compose interpolates `$NAME` in that file and a bcrypt hash is
  `$2b$12$<salt><digest>`, so the salt was read as an unset variable — compose
  said `The "hnn" variable is not set` and handed the container a hash with a
  bite out of it. Non-empty, so the startup check for an unset hash stayed
  quiet; not a hash, so every login was refused, with nothing anywhere joining
  the two. Four salts in five start with a letter and hit it. The script now
  prints the line single-quoted (not `$$`-escaped — `Settings` reads the same
  file and does not interpolate, so `$$` fixes compose by breaking everything
  outside it), and a hash that arrives malformed is now `CRITICAL` at startup
  rather than silent.

  **Not** ready for a public address, and this item should not be read as
  saying so: ~~no rate limit on the login endpoint,~~ no revocation before a
  session expires, no TLS of our own, no secrets manager. The rate limit is
  struck rather than deleted, because what this item shipped on its own is what
  the line records — the limiter arrived two items below, with "Rate limiting,
  audit log surfaced in UI" (ADR 0010). The other three are still the
  difference.
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
  flow and strategy-lifecycle changes are **not** wired, because those handlers
  are still `NotImplementedError` stubs and a write behind a stub is dead code.
  They land with their handlers, which is exactly how the first of them arrived:
  `halt_engaged` was added by #70 alongside the endpoint that emits it, so
  "who stopped trading" is answerable for halts engaged through the API.
  `halt_cleared` followed the same way (#75) — a halt with no clear beside it is
  still in force, so the pair is what makes "when did we start again" answerable
  at all — and the same change closed a gap the record had been claiming to
  cover since ADR 0009: a failed step-up now writes `forbidden` with
  `step_up_failed`, where before a wrong password on `/resume` or
  `/flatten-all` left no trace anywhere. Halts from `scripts/halt.py` and from
  the risk layer's own triggers are still absent from the record, and
  attributing those needs an identity the record can stand behind rather than
  another constant. The table's docstring is still the optimistic document and
  this item is the honest one.

  Two rules the design turns on. A failed audit *write* never fails the action —
  the actions worth auditing include halting trading, and refusing to stop
  because Postgres is down has the failure modes inverted. A failed audit *read*
  is a 503 and never an empty page, which is ADR 0007's "nothing published is not
  an empty book" applied to the record instead of the book. Full reasoning and
  five rejected alternatives: ADR 0010.
- [x] Alerting to a phone (feed loss, halt, reconciliation failure) — @claude
  (#52, #54, #55). Driven end to end against the live Telegram API with a real
  credential — a halt, its repeat, its all-clear and a dead transport all behave
  as the *Verifiable:* line says — and the operator confirmed all fifteen test
  messages arrived on their phone. That confirmation is what ticks this: it is
  the one clause no test suite can close, and it is the entire point of the
  item, which is why this sat built-but-unticked across #52 and #54.

  **The three named events are one event.** `HaltReason` already enumerates
  them, and all three converge on `KillSwitch.engage` — feed loss halts,
  reconciliation mismatch halts, and "halt" is the method. So the sink hangs
  off the kill switch rather than off three call sites: a halt reason added
  later alerts without anyone remembering to wire it, and reconciliation is
  covered before its handler exists. ADR 0012, and the same argument ADR 0005
  makes for one submit path.

  `atp_core.alerts` is the port and its transports — `NtfyAlertSink` (no
  account, one POST, self-hostable), `TelegramAlertSink` (a bot messaging the
  operator's own chat, nothing to install for anyone who already has Telegram)
  and `LoggingAlertSink`, which is the default and is a real sink rather than a
  no-op so an unconfigured platform is loud in its logs instead of quietly
  un-alerted. Configure both phone transports and both are sent, via
  `FanOutAlertSink`: two configured transports is a request for two, and
  quietly picking a winner would be a surprise discovered during an incident.
  Bound in the worker, the API and `scripts/halt.py`, so a halt from any of the
  three reaches the same place.

  Telegram carries one trap worth naming, because getting it wrong fails
  silently: **`{"ok": false}` arrives with HTTP 200**. A revoked token or a
  chat the bot was removed from comes back with a successful status line, so a
  sink that trusted `raise_for_status` would report a bot deleted months ago as
  delivering every halt. The body is read instead.

  Two properties are the reason it is a wrapper around one choke point rather
  than a `structlog` handler. **Deduplication is the Redis state**: `engage`
  returns early on an already-active halt, so `StalenessMonitor` re-engaging
  every five seconds through an outage sends one notification, not twelve a
  minute — there is no flag anywhere that could drift out of step. And **a
  failing sink cannot fail a halt**: the send happens after the halt is durable
  and its exceptions are swallowed, which is `_announce`'s rule and ADR 0010's.
  That second one was a real bug caught by its own test — the first draft let a
  raising sink propagate straight out of `engage`.

  Alerts carry the reason and the scope and never a balance, a position or a
  P&L: the transport is a third party, the notification renders on a lock
  screen, and on a public ntfy server a guessable topic is all that is in front
  of it. Hence also that the topic is a credential, lives in the SOPS bundle,
  and is never logged — including on the failure path.

  **That last clause was false when it was written, and #54 is what makes it
  true.** The credential is in the URL for both transports, `httpx`'s
  `HTTPStatusError` quotes the URL in its message, and the sinks logged
  `error=str(exc)` — so a wrong or revoked credential, which answers 401 or
  404, printed itself into the log on the one path an operator reads *because*
  no alert arrived. The two tests cited below missed it for a precise reason
  worth recording: they covered a transport error, whose message is the
  socket's, and Telegram's `ok: false`, whose message is Telegram's prose.
  Neither is an HTTP status, so neither ever built the string that carried the
  URL. Both now parametrise over 401/403/404/500, and reverting the fix fails
  eleven of them.

  What was actually shown: 49 unit tests, and **both** transports' failure paths
  exercised against a real network refusal rather than a mock — this
  environment's egress policy answers 403 for `ntfy.sh` and for
  `api.telegram.org` alike. Each logged
  `THE ALERT DID NOT GO OUT — the event it describes still did`, neither raised,
  and neither printed its credential. The two "a failure never logs the credential"
  tests were checked by making the sinks log their URLs and watching both fail;
  before that they asserted against `caplog`, which captures nothing from
  structlog here, so they had been passing against an empty string and proving
  nothing. They read the captured event dicts now.

  **The same shape of mistake was made a second time, and #55 is what found
  it.** `_describe` guards what the sinks write; nothing guarded what `httpx`
  writes underneath them. httpx logs `HTTP Request: POST <url> "HTTP/1.1 200
  OK"` at INFO, the credential is in that URL, and `configure` sets the stdlib
  root to `ATP_LOG_LEVEL` — so the bot token went into the log on every
  **successful** alert, at the default level, in the worker, the API and every
  operator script. Not the rare 4xx the first fix was written for: every halt
  and every all-clear, forever.

  Worth recording *why* a test file full of scrub assertions missed it, because
  it is the mirror image of the last miss. Those tests had been fixed to read
  structlog's captured event dicts after `caplog` turned out to capture nothing
  from structlog — and this record is written by a library to the *stdlib*
  stream, which the event dicts cannot see. Each fix moved the tests to the one
  instrument that could not see the next leak. The new cases read the stdlib
  stream directly, for both transports; reverting
  `logging._silence_url_logging_libraries` fails three of them.

  What was **not** shown for the length of this item now is. With credentials
  configured, Telegram accepted every message the platform sends — halt,
  all-clear, and every severity — into the operator's own chat, and the operator
  confirmed all fifteen arrived on their phone. No test suite can take that last
  step, which is exactly why the item stayed unticked while the code sat
  finished: the thing being verified is that a human is reachable, and the only
  instrument for it is a human. `scripts/check_alerts.py` sends the set on
  demand, so re-checking it after a credential rotation does not require
  halting anything.

  Not alerted, deliberately: `order.submit_indeterminate` and
  `order.position_unprotected`, both `CRITICAL` in docs/RUNBOOK.md and neither
  of which halts. They are reachable from the same port when somebody decides
  they should be; ADR 0012 does not decide it for them.
- [ ] Metrics/tracing — @claude (#53). Built, wired and driven against
  running processes; unticked, and the reason is narrower than "not finished".
  **The tracing half is done. The metrics half is an exporter nothing has ever
  collected from**, and a counter nothing samples over time is a number without
  a denominator — `atp_orders_rejected_total 47` says almost nothing on its own.
  ADR 0013 and docs/OBSERVABILITY.md.

  **The failure this is built for is not a crash.** `atp_worker.main`'s
  supervisor already writes it down — *"a worker that half-runs is more
  dangerous than one that is plainly down, because monitoring still sees a live
  process while positions go unmanaged"* — and `runner.evaluate` says the same
  of a loop erroring every tick: it "looks alive to a health check". Every state
  worth catching here is one where `/healthz` answers `ok` throughout.

  The probes themselves are real now, which is a smaller thing than metrics and
  was blocking something concrete. `/readyz` raised `NotImplementedError` and so
  answered `500` — the one endpoint an operator reaches for when the dashboard
  is broken read as a *third* fault, on top of whatever they were chasing. It
  checks Postgres and Redis concurrently, each bounded, and reports them
  separately: `ok`, `unreachable` or `absent`, with `503` unless all are `ok`.
  It reports no exception text, because it is open without a session and a
  driver's connection error quotes the DSN — that goes to the log instead
  (CLAUDE.md §1.6). `/healthz` stays dependency-free, deliberately: it is what
  the Docker HEALTHCHECK and compose's `depends_on` gate on, and a version of it
  that consulted Postgres would let a slow database get a healthy API killed.
  This does not tick anything — the line here is about a collector scraping
  across a trading day, and nothing scrapes yet.

  **The same confusion existed one layer out, on the request path, and is fixed
  too.** A wrong `ATP_DB_PASSWORD` made every database-backed endpoint answer
  `500` — strategies, positions, orders, backtests, the equity curve, all three
  analytics endpoints — while `/dashboard/live` and `/risk/status` answered
  `200` off Redis. That shape reads as eight bugs rather than one outage, and it
  read that way because asyncpg raises `InvalidPasswordError` while *opening* a
  connection, where SQLAlchemy does not wrap it: not a `DBAPIError`, not an
  `OSError`, so nothing recognised it. `atp_core.persistence.db.is_unavailable`
  now makes that call once, `session_scope` raises `DatabaseUnavailableError`,
  and `atp_api.errors` answers `503` for every route at once. A statement that
  merely *failed* is still a `500`, deliberately: widening it to "any database
  exception is a 503" would delete the platform's ability to report its own
  bugs. This ticks nothing either — it is a fix, not an item.

  **The operator tools were the last place that fault was still misread, and
  the miss there was worse than the `500`s.** The API's part of this is a
  report; `scripts/preflight.py` is a *decision*. It classified a refused
  password as an unreachable database, which is true, and rendered it as `SKIP`
  — a status that deliberately does not fail the command, because "the stack is
  not up yet" is the normal state of a machine an operator is bringing up a
  piece at a time. So `make preflight` exited `0` against a database that would
  refuse every write for the whole week it was being asked to approve, and the
  `fix` it offered was `make up && make migrate` — a stack that is already up,
  and a command that fails with this same error. This tool exists because the
  input a paper week needs and cannot re-run is calendar time, and the failure
  it was built to prevent is a week of silence indistinguishable from a correct
  run: giving a go signal here is that failure, produced by the check meant to
  catch it.

  `is_auth_failure` splits SQLSTATE class `28` — the server answered and refused
  *these credentials* — out of `is_unavailable`, because the two call for
  opposite advice: everything else that function catches is a state that ends
  without anyone editing a password, and a `28` is a state that ends only when
  somebody does. A refused password now `FAIL`s; a database that is merely not
  up yet still skips, so the local-only run an operator does first is unchanged.

  `scripts/status.py` did not catch the outage at all. Its bars section is a
  database read, so the failure propagated out of `_print_local` and ended the
  script **before** the broker section — discarding the venue's positions and
  working orders at the one moment they are the only book still readable. It
  now reports the outage on the `bars` line and continues, which is the rule
  `_print_broker` already stated and the database was simply never held to.
  docs/RUNBOOK.md claimed this tool kept working during this fault; it did not,
  and that claim is corrected rather than quietly dropped.

  Ticks nothing — a fix, like the two above it.

  **And the cause those three kept deferring to the runbook is now caught.**
  Each of them ends at the same sentence — *the dominant cause in the field is
  not catchable statically* — and that was true of the technique, not of the
  question. `POSTGRES_PASSWORD` is read by initdb and never again, so a password
  rotated against an existing volume leaves `.env` correct, internally
  consistent, passing every check `make check-env` had, and refused by the
  database anyway. The three characters that script already caught are a
  password that was *never* right; this is the ordinary story of one that was
  right and then changed on one side, and it is the one operators actually hit.

  So after the file comes back clean, the script asks the database: one
  connection, three second timeout, closed immediately. The gate is the design
  rather than an optimisation — every static finding is *already* a reason the
  database would refuse us, so probing through one would report a single fault
  twice and put the vaguer half last, after the line that named the character
  and the file line. A refusal that survives the gate is unexplainable by
  anything in `.env`, which is what makes it a diagnosis instead of an echo.

  This is the first check here that needs the platform, and the header's promise
  is narrowed rather than dropped: every check of the *file* still runs with no
  container, database or network, a database that does not answer is not a
  finding, and `--offline` skips the question. "We did not look" and "we looked
  and it was fine" print differently, which is the distinction `preflight`'s
  SKIP exists for applied one tool over — and the accepted case is now the only
  line the command prints that was confirmed against a running database rather
  than inferred from a file.

  Verified against a real Postgres rather than a fake, because everything worth
  checking is behaviour of the server: a wrong password refused rather than
  ignored, the refusal carrying the SQLSTATE the classifier reads, a right one
  accepted over a real scram-sha-256 handshake. `tests/integration/` runs it
  against the CI service container, which is initialised with one password and
  asked about another — the stale volume, reproduced.

  Ticks nothing either. The Phase 6 *Verifiable:* line wants a week of unattended
  uptime on a provisioned host; this is one more thing that host will not need a
  human to diagnose, not the host.

  **And the fifth tool in that family turned out to be the one causing the
  fault, not reporting it.** Every entry above treats a refused password as
  something an operator did — a rotation against an existing volume, a `%` that
  does not survive the trip — and improves how the platform says so.
  `infra/alembic/env.py` was producing it on its own. It read
  `os.environ.get("DATABASE_URL", "postgresql+asyncpg://atp:atp@localhost:5432/atp")`
  under a docstring that said the url comes from `Settings`, and neither `make`
  nor `uv run` puts `.env` into the environment — so host-side `make migrate`
  sent the development password to whatever database was on 5432, whatever the
  operator had written. `seed`, `backfill`, `status` and `preflight` all read the
  file through `Settings` and did not.

  What that costs is worse than a wrong url, because of *where* the command sits.
  `make migrate` is the first line of the deploy procedure and of every upgrade
  after it, and `.env.example`, `docs/DEPLOYMENT.md` and `check_env`'s own advice
  all named it as a reader of `DATABASE_URL`. So an operator who followed the
  deployment doc exactly — generate a password, set `ATP_DB_PASSWORD`, carry it
  into `DATABASE_URL` — got `password authentication failed for user "atp"` for a
  password they had never typed, from the only command that had to work before
  anything else could, with three documents telling them the value they had set
  was the one being used. `make check-env` would then open a connection with that
  same url, be *accepted*, and report the file clean.

  It survived because the fallback is correct on the two machines anyone tests
  on: a laptop running the base compose file, whose password is `atp`, and CI,
  whose service container is also `atp`. The url is only wrong where nobody was
  looking, which is the deployed host. `tests/integration/test_alembic_env.py`
  covers it the one way that works — a password in `.env` the database will
  *refuse*, so the assertion fails whenever the file is not being read, rather
  than passing on a fallback that happens to be right.

  A refused password is now diagnosed rather than raised, the way `preflight` and
  `status` were taught to in the entries above and off the same `is_auth_failure`
  predicate: the url without its password, the SQLSTATE, `make check-env`, and
  the sentence that a `28` does not end by waiting. The url no longer goes
  through `config.set_main_option` either — that hands it to `ConfigParser`,
  where a `%` in a password is an interpolation symbol and the resulting
  `ValueError` quotes the whole DSN, which is §1.6 in a terminal.

  **The same fault had a second half, in the deployed compose configuration.**
  `docker-compose.prod.yml` overrides `DATABASE_URL` for `api`, `worker` and
  `queue` and had no `migrate` service at all, so the overlay initialised
  Postgres with `ATP_DB_PASSWORD` and left the schema step carrying the base
  file's `atp:atp@db`. Nothing in the Makefile runs that combination — ADR 0024
  keeps the migrate profile out of `make deploy` deliberately, and the runbook
  sends an operator to the host-side command — so it was a trap rather than an
  outage, and it is the same trap: one variable that has to reach both ends of a
  connection, written out once per service, with one copy missed.

  So the override is there now, and the thing worth keeping is the check rather
  than the two lines. `scripts/check_port_bindings.py` already asserts the
  deployed *shape* against the resolved configuration, on the argument that
  reading a compose file tells you what was intended and not what compose did
  with it; `check_database_credentials` asks the same question of the
  credentials, for every service at once, and fails the build when a client
  would authenticate with a password `db` was not built with. It compares raw
  strings deliberately — a password that cannot survive the trip into a url is
  `make check-env`'s finding (`db_password_problem`), and reporting it here too
  would be one fault twice with the vaguer half last.

  Both configurations now resolve the `migrate` profile, which nothing did
  before: `docker compose config` omits a profiled service entirely, so the
  service with the wrong password was in neither configuration under test. That
  is the blind spot the restart-policy check had over `web`, in a second place,
  and the reason it is worth naming twice is that a profile is how this
  repository says "this exists and is not started by default" — which is a
  statement about when it runs, never a reason to stop checking what it does.

  Ticks nothing. It is a fix, like the four above it — and unlike them, one to
  something this file's own Phase 0 tick already claimed worked.

  So **each process exports its own `/metrics` and the worker does not push**,
  which is the decision worth arguing with. The natural alternative was for the
  worker to write its numbers into Redis — already the cross-process bus for the
  kill switch, the quote cache and the dashboard snapshot — and let the API
  serve one endpoint. It was rejected because values pushed into Redis **stay
  there after the process that wrote them dies**: the last tick rate, the last
  quote age, no errors — a photograph of a working platform, served for as long
  as anyone cares to look. That design makes exactly the failure above
  invisible. A scrape cannot do that, and a failed scrape is the one signal a
  corpse cannot fake. It is the same objection the Prometheus project documents
  against its own Pushgateway.

  Every metric sits **beside a log line that already existed, at a choke point
  that was already the only path** — `KillSwitch.engage`, `RiskEngine.validate`,
  `OrderRouter._route`, the ingestor's handlers, the alert sinks. That is
  ADR 0012's argument reused: one line of code produces both, so a metric cannot
  drift from the log. And every metric name in the platform is declared in one
  module, with callers given typed functions rather than the instruments, so a
  mistyped label is a `mypy --strict` error rather than a brand-new series that
  reads zero forever.

  **There is deliberately no locally-maintained "is it halted" gauge.** That
  state lives in Redis and several processes write it; a copy kept by whichever
  one happened to call `engage` disagrees with the platform the moment another
  one does. The API reads it authoritatively at scrape time, and an unreadable
  read is reported as `atp_halt_state_readable 0` rather than failing the whole
  scrape — the kill switch fails closed, so that metric means *every order is
  being refused*, which is the thing to alert on and the worst possible moment
  to also lose every other number.

  Tracing is a **correlation id, not spans**: one per API request (from
  `X-Request-ID` if offered, echoed back), per scheduled job, and per pass of
  the strategy loop, carried on every event underneath by the
  `merge_contextvars` that was already in the structlog chain. Two processes on
  one VM that do not call each other synchronously have no "which hop was slow"
  question for spans to answer; they have a "which lines belong together" one.
  An inbound id is sanitised rather than trusted — under the console renderer a
  caller-supplied newline **writes its own log lines**, which is how somebody
  who can reach the API forges a log entry about a platform that moves money.

  One thing found by running it rather than reading it, and it would have
  shipped looking plausible. FastAPI mounts each included router as a
  *sub-router* rather than flattening its routes, so the route that lands in the
  request scope is the one inside `positions.router` and its `path_format` is
  `/positions/{symbol}` — the `/api/v1` that `include_router` added is consumed
  on the way in and appears nowhere in the scope. Labelling by `path_format`
  alone dropped the version from every business route in the platform. The
  template is reconstructed from the concrete path and the matched suffix now,
  and a test pins the version prefix specifically.

  **What was actually driven**, on two real processes rather than in the suite:
  the API refused a scrape with no token and with a wrong one, served the right
  token and a signed-in session, and answered `text/plain; version=1.0.0`. A
  halt engaged by **a different process** appeared in the API's scrape as
  `atp_halt_active` while that API's own `atp_halts_engaged_total` stayed empty
  — which is the authoritative-read design observed rather than asserted, and
  the counter-based design would have shown nothing there. Redis was then
  stopped: the scrape stayed 200, reported `atp_halt_state_readable 0.0`, and
  still served its other 56 series. The worker's exporter refused an unauthenticated
  scrape, refused a wrong token, answered 404 off `/metrics`, and served the
  halt it had engaged; **killing that process turned the scrape into a connection
  refusal** rather than a stale reading, which is the whole argument for the
  second target. Route labels came back as `/api/v1/positions/{symbol}` for two
  different symbols and `<unmatched>` for an invented path, with the invented
  path absent from the body. And one request carrying `X-Request-ID:
  incident-42` produced a log line from `routers/metrics.py` — code that never
  sees the id — stamped `correlation_id: incident-42`, while an id with spaces
  and a 200-character one were both replaced and left nothing of themselves in
  the log. 59 unit tests alongside.

  **What was not shown is the item's actual value.** No Prometheus has ever
  scraped this, there is no dashboard and no alerting rule, and no host exists
  to run one on (ADR 0011). Until something samples these over time the counters
  are single numbers rather than rates, and the histogram buckets are guesses
  made from reading the code rather than from a week of trading. The gauges —
  the halt state, the per-symbol last tick — are useful from one `curl` today,
  and that is the honest extent of it.
- [ ] Backups and a tested restore — @claude (#57).
  **The tooling is built and the restore is genuinely tested. Nothing in this
  repository schedules it** — so this stays unticked.

  The machine is no longer what is missing: ADR 0021 chose one, and there is now
  a recipe for each host shape — the cron lines in docs/BACKUPS.md, and a
  launchd LaunchAgent in docs/LOCAL_HOSTING.md for the Mac, where cron is the
  wrong mechanism outright (a LaunchDaemon cannot see Docker, the mounted drive
  or `uv`, and launchd re-runs a calendar job missed while the machine slept
  where cron skips it). What is missing is that **loading** one is an act on a
  host, which a checkout can neither perform nor observe. Whoever loads it ticks
  this against their own `launchctl print` and a dump that outlived the disk
  that made it — not against this file.

  `scripts/backup_db.py` is `create`, `list`, `verify`, `restore` and `prune`
  over `pg_dump -Fc`; ADR 0014 has the argument for logical dumps over PITR, and
  it turns on an asymmetry in the data rather than on taste. `bars` is the
  expensive table and `scripts/backfill_bars.py` can refetch every row of it;
  `audit_log` is tiny and exists nowhere else in the world. A mechanism whose
  cost scales with the half Alpaca already has, to protect the half that is
  small, is the wrong shape.

  **`verify` is the item and the rest is scaffolding for it.** It restores the
  newest dump into a scratch database, compares it against the source, drops the
  scratch database and exits non-zero if anything disagrees. The comparison is
  bracketed rather than exact on purpose: counts are read either side of the
  dump, and `pg_dump`'s snapshot sits between them, so `before <= restored <=
  after` holds on a database that is being written to while it is backed up.
  An equality check would have been correct on a laptop and flaky at 03:00,
  which is the only time it runs.

  Two things it checks that no other test in this repository could. That `bars`
  comes back **a hypertable** with its compression policy — a restore that loses
  the partitioning answers every query correctly and goes on doing so until the
  table is too big to fix (ADR 0004). And that the restore *can* fail: the
  suite edits a manifest to claim more rows than the dump holds and requires
  `verify` to notice, because a verification step that always passes converts
  "we have never tested a restore" into "we test it nightly", which is worse
  than having neither.

  **What was actually driven**, against a real PostgreSQL 16 rather than in the
  suite: a dump of the real schema built from the SQLAlchemy metadata with 504
  rows across 10 tables; `verify` restoring it into a scratch database, matching
  every count, reporting the Alembic revision and dropping the scratch database
  behind it; a restore into a fresh database with `NUMERIC` values arriving back
  as `500.12500000` rather than a float; and `--overwrite` renaming the existing
  database aside instead of dropping it. Then the refusals, which are the half
  that matters and the half nobody exercises: a dump with one byte flipped
  refused on its checksum before anything was restored; a manifest claiming
  9999 rows failing verification with the table named; a restore into a database
  with a client connected refused; and a paper dump refused against a host
  configured for another run mode, which is a guard only this platform's own
  shape makes possible (ADR 0011 puts the two on separate hosts, which is
  exactly what makes their databases look interchangeable). 39 unit tests
  alongside, and 9 integration tests that need a real TimescaleDB.

  **The TimescaleDB half was driven in CI rather than here**, because this
  environment has no timescaledb extension available and no docker daemon to run
  the image. It ran against the same `timescale/timescaledb:2.15.2-pg16` the
  stack uses — 97 integration tests passed, and the *database server's own log*
  carries the evidence rather than the test runner's exit code: the scratch
  databases appear in it by name being created and dropped, the extension
  reports itself installed at 2.15.2 in each one after the restore, and `bars`
  comes back through `ALTER TABLE bars SET (timescaledb.compress,
  timescaledb.compress_segmentby = 'symbol, timeframe')`. The compression policy
  surviving a dump/restore was the one clause written from the documentation
  rather than from observation; it holds.

  Two things found by running it that would have shipped looking plausible.
  `pg_dump --version` **exits 1 with a bare "Try --help"** when connection flags
  are passed alongside it, so a version check written the obvious way fails and
  blames the database; the probe drops every connection flag now and a test pins
  it. And TimescaleDB keeps a **background worker connected to every database it
  lives in**, which appears in `pg_stat_activity` like any client — so the
  "refuse if anything is connected" guard would have refused every restore
  forever, on every database this platform uses, including on a host where
  nothing at all was running. The guard counts `backend_type = 'client backend'`
  and the scratch database is dropped `WITH (FORCE)`, because that same worker
  attaches the moment `post_restore` hands the database back. Both halves were
  reasoned about first and then *observed*: CI's PostgreSQL log pairs
  `TimescaleDB background worker scheduler for database 18697 will be stopped`
  with the `DROP DATABASE ... WITH (FORCE)` that stopped it, which is the worker
  a plain DROP would have failed on.

  **What is missing is what a host would supply.** Nothing runs this: the cron
  lines are two lines in docs/BACKUPS.md and are documentation until there is a
  machine. Dumps land on the host's own disk unless `ATP_BACKUP_DIR` says
  otherwise, and a dump beside the database it came from dies with it — so what
  exists today is a fast undo for operator error, not disaster recovery, and
  docs/BACKUPS.md says so in its first paragraph rather than its last. They are
  not encrypted at rest. And the restore *procedure* has never been walked by
  anyone but its author.

  One thing found on the way and worth more than the feature: a rebuilt host
  comes back with an **empty** Redis, and an empty Redis holds no halt. The kill
  switch fails closed against an *unreachable* Redis, not an empty one — so a
  restored stack starts **willing to trade**, against a book as of the dump and
  a broker as of now. That is the opposite of the posture everything else here
  takes, it is not something the backup tooling can fix, and it is now step 4 of
  the restore procedure and a paragraph in docs/DEPLOYMENT.md.
- [ ] Deployment target chosen; secrets manager — @claude (#50, #51, #100).
  **The shape and the host are both chosen now. Nothing is deployed, which is
  the whole of why this stays open.** ADR 0011: one always-on VM per run mode in
  a US-East region, the existing compose stack, reached over a private network,
  deployed by an explicit operator action, with paper and live on separate
  hosts so that docs/SAFETY.md layer 3 is structural rather than conventional.
  That ADR deliberately did not pick a machine or a vendor.

  **ADR 0021 picks one, for paper: the operator's own Mac.** It amends ADR 0011's
  x86-64 clause (Apple Silicon is arm64, and docs/HOSTING.md's manifest analysis
  is what it rests on), accepts the loss of US-East proximity and of provider
  snapshots, and is conditional on the machine being configured not to sleep —
  which is the property ADR 0011 rejected a laptop for, and the one thing that
  decides whether a paper week is possible at all. docs/LOCAL_HOSTING.md is the
  delta against docs/DEPLOYMENT.md; live still needs a second host.

  **Unticked, and choosing a host is not what would tick it.** This item's
  demonstration is a host with the stack actually on it, `scripts/status.py`
  answering, and an alert that reached a phone — none of which is a decision,
  and none of which has happened. What ADR 0021 closes is the sentence "no host
  has been selected", which docs/DEPLOYMENT.md, docs/HOSTING.md and this item
  were all carrying. What stays open is everything that needs the machine to
  exist and to have run. The secrets-manager half is likewise unchanged: SOPS +
  age is chosen and `scripts/manage_secrets.py` is written, and on a machine the
  operator is sitting at it is optional rather than load-bearing.

  **Tailscale is not the deployment target**, and where the docs name it they
  mean the access layer: the VPN that keeps the dashboard off a public address.
  WireGuard or an SSH tunnel serve the same purpose (docs/DASHBOARD.md has the
  three). Whatever host is chosen is a Linux box running Docker; how the
  dashboard is reached on it is a separate decision from what it runs on.

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
  once: `db`, `redis` and `api` carried no restart policy while `worker` did, so
  a reboot brought back the worker alone, whose kill switch fails closed against
  an unreachable Redis and which therefore came up halted while looking alive;
  `api` and `worker` bind-mount `./libs` and `./apps/*` over the code baked into
  the images, so what runs is the checkout rather than what was built and
  tested; and `api` runs uvicorn with `--reload`, which makes a `git pull` a hot
  deploy mid-session. The overlay corrects the last two, `make deploy` applies
  it, and docs/DEPLOYMENT.md is the procedure — the page README.md has linked to
  since the skeleton and which did not exist.

  **The first of those three is no longer among them, and correcting it here is
  the point rather than a tidy-up.** Treating a missing restart policy as a
  *deployment* concern was the mistake: the base file also serves the dashboard,
  through `make up-prod`, and there the asymmetry had a second cost nobody had
  named. `web-prod` restarted forever and `api` did not, so a single API exit
  left nginx serving the whole dashboard against nothing — `Failed to load
  dashboard: Error: 502: Bad Gateway`, permanently, from a page that rendered
  perfectly. `db`, `redis` and `api` carry `restart: unless-stopped` in
  `docker-compose.yml` now; the overlay's copies are a restatement, and its
  header says so.

  **Two of that item's conclusions were wrong, and an operator found both** —
  @claude (#84). The first was scope: the fix said "`db`, `redis` and `api`
  carry a policy now", and `web` did not. The overlay puts the dev server behind
  a profile, so the check added alongside — which read the *deployed*
  configuration only — could not see the one service still missing one. A reboot
  brought back the API, the worker, the queue and both stores, and left the
  thing serving the dashboard stopped.
  `check_restart_policies` now runs against both configurations, which is where
  it should have been from the start; verified by removing the line and watching
  the check fail.

  The second was the belief that a restart policy is what makes an API exit
  recoverable. It makes an *exit* recoverable, and the failure that kept the
  dashboard on `Cannot reach the API.` through three separate fixes was not one.
  `docker-compose.yml` runs the API as `uvicorn --reload`, and `--reload` is a
  supervisor: it forks a child that binds the port and keeps a parent that only
  watches files. Any `Settings` value that will not validate — one typo in
  `.env`, and `atp_api.main` builds its app at import — kills the child and
  leaves the parent idling for an edit that a container has nobody to make. The
  process never exits, so `restart: unless-stopped` never fires; the HEALTHCHECK
  notices and Docker does nothing with an unhealthy container but label it. What
  reaches the operator is `docker compose ps` reporting `Up 3 hours (unhealthy)`
  and nginx reporting `connect() failed (111: Connection refused)` — refused
  rather than a 502, because the container was found and the port was shut.
  Every earlier fix had addressed a *cause* of the API not starting; none of
  them could address the fact that not starting was permanent and silent.

  The API is now imported once in the foreground before the reloader is given
  PID 1, so a configuration it cannot start with is an exit code, a climbing
  restart count and a traceback in `logs api` — the diagnosis docs/RUNBOOK.md
  already gives for that screen, which until now was advice for a symptom the
  stack could not produce. CI asserts it as behaviour rather than as YAML: a
  malformed risk limit is written into `.env`, and the job fails if the api
  container has never exited while nothing answers on 8000 — see the correction
  below, which is what that assertion says now. It also now requires
  every container with a healthcheck to report `healthy` — the previous step
  read `.State`, which is `running` for a container that has been unhealthy for
  hours, and would have passed this bug at every stage.

  **That made the failure visible and left the operator a translation to do** —
  `make check-env` finishes the job. The API exiting loudly answers "something in
  the configuration is wrong"; the next question is *which value*, and the only
  answer was a pydantic traceback naming a **field**. `max_position_pct` is not
  in `.env`; `RISK_MAX_POSITION_PCT` is, and mapping one to the other means
  knowing that `RiskLimits` carries an `env_prefix`. The script reads the same
  file through the same `Settings` and prints the name as written, the line it is
  on, and what is wrong with it — for every broken value at once rather than one
  per restart. Secrets are never printed, and anything it cannot classify is
  withheld too, which is the safe direction for a value that failed to load and
  is still a credential.

  It also reports **where the value came from**, because `.env` is neither the
  only source nor the winning one: compose sets `DATABASE_URL` and `REDIS_URL` in
  `environment:` and an `export` beats the file, so a key that is both exported
  and written is being read from the export — and the first implementation of
  this pointed at the `.env` line anyway, which is worse than saying nothing.

  The two tools an operator reaches for when nothing works, `scripts/preflight.py`
  and `scripts/status.py`, **were themselves killed by the fault they would be
  run to diagnose**: both called `get_settings()` and exited with the traceback
  they existed to explain. Both now name the variable and point at
  `make check-env`.

  Not in `make check`: there is no `.env` on a CI runner and nothing to diagnose.
  This is an operator command for the moment the stack will not come up, and it
  needs no container, database or network.

  **It then only answered half the question, and the missing half was the
  dangerous one.** `make check-env` reported values that would not *load* — and
  a misspelled key does not fail to load, it is dropped. `Settings` is
  `extra="ignore"`, correctly, because `.env` is shared with compose and Vite
  and a process that refused to start over a variable meant for one of them
  would be wrong. The cost is silence:

      RISK_MAX_POSITION_PC=0.02      # the T is missing

  loads cleanly, reports nothing, and leaves `max_position_pct` at its `0.10`
  default. The operator believes they capped a position at 2% of equity; the
  platform will let one reach 10%, and no log line, probe, banner or check said
  otherwise. It is a worse failure than the one above it — a stack that will not
  boot announces itself, and a risk limit you believe you tightened does not.
  Nothing in this repository looked for it.

  `known_env_vars()` derives the accepted names from the models, so it cannot
  lag a field added tomorrow, and `check_env` reports every assignment in `.env`
  that is not one of them, with a close match where there is one. The keys that
  legitimately are not `Settings` fields — the `VITE_*` pair, `ATP_DEV_PROXY_TARGET`,
  `ATP_WEB_BIND_ADDR`, `ATP_DB_PASSWORD` — are allowlisted with what reads each,
  because a check that reports four false positives on a stock `.env` is a check
  people learn to skip.

  That allowlist is the one hand-maintained thing here, and a stale entry fails
  *open* — the check stops reporting a key it should. Two tests pin it in both
  directions: a stock `.env.example` must produce no unread keys, and no real
  `Settings` field may appear in the list. Both were mutation-checked.
  `.env.example` documents the trap in its header and marks the dashboard
  section as deliberately not `Settings`.

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

  **The secrets half is built** (#51), which closes the gap this item recorded
  when the target was chosen. `scripts/manage_secrets.py` wraps SOPS and age: `init`
  generates the key and writes `.sops.yaml`, `import` encrypts an existing
  `.env` into `infra/env/<mode>.sops.env`, `edit` re-encrypts on save, `check`
  decrypts in memory, and `install` writes the host's `.env` — `0600`, created
  with that mode rather than chmod-ed into it, and replaced atomically so a
  crash mid-write cannot leave the stack holding half its credentials.

  Dotenv rather than YAML, because SOPS encrypts dotenv *values* and leaves the
  *keys* readable: a rotation diffs as `ALPACA_API_SECRET changed` instead of one
  opaque blob, which is what a reviewer needs to see and the value is what they
  must not.

  Two refusals are the point of it being a wrapper rather than a shell alias.
  **The run-mode locks may not be in a bundle** — a bundle is copied between
  hosts, restored from backups and re-synced by tooling, and none of that may
  switch on live trading. They are refused on import *and* again on install,
  because a bundle can acquire one afterwards through `sops` directly. ADR 0011
  named `ATP_ALLOW_LIVE_TRADING` and `WORKER_ALLOW_LIVE_ORDERS`; `ATP_RUN_MODE`
  is refused too, which extends that decision rather than restating it and wants
  a reviewer's eye. And **a failure never quotes plaintext**: on the decrypt path
  `sops`'s stdout *is* the secret, so only stderr is ever put in an error, which
  is a unit test rather than a convention.

  Two traps in `.gitignore` were in the way and are now handled. `secrets/` is
  unanchored, so a bundle under it would be ignored at every depth — and git
  cannot re-include a file whose parent directory is excluded, so no negation
  could rescue it; bundles live in `infra/env/` instead, which is ignored
  *except* for `*.sops.env`, so ciphertext is committable and a stray plaintext
  is not. `make check-tracked` covers that path, and `secrets.py` refuses to
  write a bundle git would ignore — an encrypted bundle nothing tracks is a
  deployment that silently has no credentials.

  **Unticked, and for a narrower reason than before.** Nothing has been
  provisioned and nothing has been deployed: this is a decision, a compose file,
  a runbook and now a key-management tool, none of which is a running host. The
  proposed *Verifiable:* line below asks for a week of unattended uptime and for
  no plaintext secret on the host outside the `0600` runtime file; the second
  clause is now buildable and the first still needs a machine. Alerting and
  backups, the two items above, are what a live host would need next, and
  docs/DEPLOYMENT.md says so rather than implying a host is ready.

  One thing found on the way and recorded rather than fixed: `tailscale serve`
  is the documented way to get HTTPS, but TLS terminates at Tailscale and nginx
  sets `X-Forwarded-Proto` from its own `$scheme`, which is `http` — so the
  session cookie `_is_https()` guards is **not** marked `Secure` behind exactly
  the TLS this recommends. Fixing it means deciding which proxy's headers to
  trust, which is a security change rather than a deployment one. SECURITY.md
  lists it and docs/DEPLOYMENT.md explains it.

  **The specification now has a survey against it** — @claude (#100),
  docs/HOSTING.md. Not itself a choice — that came afterwards, with ADR 0021
  above — and the box moves for neither: what landed is which offerings can
  satisfy DEPLOYMENT.md's table and what each of the rest fails on, so that
  whoever picks a machine picks it with the tradeoffs in front of them, which
  is what ADR 0021 then did. Three findings are worth having here rather than only there.
  **The database cannot be split onto a free managed tier to shrink the VM**:
  Neon ships Apache-2 `timescaledb`, whose missing native compression fails the
  initial migration on `add_compression_policy`, and Supabase cannot enable the
  extension on new PG17+ projects at all — so ADR 0011's constraint 3 is not
  negotiable by re-arranging the deployment. **The only free tier that clears
  the RAM floor is ARM64**, which the spec's x86-64 row does not allow; every
  image in the stack publishes an `arm64` manifest and nothing in the tree pins
  a platform, but that is evidence rather than a build anyone has run, and
  changing the row is an amendment to ADR 0011 rather than a docs edit. And
  **free is enough to earn the paper week** but not the second host
  docs/SAFETY.md layer 3 wants for live, which puts the first real bill after
  the demonstration rather than before it.

  **A third conclusion of the restart-policy item was wrong, and `claude/main`
  found it by going red** — @claude. The CI step above asserted "a config the
  API cannot start with leaves the container not `running`", and that is not a
  property the corrected behaviour has. The fix makes the API *crash-loop*: the
  foreground import raises, PID 1 exits, `restart: unless-stopped` starts it
  again. `docker compose ps` therefore reads `restarting` for most of that cycle
  and `running` for the fraction of a second the import takes before it fails,
  so a single sample after a fixed `sleep 25` was a coin weighted heavily enough
  to pass for months. Run 32956727804 landed in the window and reported `Up Less
  than a second (health: starting)` — on a tree byte-identical to one CI had
  already passed, which is what makes it a defect in the assertion rather than
  in the stack.

  The step now polls the **restart count**, which is what this item's own
  paragraph above claimed the fix produced and what docs/RUNBOOK.md tells an
  operator to look for. It is the exact discriminator rather than a
  probabilistic one: the regression — a `--reload` parent idling over a dead
  child — never exits, so its count stays 0 for as long as anyone waits, while
  the corrected behaviour reaches 1 within a second or two. Nothing was relaxed
  to get green; the check that catches the original bug still catches it, and
  the `/healthz` precondition that stops the step proving nothing is untouched.
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

  **What this item also introduced, and did not account for, is the 502.** Once
  the browser reaches the API through nginx, every way the API can be
  unreachable arrives on screen as one indistinguishable string —
  `Failed to load dashboard: Error: 502: Bad Gateway` — and the dashboard around
  it renders perfectly, because the bundle is static files off disk and only the
  poll crosses the proxy. Three different faults produce it: the API is not
  running, the API is up and cannot reach Redis or Postgres, or nginx cannot
  resolve it. The item shipped with no way to tell them apart and one of them
  made permanent by a missing restart policy (see the deployment item above).
  Both halves are addressed now — `/readyz` is implemented rather than raising
  `NotImplementedError`, so `/healthz` and `/readyz` together separate the three
  from the same browser, and the client says which condition it hit instead of
  repeating nginx's wording at the reader. docs/RUNBOOK.md, "Dashboard shows 502
  Bad Gateway". This does not touch the tick: the serving-layer line is about
  what CI re-checks, and it still holds.

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

*Verifiable (alerting, proposed):* with a transport configured, engaging a halt
puts a notification on a phone that names the reason and carries no numbers from
the book; re-engaging the same halt for the length of an outage sends exactly one
more nothing; clearing it sends an all-clear; and a sink pointed at an
unreachable host leaves the halt in effect and the process running.
**Shown** — @claude (#55). With real `ALERT_TELEGRAM_TOKEN` and
`ALERT_TELEGRAM_CHAT_ID` supplied, all four clauses were driven against the live
Telegram API rather than a mock, through the sink `build_alert_sink` builds from
the deployment's own settings and the real `RedisKillSwitch`:

- Engaging a global `data_feed_lost` halt sent one message, `ok: true`, titled
  `Trading halted: data_feed_lost`, body naming the scope, who halted and what
  to go and read — and no balance, position or fill price, as ADR 0012 requires.
- Re-engaging the same halt sent **nothing**: the Redis state is the dedup, and
  the early return in `engage` never reaches an alert.
- Clearing it sent one `Trading resumed`, INFO, and Telegram-silent.
- A sink pointed at a dead host logged `THE ALERT DID NOT GO OUT` and left the
  halt engaged and the process running.

Severity mapping was checked the same way: INFO delivered with
`disable_notification: true` and both louder levels without it, which is the
only lever Telegram gives and the whole difference between an all-clear at 02:00
and a halt.

The first clause is the one no code here could close — what the run showed is
that Telegram *accepted* every message into the operator's own chat, and the
line asks that a notification arrives on a phone. **The operator confirmed
receipt: all fifteen messages arrived.** That is what completes the line, and it
is worth being explicit that the evidence came from a person rather than from
this repository, because it is the only clause in this file that could not have
come from anywhere else.

Worth noting against the usual caveat: the line was proposed by @claude and is
ticked by @claude, which normally wants a reviewer's eye. Here the deciding
evidence came from the operator, which is a stronger check than a review of the
tick would have been — but the four clauses *below* the first are still
self-proposed and self-shown, and those are the ones to re-read sceptically.

`scripts/check_alerts.py` is what makes the whole line repeatable without
engaging a real halt (#55) — worth re-running after any credential rotation,
since a revoked token and a working one are indistinguishable from anything
except a send. Proposed because the item had no line of its own, which is the
defect #45 and #46 both named.

*Verifiable (metrics and tracing, proposed):* a scrape config collects from
both processes across a trading day; the platform's state is legible from it
without reading a log — a halt shows as engaged and cleared, a symbol that
stopped printing shows as stale, and a loop erroring every tick is
distinguishable from one that is idle; a worker that dies takes its target down
rather than leaving its last healthy values readable; and one unit of work's log
lines can be recovered from a single id taken off a response header.
**Partly shown** — @claude. Every clause but the first has been driven against
running processes, including the dead-worker one, which is the design's whole
argument: killing the exporter turned the scrape into a connection refusal, and
a halt engaged in one process was read authoritatively by the other. The first
clause is the one that matters and is **not shown**: nothing has ever scraped
this, so "across a trading day" has no observer and the counters have never been
a rate. That is not a defect in the exporter — it is the collector, which needs
the host ADR 0011 has specified and nobody has bought. Proposed because this
item had no line of its own, the same defect #45 and #46 named; scoped to
*metrics and tracing* and saying nothing about alerting or backups.

*Verifiable (backups, proposed):* a scheduled backup runs unattended on the
host across a week without anyone touching it; a dump taken by that schedule is
restored into a scratch database and matches the source it came from, `bars`
included as a hypertable with its compression policy; at least one dump is
demonstrably somewhere the host's disk failing would not take with it; and the
restore procedure has been walked end to end — stack down, database back,
migrations applied, book reconciled against the broker, halt cleared
deliberately — by somebody following docs/BACKUPS.md rather than by the person
who wrote it.
**Partly shown** — @claude. The second clause is shown and is the one this item
is named for: `backup_db.py verify` restores and compares on demand, fails when
the restore does not match, and is a command rather than an intention. The
other three are not, and only the last is about this repository — the first and
third need the host ADR 0011 specified and nobody has bought, which is the same
blocker metrics has above. Proposed because this item had no line of its own,
the same defect #45 and #46 named; scoped to *backups* and saying nothing about
the schedule that would make them a disaster-recovery story rather than an undo.

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
right one. "Past the gate" was a 500 into a stub when this was written, which
was the honest assertion while the handler was unbuilt; `/risk/resume` is
implemented as of #75 and the assertion is now the 200 it actually answers.

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
metrics, backups or a secrets manager, and it must not be read as evidence for
any of them. (Those six were all unstarted when this was written; all six have
since been built to varying depths, backups last. The scope of this line is
unchanged either way — it is about serving.)
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
alerting, metrics or backups, which have their own items and their own lines
above — and, in the case of metrics, a collector that this same missing host is
what stands in the way of.

That line and Phase 4's are the same week of uptime seen from two ends, which is
the argument for doing this now rather than at go-live: Phase 4 has eight built
items held against "a strategy trades the paper account for a week", Phase 5
needs "a worker trading paper", and Phase 1's streaming line needs a forced
disconnect during a session. None of that is demonstrable on a laptop that
sleeps.

The phase still needs a line covering production readiness as a whole. The
reason previously given for not writing one — that nothing is deployable until
authentication lands — no longer holds, since it has. What stands in its way now
is a shorter list than it was, and a more specific one, and both of what is on
it now wants the same thing. **Backups** are taken and a restore of one is
demonstrable on demand, and nothing schedules them and no dump has left the host
that wrote it. **Metrics** are exported and have never been collected. Neither
is a missing mechanism any more; both are waiting on the same missing host. **Alerting** has come off this list:
it reaches a phone over a real credential and is ticked above, which closes the
one item here that was waiting on a person rather than on a machine — and it is
the go-live checklist line most likely to be assumed rather than tested, since
an unalerted platform behaves exactly like an alerted one until the day it
matters. A line written today would still describe a system docs/SAFETY.md's own
go-live checklist refuses, but for two reasons now instead of three.

## Later
Declarative rule builder UI · walk-forward optimisation · sector/factor exposure
limits · multi-broker · options · portfolio-level strategy allocation · IBKR
adapter for SG/HK markets

## Explicitly out of scope
HFT or latency-sensitive strategies (wrong architecture and wrong language) ·
market making · anything requiring co-location · ML model training (train
elsewhere, serve the signal here)
