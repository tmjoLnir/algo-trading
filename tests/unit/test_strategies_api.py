"""`GET` and `POST /api/v1/strategies` over ASGI.

A unit test: every source these handlers touch is behind a port, so the whole
route runs against a fake with no database (CLAUDE.md §1.7). The registry half
is in-process by construction.

What is worth holding on the read side is the *difference* between the two
halves, which is the reason the endpoint returns both:

1. **A registered class with no row has never been loaded by a worker**, and
   that is invisible anywhere else. `WORKER_STRATEGY` is empty by default, so
   this is the ordinary state of a fresh install.
2. **The registry is populated by an import side effect.** A process that never
   imports the strategy modules reports, with total confidence, that this
   platform has no strategies. The API had never read the registry before.
3. **`state` and `updated_at` mean less than their names say**, and the response
   renames one and documents the other rather than letting each client discover
   it.

On the write side, nearly every test is a *refusal*, and that is the shape of
the endpoint rather than pessimism: a stored strategy is a document two other
processes will execute without asking anything further, so each refusal here is
a failure that would otherwise arrive minutes or days later, from somewhere
else, as a run that says `failed` or a flat equity curve nobody can explain.
The three that matter most:

- a spec whose name disagrees with the row it is stored in — the strategy runs
  and every decision it records fails its foreign key;
- a rule set taking a registered class's name — two strategies filing signals
  under one `strategy_id`, attribution silently merged;
- a lowercase universe — compiles, runs, never trades, reports a flat curve.
"""

from __future__ import annotations

from dataclasses import fields
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

import httpx
import pytest

from atp_api.auth import Scope, Session
from atp_api.deps import get_audit_sink, get_current_session, get_strategy_repository
from atp_api.main import create_app
from atp_api.routers.strategies import StrategyCreate
from atp_core.audit.ports import Action
from atp_core.domain import StrategyState
from atp_core.strategy import registry
from atp_core.strategy.examples import rsi_mean_reversion
from atp_core.strategy.ports import NewStrategy, StoredStrategy
from tests.fakes import FakeStrategyRepository, RecordingAuditSink

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


def available(body: dict[str, Any], name: str) -> dict[str, Any]:
    """The `available` entry for one class, by name.

    Addressed by name rather than by index because `available` is sorted and
    the registry grows: `[0]` meant `sma_crossover` while it was the only class
    registered and meant `buy_and_hold` the day a second one landed, which is a
    test that silently changes its subject rather than failing.
    """
    entry = next((a for a in body["available"] if a["name"] == name), None)
    assert entry is not None, f"{name} is registered but the endpoint did not report it"
    return entry


@pytest.fixture
def strategies() -> FakeStrategyRepository:
    return FakeStrategyRepository()


@pytest.fixture
def audit() -> RecordingAuditSink:
    return RecordingAuditSink()


@pytest.fixture
def app(strategies: FakeStrategyRepository, audit: RecordingAuditSink) -> FastAPI:
    application = create_app()
    application.dependency_overrides[get_strategy_repository] = lambda: strategies
    application.dependency_overrides[get_audit_sink] = lambda: audit
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

        registered = sorted(registry.all_strategies())
        assert body["never_run"] == registered
        assert [a["name"] for a in body["available"]] == registered
        assert available(body, SHIPPED)["has_run"] is False

    @pytest.mark.asyncio
    async def test_a_class_a_worker_has_run_is_not_in_never_run(
        self, client: httpx.AsyncClient, strategies: FakeStrategyRepository
    ) -> None:
        strategies.rows = [a_strategy(SHIPPED)]

        body = (await client.get(STRATEGIES)).json()

        # The one with a row drops out; every other registered class stays in,
        # which is the contrast the field exists to draw.
        assert body["never_run"] == sorted(set(registry.all_strategies()) - {SHIPPED})
        assert SHIPPED not in body["never_run"]
        assert available(body, SHIPPED)["has_run"] is True

    @pytest.mark.asyncio
    async def test_available_classes_carry_what_the_class_declares(
        self, client: httpx.AsyncClient, strategies: FakeStrategyRepository
    ) -> None:
        """Read off the class rather than an instance: a `Strategy` validates
        its params at construction, so building one to ask its name would fail
        for every class with required params."""
        body = (await client.get(STRATEGIES)).json()

        entry = available(body, SHIPPED)
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
        assert body["never_run"] == sorted(set(registry.all_strategies()) - {SHIPPED})
        assert SHIPPED not in body["never_run"]
        assert available(body, SHIPPED)["has_run"] is True

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


