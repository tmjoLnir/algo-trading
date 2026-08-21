"""The trade-updates stream and the applier that folds it into an order.

No socket is opened: the connection factory is injected, so the handshake, the
reconnect ladder and the parser all run off a scripted fake (CLAUDE.md §1.7).

The frames below are shaped like Alpaca's documented ones. That is worth
stating plainly rather than trusting: #34 found the market-data wire disagreeing
with the documentation in three ways at once, and nothing here has been checked
against a live account stream. What these tests pin is our *handling* — the
reconnect signal, the duplicate discard, the state-machine guard — not the
vendor's shape.
"""

from __future__ import annotations

import json
import random
from datetime import UTC, datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any

import pytest
from pydantic import SecretStr

from atp_core.brokers.alpaca import AlpacaBroker
from atp_core.brokers.ports import TradeUpdate, TradeUpdatesReconnected
from atp_core.config import Settings
from atp_core.domain import Fill, Order, OrderStatus, OrderType, Side, TimeInForce
from atp_core.errors import BrokerConnectionError, BrokerError, ReconciliationError
from atp_core.execution.trade_updates import apply_trade_update

if TYPE_CHECKING:
    from collections.abc import Sequence

AUTHORIZED = {"stream": "authorization", "data": {"status": "authorized", "action": "authenticate"}}
UNAUTHORIZED = {
    "stream": "authorization",
    "data": {"status": "unauthorized", "action": "authenticate"},
}
LISTENING = {"stream": "listening", "data": {"streams": ["trade_updates"]}}

CLIENT_ID = "atp-deadbeef"


def order_payload(**overrides: Any) -> dict[str, Any]:
    payload = {
        "id": "brk-abc-123",
        "client_order_id": CLIENT_ID,
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
        "submitted_at": "2024-06-03T13:30:00Z",
        "filled_at": None,
    }
    payload.update(overrides)
    return payload


def update_frame(event: str, **data: Any) -> dict[str, Any]:
    """One `trade_updates` frame, shaped like Alpaca's."""
    body: dict[str, Any] = {"event": event, "timestamp": "2024-06-03T14:30:00.123456Z"}
    body.update(data)
    body.setdefault("order", order_payload())
    return {"stream": "trade_updates", "data": body}


def fill_frame(
    qty: str = "100",
    price: str = "512.30",
    *,
    event: str = "fill",
    execution_id: str | None = "exec-1",
) -> dict[str, Any]:
    return update_frame(
        event,
        qty=qty,
        price=price,
        position_qty=qty,
        execution_id=execution_id,
        order=order_payload(status="filled", filled_qty=qty, filled_avg_price=price),
    )


class DroppedError(Exception):
    """Stands in for `websockets.ConnectionClosed`."""


class FakeConnection:
    """Hands back scripted frames, then whatever ends the connection."""

    def __init__(self, script: Sequence[Any]) -> None:
        self._script = list(script)
        self.sent: list[dict[str, Any]] = []
        self.closed = False

    async def send(self, message: str) -> None:
        self.sent.append(json.loads(message))

    async def recv(self) -> str:
        if not self._script:
            raise DroppedError("script exhausted")
        item = self._script.pop(0)
        if isinstance(item, Exception):
            raise item
        return json.dumps(item)

    async def close(self) -> None:
        self.closed = True


def make_settings() -> Settings:
    return Settings(
        ATP_RUN_MODE="paper",
        alpaca_api_key=SecretStr("test-key-id"),
        alpaca_api_secret=SecretStr("test-secret"),
    )


def build(
    *connections: FakeConnection, max_reconnect_attempts: int = 0
) -> tuple[AlpacaBroker, list[float], list[FakeConnection]]:
    """A broker wired to a queue of connections. Returns it, the sleeps it
    asked for, and the connections it was handed."""
    queue = list(connections)
    handed: list[FakeConnection] = []
    slept: list[float] = []

    async def connect(url: str) -> FakeConnection:
        if not queue:
            raise DroppedError("no connection left")
        connection = queue.pop(0)
        handed.append(connection)
        return connection

    async def sleep(seconds: float) -> None:
        slept.append(seconds)

    broker = AlpacaBroker(
        make_settings(),
        connect=connect,
        sleep=sleep,
        rng=random.Random(0),
        max_reconnect_attempts=max_reconnect_attempts,
    )
    return broker, slept, handed


