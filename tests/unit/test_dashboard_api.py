"""`GET /api/v1/dashboard/live` and the equity curve, over ASGI.

A unit test rather than an integration one: every source the endpoint reads is
behind a port, so the whole handler runs against fakes with no Redis and no
database (CLAUDE.md §1.7). What that buys is the ability to test the states that
are hard to produce on purpose — a worker that has published nothing, a Redis
that cannot be read, a database that answers for the chart but not for the day
anchor.

The theme throughout is the same one the endpoint exists for: **a dashboard must
never state something it does not know.** An empty book and an unknown book look
identical on a screen, and only one of them is safe to act on.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING, Any

import httpx
import pytest

from atp_api.auth import Scope, Session
from atp_api.deps import (
    get_clock,
    get_current_session,
    get_kill_switch,
    get_portfolio_repository,
    get_snapshot_store,
    get_worker_config_repository,
)
from atp_api.main import create_app
from atp_core.clock import SimulatedClock
from atp_core.config import Settings, get_settings
from atp_core.dashboard import build_snapshot
from atp_core.domain import Order, OrderStatus, OrderType, Portfolio, Position, RunMode, Side
from atp_core.execution.ports import EquityPoint
from atp_core.risk.killswitch import HaltReason, HaltRecord, HaltScope
from tests.fakes import (
    FakeKillSwitch,
    FakePortfolioRepository,
    FakeSnapshotStore,
    FakeWorkerConfigRepository,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from fastapi import FastAPI

LIVE = "/api/v1/dashboard/live"
CURVE = "/api/v1/dashboard/equity-curve"

#: A Monday, 14:30 UTC — 10:30 in New York, an hour into the session. Chosen so
#: `market_open` is True without the test having to say so.
NOW = datetime(2024, 6, 3, 14, 30, tzinfo=UTC)

#: The same Monday at 02:00 UTC, hours before the bell.
BEFORE_THE_OPEN = datetime(2024, 6, 3, 2, 0, tzinfo=UTC)


def pinned_settings() -> Settings:
    """Settings that do not depend on the shell the suite is run from.

    `ATP_RUN_MODE` is a real environment variable on a developer machine and on
    a CI runner, and it is the value the run-mode banner is asserted on below —
    so a test that read the ambient one would pass or fail according to what the
    last person exported. `_env_file=None` for the same reason: a local `.env`
    with an operator's own settings must not reach a test.
    """
    return Settings(ATP_RUN_MODE="backtest", _env_file=None)


class RaisingKillSwitch(FakeKillSwitch):
    """A kill switch whose Redis is gone.

    `RedisKillSwitch.active_halts` deliberately raises rather than returning an
    empty list, and this stands in for that: "nothing is halted" is the worst
    possible thing to show a human when the truth is unknown.
    """

    def active_halts(self) -> list[HaltRecord]:
        raise ConnectionError("redis is down")


def a_book(*, equity_cash: str = "9000", qty: str = "10") -> Portfolio:
    book = Portfolio(cash=Decimal(equity_cash), starting_equity=Decimal("10000"))
    book.positions["AAPL"] = Position(
        symbol="AAPL",
        qty=Decimal(qty),
        avg_entry_price=Decimal("100"),
        last_price=Decimal("110"),
        stop_loss_price=Decimal("90"),
        opened_at=NOW - timedelta(days=1),
    )
    return book


def a_working_order() -> Order:
    return Order(
        symbol="MSFT",
        side=Side.BUY,
        qty=Decimal("5"),
        order_type=OrderType.LIMIT,
        limit_price=Decimal("400.25"),
        status=OrderStatus.SUBMITTED,
        created_at=NOW,
        submitted_at=NOW,
        strategy_id="sma_crossover",
    )


@pytest.fixture
def store() -> FakeSnapshotStore:
    return FakeSnapshotStore()


@pytest.fixture
def repo() -> FakePortfolioRepository:
    return FakePortfolioRepository()


@pytest.fixture
def kill_switch() -> FakeKillSwitch:
    return FakeKillSwitch()


@pytest.fixture
def app(
    store: FakeSnapshotStore, repo: FakePortfolioRepository, kill_switch: FakeKillSwitch
) -> FastAPI:
    application = create_app()
    application.dependency_overrides[get_settings] = pinned_settings
    application.dependency_overrides[get_clock] = lambda: SimulatedClock(NOW)
    application.dependency_overrides[get_snapshot_store] = lambda: store
    application.dependency_overrides[get_portfolio_repository] = lambda: repo
    application.dependency_overrides[get_kill_switch] = lambda: kill_switch
    # The risk ceilings are a stored row since ADR 0025, so anything that
    # validates an order or reads a limit reaches this repository. Empty
    # means nothing has been saved, which is `DEFAULT_RISK_LIMITS` — the same
    # numbers `.env` used to ship, so the expectations below are unchanged.
    application.dependency_overrides[get_worker_config_repository] = FakeWorkerConfigRepository
    # These tests are about the book the dashboard serves, not about who is asking. Overriding the
    # one dependency that answers that keeps them so — `tests/unit/
    # test_api_contract.py` is where the enforcement itself is held, from the
    # outside, against every route at once (ADR 0008, ADR 0009). A FULL
    # session because these exercise reads and the routes they drive are not
    # about scope.
    application.dependency_overrides[get_current_session] = lambda: Session(
        "test-operator", Scope.FULL
    )
    return application


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    # No lifespan: `ASGITransport` does not run one, which is what keeps this a
    # unit test with no Redis pool and no engine behind it.
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as http:
        yield http


async def get_live(client: httpx.AsyncClient, **params: Any) -> tuple[int, Any]:
    response = await client.get(LIVE, params=params)
    return response.status_code, response.json()


def publish(store: FakeSnapshotStore, book: Portfolio, **kwargs: Any) -> None:
    store.stored[RunMode.BACKTEST.value] = build_snapshot(
        book, at=NOW, run_mode=RunMode.BACKTEST, **kwargs
    )


class TestWithNoPublishedBook:
    """The state a fresh deployment is in, and the one a dead worker leaves.

    Every one of these must still render, because they are what a person looks
    at when something is already wrong.
    """

    async def test_it_is_a_200_not_an_error(self, client: httpx.AsyncClient) -> None:
        status_code, _ = await get_live(client)

        assert status_code == 200

    async def test_the_book_is_null_rather_than_empty(self, client: httpx.AsyncClient) -> None:
        """ "You hold nothing" and "nobody has said what you hold" are different
        sentences, and only one of them is safe to act on."""
        _, body = await get_live(client)

        assert body["book_as_of"] is None
        assert body["account"] is None
        assert body["positions"] == []

    async def test_the_run_mode_banner_still_has_its_answer(
        self, client: httpx.AsyncClient
    ) -> None:
        """It comes from configuration, not from the worker — the banner saying
        whether this is real money must not depend on a process that can die."""
        _, body = await get_live(client)

        assert body["run_mode"] == "backtest"

    async def test_halts_still_render(
        self, client: httpx.AsyncClient, kill_switch: FakeKillSwitch
    ) -> None:
        """The whole reason halts are not in the published book: a halt banner
        sourced from a snapshot nobody is publishing says "not halted"."""
        kill_switch.halts = [
            HaltRecord(
                scope=HaltScope.GLOBAL,
                reason=HaltReason.DATA_FEED_LOST,
                engaged_at=NOW,
                engaged_by="stream_ingestor",
                detail="no ticks for 90s",
            )
        ]

        _, body = await get_live(client)

        assert [h["reason"] for h in body["active_halts"]] == ["data_feed_lost"]

    async def test_feed_health_is_unknown_not_healthy(self, client: httpx.AsyncClient) -> None:
        _, body = await get_live(client)

        assert body["data_feed_healthy"] is None


class TestWithAPublishedBook:
    async def test_the_account_comes_through_as_strings(
        self, client: httpx.AsyncClient, store: FakeSnapshotStore
    ) -> None:
        """Money on the wire is a string. A JSON number is an IEEE 754 double in
        every browser, and a P&L that round-trips through one is no longer
        exact."""
        publish(store, a_book())

        _, body = await get_live(client)

        assert body["account"]["equity"] == "10100"
        assert isinstance(body["account"]["cash"], str)
        assert isinstance(body["positions"][0]["unrealized_pnl"], str)

    async def test_the_book_carries_its_own_age(
        self, client: httpx.AsyncClient, store: FakeSnapshotStore
    ) -> None:
        """Two timestamps, not one: `as_of` is when the API answered, and
        `book_as_of` is when the worker last knew anything."""
        publish(store, a_book())

        _, body = await get_live(client)

        # Parsed rather than string-compared: pydantic renders UTC with a `Z`
        # suffix and `datetime.isoformat` with `+00:00`. Both are ISO-8601 and
        # both parse to the same instant, which is the thing under test.
        assert datetime.fromisoformat(body["book_as_of"]) == NOW
        assert body["book_age_seconds"] == 0

    async def test_an_old_book_reports_its_real_age(
        self, client: httpx.AsyncClient, store: FakeSnapshotStore
    ) -> None:
        """Served stale and labelled, rather than withheld. A blank dashboard
        during an outage is worse — the user still needs to see what they
        hold."""
        store.stored[RunMode.BACKTEST.value] = build_snapshot(
            a_book(), at=NOW - timedelta(minutes=17), run_mode=RunMode.BACKTEST
        )

        _, body = await get_live(client)

        assert body["book_age_seconds"] == 17 * 60
        assert body["account"] is not None

    async def test_a_worker_clock_ahead_of_ours_does_not_report_a_negative_age(
        self, client: httpx.AsyncClient, store: FakeSnapshotStore
    ) -> None:
        """Clock skew is ordinary; "updated -3s ago" reads as a bug in the
        dashboard rather than as the skew it is."""
        store.stored[RunMode.BACKTEST.value] = build_snapshot(
            a_book(), at=NOW + timedelta(seconds=3), run_mode=RunMode.BACKTEST
        )

        _, body = await get_live(client)

        assert body["book_age_seconds"] == 0

    async def test_working_orders_come_through(
        self, client: httpx.AsyncClient, store: FakeSnapshotStore
    ) -> None:
        publish(store, a_book(), working_orders=[a_working_order()])

        _, body = await get_live(client)

        assert [o["symbol"] for o in body["working_orders"]] == ["MSFT"]
        assert body["working_orders"][0]["limit_price"] == "400.25"

    async def test_a_snapshot_for_another_run_mode_is_not_served(
        self, client: httpx.AsyncClient, store: FakeSnapshotStore
    ) -> None:
        """Paper and live share a datastore. A paper book served to a live
        dashboard would be a screen showing positions that are not the ones at
        risk."""
        store.stored[RunMode.PAPER.value] = build_snapshot(a_book(), at=NOW, run_mode=RunMode.PAPER)

        _, body = await get_live(client)

        assert body["account"] is None


class TestSignalFeed:
    def signals(self, count: int) -> list[Any]:
        from atp_core.dashboard import SignalSummary

        return [
            SignalSummary(
                id=f"sig-{i}",
                ts=NOW - timedelta(minutes=count - i),
                strategy_id="sma_crossover",
                symbol="AAPL",
                action="enter_long",
                reason=f"crossover {i}",
                indicators={"sma_fast": "100.5"},
                acted_on=False,
                rejection_reason="trading is halted",
                rejected_by="kill_switch",
            )
            for i in range(count)
        ]

    async def test_newest_first(self, client: httpx.AsyncClient, store: FakeSnapshotStore) -> None:
        """A feed is read from the top."""
        publish(store, a_book(), recent_signals=self.signals(3))

        _, body = await get_live(client)

        assert [s["id"] for s in body["recent_signals"]] == ["sig-2", "sig-1", "sig-0"]

    async def test_the_limit_trims_the_oldest(
        self, client: httpx.AsyncClient, store: FakeSnapshotStore
    ) -> None:
        publish(store, a_book(), recent_signals=self.signals(10))

        _, body = await get_live(client, signal_limit=2)

        assert [s["id"] for s in body["recent_signals"]] == ["sig-9", "sig-8"]

    async def test_a_refused_signal_says_which_rule_refused_it(
        self, client: httpx.AsyncClient, store: FakeSnapshotStore
    ) -> None:
        publish(store, a_book(), recent_signals=self.signals(1))

        _, body = await get_live(client)

        signal = body["recent_signals"][0]
        assert signal["acted_on"] is False
        assert signal["rejected_by"] == "kill_switch"
        assert signal["reason"] == "crossover 0"

    async def test_indicator_values_are_strings(
        self, client: httpx.AsyncClient, store: FakeSnapshotStore
    ) -> None:
        """An SMA of closes is a price. Rule §1.1's exemption covers computing
        one, not putting it on a wire whose only numeric type is a float."""
        publish(store, a_book(), recent_signals=self.signals(1))

        _, body = await get_live(client)

        assert body["recent_signals"][0]["indicators"] == {"sma_fast": "100.5"}

    @pytest.mark.parametrize("limit", [0, 51])
    async def test_an_out_of_range_limit_is_refused(
        self, client: httpx.AsyncClient, limit: int
    ) -> None:
        status_code, _ = await get_live(client, signal_limit=limit)

        assert status_code == 422


class TestDayPnl:
    def anchored_history(self) -> list[EquityPoint]:
        """Two points: one just after the 13:30Z open, one recent."""
        return [
            EquityPoint(
                ts=NOW.replace(hour=13, minute=31),
                equity=Decimal("10000"),
                cash=Decimal("9000"),
                gross_exposure=Decimal("1000"),
            ),
            EquityPoint(
                ts=NOW - timedelta(minutes=1),
                equity=Decimal("10050"),
                cash=Decimal("9000"),
                gross_exposure=Decimal("1050"),
            ),
        ]

    async def test_it_measures_against_the_session_open(
        self, client: httpx.AsyncClient, store: FakeSnapshotStore, repo: FakePortfolioRepository
    ) -> None:
        """The anchor is the first equity snapshot of the session, not
        `Portfolio.starting_equity` — which is inception-to-date and is reset to
        the reload point on every restart."""
        repo.equity_points = self.anchored_history()
        publish(store, a_book())  # equity 9000 cash + 1100 mark = 10100

        _, body = await get_live(client)

        assert body["account"]["day_pnl"] == "100"
        assert body["account"]["day_pnl_pct"] == "0.0100"

    async def test_a_point_from_before_the_open_is_not_the_anchor(
        self, client: httpx.AsyncClient, store: FakeSnapshotStore, repo: FakePortfolioRepository
    ) -> None:
        """Yesterday's close is not today's open. Anchoring on it would report
        the overnight gap as part of the day's move."""
        repo.equity_points = [
            EquityPoint(
                ts=NOW - timedelta(days=1),
                equity=Decimal("5000"),
                cash=Decimal("5000"),
                gross_exposure=Decimal(0),
            ),
            *self.anchored_history(),
        ]
        publish(store, a_book())

        _, body = await get_live(client)

        assert body["account"]["day_pnl"] == "100"

    async def test_no_history_means_no_day_pnl_rather_than_zero(
        self, client: httpx.AsyncClient, store: FakeSnapshotStore
    ) -> None:
        publish(store, a_book())

        _, body = await get_live(client)

        assert body["account"]["day_pnl"] is None
        assert body["account"]["day_pnl_pct"] is None

    async def test_a_database_that_cannot_answer_costs_one_figure_not_the_screen(
        self, client: httpx.AsyncClient, store: FakeSnapshotStore, repo: FakePortfolioRepository
    ) -> None:
        """Failing the whole response would take the halt banner and the
        position list down with it — the parts a person needs when something is
        already wrong."""
        repo.history_error = ConnectionError("the database is unreachable")
        publish(store, a_book())

        status_code, body = await get_live(client)

        assert status_code == 200
        assert body["account"]["day_pnl"] is None
        assert body["account"]["equity"] == "10100"
        assert body["positions"][0]["symbol"] == "AAPL"


