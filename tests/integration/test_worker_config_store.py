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
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import TYPE_CHECKING

import asyncpg
import pytest

from atp_core.persistence.db import create_engine, create_session_factory
from atp_core.persistence.worker_config import PostgresWorkerConfigRepository
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

        with pytest.raises(asyncpg.CheckViolationError):
            await raw.execute(
                "INSERT INTO worker_config "
                "(id, symbols, max_silence_seconds, strategy, strategy_params, "
                " sizing_method, sizing_value, stop_type, stop_multiplier, stop_period, "
                " allow_live_orders, revision, updated_at, updated_by) "
                "VALUES ('second', '[]', 60, '', '{}', 'risk_pct', 0.01, 'atr', 2, 14, "
                "        false, 1, now(), 'somebody')"
            )

    async def test_saving_twice_leaves_one_row(
        self, repo: PostgresWorkerConfigRepository, raw: asyncpg.Connection
    ) -> None:
        await repo.save(a_config(), actor="operator", at=NOW)
        await repo.save(a_config(strategy=""), actor="somebody-else", at=NOW)

        assert await raw.fetchval("SELECT count(*) FROM worker_config") == 1
        assert await raw.fetchval("SELECT updated_by FROM worker_config") == "somebody-else"
