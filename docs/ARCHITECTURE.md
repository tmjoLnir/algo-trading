# Architecture

## The central idea

**Ports and adapters, so that backtest, paper and live are one code path.**

The value of a backtest is entirely conditional on the backtested system being
the system that trades. If the live loop differs from the backtest loop — a
different ordering, a different fill assumption, an extra `if live:` branch —
then the backtest describes a program that does not exist, and every number it
produced is fiction.

So: `libs/core` contains pure logic with no I/O. Everything external is a
`Protocol`. Run mode selects which adapters get bound, and nothing else changes.

```
              backtest          paper              live
clock         Simulated         System             System
market data   historical bars   Alpaca stream      Alpaca stream
broker        Simulated         Alpaca paper       Alpaca live
strategy      ← identical object in all three →
risk engine   ← identical rules in all three  →
```

There is no `if run_mode == "paper"` anywhere in `libs/core`. If you need one,
you have found a design problem — the difference belongs behind a port.

## Processes

```
┌─────────────┐   HTTP/WS   ┌──────────────┐
│  apps/web   │◀───────────▶│  apps/api    │  stateless, horizontally scalable
│  React      │             │  FastAPI     │  reads state, publishes intents
└─────────────┘             └──────┬───────┘
                                   │ Redis pub/sub
                            ┌──────┴────────┐
                            │ apps/worker   │  singleton (leader-elected)
                            │ ingestor      │  ONE market-data connection
                            │ runners       │  one per active strategy
                            │ scheduler     │  reconcile, backfill, reports
                            └──────┬────────┘
                                   │
                     ┌─────────────┴──────────────┐
                     │  Postgres/TimescaleDB      │  bars, orders, fills, audit
                     │  Redis                     │  quotes, pub/sub, kill switch
                     └────────────────────────────┘
```

**Why the API never places orders directly.** It publishes an intent; the worker
consumes it and routes it through the same `OrderRouter` a strategy uses. Two
submission paths would mean two places to enforce risk, and the second one is
always the one that gets forgotten. One path, one audit trail.

**Why the worker is a singleton.** Two ingestors would double-write bars and
burn the broker's connection limit. Two runners of the same strategy would
double every position. Run one; if you need HA, use leader election with a Redis
lease — not two actives.

## Layers inside `libs/core`

```
domain/      entities. imports nothing from siblings.
   ↑
indicators/  strategy/  risk/  backtest/  analytics/  execution/     pure logic
   ↑
data/ports  brokers/ports  persistence/     protocol definitions
   ↑
adapters: alpaca.py, simulated.py, providers/, repositories/
```

Siblings on the pure-logic tier may import one another — `backtest/` uses
`strategy/`, `execution/` uses `risk/` and `strategy/` — and should, where the
alternative is a second copy of a rule. `execution/router.py` sizes through
`risk.rules.position_size` and prices through `risk.rules.reference_price` for
exactly that reason: sizing against one price and validating against another is
invisible, because both numbers look right on their own.

Dependencies point downward only. A violation — core importing from `apps/`, or
`domain/` importing from `strategy/` — is a review rejection, because it is what
turns a testable core into an untestable one over about six months.

`logging/` and `metrics/` sit outside that stack rather than on a tier of it.
Both are module-global and every layer reaches for them without being handed
one, which is deliberate: threading a logger or a metrics registry through every
constructor in the platform would buy nothing when there is one process-wide
answer in each case. Neither does I/O — `metrics/` imports `prometheus_client`'s
registry and text exposition and none of the parts that open a socket, so
CLAUDE.md §1.3 still holds. Serving the text is `apps/`' job (ADR 0013).

## Data flow: a trade, end to end

```
Alpaca WS ──▶ StreamIngestor ──▶ Redis quote cache
                    │                    │
                    ▼                    │
              BarRepository              │
                    │                    │
                    ▼                    ▼
              StrategyRunner.evaluate()
                    │
                    ├─ 1. mark positions
                    ├─ 2. check stops ────────────┐
                    ├─ 3. process fills           │
                    ├─ 4. strategy.on_bar() ─▶ Signal
                    │                             │
                    ▼                             ▼
                 position sizing            OrderRouter.submit()
                                                  │
                                            RiskEngine.validate()
                                             ├── denied → log, publish, stop
                                             └── approved
                                                  │
                                            BrokerPort.submit_order()
                                                  │
                                            fill event ──▶ protective stop
                                                  │
                                            persist + publish ──▶ dashboard
```

Step 2 before step 4 is deliberate and mirrors the backtest exactly: in reality
a stop can fire before the strategy would have acted. Reordering them lets a
strategy exit at a price it could not have obtained.

## Key decisions and why

**TimescaleDB, not plain Postgres.** Bars are the only unbounded table. One year
of 1-minute bars for 500 symbols is ~50M rows; hypertable partitioning plus
native compression makes that queryable on modest hardware. Everything else is
ordinary Postgres.

**Decimal everywhere for money.** Binary floats cannot represent 0.1. Error
accumulates across thousands of fills, and a P&L that is wrong by cents is
wrong — you cannot reconcile against a broker with it.

**Redis for the kill switch.** It must be settable by the API while the worker
is mid-loop, and it must survive a worker restart. In-process state fails both,
and the second failure is dangerous: a crash-looping worker would silently
resume trading on every restart.

**Signals separate from orders.** Strategies express intent; sizing and risk are
applied uniformly outside them. One risk policy governs every strategy, and a
strategy author cannot accidentally bypass it.

**Orders and fills are never deleted.** They are the audit trail. Cancel by
status.

## Scaling, when you get there

The first bottleneck is almost always the strategy loop, not the database. In
order: run strategies as separate worker processes partitioned by symbol; move
indicator computation to a shared cache (already the shape of
`StrategyContext.indicator`); only then consider a faster ingestion path.

Do not build for scale you do not have. A single worker handles a few hundred
symbols on daily or minute bars comfortably.

## Where to add things

| Adding… | Goes in | Also update |
|---|---|---|
| A broker | `brokers/<venue>.py` implementing `BrokerPort` | `deps.py`, an ADR |
| A data vendor | `data/providers/<vendor>.py` | `deps.py` |
| An indicator | `indicators/ta.py` | tests |
| A risk rule | `risk/rules.py` + `default_rules()` | `docs/RISK.md` |
| A strategy | `strategy/examples/` or a stored `RuleSet` | — |
| A strategy metric (Sharpe, drawdown) | `backtest/metrics.py` | shared with analytics automatically |
| An operational metric | `metrics/registry.py` — nowhere else | `docs/OBSERVABILITY.md` |
| An endpoint | `apps/api/routers/` | `make gen-types` |
