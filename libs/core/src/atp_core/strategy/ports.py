"""Persistence ports for the decision record: strategies and their signals.

Separate from `execution/ports.py` because these store a different kind of fact.
An order is what we *did* and its authority is the venue; a signal is what the
strategy *decided*, and nothing outside this platform holds a copy. A lost order
can be recovered by asking the broker. A lost signal is gone.

**Why these exist at all.** `persistence.models.SignalRow` sat in the schema
with no writer, and `PostgresOrderRepository` stored `strategy_id` and
`signal_id` as literal `None` with a comment saying a strategies row would have
to exist first. So an order could not be traced back to the decision that caused
it, which is the whole of attribution: "which strategy made this money" and "why
did this position close" are both that join. docs/DASHBOARD.md listed it under
"Not built yet"; this is the half that was missing.

**Ordering matters, and the foreign keys enforce it.** `signals.strategy_id`
references `strategies.id`, and `orders.signal_id` references `signals.id`. So
the sequence is: ensure the strategy, record the signal, save the order. A
caller that saves an order naming a signal nobody recorded gets an integrity
error rather than a null, which is the correct outcome — a silent null is how
this became invisible in the first place.

Conformance to these protocols is checked at each adapter rather than here:
`persistence/strategies.py` and `persistence/signals.py` each end with a
`_typecheck` function mypy verifies, matching every other port in the platform.

**A blocked signal is recorded too**, and that is the point rather than an
edge case. `SignalRow`'s own docstring says so: a signal the risk engine refused
is exactly what you want when asking why a strategy underperformed its backtest.
A strategy whose every idea was refused looks, from the orders table alone,
identical to a strategy that had no ideas.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from datetime import datetime

    from atp_core.domain import Signal


@dataclass(frozen=True, slots=True)
class StrategyRecord:
    """The identity of a strategy, as stored.

    Deliberately thin. `persistence.models.StrategyRow` carries a great deal
    more — a rule spec, a universe, a risk config, a lifecycle state — because
    it is also the row a future strategy-management API will edit. What a
    *running* worker knows is only this: which strategy it is, what kind of
    thing it is, and how it was parameterised. Writing the rest from the worker
    would mean inventing values for fields nothing running has an opinion about,
    and a `state` of "draft" stamped over an "active" row by a restart would be
    a worker overwriting configuration it does not own.

    `id` is the strategy's stable identity, and for a coded strategy that is its
    registered name rather than a generated uuid: `Signal.strategy_id` is
    populated with `Strategy.name` everywhere in the platform, and minting a
    separate key here would mean every signal referenced a strategy row that
    did not exist.
    """

    id: str
    name: str
    kind: str  # "coded" | "ruleset"
    class_name: str | None = None
    params: dict[str, object] | None = None
    universe: tuple[str, ...] = ()
    timeframe: str = "1d"


@dataclass(frozen=True, slots=True)
class SignalOutcome:
    """What became of a signal, recorded alongside it.

    Split from `Signal` rather than added to it, because a `Signal` is what the
    strategy said and this is what the platform did about it. Folding the two
    together would let a strategy construct a signal that claims it was acted
    on.

    `rejected_by` names the rule and `rejection_reason` is what a human reads.
    Both are None for a signal that was submitted, and both are None for one the
    router reported as `no_action` — an exit against an already-flat position,
    say — which is `acted_on=False` with nothing to blame. That third state is
    real and the two fields being None is how it is told apart from a refusal;
    counting it as a rejection would inflate the number an operator reads to
    judge whether risk is set too tight.
    """

    acted_on: bool
    rejection_reason: str | None = None
    rejected_by: str | None = None


class StrategyRepository(Protocol):
    """The `strategies` table — the row every signal and order points at."""

    async def ensure(self, record: StrategyRecord) -> None:
        """Make sure this strategy has a row, creating it if it does not.

        Idempotent, and called on every worker boot: the runner does not know
        whether it has run before and should not have to ask. An existing row is
        left as it is apart from `updated_at` — see `StrategyRecord` for why a
        worker must not overwrite configuration it did not author.

        This is the foreign key `signals.strategy_id` needs. Calling it before
        the first signal is not an optimisation; without it every signal write
        fails.
        """
        ...

    async def get(self, strategy_id: str) -> StrategyRecord | None:
        """The stored identity, or None if this strategy has no row yet."""
        ...


class SignalRepository(Protocol):
    """Durable record of what strategies decided, acted on or not."""

    async def save(self, signal: Signal, outcome: SignalOutcome) -> None:
        """Write one decision and its fate.

        Idempotent on the signal's id, so a retried evaluation cannot record the
        same decision twice. The outcome is updated on a re-save — a signal
        recorded as refused and later submitted is one signal whose fate
        changed, not two signals.

        Requires the strategy's row to exist (`StrategyRepository.ensure`).
        """
        ...

    async def recent(
        self, strategy_id: str | None = None, *, limit: int = 200
    ) -> list[tuple[Signal, SignalOutcome]]:
        """The newest signals first, optionally for one strategy.

        Newest first because this feeds a screen, and the only question a
        signal feed answers is "what just happened". The runner keeps an
        in-memory ring for the live view; this is what survives a restart.
        """
        ...

    async def between(
        self, start: datetime, end: datetime, *, strategy_id: str | None = None
    ) -> list[tuple[Signal, SignalOutcome]]:
        """Every signal in `[start, end]`, oldest first.

        What attribution over a period reads. Oldest first, unlike `recent`,
        because a period report is read forwards. Inclusive of both ends, for
        the same reason `PortfolioRepository.equity_history` is: the bounds are
        instants the caller computed from a session or a day, and quietly
        dropping the last one puts the wrong number in a report.
        """
        ...
