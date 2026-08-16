"""The market-data calendar endpoint.

Served straight from the exchange rules — no database, no vendor call — so this
is a unit test that exercises the real handler over ASGI. The clock is pinned
through the dependency, which is the point of having one: a default range
computed from the wall clock would make "what does this return by default" an
untestable question.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

import httpx
import pytest

from atp_api.deps import get_clock
from atp_api.main import create_app
from atp_api.routers.marketdata import MAX_CALENDAR_DAYS
from atp_core.clock import SimulatedClock

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from fastapi import FastAPI

CALENDAR = "/api/v1/market-data/calendar"

#: A Wednesday, 21:00 UTC — after the New York close, and late enough that UTC
#: and New York disagree about nothing. `TestDefaultRange` moves it deliberately.
NOW = datetime(2024, 7, 10, 21, 0, tzinfo=UTC)


@pytest.fixture
def app() -> FastAPI:
    application = create_app()
    application.dependency_overrides[get_clock] = lambda: SimulatedClock(NOW)
    return application


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        yield client


async def get_calendar_range(client: httpx.AsyncClient, **params: str) -> tuple[int, Any]:
    response = await client.get(CALENDAR, params=params)
    return response.status_code, response.json()


class TestSessions:
    async def test_returns_a_session_per_trading_day(self, client: httpx.AsyncClient) -> None:
        _, body = await get_calendar_range(client, start="2024-07-01", end="2024-07-08")

        assert [s["day"] for s in body["sessions"]] == [
            "2024-07-01",
            "2024-07-02",
            "2024-07-03",
            "2024-07-05",
            "2024-07-08",
        ], "4 July is a holiday, 6-7 July a weekend"

    async def test_bounds_are_utc(self, client: httpx.AsyncClient) -> None:
        """Rule §1.2 all the way to the wire. A client wanting New York
        wall-clock has `timezone` to convert with."""
        _, body = await get_calendar_range(client, start="2024-07-08", end="2024-07-08")

        session = body["sessions"][0]
        assert session["open_at"] == "2024-07-08T13:30:00Z"
        assert session["close_at"] == "2024-07-08T20:00:00Z"

    async def test_an_early_close_is_flagged(self, client: httpx.AsyncClient) -> None:
        """3 July shuts at 13:00 New York. A dashboard that renders it as a
        normal session teaches its reader the market always shuts at four."""
        _, body = await get_calendar_range(client, start="2024-07-03", end="2024-07-03")

        session = body["sessions"][0]
        assert session["is_early_close"] is True
        assert session["close_at"] == "2024-07-03T17:00:00Z"

    async def test_an_ordinary_session_is_not_flagged(self, client: httpx.AsyncClient) -> None:
        _, body = await get_calendar_range(client, start="2024-07-08", end="2024-07-08")

        assert body["sessions"][0]["is_early_close"] is False

    async def test_the_range_is_inclusive_at_both_ends(self, client: httpx.AsyncClient) -> None:
        _, body = await get_calendar_range(client, start="2024-07-08", end="2024-07-09")

        assert [s["day"] for s in body["sessions"]] == ["2024-07-08", "2024-07-09"]

    async def test_a_closed_range_has_no_sessions(self, client: httpx.AsyncClient) -> None:
        _, body = await get_calendar_range(client, start="2024-07-06", end="2024-07-07")

        assert body["sessions"] == []

    async def test_the_exchange_and_timezone_are_named(self, client: httpx.AsyncClient) -> None:
        """So a client is never guessing which venue's day it is looking at."""
        _, body = await get_calendar_range(client, start="2024-07-08", end="2024-07-08")

        assert body["exchange"] == "NYSE"
        assert body["timezone"] == "America/New_York"


class TestHolidays:
    async def test_a_holiday_is_reported(self, client: httpx.AsyncClient) -> None:
        _, body = await get_calendar_range(client, start="2024-07-01", end="2024-07-08")

        assert body["holidays"] == ["2024-07-04"]

    async def test_weekends_are_not_holidays(self, client: httpx.AsyncClient) -> None:
        """Every client knows about Saturdays. Burying ~10 real closures a year
        in 104 ordinary ones makes the list useless."""
        _, body = await get_calendar_range(client, start="2024-07-06", end="2024-07-07")

        assert body["holidays"] == []

    async def test_a_range_with_no_closures_reports_none(self, client: httpx.AsyncClient) -> None:
        _, body = await get_calendar_range(client, start="2024-07-08", end="2024-07-12")

        assert body["holidays"] == []


class TestDefaultRange:
    async def test_no_bounds_centres_on_today(self, client: httpx.AsyncClient) -> None:
        _, body = await get_calendar_range(client)

        assert body["start"] == "2024-06-10"
        assert body["end"] == "2024-08-09"

    async def test_only_a_start_runs_forward_from_it(self, client: httpx.AsyncClient) -> None:
        _, body = await get_calendar_range(client, start="2024-07-01")

        assert (body["start"], body["end"]) == ("2024-07-01", "2024-07-31")

    async def test_only_an_end_runs_back_from_it(self, client: httpx.AsyncClient) -> None:
        _, body = await get_calendar_range(client, end="2024-07-31")

        assert (body["start"], body["end"]) == ("2024-07-01", "2024-07-31")

    async def test_today_is_the_exchanges_today_not_utcs(self, app: FastAPI) -> None:
        """01:00 UTC on 11 July is still 21:00 on the 10th in New York. A
        default range built from the UTC date would disagree with the sessions
        it contains, in exactly the hours an operator is up late."""
        app.dependency_overrides[get_clock] = lambda: SimulatedClock(
            datetime(2024, 7, 11, 1, 0, tzinfo=UTC)
        )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            _, body = await get_calendar_range(client)

        assert body["end"] == "2024-08-09", "centred on 10 July in New York, not 11 July in UTC"


class TestBadRequests:
    async def test_an_inverted_range_is_refused(self, client: httpx.AsyncClient) -> None:
        status_code, body = await get_calendar_range(client, start="2024-07-08", end="2024-07-01")

        assert status_code == 422
        assert "on or before" in str(body["detail"])

    async def test_an_unbounded_range_is_refused(self, client: httpx.AsyncClient) -> None:
        """The exchange rules span three centuries and are built a year at a
        time. One request must not be able to spend minutes on that."""
        status_code, body = await get_calendar_range(client, start="1990-01-01", end="2024-01-01")

        assert status_code == 422
        assert str(MAX_CALENDAR_DAYS) in str(body["detail"])

    async def test_the_largest_allowed_range_is_accepted(self, client: httpx.AsyncClient) -> None:
        """The boundary itself, so the cap cannot drift into an off-by-one that
        rejects the range it advertises."""
        start = datetime(2020, 1, 1, tzinfo=UTC).date()
        end = start + timedelta(days=MAX_CALENDAR_DAYS - 1)

        status_code, _ = await get_calendar_range(
            client, start=start.isoformat(), end=end.isoformat()
        )

        assert status_code == 200

    async def test_a_malformed_date_is_refused(self, client: httpx.AsyncClient) -> None:
        status_code, _ = await get_calendar_range(client, start="last tuesday")

        assert status_code == 422
