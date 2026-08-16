"""Bar storage against a real TimescaleDB.

These cannot be unit tests. Everything worth checking here is behaviour of the
database rather than of Python: whether `ON CONFLICT` actually merges instead of
duplicating, whether a NUMERIC column hands back the Decimal that went in,
whether COALESCE preserves a column a later write left null.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

import pytest

from atp_core.domain import Bar, Timeframe
from atp_core.persistence.bars import PostgresBarRepository
from atp_core.persistence.db import create_engine, create_session_factory

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

pytestmark = pytest.mark.integration

T0 = datetime(2024, 1, 2, tzinfo=UTC)


def make_bar(
    day: int,
    *,
    symbol: str = "SPY",
    timeframe: Timeframe = Timeframe.D1,
    close: str = "101.5",
    adj_close: str | None = None,
    vwap: str | None = None,
    trade_count: int | None = None,
) -> Bar:
    """High and low bracket open and close so any close stays valid — the
    fixture should not have to think about the OHLC invariant."""
    open_ = Decimal("100")
    close_d = Decimal(close)
    return Bar(
        symbol=symbol,
        ts=T0 + timedelta(days=day),
        timeframe=timeframe,
        open=open_,
        high=max(open_, close_d),
        low=min(open_, close_d),
        close=close_d,
        volume=Decimal("1000"),
        adj_close=Decimal(adj_close) if adj_close is not None else None,
        vwap=Decimal(vwap) if vwap is not None else None,
        trade_count=trade_count,
    )


@pytest.fixture
async def repo(clean_bars: str) -> AsyncIterator[PostgresBarRepository]:
    engine = create_engine(clean_bars)
    try:
        yield PostgresBarRepository(create_session_factory(engine))
    finally:
        await engine.dispose()


async def window(repo: PostgresBarRepository, days: int = 365) -> list[Bar]:
    return await repo.get_bars("SPY", Timeframe.D1, T0, T0 + timedelta(days=days))


class TestUpsertIdempotency:
    """Backfills overlap constantly (docs/DATA.md). Re-running one must be a
    no-op in effect, not a source of duplicate candles."""

    async def test_inserts_new_bars(self, repo: PostgresBarRepository) -> None:
        written = await repo.upsert_bars([make_bar(0), make_bar(1), make_bar(2)])

        assert written == 3
        assert len(await window(repo)) == 3

    async def test_rerunning_the_same_window_does_not_duplicate(
        self, repo: PostgresBarRepository
    ) -> None:
        bars = [make_bar(0), make_bar(1), make_bar(2)]

        await repo.upsert_bars(bars)
        await repo.upsert_bars(bars)

        assert len(await window(repo)) == 3

    async def test_overlapping_backfills_converge(self, repo: PostgresBarRepository) -> None:
        """The realistic shape: a gap backfill that partly covers what is
        already stored."""
        await repo.upsert_bars([make_bar(0), make_bar(1), make_bar(2)])
        await repo.upsert_bars([make_bar(2), make_bar(3), make_bar(4)])

        assert [b.ts.day for b in await window(repo)] == [2, 3, 4, 5, 6]

    async def test_corrected_prices_overwrite(self, repo: PostgresBarRepository) -> None:
        """A vendor restatement has to be able to land."""
        await repo.upsert_bars([make_bar(0, close="101.5")])
        await repo.upsert_bars([make_bar(0, close="103.25")])

        stored = await window(repo)
        assert len(stored) == 1
        assert stored[0].close == Decimal("103.25")

    async def test_empty_list_is_a_no_op(self, repo: PostgresBarRepository) -> None:
        assert await repo.upsert_bars([]) == 0


class TestOptionalColumnsAreMerged:
    """The subtle one. A raw-only fetch carries no `adj_close`, and a plain
    overwrite would erase what an adjusted pass stored — leaving backtests
    running on NULLs with nothing to indicate why."""

    async def test_raw_only_write_preserves_adj_close(self, repo: PostgresBarRepository) -> None:
        await repo.upsert_bars([make_bar(0, adj_close="50.0")])

        await repo.upsert_bars([make_bar(0, close="102.0")])  # raw pass: adj_close is None

        stored = (await window(repo))[0]
        assert stored.close == Decimal("102.0"), "the raw price still updates"
        assert stored.adj_close == Decimal("50.0"), "the adjusted price must survive"

    async def test_raw_only_write_preserves_vwap_and_trade_count(
        self, repo: PostgresBarRepository
    ) -> None:
        await repo.upsert_bars([make_bar(0, vwap="101.0", trade_count=42)])

        await repo.upsert_bars([make_bar(0, close="102.0")])

        stored = (await window(repo))[0]
        assert stored.vwap == Decimal("101.0")
        assert stored.trade_count == 42

    async def test_a_newer_adjusted_price_still_wins(self, repo: PostgresBarRepository) -> None:
        """A corporate action makes every historical adj_close for the symbol
        stale, so a present incoming value must overwrite."""
        await repo.upsert_bars([make_bar(0, adj_close="50.0")])

        await repo.upsert_bars([make_bar(0, adj_close="45.0")])

        assert (await window(repo))[0].adj_close == Decimal("45.0")


class TestDecimalFidelity:
    """CLAUDE.md §1.1 all the way to the disk and back."""

    async def test_prices_round_trip_exactly(self, repo: PostgresBarRepository) -> None:
        await repo.upsert_bars([make_bar(0, close="0.1")])

        stored = (await window(repo))[0]

        assert isinstance(stored.close, Decimal)
        assert stored.close == Decimal("0.1")

    async def test_precision_beyond_float_survives(self, repo: PostgresBarRepository) -> None:
        """NUMERIC(20, 8) — eight places, which is what fractional shares and
        crypto need. A float would have lost this before it reached the wire."""
        await repo.upsert_bars([make_bar(0, close="123.45678901")])

        assert (await window(repo))[0].close == Decimal("123.45678901")


class TestGetBars:
    async def test_window_is_half_open(self, repo: PostgresBarRepository) -> None:
        """[start, end) — so consecutive windows chained end-to-start cover the
        range exactly once instead of duplicating the seam."""
        await repo.upsert_bars([make_bar(0), make_bar(1), make_bar(2)])

        got = await repo.get_bars("SPY", Timeframe.D1, T0, T0 + timedelta(days=2))

        assert [b.ts.day for b in got] == [2, 3]

    async def test_chained_windows_cover_each_bar_once(self, repo: PostgresBarRepository) -> None:
        await repo.upsert_bars([make_bar(i) for i in range(6)])

        first = await repo.get_bars("SPY", Timeframe.D1, T0, T0 + timedelta(days=3))
        second = await repo.get_bars(
            "SPY", Timeframe.D1, T0 + timedelta(days=3), T0 + timedelta(days=6)
        )

        assert [b.ts for b in first + second] == [make_bar(i).ts for i in range(6)]

    async def test_returns_chronological_order(self, repo: PostgresBarRepository) -> None:
        await repo.upsert_bars([make_bar(2), make_bar(0), make_bar(1)])

        stored = await window(repo)

        assert [b.ts for b in stored] == sorted(b.ts for b in stored)

    async def test_filters_by_symbol(self, repo: PostgresBarRepository) -> None:
        await repo.upsert_bars([make_bar(0), make_bar(0, symbol="QQQ")])

        assert len(await window(repo)) == 1

    async def test_filters_by_timeframe(self, repo: PostgresBarRepository) -> None:
        """The same symbol at two timeframes shares the table; mixing them would
        feed an indicator a series that is not one series."""
        await repo.upsert_bars([make_bar(0), make_bar(0, timeframe=Timeframe.H1)])

        assert len(await window(repo)) == 1

    async def test_empty_range_returns_empty(self, repo: PostgresBarRepository) -> None:
        await repo.upsert_bars([make_bar(0)])

        got = await repo.get_bars(
            "SPY", Timeframe.D1, T0 + timedelta(days=50), T0 + timedelta(days=60)
        )

        assert got == []

    async def test_naive_bounds_are_rejected(self, repo: PostgresBarRepository) -> None:
        with pytest.raises(ValueError, match="timezone-aware"):
            await repo.get_bars(
                "SPY",
                Timeframe.D1,
                datetime(2024, 1, 2),  # noqa: DTZ001 — the naive input under test
                T0,
            )


class TestGetLastNBars:
    async def test_returns_the_most_recent_n_chronologically(
        self, repo: PostgresBarRepository
    ) -> None:
        """Fetched newest-first for the LIMIT, returned oldest-first because an
        indicator over a reversed series produces plausible, wrong numbers."""
        await repo.upsert_bars([make_bar(i) for i in range(5)])

        got = await repo.get_last_n_bars("SPY", Timeframe.D1, 3)

        assert [b.ts.day for b in got] == [4, 5, 6]

    async def test_asking_for_more_than_exists_returns_all(
        self, repo: PostgresBarRepository
    ) -> None:
        await repo.upsert_bars([make_bar(0), make_bar(1)])

        assert len(await repo.get_last_n_bars("SPY", Timeframe.D1, 100)) == 2

    async def test_filters_by_symbol_and_timeframe(self, repo: PostgresBarRepository) -> None:
        await repo.upsert_bars(
            [make_bar(0), make_bar(1, symbol="QQQ"), make_bar(2, timeframe=Timeframe.H1)]
        )

        got = await repo.get_last_n_bars("SPY", Timeframe.D1, 10)

        assert [b.symbol for b in got] == ["SPY"]

    async def test_zero_or_negative_n_is_rejected(self, repo: PostgresBarRepository) -> None:
        with pytest.raises(ValueError, match="at least 1"):
            await repo.get_last_n_bars("SPY", Timeframe.D1, 0)


class TestBulkWrites:
    async def test_writes_more_rows_than_one_statement_can_bind(
        self, repo: PostgresBarRepository
    ) -> None:
        """PostgreSQL caps a statement at 65535 bind parameters. A five-year
        minute backfill is ~500k bars, so a single INSERT is not an option and
        the chunking has to actually work."""
        bars = [make_bar(i) for i in range(4500)]

        written = await repo.upsert_bars(bars)

        assert written == 4500
        assert len(await repo.get_bars("SPY", Timeframe.D1, T0, T0 + timedelta(days=5000))) == 4500


#: The first two weeks of 2024, as stored: 1 January is New Year's Day, 6-7
#: January a weekend. A gap finder without the calendar reports three holes here
#: on a dataset that is complete.
WEEK = [date(2024, 1, d) for d in (2, 3, 4, 5, 8, 9, 10, 11, 12)]
WEEK_START = datetime(2024, 1, 1, tzinfo=UTC)
WEEK_END = datetime(2024, 1, 13, tzinfo=UTC)

#: 3 January 2024, a regular session: 14:30-21:00 UTC, thirteen 30-minute bars.
SESSION_OPEN = datetime(2024, 1, 3, 14, 30, tzinfo=UTC)
SESSION_DAY_START = datetime(2024, 1, 3, tzinfo=UTC)
SESSION_DAY_END = datetime(2024, 1, 4, tzinfo=UTC)


def daily_bar(day: date, *, symbol: str = "SPY") -> Bar:
    """A daily bar stamped where Alpaca stamps one: 00:00 America/New_York.

    Deliberately not `make_bar`'s UTC midnight. Which session a stored daily bar
    belongs to is decided by the exchange-local date its timestamp falls in, so
    the five-hour difference is the difference between a clean dataset and one
    that reports a gap every Friday (docs/DATA.md 'Gaps').
    """
    ts = datetime.combine(day, time(0, 0), tzinfo=ZoneInfo("America/New_York"))
    return Bar(
        symbol=symbol,
        ts=ts.astimezone(UTC),
        timeframe=Timeframe.D1,
        open=Decimal("100"),
        high=Decimal("101"),
        low=Decimal("99"),
        close=Decimal("100.5"),
        volume=Decimal("1000"),
    )


def half_hour_bar(ts: datetime, *, symbol: str = "SPY") -> Bar:
    return Bar(
        symbol=symbol,
        ts=ts,
        timeframe=Timeframe.M30,
        open=Decimal("100"),
        high=Decimal("101"),
        low=Decimal("99"),
        close=Decimal("100.5"),
        volume=Decimal("1000"),
    )


class TestStoredSeries:
    """What the nightly gap sweep iterates over. Needs a database because the
    question is `DISTINCT` over the hypertable's key, not Python."""

    async def test_lists_each_symbol_and_timeframe_once(self, repo: PostgresBarRepository) -> None:
        await repo.upsert_bars(
            [
                make_bar(0),
                make_bar(1),  # same series, second bar
                make_bar(0, symbol="QQQ"),
                half_hour_bar(SESSION_OPEN),
            ]
        )

        assert await repo.stored_series() == [
            ("QQQ", Timeframe.D1),
            ("SPY", Timeframe.D1),
            ("SPY", Timeframe.M30),
        ]

    async def test_an_empty_store_has_no_series(self, repo: PostgresBarRepository) -> None:
        assert await repo.stored_series() == []


