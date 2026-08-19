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
    get_bar_repository,
    get_clock,
    get_current_session,
    get_order_repository,
)
from atp_api.main import create_app
from atp_core.clock import SimulatedClock
from atp_core.config import Settings, get_settings
from atp_core.domain import Bar, Fill, Order, Side, Timeframe
from atp_core.execution.idempotency import ENTRY, EXIT, STOP_LOSS, TAKE_PROFIT

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from fastapi import FastAPI

PERFORMANCE = "/api/v1/analytics/performance"
TRADES = "/api/v1/analytics/trades"
ATTRIBUTION = "/api/v1/analytics/attribution"

SYMBOL = "SPY"
#: A Monday. Every trade below is placed relative to it so the weekday
#: attribution has a stable answer.
T0 = datetime(2026, 3, 2, 14, 30, tzinfo=UTC)

#: "Now" for every request below. Pinned rather than ambient: the default
#: window is the last 30 days, so a suite reading the real clock would stop
#: finding its own fixtures 30 days after they were written.
NOW = datetime(2026, 3, 20, 12, 0, tzinfo=UTC)


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
def app(orders: RecordingOrderRepo, bars: FakeBars) -> FastAPI:
    application = create_app()
    application.dependency_overrides[get_settings] = pinned_settings
    application.dependency_overrides[get_clock] = lambda: SimulatedClock(NOW)
    application.dependency_overrides[get_order_repository] = lambda: orders
    application.dependency_overrides[get_bar_repository] = lambda: bars
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


class TestStillStubs:
    """Two endpoints on this router are separate roadmap items.

    Held by a test so that "these are unbuilt" stays a fact about the code
    rather than a claim in a docstring — and so that building one has to delete
    the test that says it is not built.
    """

    @pytest.mark.asyncio
    async def test_live_vs_backtest_is_not_built(self, client: httpx.AsyncClient) -> None:
        with pytest.raises(NotImplementedError):
            await client.get("/api/v1/analytics/live-vs-backtest/sma")

    @pytest.mark.asyncio
    async def test_the_daily_report_is_not_built(self, client: httpx.AsyncClient) -> None:
        with pytest.raises(NotImplementedError):
            await client.get("/api/v1/analytics/reports/daily")