def a_ruleset(name: str = "my_rules", **overrides: Any) -> dict[str, Any]:
    """The shipped reference spec, as JSON, under whatever name a test wants.

    `rsi_mean_reversion()` rather than a dict written here, so these tests
    author the same document `docs/STRATEGY_AUTHORING.md` prints and the UI's
    save would post. A spec invented in a test file is a spec that can drift
    into validating something the real one would not.
    """
    spec = rsi_mean_reversion().model_dump(mode="json")
    spec["name"] = name
    spec.update(overrides)
    return spec


def create(kind: str = "ruleset", **fields: Any) -> dict[str, Any]:
    """A create request, defaulted to the smallest valid rule set."""
    if kind == "ruleset":
        body: dict[str, Any] = {"name": "my_rules", "kind": "ruleset", "ruleset": a_ruleset()}
    else:
        body = {"name": SHIPPED, "kind": "coded"}
    body.update(fields)
    return body


class TestCreatingARuleSet:
    """The half of this endpoint the platform could not do without.

    `POST /api/v1/backtests` has resolved a stored rule set into a run since
    #96, and until now nothing could store one: `StrategyRecord` has no
    `ruleset` field and the adapter wrote that column as a hard-coded `None`.
    """

    @pytest.mark.asyncio
    async def test_the_spec_is_stored_and_the_row_comes_back(
        self, client: httpx.AsyncClient, strategies: FakeStrategyRepository
    ) -> None:
        response = await client.post(STRATEGIES, json=create(name="my_rules"))

        assert response.status_code == 201, response.text
        body = response.json()
        assert body["id"] == "my_rules"
        assert body["kind"] == "ruleset"
        assert body["ruleset"]["entry_long"] == a_ruleset()["entry_long"]
        # Read off the spec rather than off the request: the row must not be
        # able to advertise a universe the compiled strategy would ignore.
        assert body["universe"] == ["SPY", "QQQ", "IWM"]
        assert body["timeframe"] == "1d"
        # No class stands behind a rule set, and it takes no params.
        assert body["class_name"] is None
        assert body["params"] == {}

    @pytest.mark.asyncio
    async def test_what_was_created_is_what_the_list_then_shows(
        self, client: httpx.AsyncClient, strategies: FakeStrategyRepository
    ) -> None:
        """One row, one shape, both endpoints.

        The response is `StoredStrategyView` rather than an echo of the request
        precisely so these two agree — and the fake stores into the same list
        `list_all` reads, so this fails if a create landed somewhere a reader
        cannot see it.
        """
        created = (await client.post(STRATEGIES, json=create())).json()

        listed = (await client.get(STRATEGIES)).json()["strategies"]

        assert listed == [created]

    @pytest.mark.asyncio
    async def test_a_spec_that_names_something_else_is_refused(
        self, client: httpx.AsyncClient, strategies: FakeStrategyRepository
    ) -> None:
        """The check that stops a strategy whose every decision is unrecordable.

        `compile_ruleset` copies `spec.name` onto the class, and every `Signal`
        it emits stamps `strategy_id` with it. Stored under a different name,
        the row is not the row those signals point at — so the strategy would
        run and every write of what it decided would fail its foreign key.
        """
        body = create(name="my_rules", ruleset=a_ruleset(name="something_else"))

        response = await client.post(STRATEGIES, json=body)

        assert response.status_code == 400
        assert "something_else" in response.text
        assert strategies.rows == []

    @pytest.mark.asyncio
    async def test_a_name_a_registered_class_already_has_is_refused(
        self, client: httpx.AsyncClient, strategies: FakeStrategyRepository
    ) -> None:
        """`registry.register` refuses a duplicate name to keep results
        unambiguous. This is that rule across the two namespaces: a class and a
        rule set sharing one name would file every signal under one
        `strategy_id`, with their attribution silently merged."""
        body = create(name=SHIPPED, ruleset=a_ruleset(name=SHIPPED))

        response = await client.post(STRATEGIES, json=body)

        assert response.status_code == 400
        assert SHIPPED in response.text

    @pytest.mark.asyncio
    async def test_a_malformed_spec_is_a_400_not_a_500(
        self, client: httpx.AsyncClient, strategies: FakeStrategyRepository
    ) -> None:
        """A rule set arrives over HTTP and is untrusted input.

        `crosses_sideways` is not a comparator, and the failure belongs to the
        author rather than to the server.
        """
        broken = a_ruleset()
        broken["exit"] = {"any": [{"left": {"price": "close"}, "op": "??", "right": {"value": 1}}]}

        response = await client.post(STRATEGIES, json=create(ruleset=broken))

        assert response.status_code == 400
        assert "malformed" in response.text
        assert strategies.rows == []

    @pytest.mark.asyncio
    async def test_a_spec_that_can_never_close_a_position_is_refused(
        self, client: httpx.AsyncClient, strategies: FakeStrategyRepository
    ) -> None:
        """`RuleSet` already refuses no-exit-and-no-stop; this is that refusal
        reaching the author at creation instead of at 09:31 next Tuesday."""
        no_way_out = a_ruleset()
        no_way_out["exit"] = None
        no_way_out["risk"]["stop_loss"] = None

        response = await client.post(STRATEGIES, json=create(ruleset=no_way_out))

        assert response.status_code == 400
        assert "close" in response.text

    @pytest.mark.asyncio
    async def test_a_declarative_row_with_no_rules_is_refused(
        self, client: httpx.AsyncClient, strategies: FakeStrategyRepository
    ) -> None:
        """The state `POST /backtests` already refuses to run, refused one step
        earlier — nothing can execute it and no class can stand in."""
        response = await client.post(STRATEGIES, json=create(ruleset=None))

        assert response.status_code == 400
        assert strategies.rows == []

    @pytest.mark.asyncio
    async def test_params_alongside_a_rule_set_are_refused(
        self, client: httpx.AsyncClient, strategies: FakeStrategyRepository
    ) -> None:
        """Not dropped: a caller who sent params believes they do something."""
        response = await client.post(STRATEGIES, json=create(params={"period": 14}))

        assert response.status_code == 400
        assert "period" in response.text

    @pytest.mark.asyncio
    async def test_a_lowercase_universe_would_never_trade_and_is_refused(
        self, client: httpx.AsyncClient, strategies: FakeStrategyRepository
    ) -> None:
        """The quietest failure this endpoint can prevent.

        `_RuleSetStrategy.on_bar` matches `bar.symbol` against the spec's
        universe exactly, and every stored bar is uppercase. A spec on `spy`
        compiles, runs, takes no trade, and reports a flat curve
        indistinguishable from a strategy that never signalled.
        """
        response = await client.post(STRATEGIES, json=create(ruleset=a_ruleset(universe=["spy"])))

        assert response.status_code == 400
        assert "never trade" in response.text

    @pytest.mark.asyncio
    async def test_a_universe_that_disagrees_with_the_spec_is_refused(
        self, client: httpx.AsyncClient, strategies: FakeStrategyRepository
    ) -> None:
        """Two answers to "what does this trade" in one row. The spec wins, and
        a request that asserts otherwise is told so rather than overruled."""
        response = await client.post(STRATEGIES, json=create(universe=["TSLA"]))

        assert response.status_code == 400
        assert "TSLA" in response.text

    @pytest.mark.asyncio
    async def test_a_universe_that_agrees_with_the_spec_is_fine(
        self, client: httpx.AsyncClient, strategies: FakeStrategyRepository
    ) -> None:
        """Restating the spec is not an error — only contradicting it is."""
        response = await client.post(STRATEGIES, json=create(universe=["IWM", "SPY", "QQQ"]))

        assert response.status_code == 201, response.text

    @pytest.mark.asyncio
    async def test_a_timeframe_that_disagrees_with_the_spec_is_refused(
        self, client: httpx.AsyncClient, strategies: FakeStrategyRepository
    ) -> None:
        response = await client.post(STRATEGIES, json=create(timeframe="1h"))

        assert response.status_code == 400
        assert "1d" in response.text

    @pytest.mark.asyncio
    async def test_the_description_falls_back_to_the_spec(
        self, client: httpx.AsyncClient, strategies: FakeStrategyRepository
    ) -> None:
        """Unlike the coded case there is no other copy to drift from: the spec
        lives in this same row, so the fallback reads from the thing it
        describes."""
        body = (await client.post(STRATEGIES, json=create())).json()

        assert body["description"] == rsi_mean_reversion().description


