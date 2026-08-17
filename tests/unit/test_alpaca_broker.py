"""Alpaca broker adapter.

No test here touches the network — `respx` intercepts httpx at the transport
layer (CLAUDE.md §1.7). Payloads are shaped like real Alpaca order and position
responses, strings and all, because how those strings become `Decimal` is one
of the things being tested.

The failure paths carry the weight. A submit that times out having already
landed is the case that turns a network blip into a duplicate position, and an
order status nobody mapped is the case that reports a dead order as working.
"""

from __future__ import annotations

from decimal import Decimal
from typing import TYPE_CHECKING, Any

import httpx
import pytest
import respx
from pydantic import SecretStr

from atp_core.brokers import BrokerPort
from atp_core.brokers.alpaca import AlpacaBroker
from atp_core.config import Settings
from atp_core.domain import Order, OrderStatus, OrderType, Side, TimeInForce
from atp_core.errors import (
    BrokerConnectionError,
    BrokerError,
    InsufficientFundsError,
    OrderRejectedError,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

BASE = "https://paper-api.alpaca.markets"
ORDERS_URL = f"{BASE}/v2/orders"


def make_settings(**kwargs: Any) -> Settings:
    """Paper settings.

    `run_mode` is passed by its **alias**. The four process switches are
    aliased to `ATP_*`, and this model does not populate by field name — so
    `Settings(run_mode=...)` is silently dropped by `extra="ignore"` and you
    get the default instead of the mode you asked for.
    """
    return Settings(
        ATP_RUN_MODE="paper",
        alpaca_api_key=SecretStr("test-key-id"),
        alpaca_api_secret=SecretStr("test-secret"),
        **kwargs,
    )


_OPEN_CLIENTS: list[httpx.AsyncClient] = []


@pytest.fixture(autouse=True)
async def _close_clients() -> AsyncIterator[None]:
    yield
    for client in _OPEN_CLIENTS:
        await client.aclose()
    _OPEN_CLIENTS.clear()


def make_broker(**kwargs: Any) -> AlpacaBroker:
    #: Zero backoff: the retry paths are under test, not the wall clock.
    client = httpx.AsyncClient(timeout=httpx.Timeout(30.0, connect=10.0))
    _OPEN_CLIENTS.append(client)
    return AlpacaBroker(make_settings(), backoff_base_seconds=0.0, client=client, **kwargs)


def order_payload(**overrides: Any) -> dict[str, Any]:
    """One raw Alpaca order. Money arrives as strings, as it really does."""
    payload = {
        "id": "brk-abc-123",
        "client_order_id": "atp-deadbeef",
        "symbol": "SPY",
        "qty": "100",
        "side": "buy",
        "order_type": "market",
        "time_in_force": "day",
        "status": "new",
        "filled_qty": "0",
        "filled_avg_price": None,
        "limit_price": None,
        "stop_price": None,
        "created_at": "2024-06-03T13:30:00Z",
        "submitted_at": "2024-06-03T13:30:00.123456Z",
        "filled_at": None,
    }
    payload.update(overrides)
    return payload


def an_order() -> Order:
    return Order(
        symbol="SPY",
        side=Side.BUY,
        qty=Decimal("100"),
        order_type=OrderType.MARKET,
        time_in_force=TimeInForce.DAY,
        client_order_id="atp-deadbeef",
    )


class TestConstruction:
    def test_satisfies_the_broker_port(self) -> None:
        assert isinstance(make_broker(), BrokerPort)

    def test_refuses_to_serve_a_backtest(self) -> None:
        with pytest.raises(ValueError, match="use SimulatedBroker"):
            AlpacaBroker(Settings(ATP_RUN_MODE="backtest"))

    def test_paper_and_live_are_the_same_adapter_on_different_hosts(self) -> None:
        """Requirement #5 at this layer: identical code, different endpoint."""
        paper = make_broker()
        assert paper.name == "alpaca-paper"
        assert paper._base_url == "https://paper-api.alpaca.markets"

        live = AlpacaBroker(
            Settings(
                ATP_RUN_MODE="live",
                ATP_ALLOW_LIVE_TRADING=True,
                alpaca_api_key=SecretStr("k"),
                alpaca_api_secret=SecretStr("s"),
            )
        )
        assert live.name == "alpaca-live"
        assert live._base_url == "https://api.alpaca.markets"


class TestSubmit:
    @respx.mock
    @pytest.mark.asyncio
    async def test_sends_the_client_order_id_and_stringifies_every_number(self) -> None:
        """`json.dumps` cannot serialise a `Decimal`, and both fallbacks lose
        exactness on exactly the fields where it matters (rule §1.1)."""
        route = respx.post(ORDERS_URL).mock(return_value=httpx.Response(200, json=order_payload()))

        await make_broker().submit_order(
            Order(
                symbol="SPY",
                side=Side.BUY,
                qty=Decimal("100"),
                order_type=OrderType.LIMIT,
                limit_price=Decimal("123.45"),
                client_order_id="atp-deadbeef",
            )
        )

        body = route.calls.last.request.read().decode()
        assert '"client_order_id":"atp-deadbeef"' in body
        assert '"limit_price":"123.45"' in body
        assert '"qty":"100"' in body

    @respx.mock
    @pytest.mark.asyncio
    async def test_credentials_go_in_headers_never_the_url(self) -> None:
        """URLs end up in access logs, traces and exception messages
        (CLAUDE.md §1.6)."""
        route = respx.post(ORDERS_URL).mock(return_value=httpx.Response(200, json=order_payload()))

        await make_broker().submit_order(an_order())

        request = route.calls.last.request
        assert request.headers["APCA-API-KEY-ID"] == "test-key-id"
        assert "test-secret" not in str(request.url)

    @respx.mock
    @pytest.mark.asyncio
    async def test_a_venue_refusal_is_not_retried(self) -> None:
        """It is a refusal, not a blip. Repeating it asks the same question."""
        route = respx.post(ORDERS_URL).mock(
            return_value=httpx.Response(422, text="stop price must be below current price")
        )

        with pytest.raises(OrderRejectedError, match="stop price"):
            await make_broker().submit_order(an_order())

        assert route.call_count == 1

    @respx.mock
    @pytest.mark.asyncio
    async def test_insufficient_buying_power_is_its_own_error(self) -> None:
        """ "No money" and "no permission" are both 403 and need different
        handling by whatever catches them."""
        respx.post(ORDERS_URL).mock(
            return_value=httpx.Response(
                403, json={"code": 40310000, "message": "insufficient buying power"}
            )
        )

        with pytest.raises(InsufficientFundsError):
            await make_broker().submit_order(an_order())

    @respx.mock
    @pytest.mark.asyncio
    async def test_a_transport_failure_that_landed_is_adopted_not_resubmitted(self) -> None:
        """The case that turns a network blip into a duplicate position.

        The POST dies in transport, so we do not know whether the venue acted
        on it. The adapter asks — and having found the order, returns it. A
        blind resubmit here is the bug rule §1.4 exists to prevent.
        """
        submit = respx.post(ORDERS_URL).mock(side_effect=httpx.ConnectError("reset by peer"))
        lookup = respx.get(f"{BASE}/v2/orders:by_client_order_id").mock(
            return_value=httpx.Response(
                200,
                json=order_payload(status="filled", filled_qty="100", filled_avg_price="512.30"),
            )
        )

        result = await make_broker().submit_order(an_order())

        assert result.broker_order_id == "brk-abc-123"
        assert result.filled_qty == Decimal("100")
        assert lookup.call_count == 1
        # Retried in transport, but never a second *order*: every attempt
        # carries the same client_order_id, which is what makes that safe.
        assert all(
            b'"client_order_id":"atp-deadbeef"' in call.request.read() for call in submit.calls
        )

    @respx.mock
    @pytest.mark.asyncio
    async def test_a_transport_failure_that_never_landed_raises(self) -> None:
        """Nothing was created and nothing was resubmitted. The caller retries
        with the same key, which is the only safe retry."""
        respx.post(ORDERS_URL).mock(side_effect=httpx.ConnectError("reset by peer"))
        respx.get(f"{BASE}/v2/orders:by_client_order_id").mock(return_value=httpx.Response(404))

        with pytest.raises(BrokerConnectionError):
            await make_broker().submit_order(an_order())

    @respx.mock
    @pytest.mark.asyncio
    async def test_a_429_is_retried(self) -> None:
        """200 req/min on the free tier; rate limiting is ordinary operation."""
        route = respx.post(ORDERS_URL).mock(
            side_effect=[
                httpx.Response(429, headers={"Retry-After": "0"}),
                httpx.Response(200, json=order_payload()),
            ]
        )

        result = await make_broker().submit_order(an_order())

        assert route.call_count == 2
        assert result.status is OrderStatus.SUBMITTED


class TestStatusTranslation:
    """The vocabulary gap. Alpaca has more states than we do."""

    @pytest.mark.parametrize(
        ("alpaca", "ours"),
        [
            ("new", OrderStatus.SUBMITTED),
            ("accepted", OrderStatus.SUBMITTED),
            ("pending_new", OrderStatus.SUBMITTED),
            ("partially_filled", OrderStatus.PARTIALLY_FILLED),
            ("filled", OrderStatus.FILLED),
            ("canceled", OrderStatus.CANCELLED),
            ("expired", OrderStatus.EXPIRED),
            ("done_for_day", OrderStatus.EXPIRED),
            ("rejected", OrderStatus.REJECTED),
            ("pending_cancel", OrderStatus.SUBMITTED),
        ],
    )
    def test_every_documented_status_maps(self, alpaca: str, ours: OrderStatus) -> None:
        # A *partial* fill has to be genuinely partial: fill the whole 100 and
        # `apply_fill` correctly reports FILLED whatever the venue called it.
        filled: dict[str, Any] = {}
        if alpaca == "filled":
            filled = {"filled_qty": "100", "filled_avg_price": "500"}
        elif alpaca == "partially_filled":
            filled = {"filled_qty": "40", "filled_avg_price": "500"}

        order = AlpacaBroker._from_alpaca_order(order_payload(status=alpaca, **filled))

        assert order.status is ours

    def test_an_unknown_status_raises_rather_than_defaulting(self) -> None:
        """The plausible default is SUBMITTED, and an order reported as working
        when the venue has killed it is a position nobody is watching."""
        with pytest.raises(BrokerError, match="unrecognised Alpaca order status"):
            AlpacaBroker._from_alpaca_order(order_payload(status="teleported"))

    def test_a_rejection_carries_its_reason(self) -> None:
        order = AlpacaBroker._from_alpaca_order(
            order_payload(status="rejected", reject_reason="symbol halted")
        )
        assert order.reject_reason == "symbol halted"


class TestFillTranslation:
    def test_prices_and_quantities_arrive_as_decimal(self) -> None:
        """Never float — rule §1.1."""
        order = AlpacaBroker._from_alpaca_order(
            order_payload(status="filled", filled_qty="100", filled_avg_price="512.30")
        )

        assert isinstance(order.avg_fill_price, Decimal)
        assert order.avg_fill_price == Decimal("512.30")
        assert order.filled_qty == Decimal("100")

    def test_a_partial_fill_is_reported_as_partial(self) -> None:
        order = AlpacaBroker._from_alpaca_order(
            order_payload(status="partially_filled", filled_qty="40", filled_avg_price="512.30")
        )

        assert order.status is OrderStatus.PARTIALLY_FILLED
        assert order.remaining_qty == Decimal("60")

    def test_fills_go_through_apply_fill_rather_than_around_it(self) -> None:
        """So the average price comes from the same accounting every other
        fill in the platform goes through."""
        order = AlpacaBroker._from_alpaca_order(
            order_payload(status="filled", filled_qty="100", filled_avg_price="512.30")
        )

        assert len(order.fills) == 1
        assert order.fills[0].qty == Decimal("100")

    def test_a_filled_quantity_with_no_price_raises(self) -> None:
        """It would otherwise book a fill at a price of None and corrupt P&L."""
        with pytest.raises(BrokerError, match="no average price"):
            AlpacaBroker._from_alpaca_order(
                order_payload(status="filled", filled_qty="100", filled_avg_price=None)
            )

    def test_timestamps_are_utc_aware(self) -> None:
        """Naive datetimes are rejected at the domain boundary (rule §1.2)."""
        order = AlpacaBroker._from_alpaca_order(order_payload())
        assert order.submitted_at is not None
        assert order.submitted_at.tzinfo is not None


class TestReads:
    @respx.mock
    @pytest.mark.asyncio
    async def test_account_maps_both_blocked_flags_to_one_refusal(self) -> None:
        """The caller's decision is the same for both, and reading only
        `trading_blocked` misses an account frozen at the account level."""
        respx.get(f"{BASE}/v2/account").mock(
            return_value=httpx.Response(
                200,
                json={
                    "id": "acct-1",
                    "equity": "100000.55",
                    "cash": "50000.25",
                    "buying_power": "200000",
                    "maintenance_margin": "0",
                    "pattern_day_trader": False,
                    "trading_blocked": False,
                    "account_blocked": True,
                },
            )
        )

        account = await make_broker().get_account()

        assert account.trading_blocked is True
        assert account.equity == Decimal("100000.55")
        assert isinstance(account.cash, Decimal)

    @respx.mock
    @pytest.mark.asyncio
    async def test_get_order_returns_none_for_an_unknown_id(self) -> None:
        respx.get(f"{ORDERS_URL}/nope").mock(return_value=httpx.Response(404))
        assert await make_broker().get_order("nope") is None

    @respx.mock
    @pytest.mark.asyncio
    async def test_open_orders_asks_for_a_flat_list(self) -> None:
        """A bracket's children are orders in their own right; hidden inside a
        parent they read to reconciliation as orders that do not exist."""
        route = respx.get(ORDERS_URL).mock(return_value=httpx.Response(200, json=[order_payload()]))

        orders = await make_broker().get_open_orders()

        assert len(orders) == 1
        assert route.calls.last.request.url.params["nested"] == "false"
        assert route.calls.last.request.url.params["status"] == "open"

    @respx.mock
    @pytest.mark.asyncio
    async def test_positions_take_the_sign_as_authoritative(self) -> None:
        """`qty` and `side` can disagree; the sign is what every downstream
        calculation actually uses."""
        respx.get(f"{BASE}/v2/positions").mock(
            return_value=httpx.Response(
                200,
                json=[
                    {
                        "symbol": "SPY",
                        "qty": "-50",
                        "side": "short",
                        "avg_entry_price": "512.30",
                        "current_price": "510.00",
                    }
                ],
            )
        )

        positions = await make_broker().get_positions()

        assert positions[0].qty == Decimal("-50")
        assert positions[0].is_short
        assert positions[0].avg_entry_price == Decimal("512.30")

    @respx.mock
    @pytest.mark.asyncio
    async def test_is_market_open_reads_the_venue_clock(self) -> None:
        respx.get(f"{BASE}/v2/clock").mock(
            return_value=httpx.Response(200, json={"is_open": False})
        )
        assert await make_broker().is_market_open() is False


class TestCancelAndFlatten:
    @respx.mock
    @pytest.mark.asyncio
    async def test_cancelling_an_already_filled_order_is_not_an_error(self) -> None:
        """A race we lost, and the fill stands — as `BrokerPort` requires."""
        respx.delete(f"{ORDERS_URL}/brk-abc-123").mock(
            return_value=httpx.Response(422, text="order is not cancelable")
        )

        await make_broker().cancel_order("brk-abc-123")  # does not raise

    @respx.mock
    @pytest.mark.asyncio
    async def test_flatten_cancels_resting_orders_first(self) -> None:
        """Otherwise a stop keeps working against a position that no longer
        exists, and opens the other side the moment it fires."""
        route = respx.delete(f"{BASE}/v2/positions").mock(
            return_value=httpx.Response(
                200, json=[{"symbol": "SPY", "status": 200, "body": order_payload(side="sell")}]
            )
        )

        closed = await make_broker().close_all_positions()

        assert len(closed) == 1
        assert route.calls.last.request.url.params["cancel_orders"] == "true"

    @respx.mock
    @pytest.mark.asyncio
    async def test_a_partial_flatten_failure_raises_rather_than_reporting_success(self) -> None:
        """Alpaca answers 207 with per-symbol statuses, so a failure looks like
        a success at the HTTP level. A flatten that silently left a position
        open is the worst possible outcome for this call."""
        respx.delete(f"{BASE}/v2/positions").mock(
            return_value=httpx.Response(
                200,
                json=[
                    {"symbol": "SPY", "status": 200, "body": order_payload(side="sell")},
                    {"symbol": "QQQ", "status": 500, "body": None},
                ],
            )
        )

        with pytest.raises(BrokerError, match="QQQ"):
            await make_broker().close_all_positions()