async def drain(broker: AlpacaBroker, limit: int = 20) -> list[Any]:
    """Everything the stream yields before it gives up."""
    events: list[Any] = []
    with pytest.raises((BrokerConnectionError, BrokerError, DroppedError)):
        async for event in broker.stream_trade_updates():
            events.append(event)
            if len(events) >= limit:
                raise DroppedError("enough")
    return events


class TestHandshake:
    @pytest.mark.asyncio
    async def test_authenticates_then_listens(self) -> None:
        connection = FakeConnection([AUTHORIZED, LISTENING, fill_frame()])
        broker, _, _ = build(connection)

        await drain(broker, limit=1)

        auth, listen = connection.sent[0], connection.sent[1]
        assert auth["action"] == "authenticate"
        assert auth["data"]["key_id"] == "test-key-id"
        assert listen == {"action": "listen", "data": {"streams": ["trade_updates"]}}

    @pytest.mark.asyncio
    async def test_the_account_handshake_is_not_the_market_data_one(self) -> None:
        """Different action, nested credentials, different key names. Sending
        the market-data frame here authenticates nothing and the server simply
        never answers."""
        connection = FakeConnection([AUTHORIZED, LISTENING, fill_frame()])
        broker, _, _ = build(connection)

        await drain(broker, limit=1)

        auth = connection.sent[0]
        assert auth["action"] != "auth"
        assert "key" not in auth
        assert set(auth["data"]) == {"key_id", "secret_key"}

    @pytest.mark.asyncio
    async def test_a_refused_handshake_is_not_retried(self) -> None:
        """Bad credentials are not a blip. Another connection performs the
        same refusal, and paper and live use separate key pairs."""
        broker, slept, handed = build(FakeConnection([UNAUTHORIZED]), max_reconnect_attempts=5)

        with pytest.raises(BrokerError, match="paper and live"):
            async for _ in broker.stream_trade_updates():
                pass

        assert len(handed) == 1
        assert slept == []

    @pytest.mark.asyncio
    async def test_credentials_never_reach_an_error_message(self) -> None:
        """Rule §1.6."""
        broker, _, _ = build(FakeConnection([UNAUTHORIZED]), max_reconnect_attempts=0)

        with pytest.raises(BrokerError) as caught:
            async for _ in broker.stream_trade_updates():
                pass

        assert "test-secret" not in str(caught.value)
        assert "test-key-id" not in str(caught.value)

    @pytest.mark.asyncio
    async def test_a_half_open_socket_is_closed_before_the_retry(self) -> None:
        """A socket left open still holds a connection slot, so the retry would
        be refused by our own leak."""
        failed = FakeConnection([DroppedError("hung up mid-handshake")])
        good = FakeConnection([AUTHORIZED, LISTENING, fill_frame()])
        broker, _, _ = build(failed, good, max_reconnect_attempts=3)

        await drain(broker, limit=2)

        assert failed.closed is True


class TestTheStreamUrl:
    def test_is_derived_from_the_rest_host_rather_than_configured(self) -> None:
        """Trade updates must come from the account the orders went to. A
        separately-configured URL is one edit away from watching paper while
        trading live."""
        broker, _, _ = build()
        assert broker.stream_url == "wss://paper-api.alpaca.markets/stream"

        live = AlpacaBroker(
            Settings(
                ATP_RUN_MODE="live",
                ATP_ALLOW_LIVE_TRADING=True,
                alpaca_api_key=SecretStr("k"),
                alpaca_api_secret=SecretStr("s"),
            )
        )
        assert live.stream_url == "wss://api.alpaca.markets/stream"


