"""`GET /api/v1/analytics/*` over ASGI.

A unit test rather than an integration one, for the same reason the dashboard's
is: both sources the handlers read — the order repository and the bar
repository — are behind ports, so the whole route runs against fakes with no
database (CLAUDE.md §1.7).

What is worth holding here is not the arithmetic, which
`test_analytics_performance.py` covers directly. It is the *windowing*, which is
the part that is easy to get subtly wrong and impossible to notice from a
screen: which trades a period contains, whether the read that feeds the
reconstruction is windowed too (it must not be), and whether a column of nulls
means "we measured nothing" or "we did not look".
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING, Any

import httpx
import pytest

from atp_api.auth import Scope, Session
from atp_api.deps import (
    get_audit_reader,
    get_backtest_repository,
    get_bar_repository,
    get_clock,
    get_current_session,
    get_order_repository,
)
from atp_api.main import create_app
from atp_core.backtest.ports import BacktestRunSpec, StoredBacktestRun
from atp_core.clock import SimulatedClock
from atp_core.config import Settings, get_settings
from atp_core.domain import Bar, Fill, Order, Side, Timeframe
from atp_core.execution.idempotency import ENTRY, EXIT, STOP_LOSS, TAKE_PROFIT
from tests.fakes import FakeBacktestRunRepository

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from fastapi import FastAPI

PERFORMANCE = "/api/v1/analytics/performance"
TRADES = "/api/v1/analytics/trades"
ATTRIBUTION = "/api/v1/analytics/attribution"
LIVE_VS_BACKTEST = "/api/v1/analytics/live-vs-backtest"

SYMBOL = "SPY"
#: A Monday. Every trade below is placed relative to it so the weekday
#: attribution has a stable answer.
T0 = datetime(2026, 3, 2, 14, 30, tzinfo=UTC)

#: "Now" for every request below. Pinned rather than ambient: the default
#: window is the last 30 days, so a suite reading the real clock would stop
#: finding its own fixtures 30 days after they were written.
NOW = datetime(2026, 3, 20, 12, 0, tzinfo=UTC)

#: A trade older than the 30-day default lookback the other endpoints on this
#: router apply. `/live-vs-backtest` deliberately does not apply one, and this
#: is what distinguishes the two.
OLD = datetime(2026, 1, 6, 14, 30, tzinfo=UTC)

RUN_ID = "run-1"
BT_START = datetime(2026, 1, 5, tzinfo=UTC)
BT_END = datetime(2026, 3, 1, tzinfo=UTC)

#: A plausible stored metric set: 40 trades and a Sharpe of 1.2, so
#: `backtest.runner.suspicious` finds nothing to say about it by default and a
#: test that wants a warning has to ask for one.
BACKTEST_METRICS: dict[str, float] = {
    "total_return": 0.35,
    "cagr": 0.21,
    "sharpe": 1.2,
    "sortino": 1.6,
    "calmar": 1.05,
    "max_drawdown": -0.2,
    "max_drawdown_duration_days": 14,
    "volatility": 0.18,
    "win_rate": 0.55,
    "profit_factor": 1.7,
    "expectancy": 42.0,
    "avg_win": 180.0,
    "avg_loss": -110.0,
    "largest_win": 900.0,
    "largest_loss": -400.0,
    "num_trades": 40,
    "avg_holding_period_hours": 30.0,
    "exposure_pct": 0.4,
    "turnover": 6.0,
}

#: Tells "the caller did not name any metrics" apart from "the caller named
#: none". A stored run legitimately has both shapes and they mean different
#: things to the endpoint.
_UNSET: Any = object()


def pinned_settings() -> Settings:
    """Settings that do not depend on the shell the suite is run from.

    `ATP_RUN_MODE` reaches `filled_orders` as its run-mode filter, so an ambient
    value would change which rows the fake is asked for.
    """
    return Settings(ATP_RUN_MODE="backtest", _env_file=None)


def at(hours: float) -> datetime:
    return T0 + timedelta(hours=hours)


def order(
    side: Side,
    qty: str,
    price: str,
    ts: datetime,
    purpose: str,
    *,
    symbol: str = SYMBOL,
    strategy_id: str = "sma",
) -> Order:
    built = Order(
        symbol=symbol,
        side=side,
        qty=Decimal(qty),
        strategy_id=strategy_id,
        purpose=purpose,
        created_at=ts,
    )
    built.apply_fill(
        Fill(order_id=built.id, ts=ts, qty=Decimal(qty), price=Decimal(price), fee=Decimal(0))
    )
    return built


def round_trip(
    entry_at: datetime,
    exit_at: datetime,
    *,
    entry: str = "100",
    exit_price: str = "110",
    purpose: str = EXIT,
    symbol: str = SYMBOL,
    strategy_id: str = "sma",
) -> list[Order]:
    return [
        order(Side.BUY, "10", entry, entry_at, ENTRY, symbol=symbol, strategy_id=strategy_id),
        order(
            Side.SELL, "10", exit_price, exit_at, purpose, symbol=symbol, strategy_id=strategy_id
        ),
    ]


def a_spec(strategy_id: str = "sma", **overrides: Any) -> BacktestRunSpec:
    fields: dict[str, Any] = {
        "strategy_id": strategy_id,
        "symbols": (SYMBOL,),
        "start": BT_START,
        "end": BT_END,
        "timeframe": "1d",
        "starting_cash": "100000",
        "cost_model": "alpaca_equities",
        "params": {},
        "qty": "100",
    }
    fields.update(overrides)
    return BacktestRunSpec(**fields)


async def store(
    runs: FakeBacktestRunRepository,
    *,
    spec: BacktestRunSpec | None = None,
    status: str = "done",
    metrics: Any = _UNSET,
) -> None:
    """Put one run on record, in whatever state a test needs it in.

    Written straight into the fake rather than driven through `finish`, because
    the states worth testing here include the ones `finish` refuses to produce —
    a `done` run whose metric set predates a field, a `failed` run somebody
    asked to compare against.
    """
    if metrics is _UNSET:
        metrics = dict(BACKTEST_METRICS) if status == "done" else None
    await runs.create(
        StoredBacktestRun(
            id=RUN_ID,
            spec=spec or a_spec(),
            status=status,
            error="it broke" if status == "failed" else None,
            queued_at=BT_START,
            started_at=BT_START,
            finished_at=BT_END if status in ("done", "failed") else None,
            metrics=metrics,
        )
    )


class RecordingOrderRepo:
    """A `FakeOrderRepository` that also remembers how it was queried.

    The `until`/`strategy_id` it was called with is the assertion for the
    windowing rule: reconstruction has to read from the beginning of the
    account, because a window applied to the *orders* would present every
    position opened before it as an exit with no entry.
    """

    def __init__(self, orders: list[Order] | None = None) -> None:
        self.orders = orders or []
        self.calls: list[dict[str, Any]] = []

    async def save(self, order: Order, *, run_mode: object) -> None:  # pragma: no cover
        raise AssertionError("the API never writes an order")

    async def open_orders(self, run_mode: object) -> list[Order]:  # pragma: no cover
        return []

    async def recent_orders(
        self,
        run_mode: object,
        *,
        status: object = None,
        symbol: str | None = None,
        strategy_id: str | None = None,
        since: datetime | None = None,
        limit: int = 100,
    ) -> list[Order]:
        self.calls.append({"since": since, "limit": limit})
        return [
            o
            for o in self.orders
            if o.created_at is not None and (since is None or o.created_at >= since)
        ][:limit]

    async def filled_orders(
        self, run_mode: object, *, until: datetime, strategy_id: str | None = None
    ) -> list[Order]:
        self.calls.append({"until": until, "strategy_id": strategy_id})
        return [
            o
            for o in self.orders
            if o.created_at is not None
            and o.created_at <= until
            and (strategy_id is None or o.strategy_id == strategy_id)
        ]


class FakeBars:
    def __init__(self, bars: dict[str, list[Bar]] | None = None) -> None:
        self.bars = bars or {}
        self.queried: list[str] = []
        self.fail_for: set[str] = set()

    async def upsert_bars(self, bars: list[Bar]) -> int:  # pragma: no cover
        return 0

    async def get_bars(
        self, symbol: str, timeframe: Timeframe, start: datetime, end: datetime
    ) -> list[Bar]:
        self.queried.append(symbol)
        if symbol in self.fail_for:
            raise ConnectionError(f"cannot read bars for {symbol}")
        return [b for b in self.bars.get(symbol, []) if start <= b.ts <= end]

    async def get_last_n_bars(
        self, symbol: str, timeframe: Timeframe, n: int
    ) -> list[Bar]:  # pragma: no cover
        return []

    async def find_gaps(self, *args: Any, **kwargs: Any) -> list[Any]:  # pragma: no cover
        return []

    async def stored_series(self) -> list[tuple[str, Timeframe]]:  # pragma: no cover
        return []


def bar(ts: datetime, low: str, high: str, *, symbol: str = SYMBOL) -> Bar:
    return Bar(
        symbol=symbol,
        ts=ts,
        timeframe=Timeframe.D1,
        open=Decimal(low),
        high=Decimal(high),
        low=Decimal(low),
        close=Decimal(high),
        volume=Decimal("1000000"),
    )


@pytest.fixture
def orders() -> RecordingOrderRepo:
    return RecordingOrderRepo()


@pytest.fixture
def bars() -> FakeBars:
    return FakeBars()


@pytest.fixture
def runs() -> FakeBacktestRunRepository:
    return FakeBacktestRunRepository()


@pytest.fixture
def app(orders: RecordingOrderRepo, bars: FakeBars, runs: FakeBacktestRunRepository) -> FastAPI:
    application = create_app()
    application.dependency_overrides[get_settings] = pinned_settings
    application.dependency_overrides[get_clock] = lambda: SimulatedClock(NOW)
    application.dependency_overrides[get_order_repository] = lambda: orders
    application.dependency_overrides[get_bar_repository] = lambda: bars
    application.dependency_overrides[get_backtest_repository] = lambda: runs
    # These are read routes and are not about who is asking;
    # `test_api_contract.py` holds the enforcement itself against every route.
    application.dependency_overrides[get_current_session] = lambda: Session(
        "test-operator", Scope.FULL
    )
    return application


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
        yield http


class TestTheWindow:
    @pytest.mark.asyncio
    async def test_the_order_read_is_not_windowed_at_the_start(
        self, client: httpx.AsyncClient, orders: RecordingOrderRepo
    ) -> None:
        """The rule the whole reconstruction depends on.

        FIFO matching pairs an exit with the entry it closes, so an entry
        outside the read is an exit with no entry — and the tempting reading of
        that is a short that was never opened, which inverts the sign of its
        P&L.
        """
        await client.get(TRADES, params={"start": "2026-03-10", "end": "2026-03-20"})

        assert len(orders.calls) == 1
        # Bounded at the end only. Nothing named a start.
        assert orders.calls[0]["until"].date() == date(2026, 3, 20)

    @pytest.mark.asyncio
    async def test_trades_are_filtered_on_the_exit_not_the_entry(
        self, client: httpx.AsyncClient, orders: RecordingOrderRepo
    ) -> None:
        """A round trip belongs to the period whose P&L it landed in.

        A position opened in March and closed in August made its money in
        August; attributing it to March would put a realised gain in a month
        whose reported total does not contain it.
        """
        orders.orders = round_trip(
            datetime(2026, 3, 2, 14, 30, tzinfo=UTC),
            datetime(2026, 8, 3, 14, 30, tzinfo=UTC),
        )

        march = await client.get(TRADES, params={"start": "2026-03-01", "end": "2026-03-31"})
        august = await client.get(TRADES, params={"start": "2026-08-01", "end": "2026-08-31"})

        assert march.json()["trades"] == []
        assert len(august.json()["trades"]) == 1

    @pytest.mark.asyncio
    async def test_the_end_date_is_inclusive(
        self, client: httpx.AsyncClient, orders: RecordingOrderRepo
    ) -> None:
        """A request for "through the nineteenth" that dropped the nineteenth
        would leave today's trades out of every report asked for today."""
        orders.orders = round_trip(at(0), datetime(2026, 3, 19, 20, 0, tzinfo=UTC))

        response = await client.get(TRADES, params={"start": "2026-03-01", "end": "2026-03-19"})

        assert len(response.json()["trades"]) == 1

    @pytest.mark.asyncio
    async def test_a_backwards_range_is_refused(self, client: httpx.AsyncClient) -> None:
        response = await client.get(TRADES, params={"start": "2026-03-20", "end": "2026-03-10"})
        assert response.status_code == 422


