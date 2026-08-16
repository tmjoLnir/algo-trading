"""Time, behind a port.

Every read of "now" in this codebase goes through a `Clock`. A backtest binds
`SimulatedClock` and time advances bar by bar; production binds `SystemClock`.
Code that calls `datetime.now()` directly works in production and lies in a
backtest — the hardest bug class in this system to notice (CLAUDE.md §5).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from functools import lru_cache
from typing import Any, Protocol, runtime_checkable
from zoneinfo import ZoneInfo

from atp_core.errors import ConfigError

MARKET_TZ = "America/New_York"
REGULAR_OPEN = time(9, 30)
REGULAR_CLOSE = time(16, 0)

#: Upper bound on how far `next_open` will scan. Generous on purpose: the
#: longest closure in NYSE history was four and a half months in 1914, and a
#: bound that cannot span it would turn a historical query into a false error.
_MAX_LOOKAHEAD_DAYS = 400


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


@lru_cache(maxsize=8)
def _exchange_calendar(exchange: str) -> Any:
    """The `pandas_market_calendars` calendar for `exchange`, built once.

    Imported inside the function rather than at module scope on purpose: it
    pulls in pandas, which costs the better part of a second, and nearly
    everything that imports `atp_core.clock` only wants `Clock.now()`.
    """
    import pandas_market_calendars as mcal

    try:
        return mcal.get_calendar(exchange)
    except RuntimeError as exc:
        # The library's message lists all 200-odd registered names, which is
        # noise in a config error. Name the one that was wrong instead.
        raise ConfigError(
            f"unknown exchange calendar {exchange!r} — see pandas_market_calendars"
            ".get_calendar_names() for the registered names"
        ) from exc


@lru_cache(maxsize=8)
def _holiday_range(exchange: str) -> tuple[date, date] | None:
    """The span the exchange's holiday rules actually cover, if it declares any.

    Outside it `pandas_market_calendars` still drops weekends but stops dropping
    holidays, so 1 January 2201 comes back as an ordinary session. Knowing where
    the rules stop is what lets this class refuse instead: a calendar that is
    confidently wrong about a closure is worse than one that says it does not
    know.
    """
    holidays = getattr(_exchange_calendar(exchange).holidays(), "holidays", None)
    if not holidays:
        return None
    # `numpy.datetime64` always stringifies as YYYY-MM-DD[...], whatever its
    # unit; `.item()` does not — it hands back an int at nanosecond precision.
    return (
        date.fromisoformat(str(min(holidays))[:10]),
        date.fromisoformat(str(max(holidays))[:10]),
    )


@dataclass(frozen=True, slots=True)
class Session:
    """One trading session, in UTC.

    `day` is the exchange-local calendar date the session belongs to, which is
    the only stable name for it: the same session opens at 14:30Z in winter and
    13:30Z in summer.
    """

    day: date
    open_at: datetime
    close_at: datetime
    #: A half-day — roughly three a year on the NYSE. Code that assumes 16:00
    #: submits into a closed market on every one of them.
    is_early_close: bool = False


class TradingCalendar:
    """Exchange sessions: holidays, half-days, and the current session window.

    Backed by `pandas_market_calendars`. Do not approximate this with
    "weekdays 9:30–16:00" — the US market has ~10 holidays and several 13:00
    early closes a year, and a strategy that submits into a closed market
    accumulates rejects.

    Sessions are materialised a year at a time and cached on the instance, so
    the first question about a year costs ~50ms and every later one is a dict
    lookup. Build one and share it rather than constructing one per call.

    Assumes a session opens and closes on the same exchange-local date, which is
    true of every equity venue this platform trades and not of overnight futures
    calendars.
    """

    def __init__(self, exchange: str = "NYSE") -> None:
        self.exchange = exchange
        # Resolved now rather than on the first query, so a typo in an exchange
        # name fails where it was made.
        calendar = _exchange_calendar(exchange)
        #: Exchange-local time zone. Taken from the calendar rather than from
        #: `MARKET_TZ` so a non-US exchange is not silently read in New York.
        self.tz = ZoneInfo(str(calendar.tz))
        self._sessions_by_year: dict[int, dict[date, Session]] = {}

    # ── sessions ────────────────────────────────────────────────────────────

    def session_on(self, day: date) -> Session | None:
        """The session on `day`, or None if the market was shut."""
        return self._year(day.year).get(day)

    def is_trading_day(self, day: date) -> bool:
        return day in self._year(day.year)

    def session_bounds(self, day: date) -> tuple[datetime, datetime] | None:
        """(open, close) in UTC, or None if the market is shut that day."""
        session = self.session_on(day)
        return None if session is None else (session.open_at, session.close_at)

    def sessions(self, first: date, last: date) -> list[Session]:
        """Every session in `[first, last]`, inclusive, chronological.

        Inclusive rather than half-open because the arguments are calendar days
        rather than instants: "2020-01-01 to 2024-12-31" is how an operator says
        five years, and making them exclude the last day would be a trap.
        """
        if last < first:
            raise ValueError(f"first must be on or before last, got {first} > {last}")
        return [
            session
            for year in range(first.year, last.year + 1)
            for day, session in self._year(year).items()
            if first <= day <= last
        ]

    def day_bounds(self, day: date) -> tuple[datetime, datetime]:
        """The UTC instants bounding an exchange-local calendar date.

        `[midnight, next midnight)` in exchange-local terms, which is 23, 24 or
        25 hours wide depending on daylight saving. This is what a daily bar's
        timestamp is anchored to — Alpaca stamps a daily bar at 00:00 New York
        — so gap detection needs it to decide which session a stored daily bar
        belongs to (docs/DATA.md).
        """
        start = datetime.combine(day, time(0, 0), tzinfo=self.tz)
        end = datetime.combine(day + timedelta(days=1), time(0, 0), tzinfo=self.tz)
        return start.astimezone(UTC), end.astimezone(UTC)

    # ── instants ────────────────────────────────────────────────────────────

    def is_open(self, ts: datetime) -> bool:
        """Is the market trading at `ts`?

        Half-open: the instant of the open counts, the instant of the close does
        not. An order timestamped exactly 16:00:00 would be rejected by the
        venue, so reporting the market as open then would be a lie in the
        direction that costs money.
        """
        ts = _to_utc(ts, "ts")
        session = self.session_on(self._local_date(ts))
        return session is not None and session.open_at <= ts < session.close_at

    def next_open(self, after: datetime) -> datetime:
        """The first session open strictly after `after`.

        Strictly, so that the natural sleep loop — "while the market is shut,
        wait until `next_open()`" — cannot spin: an open that returned `after`
        itself would busy-wait at the boundary.
        """
        after = _to_utc(after, "after")
        day = self._local_date(after)
        for _ in range(_MAX_LOOKAHEAD_DAYS):
            session = self.session_on(day)
            if session is not None and session.open_at > after:
                return session.open_at
            day += timedelta(days=1)
        raise ValueError(
            f"no {self.exchange} session opens within {_MAX_LOOKAHEAD_DAYS} days of {after}"
        )

    def minutes_to_close(self, ts: datetime) -> int | None:
        """Whole minutes left in the session, or None if the market is shut.

        Used by time-based exits and end-of-day flattening. Rounded down, so
        "flatten with 5 minutes left" fires at 15:55 rather than at 15:55:59 —
        a late exit is the expensive direction.
        """
        ts = _to_utc(ts, "ts")
        session = self.session_on(self._local_date(ts))
        if session is None or not (session.open_at <= ts < session.close_at):
            return None
        return int((session.close_at - ts).total_seconds() // 60)

    # ── internals ───────────────────────────────────────────────────────────

    def _local_date(self, ts: datetime) -> date:
        return ts.astimezone(self.tz).date()

    def _year(self, year: int) -> dict[date, Session]:
        """Every session in `year`, keyed by exchange-local date, ascending."""
        cached = self._sessions_by_year.get(year)
        if cached is not None:
            return cached

        covered = _holiday_range(self.exchange)
        if covered is not None and not (covered[0].year <= year <= covered[1].year):
            raise ValueError(
                f"{self.exchange} holiday rules only cover {covered[0].year} to {covered[1].year}; "
                f"outside that range weekends are still excluded but holidays are not, so "
                f"{year} would report closures as ordinary sessions"
            )

        calendar = _exchange_calendar(self.exchange)
        schedule = calendar.schedule(
            start_date=date(year, 1, 1).isoformat(), end_date=date(year, 12, 31).isoformat()
        )
        # A tz-aware `time`, so it cannot be compared against a naive one.
        regular_close = getattr(calendar, "close_time", None)
        regular_close = None if regular_close is None else regular_close.replace(tzinfo=None)

        sessions: dict[date, Session] = {}
        # By column name rather than positionally: some calendars carry extra
        # columns (lunch breaks), and `schedule` is already sorted ascending.
        for index, open_ts, close_ts in zip(
            schedule.index, schedule["market_open"], schedule["market_close"], strict=True
        ):
            day: date = index.date()
            close_at: datetime = close_ts.to_pydatetime().astimezone(UTC)
            sessions[day] = Session(
                day=day,
                open_at=open_ts.to_pydatetime().astimezone(UTC),
                close_at=close_at,
                is_early_close=(
                    regular_close is not None
                    and close_at.astimezone(self.tz).time() < regular_close
                ),
            )

        self._sessions_by_year[year] = sessions
        return sessions


def _to_utc(ts: datetime, field: str) -> datetime:
    """Normalise to UTC, refusing naive input at the boundary (rule §1.2)."""
    if ts.tzinfo is None:
        raise ValueError(f"{field} must be timezone-aware (rule §1.2), got naive {ts!r}")
    return ts.astimezone(UTC)
