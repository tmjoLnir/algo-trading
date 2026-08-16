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

**Live-feed status.** The feed has now been reached: with egress opened to
`data.alpaca.markets` and credentials supplied, a 5-year SPY daily backfill ran
against the real Alpaca API — 1,254 bars, and a gap scan against the NYSE
calendar returning 1,254 expected / 1,254 matched / 0 missing / 0 unmatched
(571 of the range's 1,825 calendar days correctly excluded as weekends and
holidays). This retires the "no bar has ever been fetched from Alpaca" caveat
that stood on the four items below, and settles three things that were pinned by
tests but unseen on live data:

- Daily bars **are** stamped 00:00 New York — 05:00Z in winter, 04:00Z in
  summer — so the attribution rule in `atp_core.data.gaps` holds across DST.
- Pagination **does** cross real `next_page_token` boundaries: 22,727 one-minute
  bars over 3 pages, ascending and duplicate-free across both seams.
- The calendar handles a one-off closure the vendor also skips (2025-01-09, the
  national day of mourning), so the scan is not passing vacuously.

Everything stays unticked, because the run stored nothing. TimescaleDB cannot be
provisioned in this environment — Docker Hub, ghcr, quay, ECR, packagecloud and
the Ubuntu archives are all 403 at the egress gateway, and the initial migration
correctly refuses to create `bars` as a plain table without the extension. The
backfill therefore ran against an in-memory `BarRepository` standing in for
`PostgresBarRepository`, so the *fetch* and *gap* legs of the *Verifiable:* line
are shown and the *store* leg is not. Opening egress to a container registry (or
any TimescaleDB instance) is the one remaining blocker.

- [ ] Alpaca historical provider with pagination — @claude (wip #8).
  `AlpacaHistoricalProvider` is implemented, unit-tested against recorded
  responses, and now exercised against the live feed including multi-page
  pagination (above). Unticked: Phase 1 ticks against its one *Verifiable:*
  line, and that line needs a backfill that persists.
- [ ] `BarRepository` + backfill script — @claude (wip #10).
  Both halves are built: `PostgresBarRepository` (#9, integration-tested
  against the real hypertable) and `scripts/backfill_bars.py` over
  `atp_core.data.backfill`. The `backfill_bars` orchestration — windowing,
  batching, rate limiting, empty-window detection — has now run end to end
  against the live feed. Still unticked, and this is the item the remaining
  blocker sits on: `PostgresBarRepository` has never seen live data, because no
  hypertable can be created here. `find_gaps` stays deliberately unimplemented
  on this item; it belongs to the calendar-aware item below.
- [ ] Gap detection, calendar-aware — @claude (wip #15, #16).
  `TradingCalendar` is implemented over `pandas_market_calendars` (sessions,
  holidays, early closes, `next_open`, `minutes_to_close`), `find_gaps` is
  implemented on top of it, and `--verify` now runs instead of refusing (#15).
  The nightly sweep that consumes it — `backfill_missing_bars`, over every
  stored series for the last 7 days, re-checking and naming what it could not
  fill — landed in #16. The pure logic (`expected_windows`, `scan_gaps`) has now
  been run over five years of live SPY dailies and reported clean, and the
  00:00-New-York assumption it rests on has been confirmed on live data rather
  than only in tests. Unticked because the SQL half of `find_gaps` — the stored
  timestamp read — has not run against live data. `1h`/`4h` gap detection is
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
  Unticked, and unlike the three items above the live run did not touch it: the
  historical REST feed has now been exercised, but nothing here has yet held a
  socket open to Alpaca, so the reconnect ladder and the bar-message shape are
  still pinned by tests rather than by live data.
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