class TestCreatingACodedStrategy:
    @pytest.mark.asyncio
    async def test_the_class_and_its_defaults_are_recorded(
        self, client: httpx.AsyncClient, strategies: FakeStrategyRepository
    ) -> None:
        """`registry.default_params`' argument, applied at the point it matters.

        Storing `{}` would record a strategy configured with nothing, when what
        would actually run is a crossover on 20 and 50. A reader cannot tell
        those apart and the second one is the truth.
        """
        response = await client.post(STRATEGIES, json=create("coded", params={"fast_period": 5}))

        assert response.status_code == 201, response.text
        body = response.json()
        assert body["class_name"] == "SmaCrossover"
        assert body["params"] == {"fast_period": 5, "slow_period": 50, "timeframe": "1d"}

    @pytest.mark.asyncio
    async def test_an_unknown_name_names_the_registry(
        self, client: httpx.AsyncClient, strategies: FakeStrategyRepository
    ) -> None:
        """What makes a typo self-correcting. The registry is populated by the
        `examples` import at the top of the router — without it this endpoint
        would refuse every strategy with total confidence."""
        response = await client.post(STRATEGIES, json=create("coded", name="sma_crossovr"))

        assert response.status_code == 400
        assert SHIPPED in response.text

    @pytest.mark.asyncio
    async def test_params_the_class_rejects_are_refused(
        self, client: httpx.AsyncClient, strategies: FakeStrategyRepository
    ) -> None:
        """The class is constructed, not merely looked up: a `Strategy`
        validates its params in its constructor, so an impossible pair is a 400
        here rather than a worker that fails at its first session open."""
        body = create("coded", params={"fast_period": 50, "slow_period": 10})

        response = await client.post(STRATEGIES, json=body)

        assert response.status_code == 400
        assert "params" in response.text
        assert strategies.rows == []

    @pytest.mark.asyncio
    async def test_a_ruleset_on_a_coded_strategy_is_refused(
        self, client: httpx.AsyncClient, strategies: FakeStrategyRepository
    ) -> None:
        """Its logic is the registered class, so a spec here is a document
        nothing would ever read."""
        body = create("coded", ruleset=a_ruleset(name=SHIPPED))

        response = await client.post(STRATEGIES, json=body)

        assert response.status_code == 400
        assert strategies.rows == []

    @pytest.mark.asyncio
    async def test_the_universe_is_stored_as_asked_deduplicated(
        self, client: httpx.AsyncClient, strategies: FakeStrategyRepository
    ) -> None:
        body = create("coded", universe=["SPY", "QQQ", "SPY"])

        assert (await client.post(STRATEGIES, json=body)).json()["universe"] == ["SPY", "QQQ"]

    @pytest.mark.asyncio
    async def test_no_universe_at_all_is_allowed(
        self, client: httpx.AsyncClient, strategies: FakeStrategyRepository
    ) -> None:
        """`scripts/seed.py` writes rows with an empty universe deliberately —
        the column records what a strategy is *configured* to trade, and a
        backtest asks for symbols per run."""
        assert (await client.post(STRATEGIES, json=create("coded"))).status_code == 201

    @pytest.mark.asyncio
    async def test_a_lowercase_symbol_is_refused(
        self, client: httpx.AsyncClient, strategies: FakeStrategyRepository
    ) -> None:
        """Required rather than applied. This universe is matched against
        `Bar.symbol` later, by something else, and a symbol spelled the way no
        bar is spelled matches nothing at all."""
        response = await client.post(STRATEGIES, json=create("coded", universe=["spy"]))

        assert response.status_code == 400
        assert "ticker" in response.text


