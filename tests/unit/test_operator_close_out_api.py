"""The operator's close-out endpoints, over ASGI.

Three handlers that had been `NotImplementedError` since the routers were
written, and one carve-out between them that is the reason this file exists as
its own suite:

- `DELETE /orders/{id}` and `POST /orders/cancel-all` **withdraw** intent. They
  consult no risk rule, because there is no order to judge.
- `POST /positions/{symbol}/close` **places** one, so it goes down the same
  nine-rule chain a strategy's order takes (ADR 0005) and can be refused.
- `POST /risk/flatten-all` is the single path in the platform that reaches a
  venue *around* that chain, which is exactly why ADR 0005 names it, and why
  most of what follows is about the proofs and the failure reporting rather
  than about the happy path.

The theme, as in `test_risk_api.py`: **a close-out must never overstate what it
achieved.** A refused flatten that reads as "closed", or a partial liquidation
that reads as success, is worse than an error — the operator stops looking.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any

import httpx
import pytest
from pydantic import SecretStr

from atp_api.auth import Scope, Session, hash_password
from atp_api.deps import (
    get_audit_sink,
    get_broker,
    get_calendar,
    get_clock,
    get_current_session,
    get_kill_switch,
    get_portfolio_repository,
    get_quote_cache,
    get_worker_config_repository,
)
from atp_api.main import create_app
from atp_core.audit.ports import Action
from atp_core.clock import SimulatedClock, TradingCalendar
from atp_core.config import Settings, get_settings
from atp_core.domain import Order, OrderType, Portfolio, Quote, Side
from atp_core.risk.killswitch import HaltReason, HaltScope
from tests.fakes import (
    FakeBroker,
    FakeKillSwitch,
    FakePortfolioRepository,
    FakeWorkerConfigRepository,
    RecordingAuditSink,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from fastapi import FastAPI

CANCEL_ALL = "/api/v1/orders/cancel-all"
CLOSE = "/api/v1/positions/SPY/close"
FLATTEN = "/api/v1/risk/flatten-all"

PASSWORD = "a-perfectly-ordinary-password"
CONFIRM = "FLATTEN ALL POSITIONS"

#: Mid-session on an ordinary NYSE day, so `TradingHoursRule` allows and the
#: cases that are about *other* rules are not silently all the same case.
NOW = datetime(2024, 6, 3, 15, 0, tzinfo=UTC)


def settings_with_password(password: str = PASSWORD) -> Settings:
    """`api_user` matches the session the `app` fixture installs — `authenticate`
    compares the two, so a mismatch would make every step-up a 403 for the wrong
    reason and turn the happy paths into duplicates of the refusals."""
    return Settings(
        ATP_RUN_MODE="paper",
        api_user="test-operator",
        api_secret_key=SecretStr("k" * 64),
        api_password_hash=SecretStr(hash_password(password)),
        alpaca_api_key=SecretStr("key"),
        alpaca_api_secret=SecretStr("secret"),
        _env_file=None,
    )


class FakeQuoteCache:
    """The ingestor's side of Redis, in memory."""

    def __init__(self) -> None:
        self.quotes: dict[str, Quote] = {}

    def seed(self, symbol: str, price: float, *, at: datetime = NOW) -> None:
        self.quotes[symbol] = Quote(
            symbol=symbol,
            ts=at,
            bid=Decimal(str(price)) - Decimal("0.01"),
            ask=Decimal(str(price)) + Decimal("0.01"),
        )

    async def set_quote(self, quote: Quote) -> None:
        self.quotes[quote.symbol] = quote

    async def get_quote(self, symbol: str) -> Quote | None:
        return self.quotes.get(symbol)

    async def get_quotes(self, symbols: list[str]) -> dict[str, Quote]:
        return {s: self.quotes[s] for s in symbols if s in self.quotes}