class TestTrades:
    @pytest.mark.asyncio
    async def test_a_completed_round_trip_is_returned(
        self, client: httpx.AsyncClient, orders: RecordingOrderRepo
    ) -> None:
        orders.orders = round_trip(at(0), at(5))

        trades = (await client.get(TRADES)).json()["trades"]

        assert len(trades) == 1
        assert trades[0]["symbol"] == SYMBOL
        assert trades[0]["side"] == "long"
        assert trades[0]["exit_reason"] == "signal"

    @pytest.mark.asyncio
    async def test_money_crosses_the_wire_as_a_string(
        self, client: httpx.AsyncClient, orders: RecordingOrderRepo
    ) -> None:
        """Rule §1.1, all the way to the pixels.

        The dashboard performs no arithmetic on money, so nothing downstream
        parses one back — but a float on the wire has already lost the exactness
        before anyone decides not to use it.
        """
        orders.orders = round_trip(at(0), at(5))

        trade = (await client.get(TRADES)).json()["trades"][0]

        for field in ("entry_price", "exit_price", "gross_pnl", "net_pnl", "fees", "qty"):
            assert isinstance(trade[field], str), field

    @pytest.mark.asyncio
    async def test_newest_first(
        self, client: httpx.AsyncClient, orders: RecordingOrderRepo
    ) -> None:
        orders.orders = [
            *round_trip(at(0), at(1)),
            *round_trip(at(50), at(51)),
        ]

        trades = (await client.get(TRADES)).json()["trades"]

        assert trades[0]["exit_ts"] > trades[1]["exit_ts"]

    @pytest.mark.asyncio
    async def test_the_cap_keeps_the_most_recent(
        self, client: httpx.AsyncClient, orders: RecordingOrderRepo
    ) -> None:
        """Sorted before capped, so a limit keeps the latest rather than
        whichever the reconstruction happened to close first."""
        orders.orders = [
            o for index in range(5) for o in round_trip(at(index * 10), at(index * 10 + 1))
        ]

        trades = (await client.get(TRADES, params={"limit": 2})).json()["trades"]

        assert len(trades) == 2
        assert trades[0]["exit_ts"] == at(41).isoformat().replace("+00:00", "Z")

    @pytest.mark.asyncio
    async def test_a_strategy_filter_reaches_the_repository(
        self, client: httpx.AsyncClient, orders: RecordingOrderRepo
    ) -> None:
        await client.get(TRADES, params={"strategy_id": "momentum"})
        assert orders.calls[0]["strategy_id"] == "momentum"