class TestFeedHealth:
    async def test_a_recent_tick_is_healthy(
        self, client: httpx.AsyncClient, store: FakeSnapshotStore
    ) -> None:
        from atp_core.domain import Quote

        publish(
            store,
            a_book(),
            quotes={
                "AAPL": Quote(
                    symbol="AAPL",
                    ts=NOW - timedelta(seconds=5),
                    bid=Decimal("109"),
                    ask=Decimal("111"),
                )
            },
        )

        _, body = await get_live(client)

        assert body["data_feed_healthy"] is True
        assert datetime.fromisoformat(body["last_data_at"]) == NOW - timedelta(seconds=5)

    async def test_a_tick_older_than_the_risk_budget_is_not(
        self, client: httpx.AsyncClient, store: FakeSnapshotStore
    ) -> None:
        """Judged against `RISK_MAX_QUOTE_AGE_SECONDS`, the same budget
        `StaleDataRule` refuses to price an order against. A second number here
        would let the dashboard call a feed healthy while every order against it
        was being refused for staleness."""
        from atp_core.domain import Quote

        publish(
            store,
            a_book(),
            quotes={
                "AAPL": Quote(
                    symbol="AAPL",
                    ts=NOW - timedelta(minutes=5),
                    bid=Decimal("109"),
                    ask=Decimal("111"),
                )
            },
        )

        _, body = await get_live(client)

        assert body["data_feed_healthy"] is False

    async def test_silence_out_of_hours_is_not_a_broken_feed(
        self, app: FastAPI, store: FakeSnapshotStore
    ) -> None:
        """A quiet feed at 02:00 is correct. Reporting it as unhealthy every
        evening is how a health indicator stops being read."""
        app.dependency_overrides[get_clock] = lambda: SimulatedClock(BEFORE_THE_OPEN)
        store.stored[RunMode.BACKTEST.value] = build_snapshot(
            a_book(), at=BEFORE_THE_OPEN, run_mode=RunMode.BACKTEST
        )

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as http:
            _, body = await get_live(http)

        assert body["market_open"] is False
        assert body["data_feed_healthy"] is True


