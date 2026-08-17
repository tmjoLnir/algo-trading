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
- [x] `make install` and `make up` work end to end — @claude (#7).
  Demonstrated by the `stack` CI job, which runs both on a clean checkout.
  `make up` starts db, redis, api and web; the worker is behind a compose
  profile until its entry point stops raising `NotImplementedError` (Phase 4).
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
  A socket has now been held open to Alpaca: `AlpacaRealtimeFeed` authenticates
  and subscribes against the live server (`stream_connected`,
  `stream_subscribed bars=1 quotes=1`), which also confirms the credentials
  carry the stream entitlement — a separate grant from the REST API. That is
  the handshake only. It was run with the exchange shut, so no bar or quote has
  ever been parsed from the live stream, and the reconnect ladder and the
  `FeedReconnected` gap backfill — the parts that decide whether a dropped
  connection trades across a hole — remain pinned by tests. Unticked on that
  basis, not on the historical *Verifiable:* line, which does not reach here.
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

*Verifiable:* backfill 5 years of SPY dailies; no gaps outside holidays.
**Shown** — @claude (#23); see the run above.

*Verifiable (streaming):* hold the live stream through a forced disconnect
during market hours; every session bar lands in the hypertable, the reconnect
backfills the gap, and the latest quote is readable from Redis.
**Not yet shown** — proposed here because the two items above have no line of
their own, and were being held against a historical-data line that cannot
demonstrate them. Needs an open market; adjust the wording if it is not the
demonstration you want.

## Phase 2 — Backtesting (requirement #2)
- [ ] Indicators (`ema`, `rsi`, `atr`, …) — @claude (wip #24).
  `ema`, `rsi`, `atr`, `bollinger`, `macd` and `stddev` are implemented, along
  with the `*_series` variants the module docstring promised the engine, plus
  `true_range` and `sma_series`. The series form is the primitive and the
  scalar is its last element: Wilder's smoothing is recursive, and two
  implementations of it are two chances to get it subtly wrong. Warmup is
  `nan` rather than a partial average, which is what stops a run opening on an
  SMA(200) computed from six bars. Conventions that reasonable implementations
  disagree on — SMA-seeded EMA, Wilder's alpha for RSI and ATR, population
  `stddev` for Bollinger — are stated at the top of `ta.py`, and each is
  cross-checked to 1e-9 against pandas' independent `ewm`/`rolling`.
  Unticked: Phase 2 ticks against an engine that does not exist yet.
- [ ] Backtest engine event loop, next-bar fills
- [ ] Cost and slippage models
- [ ] Metrics
- [ ] `SmaCrossover` runs end to end

*Verifiable:* a hand-computed 20-bar fixture matches the engine exactly.

## Phase 3 — Risk (requirement #3)
- [ ] `RiskEngine` + full rule chain
- [ ] Position sizing, all methods
- [ ] `StopManager` — fixed, ATR, trailing, chandelier, time
- [ ] Redis kill switch

**Before any live order path exists.** Not after.

## Phase 4 — Execution & paper trading (requirements #1, #5)
- [ ] `BrokerPort` + Alpaca adapter (paper first)
- [ ] `OrderRouter`, order state machine
- [ ] `SimulatedBroker`
- [ ] Reconciliation
- [ ] `StrategyRunner` live loop — also drop the `worker` compose profile (#7),
      so the worker rejoins the default stack once it can actually start
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