class TestWhatCreateRefuses:
    @pytest.mark.asyncio
    async def test_a_name_longer_than_the_key_is_refused(
        self, client: httpx.AsyncClient, strategies: FakeStrategyRepository
    ) -> None:
        """`strategies.id` is `String(36)`. Without this the refusal is a
        driver's "value too long for type character varying(36)", on a request
        that looked entirely reasonable."""
        long_name = "r" * 37

        response = await client.post(
            STRATEGIES, json=create(name=long_name, ruleset=a_ruleset(name=long_name))
        )

        assert response.status_code == 400
        assert "36" in response.text

    @pytest.mark.asyncio
    @pytest.mark.parametrize("name", ["my rules", "rules/1", "", "   ", "-leading"])
    async def test_a_name_that_is_not_a_usable_id_is_refused(
        self, client: httpx.AsyncClient, strategies: FakeStrategyRepository, name: str
    ) -> None:
        """The id travels in a URL path, a query string and every log line
        about the strategy."""
        response = await client.post(
            STRATEGIES, json=create(name=name, ruleset=a_ruleset(name=name))
        )

        assert response.status_code == 400, name
        assert strategies.rows == []

    @pytest.mark.asyncio
    async def test_an_unknown_kind_is_a_422_naming_both(
        self, client: httpx.AsyncClient, strategies: FakeStrategyRepository
    ) -> None:
        """A `Literal`, not a `str`. The same reasoning that types the `state`
        filter as its enum: a handler with two branches should not have to
        invent a third refusal."""
        body = create()
        # Set on the body rather than passed to `create`, whose first argument
        # *is* the kind and would build a different request instead of a bad one.
        body["kind"] = "declarative"

        response = await client.post(STRATEGIES, json=body)

        assert response.status_code == 422
        assert "ruleset" in response.text
        assert "coded" in response.text

    @pytest.mark.asyncio
    async def test_a_risk_config_is_refused_rather_than_stored(
        self, client: httpx.AsyncClient, strategies: FakeStrategyRepository
    ) -> None:
        """Nothing in the platform reads that column — every pre-trade limit
        comes from `Settings`. Storing one would put a limit on the Strategies
        tab that no order is ever checked against, which is the one kind of
        silence this domain cannot afford."""
        body = create(risk_config={"max_position_pct": "0.1"})

        response = await client.post(STRATEGIES, json=body)

        assert response.status_code == 400
        assert "max_position_pct" in response.text

    @pytest.mark.asyncio
    async def test_a_name_that_is_taken_is_a_409(
        self, client: httpx.AsyncClient, strategies: FakeStrategyRepository
    ) -> None:
        """And the name may be taken by something nobody authored: a worker
        writes a row at its first session open, and `scripts/seed.py` writes one
        per registered class."""
        strategies.rows = [a_strategy("my_rules", kind="ruleset")]

        response = await client.post(STRATEGIES, json=create(name="my_rules"))

        assert response.status_code == 409
        assert "my_rules" in response.text
        # Not overwritten. The stored row is the one that was already there.
        assert len(strategies.rows) == 1
        assert strategies.rows[0].description == "a moving-average crossover"