class TestExcursions:
    @pytest.mark.asyncio
    async def test_bars_covering_the_holding_period_are_measured(
        self, client: httpx.AsyncClient, orders: RecordingOrderRepo, bars: FakeBars
    ) -> None:
        orders.orders = round_trip(at(0), at(48))
        bars.bars = {SYMBOL: [bar(at(24), "92", "115")]}

        trade = (await client.get(TRADES)).json()["trades"][0]

        assert trade["max_favorable_excursion"] == "150"  # (115 − 100) × 10
        assert trade["max_adverse_excursion"] == "-80"  # (92 − 100) × 10
        assert (await client.get(TRADES)).json()["excursions_omitted"] is False

    @pytest.mark.asyncio
    async def test_no_bars_reports_null_not_zero(
        self, client: httpx.AsyncClient, orders: RecordingOrderRepo
    ) -> None:
        orders.orders = round_trip(at(0), at(5))

        trade = (await client.get(TRADES)).json()["trades"][0]

        assert trade["max_favorable_excursion"] is None
        assert trade["max_adverse_excursion"] is None

    @pytest.mark.asyncio
    async def test_one_query_per_symbol_not_per_trade(
        self, client: httpx.AsyncClient, orders: RecordingOrderRepo, bars: FakeBars
    ) -> None:
        """A symbol traded forty times in a month is one range read."""
        orders.orders = [
            o for index in range(4) for o in round_trip(at(index * 10), at(index * 10 + 5))
        ]

        await client.get(TRADES)

        assert bars.queried == [SYMBOL]

    @pytest.mark.asyncio
    async def test_a_symbol_whose_bars_cannot_be_read_still_reports_its_pnl(
        self, client: httpx.AsyncClient, orders: RecordingOrderRepo, bars: FakeBars
    ) -> None:
        """An excursion is a column on a table; losing it must not lose the
        P&L figures beside it."""
        orders.orders = round_trip(at(0), at(5))
        bars.fail_for = {SYMBOL}

        trades = (await client.get(TRADES)).json()["trades"]

        assert len(trades) == 1
        assert trades[0]["net_pnl"] == "100"
        assert trades[0]["max_adverse_excursion"] is None


