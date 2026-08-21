"""The seed's write path, against a real PostgreSQL.

`tests/unit/test_seed.py` covers the shape of what the seed generates and the
guards around running it. What it cannot cover is the reason the seed exists,
because the reason is a database constraint: `backtest_runs.strategy_id` is a
foreign key onto `strategies`, and until this script there was nothing but a
booting trading worker that ever wrote that table. So a clean install had an
empty picker and a 409 on the one action the Backtests tab offers.

Whether the rows the seed writes actually satisfy that constraint is a question
only the constraint can answer. `TestTheForeignKeyIsSatisfied` below is the
whole point of the feature, stated as the failure it removes; the rest is the
storage round trip — that a fabricated `Decimal` survives NUMERIC, that a
session-aligned timestamp comes back addressing the range it was written for.

`tests/integration/test_backtest_runs.py` asserts the same foreign key from the
other side: that it *refuses* a run naming a strategy nothing has registered.
These two together are the before and the after.
"""

from __future__ import annotations

import importlib.util
import sys
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Any

import asyncpg
import pytest

from atp_core.backtest.ports import BacktestRunSpec
from atp_core.clock import SimulatedClock, TradingCalendar
from atp_core.data.seed import DEFAULT_SEED_SYMBOLS, synthetic_daily_bars
from atp_core.domain import StrategyState, Timeframe
from atp_core.persistence.backtests import PostgresBacktestRunRepository, new_run
from atp_core.persistence.bars import PostgresBarRepository
from atp_core.persistence.db import create_engine, create_session_factory
from atp_core.persistence.strategies import PostgresStrategyRepository

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

pytestmark = pytest.mark.integration

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"


def _load(name: str) -> Any:
    """Import a script by path — `scripts/` is deliberately not a package.

    Loaded here rather than imported from `tests.unit.test_seed` so this file
    depends on the script itself and not on another test module's plumbing.
    """
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


seed_script = _load("seed")

T0 = datetime(2026, 3, 2, 14, 30, tzinfo=UTC)

#: A short window on purpose. The seed's default is three years per symbol and
#: what is under test here is the round trip, not the volume.
FIRST, LAST = date(2023, 1, 1), date(2023, 3, 31)
SYMBOL = DEFAULT_SEED_SYMBOLS[0]


@pytest.fixture(scope="module")
def calendar() -> TradingCalendar:
    return TradingCalendar()


@pytest.fixture
async def clean(clean_bars: str) -> AsyncIterator[str]:
    """Empty `bars`, `backtest_runs` and `strategies`.

    `bars` is delegated to `clean_bars` rather than truncated here alongside the
    other two. That fixture carries the deadlock retry TimescaleDB's compression
    policy makes necessary, and a second, weaker truncate of the same table
    would reintroduce exactly the flake it exists to prevent.

    Truncated before rather than after, so a failed test leaves its rows to be
    inspected instead of tidying away the evidence.
    """
    conn = await asyncpg.connect(clean_bars.replace("postgresql+asyncpg://", "postgresql://"))
    try:
        await conn.execute("TRUNCATE TABLE backtest_runs, strategies CASCADE")
    finally:
        await conn.close()
    yield clean_bars


@pytest.fixture
async def repos(
    clean: str,
) -> AsyncIterator[
    tuple[PostgresStrategyRepository, PostgresBarRepository, PostgresBacktestRunRepository]
]:
    engine = create_engine(clean)
    try:
        factory = create_session_factory(engine)
        yield (
            PostgresStrategyRepository(factory, SimulatedClock(T0)),
            PostgresBarRepository(factory),
            PostgresBacktestRunRepository(factory),
        )
    finally:
        await engine.dispose()


async def _seed(
    repos: tuple[PostgresStrategyRepository, PostgresBarRepository, PostgresBacktestRunRepository],
    calendar: TradingCalendar,
) -> int:
    """What `scripts/seed.py` does, minus the argument handling and the guards.

    The records come from the script itself rather than being rewritten here: a
    copy would keep passing after the script started writing something else,
    which is the failure mode this test is supposed to catch.
    """
    strategies, bars, _ = repos
    for record in seed_script.seed_strategies():
        await strategies.ensure(record)
    return await bars.upsert_bars(synthetic_daily_bars(SYMBOL, FIRST, LAST, calendar=calendar))


class TestTheForeignKeyIsSatisfied:
    """The 409 this feature removes, proven against the real constraint."""

    async def test_a_run_can_be_recorded_for_a_seeded_strategy(
        self,
        repos: tuple[
            PostgresStrategyRepository, PostgresBarRepository, PostgresBacktestRunRepository
        ],
        calendar: TradingCalendar,
    ) -> None:
        """Before the seed, this insert was the 409 on `POST /backtests`: the
        registry knew `sma_crossover` and `strategies` had no row for it, so the
        API accepted the request at the door and the write refused it."""
        _, _, runs = repos
        await _seed(repos, calendar)

        run = new_run(
            "run-seeded",
            BacktestRunSpec(
                strategy_id="sma_crossover",
                symbols=(SYMBOL,),
                start=datetime(FIRST.year, FIRST.month, FIRST.day, tzinfo=UTC),
                end=datetime(LAST.year, LAST.month, LAST.day, tzinfo=UTC),
                timeframe="1d",
                starting_cash="100000",
                cost_model="alpaca_equities",
                params={},
                qty="100",
            ),
            queued_at=T0,
        )
        await runs.create(run)

        stored = await runs.get("run-seeded")
        assert stored is not None
        assert stored.spec.strategy_id == "sma_crossover"


