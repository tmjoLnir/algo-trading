"""`GET /api/v1/risk/limits` and `/api/v1/risk/status` — the read half of risk.

A unit test rather than an integration one: the book is behind a port, so both
handlers run against fakes with no Redis (CLAUDE.md §1.7).

**The theme is that a risk screen must never make an unknown book look like a
compliant one.** Everything else here is arithmetic. The endpoint answers "how
close are we to being refused", and the two ways it can mislead both point the
same direction: reporting zero where it means unknown, and understating a
figure that a limit is measured against. Both render as green on a screen whose
entire purpose is to be read before somebody promotes a strategy to live.

The second theme is that the comparisons have to be the *rules'* comparisons.
The rules disagree with each other about their boundaries on purpose, and a
status page that harmonised them would tell an operator they are fine while the
engine refuses their next order.
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
    get_portfolio_repository,
    get_snapshot_store,
)
from atp_api.main import create_app
from atp_core.clock import SimulatedClock
from atp_core.config import Settings, get_settings
from atp_core.dashboard.snapshot import build_snapshot
from atp_core.domain import Portfolio, Position, RunMode
from atp_core.execution.ports import EquityPoint
from tests.fakes import FakePortfolioRepository, FakeSnapshotStore

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from fastapi import FastAPI

LIMITS = "/api/v1/risk/limits"
STATUS = "/api/v1/risk/status"

#: A Monday, 14:30 UTC — 10:30 in New York, an hour into the session, so the
#: market is open without a test having to say so.
NOW = datetime(2024, 6, 3, 14, 30, tzinfo=UTC)
SESSION_OPEN = datetime(2024, 6, 3, 13, 30, tzinfo=UTC)


def pinned_settings() -> Settings:
    """Settings that do not depend on the shell the suite is run from.

    The risk limits are the defaults in `RiskLimits`, which is what the
    assertions below are written against: 10% position, 100% gross, 3% daily
    loss, 30 orders/min, 20 positions, 30s quote age.
    """
    return Settings(ATP_RUN_MODE="backtest", _env_file=None)


def a_book(*, cash: str = "9000", positions: dict[str, tuple[str, str | None]] | None = None):
    """A portfolio with exact numbers, so a boundary can be landed on.

    `positions` maps symbol → (qty, last_price). A `None` price is an *unmarked*
    position, which is the case that makes every exposure figure a lower bound.
    """
    book = Portfolio(cash=Decimal(cash), starting_equity=Decimal("10000"))
    chosen = {"AAPL": ("10", "110")} if positions is None else positions
    for symbol, (qty, price) in chosen.items():
        book.positions[symbol] = Position(
            symbol=symbol,
            qty=Decimal(qty),
            avg_entry_price=Decimal("100"),
            last_price=None if price is None else Decimal(price),
            opened_at=NOW - timedelta(days=1),
        )
    return book


@pytest.fixture
def store() -> FakeSnapshotStore:
    return FakeSnapshotStore()


@pytest.fixture
def repo() -> FakePortfolioRepository:
    return FakePortfolioRepository()


@pytest.fixture
def app(store: FakeSnapshotStore, repo: FakePortfolioRepository) -> FastAPI:
    application = create_app()
    application.dependency_overrides[get_settings] = pinned_settings
    application.dependency_overrides[get_clock] = lambda: SimulatedClock(NOW)
    application.dependency_overrides[get_snapshot_store] = lambda: store
    application.dependency_overrides[get_portfolio_repository] = lambda: repo
    # Scope is not what these are about — `test_api_contract.py` holds that from
    # the outside against every route at once. Both routes here are GETs, so a
    # read-only session may call them either way.
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


def publish(store: FakeSnapshotStore, book: Portfolio, **kwargs: Any) -> None:
    store.stored[RunMode.BACKTEST.value] = build_snapshot(
        book, at=NOW, run_mode=RunMode.BACKTEST, **kwargs
    )


def anchor(repo: FakePortfolioRepository, equity: str) -> None:
    """Seed the day's opening equity, which is what day P&L is measured from."""
    repo.equity_points = [
        EquityPoint(
            ts=SESSION_OPEN,
            equity=Decimal(equity),
            cash=Decimal(equity),
            gross_exposure=Decimal(0),
        )
    ]