def stored_book(**holdings: float) -> Portfolio:
    """A book as `PortfolioRepository.latest` would return it — **unmarked**.

    Marks are not stored with it and are not the repository's business; they are
    put on by `execution.marked_book` from the quote cache, which is the seam
    the unmarked-book case below is about.
    """
    portfolio = Portfolio(cash=Decimal("50000"), starting_equity=Decimal("100000"))
    for symbol, qty in holdings.items():
        position = portfolio.position(symbol)
        position.qty = Decimal(str(qty))
        position.avg_entry_price = Decimal("100")
    return portfolio


def working_order(broker: FakeBroker, symbol: str = "SPY", qty: int = 10) -> Order:
    """Put one live order at the venue and hand it back."""
    order = Order(
        symbol=symbol,
        side=Side.BUY,
        qty=Decimal(qty),
        order_type=OrderType.LIMIT,
        limit_price=Decimal("99"),
    )
    return broker._accept(order)


@pytest.fixture
def broker() -> FakeBroker:
    return FakeBroker()


@pytest.fixture
def kill_switch() -> FakeKillSwitch:
    return FakeKillSwitch()


@pytest.fixture
def audit() -> RecordingAuditSink:
    return RecordingAuditSink()


@pytest.fixture
def portfolio_repo() -> FakePortfolioRepository:
    return FakePortfolioRepository()


@pytest.fixture
def quotes() -> FakeQuoteCache:
    cache = FakeQuoteCache()
    cache.seed("SPY", 110.0)
    return cache


@pytest.fixture
def app(
    broker: FakeBroker,
    kill_switch: FakeKillSwitch,
    audit: RecordingAuditSink,
    portfolio_repo: FakePortfolioRepository,
    quotes: FakeQuoteCache,
) -> FastAPI:
    application = create_app()
    application.dependency_overrides[get_settings] = settings_with_password
    application.dependency_overrides[get_clock] = lambda: SimulatedClock(NOW)
    application.dependency_overrides[get_calendar] = lambda: TradingCalendar()
    application.dependency_overrides[get_broker] = lambda: broker
    application.dependency_overrides[get_kill_switch] = lambda: kill_switch
    application.dependency_overrides[get_audit_sink] = lambda: audit
    application.dependency_overrides[get_portfolio_repository] = lambda: portfolio_repo
    application.dependency_overrides[get_quote_cache] = lambda: quotes
    # The risk ceilings are a stored row since ADR 0025, so anything that
    # validates an order or reads a limit reaches this repository. Empty
    # means nothing has been saved, which is `DEFAULT_RISK_LIMITS` — the same
    # numbers `.env` used to ship, so the expectations below are unchanged.
    application.dependency_overrides[get_worker_config_repository] = FakeWorkerConfigRepository
    application.dependency_overrides[get_current_session] = lambda: Session(
        "test-operator", Scope.FULL
    )
    return application


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as http:
        yield http