class TestAttribution:
    @pytest.fixture
    def mixed(self) -> list[Order]:
        return [
            *round_trip(at(0), at(1), exit_price="90", purpose=STOP_LOSS, strategy_id="a"),
            *round_trip(at(2), at(3), exit_price="130", purpose=TAKE_PROFIT, strategy_id="b"),
        ]

    @pytest.mark.asyncio
    async def test_by_exit_reason(
        self, client: httpx.AsyncClient, orders: RecordingOrderRepo, mixed: list[Order]
    ) -> None:
        orders.orders = mixed

        rows = (await client.get(ATTRIBUTION, params={"by": "exit_reason"})).json()["rows"]

        assert {r["key"]: r["net_pnl"] for r in rows} == {
            "take_profit": "300",
            "stop_loss": "-100",
        }

    @pytest.mark.asyncio
    async def test_by_strategy(
        self, client: httpx.AsyncClient, orders: RecordingOrderRepo, mixed: list[Order]
    ) -> None:
        orders.orders = mixed

        rows = (await client.get(ATTRIBUTION, params={"by": "strategy"})).json()["rows"]

        assert [r["key"] for r in rows] == ["b", "a"]

    @pytest.mark.asyncio
    async def test_an_unknown_dimension_is_422_naming_the_real_ones(
        self, client: httpx.AsyncClient
    ) -> None:
        """Not an empty list.

        A report silently grouped by nothing looks like a period with no trades.
        """
        response = await client.get(ATTRIBUTION, params={"by": "phase_of_moon"})

        assert response.status_code == 422
        assert "exit_reason" in response.json()["detail"]


