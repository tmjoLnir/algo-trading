"""The development seed — the generated series, and the guards around writing it.

Two things are worth testing here and they are not the same thing.

The **shape of the data** is `atp_core.data.seed`'s job: bars a `Bar` will
accept, on days the market was actually open, stamped where the gap detector
expects, and reproducible. A seed that produced a bar on Thanksgiving would make
`find_gaps` report seeded data as broken in a way no real dataset is, and a seed
that produced different bars on every run would make every backtest taken
against it unreproducible.

The **guards** are the script's job: where fabricated bars may be written
(reserved tickers only) and which database they may be written to (a development
one). Both exist because fake market data reaching somewhere real is the failure
this whole feature has to not have.

No database anywhere in this file. The repositories these feed have their own
tests, including against a real PostgreSQL in `tests/integration/`.
"""

from __future__ import annotations

import importlib.util
import sys
from datetime import UTC, date
from decimal import Decimal
from pathlib import Path
from typing import Any, ClassVar

import pytest

from atp_core.clock import TradingCalendar
from atp_core.config import Settings
from atp_core.data.seed import (
    DEFAULT_FIRST_DAY,
    DEFAULT_LAST_DAY,
    DEFAULT_SEED_SYMBOLS,
    RESERVED_TEST_SYMBOLS,
    require_reserved,
    synthetic_daily_bars,
)
from atp_core.domain import Bar, Timeframe
from atp_core.errors import ConfigError
from atp_core.strategy import registry
from atp_core.strategy.examples.sma_crossover import SmaCrossover

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
CORE_SEED = Path(__file__).resolve().parents[2] / "libs/core/src/atp_core/data/seed.py"


def _load(name: str) -> Any:
    """Import a script by path — see `test_operator_scripts.py` for why
    `scripts/` is deliberately not a package."""
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


seed_script = _load("seed")


@pytest.fixture(scope="module")
def calendar() -> TradingCalendar:
    """One calendar for the module. It materialises sessions a year at a time
    and caches them, so a fresh one per test pays ~50ms per year for nothing."""
    return TradingCalendar()


@pytest.fixture(scope="module")
def year(calendar: TradingCalendar) -> list[Bar]:
    return synthetic_daily_bars("ZVZZT", date(2023, 1, 1), date(2023, 12, 31), calendar=calendar)


class TestReservedSymbols:
    def test_a_reserved_ticker_is_allowed(self) -> None:
        require_reserved(sorted(RESERVED_TEST_SYMBOLS))

    def test_a_real_ticker_is_refused(self) -> None:
        """The guard that matters. `upsert_bars` is keyed on
        `(symbol, timeframe, ts)`, so fabricating SPY would not sit beside a real
        SPY history — it would overwrite it, row for row, silently."""
        with pytest.raises(ConfigError, match="SPY"):
            require_reserved(["SPY"])

    def test_the_refusal_names_what_is_allowed_and_how_to_get_real_bars(self) -> None:
        """Somebody who reaches for SPY here wants real history, and the message
        should end that search rather than only stopping it."""
        with pytest.raises(ConfigError) as exc:
            require_reserved(["SPY", "QQQ"])
        assert "ZVZZT" in str(exc.value)
        assert "backfill_bars.py" in str(exc.value)

    def test_generation_refuses_an_unreserved_symbol_too(self, calendar: TradingCalendar) -> None:
        """Not only at the script's entry point. A caller reaching straight for
        the generator has to hit the same wall, or the guard is advisory."""
        with pytest.raises(ConfigError, match="AAPL"):
            synthetic_daily_bars("AAPL", date(2023, 1, 1), date(2023, 3, 1), calendar=calendar)

    def test_the_defaults_are_themselves_reserved(self) -> None:
        """A default that failed its own guard would be a script nobody could
        run without arguments."""
        require_reserved(DEFAULT_SEED_SYMBOLS)


