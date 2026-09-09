"""The `Strategy` contract.

A strategy is a pure decision function over market events. It emits `Signal`s
and nothing else — it never sizes a position, never calls a broker, never reads
the clock (rule §1.5). That restriction is what lets the identical object run in
a backtest, in paper and in live without a branch anywhere inside it.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Any, ClassVar

from atp_core.domain import Timeframe
from atp_core.errors import StrategyError

if TYPE_CHECKING:
    from atp_core.domain import Bar, Fill, Order, Quote, Signal
    from atp_core.strategy.context import StrategyContext


class Strategy(ABC):
    """Base class for every strategy.

    Lifecycle:

        on_start()                      once, before any data
        on_bar(ctx, bar)      ← primary hook, once per completed bar
        on_quote(ctx, quote)  ← optional, for intrabar stop monitoring
        on_fill(ctx, order, fill)       when one of our orders executes
        on_stop()                       once, at shutdown

    Implementations MUST be deterministic: identical inputs produce identical
    signals. Non-determinism (wall-clock reads, unseeded randomness, dict
    ordering assumptions) makes a backtest unreproducible and therefore
    worthless as evidence.
    """

    #: Stable identifier used in configs, the registry and the database.
    name: ClassVar[str] = ""
    #: Human-facing description shown on the dashboard.
    description: ClassVar[str] = ""
    #: JSON Schema for `params`, used to validate configs and render the UI form.
    params_schema: ClassVar[dict[str, Any]] = {}

    def __init__(self, params: dict[str, Any] | None = None) -> None:
        self.params = params or {}
        self.validate_params()

    def validate_params(self) -> None:
        """Reject a bad configuration at construction, not at bar 40,000."""
        return None

    @property
    def declared_timeframe(self) -> Timeframe | None:
        """The bar series this strategy's signals are defined over, if it says.

        `None` when the strategy names no series — it is then indifferent, and
        whatever the worker is configured for is the right answer.

        **A declared series is a claim about what the numbers mean, not a
        preference.** `SmaCrossover` defaults to `1d`, so its 20/50 pair is a
        20-day/50-day trend system. Served minute bars it is a 20-minute
        /50-minute scalper — a different strategy with the same name, and the
        one that actually traded 38 round trips on day 2 of the paper week
        while the platform logged the substitution *once*, at 13:33, and ran
        385 more evaluations in silence (docs/paper-week/day-2-review.md, F8).

        Read from `params` so it works for both kinds of strategy: a
        hand-written one takes it from its schema default or the operator's
        override, and a `RuleSet` puts its own `timeframe` there when it
        serialises the spec into `params`.

        The schema default is consulted rather than only the supplied params,
        because a strategy that ships a default has declared one — that is what
        a default is — and reading only the override would make an unconfigured
        strategy look indifferent when it is not. That is exactly the reading
        that let day 2 happen.
        """
        raw = self.params.get("timeframe")
        if raw is None:
            properties = self.params_schema.get("properties", {})
            raw = properties.get("timeframe", {}).get("default")
        if raw is None:
            return None
        if isinstance(raw, Timeframe):
            return raw
        try:
            return Timeframe(raw)
        except ValueError as exc:
            raise StrategyError(
                f"{type(self).__name__} declares timeframe {raw!r}, which is not one this "
                f"platform stores ({', '.join(sorted(t.value for t in Timeframe))})"
            ) from exc

    @property
    @abstractmethod
    def warmup_bars(self) -> int:
        """Bars needed before signals are meaningful.

        An SMA(50) crossover needs 50. The engine feeds these bars to the
        strategy but discards any signals produced during warmup — otherwise
        every backtest opens with a burst of trades taken on partial indicators.
        """

    @abstractmethod
    def on_bar(self, ctx: StrategyContext, bar: Bar) -> list[Signal]:
        """Decide, given a completed bar.

        `ctx` exposes history up to and including `bar` — never beyond. Return
        an empty list to do nothing; returning `None` is a bug.
        """

    def on_quote(self, ctx: StrategyContext, quote: Quote) -> list[Signal]:
        """Optional tick-level hook. Default: ignore quotes."""
        return []

    def on_fill(self, ctx: StrategyContext, order: Order, fill: Fill) -> list[Signal]:
        """React to one of our own executions (e.g. place a protective stop)."""
        return []

    def on_start(self) -> None:
        return None

    def on_stop(self) -> None:
        return None

    def __repr__(self) -> str:
        return f"<{type(self).__name__} name={self.name!r} params={self.params!r}>"