class TestPerformance:
    @pytest.mark.asyncio
    async def test_the_metric_set_comes_back(
        self, client: httpx.AsyncClient, orders: RecordingOrderRepo
    ) -> None:
        orders.orders = [*round_trip(at(0), at(1)), *round_trip(at(2), at(3))]

        body = (await client.get(PERFORMANCE)).json()

        assert body["metrics"]["num_trades"] == 2
        assert body["metrics"]["win_rate"] == 1.0
        assert body["metrics"]["avg_holding_period_hours"] == 1.0

    @pytest.mark.asyncio
    async def test_it_says_what_it_annualised_by(
        self, client: httpx.AsyncClient, orders: RecordingOrderRepo
    ) -> None:
        """Every ratio scales with it, so a reader who disagrees with a Sharpe
        should see this before doubting the arithmetic."""
        orders.orders = [*round_trip(at(0), at(1)), *round_trip(at(2), at(3))]

        body = (await client.get(PERFORMANCE, params={"periods_per_year": 12})).json()

        assert body["periods_per_year"] == 12

    @pytest.mark.asyncio
    async def test_an_empty_period_is_zeroes_not_an_error(self, client: httpx.AsyncClient) -> None:
        body = (await client.get(PERFORMANCE)).json()

        assert body["metrics"]["num_trades"] == 0
        assert body["equity_points"] == 0


class TestTheDailyReport:
    """The last stub on this router, and the rule that governed it.

    `TestStillStubs` held "this is unbuilt" as a fact about the code rather than
    a claim in a docstring, so that building it had to delete the test saying it
    was not built. `/live-vs-backtest` went the same way before it. This class is
    what replaced the entry.
    """

    @pytest.mark.asyncio
    async def test_it_reports_a_day_that_traded(
        self, client: httpx.AsyncClient, orders: RecordingOrderRepo
    ) -> None:
        orders.orders = list(round_trip(at(0), at(1), exit_price="130"))

        response = await client.get(f"/api/v1/analytics/reports/daily?day={T0.date().isoformat()}")

        assert response.status_code == 200
        body = response.json()
        assert body["orders_filled"] == 2
        assert "SPY" in body["symbols"]

    @pytest.mark.asyncio
    async def test_a_day_the_orders_fall_outside_reports_that_day(
        self, client: httpx.AsyncClient, orders: RecordingOrderRepo
    ) -> None:
        """The window is the requested day, not "everything since". A report
        that quietly included yesterday's trades would make two days look like
        one good one."""
        orders.orders = list(round_trip(at(0), at(1), exit_price="130"))

        body = (await client.get("/api/v1/analytics/reports/daily")).json()

        assert body["orders_submitted"] == 0, "T0 is eighteen days before the pinned now"

    @pytest.mark.asyncio
    async def test_a_day_that_submitted_nothing_says_so_first(
        self, client: httpx.AsyncClient
    ) -> None:
        """The outcome this platform has actually produced. Day 1 of the paper
        week ran ten hours, submitted zero orders and reported it nowhere."""
        response = await client.get("/api/v1/analytics/reports/daily")

        assert response.status_code == 200
        assert response.json()["headline"] == "no orders submitted"

    @pytest.mark.asyncio
    async def test_feed_incidents_are_null_and_never_zero(self, client: httpx.AsyncClient) -> None:
        """The whole design of this report in one assertion.

        Nothing counts feed incidents — reconnects, gaps and staleness are log
        lines with no table behind them. Rendering `0` for them would be
        believed, and the day this report summarises is exactly the day a reader
        wants to know whether the feed misbehaved.
        """
        body = (await client.get("/api/v1/analytics/reports/daily")).json()

        feed = next(s for s in body["sections"] if s["name"] == "feed incidents")
        assert feed["value"] is None
        assert feed["how_to_check"]
        assert "feed incidents" in body["not_measured"]

    @pytest.mark.asyncio
    async def test_a_countable_section_is_zero_and_never_null(
        self, client: httpx.AsyncClient
    ) -> None:
        """The control for the case above. Refused orders *are* rows, so a day
        with none is a measured zero and must not read as unmeasured."""
        body = (await client.get("/api/v1/analytics/reports/daily")).json()

        refusals = next(s for s in body["sections"] if s["name"] == "risk rejections")
        assert refusals["value"] == 0
        assert "risk rejections" not in body["not_measured"]

    @pytest.mark.asyncio
    async def test_an_unreadable_audit_table_degrades_rather_than_502s(
        self, app: FastAPI, orders: RecordingOrderRepo
    ) -> None:
        """Deliberately the opposite of what `/audit` does, and for a reason.

        That page is nothing but audit rows, so a read it cannot make has to be
        a 503. Here the halts are one section of five, and answering 503 would
        hide the day's trades to report the absence of one section.
        """
        app.dependency_overrides[get_audit_reader] = lambda: _UnreadableAudit()
        orders.orders = list(round_trip(at(0), at(1), exit_price="130"))
        transport = httpx.ASGITransport(app=app)

        async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
            response = await http.get(
                f"/api/v1/analytics/reports/daily?day={T0.date().isoformat()}"
            )

        assert response.status_code == 200
        body = response.json()
        assert body["orders_filled"] == 2, "the day's trades survive an unreadable audit table"
        halts = next(s for s in body["sections"] if s["name"] == "halts")
        assert halts["value"] is None
        assert "halts" in body["not_measured"]