def row(body: Any, rule: str) -> Any:
    return next(r for r in body["limits"] if r["rule"] == rule)


class TestLimits:
    async def test_it_serves_the_configured_ceilings(self, client: httpx.AsyncClient) -> None:
        body = (await client.get(LIMITS)).json()

        assert body["max_position_pct"] == "0.10"
        assert body["max_gross_exposure_pct"] == "1.00"
        assert body["max_daily_loss_pct"] == "0.03"
        assert body["max_orders_per_minute"] == 30
        assert body["max_open_positions"] == 20
        assert body["max_quote_age_seconds"] == 30

    async def test_the_fractions_are_strings(self, client: httpx.AsyncClient) -> None:
        """They are multiplied by equity to produce a ceiling an order is
        measured against, so a `0.1` that arrived as a binary float would move
        the ceiling (CLAUDE.md §1.1)."""
        body = (await client.get(LIMITS)).json()

        assert isinstance(body["max_position_pct"], str)
        assert isinstance(body["max_daily_loss_pct"], str)

    async def test_it_answers_with_every_store_gone(
        self, app: FastAPI, client: httpx.AsyncClient
    ) -> None:
        """The reason this is not a field on `/status`.

        The moment an operator most wants to know what the limits are is an
        incident, which is also when the stores are least likely to answer.
        This route reads config and nothing else, so it survives all of them.
        """
        del app.dependency_overrides[get_snapshot_store]
        del app.dependency_overrides[get_portfolio_repository]

        response = await client.get(LIMITS)

        assert response.status_code == 200
        assert response.json()["max_open_positions"] == 20


class TestStatusWithNoPublishedBook:
    """The state a fresh deployment is in, and the one a dead worker leaves.

    This is the class that matters. A worker that is up but not trading
    publishes nothing, and so does one that has just started or just died.
    """

    async def test_it_is_a_200_not_an_error(self, client: httpx.AsyncClient) -> None:
        assert (await client.get(STATUS)).status_code == 200

    async def test_it_says_there_is_no_book(self, client: httpx.AsyncClient) -> None:
        body = (await client.get(STATUS)).json()

        assert body["book_published"] is False
        assert body["book_as_of"] is None
        assert body["equity"] is None

    async def test_no_reading_is_reported_as_zero(self, client: httpx.AsyncClient) -> None:
        """The safety property of the whole endpoint.

        "0% of your exposure limit, 0 of 20 positions" is a compliant book. The
        truth here is that nobody knows what the book contains, and those two
        sentences must not render identically (ADR 0007).
        """
        body = (await client.get(STATUS)).json()

        assert body["limits"], "the rows must still be served"
        for entry in body["limits"]:
            assert entry["current"] is None, f"{entry['rule']} reported a reading"
            assert entry["utilisation"] is None, f"{entry['rule']} reported a utilisation"
            assert entry["at_limit"] is None, f"{entry['rule']} claimed to know"

    async def test_the_ceilings_are_still_served(self, client: httpx.AsyncClient) -> None:
        """ "What is the exposure limit" has an answer whether or not anyone is
        trading. Dropping the rows would read as the limits having gone away."""
        body = (await client.get(STATUS)).json()

        assert row(body, "max_open_positions")["ceiling"] == "20"
        assert row(body, "max_gross_exposure")["ceiling"] == "1.00"


