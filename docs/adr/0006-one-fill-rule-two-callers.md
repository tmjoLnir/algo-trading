# 6. One fill rule, shared by the backtest engine and the simulated broker

**Status:** Accepted · 2026-08-17

## Context
Two components decide when a resting order fills and at what price: the
backtest engine, which walks historical bars, and `SimulatedBroker`, which is
the `BrokerPort` bound in backtests and in paper trading against our own
simulator rather than Alpaca's.

The engine shipped first (#25) with the rule as a private method,
`BacktestEngine._intended_price`. Implementing `SimulatedBroker` meant either
calling that private method, duplicating it, or moving it somewhere both can
reach.

Duplicating is the tempting option, because the two callers *look* different —
one is an event loop over a fixed series, the other an adapter behind an async
port. They are not different. Both are answering the same question about the
same bar.

The reason this is worth an ADR rather than a refactor nobody records: the two
copies would not diverge loudly. They would diverge by a `<` becoming a `<=` on
a limit touched exactly at the bar's low, and the symptom would be a paper run
whose fills do not match the backtest that approved the strategy — with nothing
failing, no test going red, and the discrepancy showing up as "paper
underperformed, markets must have moved" rather than as a bug.

## Decision
The rule lives in `atp_core.execution.matching.intended_price`, a pure function
over `(Order, Bar)`. `BacktestEngine._intended_price` delegates to it and
`SimulatedBroker.on_bar` calls it directly.

It answers *where the order touched*, not *what it paid*: slippage and
commission stay with the caller's cost model. That split is what lets the two
share the touch rule while pricing the crossing differently — and it is why the
function is in `execution/` rather than in `backtest/`, since a broker adapter
importing the backtesting package to fill an order would have the dependency
backwards.

## Consequences
- A change to fill semantics lands in both callers or in neither. There is one
  place to audit and one place for a reviewer to look.
- `execution/matching.py` imports only `domain` and `errors`, so neither
  `brokers/` nor `backtest/` gains a dependency on the other.
- The unmodelled-order-type path now raises `ExecutionError` where the engine
  previously raised `BacktestError`. Both derive from `ATPError`; no test
  asserted the narrower type. `SimulatedBroker._quote_price` raises
  `ExecutionError` too, so a caller does not need to catch two exception types
  depending on whether it fed the simulator a bar or a quote.
- `require_through` — demand the bar trade *through* a limit rather than merely
  touch it — is now a parameter rather than a fixed choice. The engine keeps
  its existing optimistic reading by default. It exists because the honest
  answer for a thin book is that we do not know whether a touch at the extreme
  filled, and both readings should be available to whoever calibrates against
  real fills.
- The extraction is behaviour-preserving, and the evidence is the engine's
  hand-computed 20-bar fixture (`TestAgainstKnownFixture`) passing unchanged —
  the test written precisely to catch a fill-timing change.

## Alternatives
**Duplicate the rule in the simulator.** Rejected above: the divergence is
silent and shows up as a strategy that paper-traded differently from its
backtest, which is the one comparison paper trading exists to make. This
codebase already took the same position for indicators, where the series form
is the primitive and the scalar is its last element because "two
implementations of it are two chances to get it subtly wrong".

**Have `SimulatedBroker` call `BacktestEngine._intended_price`.** Makes a
broker adapter depend on the backtesting package, and on a private method of a
class it otherwise has nothing to do with.

**Give `SimulatedBroker` deliberately *different*, more pessimistic fills, so
paper is a stricter test than the backtest.** Appealing, and rejected: a
simulator that is stricter in an unstated way is not more conservative, it is
just uncalibrated, and it destroys the one property that makes a paper run
interpretable — that a difference between paper and backtest means something
about the strategy rather than about the two simulators. `require_through`
gives that pessimism as an explicit, per-run choice instead.