class _UnreadableAudit:
    """An audit reader whose table cannot be reached, for the degradation test."""

    async def recent(self, **kwargs: object) -> list[object]:
        raise ConnectionError("the database is gone")


class TestLiveVsBacktest:
    """The comparison, and the several ways it can quietly answer the wrong question.

    The arithmetic is `test_analytics_performance.py`'s. What is held here is
    everything around it: which strategy the live half is read for, which
    window it covers, and whether a reader is told that the two halves were
    measured on bases that are not the same.
    """

    @pytest.mark.asyncio
    async def test_the_strategy_comes_from_the_run_not_the_request(
        self, client: httpx.AsyncClient, orders: RecordingOrderRepo, runs: FakeBacktestRunRepository
    ) -> None:
        """The property that makes this endpoint answerable at all.

        Nothing in the request names a strategy, so the two halves of the
        comparison cannot be about different ones. A `strategy_id` query
        parameter beside a run id would make "live SMA against a backtested
        mean-reversion" a supported call.
        """
        await store(runs, spec=a_spec(strategy_id="mean_reversion"))
        orders.orders = round_trip(at(0), at(24), strategy_id="mean_reversion")

        response = await client.get(f"{LIVE_VS_BACKTEST}/{RUN_ID}")

        assert response.status_code == 200
        assert orders.calls[0]["strategy_id"] == "mean_reversion"
        assert response.json()["live"]["strategy_id"] == "mean_reversion"

    @pytest.mark.asyncio
    async def test_an_unknown_run_is_a_404(self, client: httpx.AsyncClient) -> None:
        response = await client.get(f"{LIVE_VS_BACKTEST}/nope")
        assert response.status_code == 404

    @pytest.mark.asyncio
    @pytest.mark.parametrize("status", ["queued", "running", "failed"])
    async def test_a_run_with_no_result_is_refused(
        self, client: httpx.AsyncClient, runs: FakeBacktestRunRepository, status: str
    ) -> None:
        """Not a comparison against a column of nulls.

        A queued run has no metrics and a failed one has none either. Comparing
        against them would report every live metric as an unexplained
        divergence — the shape of answer somebody acts on.
        """
        await store(runs, status=status, metrics=None)

        response = await client.get(f"{LIVE_VS_BACKTEST}/{RUN_ID}")

        assert response.status_code == 400
        assert status in response.json()["detail"]

    @pytest.mark.asyncio
    async def test_a_null_stored_metric_is_a_null_divergence(
        self, client: httpx.AsyncClient, orders: RecordingOrderRepo, runs: FakeBacktestRunRepository
    ) -> None:
        """The case a stored backtest actually produces, and it is not rare.

        `runner.jsonable` nulls every non-finite metric on the way into the row,
        because `Infinity` is not legal JSON — and an infinite `profit_factor`
        means the backtest had no losing trade, which is precisely the run
        somebody holds a live record up against. Subtracted raw it raises;
        rendered as zero it would claim live matched it exactly.
        """
        await store(runs, metrics={**BACKTEST_METRICS, "profit_factor": None})
        orders.orders = round_trip(at(0), at(24))

        body = (await client.get(f"{LIVE_VS_BACKTEST}/{RUN_ID}")).json()

        assert body["divergence"]["profit_factor"] is None
        assert body["divergence"]["win_rate"] is not None

    @pytest.mark.asyncio
    async def test_a_metric_the_stored_run_never_had_is_null_too(
        self, client: httpx.AsyncClient, orders: RecordingOrderRepo, runs: FakeBacktestRunRepository
    ) -> None:
        """A run stored before the metric set grew a field is still comparable.

        On every other field. The alternative — a 500, or dropping the row —
        would make one added metric retire every backtest on record.
        """
        await store(runs, metrics={k: v for k, v in BACKTEST_METRICS.items() if k != "turnover"})
        orders.orders = round_trip(at(0), at(24))

        body = (await client.get(f"{LIVE_VS_BACKTEST}/{RUN_ID}")).json()

        assert body["divergence"]["turnover"] is None
        assert body["divergence"]["num_trades"] == 1 - 40

    @pytest.mark.asyncio
    async def test_divergence_is_live_minus_backtest(
        self, client: httpx.AsyncClient, orders: RecordingOrderRepo, runs: FakeBacktestRunRepository
    ) -> None:
        """The direction, which is the whole reading of the table.

        One live trade against forty backtested is −39: a strategy that has been
        refused, not one that has underperformed.
        """
        await store(runs)
        orders.orders = round_trip(at(0), at(24))

        body = (await client.get(f"{LIVE_VS_BACKTEST}/{RUN_ID}")).json()

        assert body["live"]["metrics"]["num_trades"] == 1
        assert body["backtest"]["metrics"]["num_trades"] == 40
        assert body["divergence"]["num_trades"] == -39

    @pytest.mark.asyncio
    async def test_the_live_window_is_open_at_the_start_by_default(
        self, client: httpx.AsyncClient, orders: RecordingOrderRepo, runs: FakeBacktestRunRepository
    ) -> None:
        """The deliberate divergence from every other endpoint on this router.

        They default to the last 30 days, which is the period an operator asks
        about without thinking. This one asks whether a strategy has held up,
        and the denominator for that is its whole live record — a default that
        compared the last month of a longer paper run against a five-year
        backtest would answer a narrower question invisibly.

        `NOW` is 2026-03-20 and this trade closed on 2026-01-06, well outside a
        30-day lookback.
        """
        await store(runs)
        orders.orders = round_trip(OLD, OLD + timedelta(hours=24))

        body = (await client.get(f"{LIVE_VS_BACKTEST}/{RUN_ID}")).json()

        assert body["live"]["num_trades"] == 1
        assert body["live"]["requested_start"] is None
        assert orders.calls[0]["until"].date() == date(2026, 3, 20)

    @pytest.mark.asyncio
    async def test_a_named_start_still_bounds_it(
        self, client: httpx.AsyncClient, orders: RecordingOrderRepo, runs: FakeBacktestRunRepository
    ) -> None:
        """Open by default is not open regardless."""
        await store(runs)
        orders.orders = round_trip(OLD, OLD + timedelta(hours=24)) + round_trip(at(0), at(24))

        body = (
            await client.get(f"{LIVE_VS_BACKTEST}/{RUN_ID}", params={"start": "2026-03-01"})
        ).json()

        assert body["live"]["num_trades"] == 1
        assert body["live"]["requested_start"].startswith("2026-03-01")

    @pytest.mark.asyncio
    async def test_the_live_window_spans_first_entry_to_last_exit(
        self, client: httpx.AsyncClient, orders: RecordingOrderRepo, runs: FakeBacktestRunRepository
    ) -> None:
        """Entry, not first exit.

        A strategy that opened on day one and closed nothing until day forty has
        a forty-day live record. Measured exit-to-exit a single round trip spans
        zero days, which would then suppress the window-length warning on
        exactly the comparison that most needs it.
        """
        await store(runs)
        orders.orders = round_trip(at(0), at(48))

        window = (await client.get(f"{LIVE_VS_BACKTEST}/{RUN_ID}")).json()["live"]["window"]

        assert window["start"].startswith("2026-03-02")
        assert window["days"] == pytest.approx(2.0)

    @pytest.mark.asyncio
    async def test_no_closed_trades_is_a_sentence_not_a_500(
        self, client: httpx.AsyncClient, runs: FakeBacktestRunRepository
    ) -> None:
        """An empty live half is a legitimate answer and the most misleading one.

        `compute_all` returns 0.0 for every ratio it cannot compute, so the
        divergence column becomes the backtest's own metrics negated — which
        reads as catastrophic underperformance rather than as no data.
        """
        await store(runs)

        body = (await client.get(f"{LIVE_VS_BACKTEST}/{RUN_ID}")).json()

        assert body["live"]["num_trades"] == 0
        assert body["live"]["window"] == {"start": None, "end": None, "days": None}
        assert any("no live round trips" in w for w in body["warnings"])

    @pytest.mark.asyncio
    async def test_the_two_annualisation_bases_are_reported_and_warned_about(
        self, client: httpx.AsyncClient, orders: RecordingOrderRepo, runs: FakeBacktestRunRepository
    ) -> None:
        """The divergence that is measurement rather than performance.

        The engine annualises a backtest by its bar spacing; the live curve
        steps once per closed trade and is inferred from that. Every annualised
        metric differs partly for that reason alone, and a Sharpe divergence
        nobody attributed to it is the most plausible-looking wrong number this
        endpoint can produce.
        """
        await store(runs, spec=a_spec(timeframe="1m"))
        orders.orders = round_trip(at(0), at(24)) + round_trip(at(48), at(72))

        body = (await client.get(f"{LIVE_VS_BACKTEST}/{RUN_ID}")).json()

        assert body["backtest"]["periods_per_year"] == 252 * 390
        assert body["live"]["periods_per_year"] != body["backtest"]["periods_per_year"]
        assert any("annualised on different bases" in w for w in body["warnings"])

    @pytest.mark.asyncio
    async def test_pinning_periods_per_year_puts_both_sides_on_one_basis(
        self, client: httpx.AsyncClient, orders: RecordingOrderRepo, runs: FakeBacktestRunRepository
    ) -> None:
        """Which makes the warning above actionable rather than decorative."""
        await store(runs)
        orders.orders = round_trip(at(0), at(24)) + round_trip(at(48), at(72))

        body = (
            await client.get(f"{LIVE_VS_BACKTEST}/{RUN_ID}", params={"periods_per_year": 252})
        ).json()

        assert body["live"]["periods_per_year"] == 252
        assert not any("annualised on different bases" in w for w in body["warnings"])

    @pytest.mark.asyncio
    async def test_a_symbol_live_traded_that_the_backtest_never_covered_is_named(
        self, client: httpx.AsyncClient, orders: RecordingOrderRepo, runs: FakeBacktestRunRepository
    ) -> None:
        """Not filtered out, and that is the decision worth stating.

        Dropping those trades would make the live metrics tidier and would hide
        a strategy trading names it was never approved on — which is a finding,
        not noise. The report surfaces; it does not launder.
        """
        await store(runs, spec=a_spec(symbols=("SPY",)))
        orders.orders = round_trip(at(0), at(24)) + round_trip(at(0), at(24), symbol="TSLA")

        body = (await client.get(f"{LIVE_VS_BACKTEST}/{RUN_ID}")).json()

        assert body["live"]["symbols"] == ["SPY", "TSLA"]
        assert body["live"]["metrics"]["num_trades"] == 2
        assert any("TSLA" in w and "never covered" in w for w in body["warnings"])

    @pytest.mark.asyncio
    async def test_every_divergence_row_carries_a_comparability(
        self, client: httpx.AsyncClient, orders: RecordingOrderRepo, runs: FakeBacktestRunRepository
    ) -> None:
        """An unlabelled row is what the label exists to prevent.

        A metric added to the set without a basis would reach a comparison table
        as a number with no guidance on whether to believe it.
        """
        await store(runs)
        orders.orders = round_trip(at(0), at(24))

        body = (await client.get(f"{LIVE_VS_BACKTEST}/{RUN_ID}")).json()

        assert set(body["comparability"]) == set(body["divergence"])
        assert body["comparability"]["sharpe"] == "annualised"
        assert body["comparability"]["win_rate"] == "per_trade"

    @pytest.mark.asyncio
    async def test_the_run_is_identified_not_just_quoted(
        self, client: httpx.AsyncClient, orders: RecordingOrderRepo, runs: FakeBacktestRunRepository
    ) -> None:
        """A divergence is only meaningful against a backtest somebody can identify.

        Cost model, share count, timeframe and window are what make two runs of
        one strategy different results, so they travel with the numbers rather
        than being another request away.
        """
        await store(runs)
        orders.orders = round_trip(at(0), at(24))

        backtest = (await client.get(f"{LIVE_VS_BACKTEST}/{RUN_ID}")).json()["backtest"]

        assert backtest["run_id"] == RUN_ID
        assert backtest["cost_model"] == "alpaca_equities"
        assert backtest["qty"] == "100"
        assert backtest["window"]["start"].startswith("2026-01-05")

    @pytest.mark.asyncio
    async def test_the_backtests_own_warnings_come_with_it(
        self, client: httpx.AsyncClient, orders: RecordingOrderRepo, runs: FakeBacktestRunRepository
    ) -> None:
        """A divergence against a nine-trade backtest is a statement about the backtest.

        The reader of this response is not necessarily the person who read that
        run's own page, so `suspicious` is applied again here rather than
        referenced.
        """
        await store(runs, metrics={**BACKTEST_METRICS, "num_trades": 9})
        orders.orders = round_trip(at(0), at(24))

        body = (await client.get(f"{LIVE_VS_BACKTEST}/{RUN_ID}")).json()

        assert any("only 9 trades" in w for w in body["backtest"]["warnings"])
