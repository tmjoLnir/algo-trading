# CLAUDE.md — working agreement for this repository

Instructions for AI coding agents and new contributors. Read this before changing code.
Domain background lives in `docs/`; this file is about *how we work here*.

---

## 1. Non-negotiable rules

These exist because this codebase moves real money. Violating one is a blocking review
comment, not a nit.

1. **Money and quantities are `Decimal`, never `float`.** Binary floats cannot represent
   `0.1`; accumulated error in a P&L ledger is a correctness bug, not a rounding detail.
   Prices, quantities, cash, fees — all `decimal.Decimal`. Floats are fine for indicator
   maths and statistics (Sharpe, correlations) where you are not tracking a balance.
2. **All timestamps are timezone-aware UTC.** Never `datetime.now()` — use
   `atp_core.clock.Clock.now()`. Naive datetimes are rejected at the domain boundary.
   Convert to exchange-local time only for display.
3. **No network calls inside `libs/core`.** Core is pure. If you need the outside world,
   define a `Protocol` in the relevant `ports.py` and write an adapter in
   `brokers/`, `data/providers/` or `persistence/`.
4. **Every order carries a `client_order_id`** generated deterministically before submit.
   Retries reuse it. This is the only thing standing between a network timeout and a
   duplicate position.
5. **Every order passes `RiskEngine.validate()` before it reaches a broker adapter.**
   There is exactly one submit path — `execution.router.OrderRouter.submit()`. Do not add
   another. Do not call a broker adapter directly from a strategy.
6. **Never commit secrets.** Keys live in `.env` (gitignored) and are read through
   `atp_core.config.Settings`. No API key ever appears in a log line, an exception
   message, a test fixture, or a commit.
7. **Tests never touch a live endpoint.** `conftest.py` hard-fails any test session where
   `ATP_RUN_MODE == "live"`. Broker interactions in tests use the in-memory fake.
8. **Live mode is opt-in and loud.** `ATP_RUN_MODE=live` additionally requires
   `ATP_ALLOW_LIVE_TRADING=true`; the worker logs a `CRITICAL` banner at startup. Never
   change the default, and never widen the guard to make a test pass.

## 2. Repository map

```
apps/
  api/      FastAPI — REST + WebSocket. Thin: validate, delegate to core, serialise.
  worker/   Long-running processes: live strategy loop, data ingestion, scheduled jobs.
  web/      React + TypeScript + Vite dashboard.
libs/
  core/     The platform. Pure Python, no I/O. Everything important is here.
infra/      Dockerfiles, Alembic migrations, DB init SQL.
scripts/    Operator entry points (backfill, run a backtest, seed).
tests/      unit/ (pure, fast) · integration/ (DB, Redis) · e2e/
docs/       Architecture, domain guides, ADRs, runbook.
```

Inside `libs/core/src/atp_core/`:

| Package | Responsibility |
|---|---|
| `domain/` | Entities and value objects. No behaviour that needs I/O. |
| `strategy/` | `Strategy` base class, the declarative rule spec, the registry. |
| `indicators/` | Pure functions over price series. |
| `data/` | Market-data ports, bar storage, the realtime stream consumer. |
| `brokers/` | `BrokerPort` + adapters (Alpaca live, Alpaca paper, simulated, fake). |
| `execution/` | Order router, order state machine, broker reconciliation. |
| `risk/` | Pre-trade validation, stop management, the kill switch. |
| `backtest/` | Historical event loop, portfolio simulation, cost models, metrics. |
| `analytics/` | Performance statistics and report generation. |
| `persistence/` | SQLAlchemy models, repositories, session management. |

**Dependency rule:** `apps/*` → `libs/core`. Never the reverse. Within core:
`domain` ← everything; `domain` imports nothing from its siblings.

## 3. Commands

```bash
make install      # uv sync --all-packages && npm --prefix apps/web install
make up           # docker compose up -d
make down
make migrate      # alembic upgrade head
make revision m="add orders table"
make test         # pytest + vitest
make test-unit    # pytest tests/unit -q   (fast; run this constantly)
make lint         # ruff check + ruff format --check + eslint
make typecheck    # mypy libs apps && tsc --noEmit
make fmt          # ruff format + prettier
make check        # lint + typecheck + test  ← must pass before you push
```

Python is managed with **uv** (workspace at the repo root). Add a dependency with
`uv add --package atp-core <name>`, never by hand-editing a lockfile.

## 4. Conventions

**Python.** 3.12+. Full type annotations on every public function; `mypy --strict` over
`libs/core`. Pydantic v2 for anything crossing a boundary (API payloads, config, rule
specs); frozen dataclasses for internal value objects. Structured logging via `structlog`
— `log.info("order.submitted", order_id=..., symbol=...)`, never f-strings in log calls.
Custom exceptions derive from `atp_core.errors.ATPError`. Async by default in `apps/`;
core stays synchronous and pure unless it genuinely needs concurrency.

**Naming.** `symbol` is always an uppercase ticker string. `qty` is signed only inside a
position (negative = short); order quantities are unsigned with an explicit `Side`.
`price` is always in the instrument's quote currency. Timestamps end in `_at`.

**TypeScript.** Strict mode. TanStack Query owns all server state — no `useEffect` fetch
loops. Types for API payloads are **generated** from the OpenAPI schema
(`make gen-types`); do not hand-write them and let them drift.

**Migrations.** Every schema change gets an Alembic revision. Never edit an applied
migration. Bar tables are TimescaleDB hypertables — see `infra/db/init/`.

**Commits.** Conventional Commits (`feat:`, `fix:`, `refactor:`, `docs:`, `test:`,
`chore:`). Scope by package where useful: `feat(risk): add ATR trailing stop`.

## 5. Things that are easy to get wrong here

- **Lookahead bias.** A strategy handling the bar closing at 10:30 may use data up to and
  including 10:30's close, and its order fills at the *next* bar. The backtest engine
  enforces this; if you "fix" a test by relaxing it, you have invented a profitable
  strategy that loses money in production. See `docs/BACKTESTING.md`.
- **Survivorship bias.** A universe built from today's index membership excludes every
  company that went bankrupt. Backtests on it lie.
- **Corporate actions.** Splits and dividends change historical prices. Store both raw
  and adjusted closes; backtest on adjusted, trade on raw.
- **Partial fills.** An order is not binary. `Order` tracks `filled_qty` and
  `avg_fill_price`; a position update must handle a fill sequence, not one fill.
- **Reconnect gaps.** A dropped WebSocket loses ticks. On reconnect, backfill the gap via
  REST before resuming — do not just resume and hope.
- **The clock.** Backtests use `SimulatedClock`. Anything reading wall-clock time
  directly will behave differently in a backtest than in production, which is the
  hardest class of bug in this system to notice.

## 6. Definition of done

A change is done when: `make check` passes; new logic has unit tests; anything touching
order flow, risk or P&L has tests for the failure path, not only the happy path; public
functions have docstrings explaining *why*; docs are updated if behaviour changed; and an
ADR exists if you made an architectural decision.

## 7. For AI agents specifically

- Read the relevant `docs/` page before implementing a subsystem — the domain rules there
  are not reconstructable from the code alone.
- Prefer filling in an existing `NotImplementedError` stub over inventing a new module.
  The skeleton's shape is deliberate.
- If a requirement seems to conflict with a rule in §1, stop and ask. §1 wins by default.
- Do not weaken a guardrail, a validation, or a test assertion to make something pass.
  Report the conflict instead.
- Never commit or push unless explicitly asked.