class TestStatusFromABook:
    @pytest.fixture(autouse=True)
    def _book(self, store: FakeSnapshotStore, repo: FakePortfolioRepository) -> None:
        # cash 9000 + AAPL 10 @ 110 = 1100 → equity 10100, gross exposure 1100.
        publish(store, a_book())
        anchor(repo, "10000")

    async def test_it_reports_the_book(self, client: httpx.AsyncClient) -> None:
        body = (await client.get(STATUS)).json()

        assert body["book_published"] is True
        assert body["equity"] == "10100"
        assert body["book_age_seconds"] == 0

    async def test_gross_exposure_against_equity(self, client: httpx.AsyncClient) -> None:
        entry = row(body := (await client.get(STATUS)).json(), "max_gross_exposure")
        assert body["book_published"] is True

        # 1100 / 10100 = 0.1089…
        assert entry["current"] == "0.1089"
        assert entry["at_limit"] is False

    async def test_the_largest_position_against_equity(self, client: httpx.AsyncClient) -> None:
        entry = row((await client.get(STATUS)).json(), "max_position_size")

        assert entry["current"] == "0.1089"
        # Over the 10% single-position cap, while gross exposure is nowhere near
        # its own — which is the pair of readings this row exists to separate.
        assert entry["at_limit"] is True

    async def test_the_open_position_count(self, client: httpx.AsyncClient) -> None:
        entry = row((await client.get(STATUS)).json(), "max_open_positions")

        assert entry["current"] == "1"
        assert entry["at_limit"] is False

    async def test_the_rule_names_match_the_engine(self, client: httpx.AsyncClient) -> None:
        """A refusal reads "refused by max_gross_exposure" on the orders screen.

        The row that should have predicted it has to carry the same string, or
        an operator cannot get from one to the other.
        """
        from atp_core.risk import rules

        served = {entry["rule"] for entry in (await client.get(STATUS)).json()["limits"]}
        engine_names = {
            rules.MaxPositionSizeRule().name,
            rules.MaxExposureRule().name,
            rules.DailyLossLimitRule().name,
            rules.MaxOpenPositionsRule().name,
        }
        assert engine_names <= served


class TestTheBoundariesMatchTheRules:
    """Each rule's own comparison, including where they disagree."""

    async def test_holding_the_position_limit_is_at_limit(
        self, client: httpx.AsyncClient, store: FakeSnapshotStore, repo: FakePortfolioRepository
    ) -> None:
        """`MaxOpenPositionsRule` refuses at `>=`: holding the limit means no
        *new* symbol may be opened, so at the limit already blocks."""
        held = {f"SYM{i}": ("1", "10") for i in range(20)}
        publish(store, a_book(cash="1000", positions=held))
        anchor(repo, "10000")

        entry = row((await client.get(STATUS)).json(), "max_open_positions")

        assert entry["current"] == "20"
        assert entry["at_limit"] is True

    async def test_exposure_exactly_at_the_ceiling_is_not_at_limit(
        self, client: httpx.AsyncClient, store: FakeSnapshotStore, repo: FakePortfolioRepository
    ) -> None:
        """`MaxExposureRule` refuses at `>`, not `>=` — the ceiling is a value
        exposure may reach. Rounding this together with the rule above would
        tell someone they are blocked when they are not."""
        # No cash, one position: gross exposure is exactly equity, so 100%.
        publish(store, a_book(cash="0", positions={"AAPL": ("10", "100")}))
        anchor(repo, "10000")

        entry = row((await client.get(STATUS)).json(), "max_gross_exposure")

        assert entry["current"] == "1.0000"
        assert entry["at_limit"] is False, "the rule refuses above the ceiling, not at it"

    async def test_exactly_at_the_daily_loss_limit_is_at_limit(
        self, client: httpx.AsyncClient, store: FakeSnapshotStore, repo: FakePortfolioRepository
    ) -> None:
        """`DailyLossLimitRule` refuses at `change <= -limit`."""
        # Equity 9700 against a 10000 anchor is exactly -3%.
        publish(store, a_book(cash="9700", positions={}))
        anchor(repo, "10000")

        entry = row((await client.get(STATUS)).json(), "daily_loss_limit")

        assert entry["current"] == "-0.0300"
        assert entry["at_limit"] is True

    async def test_a_profitable_day_uses_none_of_the_loss_allowance(
        self, client: httpx.AsyncClient, store: FakeSnapshotStore, repo: FakePortfolioRepository
    ) -> None:
        """`current` is the day's *signed* change, so dividing it by a loss
        limit would render a good day as a negative percentage used — which on
        a progress bar is indistinguishable from nothing at all, and on a
        number is nonsense."""
        publish(store, a_book(cash="10500", positions={}))
        anchor(repo, "10000")

        entry = row((await client.get(STATUS)).json(), "daily_loss_limit")

        assert entry["current"] == "0.0500"
        assert entry["utilisation"] == "0.0000"
        assert entry["at_limit"] is False

    async def test_a_losing_day_uses_part_of_it(
        self, client: httpx.AsyncClient, store: FakeSnapshotStore, repo: FakePortfolioRepository
    ) -> None:
        # -1.5% against a 3% allowance is half of it.
        publish(store, a_book(cash="9850", positions={}))
        anchor(repo, "10000")

        entry = row((await client.get(STATUS)).json(), "daily_loss_limit")

        assert entry["current"] == "-0.0150"
        assert entry["utilisation"] == "0.5000"
        assert entry["at_limit"] is False


