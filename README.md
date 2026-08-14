# ATP — Algorithmic Trading Platform

A web-based platform for building, backtesting, paper-trading and running rule-based
trading strategies against live market data.

> **Status: skeleton.** The architecture, contracts and guardrails are defined; most
> module bodies raise `NotImplementedError`. See [`docs/ROADMAP.md`](docs/ROADMAP.md)
> for the build order.

---

## What it does

| # | Capability | Where it lives |
|---|---|---|
| 1 | **Automated execution** from preset rules | `libs/core/.../strategy/`, `execution/`, `apps/worker/` |
| 2 | **Backtesting** on historical data | `libs/core/.../backtest/` |
| 3 | **Risk management** — stops, sizing, exposure caps, kill switch | `libs/core/.../risk/` |
| 4 | **Real-time data** for execution | `libs/core/.../data/`, Redis pub/sub |
| 5 | **Paper trading** on live data, no real money | `libs/core/.../brokers/paper.py` + Alpaca paper endpoint |
| 6 | **Analytics & reporting** | `libs/core/.../analytics/` |
| 7 | **Dashboard**, auto-refresh every 5 min | `apps/web/` |

## Architecture in one paragraph

A **hexagonal (ports & adapters)** core. `libs/core` holds pure domain logic — strategies,
risk rules, the backtest engine, portfolio maths — and depends on nothing external. Every
outside system (broker, market-data feed, database, clock) sits behind a `Protocol` in a
`ports.py` module. That is what makes requirement #5 cheap: **backtest, paper and live are
the same code path with a different adapter bound**, so a strategy that passes a backtest
runs unmodified against live data. `apps/api` (FastAPI) serves the dashboard and REST/WS;
`apps/worker` runs the live strategy loop. Full detail in
[`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md).

```
                    ┌──────────────┐
   React dashboard ─┤  apps/api    ├─ REST + WebSocket
                    └──────┬───────┘
                           │        ┌─────────────────────┐
                    ┌──────┴───────┐│  libs/core (pure)   │
   market data ────▶│ apps/worker  ├┤  strategy · risk    │
   broker    ◀─────▶│ strategy loop││  backtest · analytics│
                    └──────────────┘└─────────────────────┘
                           │
                  Postgres/TimescaleDB · Redis
```

## Quickstart

```bash
cp .env.example .env          # then fill in ALPACA_API_KEY / ALPACA_API_SECRET
make up                       # postgres+timescale, redis, api, worker, web
make migrate                  # create schema
make seed                     # backfill a little bar history
open http://localhost:5173    # dashboard
open http://localhost:8000/docs   # OpenAPI
```

Local (no containers) development:

```bash
make install                  # uv sync + npm install
make dev-api                  # uvicorn --reload
make dev-web                  # vite
make test
```

## Safety

This software can place real orders with real money. Before you touch
`ATP_RUN_MODE=live`, read [`docs/SAFETY.md`](docs/SAFETY.md) — it is not optional
reading. The default run mode is `paper`, and switching to `live` requires an explicit
env flag plus a typed confirmation.

## Documentation

| Doc | Read it when |
|---|---|
| [ARCHITECTURE.md](docs/ARCHITECTURE.md) | You need the module map and data flow |
| [SAFETY.md](docs/SAFETY.md) | **Before enabling live trading, always** |
| [STRATEGY_AUTHORING.md](docs/STRATEGY_AUTHORING.md) | Writing a new strategy or rule set |
| [BACKTESTING.md](docs/BACKTESTING.md) | Running backtests; avoiding lookahead bias |
| [RISK.md](docs/RISK.md) | Configuring stops, sizing and limits |
| [DATA.md](docs/DATA.md) | Market-data ingestion, storage, backfill |
| [DASHBOARD.md](docs/DASHBOARD.md) | Frontend conventions, the 5-min refresh |
| [API.md](docs/API.md) | REST/WS surface and conventions |
| [TESTING.md](docs/TESTING.md) | Test layout and what must be covered |
| [DEPLOYMENT.md](docs/DEPLOYMENT.md) | Shipping it somewhere |
| [RUNBOOK.md](docs/RUNBOOK.md) | Something is broken in production |
| [GLOSSARY.md](docs/GLOSSARY.md) | A trading term is unfamiliar |
| [adr/](docs/adr/) | "Why is it built this way?" |

`CLAUDE.md` carries the conventions AI coding agents (and new humans) must follow.

## Licence

See [LICENSE](LICENSE). No warranty — **this is not financial advice and you trade at
your own risk.**
