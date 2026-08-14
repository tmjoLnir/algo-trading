"""Time, behind a port.

Every read of "now" in this codebase goes through a `Clock`. A backtest binds
`SimulatedClock` and time advances bar by bar; production binds `SystemClock`.
Code that calls `datetime.now()` directly works in production and lies in a
backtest — the hardest bug class in this system to notice (CLAUDE.md §5).
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time
from typing import Protocol, runtime_checkable

MARKET_TZ = "America/New_York"
REGULAR_OPEN = time(9, 30)
REGULAR_CLOSE = time(16, 0)


@runtime_checkable
class Clock(Protocol):
    def now(self) -> datetime:
        """Current time, tz-aware UTC."""
        ...


class SystemClock:
    """Wall-clock time. Production and paper trading."""

    def now(self) -> datetime:
        return datetime.now(UTC)


class SimulatedClock:
    """Backtest time. Advanced explicitly by the backtest engine.

    Never advances on its own — if a metric looks wrong because time did not
    move, the engine forgot to call `set()`.
    """

    def __init__(self, start: datetime) -> None:
        if start.tzinfo is None:
            raise ValueError("SimulatedClock needs a tz-aware start")
        self._now = start.astimezone(UTC)

    def now(self) -> datetime:
        return self._now

    def set(self, ts: datetime) -> None:
        if ts < self._now:
            raise ValueError(f"clock cannot go backwards: {ts} < {self._now}")
        self._now = ts.astimezone(UTC)


class TradingCalendar:
    """Exchange sessions: holidays, half-days, and the current session window.

    Backed by `pandas_market_calendars`. Do not approximate this with
    "weekdays 9:30–16:00" — the US market has ~10 holidays and several 13:00
    early closes a year, and a strategy that submits into a closed market
    accumulates rejects.
    """

    def __init__(self, exchange: str = "NYSE") -> None:
        self.exchange = exchange

    def is_trading_day(self, day: date) -> bool:
        raise NotImplementedError

    def session_bounds(self, day: date) -> tuple[datetime, datetime] | None:
        """(open, close) in UTC, or None if the market is shut that day."""
        raise NotImplementedError

    def is_open(self, ts: datetime) -> bool:
        raise NotImplementedError

    def next_open(self, after: datetime) -> datetime:
        raise NotImplementedError

    def minutes_to_close(self, ts: datetime) -> int | None:
        """Used by time-based exits and end-of-day flattening."""
        raise NotImplementedError
