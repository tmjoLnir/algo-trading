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

from datetime import date
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

    def test_a_trade_update_names_the_venue_it_came_from(self) -> None:
        """The adapter stamps its own name, because nothing downstream can.

        A rejection pushed on this stream is one of the three ways a venue
        refusal reaches an order, and it is the only one with no broker within
        reach when it lands: the runner consumes the stream and reaches a
        broker only through the router (rule §1.5). Carried on the event, it
        becomes `Order.rejected_by` in `execution.trade_updates`.
        """
        broker = make_broker()
        update = broker._to_trade_update(
            {
                "stream": "trade_updates",
                "data": {
                    "event": "rejected",
                    "order": order_payload(status="rejected", reject_reason="symbol halted"),
                },
            }
        )

        assert update is not None
        # Paper and live are different venues and the name separates them, the
        # same distinction `run_mode` keeps in the order table.
        assert update.broker == broker.name == "alpaca-paper"
        assert update.reason == "symbol halted"


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


ACTIVITIES_URL = f"{BASE}/v2/account/activities"

#: The account feed for 2026-09-08, verbatim. Kept whole rather than trimmed to
#: the fields the parser reads, because the shape is half of what is under test
#: — `REG` and `TAF` are `activity_sub_type` values under one `FEE`
#: `activity_type`, and a reader who takes them for activity types asks a
#: question the venue answers with an empty list.
DAY_TWO_ACTIVITIES: list[dict[str, Any]] = [
    {
        "id": "20260908000000000::da7b17a5-2eb4-4990-858c-ff53b830f8de",
        "activity_type": "FEE",
        "activity_sub_type": "CAT",
        "date": "2026-09-08",
        "created_at": "2026-09-09T00:06:01.682153Z",
        "net_amount": "-0.01",
        "description": "CAT fee for proceed of 174 trades on 2026-09-08 by PA3C8I8RRUBZ",
        "status": "executed",
        "currency": "USD",
    },
    {
        "id": "20260908000000000::97e8ab4e-6a81-42f5-8338-1bd6b5609864",
        "activity_type": "FEE",
        "activity_sub_type": "REG",
        "date": "2026-09-08",
        "created_at": "2026-09-09T00:17:05.448916Z",
        "net_amount": "-3.82",
        "description": "REG fee for proceed of $185271.37 on 2026-09-08 by PA3C8I8RRUBZ",
        "status": "executed",
        "currency": "USD",
    },
    {
        "id": "20260908000000000::6fa7483d-449e-474d-8909-b1bc9a8e029b",
        "activity_type": "FEE",
        "activity_sub_type": "TAF",
        "date": "2026-09-08",
        "created_at": "2026-09-09T00:17:05.448916Z",
        "net_amount": "-0.31",
        "description": "TAF fee for proceed of 1572 shares (89 trades) on 2026-09-08",
        "status": "executed",
        "currency": "USD",
    },
]


class TestFeeActivities:
    """The endpoint two comments in this adapter called "a known gap the
    activities endpoint closes" while nothing called it."""

    @respx.mock
    @pytest.mark.asyncio
    async def test_the_real_feed_parses_to_the_four_dollars_that_halted_the_worker(
        self,
    ) -> None:
        route = respx.get(ACTIVITIES_URL).mock(
            return_value=httpx.Response(200, json=DAY_TWO_ACTIVITIES)
        )

        fees = await make_broker().get_fee_activities(date(2026, 8, 10))

        assert sum(f.amount for f in fees) == Decimal("4.14")
        # The venue's own order, not the order of a UUID suffix.
        assert [f.sub_type for f in fees] == ["CAT", "REG", "TAF"]
        assert all(f.booked_on == date(2026, 9, 8) for f in fees)
        # One activity type, not three. Asking for REG and TAF as types is the
        # mistake that returns nothing and looks like a venue charging nothing.
        assert route.calls.last.request.url.params["activity_types"] == "FEE"

    @respx.mock
    @pytest.mark.asyncio
    async def test_the_sign_is_normalised_to_positive_for_a_charge(self) -> None:
        """Alpaca signs money leaving the account negative. Every caller settles
        with `cash -= amount`, so the adapter owns the venue's convention and
        nothing downstream has to remember it."""
        respx.get(ACTIVITIES_URL).mock(
            return_value=httpx.Response(200, json=[DAY_TWO_ACTIVITIES[1]])
        )

        fees = await make_broker().get_fee_activities(date(2026, 8, 10))

        assert fees[0].amount == Decimal("3.82"), "positive, though the wire said -3.82"

    @respx.mock
    @pytest.mark.asyncio
    async def test_a_second_page_is_followed(self) -> None:
        """A partial read looks exactly like a session that paid fewer fees."""
        first = [dict(row, id=f"page1-{i}") for i, row in enumerate(DAY_TWO_ACTIVITIES)]
        first += [dict(DAY_TWO_ACTIVITIES[0], id=f"pad-{i}") for i in range(97)]
        assert len(first) == 100
        second = [dict(DAY_TWO_ACTIVITIES[1], id="page2-1")]
        respx.get(ACTIVITIES_URL).mock(
            side_effect=[httpx.Response(200, json=first), httpx.Response(200, json=second)]
        )

        fees = await make_broker().get_fee_activities(date(2026, 8, 10))

        assert len(fees) == 101
        assert any(f.activity_id == "page2-1" for f in fees)

    @respx.mock
    @pytest.mark.asyncio
    async def test_a_fee_the_venue_has_not_charged_yet_is_not_applied(self) -> None:
        """Applying a pending fee puts our cash *below* the venue's — drift in
        the other direction, reported identically and harder to read."""
        respx.get(ACTIVITIES_URL).mock(
            return_value=httpx.Response(200, json=[dict(DAY_TWO_ACTIVITIES[1], status="pending")])
        )

        assert await make_broker().get_fee_activities(date(2026, 8, 10)) == []

    @respx.mock
    @pytest.mark.asyncio
    async def test_one_unreadable_row_costs_that_row_and_not_the_batch(self) -> None:
        """The same argument `sweepable_series` makes for one dead ticker. What
        is skipped stays unapplied and therefore stays visible as drift."""
        rows: list[dict[str, Any]] = [{"id": "broken", "activity_type": "FEE"}]
        rows += DAY_TWO_ACTIVITIES

        fees = await self._fetch(rows)

        assert sum(f.amount for f in fees) == Decimal("4.14")

    @respx.mock
    @pytest.mark.asyncio
    async def test_a_venue_that_charges_nothing_returns_nothing(self) -> None:
        assert await self._fetch([]) == []

    @staticmethod
    async def _fetch(rows: list[dict[str, Any]]) -> list[Any]:
        respx.get(ACTIVITIES_URL).mock(return_value=httpx.Response(200, json=rows))
        return await make_broker().get_fee_activities(date(2026, 8, 10))


@pytest.mark.asyncio
async def test_the_adapter_still_satisfies_the_port() -> None:
    assert isinstance(make_broker(), BrokerPort)