class TestGeneratedSeries:
    def test_one_bar_per_trading_session(self, year: list[Bar], calendar: TradingCalendar) -> None:
        assert len(year) == len(calendar.sessions(date(2023, 1, 1), date(2023, 12, 31)))

    def test_no_bar_on_a_market_holiday(self, year: list[Bar], calendar: TradingCalendar) -> None:
        """Christmas 2023 fell on a Monday — a weekday filter would emit a bar
        for it, and gap detection would then disagree with the data forever."""
        days = {bar.ts.astimezone(calendar.tz).date() for bar in year}
        assert date(2023, 12, 25) not in days
        assert date(2023, 7, 4) not in days

    def test_stamped_at_exchange_local_midnight(
        self, year: list[Bar], calendar: TradingCalendar
    ) -> None:
        """Where Alpaca stamps a daily bar, and therefore where `data.gaps`
        looks for one (docs/DATA.md). Stamping at the session open would put
        every bar in the right day by eye and the wrong one by the code."""
        for bar in year[:5]:
            day = bar.ts.astimezone(calendar.tz).date()
            assert bar.ts == calendar.day_bounds(day)[0]

    def test_timestamps_are_utc_and_ascending(self, year: list[Bar]) -> None:
        assert all(bar.ts.utcoffset() == UTC.utcoffset(None) for bar in year)
        assert [bar.ts for bar in year] == sorted(bar.ts for bar in year)

    def test_prices_are_decimal_at_cent_resolution(self, year: list[Bar]) -> None:
        """Rule §1.1. The walk underneath is float, which is fine — it generates
        a number rather than tracking a balance — but what lands in the row is
        exact."""
        for bar in year[:20]:
            for price in (bar.open, bar.high, bar.low, bar.close):
                assert isinstance(price, Decimal)
                assert -price.as_tuple().exponent <= 2

    def test_every_candle_is_internally_consistent(self, year: list[Bar]) -> None:
        """`Bar.__post_init__` enforces this, so a violation would have raised
        during generation — which is the point. This asserts the generator does
        not merely avoid raising on one lucky seed."""
        for bar in year:
            assert bar.low <= bar.open <= bar.high
            assert bar.low <= bar.close <= bar.high

    def test_prices_stay_positive(self, year: list[Bar]) -> None:
        assert all(bar.low > 0 for bar in year)

    def test_volume_is_deep_enough_not_to_cap_an_ordinary_fill(self, year: list[Bar]) -> None:
        """The engine caps a fill at 10% of the bar's volume. A thin synthetic
        series would make every fill partial and teach a reader something about
        this module's constants instead of about their strategy."""
        assert all(bar.volume * Decimal("0.10") > 1000 for bar in year)

    def test_adjusted_close_is_set_and_equals_the_close(self, year: list[Bar]) -> None:
        """No corporate actions in a fabricated series, so it simply is the
        close. Written rather than left null so a seeded row is shaped like a
        real one."""
        assert all(bar.adj_close == bar.close for bar in year)

    def test_the_timeframe_is_daily(self, year: list[Bar]) -> None:
        assert all(bar.timeframe is Timeframe.D1 for bar in year)


class TestReproducibility:
    def test_the_same_window_regenerates_identically(self, calendar: TradingCalendar) -> None:
        """The property a development database's usefulness rests on: a re-seed
        must not change the numbers a developer already backtested."""
        args = ("ZVZZT", date(2023, 1, 1), date(2023, 6, 30))
        assert synthetic_daily_bars(*args, calendar=calendar) == synthetic_daily_bars(
            *args, calendar=calendar
        )

    def test_extending_the_window_appends_rather_than_rewrites(
        self, calendar: TradingCalendar
    ) -> None:
        """`last` is deliberately not part of the seed. Moving the end date
        forward has to extend the history, the way backfilling more of a real
        one does — not reshuffle every bar behind it."""
        short = synthetic_daily_bars(
            "ZVZZT", date(2023, 1, 1), date(2023, 6, 30), calendar=calendar
        )
        longer = synthetic_daily_bars(
            "ZVZZT", date(2023, 1, 1), date(2023, 12, 31), calendar=calendar
        )
        assert longer[: len(short)] == short
        assert len(longer) > len(short)

    def test_different_symbols_are_different_series(self, calendar: TradingCalendar) -> None:
        """Three seeded tickers moving in lockstep would make a multi-symbol
        backtest a single-symbol one with extra steps."""
        window = (date(2023, 1, 1), date(2023, 6, 30))
        a = synthetic_daily_bars("ZVZZT", *window, calendar=calendar)
        b = synthetic_daily_bars("ZWZZT", *window, calendar=calendar)
        assert [bar.close for bar in a] != [bar.close for bar in b]

    def test_the_default_window_is_fixed_rather_than_relative_to_today(self) -> None:
        """Computed from the clock, these would move the series under a
        developer whenever they re-seeded on a different day."""
        assert DEFAULT_FIRST_DAY < DEFAULT_LAST_DAY

    def test_generation_reads_no_clock(self) -> None:
        """A series that depended on when it was generated would not be
        reproducible, and rule §1.2 forbids `datetime.now()` regardless.
        Asserted against the source because the failure is one import away."""
        source = CORE_SEED.read_text(encoding="utf-8")
        assert "datetime.now" not in source
        assert "date.today" not in source