class TestTheRatchet:
    def test_a_create_request_cannot_name_a_state(self) -> None:
        """Held as a property of the type rather than as a check.

        `draft` is not a default this endpoint applies to a missing field —
        there is no field. `NewStrategy` has no `state` either, so nothing
        between here and the INSERT could carry a higher rung even if a caller
        found a way to ask for one.
        """
        assert "state" not in StrategyCreate.model_fields
        assert not any(f.name == "state" for f in fields(NewStrategy))

    @pytest.mark.asyncio
    async def test_a_new_strategy_starts_on_the_first_rung(
        self, client: httpx.AsyncClient, strategies: FakeStrategyRepository
    ) -> None:
        response = await client.post(STRATEGIES, json=create())

        assert response.json()["state"] == StrategyState.DRAFT.value

    @pytest.mark.asyncio
    async def test_an_unknown_field_does_not_smuggle_one_in(
        self, client: httpx.AsyncClient, strategies: FakeStrategyRepository
    ) -> None:
        """Pydantic ignores what it does not know, which is the behaviour that
        would make a silently-accepted `state` dangerous. This pins that the
        row is `draft` regardless."""
        response = await client.post(STRATEGIES, json=create(state="live", id="something"))

        assert response.status_code == 201, response.text
        assert response.json()["state"] == StrategyState.DRAFT.value
        assert response.json()["id"] == "my_rules"


class TestTheRecord:
    @pytest.mark.asyncio
    async def test_the_creation_is_audited_against_the_session(
        self, client: httpx.AsyncClient, audit: RecordingAuditSink
    ) -> None:
        """Who minted this identity, from the cookie rather than from the body.

        An actor a request can name is not an audit trail (ADR 0008), and this
        is the moment a strategy's name — the key every later signal and order
        carries — comes into existence.
        """
        await client.post(STRATEGIES, json=create())

        assert len(audit.entries) == 1
        entry = audit.entries[0]
        assert entry.action == Action.STRATEGY_CREATED
        assert entry.actor == "test-operator"
        assert entry.target == "my_rules"
        assert entry.detail["kind"] == "ruleset"

    @pytest.mark.asyncio
    async def test_a_refused_create_is_not_recorded_as_one(
        self, client: httpx.AsyncClient, audit: RecordingAuditSink
    ) -> None:
        """Written after the row exists, like the two halt verbs: an entry
        claiming an action that did not take is read as fact by whoever reviews
        it afterwards."""
        await client.post(STRATEGIES, json=create(ruleset=a_ruleset(name="mismatch")))

        assert audit.entries == []