class TestCancellingOneOrder:
    async def test_the_venues_id_cancels_it(
        self, client: httpx.AsyncClient, broker: FakeBroker, audit: RecordingAuditSink
    ) -> None:
        order = working_order(broker)
        assert order.broker_order_id is not None

        response = await client.delete(f"/api/v1/orders/{order.broker_order_id}")

        assert response.status_code == 200
        assert response.json()["cancelled"] is True
        assert broker.cancelled == [order.broker_order_id]
        assert audit.entries[-1].action == Action.ORDER_CANCELLED

    async def test_our_own_id_cancels_it_too(
        self, client: httpx.AsyncClient, broker: FakeBroker
    ) -> None:
        """The order table shows `client_order_id` and an Alpaca screen shows
        theirs. Refusing whichever one the operator is holding is a way to make
        somebody retype an id during an incident."""
        order = working_order(broker)

        response = await client.delete(f"/api/v1/orders/{order.client_order_id}")

        assert response.status_code == 200
        assert broker.cancelled == [order.broker_order_id]

    async def test_nothing_working_is_a_404_not_an_error(
        self, client: httpx.AsyncClient, audit: RecordingAuditSink
    ) -> None:
        """ "It filled while you were deciding" and "no such order" are the same
        answer to "cancel this": there is nothing to cancel. Neither is worth an
        audit row, because nothing happened."""
        response = await client.delete("/api/v1/orders/brk-nonexistent")

        assert response.status_code == 404
        assert "filled" in response.json()["detail"]
        assert audit.entries == []

    async def test_a_venue_refusal_says_the_order_is_still_working(
        self, client: httpx.AsyncClient, broker: FakeBroker, audit: RecordingAuditSink
    ) -> None:
        """The failure that must not read as success. An operator told
        "cancelled" about an order that is still live stops watching it."""
        order = working_order(broker)
        assert order.broker_order_id is not None
        broker.cancel_refuses = {order.broker_order_id}

        response = await client.delete(f"/api/v1/orders/{order.broker_order_id}")

        assert response.status_code == 502
        assert "still working" in response.json()["detail"]
        assert audit.entries == []

    async def test_an_unreadable_venue_is_not_a_missing_order(
        self, client: httpx.AsyncClient, broker: FakeBroker, audit: RecordingAuditSink
    ) -> None:
        """A 404 would say "there is no such order", which is a claim about the
        venue's state — and the venue is exactly what could not be read. The
        two failures are one line apart in the handler and mean opposite
        things."""
        order = working_order(broker)
        broker.reads_fail = True

        response = await client.delete(f"/api/v1/orders/{order.broker_order_id}")

        assert response.status_code == 502
        assert "unknown" in response.json()["detail"]
        assert broker.cancelled == []
        assert audit.entries == []


class TestCancellingEverything:
    async def test_it_cancels_every_working_order(
        self, client: httpx.AsyncClient, broker: FakeBroker, audit: RecordingAuditSink
    ) -> None:
        working_order(broker, "SPY")
        working_order(broker, "QQQ")

        response = await client.post(CANCEL_ALL, json={})

        assert response.status_code == 200
        assert response.json() == {"cancelled": 2}
        assert len(broker.cancelled) == 2
        assert audit.entries[-1].detail["scope"] == "all"

    async def test_a_symbol_scopes_it(
        self, client: httpx.AsyncClient, broker: FakeBroker, audit: RecordingAuditSink
    ) -> None:
        spy = working_order(broker, "SPY")
        working_order(broker, "QQQ")

        response = await client.post(f"{CANCEL_ALL}?symbol=SPY", json={})

        assert response.status_code == 200
        assert response.json() == {"cancelled": 1}
        assert broker.cancelled == [spy.broker_order_id]
        assert audit.entries[-1].detail["scope"] == "SPY"

    async def test_it_does_not_close_positions(
        self, client: httpx.AsyncClient, broker: FakeBroker
    ) -> None:
        """Cancelling is not flattening, and the fake records the difference.
        A cancel-all that also closed the book would be `/risk/flatten-all`
        without any of its proofs."""
        working_order(broker)

        await client.post(CANCEL_ALL, json={})

        assert broker.close_calls == []

    async def test_a_partial_failure_is_a_502_that_says_so(
        self, client: httpx.AsyncClient, broker: FakeBroker, audit: RecordingAuditSink
    ) -> None:
        """`cancel_all` attempts every order before raising, so some are gone.
        A count would imply the rest are still working; they may not be."""
        working_order(broker)
        broker.reads_fail = True

        response = await client.post(CANCEL_ALL, json={})

        assert response.status_code == 502
        assert "re-read" in response.json()["detail"]
        assert audit.entries == []