class TestWhatCannotBeRead:
    async def test_the_order_rate_is_reported_as_unobservable(
        self, client: httpx.AsyncClient, store: FakeSnapshotStore, repo: FakePortfolioRepository
    ) -> None:
        """Not null-because-today, but null-always, and the row says which.

        `RateLimitRule` keeps its window in the worker's own process and counts
        refused attempts. Those are recorded as *signals*, never orders, so a
        count from the order table would read as calm during exactly the
        runaway this limit exists to catch.
        """
        publish(store, a_book())
        anchor(repo, "10000")

        entry = row((await client.get(STATUS)).json(), "rate_limit")

        assert entry["observable"] is False
        assert entry["current"] is None
        assert "signals" in (entry["note"] or "")

    async def test_it_stays_unobservable_with_no_book(self, client: httpx.AsyncClient) -> None:
        """The distinction survives the book being absent: the other rows are
        unknown *right now*, this one is unknown always."""
        entry = row((await client.get(STATUS)).json(), "rate_limit")

        assert entry["observable"] is False
        assert all(
            e["observable"] is True
            for e in (await client.get(STATUS)).json()["limits"]
            if e["rule"] != "rate_limit"
        )

    async def test_unmarked_positions_make_exposure_a_lower_bound(
        self, client: httpx.AsyncClient, store: FakeSnapshotStore, repo: FakePortfolioRepository
    ) -> None:
        """An unmarked position contributes nothing to exposure, so every
        figure understates — the direction that makes a breached limit look
        compliant. It travels with the numbers rather than being inferred."""
        publish(store, a_book(positions={"AAPL": ("10", "110"), "MSFT": ("50", None)}))
        anchor(repo, "10000")

        body = (await client.get(STATUS)).json()

        assert body["unmarked_symbols"] == ["MSFT"]
        assert "understated" in (row(body, "max_gross_exposure")["note"] or "")

    async def test_no_day_anchor_means_no_reading_rather_than_zero(
        self, client: httpx.AsyncClient, store: FakeSnapshotStore
    ) -> None:
        """A first-ever run, or a database that could not answer. Zero would
        say "flat on the day", which is a number a reader acts on."""
        publish(store, a_book())

        entry = row((await client.get(STATUS)).json(), "daily_loss_limit")

        assert entry["current"] is None
        assert entry["at_limit"] is None
        assert entry["note"]


class TestWhenTheBookCannotBeRead:
    async def test_it_is_a_503_rather_than_an_all_clear(
        self, client: httpx.AsyncClient, store: FakeSnapshotStore
    ) -> None:
        """The most misleading page in the application, if this got it wrong.

        A risk screen that rendered "nothing is near a limit" because Redis
        blinked is worse than one that fails, because a person acts on it.
        """
        store.get_error = ConnectionError("Error 111 connecting to redis:6379.")

        response = await client.get(STATUS)

        assert response.status_code == 503
        assert "cannot read the published book" in response.json()["detail"]


class TestZeroEquity:
    async def test_fractions_of_equity_are_unknown_rather_than_zero(
        self, client: httpx.AsyncClient, store: FakeSnapshotStore, repo: FakePortfolioRepository
    ) -> None:
        """`MaxPositionSizeRule` and `MaxExposureRule` both refuse outright at
        `equity <= 0`, so there is no compliant reading to report. A fraction of
        nothing is undefined, not zero."""
        publish(store, a_book(cash="0", positions={}))
        anchor(repo, "10000")

        body = (await client.get(STATUS)).json()

        assert body["equity"] == "0"
        assert row(body, "max_gross_exposure")["current"] is None
        assert row(body, "max_position_size")["current"] is None
        # The count does not depend on equity and is still real.
        assert row(body, "max_open_positions")["current"] == "0"
