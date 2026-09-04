"""The worker configuration row against a real PostgreSQL.

Not a unit test, for the reason its neighbours give: what is worth checking here
is the database's behaviour rather than Python's. Three properties, and none of
them can be observed against a fake:

1. **The revision is allocated by the database.** `save` upserts and takes
   `revision + 1` from the row's own committed value in the same statement. A
   fake that incremented in Python would pass while the SQL was wrong, and the
   number is what the dashboard's restart notice is derived from.
2. **The table holds exactly one row, by constraint.** `ck_worker_config_single_row`
   is a CHECK on the primary key, so a second configuration cannot be inserted
   even by hand. A convention would read identically right up until the day two
   rows exist and the worker and the dashboard disagree about which is in force.
3. **`NUMERIC(20, 8)` pads, and the read path trims it back.** `0.01` is stored
   as `0.01000000`; the API sends Decimals as strings, so without the trim the
   settings form redraws a saved value as a different-looking one.
4. **The eight risk ceilings share all three of those behaviours** (ADR 0025),
   and the fourth is theirs alone: the column *rounds* rather than refusing, and
   the row is re-validated on the way out — so a value the value object accepted
   but the column could not hold exactly would come back outside its own bound
   and raise on every read, on the row every screen and the worker's boot need.
   The guard against that is in `RiskLimits`, and this is where it is confirmed
   against the database it is a guard about.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import TYPE_CHECKING

import asyncpg
import pytest

from atp_core.errors import ConfigError
from atp_core.persistence.db import create_engine, create_session_factory
from atp_core.persistence.worker_config import PostgresWorkerConfigRepository
from atp_core.risk.limits import MAX_COUNT, MAX_GROSS_CEILING, MAX_TAKE_PROFIT, RiskLimits
from atp_core.worker import WorkerConfig

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

pytestmark = pytest.mark.integration

NOW = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)


def _asyncpg_dsn(url: str) -> str:
    return url.replace("postgresql+asyncpg://", "postgresql://")


@pytest.fixture
async def repo(migrated_db: str) -> AsyncIterator[PostgresWorkerConfigRepository]:
    """An empty `worker_config`, and a repository over it."""
    connection = await asyncpg.connect(_asyncpg_dsn(migrated_db))
    try:
        await connection.execute("TRUNCATE worker_config")
    finally:
        await connection.close()

    engine = create_engine(migrated_db)
    try:
        yield PostgresWorkerConfigRepository(create_session_factory(engine))
    finally:
        await engine.dispose()


@pytest.fixture
async def raw(migrated_db: str) -> AsyncIterator[asyncpg.Connection]:
    connection = await asyncpg.connect(_asyncpg_dsn(migrated_db))
    try:
        yield connection
    finally:
        await connection.close()


def a_config(**overrides: object) -> WorkerConfig:
    base: dict[str, object] = {
        "symbols": ("SPY", "QQQ"),
        "strategy": "sma_crossover",
        "strategy_params": {"fast_period": 20},
    }
    base.update(overrides)
    return WorkerConfig(**base)  # type: ignore[arg-type]


class TestTheRoundTrip:
    async def test_nothing_saved_reads_as_none(self, repo: PostgresWorkerConfigRepository) -> None:
        """An ordinary state — a fresh database — and it must not be confused
        with a read failure, which raises."""
        assert await repo.load() is None

    async def test_every_field_survives(self, repo: PostgresWorkerConfigRepository) -> None:
        saved = a_config(
            max_silence_seconds=45,
            # Not the default, so the column is proved to carry the value rather
            # than the row happening to agree with `WorkerConfig`'s fallback.
            timeframe="5m",
            sizing_method="fixed_qty",
            sizing_value=Decimal("3"),
            stop_type="chandelier",
            stop_multiplier=Decimal("2.5"),
            stop_period=21,
            allow_live_orders=True,
        )
        await repo.save(saved, actor="operator", at=NOW)

        stored = await repo.load()
        assert stored is not None
        assert stored.config == saved
        assert stored.updated_by == "operator"
        assert stored.updated_at == NOW

    async def test_numeric_padding_is_trimmed_on_the_way_out(
        self, repo: PostgresWorkerConfigRepository
    ) -> None:
        """`NUMERIC(20, 8)` stores `0.01` as `0.01000000`. Equal as Decimals,
        different as the strings the API sends — and the form renders the
        string."""
        await repo.save(a_config(), actor="operator", at=NOW)

        stored = await repo.load()
        assert stored is not None
        assert str(stored.config.sizing_value) == "0.01"
        assert str(stored.config.stop_multiplier) == "2"

    async def test_json_columns_come_back_as_themselves(
        self, repo: PostgresWorkerConfigRepository
    ) -> None:
        await repo.save(a_config(), actor="operator", at=NOW)

        stored = await repo.load()
        assert stored is not None
        assert stored.config.symbols == ("SPY", "QQQ")
        assert stored.config.strategy_params == {"fast_period": 20}


class TestTheRiskCeilings:
    """The eight columns ADR 0025 added, against the real numeric types.

    Not covered by `test_every_field_survives` above: that saves the defaults for
    all eight, so a repository that dropped every risk column and rebuilt
    `DEFAULT_RISK_LIMITS` on load would pass it. These save values that are not
    the defaults, which is the only way to tell a round trip from a fallback.
    """

    async def test_every_ceiling_survives_the_round_trip(
        self, repo: PostgresWorkerConfigRepository
    ) -> None:
        tightened = RiskLimits(
            max_position_pct=Decimal("0.04"),
            max_gross_exposure_pct=Decimal("2.5"),
            max_daily_loss_pct=Decimal("0.015"),
            max_orders_per_minute=5,
            max_open_positions=6,
            max_quote_age_seconds=10,
            default_stop_loss_pct=Decimal("0.011"),
            default_take_profit_pct=Decimal("9.5"),
        )
        await repo.save(a_config(risk=tightened), actor="operator", at=NOW)

        stored = await repo.load()
        assert stored is not None
        assert stored.config.risk == tightened

    async def test_the_extreme_of_every_bound_is_storable(
        self, repo: PostgresWorkerConfigRepository
    ) -> None:
        """A bound the value object allows but the column cannot hold would be a
        value that saves and then will not load — and the widest value of each is
        exactly where `NUMERIC(20, 8)` and `Integer` run out."""
        edge = RiskLimits(
            max_position_pct=Decimal("1"),
            max_gross_exposure_pct=MAX_GROSS_CEILING,
            max_daily_loss_pct=Decimal("1"),
            max_orders_per_minute=MAX_COUNT,
            max_open_positions=MAX_COUNT,
            max_quote_age_seconds=MAX_COUNT,
            default_stop_loss_pct=Decimal("0.99999999"),
            default_take_profit_pct=MAX_TAKE_PROFIT,
        )
        await repo.save(a_config(risk=edge), actor="operator", at=NOW)

        stored = await repo.load()
        assert stored is not None
        assert stored.config.risk == edge

    async def test_the_finest_storable_precision_survives_exactly(
        self, repo: PostgresWorkerConfigRepository
    ) -> None:
        """Eight decimal places is what the value object permits, so eight
        decimal places has to come back unrounded — otherwise the guard is one
        place out and the bound it protects is still reachable."""
        fine = RiskLimits(max_position_pct=Decimal("0.12345678"))
        await repo.save(a_config(risk=fine), actor="operator", at=NOW)

        stored = await repo.load()
        assert stored is not None
        assert stored.config.risk.max_position_pct == Decimal("0.12345678")

    async def test_padding_is_trimmed_on_the_ceilings_too(
        self, repo: PostgresWorkerConfigRepository
    ) -> None:
        """`0.10` comes back as `0.10000000` untrimmed, and the form renders the
        string the API sends — so a saved ceiling would redraw itself as a
        different-looking number the operator did not type."""
        await repo.save(a_config(), actor="operator", at=NOW)

        stored = await repo.load()
        assert stored is not None
        assert str(stored.config.risk.max_position_pct) == "0.1"
        assert str(stored.config.risk.max_daily_loss_pct) == "0.03"

    async def test_a_row_written_past_the_precision_guard_refuses_to_load(
        self, repo: PostgresWorkerConfigRepository, raw: asyncpg.Connection
    ) -> None:
        """The guard is in Python, so the database can still be handed such a
        value by hand — and when it is, the read path must fail loudly rather
        than serve a ceiling that is not the one stored.

        This is the failure the guard exists to keep unreachable *through the
        API*; asserting it here is what says the guard is the only thing keeping
        it unreachable, rather than something else quietly clamping.
        """
        await repo.save(a_config(), actor="operator", at=NOW)
        # Rounds to 1.00000000, which the exclusive bound refuses.
        await raw.execute("UPDATE worker_config SET risk_default_stop_loss_pct = 0.999999999")

        with pytest.raises(ConfigError, match="default_stop_loss_pct"):
            await repo.load()


class TestTheRevision:
    async def test_the_first_save_is_revision_one(
        self, repo: PostgresWorkerConfigRepository
    ) -> None:
        """Zero is reserved for "a worker booted before anything was saved",
        which the dashboard must be able to tell from this."""
        assert (await repo.save(a_config(), actor="operator", at=NOW)).revision == 1

    async def test_every_later_save_increments_it(
        self, repo: PostgresWorkerConfigRepository
    ) -> None:
        """The upsert reads the stored value in the same statement, so this is
        the database counting rather than the caller."""
        await repo.save(a_config(), actor="operator", at=NOW)
        second = await repo.save(a_config(strategy=""), actor="operator", at=NOW)

        assert second.revision == 2

    async def test_a_save_that_changed_nothing_still_increments_it(
        self, repo: PostgresWorkerConfigRepository
    ) -> None:
        """ "Somebody looked at this and pressed save" is a fact worth keeping,
        and a revision that only moved on a diff would make the restart notice
        depend on what changed rather than on when."""
        await repo.save(a_config(), actor="operator", at=NOW)
        again = await repo.save(a_config(), actor="operator", at=NOW)

        assert again.revision == 2

    async def test_the_returned_revision_is_the_stored_one(
        self, repo: PostgresWorkerConfigRepository
    ) -> None:
        """The save's RETURNING and a fresh read must agree — the dashboard
        renders one and the worker boots on the other."""
        saved = await repo.save(a_config(), actor="operator", at=NOW)

        stored = await repo.load()
        assert stored is not None
        assert stored.revision == saved.revision


class TestOnlyOneRow:
    async def test_a_second_configuration_is_refused_by_the_database(
        self, repo: PostgresWorkerConfigRepository, raw: asyncpg.Connection
    ) -> None:
        """A `SELECT ... LIMIT 1` over an unconstrained table reads identically
        until the day two rows exist, at which point the worker and the
        dashboard can silently disagree about which is in force."""
        await repo.save(a_config(), actor="operator", at=NOW)

        # Every column, including `timeframe` and the eight ceilings — none of
        # which has a server default, deliberately (each migration drops the one
        # it needs to backfill). An INSERT that omitted one would trip the NOT
        # NULL constraint *before* reaching the CHECK this is about, and pass or
        # fail for a reason that has nothing to do with single-row-ness.
        #
        # That is not hypothetical: `timeframe` was added without being listed
        # here, and this test failed with `NotNullViolationError` — the comment
        # above describing the trap it had just fallen into. Any later NOT NULL
        # column has to be added to both halves of this statement.
        with pytest.raises(asyncpg.CheckViolationError):
            await raw.execute(
                "INSERT INTO worker_config "
                "(id, symbols, max_silence_seconds, strategy, strategy_params, timeframe, "
                " sizing_method, sizing_value, stop_type, stop_multiplier, stop_period, "
                " allow_live_orders, revision, updated_at, updated_by, "
                " risk_max_position_pct, risk_max_gross_exposure_pct, risk_max_daily_loss_pct, "
                " risk_max_orders_per_minute, risk_max_open_positions, risk_max_quote_age_seconds, "
                " risk_default_stop_loss_pct, risk_default_take_profit_pct) "
                "VALUES ('second', '[]', 60, '', '{}', '1m', 'risk_pct', 0.01, 'atr', 2, 14, "
                "        false, 1, now(), 'somebody', "
                "        0.10, 1.00, 0.03, 30, 20, 30, 0.02, 0.06)"
            )

    async def test_saving_twice_leaves_one_row(
        self, repo: PostgresWorkerConfigRepository, raw: asyncpg.Connection
    ) -> None:
        await repo.save(a_config(), actor="operator", at=NOW)
        await repo.save(a_config(strategy=""), actor="somebody-else", at=NOW)

        assert await raw.fetchval("SELECT count(*) FROM worker_config") == 1
        assert await raw.fetchval("SELECT updated_by FROM worker_config") == "somebody-else"