class TestSeededStrategyRows:
    async def test_a_row_lands_for_every_registered_strategy(
        self,
        repos: tuple[
            PostgresStrategyRepository, PostgresBarRepository, PostgresBacktestRunRepository
        ],
        calendar: TradingCalendar,
    ) -> None:
        strategies, _, _ = repos
        await _seed(repos, calendar)
        assert {s.id for s in await strategies.list_all()} == {
            record.id for record in seed_script.seed_strategies()
        }

    async def test_the_row_starts_at_draft(
        self,
        repos: tuple[
            PostgresStrategyRepository, PostgresBarRepository, PostgresBacktestRunRepository
        ],
        calendar: TradingCalendar,
    ) -> None:
        """A seed grants no promotion. `draft` is the ratchet's first rung, and
        the CHECK constraint added in `e2b6d1a70f93` is what would reject
        anything that is not a real rung — including the `"active"` that used to
        be written here."""
        strategies, _, _ = repos
        await _seed(repos, calendar)
        stored = await strategies.list_all()
        assert all(s.state == StrategyState.DRAFT.value for s in stored)

    async def test_seeding_twice_is_idempotent(
        self,
        repos: tuple[
            PostgresStrategyRepository, PostgresBarRepository, PostgresBacktestRunRepository
        ],
        calendar: TradingCalendar,
    ) -> None:
        """`make seed` is a command people re-run without thinking about it. A
        second run must not duplicate rows, and `ensure`'s upsert must not reset
        a row somebody has since edited — it touches only `updated_at`."""
        strategies, _, _ = repos
        await _seed(repos, calendar)
        before = await strategies.list_all()
        await _seed(repos, calendar)
        after = await strategies.list_all()
        assert len(after) == len(before)
        assert {s.id for s in after} == {s.id for s in before}

    async def test_default_params_survive_the_json_column(
        self,
        repos: tuple[
            PostgresStrategyRepository, PostgresBarRepository, PostgresBacktestRunRepository
        ],
        calendar: TradingCalendar,
    ) -> None:
        """The reason the seed writes schema defaults rather than `{}`: the
        backtest form reads this column, and an empty one says the strategy was
        configured with nothing."""
        strategies, _, _ = repos
        await _seed(repos, calendar)
        record = await strategies.get("sma_crossover")
        assert record is not None
        assert record.params == {"fast_period": 20, "slow_period": 50, "timeframe": "1d"}


class TestSeededBars:
    async def test_bars_come_back_addressable_by_the_window_they_were_written_for(
        self,
        repos: tuple[
            PostgresStrategyRepository, PostgresBarRepository, PostgresBacktestRunRepository
        ],
        calendar: TradingCalendar,
    ) -> None:
        """The timestamp convention, end to end. Bars are stamped at exchange-
        local midnight; a query in UTC over the same calendar days has to find
        them, or the seed writes rows the backtest coverage check cannot see."""
        _, bars, _ = repos
        written = await _seed(repos, calendar)
        assert written == len(calendar.sessions(FIRST, LAST))

        stored = await bars.get_bars(
            SYMBOL,
            Timeframe.D1,
            datetime(FIRST.year, FIRST.month, FIRST.day, tzinfo=UTC),
            datetime(LAST.year, LAST.month, LAST.day, tzinfo=UTC),
        )
        assert stored
        assert all(bar.symbol == SYMBOL for bar in stored)

    async def test_prices_survive_the_numeric_column_as_decimals(
        self,
        repos: tuple[
            PostgresStrategyRepository, PostgresBarRepository, PostgresBacktestRunRepository
        ],
        calendar: TradingCalendar,
    ) -> None:
        """Rule §1.1 across the storage boundary. A float anywhere in this path
        would come back with a tail of binary noise."""
        _, bars, _ = repos
        await _seed(repos, calendar)
        stored = await bars.get_last_n_bars(SYMBOL, Timeframe.D1, 5)
        assert stored
        for bar in stored:
            assert isinstance(bar.close, Decimal)
            assert -bar.close.as_tuple().exponent <= 2
            assert bar.adj_close == bar.close

    async def test_re_seeding_writes_the_same_bars(
        self,
        repos: tuple[
            PostgresStrategyRepository, PostgresBarRepository, PostgresBacktestRunRepository
        ],
        calendar: TradingCalendar,
    ) -> None:
        """Reproducibility where it actually matters. `upsert_bars` overwrites on
        conflict, so a non-deterministic generator would silently rewrite the
        history a developer's earlier backtest numbers came from."""
        _, bars, _ = repos
        await _seed(repos, calendar)
        first = await bars.get_last_n_bars(SYMBOL, Timeframe.D1, 20)
        await _seed(repos, calendar)
        assert await bars.get_last_n_bars(SYMBOL, Timeframe.D1, 20) == first

    async def test_the_seeded_range_has_no_gaps(
        self,
        repos: tuple[
            PostgresStrategyRepository, PostgresBarRepository, PostgresBacktestRunRepository
        ],
        calendar: TradingCalendar,
    ) -> None:
        """Gap detection is calendar-aware, and it is the check an operator runs
        to decide whether a dataset is trustworthy. A seed that emitted bars on
        weekends — or missed a half-day — would report itself broken."""
        _, bars, _ = repos
        await _seed(repos, calendar)
        gaps = await bars.find_gaps(
            SYMBOL,
            Timeframe.D1,
            datetime(FIRST.year, FIRST.month, FIRST.day, tzinfo=UTC),
            datetime(LAST.year, LAST.month, LAST.day, tzinfo=UTC),
        )
        assert gaps == []