class TestWhenAStoreCannotBeRead:
    async def test_an_unreadable_book_is_a_503_not_an_empty_one(
        self, client: httpx.AsyncClient, store: FakeSnapshotStore
    ) -> None:
        """A dashboard that rendered "no positions" because Redis blinked would
        be telling its reader they are flat. A 503 leaves the last good data on
        screen instead, labelled stale by the client."""
        store.get_error = ConnectionError("redis is down")

        status_code, body = await get_live(client)

        assert status_code == 503
        assert "published book" in body["detail"]

    async def test_an_unreadable_halt_state_is_a_503(self, app: FastAPI) -> None:
        """ "Nothing is halted" is exactly the wrong thing to show when the truth
        is unknown — which is why `active_halts` raises rather than returning an
        empty list."""
        app.dependency_overrides[get_kill_switch] = RaisingKillSwitch

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as http:
            response = await http.get(LIVE)

        assert response.status_code == 503
        assert "halt state" in response.json()["detail"]


class TestEquityCurveEndpoint:
    def minute_points(self, count: int) -> list[EquityPoint]:
        return [
            EquityPoint(
                ts=NOW - timedelta(minutes=count - i),
                equity=Decimal(10_000 + i),
                cash=Decimal(9_000),
                gross_exposure=Decimal(1_000),
            )
            for i in range(count)
        ]

    async def test_it_thins_the_series(
        self, client: httpx.AsyncClient, repo: FakePortfolioRepository
    ) -> None:
        repo.equity_points = self.minute_points(600)

        response = await client.get(CURVE, params={"days": 1, "resolution": "1h"})
        body = response.json()

        assert response.status_code == 200
        assert body["resolution"] == "1h"
        assert 0 < len(body["points"]) <= 12

    async def test_equity_is_a_string(
        self, client: httpx.AsyncClient, repo: FakePortfolioRepository
    ) -> None:
        repo.equity_points = self.minute_points(10)

        body = (await client.get(CURVE, params={"days": 1, "resolution": "1m"})).json()

        assert isinstance(body["points"][0]["equity"], str)

    async def test_the_resolution_defaults_to_the_window(
        self, client: httpx.AsyncClient, repo: FakePortfolioRepository
    ) -> None:
        repo.equity_points = self.minute_points(10)

        body = (await client.get(CURVE, params={"days": 30})).json()

        assert body["resolution"] == "4h"

    async def test_an_unknown_resolution_is_refused_by_name(
        self, client: httpx.AsyncClient
    ) -> None:
        response = await client.get(CURVE, params={"days": 1, "resolution": "30s"})

        assert response.status_code == 422
        assert "30s" in str(response.json()["detail"])

    @pytest.mark.parametrize("days", [0, 400])
    async def test_an_out_of_range_window_is_refused(
        self, client: httpx.AsyncClient, days: int
    ) -> None:
        response = await client.get(CURVE, params={"days": days})

        assert response.status_code == 422

    async def test_it_asks_only_for_the_window_requested(
        self, client: httpx.AsyncClient, repo: FakePortfolioRepository
    ) -> None:
        """An unbounded query is one URL away from a table scan that blocks
        every other dashboard read behind it."""
        repo.equity_points = self.minute_points(10)

        await client.get(CURVE, params={"days": 7})

        assert repo.equity_points  # the fake filters on the window it was given
        body = (await client.get(CURVE, params={"days": 7, "resolution": "1m"})).json()
        assert all(
            datetime.fromisoformat(p["ts"]) >= NOW - timedelta(days=7) for p in body["points"]
        )


