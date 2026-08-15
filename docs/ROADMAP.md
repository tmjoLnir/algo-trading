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
- [ ] `make install` and `make up` work end to end
- [x] Alembic initial migration; TimescaleDB hypertable created — @claude (#4)
- [x] `Position.apply_fill` + `Order.apply_fill` implemented and property-tested — @claude (#4)

**Do these two first.** Everything downstream computes P&L from them; a bug here
is invisible and poisons every number the platform produces.

## Phase 1 — Data (requirement #4)
- [ ] Alpaca historical provider with pagination
- [ ] `BarRepository` + backfill script
- [ ] Gap detection, calendar-aware
- [ ] Real-time WS ingestor, reconnect + gap backfill
- [ ] Redis quote cache, staleness monitor

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
- [ ] `StrategyRunner` live loop
- [ ] Trade-updates WS with reconnect

*Verifiable:* a strategy trades the paper account for a week and reconciles clean.

## Phase 5 — Dashboard & analytics (requirements #6, #7)
- [ ] `/dashboard/live` aggregate endpoint
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
