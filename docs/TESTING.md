# Testing

A bug here costs money, so the bar is higher than "it renders".

## Layout

```
tests/
  unit/         pure, fast, no I/O. The bulk.
  integration/  Postgres + Redis. @pytest.mark.integration
  e2e/          full stack against the paper account. @pytest.mark.e2e
```

```bash
make test-unit          # run constantly
make test               # everything
```

## Non-negotiable

**Tests never touch a live endpoint.** `conftest.py` hard-fails any session
where `ATP_RUN_MODE == "live"`. Broker interactions use the in-memory fake.

## What must be tested

Failure paths, not just happy paths, for anything touching order flow, risk or
P&L:

| Area | Must cover |
|---|---|
| `Position.apply_fill` | add, partial reduce, full close, **flip through zero**, shorts |
| `Order.apply_fill` | partial fills, VWAP correctness, overfill rejection |
| Risk rules | each rule blocks what it should and allows what it should |
| Daily loss limit | blocks entries, **allows exits** |
| Stops | long and short, monotonic trailing, triggers off high/low not close |
| Order state machine | every illegal transition raises; stale events discarded |
| Backtest engine | no lookahead; next-bar fills; volume cap |
| Metrics | against known-good fixtures |
| Broker adapter | timeout does not double-submit; same `client_order_id` |
| Reconciliation | mismatch halts trading |

## Property tests

Some invariants are better stated than enumerated. `hypothesis` is a dependency
for this reason:

- Any fill sequence: `realized + unrealized` equals total P&L computed directly.
- Portfolio equity never changes from a mark that does not move a price.
- A trailing stop, over any price path, never decreases (for a long).
- Order VWAP always lies between the min and max fill price.

The flip-through-zero case in particular is easier to get right by property test
than by example.

## Fixtures

- `fake_broker` — in-memory `BrokerPort`, controllable fills and failures
- `sample_bars` — deterministic OHLCV, including a gap and a split
- `frozen_clock` — `SimulatedClock` at a fixed instant
- Golden-file metrics fixtures with hand-verified expected values

## Coverage

Not a target to game. `libs/core/risk`, `execution` and `backtest` should be
near-complete on both branches; a router with a stub body does not need a test
asserting it raises `NotImplementedError`.
