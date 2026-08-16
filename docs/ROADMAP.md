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
- [ ] Alpaca historical provider with pagination — @claude (wip #8).
  `AlpacaHistoricalProvider` is implemented and unit-tested against recorded
  responses. Deliberately **not** ticked: the phase's *Verifiable:* line has not
  been shown, and cannot be until `BarRepository` and the backfill script exist
  to run it against the real feed. No bar has been fetched from Alpaca yet.
- [ ] `BarRepository` + backfill script — @claude (wip #10).
  Both halves are now built: `PostgresBarRepository` (#9, integration-tested
  against the real hypertable) and `scripts/backfill_bars.py` over
  `atp_core.data.backfill`. Still unticked — the phase's *Verifiable:* line
  needs the script run against the live feed, and nothing here has fetched a
  bar from Alpaca yet. `find_gaps` stays deliberately unimplemented; it belongs
  to the calendar-aware item below, and `--verify` refuses up front rather than
  pretending to check.
- [ ] Gap detection, calendar-aware — @claude (wip #15, #16).
  `TradingCalendar` is implemented over `pandas_market_calendars` (sessions,
  holidays, early closes, `next_open`, `minutes_to_close`), `find_gaps` is
  implemented on top of it, and `--verify` now runs instead of refusing (#15).
  The nightly sweep that consumes it — `backfill_missing_bars`, over every
  stored series for the last 7 days, re-checking and naming what it could not
  fill — landed in #16. Still unticked for the same reason as the two items
  above: the phase's *Verifiable:* line needs a real 5-year SPY backfill, and no
  bar has been fetched from Alpaca yet — so the one assumption this rests on,
  that daily bars arrive stamped at 00:00 New York, has been pinned by tests and
  documented but not yet seen on live data. `1h`/`4h` gap detection is
  deliberately refused rather than guessed (docs/DATA.md 'Gaps').
- [ ] Real-time WS ingestor, reconnect + gap backfill — @claude (wip).
  Both halves are built and unit-tested against scripted fakes.
  `AlpacaRealtimeFeed` owns the socket — auth handshake, subscription replay,
  exponential backoff with jitter, and a refusal to loop on an error another
  connection would not fix (bad credentials, plan, or the one-connection limit).
  `StreamIngestor` owns the fan-out and the data gap: it caches quotes,
  persists bars, and on `FeedReconnected` re-fetches `[last message, last
  completed bar)` before handling anything from the new connection. A gap it
  cannot close engages the kill switch instead of trading across the hole.
  Unticked for the same reason as the three items above: the phase's
  *Verifiable:* line is still unshown and nothing here has yet held a socket
  open to Alpaca, so the reconnect ladder and the bar-message shape are pinned
  by tests rather than by live data.
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
  Unticked for the same reason as everything above it: Phase 1's *Verifiable:*
  line still has not been shown.

*Verifiable:* backfill 5 years of SPY dailies; no gaps outside holidays.

## Phase 2 — Backtesting (requirement #2)
- [ ] Indicators (`ema`, `rsi`, `atr`, …)
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