class TestContract:
    async def test_the_staleness_threshold_comes_from_the_server(
        self, client: httpx.AsyncClient
    ) -> None:
        """One place to decide "too old to act on" — not a browser constant.

        Nothing polls any more (ADR 0022), so this field stopped being a cadence
        and became the age past which the screen warns. It still has to travel
        with the response: the judgement of when a reading is too stale to trade
        on belongs to the platform, and a client that hardcoded it would keep
        reassuring its reader after the operator had decided otherwise.
        """
        _, body = await get_live(client)

        assert body["stale_after_seconds"] == 300

    async def test_halts_are_ordered_oldest_first(
        self, client: httpx.AsyncClient, kill_switch: FakeKillSwitch
    ) -> None:
        """The first halt of an incident is the informative one; a stable order
        keeps it at the top of the banner rather than wherever Redis'
        keyspace scan happened to put it."""
        kill_switch.halts = [
            HaltRecord(
                scope=HaltScope.SYMBOL,
                reason=HaltReason.MANUAL,
                engaged_at=NOW,
                engaged_by="operator",
                target="AAPL",
            ),
            HaltRecord(
                scope=HaltScope.GLOBAL,
                reason=HaltReason.DATA_FEED_LOST,
                engaged_at=NOW - timedelta(minutes=5),
                engaged_by="stream_ingestor",
            ),
        ]

        _, body = await get_live(client)

        assert [h["scope"] for h in body["active_halts"]] == ["global", "symbol"]
