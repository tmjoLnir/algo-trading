"""Calendar-aware gap detection.

Pure: it is handed a calendar, a timeframe and the timestamps that are actually
stored, and it answers which bars are missing. No I/O, so it stays in core
(CLAUDE.md §1.3) and the interesting cases — a holiday, a half-day, a symbol
that had not listed yet — are testable without a database.

**Every weekend and holiday looks like a gap without the calendar**, and an
alert that fires every Saturday is ignored by the second week (docs/DATA.md).
So a "gap" here means: the exchange was open, a bar was therefore expected, and
no bar is stored for it.

What counts as expected depends on the timeframe, and the two cases are not the
same shape:

- **Daily.** One bar per session. Alpaca stamps a daily bar at 00:00 New York
  rather than at the session open, so a stored daily bar is attributed to a
  session by the exchange-local calendar date its timestamp falls in, and the
  window reported for a missing one is that whole local day. A provider that
  stamped daily bars at 00:00 UTC would be attributed to the *previous*
  session — normalise it in the adapter rather than loosening the rule here.
- **Intraday.** One bar per timeframe interval from the session open, for as
  many whole intervals as fit before the close. A session that is not a whole
  number of bars long contributes only its whole bars: whether the vendor emits
  a stub for the remainder is vendor behaviour, and treating its absence as a
  gap would cry wolf on every early close.

A bar is only expected when the requested range covers the whole of it — the
whole session for a daily bar, the whole interval for an intraday one. That is
what keeps "check up to now" from reporting today's unfinished session as a
hole every time it runs.

**A missing intraday bar is not proof of a missing bar.** Alpaca emits no bar
for a minute in which nothing traded, so on an illiquid symbol these windows are
ordinary rather than evidence of an outage. On a liquid symbol, and on daily
bars for anything, they are real.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, timedelta
from typing import TYPE_CHECKING

from atp_core.domain import Timeframe

if TYPE_CHECKING:
    from collections.abc import Iterable, Iterator
    from datetime import datetime

    from atp_core.clock import TradingCalendar

#: Timeframes whose expected-bar grid is known rather than guessed.
#:
#: `H1` and `H4` are deliberately absent. A regular session is 390 minutes, so
#: neither divides it: whether Alpaca emits a 30-minute stub at the open or at
#: the close of each session is vendor behaviour nobody here has checked against
#: real data. Guessing wrong misaligns the whole grid and reports every session
#: as a gap, which is worse than refusing — see `require_supported`.
SUPPORTED_TIMEFRAMES = frozenset(
    {Timeframe.M1, Timeframe.M5, Timeframe.M15, Timeframe.M30, Timeframe.D1}
)


def require_supported(timeframe: Timeframe) -> None:
    """Refuse a timeframe whose bar grid we cannot state exactly."""
    if timeframe not in SUPPORTED_TIMEFRAMES:
        supported = ", ".join(
            t.value for t in sorted(SUPPORTED_TIMEFRAMES, key=lambda t: t.seconds)
        )
        raise ValueError(
            f"gap detection does not support {timeframe.value} bars: a {timeframe.value} "
            f"grid does not divide a 390-minute session, and the vendor's alignment for "
            f"the remainder is unverified. Supported: {supported}"
        )


@dataclass(frozen=True, slots=True)
class GapScan:
    """The result of comparing expected bars against stored ones.

    The counts are not decoration. `matched == 0` while `unmatched > 0` means
    bars exist in the range and not one of them landed in a session — which is
    what a timestamp-convention mismatch looks like, and it would otherwise
    present as "every session is missing".
    """

    #: Half-open `[start, end)` windows covering runs of consecutive missing
    #: bars. Each is directly re-fetchable: hand one to the backfiller as-is.
    windows: tuple[tuple[datetime, datetime], ...]
    expected: int
    missing: int
    #: Stored timestamps that fell inside an expected window.
    matched: int
    #: Stored timestamps before the last expected bar that fell inside none of
    #: them — extended-hours bars for an intraday scan, ordinarily, and a
    #: misaligned convention if nothing matched at all. Anything after the last
    #: expected bar is not counted: readers deliberately fetch past the end of
    #: the range (see `query_bounds`), and counting that overhang would make
    #: this fire on every clean scan.
    unmatched: int


def expected_windows(
    calendar: TradingCalendar, timeframe: Timeframe, start: datetime, end: datetime
) -> Iterator[tuple[datetime, datetime]]:
    """The window of every bar the exchange's schedule says should exist.

    Yields half-open `[start, end)` windows, chronological and non-overlapping.
    A window is both where a stored bar has to fall to count as that bar, and
    what is reported (and re-fetched) if none does.

    Only bars the requested range covers in full are yielded, so a range ending
    mid-session does not report that session's unfinished bars as missing.
    """
    require_supported(timeframe)
    if start >= end:
        raise ValueError(f"start must be before end, got start={start} end={end}")

    # A day either side of the requested range in exchange-local terms: the UTC
    # range can straddle a local date at both ends, and the filters below drop
    # anything that does not actually fit.
    first_day = (start.astimezone(calendar.tz) - timedelta(days=1)).date()
    last_day = (end.astimezone(calendar.tz) + timedelta(days=1)).date()

    if timeframe is Timeframe.D1:
        for session in calendar.sessions(first_day, last_day):
            day_start, day_end = calendar.day_bounds(session.day)
            # Keyed on the session's close, not the day's end: the daily bar
            # exists once the session has finished, and requiring the whole
            # local day to be inside the range would skip the most recent
            # session on every "backfill up to now" check.
            if day_start >= start and session.close_at <= end:
                yield day_start, day_end
        return

    step = timedelta(seconds=timeframe.seconds)
    for session in calendar.sessions(first_day, last_day):
        slot = session.open_at
        while slot + step <= session.close_at:
            if slot >= start and slot + step <= end:
                yield slot, slot + step
            slot += step


def scan_gaps(
    expected: Iterable[tuple[datetime, datetime]], present: Iterable[datetime]
) -> GapScan:
    """Merge expected windows against stored timestamps.

    Both sequences must be ascending — `expected_windows` yields them that way
    and `BarRepository.get_bars` orders by timestamp — which is what lets this
    stream: a five-year minute scan is two million windows and neither side is
    ever held in memory.

    Consecutive missing bars are coalesced into one window. For intraday bars
    that window spans the overnight closure when the bars either side of it are
    both missing; it is still exactly the range to re-fetch, which is what it is
    for.
    """
    windows: list[tuple[datetime, datetime]] = []
    expected_count = missing_count = matched = unmatched = 0
    run: tuple[datetime, datetime] | None = None

    stored = iter(present)
    ts = next(stored, None)

    for window_start, window_end in expected:
        expected_count += 1

        while ts is not None and ts < window_start:
            unmatched += 1
            ts = next(stored, None)

        covered = ts is not None and ts < window_end
        while ts is not None and ts < window_end:
            matched += 1
            ts = next(stored, None)

        if covered:
            if run is not None:
                windows.append(run)
                run = None
        else:
            missing_count += 1
            run = (window_start, window_end) if run is None else (run[0], window_end)

    if run is not None:
        windows.append(run)

    return GapScan(
        windows=tuple(windows),
        expected=expected_count,
        missing=missing_count,
        matched=matched,
        unmatched=unmatched,
    )


def query_bounds(timeframe: Timeframe, start: datetime, end: datetime) -> tuple[datetime, datetime]:
    """The range of stored timestamps a scan of `[start, end)` has to read.

    Wider than the requested range for daily bars: the last session's local day
    runs past the session close, and a bar stamped inside it still belongs to
    that session. Fetching one extra day is cheaper than missing the bar and
    reporting a session that is actually there.
    """
    if timeframe is Timeframe.D1:
        return start.astimezone(UTC), (end + timedelta(days=1)).astimezone(UTC)
    return start.astimezone(UTC), end.astimezone(UTC)
