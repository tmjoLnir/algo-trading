"""`GET /api/v1/strategies` over ASGI.

A unit test: the only source the handler reads is behind a port, so the whole
route runs against a fake with no database (CLAUDE.md §1.7). The registry half
is in-process by construction.

What is worth holding here is the *difference* between the two halves, which is
the reason the endpoint returns both:

1. **A registered class with no row has never been loaded by a worker**, and
   that is invisible anywhere else. `WORKER_STRATEGY` is empty by default, so
   this is the ordinary state of a fresh install.
2. **The registry is populated by an import side effect.** A process that never
   imports the strategy modules reports, with total confidence, that this
   platform has no strategies. The API had never read the registry before.
3. **`state` and `updated_at` mean less than their names say**, and the response
   renames one and documents the other rather than letting each client discover
   it.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import httpx
import pytest

from atp_api.auth import Scope, Session
from atp_api.deps import get_current_session, get_strategy_repository
from atp_api.main import create_app
from atp_core.domain import StrategyState
from atp_core.strategy import registry
from atp_core.strategy.ports import StoredStrategy
from tests.fakes import FakeStrategyRepository

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from fastapi import FastAPI

STRATEGIES = "/api/v1/strategies"

T0 = datetime(2026, 3, 2, 14, 30, tzinfo=UTC)

#: The one strategy this repository ships. Named rather than discovered, so a
#: test asserting on the registry fails loudly if it stops being registered
#: rather than passing vacuously over an empty dict.
SHIPPED = "sma_crossover"


def a_strategy(
    strategy_id: str = SHIPPED,
    *,
    name: str | None = None,
    state: str = StrategyState.DRAFT,
    kind: str = "coded",
    created_at: datetime = T0,
    started_at: datetime | None = None,
) -> StoredStrategy:
    return StoredStrategy(
        id=strategy_id,
        name=name or strategy_id,
        description="a moving-average crossover",
        kind=kind,
        class_name="SmaCrossover" if kind == "coded" else None,
        params={"fast": 10, "slow": 30},
        ruleset=None if kind == "coded" else {"entry": []},
        state=state,
        universe=("SPY", "QQQ"),
        timeframe="1d",
        risk_config={"max_position_pct": "0.1"},
        created_at=created_at,
        updated_at=started_at or created_at,
    )


@pytest.fixture
def strategies() -> FakeStrategyRepository:
    return FakeStrategyRepository()


@pytest.fixture
def app(strategies: FakeStrategyRepository) -> FastAPI:
    application = create_app()
    application.dependency_overrides[get_strategy_repository] = lambda: strategies
    application.dependency_overrides[get_current_session] = lambda: Session(
        "test-operator", Scope.FULL
    )
    return application


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
        yield http


class TestTheRegistry:
    def test_the_api_process_has_a_populated_registry(self) -> None:
        """Guards the import side effect the whole `available` half rests on.

        `@register` runs at import time. A process that has not imported the
        strategy modules has an empty registry and would report that this
        platform has no strategies — confidently, and wrongly. Importing the
        router is what populates it, so importing this test module's subject is
        the check.
        """
        import atp_api.routers.strategies  # noqa: F401 — the import is the test

        assert SHIPPED in registry.all_strategies()

    @pytest.mark.asyncio
    async def test_a_class_nobody_has_run_is_named(
        self, client: httpx.AsyncClient, strategies: FakeStrategyRepository
    ) -> None:
        """The question this endpoint exists for.

        With `WORKER_STRATEGY` empty by default, a strategy that exists in the
        code and has never been loaded is the ordinary state of a fresh install
        — and no other screen can say so.
        """
        strategies.rows = []

        body = (await client.get(STRATEGIES)).json()

        assert body["never_run"] == [SHIPPED]
        assert [a["name"] for a in body["available"]] == [SHIPPED]
        assert body["available"][0]["has_run"] is False

    @pytest.mark.asyncio
    async def test_a_class_a_worker_has_run_is_not_in_never_run(
        self, client: httpx.AsyncClient, strategies: FakeStrategyRepository
    ) -> None:
        strategies.rows = [a_strategy(SHIPPED)]

        body = (await client.get(STRATEGIES)).json()

        assert body["never_run"] == []
        assert body["available"][0]["has_run"] is True

    @pytest.mark.asyncio
    async def test_available_classes_carry_what_the_class_declares(
        self, client: httpx.AsyncClient, strategies: FakeStrategyRepository
    ) -> None:
        """Read off the class rather than an instance: a `Strategy` validates
        its params at construction, so building one to ask its name would fail
        for every class with required params."""
        body = (await client.get(STRATEGIES)).json()

        entry = body["available"][0]
        assert entry["class_name"] == "SmaCrossover"
        assert entry["description"]


class TestTheStoredRows:
    @pytest.mark.asyncio
    async def test_updated_at_is_served_as_what_it_records(
        self, client: httpx.AsyncClient, strategies: FakeStrategyRepository
    ) -> None:
        """`ensure` bumps only `updated_at`, at every worker boot. Serving it
        under that name would invite every reader to conclude somebody edited
        the strategy this morning."""
        started = T0 + timedelta(days=5)
        strategies.rows = [a_strategy(created_at=T0, started_at=started)]

        row = (await client.get(STRATEGIES)).json()["strategies"][0]

        assert row["last_started_at"] == "2026-03-07T14:30:00Z"
        assert row["created_at"] == "2026-03-02T14:30:00Z"
        assert "updated_at" not in row

    @pytest.mark.asyncio
    async def test_the_whole_row_reaches_the_screen(
        self, client: httpx.AsyncClient, strategies: FakeStrategyRepository
    ) -> None:
        """`StrategyRecord` is deliberately thin because a worker writes it.
        A reader needs the rest of the row."""
        strategies.rows = [a_strategy()]

        row = (await client.get(STRATEGIES)).json()["strategies"][0]

        assert row["universe"] == ["SPY", "QQQ"]
        assert row["params"] == {"fast": 10, "slow": 30}
        assert row["risk_config"] == {"max_position_pct": "0.1"}
        assert row["timeframe"] == "1d"

    @pytest.mark.asyncio
    async def test_a_coded_strategy_has_no_ruleset(
        self, client: httpx.AsyncClient, strategies: FakeStrategyRepository
    ) -> None:
        """Null rather than an empty object: the logic is in Python and this row
        genuinely cannot describe it."""
        strategies.rows = [a_strategy(kind="coded")]

        assert (await client.get(STRATEGIES)).json()["strategies"][0]["ruleset"] is None

    @pytest.mark.asyncio
    async def test_a_ruleset_strategy_carries_its_spec(
        self, client: httpx.AsyncClient, strategies: FakeStrategyRepository
    ) -> None:
        """For a declarative strategy the ruleset *is* the strategy, so a screen
        omitting it would be useless for exactly those."""
        strategies.rows = [a_strategy("my_rules", kind="ruleset")]

        assert (await client.get(STRATEGIES)).json()["strategies"][0]["ruleset"] == {"entry": []}

    @pytest.mark.asyncio
    async def test_nothing_stored_is_an_empty_list_not_an_error(
        self, client: httpx.AsyncClient, strategies: FakeStrategyRepository
    ) -> None:
        """A worker that has never booted has written no rows, which is the
        default posture rather than a fault."""
        strategies.rows = []

        body = (await client.get(STRATEGIES)).json()

        assert body["strategies"] == []
        assert body["available"], "the registry half must still be reported"


class TestTheStateFilter:
    @pytest.mark.asyncio
    async def test_it_narrows_the_stored_half(
        self, client: httpx.AsyncClient, strategies: FakeStrategyRepository
    ) -> None:
        strategies.rows = [
            a_strategy("live_one", state=StrategyState.LIVE),
            a_strategy("old_one", state=StrategyState.PAUSED),
        ]

        body = (await client.get(f"{STRATEGIES}?state=paused")).json()

        assert [s["id"] for s in body["strategies"]] == ["old_one"]

    @pytest.mark.asyncio
    async def test_it_does_not_make_a_filtered_out_strategy_look_never_run(
        self, client: httpx.AsyncClient, strategies: FakeStrategyRepository
    ) -> None:
        """`never_run` is computed against the *unfiltered* table.

        Otherwise filtering to `paused` would report every active strategy as
        one nothing has ever loaded — which is the opposite of the truth, on the
        single field this endpoint exists to get right.
        """
        strategies.rows = [a_strategy(SHIPPED, state=StrategyState.LIVE)]

        body = (await client.get(f"{STRATEGIES}?state=paused")).json()

        assert body["strategies"] == []
        assert body["never_run"] == []
        assert body["available"][0]["has_run"] is True

    @pytest.mark.asyncio
    async def test_every_rung_of_the_ratchet_is_a_valid_filter(
        self, client: httpx.AsyncClient, strategies: FakeStrategyRepository
    ) -> None:
        """All six, and the screen's filter has to offer exactly these.

        The dashboard used to offer `backtest` and `active` — neither a member —
        and to omit `live` and `halted`. Every option it listed but one matched
        nothing by construction.
        """
        for rung in StrategyState:
            strategies.rows = [a_strategy("one", state=rung)]

            response = await client.get(f"{STRATEGIES}?state={rung.value}")

            assert response.status_code == 200, rung
            assert [s["id"] for s in response.json()["strategies"]] == ["one"], rung

    @pytest.mark.asyncio
    async def test_a_state_that_is_not_a_rung_is_refused(
        self, client: httpx.AsyncClient, strategies: FakeStrategyRepository
    ) -> None:
        """422, not an empty 200.

        As a bare string this returned `200` with no rows, which reads as "no
        strategy is in that state" and is indistinguishable from the truth —
        the wrong half of the two to believe. `active` is the case that matters:
        it was written into every row for four phases, so it is the word a
        person is most likely to still be typing.
        """
        strategies.rows = [a_strategy("one", state=StrategyState.DRAFT)]

        response = await client.get(f"{STRATEGIES}?state=active")

        assert response.status_code == 422
        assert "draft" in response.text, "the refusal should name the rungs that exist"