class TestReconnect:
    @pytest.mark.asyncio
    async def test_a_drop_yields_the_reconnect_marker_before_the_next_event(self) -> None:
        """The ordering is the whole requirement: the consumer must re-read
        open orders over REST before it handles anything from the new
        connection, and Alpaca does not replay the gap."""
        first = FakeConnection([AUTHORIZED, LISTENING, fill_frame(), DroppedError("reset")])
        second = FakeConnection([AUTHORIZED, LISTENING, fill_frame(execution_id="exec-2")])
        broker, _, _ = build(first, second, max_reconnect_attempts=3)

        events = await drain(broker, limit=3)

        assert isinstance(events[0], TradeUpdate)
        assert isinstance(events[1], TradeUpdatesReconnected)
        assert isinstance(events[2], TradeUpdate)

    @pytest.mark.asyncio
    async def test_the_marker_spans_the_outage(self) -> None:
        first = FakeConnection([AUTHORIZED, LISTENING, fill_frame(), DroppedError("reset")])
        second = FakeConnection([AUTHORIZED, LISTENING, fill_frame(execution_id="exec-2")])
        broker, _, _ = build(first, second, max_reconnect_attempts=3)

        events = await drain(broker, limit=3)

        marker = events[1]
        assert isinstance(marker, TradeUpdatesReconnected)
        assert marker.gap_since == datetime(2024, 6, 3, 14, 30, 0, 123456, tzinfo=UTC)
        assert marker.reconnected_at >= marker.gap_since
        assert marker.attempts == 1

    @pytest.mark.asyncio
    async def test_backoff_is_exponential_capped_and_jittered(self) -> None:
        broker, slept, _ = build(
            *[FakeConnection([DroppedError("refused")]) for _ in range(6)],
            max_reconnect_attempts=5,
        )

        with pytest.raises(BrokerConnectionError, match="did not come back"):
            async for _ in broker.stream_trade_updates():
                pass

        assert len(slept) == 5
        assert slept == sorted(slept), "each wait should be at least as long as the last"
        # Jittered into [delay/2, delay], never above the ceiling.
        assert all(0 < s <= 2.0**i for i, s in enumerate(slept))

    @pytest.mark.asyncio
    async def test_gives_up_after_the_attempt_limit(self) -> None:
        broker, _, _ = build(
            *[FakeConnection([DroppedError("refused")]) for _ in range(4)],
            max_reconnect_attempts=2,
        )

        with pytest.raises(BrokerConnectionError, match="after 2 attempts"):
            async for _ in broker.stream_trade_updates():
                pass

    @pytest.mark.asyncio
    async def test_a_connection_that_never_delivers_keeps_backing_off(self) -> None:
        """A server that accepts and immediately drops us — a flapping upstream
        — must not reset the ladder into a hot loop."""
        broker, slept, _ = build(
            *[FakeConnection([AUTHORIZED, LISTENING, DroppedError("bye")]) for _ in range(4)],
            max_reconnect_attempts=3,
        )

        with pytest.raises(BrokerConnectionError):
            async for _ in broker.stream_trade_updates():
                pass

        assert slept == sorted(slept)
        assert len(slept) == 3


class TestParsing:
    @pytest.mark.asyncio
    async def test_a_fill_carries_the_individual_print(self) -> None:
        """The whole reason this stream exists. REST reports running totals;
        only this carries the print that moved them (CLAUDE.md §5)."""
        broker, _, _ = build(
            FakeConnection([AUTHORIZED, LISTENING, fill_frame(qty="40", price="512.30")])
        )

        events = await drain(broker, limit=1)

        update = events[0]
        assert isinstance(update, TradeUpdate)
        assert update.fill is not None
        assert update.fill.qty == Decimal("40")
        assert update.fill.price == Decimal("512.30")
        assert isinstance(update.fill.price, Decimal)
        assert update.fill.venue_fill_id == "exec-1"

    @pytest.mark.asyncio
    async def test_a_fill_event_carries_no_status_of_its_own(self) -> None:
        """`Order.apply_fill` owns it — only the arithmetic knows whether this
        print completed the order."""
        broker, _, _ = build(FakeConnection([AUTHORIZED, LISTENING, fill_frame()]))

        events = await drain(broker, limit=1)

        assert events[0].status is None

    @pytest.mark.asyncio
    async def test_a_status_event_maps_to_ours(self) -> None:
        broker, _, _ = build(
            FakeConnection([AUTHORIZED, LISTENING, update_frame("canceled")]),
        )

        events = await drain(broker, limit=1)

        assert events[0].status is OrderStatus.CANCELLED
        assert events[0].fill is None

    @pytest.mark.asyncio
    async def test_an_unknown_event_raises_rather_than_being_ignored(self) -> None:
        """An ignored `rejected` is an order our book believes is working."""
        broker, _, _ = build(
            FakeConnection([AUTHORIZED, LISTENING, update_frame("teleported")]),
        )

        with pytest.raises(BrokerError, match="unrecognised Alpaca trade-update event"):
            async for _ in broker.stream_trade_updates():
                pass

    @pytest.mark.asyncio
    async def test_handshake_frames_are_not_events(self) -> None:
        broker, _, _ = build(
            FakeConnection([AUTHORIZED, LISTENING, LISTENING, fill_frame()]),
        )

        events = await drain(broker, limit=1)

        assert len(events) == 1
        assert isinstance(events[0], TradeUpdate)