class TestFindGaps:
    """Whether what we hold covers every session the exchange was open for.

    The calendar reasoning is unit-tested in `tests/unit/test_gaps.py`; what
    needs a database is the other half — that the right timestamps come back
    out of the hypertable, in order, filtered to one symbol and one timeframe.
    """

    async def test_a_complete_week_has_no_gaps(self, repo: PostgresBarRepository) -> None:
        """The test that matters most: a holiday and a weekend inside the range
        and nothing reported. An alert firing every Saturday is ignored by the
        second week."""
        await repo.upsert_bars([daily_bar(day) for day in WEEK])

        assert await repo.find_gaps("SPY", Timeframe.D1, WEEK_START, WEEK_END) == []

    async def test_a_missing_session_is_reported_as_its_local_day(
        self, repo: PostgresBarRepository
    ) -> None:
        await repo.upsert_bars([daily_bar(day) for day in WEEK if day != date(2024, 1, 4)])

        assert await repo.find_gaps("SPY", Timeframe.D1, WEEK_START, WEEK_END) == [
            (datetime(2024, 1, 4, 5, tzinfo=UTC), datetime(2024, 1, 5, 5, tzinfo=UTC))
        ]

    async def test_consecutive_missing_sessions_are_one_window(
        self, repo: PostgresBarRepository
    ) -> None:
        """One outage is one window — and one range to re-fetch."""
        missing = {date(2024, 1, 4), date(2024, 1, 5), date(2024, 1, 8)}
        await repo.upsert_bars([daily_bar(day) for day in WEEK if day not in missing])

        assert await repo.find_gaps("SPY", Timeframe.D1, WEEK_START, WEEK_END) == [
            (datetime(2024, 1, 4, 5, tzinfo=UTC), datetime(2024, 1, 9, 5, tzinfo=UTC))
        ]

    async def test_an_empty_store_reports_every_session(self, repo: PostgresBarRepository) -> None:
        assert await repo.find_gaps("SPY", Timeframe.D1, WEEK_START, WEEK_END) == [
            (datetime(2024, 1, 2, 5, tzinfo=UTC), datetime(2024, 1, 13, 5, tzinfo=UTC))
        ]

    async def test_another_symbols_bars_do_not_count(self, repo: PostgresBarRepository) -> None:
        await repo.upsert_bars([daily_bar(day, symbol="QQQ") for day in WEEK])

        assert await repo.find_gaps("SPY", Timeframe.D1, WEEK_START, WEEK_END) != []

    async def test_another_timeframes_bars_do_not_count(self, repo: PostgresBarRepository) -> None:
        """The same symbol at two timeframes shares the table. A 30-minute bar
        is not evidence that the daily bar arrived."""
        await repo.upsert_bars([half_hour_bar(SESSION_OPEN)])

        assert await repo.find_gaps("SPY", Timeframe.D1, WEEK_START, WEEK_END) != []

    async def test_a_missing_intraday_bar_is_reported(self, repo: PostgresBarRepository) -> None:
        session = [half_hour_bar(SESSION_OPEN + timedelta(minutes=30 * i)) for i in range(13)]
        await repo.upsert_bars([b for b in session if b.ts != datetime(2024, 1, 3, 17, tzinfo=UTC)])

        assert await repo.find_gaps("SPY", Timeframe.M30, SESSION_DAY_START, SESSION_DAY_END) == [
            (datetime(2024, 1, 3, 17, tzinfo=UTC), datetime(2024, 1, 3, 17, 30, tzinfo=UTC))
        ]

    async def test_a_complete_session_of_intraday_bars_has_no_gaps(
        self, repo: PostgresBarRepository
    ) -> None:
        await repo.upsert_bars(
            [half_hour_bar(SESSION_OPEN + timedelta(minutes=30 * i)) for i in range(13)]
        )

        assert await repo.find_gaps("SPY", Timeframe.M30, SESSION_DAY_START, SESSION_DAY_END) == []

    async def test_an_extended_hours_bar_does_not_cover_a_session_bar(
        self, repo: PostgresBarRepository
    ) -> None:
        """Pre-market bars are stored too. They are extra data, not a stand-in
        for the regular-session bar that is actually missing."""
        session = [half_hour_bar(SESSION_OPEN + timedelta(minutes=30 * i)) for i in range(13)]
        await repo.upsert_bars([half_hour_bar(datetime(2024, 1, 3, 13, tzinfo=UTC)), *session[1:]])

        assert await repo.find_gaps("SPY", Timeframe.M30, SESSION_DAY_START, SESSION_DAY_END) == [
            (SESSION_OPEN, SESSION_OPEN + timedelta(minutes=30))
        ]

    async def test_an_unsupported_timeframe_is_refused(self, repo: PostgresBarRepository) -> None:
        """Hourly bars do not divide a 390-minute session and the vendor's
        alignment for the remainder is unverified. Refusing beats reporting
        every session as a gap."""
        with pytest.raises(ValueError, match="does not support"):
            await repo.find_gaps("SPY", Timeframe.H1, WEEK_START, WEEK_END)

    async def test_naive_bounds_are_rejected(self, repo: PostgresBarRepository) -> None:
        with pytest.raises(ValueError, match="timezone-aware"):
            await repo.find_gaps(
                "SPY",
                Timeframe.D1,
                datetime(2024, 1, 1),  # noqa: DTZ001 — the naive input under test
                WEEK_END,
            )

    async def test_an_inverted_range_is_rejected(self, repo: PostgresBarRepository) -> None:
        with pytest.raises(ValueError, match="start must be before end"):
            await repo.find_gaps("SPY", Timeframe.D1, WEEK_END, WEEK_START)