class TestClosingOnePosition:
    async def test_it_submits_through_the_chain_not_around_it(
        self,
        client: httpx.AsyncClient,
        broker: FakeBroker,
        portfolio_repo: FakePortfolioRepository,
        audit: RecordingAuditSink,
    ) -> None:
        portfolio_repo.stored = stored_book(SPY=100)

        response = await client.post(CLOSE, json={})

        assert response.status_code == 200
        body = response.json()
        assert body["submitted"] is True
        assert body["qty"] == "100"
        assert body["refused_by"] is None
        # ADR 0005's bypass, asserted rather than assumed: the close reached the
        # venue as a submitted market order, not as `close_position`.
        assert broker.close_calls == []
        assert broker.submit_calls
        assert audit.entries[-1].action == Action.POSITION_CLOSED
        assert audit.entries[-1].detail["submitted"] is True

    async def test_a_halt_refuses_it_and_the_reply_names_the_rule(
        self,
        client: httpx.AsyncClient,
        broker: FakeBroker,
        kill_switch: FakeKillSwitch,
        portfolio_repo: FakePortfolioRepository,
        audit: RecordingAuditSink,
    ) -> None:
        """A 200 with `submitted: false`, not an HTTP error.

        Collapsing "the platform considered this and said no" into the same
        shape as "the symbol was misspelt" would hide the one of the two the
        operator has to act on. Six of the nine rules can refuse an exit; this
        pins the one an operator is most likely to have caused themselves.
        """
        portfolio_repo.stored = stored_book(SPY=100)
        kill_switch.engage(HaltScope.GLOBAL, HaltReason.MANUAL, "test-operator")

        response = await client.post(CLOSE, json={})

        assert response.status_code == 200
        body = response.json()
        assert body["submitted"] is False
        assert body["refused_by"] == "kill_switch"
        assert broker.submit_calls == []
        assert audit.entries[-1].detail["submitted"] is False

    async def test_an_unpriced_book_is_refused_by_name_not_valued_at_zero(
        self,
        client: httpx.AsyncClient,
        broker: FakeBroker,
        portfolio_repo: FakePortfolioRepository,
        quotes: FakeQuoteCache,
    ) -> None:
        """No cached quote means the position stays unmarked, and an unmarked
        holding makes `Portfolio.equity` too small — so every percentage limit
        computed from it approves what it should refuse. The chain refuses
        outright instead, naming the symbol it could not price."""
        portfolio_repo.stored = stored_book(SPY=100)
        quotes.quotes.clear()

        response = await client.post(CLOSE, json={})

        assert response.status_code == 200
        body = response.json()
        assert body["submitted"] is False
        assert "SPY" in body["reason"]
        assert broker.submit_calls == []

    async def test_no_stored_book_is_not_an_empty_one(
        self, client: httpx.AsyncClient, portfolio_repo: FakePortfolioRepository
    ) -> None:
        """`latest` returning None means nothing has ever traded. Answering
        "you hold nothing" would be a different, wrong, statement."""
        portfolio_repo.stored = None

        response = await client.post(CLOSE, json={})

        assert response.status_code == 404
        assert "has ever been stored" in response.json()["detail"]

    async def test_a_flat_symbol_is_a_404(
        self, client: httpx.AsyncClient, portfolio_repo: FakePortfolioRepository
    ) -> None:
        portfolio_repo.stored = stored_book(QQQ=50)

        response = await client.post(CLOSE, json={})

        assert response.status_code == 404
        assert "no open SPY position" in response.json()["detail"]