def an_order(status: OrderStatus = OrderStatus.SUBMITTED, qty: str = "100") -> Order:
    order = Order(
        symbol="SPY",
        side=Side.BUY,
        qty=Decimal(qty),
        order_type=OrderType.MARKET,
        time_in_force=TimeInForce.DAY,
        client_order_id=CLIENT_ID,
        broker_order_id="brk-abc-123",
    )
    order.status = status
    return order


def a_fill_update(
    qty: str = "100", price: str = "512.30", *, execution_id: str | None = "exec-1"
) -> TradeUpdate:
    return TradeUpdate(
        event="fill",
        client_order_id=CLIENT_ID,
        broker_order_id="brk-abc-123",
        symbol="SPY",
        at=datetime(2024, 6, 3, 14, 30, tzinfo=UTC),
        fill=Fill(
            order_id="ignored",
            ts=datetime(2024, 6, 3, 14, 30, tzinfo=UTC),
            qty=Decimal(qty),
            price=Decimal(price),
            venue_fill_id=execution_id,
        ),
    )


class TestTheApplier:
    def test_a_fill_lands_on_the_order(self) -> None:
        order = an_order()

        assert apply_trade_update(order, a_fill_update("40")) is True
        assert order.filled_qty == Decimal("40")
        assert order.status is OrderStatus.PARTIALLY_FILLED

    def test_a_sequence_of_prints_accumulates(self) -> None:
        """The gap REST cannot close: two prints, not one running total."""
        order = an_order()

        apply_trade_update(order, a_fill_update("40", "100", execution_id="e1"))
        apply_trade_update(order, a_fill_update("60", "110", execution_id="e2"))

        assert order.status is OrderStatus.FILLED
        assert len(order.fills) == 2
        assert order.avg_fill_price == Decimal("106")

    def test_a_redelivered_fill_is_discarded(self) -> None:
        """Keyed on the venue's execution id. Without this a re-sent event
        doubles the position."""
        order = an_order()
        update = a_fill_update("40", execution_id="exec-1")

        assert apply_trade_update(order, update) is True
        assert apply_trade_update(order, update) is False
        assert order.filled_qty == Decimal("40")
        assert len(order.fills) == 1

    def test_two_identical_prints_without_ids_both_count(self) -> None:
        """Treating an id-less fill as a duplicate would silently drop real
        volume — two prints of the same size at the same price are ordinary."""
        order = an_order()

        apply_trade_update(order, a_fill_update("40", execution_id=None))
        apply_trade_update(order, a_fill_update("40", execution_id=None))

        assert order.filled_qty == Decimal("80")

    def test_a_fill_before_we_recorded_the_submit_walks_the_order_forward(self) -> None:
        """The event is the evidence: the venue plainly has the order. Refusing
        on a technicality would leave a real position unrecorded."""
        order = an_order(OrderStatus.PENDING_SUBMIT)

        assert apply_trade_update(order, a_fill_update("100")) is True
        assert order.status is OrderStatus.FILLED

    def test_a_fill_from_pending_risk_also_walks_forward(self) -> None:
        order = an_order(OrderStatus.PENDING_RISK)

        assert apply_trade_update(order, a_fill_update("100")) is True
        assert order.status is OrderStatus.FILLED

    def test_a_fill_against_a_cancelled_order_raises(self) -> None:
        """The guard `execution/state.py` asked for. Applying it resurrects a
        dead order; dropping it leaves our book disagreeing with the venue."""
        order = an_order(OrderStatus.CANCELLED)

        with pytest.raises(ReconciliationError, match="cancelled"):
            apply_trade_update(order, a_fill_update("40"))

    def test_an_overfill_names_which_side_disagrees(self) -> None:
        order = an_order(qty="100")

        with pytest.raises(ReconciliationError, match="only 100 was outstanding"):
            apply_trade_update(order, a_fill_update("140"))

    def test_a_fill_for_a_different_order_is_refused(self) -> None:
        """A caller looking orders up by the wrong key would otherwise apply a
        fill to somebody else's position."""
        order = an_order()
        wrong = TradeUpdate(
            event="fill",
            client_order_id="atp-somebody-else",
            broker_order_id="brk-999",
            symbol="SPY",
            at=datetime(2024, 6, 3, 14, 30, tzinfo=UTC),
        )

        with pytest.raises(ReconciliationError, match="refusing to fill the wrong order"):
            apply_trade_update(order, wrong)

    def test_a_status_event_moves_through_the_transition_table(self) -> None:
        order = an_order()
        cancelled = TradeUpdate(
            event="canceled",
            client_order_id=CLIENT_ID,
            broker_order_id="brk-abc-123",
            symbol="SPY",
            at=datetime(2024, 6, 3, 14, 30, tzinfo=UTC),
            status=OrderStatus.CANCELLED,
        )

        assert apply_trade_update(order, cancelled) is True
        assert order.status is OrderStatus.CANCELLED

    def test_a_pushed_rejection_names_the_venue_that_refused(self) -> None:
        """The third of the three ways a venue refusal reaches an order.

        The other two are the router's, on submission and on acknowledgement,
        and both take the name from the broker they submitted to. This one had
        no venue in reach: the runner consumes this stream and reaches a broker
        only through the router (rule §1.5), so the name travels on the event
        rather than being handed to the applier. Without it, this would be the
        one refusal path that stored a refusal without saying who refused.
        """
        order = an_order()
        rejected = TradeUpdate(
            event="rejected",
            client_order_id=CLIENT_ID,
            broker_order_id="brk-abc-123",
            symbol="SPY",
            at=datetime(2024, 6, 3, 14, 30, tzinfo=UTC),
            status=OrderStatus.REJECTED,
            reason="insufficient buying power",
            broker="alpaca-live",
        )

        assert apply_trade_update(order, rejected) is True
        assert order.status is OrderStatus.REJECTED
        assert order.rejected_by == "alpaca-live"
        assert order.reject_reason == "insufficient buying power"

    def test_a_pushed_cancel_records_no_refuser(self) -> None:
        """A cancel is not a refusal. Stamping the venue's name on one would
        put a refuser on an order nothing refused."""
        order = an_order()
        cancelled = TradeUpdate(
            event="canceled",
            client_order_id=CLIENT_ID,
            broker_order_id="brk-abc-123",
            symbol="SPY",
            at=datetime(2024, 6, 3, 14, 30, tzinfo=UTC),
            status=OrderStatus.CANCELLED,
            broker="alpaca-live",
        )

        assert apply_trade_update(order, cancelled) is True
        assert order.rejected_by is None

    def test_a_replayed_status_against_a_terminal_order_is_discarded(self) -> None:
        """Ordinary after a reconnect, and must not overwrite a status the
        order has legitimately moved past."""
        order = an_order(OrderStatus.FILLED)
        submitted = TradeUpdate(
            event="new",
            client_order_id=CLIENT_ID,
            broker_order_id="brk-abc-123",
            symbol="SPY",
            at=datetime(2024, 6, 3, 14, 30, tzinfo=UTC),
            status=OrderStatus.SUBMITTED,
        )

        assert apply_trade_update(order, submitted) is False
        assert order.status is OrderStatus.FILLED

    def test_a_cancel_rejection_changes_nothing(self) -> None:
        """It is a real event — silently dropping it would leave a caller
        believing its cancel took effect — but the order is untouched."""
        order = an_order()
        rejected = TradeUpdate(
            event="order_cancel_rejected",
            client_order_id=CLIENT_ID,
            broker_order_id="brk-abc-123",
            symbol="SPY",
            at=datetime(2024, 6, 3, 14, 30, tzinfo=UTC),
            status=None,
        )

        assert apply_trade_update(order, rejected) is False
        assert order.status is OrderStatus.SUBMITTED

    def test_the_broker_order_id_is_recorded_when_first_seen(self) -> None:
        order = an_order()
        order.broker_order_id = None

        apply_trade_update(order, a_fill_update("40"))

        assert order.broker_order_id == "brk-abc-123"
