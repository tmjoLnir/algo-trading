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

**Some unit tests have a Markdown file as their subject.** They are ordinary
tests in `tests/unit/`, and they are here because two of this repository's
records — what is built, and what is wrong with what is built — are documents
that go stale silently and are believed while they do:

| Test | Holds |
|---|---|
| `test_roadmap_summary.py` | `docs/ROADMAP.md`'s summary tables against its checkboxes |
| `test_roadmap_wip_markers.py` | the *format* of its `wip` markers (`scripts/check_roadmap_wip.py` asks GitHub about their truth, in CI) |
| `test_audit_summary.py` | `AUDIT.md`'s tables, header and §8 against its 82 findings |
| `test_audit_citations.py` | that every `file:line` in `AUDIT.md` still names a file and a line inside it |

None of them can check whether a document is *right* — only whether it still
agrees with itself and with the tree. That is a narrower claim than it sounds
and it is worth having: `AUDIT.md` §10 exists because six days of drift went
unnoticed, and §10.6 records that the first of these two failed on its first
run, on a disagreement `AUDIT.md` had with itself.

## Non-negotiable

**Tests never touch a live endpoint.** `conftest.py` hard-fails any session
where `ATP_RUN_MODE == "live"`. Broker interactions use the in-memory fake.

**A test session reads no ambient configuration.** `Settings` has two sources —
the process environment and `env_file=".env"` — and `conftest.pytest_configure`
takes both away: it pops the alert credentials out of the environment and
detaches the `.env` from every settings model. So a bare `Settings()` in a test
means *the documented defaults*, on a fresh CI clone and on the machine of an
operator whose platform is configured and trading, which are otherwise two
different answers. Both halves matter and each was found the hard way: a machine
with `ALERT_TELEGRAM_*` exported, and then the same pair reached through a `.env`
instead. A test that genuinely wants a file passes `_env_file=<path>` when it
constructs `Settings`, which overrides this for that instance only.

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