class TestFlattenAll:
    async def test_it_closes_the_book_and_records_what_it_closed(
        self, client: httpx.AsyncClient, broker: FakeBroker, audit: RecordingAuditSink
    ) -> None:
        broker.positions.update(stored_book(SPY=100, QQQ=50).positions)
        working_order(broker)

        response = await client.post(FLATTEN, json={"confirm": CONFIRM, "password": PASSWORD})

        assert response.status_code == 200
        body = response.json()
        assert body["flattened"] is True
        assert body["positions_closed"] == 2
        assert body["symbols"] == ["QQQ", "SPY"]
        # The venue cancels resting orders as part of the same call: a stop left
        # working against a position that no longer exists opens the other side.
        assert broker.cancelled
        entry = audit.entries[-1]
        assert entry.action == Action.FLATTEN_ALL
        assert entry.detail["succeeded"] is True
        assert entry.detail["symbols"] == ["QQQ", "SPY"]

    async def test_the_wrong_phrase_touches_nothing(
        self, client: httpx.AsyncClient, broker: FakeBroker, audit: RecordingAuditSink
    ) -> None:
        """The phrase is not a password — it is proof the caller has read what
        this does. A near miss is still a miss."""
        broker.positions.update(stored_book(SPY=100).positions)

        response = await client.post(
            FLATTEN, json={"confirm": "flatten all positions", "password": PASSWORD}
        )

        assert response.status_code == 400
        assert "untouched" in response.json()["detail"]
        assert broker.close_calls == []
        assert not [e for e in audit.entries if e.action == Action.FLATTEN_ALL]

    async def test_the_wrong_password_touches_nothing(
        self, client: httpx.AsyncClient, broker: FakeBroker, audit: RecordingAuditSink
    ) -> None:
        """Both proofs, not either. A copied session cookie satisfies neither."""
        broker.positions.update(stored_book(SPY=100).positions)

        response = await client.post(FLATTEN, json={"confirm": CONFIRM, "password": "wrong"})

        assert response.status_code == 403
        assert broker.close_calls == []
        assert audit.entries[-1].action == Action.FORBIDDEN

    async def test_a_partial_flatten_is_a_502_never_a_success(
        self, client: httpx.AsyncClient, broker: FakeBroker, audit: RecordingAuditSink
    ) -> None:
        """Alpaca answers 207 with a per-symbol status array, so a partial
        failure looks like success at the HTTP level. The worst outcome this
        call has is silently leaving one position open."""
        broker.positions.update(stored_book(SPY=100, QQQ=50).positions)
        broker.flatten_fails = ["QQQ"]

        response = await client.post(FLATTEN, json={"confirm": CONFIRM, "password": PASSWORD})

        assert response.status_code == 502
        assert "may still be open" in response.json()["detail"]
        # Recorded anyway: an attempted flatten that failed is exactly the event
        # an incident review needs, and it is the one a caller cannot see.
        entry = audit.entries[-1]
        assert entry.action == Action.FLATTEN_ALL
        assert entry.detail["succeeded"] is False

    async def test_it_reports_whether_the_platform_was_halted(
        self, client: httpx.AsyncClient, broker: FakeBroker, kill_switch: FakeKillSwitch
    ) -> None:
        """ADR 0005 describes this as a human acting around a platform they have
        already halted. Not halting is not refused — the moment this exists for
        is the moment an extra step is most expensive — but it is reported, so
        an operator learns the runner may re-enter within a tick."""
        broker.positions.update(stored_book(SPY=100).positions)

        unhalted = await client.post(FLATTEN, json={"confirm": CONFIRM, "password": PASSWORD})
        assert unhalted.json()["was_halted"] is False

        broker.positions.update(stored_book(SPY=100).positions)
        kill_switch.engage(HaltScope.GLOBAL, HaltReason.MANUAL, "test-operator")

        halted = await client.post(FLATTEN, json={"confirm": CONFIRM, "password": PASSWORD})
        assert halted.json()["was_halted"] is True

    async def test_a_read_only_session_may_not_call_it(self, app: Any) -> None:
        """`/risk/halt` is the one mutating route a read-only session may call
        (ADR 0009). Liquidating the book is the furthest thing from it."""
        app.dependency_overrides[get_current_session] = lambda: Session("test-operator", Scope.READ)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as http:
            response = await http.post(FLATTEN, json={"confirm": CONFIRM, "password": PASSWORD})

        assert response.status_code == 403
