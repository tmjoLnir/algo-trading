"""Calendar-aware gap detection.

Pure: the calendar is real (the exchange rules ship as data) and the "stored"
timestamps are a list, so every case that matters — a holiday, a half-day, a
weekend, an unfinished session, a symbol that had not listed yet — is a unit
test rather than something only a populated database can show.

The dates are chosen for what they are, not for convenience. 2024-01-01 is New
Year's Day, 2024-01-06/07 a weekend, 2024-07-03 a 13:00 early close, 2024-07-04
Independence Day.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from itertools import pairwise

import pytest

from atp_core.clock import TradingCalendar
from atp_core.data.gaps import (
    SUPPORTED_TIMEFRAMES,
    expected_windows,
    query_bounds,
    require_supported,
    scan_gaps,
)
from atp_core.domain import Timeframe


@pytest.fixture(scope="module")
def nyse() -> TradingCalendar:
    return TradingCalendar()


def utc(
    year: int, month: int, day: int, hour: int = 0, minute: int = 0, second: int = 0
) -> datetime:
    return datetime(year, month, day, hour, minute, second, tzinfo=UTC)


def daily_bar_ts(year: int, month: int, day: int, *, hour: int = 5) -> datetime:
    """A daily bar's timestamp as Alpaca stamps it: 00:00 New York.

    `hour=5` is winter (EST); summer sessions are stamped at 04:00Z. Getting
    this wrong by five hours is the whole reason `day_bounds` exists.
    """
    return utc(year, month, day, hour)


class TestDailyWindows:
    def test_one_window_per_session(self, nyse: TradingCalendar) -> None:
        windows = list(expected_windows(nyse, Timeframe.D1, utc(2024, 1, 1), utc(2024, 1, 13)))

        assert [w[0].date().day for w in windows] == [2, 3, 4, 5, 8, 9, 10, 11, 12]

    def test_new_years_day_is_not_expected(self, nyse: TradingCalendar) -> None:
        """The holiday the range starts on. Without the calendar this is the
        first false gap of the year."""
        windows = list(expected_windows(nyse, Timeframe.D1, utc(2024, 1, 1), utc(2024, 1, 4)))

        assert windows[0][0] == daily_bar_ts(2024, 1, 2)

    def test_a_weekend_is_not_expected(self, nyse: TradingCalendar) -> None:
        windows = list(expected_windows(nyse, Timeframe.D1, utc(2024, 1, 5), utc(2024, 1, 9)))

        assert [w[0] for w in windows] == [daily_bar_ts(2024, 1, 5), daily_bar_ts(2024, 1, 8)]

    def test_a_window_is_the_exchange_local_day(self, nyse: TradingCalendar) -> None:
        """Not the session: a daily bar is stamped at local midnight, so the
        window a stored one has to fall in — and the range to re-fetch if it is
        missing — is the whole local day."""
        windows = list(expected_windows(nyse, Timeframe.D1, utc(2024, 1, 2), utc(2024, 1, 4)))

        assert windows[0] == (daily_bar_ts(2024, 1, 2), daily_bar_ts(2024, 1, 3))

    def test_summer_windows_shift_with_the_offset(self, nyse: TradingCalendar) -> None:
        windows = list(expected_windows(nyse, Timeframe.D1, utc(2024, 7, 8), utc(2024, 7, 10)))

        assert windows[0] == (utc(2024, 7, 8, 4), utc(2024, 7, 9, 4))

    def test_an_unfinished_session_is_not_expected(self, nyse: TradingCalendar) -> None:
        """The 'check up to now' case. At 18:00 UTC the market is still open;
        the daily bar does not exist yet and reporting it as a hole would make
        every nightly run cry wolf."""
        windows = list(expected_windows(nyse, Timeframe.D1, utc(2024, 1, 2), utc(2024, 1, 3, 18)))

        assert [w[0] for w in windows] == [daily_bar_ts(2024, 1, 2)]

    def test_a_session_is_expected_once_it_has_closed(self, nyse: TradingCalendar) -> None:
        windows = list(expected_windows(nyse, Timeframe.D1, utc(2024, 1, 2), utc(2024, 1, 3, 21)))

        assert [w[0] for w in windows] == [daily_bar_ts(2024, 1, 2), daily_bar_ts(2024, 1, 3)]

    def test_a_session_starting_before_the_range_is_not_expected(
        self, nyse: TradingCalendar
    ) -> None:
        """A range opening mid-morning does not cover the whole local day whose
        bar it would report, so it does not claim to have checked it."""
        windows = list(expected_windows(nyse, Timeframe.D1, utc(2024, 1, 3, 12), utc(2024, 1, 5)))

        assert [w[0] for w in windows] == [daily_bar_ts(2024, 1, 4)]

    def test_an_early_close_still_expects_its_daily_bar(self, nyse: TradingCalendar) -> None:
        """3 July closes at 13:00 and 4 July is shut: a half-day is a session
        with a daily bar, a holiday is neither."""
        windows = list(expected_windows(nyse, Timeframe.D1, utc(2024, 7, 3), utc(2024, 7, 6)))

        assert [w[0] for w in windows] == [utc(2024, 7, 3, 4), utc(2024, 7, 5, 4)]

    def test_windows_are_chronological_and_do_not_overlap(self, nyse: TradingCalendar) -> None:
        windows = list(expected_windows(nyse, Timeframe.D1, utc(2024, 1, 1), utc(2024, 3, 1)))

        for (_, prev_end), (next_start, _) in pairwise(windows):
            assert prev_end <= next_start


class TestIntradayWindows:
    def test_a_regular_session_is_13_half_hours(self, nyse: TradingCalendar) -> None:
        windows = list(expected_windows(nyse, Timeframe.M30, utc(2024, 1, 3), utc(2024, 1, 4)))

        assert len(windows) == 13
        assert windows[0] == (utc(2024, 1, 3, 14, 30), utc(2024, 1, 3, 15, 0))
        assert windows[-1] == (utc(2024, 1, 3, 20, 30), utc(2024, 1, 3, 21, 0))

    def test_a_regular_session_is_390_minutes(self, nyse: TradingCalendar) -> None:
        windows = list(expected_windows(nyse, Timeframe.M1, utc(2024, 1, 3), utc(2024, 1, 4)))

        assert len(windows) == 390

    def test_an_early_close_expects_fewer_bars(self, nyse: TradingCalendar) -> None:
        """3 July closes at 13:00 New York. Expecting a full session here is
        3 hours of false gaps, three times a year, on every symbol."""
        windows = list(expected_windows(nyse, Timeframe.M30, utc(2024, 7, 3), utc(2024, 7, 4)))

        assert len(windows) == 7
        assert windows[-1][1] == utc(2024, 7, 3, 17, 0)

    def test_no_bars_are_expected_overnight(self, nyse: TradingCalendar) -> None:
        windows = list(expected_windows(nyse, Timeframe.M30, utc(2024, 1, 3), utc(2024, 1, 5)))

        assert len(windows) == 26, "two sessions, nothing between them"

    def test_no_bars_are_expected_on_a_holiday(self, nyse: TradingCalendar) -> None:
        assert list(expected_windows(nyse, Timeframe.M5, utc(2024, 7, 4), utc(2024, 7, 5))) == []

    def test_a_partially_covered_bar_is_not_expected(self, nyse: TradingCalendar) -> None:
        """The range ends 15 minutes into a half-hour bar. That bar is neither
        complete nor missing, and calling it a gap would report one on every
        run that stops mid-bar."""
        windows = list(
            expected_windows(nyse, Timeframe.M30, utc(2024, 1, 3), utc(2024, 1, 3, 15, 45))
        )

        assert windows[-1][1] == utc(2024, 1, 3, 15, 30)


class TestUnsupportedTimeframes:
    """`1h` and `4h` do not divide a 390-minute session, and where the vendor
    puts the remainder is unverified. A misaligned grid reports every session as
    a gap, which is worse than refusing."""

    @pytest.mark.parametrize("timeframe", [Timeframe.H1, Timeframe.H4])
    def test_hourly_grids_are_refused(self, timeframe: Timeframe) -> None:
        with pytest.raises(ValueError, match="does not support"):
            require_supported(timeframe)

    def test_expected_windows_refuses_before_doing_any_work(self, nyse: TradingCalendar) -> None:
        with pytest.raises(ValueError, match="does not support"):
            list(expected_windows(nyse, Timeframe.H1, utc(2024, 1, 1), utc(2024, 1, 5)))

    def test_every_supported_timeframe_divides_a_regular_session(
        self, nyse: TradingCalendar
    ) -> None:
        """The property that makes the grid statable at all."""
        for timeframe in SUPPORTED_TIMEFRAMES - {Timeframe.D1}:
            assert (390 * 60) % timeframe.seconds == 0, timeframe

    def test_an_inverted_range_is_rejected(self, nyse: TradingCalendar) -> None:
        with pytest.raises(ValueError, match="start must be before end"):
            list(expected_windows(nyse, Timeframe.D1, utc(2024, 1, 5), utc(2024, 1, 1)))


#: Five abutting one-hour windows. Hand-built rather than calendar-derived, so
#: the merge's arithmetic is visible in the assertions.
WINDOWS = [(utc(2024, 1, 1, h), utc(2024, 1, 1, h + 1)) for h in range(5)]


class TestScan:
    """The merge itself."""

    def test_complete_coverage_reports_nothing(self) -> None:
        present = [w[0] for w in WINDOWS]

        scan = scan_gaps(WINDOWS, present)

        assert scan.windows == ()
        assert (scan.expected, scan.missing, scan.matched, scan.unmatched) == (5, 0, 5, 0)

    def test_one_missing_bar_is_one_window(self) -> None:
        present = [w[0] for w in WINDOWS if w[0] != utc(2024, 1, 1, 2)]

        scan = scan_gaps(WINDOWS, present)

        assert scan.windows == ((utc(2024, 1, 1, 2), utc(2024, 1, 1, 3)),)
        assert scan.missing == 1

    def test_consecutive_missing_bars_coalesce(self) -> None:
        """One outage is one window. Reporting it bar by bar is how a gap alert
        becomes a wall of text nobody reads."""
        present = [utc(2024, 1, 1, 0), utc(2024, 1, 1, 4)]

        scan = scan_gaps(WINDOWS, present)

        assert scan.windows == ((utc(2024, 1, 1, 1), utc(2024, 1, 1, 4)),)
        assert scan.missing == 3

    def test_separate_outages_stay_separate(self) -> None:
        present = [utc(2024, 1, 1, 0), utc(2024, 1, 1, 2), utc(2024, 1, 1, 4)]

        scan = scan_gaps(WINDOWS, present)

        assert scan.windows == (
            (utc(2024, 1, 1, 1), utc(2024, 1, 1, 2)),
            (utc(2024, 1, 1, 3), utc(2024, 1, 1, 4)),
        )

    def test_an_empty_store_is_one_window_over_everything(self) -> None:
        scan = scan_gaps(WINDOWS, [])

        assert scan.windows == ((utc(2024, 1, 1, 0), utc(2024, 1, 1, 5)),)
        assert scan.missing == 5

    def test_a_bar_anywhere_in_its_window_counts(self) -> None:
        """Matching is by window, not by exact timestamp: a daily bar is
        stamped at local midnight and an intraday one at its open, and both
        have to land in the slot they belong to."""
        present = [w[0] + timedelta(minutes=37) for w in WINDOWS]

        assert scan_gaps(WINDOWS, present).windows == ()

    def test_bars_outside_every_window_are_unmatched_not_gaps(self) -> None:
        """Extended-hours bars, for an intraday scan. They are extra data, not
        evidence of a hole."""
        present = [utc(2023, 12, 31, 23), *[w[0] for w in WINDOWS]]

        scan = scan_gaps(WINDOWS, present)

        assert scan.windows == ()
        assert (scan.matched, scan.unmatched) == (5, 1)

    def test_bars_past_the_last_window_are_not_counted(self) -> None:
        """A daily scan deliberately reads a day past its range (`query_bounds`),
        so the overhang is an artefact of the read rather than a bar belonging
        to no session. Counting it would make the mismatch warning in
        `find_gaps` fire on every clean scan."""
        present = [*[w[0] for w in WINDOWS], utc(2024, 1, 1, 9)]

        assert scan_gaps(WINDOWS, present).unmatched == 0

    def test_duplicate_timestamps_in_one_window_match_once_each(self) -> None:
        present = [w[0] for w in WINDOWS] + [utc(2024, 1, 1, 0)]

        scan = scan_gaps(WINDOWS, sorted(present))

        assert scan.windows == ()
        assert scan.matched == 6

    def test_no_expected_windows_is_not_a_gap(self) -> None:
        """A range containing no sessions at all — a long weekend."""
        scan = scan_gaps([], [utc(2024, 1, 1)])

        assert scan.windows == ()
        assert scan.expected == 0


class TestEndToEnd:
    """The scan as `find_gaps` runs it: real calendar, Alpaca-shaped timestamps."""

    def test_a_clean_week_has_no_gaps(self, nyse: TradingCalendar) -> None:
        present = [daily_bar_ts(2024, 1, d) for d in (2, 3, 4, 5, 8, 9, 10, 11, 12)]

        scan = scan_gaps(
            expected_windows(nyse, Timeframe.D1, utc(2024, 1, 1), utc(2024, 1, 13)), present
        )

        assert scan.windows == ()
        assert scan.expected == 9

    def test_a_missing_session_is_reported_as_its_local_day(self, nyse: TradingCalendar) -> None:
        present = [daily_bar_ts(2024, 1, d) for d in (2, 3, 5, 8)]

        scan = scan_gaps(
            expected_windows(nyse, Timeframe.D1, utc(2024, 1, 1), utc(2024, 1, 9)), present
        )

        assert scan.windows == ((daily_bar_ts(2024, 1, 4), daily_bar_ts(2024, 1, 5)),)

    def test_a_symbol_listed_mid_range_reports_only_the_sessions_before_it(
        self, nyse: TradingCalendar
    ) -> None:
        """An IPO, and the reason a gap report is a prompt for a human rather
        than an error: the hole is real and entirely expected."""
        present = [daily_bar_ts(2024, 1, d) for d in (10, 11, 12)]

        scan = scan_gaps(
            expected_windows(nyse, Timeframe.D1, utc(2024, 1, 1), utc(2024, 1, 13)), present
        )

        assert scan.windows == ((daily_bar_ts(2024, 1, 2), daily_bar_ts(2024, 1, 10)),)

    def test_utc_midnight_stamps_do_not_masquerade_as_coverage(self, nyse: TradingCalendar) -> None:
        """The convention this rests on, pinned.

        A provider stamping daily bars at 00:00 UTC rather than 00:00 New York
        attributes each bar to the *previous* local day. The scan does not
        quietly accept it: bars fall outside the sessions they were meant for,
        and the counts say so. Normalise such a feed in its adapter.
        """
        present = [utc(2024, 1, d) for d in (2, 3, 4, 5)]

        scan = scan_gaps(
            expected_windows(nyse, Timeframe.D1, utc(2024, 1, 1), utc(2024, 1, 6)), present
        )

        assert scan.windows, "a five-hour shift must not read as clean coverage"
        assert scan.unmatched == 1, "the first bar lands before any session"


class TestQueryBounds:
    def test_daily_reads_a_day_past_the_range(self) -> None:
        """The last session's local day runs past its close, and a bar stamped
        inside it belongs to that session. Reading one extra day is cheaper than
        reporting a session that is actually there."""
        assert query_bounds(Timeframe.D1, utc(2024, 1, 1), utc(2024, 1, 5)) == (
            utc(2024, 1, 1),
            utc(2024, 1, 6),
        )

    def test_intraday_reads_exactly_the_range(self) -> None:
        assert query_bounds(Timeframe.M5, utc(2024, 1, 1), utc(2024, 1, 5)) == (
            utc(2024, 1, 1),
            utc(2024, 1, 5),
        )
