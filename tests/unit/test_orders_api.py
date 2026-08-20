"""`GET /api/v1/orders` over ASGI.

A unit test rather than an integration one: the only source the handler reads is
behind a port, so the whole route runs against a fake with no database
(CLAUDE.md §1.7). The SQL itself is exercised in
`tests/integration/test_order_persistence.py`, which is where an ordering or a
filter can actually be wrong.

What is worth holding here is what the *screen* is allowed to say. Three things,
and each one is a way for this endpoint to mislead rather than to break:

1. **A rejection reaches the screen with its reason.** This endpoint exists
   because a refused order appears in no other read in the platform. Serving one
   with a null reason would put it on the screen and still leave the reader
   unable to act on it.
2. **A full page says it is full.** A list that stops at exactly the limit looks
   identical to a list that ended, and only one of them means "this is
   everything".
3. **An unknown status is a 422 naming the real ones**, not an empty list.
   Somebody filtering for rejections and being told there are none would
   conclude the opposite of the truth.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING

import httpx
import pytest

from atp_api.auth import Scope, Session
from atp_api.deps import get_current_session, get_order_repository
from atp_api.main import create_app
from atp_core.config import Settings, get_settings
from atp_core.domain import Fill, Order, OrderStatus, OrderType, Side
from atp_core.execution.idempotency import ENTRY, STOP_LOSS
from tests.fakes import FakeOrderRepository

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from fastapi import FastAPI

ORDERS = "/api/v1/orders"

T0 = datetime(2026, 3, 2, 14, 30, tzinfo=UTC)


def pinned_settings() -> Settings:
    """Settings that do not depend on the shell the suite is run from.

    `ATP_RUN_MODE` reaches `recent_orders` as its run-mode filter, and the
    response echoes it, so an ambient value would change both the query and an
    assertion about what the screen says it is showing.
    """
    return Settings(ATP_RUN_MODE="backtest", _env_file=None)


def order(
    *,
    symbol: str = "SPY",
    side: Side = Side.BUY,
    qty: str = "10",
    created_at: datetime = T0,
    status: OrderStatus = OrderStatus.FILLED,
    purpose: str = ENTRY,
    strategy_id: str | None = "sma",
    reject_reason: str | None = None,
    filled: str | None = "100",
) -> Order:
    built = Order(
        symbol=symbol,
        side=side,
        qty=Decimal(qty),
        order_type=OrderType.MARKET,
        strategy_id=strategy_id,
        purpose=purpose,
        created_at=created_at,
    )
    if filled is not None:
        built.apply_fill(
            Fill(
                order_id=built.id,
                ts=created_at,
                qty=Decimal(qty),
                price=Decimal(filled),
                fee=Decimal(0),
            )
        )
    built.status = status
    built.reject_reason = reject_reason
    return built


@pytest.fixture
def orders() -> FakeOrderRepository:
    return FakeOrderRepository()


@pytest.fixture
def app(orders: FakeOrderRepository) -> FastAPI:
    application = create_app()
    application.dependency_overrides[get_settings] = pinned_settings
    application.dependency_overrides[get_order_repository] = lambda: orders
    # A read route; `test_api_contract.py` holds the scope enforcement itself
    # against every route.
    application.dependency_overrides[get_current_session] = lambda: Session(
        "test-operator", Scope.FULL
    )
    return application


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
        yield http


class TestWhatTheScreenCanSay:
    @pytest.mark.asyncio
    async def test_a_refused_order_arrives_with_its_reason(
        self, client: httpx.AsyncClient, orders: FakeOrderRepository
    ) -> None:
        """The row this endpoint exists for.

        A rejection is in no other read: not the book, not a round trip, not the
        equity curve. Putting it on the screen without the reason it was refused
        leaves the reader knowing something went wrong and not what.
        """
        orders.history = [
            order(
                status=OrderStatus.REJECTED_RISK,
                reject_reason="MaxPositionSize: 500 shares exceeds the 100 limit",
                filled=None,
            )
        ]

        body = (await client.get(ORDERS)).json()

        assert len(body["orders"]) == 1
        row = body["orders"][0]
        assert row["status"] == "rejected_risk"
        assert row["reject_reason"] == "MaxPositionSize: 500 shares exceeds the 100 limit"

    @pytest.mark.asyncio
    async def test_orders_that_never_filled_are_included(
        self, client: httpx.AsyncClient, orders: FakeOrderRepository
    ) -> None:
        """`filled_orders` excludes these by design; this must not.

        A strategy refused every morning for a month is, from every other read
        in the platform, indistinguishable from one that never placed an order.
        """
        orders.history = [
            order(
                status=OrderStatus.REJECTED, reject_reason="insufficient buying power", filled=None
            ),
            order(status=OrderStatus.CANCELLED, filled=None),
            order(status=OrderStatus.FILLED),
        ]

        body = (await client.get(ORDERS)).json()

        assert {row["status"] for row in body["orders"]} == {"rejected", "cancelled", "filled"}

    @pytest.mark.asyncio
    async def test_a_full_page_says_so(
        self, client: httpx.AsyncClient, orders: FakeOrderRepository
    ) -> None:
        """A list that stops at the limit and a list that ended look the same."""
        orders.history = [order(created_at=T0 + timedelta(minutes=i)) for i in range(5)]

        full = (await client.get(f"{ORDERS}?limit=3")).json()
        assert len(full["orders"]) == 3
        assert full["limit_reached"] is True

        whole = (await client.get(f"{ORDERS}?limit=50")).json()
        assert len(whole["orders"]) == 5
        assert whole["limit_reached"] is False

    @pytest.mark.asyncio
    async def test_the_response_names_the_run_mode_it_scoped_to(
        self, client: httpx.AsyncClient, orders: FakeOrderRepository
    ) -> None:
        """Paper and live share a table. A screen must say which it is showing."""
        orders.history = [order()]
        assert (await client.get(ORDERS)).json()["run_mode"] == "backtest"

    @pytest.mark.asyncio
    async def test_newest_first(
        self, client: httpx.AsyncClient, orders: FakeOrderRepository
    ) -> None:
        """The opposite of `filled_orders`, and deliberately.

        That read is oldest-first because FIFO matching cannot pair an exit with
        its entry out of sequence. Nothing here is matched: it is a list read
        from the top, and the top is what just happened.
        """
        orders.history = [
            order(symbol="AAA", created_at=T0),
            order(symbol="BBB", created_at=T0 + timedelta(hours=1)),
            order(symbol="CCC", created_at=T0 + timedelta(hours=2)),
        ]

        body = (await client.get(ORDERS)).json()

        assert [row["symbol"] for row in body["orders"]] == ["CCC", "BBB", "AAA"]

    @pytest.mark.asyncio
    async def test_purpose_survives_to_the_screen(
        self, client: httpx.AsyncClient, orders: FakeOrderRepository
    ) -> None:
        """The only thing separating two exits that agree on everything else."""
        orders.history = [order(side=Side.SELL, purpose=STOP_LOSS)]
        assert (await client.get(ORDERS)).json()["orders"][0]["purpose"] == STOP_LOSS


class TestTheFilters:
    @pytest.mark.asyncio
    async def test_an_unknown_status_is_refused_by_name(
        self, client: httpx.AsyncClient, orders: FakeOrderRepository
    ) -> None:
        """Not an empty list.

        "You asked for something that does not exist" and "there are none of
        those" are different answers, and on this screen the second one is how a
        reader concludes nothing was refused when everything was.
        """
        orders.history = [order()]

        response = await client.get(f"{ORDERS}?status=nonsense")

        assert response.status_code == 422
        detail = response.json()["detail"]
        assert "nonsense" in detail
        assert "rejected_risk" in detail

    @pytest.mark.asyncio
    async def test_every_real_status_is_accepted(
        self, client: httpx.AsyncClient, orders: FakeOrderRepository
    ) -> None:
        """Guards the enum against drifting away from what the screen offers."""
        orders.history = []
        for status in OrderStatus:
            response = await client.get(f"{ORDERS}?status={status.value}")
            assert response.status_code == 200, f"{status.value} was refused"

    @pytest.mark.asyncio
    async def test_status_narrows_the_list(
        self, client: httpx.AsyncClient, orders: FakeOrderRepository
    ) -> None:
        orders.history = [
            order(symbol="AAA", status=OrderStatus.FILLED),
            order(symbol="BBB", status=OrderStatus.REJECTED_RISK, filled=None),
        ]

        body = (await client.get(f"{ORDERS}?status=rejected_risk")).json()

        assert [row["symbol"] for row in body["orders"]] == ["BBB"]

    @pytest.mark.asyncio
    async def test_a_symbol_filter_is_not_case_sensitive(
        self, client: httpx.AsyncClient, orders: FakeOrderRepository
    ) -> None:
        """A symbol is always an uppercase ticker (CLAUDE.md §4).

        Typed in lower case it would otherwise match nothing, and an empty table
        reads as "no such orders" rather than as "no such spelling".
        """
        orders.history = [order(symbol="SPY")]
        assert len((await client.get(f"{ORDERS}?symbol=spy")).json()["orders"]) == 1

    @pytest.mark.asyncio
    async def test_since_bounds_the_list_at_the_decision_instant(
        self, client: httpx.AsyncClient, orders: FakeOrderRepository
    ) -> None:
        orders.history = [
            order(symbol="OLD", created_at=T0 - timedelta(days=2)),
            order(symbol="NEW", created_at=T0),
        ]

        # Passed as a param rather than interpolated: an ISO instant ends in
        # `+00:00`, and a raw `+` in a query string decodes to a space.
        cutoff = (T0 - timedelta(hours=1)).isoformat()
        body = (await client.get(ORDERS, params={"since": cutoff})).json()

        assert [row["symbol"] for row in body["orders"]] == ["NEW"]

    @pytest.mark.asyncio
    async def test_a_limit_past_the_ceiling_is_refused(
        self, client: httpx.AsyncClient, orders: FakeOrderRepository
    ) -> None:
        """This is a screen, not an export."""
        assert (await client.get(f"{ORDERS}?limit=5000")).status_code == 422
