"""`GET /api/v1/risk/rejections` — why nothing is happening.

A unit test rather than an integration one: the record is behind a port, so the
handler runs against a fake with no Postgres (CLAUDE.md §1.7).

**The theme is that an empty list here is the most dangerous answer this
endpoint can give.** It reads as "nothing is being refused", and there are three
separate ways it can be wrong about that:

1. by filtering the wrong side of the limit — taking the newest hundred signals
   and keeping the refused ones answers "were any of the last hundred decisions
   refused", which is "no" for a strategy blocked all week that has since
   emitted a single HOLD;
2. by counting `no_action` as a refusal, which does the opposite and inflates
   the number an operator reads to decide whether risk is too tight;
3. by staying silent about the refusals that are never stored at all — a stop
   exit the risk chain denied is written to the worker's log and nowhere else,
   and it is the *worse* refusal, because it leaves a position open that should
   have closed.

The first two are assertions below. The third cannot be, because there is no
data to assert on — so what is tested is that the response says so out loud.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING

import httpx
import pytest

from atp_api.auth import Scope, Session
from atp_api.deps import get_current_session, get_signal_repository
from atp_api.main import create_app
from atp_core.config import Settings, get_settings
from atp_core.domain import Signal, SignalAction
from atp_core.strategy.ports import SignalOutcome
from tests.fakes import FakeSignalRepository

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from fastapi import FastAPI

REJECTIONS = "/api/v1/risk/rejections"

NOW = datetime(2024, 6, 3, 14, 30, tzinfo=UTC)
STRATEGY = "sma_crossover"


def pinned_settings() -> Settings:
    return Settings(ATP_RUN_MODE="backtest", _env_file=None)


@pytest.fixture
def signals() -> FakeSignalRepository:
    return FakeSignalRepository()


@pytest.fixture
def app(signals: FakeSignalRepository) -> FastAPI:
    application = create_app()
    application.dependency_overrides[get_settings] = pinned_settings
    application.dependency_overrides[get_signal_repository] = lambda: signals
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


def record(
    repo: FakeSignalRepository,
    *,
    signal_id: str,
    rule: str | None,
    reason: str | None = "refused",
    symbol: str = "SPY",
    strategy_id: str = STRATEGY,
    at: datetime = NOW,
    acted_on: bool = False,
) -> None:
    """Put one decision and its fate in the record.

    `rule=None` is a signal that was acted on; `rule="no_action"` is the
    HOLD-shaped outcome the router approves.
    """
    repo.stored[signal_id] = (
        Signal(
            strategy_id=strategy_id,
            symbol=symbol,
            action=SignalAction.ENTER_LONG,
            ts=at,
            id=signal_id,
            strength=Decimal(1),
            reason="SMA(20) crossed above SMA(50)",
            indicators={"sma_fast": Decimal("401.25"), "period": 20},
        ),
        SignalOutcome(acted_on=acted_on, rejection_reason=reason, rejected_by=rule),
    )


class TestWhatComesBack:
    async def test_a_refusal_names_the_rule_and_the_reason(
        self, client: httpx.AsyncClient, signals: FakeSignalRepository
    ) -> None:
        record(signals, signal_id="one", rule="max_position_size", reason="SPY would be 12%")

        body = (await client.get(REJECTIONS)).json()

        (row,) = body["rejections"]
        assert row["rule"] == "max_position_size"
        assert row["reason"] == "SPY would be 12%"
        assert row["symbol"] == "SPY"
        assert row["signal_id"] == "one"

    async def test_the_rule_is_the_engines_own_name(
        self, client: httpx.AsyncClient, signals: FakeSignalRepository
    ) -> None:
        """The same string `/risk/status` puts on the limit's row.

        A person reading "refused by max_gross_exposure" here has to be able to
        find the row that shows how close that limit was.
        """
        from atp_core.risk import rules

        record(signals, signal_id="one", rule=rules.MaxExposureRule().name)

        assert (await client.get(REJECTIONS)).json()["rejections"][0][
            "rule"
        ] == "max_gross_exposure"

    async def test_the_indicators_come_back_as_strings(
        self, client: httpx.AsyncClient, signals: FakeSignalRepository
    ) -> None:
        """An indicator is usually a price, and JSON has only binary floats.

        The column holds prices, period counts and boolean flags together, so
        nothing here guesses which is which (CLAUDE.md §1.1).
        """
        record(signals, signal_id="one", rule="max_position_size")

        indicators = (await client.get(REJECTIONS)).json()["rejections"][0]["indicators"]

        assert indicators == {"sma_fast": "401.25", "period": "20"}

    async def test_newest_first(
        self, client: httpx.AsyncClient, signals: FakeSignalRepository
    ) -> None:
        record(signals, signal_id="old", rule="rate_limit", at=NOW - timedelta(hours=2))
        record(signals, signal_id="new", rule="rate_limit", at=NOW)

        body = (await client.get(REJECTIONS)).json()

        assert [r["signal_id"] for r in body["rejections"]] == ["new", "old"]


class TestWhatIsNotARefusal:
    async def test_an_acted_on_signal_is_not_listed(
        self, client: httpx.AsyncClient, signals: FakeSignalRepository
    ) -> None:
        record(signals, signal_id="one", rule=None, reason=None, acted_on=True)

        assert (await client.get(REJECTIONS)).json()["rejections"] == []

    async def test_a_no_action_outcome_is_not_a_refusal(
        self, client: httpx.AsyncClient, signals: FakeSignalRepository
    ) -> None:
        """The exclusion that stops this endpoint lying in the other direction.

        `SubmitResult.no_action` marks a HOLD, or an exit against an
        already-flat position, and the router reports it as *approved* on
        purpose — reporting them as denials would inflate the number an
        operator reads to decide whether the risk config is too tight. A
        strategy that mostly holds would otherwise fill this screen with
        phantom rejections.
        """
        record(signals, signal_id="hold", rule="no_action", reason="SPY: hold")
        record(signals, signal_id="real", rule="daily_loss_limit", reason="down 3.1%")

        body = (await client.get(REJECTIONS)).json()

        assert [r["signal_id"] for r in body["rejections"]] == ["real"]
        assert "no_action" not in body["by_rule"]

    async def test_a_screen_full_of_holds_still_reports_no_refusals(
        self, client: httpx.AsyncClient, signals: FakeSignalRepository
    ) -> None:
        for i in range(50):
            record(signals, signal_id=f"hold{i}", rule="no_action", reason="hold")

        body = (await client.get(REJECTIONS)).json()

        assert body["rejections"] == []
        assert body["by_rule"] == {}


class TestTheFilteringHappensBeforeTheLimit:
    async def test_a_rejection_older_than_the_newest_signals_is_still_found(
        self, client: httpx.AsyncClient, signals: FakeSignalRepository
    ) -> None:
        """The property the whole `rejections()` method exists for.

        A strategy blocked last week that has since emitted hundreds of HOLDs
        is exactly the case an operator is investigating. Taking the newest N
        signals and keeping the refused ones would return nothing, and nothing
        reads as "there are no refusals".
        """
        record(signals, signal_id="ancient", rule="max_open_positions", at=NOW - timedelta(days=7))
        for i in range(300):
            record(signals, signal_id=f"hold{i}", rule="no_action", at=NOW - timedelta(minutes=i))

        body = (await client.get(f"{REJECTIONS}?limit=10")).json()

        assert [r["signal_id"] for r in body["rejections"]] == ["ancient"]

    async def test_the_limit_bounds_the_refusals_not_the_scan(
        self, client: httpx.AsyncClient, signals: FakeSignalRepository
    ) -> None:
        for i in range(30):
            record(signals, signal_id=f"r{i}", rule="rate_limit", at=NOW - timedelta(minutes=i))

        body = (await client.get(f"{REJECTIONS}?limit=5")).json()

        assert len(body["rejections"]) == 5


class TestTheFilters:
    async def test_by_rule(self, client: httpx.AsyncClient, signals: FakeSignalRepository) -> None:
        record(signals, signal_id="a", rule="rate_limit")
        record(signals, signal_id="b", rule="max_position_size")

        body = (await client.get(f"{REJECTIONS}?rule=rate_limit")).json()

        assert [r["signal_id"] for r in body["rejections"]] == ["a"]

    async def test_by_strategy(
        self, client: httpx.AsyncClient, signals: FakeSignalRepository
    ) -> None:
        record(signals, signal_id="a", rule="rate_limit", strategy_id="one")
        record(signals, signal_id="b", rule="rate_limit", strategy_id="two")

        body = (await client.get(f"{REJECTIONS}?strategy_id=two")).json()

        assert [r["signal_id"] for r in body["rejections"]] == ["b"]

    async def test_since(self, client: httpx.AsyncClient, signals: FakeSignalRepository) -> None:
        record(signals, signal_id="old", rule="rate_limit", at=NOW - timedelta(days=2))
        record(signals, signal_id="new", rule="rate_limit", at=NOW)

        # Passed as a param rather than interpolated: an ISO timestamp ends in
        # `+00:00`, and a bare `+` in a query string is a space — which reaches
        # the handler as an unparseable datetime and a 422.
        since = (NOW - timedelta(hours=1)).isoformat()
        response = await client.get(REJECTIONS, params={"since": since})

        assert response.status_code == 200
        assert [r["signal_id"] for r in response.json()["rejections"]] == ["new"]

    async def test_an_out_of_range_limit_is_refused(self, client: httpx.AsyncClient) -> None:
        assert (await client.get(f"{REJECTIONS}?limit=0")).status_code == 422
        assert (await client.get(f"{REJECTIONS}?limit=5000")).status_code == 422


class TestByRule:
    async def test_it_counts_the_returned_page(
        self, client: httpx.AsyncClient, signals: FakeSignalRepository
    ) -> None:
        for i in range(3):
            record(signals, signal_id=f"a{i}", rule="rate_limit")
        record(signals, signal_id="b", rule="max_position_size")

        body = (await client.get(REJECTIONS)).json()

        assert body["by_rule"] == {"rate_limit": 3, "max_position_size": 1}

    async def test_the_worst_offender_is_first(
        self, client: httpx.AsyncClient, signals: FakeSignalRepository
    ) -> None:
        """ "Which rule is refusing everything" is the question, and a dict that
        arrives in insertion order puts the answer at the top."""
        record(signals, signal_id="b", rule="max_position_size")
        for i in range(5):
            record(signals, signal_id=f"a{i}", rule="rate_limit")

        body = (await client.get(REJECTIONS)).json()

        assert next(iter(body["by_rule"])) == "rate_limit"


class TestTheBlindSpots:
    async def test_they_are_in_the_payload_not_only_the_docs(
        self, client: httpx.AsyncClient
    ) -> None:
        """An empty list reads as "nothing is being refused".

        The same reasoning that puts `observable: false` on `/risk/status`'s
        rate-limit row: a screen rendering this needs the sentence that stops a
        person drawing that conclusion, so it travels with the data.
        """
        body = (await client.get(REJECTIONS)).json()

        assert body["rejections"] == []
        assert body["blind_spots"], "an empty result must still say what it cannot see"

    async def test_the_refused_stop_exit_is_named_and_pointed_at(
        self, client: httpx.AsyncClient
    ) -> None:
        """The one worth naming specifically, and it now has somewhere to point.

        A refused entry is a trade that did not happen. A refused *stop exit* is
        a position that should have closed and did not — docs/SAFETY.md's layer
        5 failing. It used to be logged and stored nowhere, which made this list
        an apology; it is stored as a rejected order now, so the list has to
        send a reader to `/orders` rather than tell them it is lost.
        """
        spots = " ".join((await client.get(REJECTIONS)).json()["blind_spots"])

        assert "stop exit" in spots
        assert "/orders" in spots


class TestReadOnlySessions:
    async def test_a_read_only_session_may_read_them(
        self, app: FastAPI, client: httpx.AsyncClient, signals: FakeSignalRepository
    ) -> None:
        """This is the screen somebody reaches for when a strategy looks dead.

        Nothing here is a secret from a reader watching the book, and a GET is
        not a write — `require_write_scope` admits every safe method.
        """
        record(signals, signal_id="one", rule="rate_limit")
        app.dependency_overrides[get_current_session] = lambda: Session("reader", Scope.READ)

        response = await client.get(REJECTIONS)

        assert response.status_code == 200
        assert len(response.json()["rejections"]) == 1