class TestSeriesArguments:
    def test_a_non_positive_start_price_is_refused(self, calendar: TradingCalendar) -> None:
        with pytest.raises(ConfigError, match="start_price"):
            synthetic_daily_bars(
                "ZVZZT",
                date(2023, 1, 1),
                date(2023, 2, 1),
                calendar=calendar,
                start_price=Decimal("0"),
            )

    def test_a_non_positive_volatility_is_refused(self, calendar: TradingCalendar) -> None:
        with pytest.raises(ConfigError, match="annual_volatility"):
            synthetic_daily_bars(
                "ZVZZT",
                date(2023, 1, 1),
                date(2023, 2, 1),
                calendar=calendar,
                annual_volatility=0.0,
            )

    def test_a_range_with_no_sessions_yields_nothing(self, calendar: TradingCalendar) -> None:
        """New Year's Day 2023 was a Sunday. Empty is the honest answer; raising
        would make a caller special-case a legitimate window."""
        assert (
            synthetic_daily_bars("ZVZZT", date(2023, 1, 1), date(2023, 1, 1), calendar=calendar)
            == []
        )


class TestDefaultParams:
    def test_schema_defaults_are_read_from_the_class(self) -> None:
        """The row records what the strategy will actually run on. Writing `{}`
        would record that it was configured with nothing, which is a different
        claim and a false one."""
        assert registry.default_params(SmaCrossover) == {
            "fast_period": 20,
            "slow_period": 50,
            "timeframe": "1d",
        }

    def test_a_property_without_a_default_is_omitted(self) -> None:
        """The schema is saying the value must be supplied. A null here would be
        this function inventing one."""

        class Required(SmaCrossover):
            name = "test_required_params"
            params_schema: ClassVar[dict[str, Any]] = {
                "type": "object",
                "properties": {"lookback": {"type": "integer"}, "mode": {"default": "fast"}},
            }

        assert registry.default_params(Required) == {"mode": "fast"}

    def test_a_class_with_no_schema_yields_nothing(self) -> None:
        class Bare(SmaCrossover):
            name = "test_bare_params"
            params_schema: ClassVar[dict[str, Any]] = {}

        assert registry.default_params(Bare) == {}


class TestSeedRecords:
    def test_a_record_per_registered_strategy(self) -> None:
        records = seed_script.seed_strategies()
        assert {record.id for record in records} == set(registry.all_strategies())

    def test_the_id_is_the_registered_name(self) -> None:
        """`Signal.strategy_id` carries the registered name everywhere in the
        platform, and both foreign keys resolve against it. A generated uuid
        here would leave every signal pointing at a row that does not exist."""
        by_id = {record.id: record for record in seed_script.seed_strategies()}
        assert by_id["sma_crossover"].name == "sma_crossover"
        assert by_id["sma_crossover"].class_name == "SmaCrossover"

    def test_records_carry_their_default_params(self) -> None:
        sma = next(r for r in seed_script.seed_strategies() if r.id == "sma_crossover")
        assert sma.params == {"fast_period": 20, "slow_period": 50, "timeframe": "1d"}

    def test_the_universe_is_left_empty(self) -> None:
        """The column records what a strategy is configured to trade. Filling it
        with reserved test tickers would be a seed inventing a configuration."""
        assert all(record.universe == () for record in seed_script.seed_strategies())


def _settings(
    *,
    env: str = "development",
    database_url: str = "postgresql+asyncpg://atp:atp@localhost:5432/atp",
) -> Settings:
    """`Settings` validates by alias, so `env` is passed as `ATP_ENV` while
    `database_url` — which has no alias — is passed by field name. Same asymmetry
    `test_config_guards.py` documents."""
    return Settings(ATP_ENV=env, database_url=database_url)  # type: ignore[call-arg]


