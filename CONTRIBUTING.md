# Contributing

## Setup

```bash
make install
make up                   # writes .env from .env.example if you have none
make migrate
make test
```

Fill in `ALPACA_*` (paper keys) in `.env` before moving `ATP_RUN_MODE` off
`backtest`. The worker is not in the default stack — see `docker-compose.yml`.

## Workflow

1. Branch from `main`.
2. Make the change. Add tests.
3. `make check` — lint, typecheck, tests. Green before you push.
4. Open a PR; fill in the template honestly, including the trading checklist.

Commits follow Conventional Commits: `feat(risk): add ATR trailing stop`.

## What gets a change rejected

- Money as `float` (`Decimal`, always)
- Naive datetimes
- A new order path that bypasses `OrderRouter` / `RiskEngine`
- A risk check weakened or a test assertion relaxed to make something pass
- Network I/O inside `libs/core`
- A secret in a commit, a log line, or a test fixture
- Order-flow, risk or P&L logic with only happy-path tests

`CLAUDE.md` has the full working agreement — read it before your first PR.

## Review

Anything under `risk/`, `execution/` or `brokers/` needs a second reviewer
(see CODEOWNERS). These are the paths where a bug costs money rather than time.

## Reporting a security issue

Do not open a public issue. See SECURITY.md.
