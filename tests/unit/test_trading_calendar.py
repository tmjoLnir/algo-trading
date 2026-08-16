"""The trading calendar.

Every date here is a real NYSE session, holiday or half-day, checked against the
published schedule rather than against what the code happens to return. That is
the whole point of the class: the failure it exists to prevent is a plausible
approximation — "weekdays 9:30-16:00" — that is wrong about ten days a year and
wrong again for every one of the ~3 early closes.

Nothing here touches a network. `pandas_market_calendars` ships the exchange
rules as data.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest

from atp_core.clock import TradingCalendar
from atp_core.errors import ConfigError


@pytest.fixture(scope="module")
def nyse() -> TradingCalendar:
    """Module-scoped: the first question about a year materialises it."""
    return TradingCalendar()


class TestTradingDays:
    @pytest.mark.parametrize(
        ("day", "why"),
        [
            (date(2024, 1, 1), "New Year's Day"),
            (date(2024, 3, 29), "Good Friday — a holiday no weekday rule catches"),
            (date(2024, 6, 19), "Juneteenth — a holiday only since 2022"),
            (date(2024, 7, 4), "Independence Day"),
            (date(2024, 11, 28), "Thanksgiving"),
            (date(2024, 12, 25), "Christmas"),
            (date(2018, 12, 5), "closed for George H. W. Bush's funeral"),
        ],
    )
    def test_holidays_are_not_trading_days(
        self, nyse: TradingCalendar, day: date, why: str
    ) -> None:
        assert not nyse.is_trading_day(day), why

    @pytest.mark.parametrize("day", [date(2024, 1, 6), date(2024, 1, 7)])
    def test_weekends_are_not_trading_days(self, nyse: TradingCalendar, day: date) -> None:
        assert not nyse.is_trading_day(day)

    def test_an_ordinary_weekday_is_a_trading_day(self, nyse: TradingCalendar) -> None:
        assert nyse.is_trading_day(date(2024, 1, 3))

    def test_a_half_day_is_still_a_trading_day(self, nyse: TradingCalendar) -> None:
        """Early closes are sessions, not holidays. Treating 3 July as shut
        would skip a day of real bars."""
        assert nyse.is_trading_day(date(2024, 7, 3))

    def test_juneteenth_was_a_session_before_it_was_a_holiday(self, nyse: TradingCalendar) -> None:
        """Holidays are not constant through history, which is exactly why this
        is data and not a hand-written rule."""
        assert nyse.is_trading_day(date(2021, 6, 18))


class TestSessionBounds:
    def test_winter_session_is_1430_to_2100_utc(self, nyse: TradingCalendar) -> None:
        assert nyse.session_bounds(date(2024, 1, 3)) == (
            datetime(2024, 1, 3, 14, 30, tzinfo=UTC),
            datetime(2024, 1, 3, 21, 0, tzinfo=UTC),
        )

    def test_summer_session_is_an_hour_earlier_in_utc(self, nyse: TradingCalendar) -> None:
        """The session does not move; New York's offset does. Anything that
        hard-codes a UTC open is wrong for half the year."""
        assert nyse.session_bounds(date(2024, 7, 8)) == (
            datetime(2024, 7, 8, 13, 30, tzinfo=UTC),
            datetime(2024, 7, 8, 20, 0, tzinfo=UTC),
        )

    def test_a_closed_day_has_no_bounds(self, nyse: TradingCalendar) -> None:
        assert nyse.session_bounds(date(2024, 12, 25)) is None

    def test_an_early_close_ends_at_1300_local(self, nyse: TradingCalendar) -> None:
        session = nyse.session_on(date(2024, 7, 3))

        assert session is not None
        assert session.close_at == datetime(2024, 7, 3, 17, 0, tzinfo=UTC)
        assert session.is_early_close

    def test_a_regular_session_is_not_flagged_as_early(self, nyse: TradingCalendar) -> None:
        session = nyse.session_on(date(2024, 7, 8))

        assert session is not None
        assert not session.is_early_close


class TestSessions:
    def test_a_week_excludes_its_weekend(self, nyse: TradingCalendar) -> None:
        got = nyse.sessions(date(2024, 1, 1), date(2024, 1, 7))

        # 1 January is a holiday, 6-7 January a weekend.
        assert [s.day.day for s in got] == [2, 3, 4, 5]

    def test_the_range_is_inclusive_at_both_ends(self, nyse: TradingCalendar) -> None:
        got = nyse.sessions(date(2024, 1, 3), date(2024, 1, 4))

        assert [s.day for s in got] == [date(2024, 1, 3), date(2024, 1, 4)]

    def test_sessions_are_chronological_across_a_year_boundary(self, nyse: TradingCalendar) -> None:
        got = nyse.sessions(date(2023, 12, 28), date(2024, 1, 4))

        assert [s.day for s in got] == sorted(s.day for s in got)
        assert got[0].day == date(2023, 12, 28)
        assert got[-1].day == date(2024, 1, 4)

    def test_a_year_has_roughly_252_sessions(self, nyse: TradingCalendar) -> None:
        """A weekday-only count would be 262. The difference is the holidays."""
        assert len(nyse.sessions(date(2024, 1, 1), date(2024, 12, 31))) == 252

    def test_a_closed_range_is_empty(self, nyse: TradingCalendar) -> None:
        assert nyse.sessions(date(2024, 12, 25), date(2024, 12, 25)) == []

    def test_an_inverted_range_is_rejected(self, nyse: TradingCalendar) -> None:
        with pytest.raises(ValueError, match="on or before"):
            nyse.sessions(date(2024, 1, 5), date(2024, 1, 1))


class TestDayBounds:
    """What a daily bar's timestamp is anchored to: Alpaca stamps one at 00:00
    New York, so the local day is how a stored daily bar is matched to its
    session (docs/DATA.md)."""

    def test_winter_day_starts_at_0500_utc(self, nyse: TradingCalendar) -> None:
        assert nyse.day_bounds(date(2024, 1, 3)) == (
            datetime(2024, 1, 3, 5, 0, tzinfo=UTC),
            datetime(2024, 1, 4, 5, 0, tzinfo=UTC),
        )

    def test_summer_day_starts_at_0400_utc(self, nyse: TradingCalendar) -> None:
        assert nyse.day_bounds(date(2024, 7, 3)) == (
            datetime(2024, 7, 3, 4, 0, tzinfo=UTC),
            datetime(2024, 7, 4, 4, 0, tzinfo=UTC),
        )

    def test_a_spring_forward_day_is_23_hours(self, nyse: TradingCalendar) -> None:
        """Local midnights, not a fixed 24 hours. A day built by adding 24h
        would drift past the next midnight on every DST change."""
        day_start, day_end = nyse.day_bounds(date(2024, 3, 10))

        assert day_end - day_start == timedelta(hours=23)

    def test_an_autumn_back_day_is_25_hours(self, nyse: TradingCalendar) -> None:
        day_start, day_end = nyse.day_bounds(date(2024, 11, 3))

        assert day_end - day_start == timedelta(hours=25)


class TestIsOpen:
    def test_open_at_the_opening_instant(self, nyse: TradingCalendar) -> None:
        assert nyse.is_open(datetime(2024, 1, 3, 14, 30, tzinfo=UTC))

    def test_shut_a_microsecond_before_the_open(self, nyse: TradingCalendar) -> None:
        assert not nyse.is_open(datetime(2024, 1, 3, 14, 29, 59, 999_999, tzinfo=UTC))

    def test_shut_at_the_closing_instant(self, nyse: TradingCalendar) -> None:
        """Half-open. An order stamped 16:00:00 is rejected by the venue, so
        reporting the market as open then would be a lie that costs money."""
        assert not nyse.is_open(datetime(2024, 1, 3, 21, 0, tzinfo=UTC))

    def test_open_mid_session(self, nyse: TradingCalendar) -> None:
        assert nyse.is_open(datetime(2024, 1, 3, 17, 0, tzinfo=UTC))

    def test_shut_on_a_holiday(self, nyse: TradingCalendar) -> None:
        assert not nyse.is_open(datetime(2024, 12, 25, 17, 0, tzinfo=UTC))

    def test_shut_after_an_early_close(self, nyse: TradingCalendar) -> None:
        """15:00 New York on 3 July. A fixed 16:00 rule says open; the market
        shut two hours earlier."""
        assert not nyse.is_open(datetime(2024, 7, 3, 19, 0, tzinfo=UTC))

    def test_a_non_utc_instant_is_converted_not_refused(self, nyse: TradingCalendar) -> None:
        from zoneinfo import ZoneInfo

        ny_open = datetime(2024, 1, 3, 9, 30, tzinfo=ZoneInfo("America/New_York"))

        assert nyse.is_open(ny_open)

    def test_naive_input_is_rejected(self, nyse: TradingCalendar) -> None:
        with pytest.raises(ValueError, match="timezone-aware"):
            nyse.is_open(datetime(2024, 1, 3, 15, 0))  # noqa: DTZ001 — the input under test


class TestNextOpen:
    def test_from_a_friday_evening_skips_the_weekend(self, nyse: TradingCalendar) -> None:
        assert nyse.next_open(datetime(2024, 1, 5, 22, 0, tzinfo=UTC)) == datetime(
            2024, 1, 8, 14, 30, tzinfo=UTC
        )

    def test_skips_a_holiday(self, nyse: TradingCalendar) -> None:
        """From Christmas Eve's close. 25 December is shut, so the next open is
        the 26th — a naive 'tomorrow' would have the runner wake into a closed
        market."""
        assert nyse.next_open(datetime(2024, 12, 24, 20, 0, tzinfo=UTC)) == datetime(
            2024, 12, 26, 14, 30, tzinfo=UTC
        )

    def test_from_mid_session_returns_the_next_session(self, nyse: TradingCalendar) -> None:
        assert nyse.next_open(datetime(2024, 1, 3, 17, 0, tzinfo=UTC)) == datetime(
            2024, 1, 4, 14, 30, tzinfo=UTC
        )

    def test_at_the_opening_instant_returns_the_following_open(self, nyse: TradingCalendar) -> None:
        """Strictly after, so that `while shut: sleep_until(next_open())` cannot
        busy-wait at the boundary."""
        assert nyse.next_open(datetime(2024, 1, 3, 14, 30, tzinfo=UTC)) == datetime(
            2024, 1, 4, 14, 30, tzinfo=UTC
        )

    def test_before_an_open_returns_that_open(self, nyse: TradingCalendar) -> None:
        assert nyse.next_open(datetime(2024, 1, 3, 5, 0, tzinfo=UTC)) == datetime(
            2024, 1, 3, 14, 30, tzinfo=UTC
        )

    def test_naive_input_is_rejected(self, nyse: TradingCalendar) -> None:
        with pytest.raises(ValueError, match="timezone-aware"):
            nyse.next_open(datetime(2024, 1, 3, 15, 0))  # noqa: DTZ001 — the input under test


class TestMinutesToClose:
    def test_counts_down_within_the_session(self, nyse: TradingCalendar) -> None:
        assert nyse.minutes_to_close(datetime(2024, 1, 3, 20, 30, tzinfo=UTC)) == 30

    def test_rounds_down(self, nyse: TradingCalendar) -> None:
        """A late exit is the expensive direction: 90 seconds left is one
        minute, not two."""
        assert nyse.minutes_to_close(datetime(2024, 1, 3, 20, 58, 30, tzinfo=UTC)) == 1

    def test_uses_the_early_close_on_a_half_day(self, nyse: TradingCalendar) -> None:
        """16:45 UTC on 3 July is 15 minutes from a 13:00 New York close, not
        3h15 from a 16:00 one — the difference between flattening in time and
        holding overnight."""
        assert nyse.minutes_to_close(datetime(2024, 7, 3, 16, 45, tzinfo=UTC)) == 15

    def test_none_outside_the_session(self, nyse: TradingCalendar) -> None:
        assert nyse.minutes_to_close(datetime(2024, 1, 3, 22, 0, tzinfo=UTC)) is None

    def test_none_on_a_holiday(self, nyse: TradingCalendar) -> None:
        assert nyse.minutes_to_close(datetime(2024, 12, 25, 17, 0, tzinfo=UTC)) is None

    def test_zero_at_the_final_minute(self, nyse: TradingCalendar) -> None:
        assert nyse.minutes_to_close(datetime(2024, 1, 3, 20, 59, 30, tzinfo=UTC)) == 0

    def test_naive_input_is_rejected(self, nyse: TradingCalendar) -> None:
        with pytest.raises(ValueError, match="timezone-aware"):
            nyse.minutes_to_close(datetime(2024, 1, 3, 15, 0))  # noqa: DTZ001 — under test


class TestConstruction:
    def test_an_unknown_exchange_fails_at_construction(self) -> None:
        """Where the typo was made, not on the first query three layers down."""
        with pytest.raises(ConfigError, match="unknown exchange"):
            TradingCalendar("NYSEE")

    def test_a_non_us_exchange_uses_its_own_time_zone(self) -> None:
        """`MARKET_TZ` is the default venue's, not every venue's. Reading London
        in New York would shift every session by five hours."""
        lse = TradingCalendar("LSE")

        assert str(lse.tz) == "Europe/London"

    def test_beyond_the_holiday_rules_it_refuses_rather_than_guesses(
        self, nyse: TradingCalendar
    ) -> None:
        """Past the last defined holiday the library still drops weekends but
        stops dropping holidays, so 1 January 2201 comes back as an ordinary
        session. A calendar confidently wrong about a closure is worse than one
        that says it does not know."""
        with pytest.raises(ValueError, match="holiday rules only cover"):
            nyse.is_trading_day(date(2201, 1, 1))