class TestEnvironmentGuard:
    @pytest.fixture(autouse=True)
    def _no_ambient_environment(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """`Settings` reads the environment, correctly — which makes "what does
        the guard do with ATP_ENV=development" unanswerable on a machine that
        exports something else. Cleared rather than pinned: the values under
        test are the ones passed in."""
        for name in ("ATP_ENV", "DATABASE_URL"):
            monkeypatch.delenv(name, raising=False)

    def test_development_against_localhost_is_allowed(self) -> None:
        assert seed_script.refusal(_settings(), allow_remote_database=False) is None

    def test_the_compose_service_name_is_local(self) -> None:
        """`db` is what resolves inside the compose network, and a container
        running this is as local as the host running it."""
        settings = _settings(database_url="postgresql+asyncpg://atp:atp@db:5432/atp")
        assert seed_script.refusal(settings, allow_remote_database=False) is None

    @pytest.mark.parametrize("env", ["production", "staging"])
    def test_a_non_development_environment_is_refused(self, env: str) -> None:
        """Staging alongside production: fabricated bars in a staging database
        are read by whoever is validating a release against it."""
        reason = seed_script.refusal(_settings(env=env), allow_remote_database=False)
        assert reason is not None
        assert env in reason

    def test_the_environment_check_has_no_override(self) -> None:
        """The flag is about the host heuristic below it. If it also unlocked
        this, the guard would be a comment."""
        assert seed_script.refusal(_settings(env="production"), allow_remote_database=True)

    def test_a_remote_host_is_refused_by_default(self) -> None:
        """The accident this catches is a development `.env` still pointing at a
        shared database."""
        settings = _settings(database_url="postgresql+asyncpg://atp:atp@db.internal:5432/atp")
        reason = seed_script.refusal(settings, allow_remote_database=False)
        assert reason is not None
        assert "db.internal" in reason

    def test_a_remote_host_is_allowed_with_the_flag(self) -> None:
        """A remote development database is a real thing, and a guard that
        blocks legitimate work is one people learn to route around."""
        settings = _settings(database_url="postgresql+asyncpg://atp:atp@db.internal:5432/atp")
        assert seed_script.refusal(settings, allow_remote_database=True) is None


class TestArguments:
    def test_the_defaults_need_no_arguments(self) -> None:
        args = seed_script.parse_args([])
        assert args.symbols == ",".join(DEFAULT_SEED_SYMBOLS)
        assert args.start == DEFAULT_FIRST_DAY.isoformat()
        assert args.end == DEFAULT_LAST_DAY.isoformat()

    def test_a_malformed_day_is_refused(self) -> None:
        with pytest.raises(SystemExit, match="YYYY-MM-DD"):
            seed_script.parse_day("01/02/2023", "start")

    def test_a_day_parses_to_a_calendar_date(self) -> None:
        assert seed_script.parse_day("2023-04-05", "start") == date(2023, 4, 5)


class TestMainRefusesBeforeConnecting:
    """Each of these would otherwise be discovered after a database round trip,
    or — for the last one — not at all."""

    async def test_an_unreserved_symbol_stops_before_any_connection(self) -> None:
        with pytest.raises(SystemExit, match="SPY"):
            await seed_script.main(["--symbols", "SPY"])

    async def test_an_inverted_window_is_refused(self) -> None:
        with pytest.raises(SystemExit, match="on or before"):
            await seed_script.main(["--start", "2024-01-01", "--end", "2023-01-01"])

    async def test_seeding_nothing_is_refused(self) -> None:
        """Both switches together would connect, write nothing and report
        success — which a reader takes for "already seeded"."""
        with pytest.raises(SystemExit, match="seed nothing"):
            await seed_script.main(["--no-bars", "--no-strategies"])

    async def test_an_empty_symbol_list_is_refused(self) -> None:
        with pytest.raises(SystemExit, match="empty"):
            await seed_script.main(["--symbols", " , "])


def test_a_seeded_year_clears_a_crossover_strategys_warmup(year: list[Bar]) -> None:
    """The seed exists so somebody can run a backtest. `SmaCrossover(20, 50)`
    spends 51 bars on warmup, and a window leaving only a handful behind it
    would produce an empty run — which reads as a broken engine rather than as
    a short window."""
    assert len(year) > SmaCrossover({}).warmup_bars * 3


def test_the_default_window_is_three_years_of_sessions(calendar: TradingCalendar) -> None:
    assert 730 <= len(calendar.sessions(DEFAULT_FIRST_DAY, DEFAULT_LAST_DAY)) <= 760


async def test_a_non_positive_volatility_is_refused_before_connecting() -> None:
    """It would otherwise raise from inside the generation loop — after a
    connection is open and, for the first symbol, after the strategy rows are
    already written. A half-seeded database is a worse answer to a bad argument
    than a refusal."""
    with pytest.raises(SystemExit, match="volatility"):
        await seed_script.main(["--volatility", "0"])
