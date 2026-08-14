"""The `Strategy` contract.

A strategy is a pure decision function over market events. It emits `Signal`s
and nothing else — it never sizes a position, never calls a broker, never reads
the clock (rule §1.5). That restriction is what lets the identical object run in
a backtest, in paper and in live without a branch anywhere inside it.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Any, ClassVar

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
