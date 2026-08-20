"""`GET /api/v1/positions` over ASGI.

A unit test: the only source the handler reads is behind a port, so the whole
route runs against a fake with no database (CLAUDE.md §1.7).

What is worth holding here is the distinction this endpoint exists for. The
dashboard reads the book the worker *published*, which is gone when the worker
stops. This reads the copy the worker *stored*, which is not — and the price of
that is that it can be arbitrarily old. So:

1. **The age is served, not left to the client.** A stored book without its age
   is a possibly-stale book presented as the current one.
2. **Never written and empty are different answers.** "You hold nothing" and
   "nobody has ever said what you hold" are different sentences and only one is
   safe to act on (ADR 0007).
3. **The derived figures agree with the dashboard's**, because both come from
   the same `atp_core.dashboard` expressions. A distance-to-stop that differed
   between two screens would be a bug invisible from either.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING

import httpx
import pytest

from atp_api.auth import Scope, Session
from atp_api.deps import get_clock, get_current_session, get_portfolio_repository
from atp_api.main import create_app
from atp_core.clock import SimulatedClock
from atp_core.config import Settings, get_settings
from atp_core.dashboard import position_summary
from atp_core.domain import Portfolio, Position
from tests.fakes import FakePortfolioRepository

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from fastapi import FastAPI

POSITIONS = "/api/v1/positions"

WROTE_AT = datetime(2026, 3, 2, 14, 30, tzinfo=UTC)
NOW = WROTE_AT + timedelta(hours=3)


def pinned_settings() -> Settings:
    """`ATP_RUN_MODE` reaches the repository as its filter and is echoed back."""
    return Settings(ATP_RUN_MODE="backtest", _env_file=None)


def a_position(
    symbol: str = "SPY",
    *,
    qty: str = "10",
    entry: str = "100",
    last: str | None = "110",
    stop: str | None = "90",
) -> Position:
    return Position(
        symbol=symbol,
        qty=Decimal(qty),
        avg_entry_price=Decimal(entry),
        last_price=Decimal(last) if last is not None else None,
        stop_loss_price=Decimal(stop) if stop is not None else None,
        opened_at=WROTE_AT - timedelta(days=1),
    )


def a_book(*positions: Position, cash: str = "50000") -> Portfolio:
    portfolio = Portfolio(cash=Decimal(cash), starting_equity=Decimal(cash))
    for position in positions:
        portfolio.positions[position.symbol] = position
    return portfolio


@pytest.fixture
def book() -> FakePortfolioRepository:
    repo = FakePortfolioRepository()
    repo.stored_at = WROTE_AT
    return repo


@pytest.fixture
def app(book: FakePortfolioRepository) -> FastAPI:
    application = create_app()
    application.dependency_overrides[get_settings] = pinned_settings
    application.dependency_overrides[get_clock] = lambda: SimulatedClock(NOW)
    application.dependency_overrides[get_portfolio_repository] = lambda: book
    application.dependency_overrides[get_current_session] = lambda: Session(
        "test-operator", Scope.FULL
    )
    return application


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
        yield http


class TestTheAge:
    @pytest.mark.asyncio
    async def test_the_book_is_served_with_how_old_it_is(
        self, client: httpx.AsyncClient, book: FakePortfolioRepository
    ) -> None:
        """The reason this endpoint exists rather than a second live one.

        A stored book can be hours old — the worker may have stopped — and the
        reader is usually here *because* it did.
        """
        book.stored = a_book(a_position())

        body = (await client.get(POSITIONS)).json()

        assert body["as_of"] == "2026-03-02T14:30:00Z"
        assert body["age_seconds"] == 3 * 3600

    @pytest.mark.asyncio
    async def test_a_worker_clock_ahead_of_ours_does_not_report_a_negative_age(
        self, client: httpx.AsyncClient, book: FakePortfolioRepository
    ) -> None:
        """Clamped, matching `/dashboard/live`. "Written -1s ago" reads as a bug
        in the dashboard rather than the clock skew it is."""
        book.stored = a_book(a_position())
        book.stored_at = NOW + timedelta(seconds=5)

        assert (await client.get(POSITIONS)).json()["age_seconds"] == 0


class TestNothingWritten:
    @pytest.mark.asyncio
    async def test_a_book_nobody_ever_wrote_is_not_an_empty_book(
        self, client: httpx.AsyncClient, book: FakePortfolioRepository
    ) -> None:
        """ "You hold nothing" and "nobody has ever said what you hold" are
        different sentences, and only one of them is safe to act on."""
        book.stored = None

        body = (await client.get(POSITIONS)).json()

        assert body["as_of"] is None
        assert body["age_seconds"] is None
        assert body["account"] is None
        assert body["positions"] == []

    @pytest.mark.asyncio
    async def test_a_flat_book_that_was_written_is_an_empty_one(
        self, client: httpx.AsyncClient, book: FakePortfolioRepository
    ) -> None:
        """The other half of the distinction: a real snapshot holding nothing
        reports an account and an age, with no rows."""
        book.stored = a_book()

        body = (await client.get(POSITIONS)).json()

        assert body["as_of"] is not None
        assert body["positions"] == []
        assert body["account"]["cash"] == "50000"


class TestTheFigures:
    @pytest.mark.asyncio
    async def test_every_derived_figure_matches_the_dashboard_expression(
        self, client: httpx.AsyncClient, book: FakePortfolioRepository
    ) -> None:
        """Computed by `atp_core.dashboard.position_summary`, the same function
        the live screen's book is built from.

        Asserted against that function rather than against literals, so the two
        screens cannot drift apart without this failing — which is the whole
        reason the endpoint calls it instead of writing its own arithmetic.
        """
        position = a_position(qty="10", entry="100", last="110", stop="90")
        book.stored = a_book(position)
        expected = position_summary(position)

        row = (await client.get(POSITIONS)).json()["positions"][0]

        assert Decimal(row["market_value"]) == expected.market_value
        assert Decimal(row["unrealized_pnl"]) == expected.unrealized_pnl
        assert Decimal(row["distance_to_stop_pct"]) == expected.distance_to_stop_pct

    @pytest.mark.asyncio
    async def test_an_unmarked_position_reports_null_rather_than_zero(
        self, client: httpx.AsyncClient, book: FakePortfolioRepository
    ) -> None:
        """A position reported as worth nothing is a position a reader ignores,
        and one a percentage limit reads as free."""
        book.stored = a_book(a_position(last=None))

        row = (await client.get(POSITIONS)).json()["positions"][0]

        assert row["last_price"] is None
        assert row["market_value"] is None
        assert row["unrealized_pnl"] is None

    @pytest.mark.asyncio
    async def test_a_position_through_its_stop_reports_a_negative_distance(
        self, client: httpx.AsyncClient, book: FakePortfolioRepository
    ) -> None:
        """Signed on purpose: clamping at zero would render the most alarming
        row on the screen as an ordinary one."""
        book.stored = a_book(a_position(entry="100", last="85", stop="90"))

        row = (await client.get(POSITIONS)).json()["positions"][0]

        assert Decimal(row["distance_to_stop_pct"]) < 0

    @pytest.mark.asyncio
    async def test_day_pnl_is_null_rather_than_zero(
        self, client: httpx.AsyncClient, book: FakePortfolioRepository
    ) -> None:
        """It is not a property of one book — it is this equity against the
        session's first recorded one — and zero is a value a reader acts on."""
        book.stored = a_book(a_position())

        account = (await client.get(POSITIONS)).json()["account"]

        assert account["day_pnl"] is None
        assert account["day_pnl_pct"] is None

    @pytest.mark.asyncio
    async def test_the_response_names_the_run_mode(
        self, client: httpx.AsyncClient, book: FakePortfolioRepository
    ) -> None:
        """Paper and live share the snapshot tables."""
        book.stored = a_book(a_position())

        assert (await client.get(POSITIONS)).json()["run_mode"] == "backtest"

    @pytest.mark.asyncio
    async def test_rows_come_back_in_a_stable_order(
        self, client: httpx.AsyncClient, book: FakePortfolioRepository
    ) -> None:
        """Sorted server-side. A client that sorted a column of money would be
        parsing money into a float to compare it (docs/DASHBOARD.md)."""
        book.stored = a_book(a_position("TSLA"), a_position("AAPL"), a_position("MSFT"))

        body = (await client.get(POSITIONS)).json()

        assert [row["symbol"] for row in body["positions"]] == ["AAPL", "MSFT", "TSLA"]
