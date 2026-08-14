"""What a strategy is allowed to see.

`StrategyContext` is a deliberately narrow window onto the world. In a backtest
it is backed by a cursor that physically cannot return a bar later than the one
being processed — the guard against lookahead bias is structural here, not a
convention a strategy author has to remember (docs/BACKTESTING.md).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from datetime import datetime
    from decimal import Decimal

    import numpy as np

    from atp_core.domain import Bar, Position, Timeframe


class StrategyContext(Protocol):
    """Read-only view passed to every strategy hook."""

    @property
    def now(self) -> datetime:
        """Current decision time (UTC). In a backtest this is the bar's close."""
        ...

    @property
    def symbols(self) -> tuple[str, ...]:
        """The universe this strategy trades."""
        ...

    def history(self, symbol: str, timeframe: Timeframe, lookback: int) -> list[Bar]:
        """The last `lookback` completed bars, oldest first.

        Never includes a bar that closes after `now`. Raises `DataGapError` if
        fewer than `lookback` bars exist rather than quietly returning a short
        series — a 20-period SMA over 6 bars is not a 20-period SMA.
        """
        ...

    def closes(self, symbol: str, timeframe: Timeframe, lookback: int) -> np.ndarray:
        """Closing prices as a float array, for indicator maths.

        Floats are correct here: this feeds statistics, not a ledger (§1.1).
        """
        ...

    def last_price(self, symbol: str) -> Decimal | None:
        """Most recent trade price, or None if the symbol has not printed yet."""
        ...

    def position(self, symbol: str) -> Position:
        """Current holding — flat if none. Strategies read this to avoid
        re-entering something they already own."""
        ...

    @property
    def equity(self) -> Decimal:
        """Account equity, for strategies that scale with account size."""
        ...

    def indicator(self, name: str, symbol: str, **kwargs: object) -> float | None:
        """Cached indicator value.

        Shared across strategies within a run — computing SMA(50) on AAPL once
        per bar instead of once per strategy per bar matters when the universe
        is a few hundred symbols.
        """
        ...
