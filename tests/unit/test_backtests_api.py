"""`/api/v1/backtests` over ASGI.

A unit test: every source the handlers read is behind a port, so the whole route
runs against fakes with no database, no Redis and no arq worker
(CLAUDE.md §1.7). The registry half is in-process by construction.

What is worth holding here, in the order it matters:

1. **The row is written before the job is enqueued.** A row with no job is a
   queued run somebody can re-queue; a job with no row is a worker that wakes up,
   cannot find what it was asked to do, and has nowhere to write the failure.
2. **A queue that will not accept the job leaves a failed run, not a queued
   one.** A run that says `queued` when nothing accepted it is the single state a
   reader cannot act on.
3. **Missing history is a 400 with the backfill command in it**, not a job that
   dies four minutes later in another process.
4. **`started_at` is null while a run waits.** The whole reason migration
   `d7a1c9f4b208` exists: stamping it at enqueue time would make every run's
   duration include its queue wait.
5. **`/compare` is a GET and is declared before `/{run_id}`.** As a POST it would
   be refused to the read-only session it is for (ADR 0009); declared second it
   would be swallowed as a run id.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING, Any

import httpx
import pytest

from atp_api.auth import Scope, Session
from atp_api.deps import (
    get_audit_sink,
    get_backtest_queue,
    get_backtest_repository,
    get_bar_repository,
    get_current_session,
    get_strategy_repository,
)
from atp_api.main import create_app
from atp_api.routers.backtests import MAX_COMPARE, MAX_SYMBOLS
from atp_core.audit.ports import Action
from atp_core.backtest.ports import BacktestProgress, BacktestQueueError, BacktestRunSpec
from atp_core.domain import Bar, Timeframe
from atp_core.errors import StrategyExistsError
from atp_core.persistence.backtests import new_run
from atp_core.strategy import registry
from atp_core.strategy.examples import rsi_mean_reversion
from atp_core.strategy.ports import StoredStrategy
from tests.fakes import (
    FakeBacktestQueue,
    FakeBacktestRunRepository,
    FakeStrategyRepository,
    RecordingAuditSink,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from fastapi import FastAPI

BACKTESTS = "/api/v1/backtests"

T0 = datetime(2026, 3, 2, 14, 30, tzinfo=UTC)
START = datetime(2024, 1, 1, tzinfo=UTC)
END = datetime(2025, 1, 1, tzinfo=UTC)

#: The one strategy this repository ships. Named rather than discovered, so a
#: test asserting on the registry fails loudly if it stops being registered
#: rather than passing vacuously.
SHIPPED = "sma_crossover"

#: The shipped rule set, stored as a `kind="ruleset"` row.
RULES_ID = "rsi_mean_reversion"


class FakeBarRepository:
    """Just enough `BarRepository` for the coverage pre-flight.

    Only `get_bars` is exercised — the handler asks whether history exists by
    reading it, which is the same query the worker will run and the only answer
    that actually proves the series is non-empty per symbol.
    """

    def __init__(self, series: dict[str, list[Bar]] | None = None) -> None:
        self.series = series if series is not None else {}
        self.asked: list[str] = []

    async def get_bars(
        self, symbol: str, timeframe: Timeframe, start: datetime, end: datetime
    ) -> list[Bar]:
        self.asked.append(symbol)
        return list(self.series.get(symbol, []))


def a_bar(symbol: str = "SPY") -> Bar:
    return Bar(
        symbol=symbol,
        ts=START,
        timeframe=Timeframe.D1,
        open=Decimal("100"),
        high=Decimal("101"),
        low=Decimal("99"),
        close=Decimal("100.5"),
        volume=Decimal("1000000"),
    )


def a_spec(strategy_id: str = SHIPPED, **overrides: Any) -> BacktestRunSpec:
    fields: dict[str, Any] = {
        "strategy_id": strategy_id,
        "symbols": ("SPY",),
        "start": START,
        "end": END,
        "timeframe": "1d",
        "starting_cash": "100000",
        "cost_model": "alpaca_equities",
        "params": {},
        "qty": "100",
    }
    fields.update(overrides)
    return BacktestRunSpec(**fields)


def a_request(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "strategy_id": SHIPPED,
        "symbols": ["SPY"],
        "start": START.isoformat(),
        "end": END.isoformat(),
        "timeframe": "1d",
        "starting_cash": "100000",
        "cost_model": "alpaca_equities",
        "qty": "100",
    }
    payload.update(overrides)
    return payload


@pytest.fixture
def runs() -> FakeBacktestRunRepository:
    return FakeBacktestRunRepository()


@pytest.fixture
def queue() -> FakeBacktestQueue:
    return FakeBacktestQueue()


@pytest.fixture
def bars() -> FakeBarRepository:
    return FakeBarRepository({"SPY": [a_bar()], "QQQ": [a_bar("QQQ")]})


@pytest.fixture
def strategies() -> FakeStrategyRepository:
    """Empty by default, which is the shape a coded strategy has here.

    `_validated_spec` only consults a row to ask whether it is declarative, so
    no row means the registry path — which is every case in this file except the
    rule-set ones, and is what those cases were already asserting before a row
    was consulted at all.
    """
    return FakeStrategyRepository()


@pytest.fixture
def audit() -> RecordingAuditSink:
    return RecordingAuditSink()


@pytest.fixture
def app(
    runs: FakeBacktestRunRepository,
    queue: FakeBacktestQueue,
    bars: FakeBarRepository,
    strategies: FakeStrategyRepository,
    audit: RecordingAuditSink,
) -> FastAPI:
    application = create_app()
    application.dependency_overrides[get_backtest_repository] = lambda: runs
    application.dependency_overrides[get_backtest_queue] = lambda: queue
    application.dependency_overrides[get_bar_repository] = lambda: bars
    application.dependency_overrides[get_strategy_repository] = lambda: strategies
    application.dependency_overrides[get_audit_sink] = lambda: audit
    application.dependency_overrides[get_current_session] = lambda: Session(
        "test-operator", Scope.FULL
    )
    return application


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as http_client:
        yield http_client


class TestQueueingARun:
    async def test_a_queued_run_is_recorded_then_enqueued(
        self,
        client: httpx.AsyncClient,
        runs: FakeBacktestRunRepository,
        queue: FakeBacktestQueue,
    ) -> None:
        """202 with a `queued` run, one row, one job.

        The ordering is asserted by both being true rather than by observing the
        sequence — a fake cannot see the order — and the failure case below is
        what pins the direction that matters.
        """
        response = await client.post(BACKTESTS, json=a_request())

        assert response.status_code == 202
        body = response.json()
        assert body["status"] == "queued"
        assert body["strategy_id"] == SHIPPED
        assert list(runs.runs) == [body["id"]]
        assert queue.enqueued == [body["id"]]

    async def test_a_waiting_run_has_not_started(self, client: httpx.AsyncClient) -> None:
        """`started_at` is null until a worker claims it.

        The reason migration `d7a1c9f4b208` split this from `queued_at`: the
        column used to be NOT NULL, so the only value the API could write was
        "now" — and every run's duration would then have included however long
        the queue was backed up.
        """
        body = (await client.post(BACKTESTS, json=a_request())).json()

        assert body["queued_at"] is not None
        assert body["started_at"] is None
        assert body["finished_at"] is None
        assert body["metrics"] is None

    async def test_the_request_is_echoed_back_on_the_run(self, client: httpx.AsyncClient) -> None:
        """A result read a week later has to say what it was a result *of*."""
        spec = (await client.post(BACKTESTS, json=a_request(symbols=["spy", "qqq"]))).json()["spec"]

        # Upper-cased and de-duplicated, because `symbol` is always an uppercase
        # ticker in this platform (CLAUDE.md §4).
        assert spec["symbols"] == ["SPY", "QQQ"]
        assert spec["cost_model"] == "alpaca_equities"
        assert spec["starting_cash"] == "100000"
        assert spec["qty"] == "100"

    async def test_money_on_the_spec_is_a_string_not_a_float(
        self, client: httpx.AsyncClient
    ) -> None:
        """A starting cash that survived JSON exactly (CLAUDE.md §1.1).

        A value chosen to be unrepresentable as a binary float: as a JSON number
        it comes back as 100000.10000000001 and every figure the run reports is
        then computed from that.
        """
        body = (await client.post(BACKTESTS, json=a_request(starting_cash="100000.1"))).json()

        assert body["spec"]["starting_cash"] == "100000.1"
        assert isinstance(body["spec"]["starting_cash"], str)

    async def test_a_zero_cost_run_is_allowed_and_never_the_default(
        self, client: httpx.AsyncClient
    ) -> None:
        """`zero` is reachable for debugging engine mechanics and is not the
        default — docs/BACKTESTING.md is unambiguous that a zero-cost result is
        not evidence about a strategy."""
        default = (await client.post(BACKTESTS, json=a_request())).json()
        assert default["spec"]["cost_model"] == "alpaca_equities"

        explicit = await client.post(BACKTESTS, json=a_request(cost_model="zero"))
        assert explicit.status_code == 202
        assert explicit.json()["spec"]["cost_model"] == "zero"

    async def test_a_request_that_names_no_stop_stores_none(
        self, client: httpx.AsyncClient
    ) -> None:
        """An old client sends none of these fields and gets the run it always
        got: only levels the strategy itself emits. Defaulting to `atr` here
        would change what a re-queued stored spec reports."""
        spec = (await client.post(BACKTESTS, json=a_request())).json()["spec"]

        assert spec["stop_type"] == ""
        assert spec["stop_value"] == ""
        assert spec["stop_bars"] == 0

    async def test_the_stop_travels_with_the_run_that_used_it(
        self, client: httpx.AsyncClient
    ) -> None:
        """Two runs of one strategy differing only in how entries were protected
        are different results, and a reader comparing them a week later cannot
        see that unless the protection is on the spec."""
        response = await client.post(
            BACKTESTS, json=a_request(stop_type="atr", stop_value="2.5", stop_period=20)
        )

        assert response.status_code == 202
        spec = response.json()["spec"]
        assert spec["stop_type"] == "atr"
        # A string, like every other decimal on a spec: the multiple becomes a
        # price distance in the engine, and a JSON float would carry binary
        # rounding into it (CLAUDE.md §1.1).
        assert spec["stop_value"] == "2.5"
        assert isinstance(spec["stop_value"], str)
        assert spec["stop_period"] == 20

    @pytest.mark.parametrize(
        ("override", "expected"),
        [
            ({"stop_type": "trailing"}, "stop_type must be one of"),
            ({"stop_type": "atr", "stop_value": "0"}, "stop_value must be positive"),
            # The cross-field rules, which only the resolver knows. They are a
            # 400 at the door rather than a failure four minutes into a queued
            # job, and they are not restated here — the API calls the same
            # function the worker builds the engine from, so the set it accepts
            # is the set that can actually run.
            ({"stop_type": "atr"}, "needs stop_value"),
            ({"stop_type": "time"}, "positive stop_bars"),
            ({"stop_type": "atr", "stop_value": "2", "stop_period": 0}, "stop_period"),
        ],
    )
    async def test_a_stop_that_cannot_be_built(
        self, client: httpx.AsyncClient, override: dict[str, Any], expected: str
    ) -> None:
        response = await client.post(BACKTESTS, json=a_request(**override))

        assert response.status_code == 400
        assert expected in response.json()["detail"]


class TestTheStrategyRowARunPointsAt:
    """Every registered class is backtestable, run by a worker or not.

    A run's `strategy_id` is a foreign key onto `strategies`, and the only
    things that ever wrote that table were a booting worker and the development
    seed script. So this endpoint refused a class the code registers, compiles
    and can run — with a 409 asking for a *trading* worker and broker
    credentials a backtest does not need — and the dashboard's picker, which can
    only offer what the endpoint will accept, showed whatever had happened to go
    through one of those. Usually one strategy.

    What is asserted here is the row's *content* as much as its existence. It is
    written on somebody's behalf without them asking for it, so it must not
    claim anything nobody said: not this run's params, not this run's symbols,
    and not a rung above `draft`.
    """

    async def test_a_class_no_worker_has_run_is_stored_and_queued(
        self,
        client: httpx.AsyncClient,
        runs: FakeBacktestRunRepository,
        queue: FakeBacktestQueue,
        strategies: FakeStrategyRepository,
    ) -> None:
        """The case the 409 used to be, and the whole point of the change."""
        assert await strategies.get_stored(SHIPPED) is None

        response = await client.post(BACKTESTS, json=a_request())

        assert response.status_code == 202, response.text
        assert strategies.create_calls == [SHIPPED]
        assert list(runs.runs) == [response.json()["id"]]
        assert queue.enqueued == [response.json()["id"]]

    async def test_the_row_records_the_class_and_its_declared_defaults(
        self, client: httpx.AsyncClient, strategies: FakeStrategyRepository
    ) -> None:
        """`params` are what the class runs on when nobody supplies any.

        Not `{}`, which would record a strategy configured with nothing when
        what it actually runs is a 20/50 crossover — the argument
        `registry.default_params` exists for.
        """
        await client.post(BACKTESTS, json=a_request())

        stored = await strategies.get_stored(SHIPPED)
        assert stored is not None
        assert stored.kind == "coded"
        assert stored.class_name == "SmaCrossover"
        assert stored.params == registry.default_params(registry.get(SHIPPED))
        assert stored.ruleset is None
        # The ratchet's first rung. Queueing a backtest authorises nothing.
        assert stored.state == "draft"

    async def test_the_row_does_not_claim_this_run_as_configuration(
        self, client: httpx.AsyncClient, strategies: FakeStrategyRepository
    ) -> None:
        """The run's params, symbols and timeframe belong to the run.

        Copying them onto the strategy would state, in the table a worker and
        the Strategies tab both read as configuration, that somebody chose to
        trade QQQ on 1h with a 5/40 crossover. Nobody did — they asked for one
        backtest, and sweeping params over a strategy is what a backtest's own
        `params` are *for*.
        """
        response = await client.post(
            BACKTESTS,
            json=a_request(
                symbols=["QQQ"], timeframe="1h", params={"fast_period": 5, "slow_period": 40}
            ),
        )

        assert response.status_code == 202, response.text
        stored = await strategies.get_stored(SHIPPED)
        assert stored is not None
        assert stored.universe == ()
        assert stored.timeframe == "1d"
        assert stored.params == registry.default_params(registry.get(SHIPPED))
        # And the run itself carries every one of them.
        spec = response.json()["spec"]
        assert (spec["symbols"], spec["timeframe"], spec["params"]["fast_period"]) == (
            ["QQQ"],
            "1h",
            5,
        )

    async def test_an_existing_row_is_left_exactly_as_it_was(
        self, client: httpx.AsyncClient, strategies: FakeStrategyRepository
    ) -> None:
        """A worker's row is configuration this endpoint does not own.

        Nothing is written when a row is already there — not even a touch of
        `updated_at`, which the API serves as `last_started_at` and which would
        then report that a worker started a strategy nobody started.
        """
        strategies.rows = [
            StoredStrategy(
                id=SHIPPED,
                name=SHIPPED,
                description="the one a worker booted",
                kind="coded",
                class_name="SmaCrossover",
                params={"fast_period": 5, "slow_period": 40},
                ruleset=None,
                state="paper",
                universe=("SPY",),
                timeframe="1h",
                risk_config={},
                created_at=T0 - timedelta(days=30),
                updated_at=T0 - timedelta(days=1),
            )
        ]

        response = await client.post(BACKTESTS, json=a_request())

        assert response.status_code == 202
        assert strategies.create_calls == []
        assert strategies.ensure_calls == []
        (stored,) = strategies.rows
        assert (stored.state, stored.params, stored.universe, stored.timeframe) == (
            "paper",
            {"fast_period": 5, "slow_period": 40},
            ("SPY",),
            "1h",
        )
        assert stored.updated_at == T0 - timedelta(days=1)

    async def test_a_row_written_by_somebody_else_first_is_not_a_failed_run(
        self,
        client: httpx.AsyncClient,
        runs: FakeBacktestRunRepository,
        strategies: FakeStrategyRepository,
    ) -> None:
        """The race between the read that found no row and the insert.

        A worker booting, or a second queue request for the same strategy, gets
        there first. The outcome that mattered — the row exists — is the one
        that happened, and `create` refuses on the name alone, so the row it
        lost to is a row for this same strategy.
        """
        strategies.create_error = StrategyExistsError("a strategy named 'sma_crossover' is stored")

        response = await client.post(BACKTESTS, json=a_request())

        assert response.status_code == 202, response.text
        assert list(runs.runs) == [response.json()["id"]]

    async def test_the_new_row_is_audited_against_the_session(
        self, client: httpx.AsyncClient, audit: RecordingAuditSink
    ) -> None:
        """A strategy's identity is being minted, and this is the only record of
        who decided it — from the cookie rather than from the body (ADR 0008).

        `via` is on the entry because a row that appeared without anybody
        visiting the Strategies tab is otherwise a puzzle.
        """
        await client.post(BACKTESTS, json=a_request())

        assert len(audit.entries) == 1
        entry = audit.entries[0]
        assert entry.action == Action.STRATEGY_CREATED
        assert entry.actor == "test-operator"
        assert entry.target == SHIPPED
        assert entry.detail["via"] == "backtest"

    async def test_a_run_that_is_refused_leaves_no_strategy_behind(
        self, client: httpx.AsyncClient, strategies: FakeStrategyRepository, bars: FakeBarRepository
    ) -> None:
        """The row is a side effect of queueing, so it waits until every refusal
        this handler can raise has had its chance. Missing history is the likely
        one, and a strategy stored by a request that was turned away would be a
        row nobody asked for and nothing points at."""
        bars.series = {}

        response = await client.post(BACKTESTS, json=a_request())

        assert response.status_code == 400
        assert strategies.create_calls == []
        assert strategies.rows == []

    async def test_a_name_nothing_registers_is_still_a_400(
        self, client: httpx.AsyncClient, strategies: FakeStrategyRepository
    ) -> None:
        """Unchanged, and the boundary of what this stores: a row is written for
        a class the registry has, never for a name a request invented."""
        response = await client.post(BACKTESTS, json=a_request(strategy_id="not_a_strategy"))

        assert response.status_code == 400
        assert "unknown strategy" in response.json()["detail"]
        assert strategies.create_calls == []


class TestRefusalsBeforeTheJobIsQueued:
    """Everything judgeable from the request is judged now, not in four minutes."""

    async def test_missing_history_names_the_backfill_command(
        self,
        client: httpx.AsyncClient,
        bars: FakeBarRepository,
        runs: FakeBacktestRunRepository,
        queue: FakeBacktestQueue,
    ) -> None:
        """The most valuable refusal in the file, and the one the stub asked for.

        A dead end would be a 400 saying "no data". This names the exact command
        that fixes it, as the CLI does.
        """
        bars.series = {}  # nothing backfilled

        response = await client.post(BACKTESTS, json=a_request())

        assert response.status_code == 400
        detail = response.json()["detail"]
        assert "backfill_bars.py" in detail
        assert "--symbols SPY" in detail
        # And nothing was queued or recorded: the refusal is complete.
        assert runs.runs == {}
        assert queue.enqueued == []

    async def test_one_symbol_without_history_refuses_the_whole_run(
        self, client: httpx.AsyncClient, bars: FakeBarRepository
    ) -> None:
        """A partial universe is not a smaller backtest, it is a different one —
        and one whose return nobody asked about."""
        bars.series = {"SPY": [a_bar()]}  # QQQ has nothing

        response = await client.post(BACKTESTS, json=a_request(symbols=["SPY", "QQQ"]))

        assert response.status_code == 400
        assert "QQQ" in response.json()["detail"]

    async def test_an_unknown_strategy_names_the_registered_ones(
        self, client: httpx.AsyncClient
    ) -> None:
        """The registry is populated by importing `strategy.examples`, which the
        router does. Without that import this would refuse every strategy with
        total confidence, so the assertion is on the *list* rather than on the
        refusal."""
        response = await client.post(BACKTESTS, json=a_request(strategy_id="nope"))

        assert response.status_code == 400
        assert SHIPPED in response.json()["detail"]

    async def test_naming_no_strategy_is_not_reported_as_an_unknown_one(
        self, client: httpx.AsyncClient, runs: FakeBacktestRunRepository
    ) -> None:
        """A blank name and a wrong name are different mistakes, and only one of
        them is about the registry.

        `registry.get("")` answers both the same way — a failed lookup, plus
        every registered name — so a caller that sent nothing was told what the
        registry contains. The dashboard sent exactly that while its picker
        seeded itself from a strategy list that had not loaded (#88), and the
        refusal pointed every reader at the registry instead of at the request.

        The assertion is that the registered names are *absent*: listing them
        here is what made the two mistakes indistinguishable.
        """
        response = await client.post(BACKTESTS, json=a_request(strategy_id=""))

        assert response.status_code == 400
        detail = response.json()["detail"]
        assert "strategy_id is empty" in detail
        assert SHIPPED not in detail
        assert "unknown strategy" not in detail
        # And nothing was recorded for a request that never named a strategy.
        assert runs.runs == {}

    async def test_a_padded_name_is_the_same_request(
        self, client: httpx.AsyncClient, runs: FakeBacktestRunRepository
    ) -> None:
        """Stripped like `symbols`, and for a sharper reason than tidiness:
        `strategy_id` becomes a foreign key onto `strategies.id`, so padding
        that survived this far would miss the row and be reported as a strategy
        no worker has ever run — a 409 about the wrong thing entirely."""
        response = await client.post(BACKTESTS, json=a_request(strategy_id=f"  {SHIPPED}  "))

        assert response.status_code == 202
        assert response.json()["spec"]["strategy_id"] == SHIPPED
        # Stored stripped too, not merely accepted: the row is what the worker
        # and every later reader resolve the strategy from.
        assert [run.spec.strategy_id for run in runs.runs.values()] == [SHIPPED]

    async def test_params_the_strategy_rejects_are_a_400_now(
        self, client: httpx.AsyncClient
    ) -> None:
        """A strategy validates its params in its constructor, so the endpoint
        builds one. Otherwise an impossible pair is a run that fails immediately
        and needs somebody to read a traceback to find out why."""
        response = await client.post(
            BACKTESTS, json=a_request(params={"fast_period": 30, "slow_period": 10})
        )

        assert response.status_code == 400
        detail = response.json()["detail"]
        assert "rejected its params" in detail
        # The strategy's own words, not a generic refusal: it says which pair.
        assert "fast_period" in detail

    @pytest.mark.parametrize(
        ("override", "expected"),
        [
            ({"symbols": []}, "symbols is empty"),
            ({"strategy_id": ""}, "strategy_id is empty"),
            ({"strategy_id": "   "}, "strategy_id is empty"),
            ({"timeframe": "3d"}, "timeframe must be one of"),
            ({"cost_model": "free"}, "cost_model must be one of"),
            ({"starting_cash": "0"}, "starting_cash must be positive"),
            ({"qty": "0"}, "qty must be positive"),
            ({"start": END.isoformat(), "end": START.isoformat()}, "start must be before end"),
        ],
    )
    async def test_a_request_that_cannot_run(
        self, client: httpx.AsyncClient, override: dict[str, Any], expected: str
    ) -> None:
        response = await client.post(BACKTESTS, json=a_request(**override))

        assert response.status_code == 400
        assert expected in response.json()["detail"]

    async def test_a_naive_datetime_is_refused_at_the_boundary(
        self, client: httpx.AsyncClient
    ) -> None:
        """Rule §1.2. A naive start compared against aware bar timestamps raises
        somewhere far less legible than here."""
        response = await client.post(
            BACKTESTS, json=a_request(start="2024-01-01T00:00:00", end="2025-01-01T00:00:00")
        )

        assert response.status_code == 400
        assert "timezone-aware" in response.json()["detail"]

    async def test_too_many_symbols(self, client: httpx.AsyncClient) -> None:
        response = await client.post(
            BACKTESTS, json=a_request(symbols=[f"S{i}" for i in range(MAX_SYMBOLS + 1)])
        )

        assert response.status_code == 400
        assert str(MAX_SYMBOLS) in response.json()["detail"]


class TestWhenTheQueueIsNotThere:
    async def test_a_run_nothing_accepted_is_failed_not_left_queued(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        runs: FakeBacktestRunRepository,
    ) -> None:
        """503, and the row says why.

        This is the direction that matters. A row with no job left saying
        `queued` would sit on the screen forever waiting for a worker that was
        never told about it — the one state a reader cannot act on. Failing it
        here makes the outcome legible and re-queueable.
        """
        app.dependency_overrides[get_backtest_queue] = lambda: FakeBacktestQueue(
            enqueue_error=BacktestQueueError("connection refused")
        )

        response = await client.post(BACKTESTS, json=a_request())

        assert response.status_code == 503
        assert "not reachable" in response.json()["detail"]
        (stored,) = runs.runs.values()
        assert stored.status == "failed"
        assert "could not be queued" in (stored.error or "")

    async def test_a_strategy_with_no_row_is_a_409_that_says_where_to_look(
        self, client: httpx.AsyncClient, runs: FakeBacktestRunRepository
    ) -> None:
        """The realistic integrity failure: the registry knows the class and
        `strategies` has no row because no worker has ever loaded it.

        A constraint name would be useless. The message points at the screen that
        exists to show exactly this gap.
        """
        runs.create_error = RuntimeError("ForeignKeyViolation: strategies")

        response = await client.post(BACKTESTS, json=a_request())

        assert response.status_code == 409
        assert "Strategies tab" in response.json()["detail"]


class TestReadingRuns:
    async def test_newest_first_and_filtered_by_strategy(
        self, client: httpx.AsyncClient, runs: FakeBacktestRunRepository
    ) -> None:
        await runs.create(new_run("old", a_spec(), queued_at=T0 - timedelta(hours=2)))
        await runs.create(new_run("new", a_spec(), queued_at=T0))
        await runs.create(new_run("other", a_spec("elsewhere"), queued_at=T0))

        every = (await client.get(BACKTESTS)).json()
        assert [run["id"] for run in every["runs"]] == ["other", "new", "old"]

        filtered = (await client.get(BACKTESTS, params={"strategy_id": SHIPPED})).json()
        assert [run["id"] for run in filtered["runs"]] == ["new", "old"]

    async def test_a_full_page_says_so(
        self, client: httpx.AsyncClient, runs: FakeBacktestRunRepository
    ) -> None:
        """A list that stops at exactly the limit looks identical to one that
        ended, and only one of them means "this is everything"."""
        for index in range(3):
            await runs.create(
                new_run(f"r{index}", a_spec(), queued_at=T0 + timedelta(minutes=index))
            )

        assert (await client.get(BACKTESTS, params={"limit": 2})).json()["limit_reached"] is True
        assert (await client.get(BACKTESTS, params={"limit": 5})).json()["limit_reached"] is False

    async def test_an_unknown_run_is_a_404(self, client: httpx.AsyncClient) -> None:
        assert (await client.get(f"{BACKTESTS}/nope")).status_code == 404

    async def test_progress_is_attached_while_a_run_is_in_flight(
        self,
        client: httpx.AsyncClient,
        runs: FakeBacktestRunRepository,
        queue: FakeBacktestQueue,
    ) -> None:
        """A bar, not a spinner — which is what the task's docstring asked for."""
        await runs.create(new_run("r1", a_spec(), queued_at=T0))
        await runs.mark_running("r1", at=T0)
        await queue.report(BacktestProgress("r1", bars_done=125, bars_total=500, at=T0))

        body = (await client.get(f"{BACKTESTS}/r1")).json()

        assert body["status"] == "running"
        assert body["progress"]["bars_done"] == 125
        assert body["progress"]["bars_total"] == 500
        # Computed server-side: every client would compute it identically, and a
        # total of zero makes it a division rather than a fraction.
        assert body["progress"]["fraction"] == pytest.approx(0.25)

    async def test_a_finished_run_carries_no_progress(
        self,
        client: httpx.AsyncClient,
        runs: FakeBacktestRunRepository,
        queue: FakeBacktestQueue,
    ) -> None:
        """A finished run's progress is its result.

        Asserted with a progress record deliberately still readable, so this
        pins the handler's choice not to ask rather than the store's TTL.
        """
        await runs.create(new_run("r1", a_spec(), queued_at=T0))
        await queue.report(BacktestProgress("r1", bars_done=500, bars_total=500, at=T0))
        await runs.finish(
            "r1", at=T0, metrics={"sharpe": 1.2, "num_trades": 40}, equity_curve=[], trades=[]
        )

        body = (await client.get(f"{BACKTESTS}/r1")).json()

        assert body["status"] == "done"
        assert body["progress"] is None

    async def test_an_implausible_result_says_so_on_the_run(
        self, client: httpx.AsyncClient, runs: FakeBacktestRunRepository
    ) -> None:
        """docs/BACKTESTING.md's own two thresholds, server-side.

        A number a human has already read is a number they have already believed,
        so the caveat travels with the result rather than being left to the
        client to know about.
        """
        await runs.create(new_run("r1", a_spec(), queued_at=T0))
        await runs.finish(
            "r1", at=T0, metrics={"sharpe": 6.0, "num_trades": 4}, equity_curve=[], trades=[]
        )

        warnings = (await client.get(f"{BACKTESTS}/r1")).json()["warnings"]

        assert any("only 4 trades" in w for w in warnings)
        assert any("bug until proven otherwise" in w for w in warnings)

    async def test_a_credible_result_is_not_caveated(
        self, client: httpx.AsyncClient, runs: FakeBacktestRunRepository
    ) -> None:
        """The converse. A warning on every run would be a warning nobody reads."""
        await runs.create(new_run("r1", a_spec(), queued_at=T0))
        await runs.finish(
            "r1", at=T0, metrics={"sharpe": 1.1, "num_trades": 120}, equity_curve=[], trades=[]
        )

        assert (await client.get(f"{BACKTESTS}/r1")).json()["warnings"] == []


class TestTradesAndCurve:
    async def test_trades_and_curve_are_served_from_the_stored_run(
        self, client: httpx.AsyncClient, runs: FakeBacktestRunRepository
    ) -> None:
        await runs.create(new_run("r1", a_spec(), queued_at=T0))
        await runs.finish(
            "r1",
            at=T0,
            metrics={"sharpe": 1.0, "num_trades": 1},
            equity_curve=[[T0.isoformat(), "100500.25"]],
            trades=[{"symbol": "SPY", "net_pnl": "500.25", "exit_reason": "stop_loss"}],
        )

        trades = (await client.get(f"{BACKTESTS}/r1/trades")).json()
        curve = (await client.get(f"{BACKTESTS}/r1/equity-curve")).json()

        assert trades["trades"][0]["exit_reason"] == "stop_loss"
        # Money as strings, all the way to the pixels (docs/DASHBOARD.md).
        assert trades["trades"][0]["net_pnl"] == "500.25"
        assert curve["points"] == [[T0.isoformat(), "100500.25"]]

    async def test_a_run_with_no_trades_is_an_empty_list_not_a_404(
        self, client: httpx.AsyncClient, runs: FakeBacktestRunRepository
    ) -> None:
        """A `done` run that took no trades is a result. The 404 is reserved for
        a run that does not exist, which is a different sentence."""
        await runs.create(new_run("r1", a_spec(), queued_at=T0))
        await runs.finish("r1", at=T0, metrics={"num_trades": 0}, equity_curve=[], trades=[])

        response = await client.get(f"{BACKTESTS}/r1/trades")

        assert response.status_code == 200
        assert response.json()["trades"] == []

    async def test_trades_for_an_unknown_run_is_a_404(self, client: httpx.AsyncClient) -> None:
        assert (await client.get(f"{BACKTESTS}/nope/trades")).status_code == 404
        assert (await client.get(f"{BACKTESTS}/nope/equity-curve")).status_code == 404


class TestComparing:
    async def _two_finished(self, runs: FakeBacktestRunRepository) -> None:
        for run_id, sharpe in (("a", 1.5), ("b", 0.4)):
            await runs.create(new_run(run_id, a_spec(), queued_at=T0))
            await runs.finish(
                run_id,
                at=T0,
                metrics={"sharpe": sharpe, "num_trades": 50},
                equity_curve=[],
                trades=[],
            )

    async def test_metrics_are_pivoted_by_metric_then_run(
        self, client: httpx.AsyncClient, runs: FakeBacktestRunRepository
    ) -> None:
        """A comparison table is read by row, and every client would otherwise
        pivot the same list identically."""
        await self._two_finished(runs)

        body = (await client.get(f"{BACKTESTS}/compare", params={"run_ids": ["a", "b"]})).json()

        assert body["metrics"]["sharpe"] == {"a": 1.5, "b": 0.4}
        assert [run["id"] for run in body["runs"]] == ["a", "b"]

    async def test_every_comparison_carries_the_overfitting_warning(
        self, client: httpx.AsyncClient, runs: FakeBacktestRunRepository
    ) -> None:
        """On every comparison, not just large ones. The docstring is read by
        whoever writes the client; this is read by whoever is about to promote a
        strategy."""
        await self._two_finished(runs)

        body = (await client.get(f"{BACKTESTS}/compare", params={"run_ids": ["a", "b"]})).json()

        assert "overfitting" in body["overfitting_warning"].lower()

    async def test_comparing_more_than_the_limit_is_refused_with_the_reason(
        self, client: httpx.AsyncClient
    ) -> None:
        """The limit *is* the argument: an endpoint that cheerfully ranked fifty
        runs would be tooling for the mistake docs/BACKTESTING.md warns about."""
        response = await client.get(
            f"{BACKTESTS}/compare",
            params={"run_ids": [f"r{i}" for i in range(MAX_COMPARE + 1)]},
        )

        assert response.status_code == 400
        assert "overfitting" in response.json()["detail"]

    async def test_an_unfinished_run_cannot_be_compared(
        self, client: httpx.AsyncClient, runs: FakeBacktestRunRepository
    ) -> None:
        """A column of nulls beside runs that produced answers, in a table read
        to pick a winner, is worse than a refusal."""
        await self._two_finished(runs)
        await runs.create(new_run("waiting", a_spec(), queued_at=T0))

        response = await client.get(f"{BACKTESTS}/compare", params={"run_ids": ["a", "waiting"]})

        assert response.status_code == 400
        assert "waiting" in response.json()["detail"]

    async def test_comparing_an_unknown_run_is_a_404(self, client: httpx.AsyncClient) -> None:
        assert (
            await client.get(f"{BACKTESTS}/compare", params={"run_ids": ["nope"]})
        ).status_code == 404

    async def test_compare_is_not_swallowed_by_the_run_id_route(
        self, client: httpx.AsyncClient, runs: FakeBacktestRunRepository
    ) -> None:
        """The route-ordering trap, pinned.

        FastAPI matches in registration order, so `/compare` declared after
        `/{run_id}` would be handled by `get_backtest` with `run_id="compare"`
        and answer 404 — a working endpoint made unreachable by the order of two
        decorators. A 400 for the empty list proves `compare` reached its own
        handler.
        """
        response = await client.get(f"{BACKTESTS}/compare")

        # 422 from FastAPI's own required-parameter check, never a 404 from the
        # id route mistaking "compare" for a run id.
        assert response.status_code == 422


class TestTheQueueIsAnApplicationResource:
    async def test_without_a_lifespan_the_queue_is_a_503(self, app: FastAPI) -> None:
        """The adapter is read off `app.state`, not built per request.

        It owns an arq connection pool, and `deps.py`'s own rule is that a pool
        built in a dependency has nowhere to be closed — per request it would be
        a pool opened and abandoned on every `POST /backtests`. `lifespan`
        constructs it and closes it, so a test driving the app over ASGI without
        one gets the same honest 503 that Redis and the kill switch give.
        """
        app.dependency_overrides.pop(get_backtest_queue)

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.get(BACKTESTS)

        assert response.status_code == 503
        assert "job queue" in response.json()["detail"]


class TestAuthorisation:
    async def test_queueing_a_run_is_an_act_and_needs_a_full_session(
        self, app: FastAPI, client: httpx.AsyncClient
    ) -> None:
        """Starting minutes of compute on the shared queue is an act."""
        app.dependency_overrides[get_current_session] = lambda: Session("reader", Scope.READ)

        assert (await client.post(BACKTESTS, json=a_request())).status_code == 403

    async def test_a_read_only_session_may_still_read_and_compare(
        self, app: FastAPI, client: httpx.AsyncClient, runs: FakeBacktestRunRepository
    ) -> None:
        """ADR 0009 — authorisation is about the act, and comparing performs
        none. This is why `/compare` is a GET rather than a third entry in
        `deps.READ_ONLY_MAY_CALL`, whose one entry is there for a domain rule
        about halting."""
        app.dependency_overrides[get_current_session] = lambda: Session("reader", Scope.READ)
        await runs.create(new_run("a", a_spec(), queued_at=T0))
        await runs.finish("a", at=T0, metrics={"sharpe": 1.0}, equity_curve=[], trades=[])

        assert (await client.get(BACKTESTS)).status_code == 200
        assert (
            await client.get(f"{BACKTESTS}/compare", params={"run_ids": ["a"]})
        ).status_code == 200


class TestQueueingARuleSet:
    """A `kind="ruleset"` row reaches a run by having its rules copied onto it.

    `strategy_id` still carries the foreign key and answers "which strategy is
    this a run of". The snapshot answers "what rules actually ran", and those
    stop being the same question the first time somebody edits a rule set.
    """

    def _stored(self, **overrides: Any) -> StoredStrategy:
        fields: dict[str, Any] = {
            "id": RULES_ID,
            "name": RULES_ID,
            "description": "",
            "kind": "ruleset",
            "class_name": None,
            "params": {},
            "ruleset": rsi_mean_reversion().model_dump(mode="json"),
            "state": "draft",
            "universe": ("SPY", "QQQ", "IWM"),
            "timeframe": "1d",
            "risk_config": {},
            "created_at": T0,
            "updated_at": T0,
        }
        fields.update(overrides)
        return StoredStrategy(**fields)

    async def test_the_rules_are_copied_onto_the_run(
        self,
        client: httpx.AsyncClient,
        runs: FakeBacktestRunRepository,
        strategies: FakeStrategyRepository,
    ) -> None:
        strategies.rows = [self._stored()]

        response = await client.post(BACKTESTS, json=a_request(strategy_id=RULES_ID))

        assert response.status_code == 202
        stored_run = runs.runs[response.json()["id"]]
        assert stored_run.spec.strategy_id == RULES_ID
        assert stored_run.spec.ruleset == rsi_mean_reversion().model_dump(mode="json")

    async def test_editing_the_strategy_afterwards_does_not_change_the_run(
        self,
        client: httpx.AsyncClient,
        runs: FakeBacktestRunRepository,
        strategies: FakeStrategyRepository,
    ) -> None:
        """The property the snapshot exists for.

        A rule set is editable, so a run that recorded only `strategy_id` would
        replay differently after an edit — two different results filed under one
        name, with nothing to say which rules produced which.
        """
        strategies.rows = [self._stored()]
        queued = (await client.post(BACKTESTS, json=a_request(strategy_id=RULES_ID))).json()

        edited = rsi_mean_reversion().model_dump(mode="json")
        edited["entry_long"]["all"][0]["right"]["value"] = "70"
        strategies.rows = [self._stored(ruleset=edited)]

        assert runs.runs[queued["id"]].spec.ruleset == rsi_mean_reversion().model_dump(mode="json")

    async def test_a_coded_strategy_records_no_rules(
        self, client: httpx.AsyncClient, runs: FakeBacktestRunRepository
    ) -> None:
        """The registry path, unchanged. `None` rather than `{}`, so nothing
        downstream reads an empty dict as "compile this"."""
        queued = (await client.post(BACKTESTS, json=a_request())).json()
        assert runs.runs[queued["id"]].spec.ruleset is None

    async def test_a_declarative_row_with_no_rules_is_refused(
        self, client: httpx.AsyncClient, strategies: FakeStrategyRepository
    ) -> None:
        """Nothing can run it, and the registry cannot stand in: a `ruleset` row
        exists because there is no class of that name."""
        strategies.rows = [self._stored(ruleset=None)]

        response = await client.post(BACKTESTS, json=a_request(strategy_id=RULES_ID))

        assert response.status_code == 400
        assert "no rules recorded" in response.json()["detail"]

    async def test_params_sent_for_a_rule_set_are_refused_not_ignored(
        self, client: httpx.AsyncClient, strategies: FakeStrategyRepository
    ) -> None:
        """A caller who sent params believes they do something."""
        strategies.rows = [self._stored()]

        response = await client.post(
            BACKTESTS, json=a_request(strategy_id=RULES_ID, params={"fast_period": 5})
        )

        assert response.status_code == 400
        assert "takes no params" in response.json()["detail"]

    async def test_symbols_outside_the_universe_are_refused_up_front(
        self, client: httpx.AsyncClient, strategies: FakeStrategyRepository, bars: FakeBarRepository
    ) -> None:
        """Otherwise the run completes, takes no trades, and reports a flat
        curve — indistinguishable from a strategy that never signalled, which is
        the least legible result this platform can produce."""
        strategies.rows = [self._stored(ruleset=rsi_mean_reversion().model_dump(mode="json"))]
        bars.series["TSLA"] = [a_bar("TSLA")]

        response = await client.post(
            BACKTESTS, json=a_request(strategy_id=RULES_ID, symbols=["TSLA"])
        )

        assert response.status_code == 400
        detail = response.json()["detail"]
        assert "outside" in detail
        assert "TSLA" in detail

    async def test_stored_rules_that_cannot_compile_are_a_400_not_a_failed_run(
        self, client: httpx.AsyncClient, strategies: FakeStrategyRepository
    ) -> None:
        """The same argument the coverage check makes: a refusal here beats a row
        that says `failed` four minutes later from another process."""
        broken = rsi_mean_reversion().model_dump(mode="json")
        broken["entry_long"]["all"][0]["left"]["indicator"] = "vwap"
        strategies.rows = [self._stored(ruleset=broken)]

        response = await client.post(BACKTESTS, json=a_request(strategy_id=RULES_ID))

        assert response.status_code == 400
        assert "does not compile" in response.json()["detail"]
