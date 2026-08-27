"""The initial migration, against a real TimescaleDB.

No unit test can cover this. `bars` being a hypertable, chunked at the interval
we chose and compressed on a policy, is a property of the database rather than
of any Python object — the migration can be syntactically perfect and still
leave the platform's one unbounded table as an ordinary Postgres table.

The drift check earns its place too: `models.py` and the migrations are two
descriptions of one schema, and nothing but a real database can tell you they
have stopped agreeing.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import timedelta
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar, cast

import asyncpg
import pytest

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Iterator

pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parents[2]
ALEMBIC_INI = REPO_ROOT / "infra" / "alembic" / "alembic.ini"

#: Spelled out because `strategies` has seven NOT NULL columns with no defaults
#: and this file talks to the database directly — there is no repository here to
#: fill them in. Written wrong first: omitting `description` passed against a
#: hand-built table and failed against the real schema, which is the whole
#: reason these tests are integration tests.
_INSERT_STRATEGY = (
    "INSERT INTO strategies "
    "(id, name, description, kind, class_name, params, universe, timeframe, "
    " risk_config, state, created_at, updated_at) "
    "VALUES ($1, $1, 'a stand-in for a registered strategy', 'coded', 'Scripted', "
    "'{}', '[]', '1d', '{}', 'draft', now(), now()) "
    "ON CONFLICT (id) DO NOTHING"
)

#: Named, never `-1`. These tests exercise one specific migration, and a
#: relative target silently starts exercising a different one the moment
#: somebody adds a revision on top — which happened during the change that
#: introduced them: `downgrade -1` began dropping `backtest_runs.totals`
#: instead of shifting the curve, and the assertion that caught it was about
#: dates rather than about columns.
RETIME_REVISION = "c5e9a03b1f47"
BEFORE_RETIME = "a9f37c14e6b2"

STRATEGY = "retime-probe"
RUN_ID = "retime-run"
QUEUED_ID = "retime-queued"

#: Tables the initial migration owns. `alembic_version` is alembic's own
#: bookkeeping and survives a downgrade to base, so it is not in this list.
EXPECTED_TABLES = {
    "audit_log",
    "backtest_runs",
    "bars",
    "equity_snapshots",
    "fills",
    "orders",
    "position_snapshots",
    "signals",
    "strategies",
}


def _database_url() -> str:
    url = os.environ.get("DATABASE_URL")
    if not url:
        pytest.skip("DATABASE_URL is unset — start the stack with `make up`")
    return url


def _asyncpg_dsn(url: str) -> str:
    """asyncpg wants a bare postgres:// DSN, not SQLAlchemy's driver form."""
    return url.replace("postgresql+asyncpg://", "postgresql://")


def _run_alembic(*args: str) -> subprocess.CompletedProcess[str]:
    """Invoke the alembic CLI exactly as the Makefile does."""
    return subprocess.run(
        [sys.executable, "-m", "alembic", "-c", str(ALEMBIC_INI), *args],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        env={**os.environ},
        check=False,
    )


@pytest.fixture
async def migrated() -> str:
    """A database at head, with the extension the migration insists on.

    Creating the extension here mirrors `infra/db/init/01-timescaledb.sql`,
    which only runs for the compose volume — a CI service container never sees
    it. The migration deliberately will not create it itself.
    """
    url = _database_url()
    conn = await asyncpg.connect(_asyncpg_dsn(url))
    try:
        await conn.execute("CREATE EXTENSION IF NOT EXISTS timescaledb")
    finally:
        await conn.close()

    result = _run_alembic("upgrade", "head")
    assert result.returncode == 0, f"upgrade failed:\n{result.stdout}\n{result.stderr}"
    return url


async def _fetchval(url: str, query: str, *args: object) -> object:
    conn = await asyncpg.connect(_asyncpg_dsn(url))
    try:
        return await conn.fetchval(query, *args)
    finally:
        await conn.close()


async def test_migration_creates_every_table(migrated: str) -> None:
    conn = await asyncpg.connect(_asyncpg_dsn(migrated))
    try:
        rows = await conn.fetch(
            "SELECT table_name FROM information_schema.tables WHERE table_schema = 'public'"
        )
    finally:
        await conn.close()

    assert {row["table_name"] for row in rows} >= EXPECTED_TABLES


async def test_bars_is_a_hypertable(migrated: str) -> None:
    """The whole point of the migration's second half."""
    found = await _fetchval(
        migrated,
        "SELECT count(*) FROM timescaledb_information.hypertables WHERE hypertable_name = 'bars'",
    )
    assert found == 1, "bars was created as an ordinary table"


async def test_bars_is_partitioned_on_ts_at_the_chosen_interval(migrated: str) -> None:
    """A hypertable partitioned on the wrong column, or in monthly chunks, is
    still a hypertable — and still wrong."""
    column = await _fetchval(
        migrated,
        "SELECT column_name FROM timescaledb_information.dimensions "
        "WHERE hypertable_name = 'bars' AND dimension_number = 1",
    )
    interval = await _fetchval(
        migrated,
        "SELECT time_interval FROM timescaledb_information.dimensions "
        "WHERE hypertable_name = 'bars' AND dimension_number = 1",
    )

    assert column == "ts"
    assert interval == timedelta(days=7)


async def test_compression_is_enabled_with_a_policy(migrated: str) -> None:
    """Compression without a policy never actually compresses anything."""
    compression_enabled = await _fetchval(
        migrated,
        "SELECT compression_enabled FROM timescaledb_information.hypertables "
        "WHERE hypertable_name = 'bars'",
    )
    assert compression_enabled is True

    policies = await _fetchval(
        migrated,
        "SELECT count(*) FROM timescaledb_information.jobs "
        "WHERE hypertable_name = 'bars' AND proc_name = 'policy_compression'",
    )
    assert policies == 1


async def test_bars_carries_exactly_one_index(migrated: str) -> None:
    """The primary key on the natural key, and nothing else.

    Two things would break this. A second declaration on the same columns —
    the `UniqueConstraint` and `Index` this table used to carry — is a btree
    maintained per chunk of the largest table for no query the primary key
    does not already serve. And Timescale's own `bars_ts_idx`, which appears
    unless `create_default_indexes` is switched off in the migration.

    The count matters as much as the columns: an index created behind
    Alembic's back is one `models.py` cannot declare, and its absence from the
    model is drift that `alembic check` reports forever.
    """
    total = await _fetchval(
        migrated,
        "SELECT count(*) FROM pg_indexes WHERE tablename = 'bars' AND schemaname = 'public'",
    )
    on_natural_key = await _fetchval(
        migrated,
        "SELECT count(*) FROM pg_indexes "
        "WHERE tablename = 'bars' AND schemaname = 'public' "
        "AND indexdef LIKE '%(symbol, timeframe, ts)%'",
    )

    assert total == 1, "bars should carry only its primary key"
    assert on_natural_key == 1


async def test_client_order_id_is_unique(migrated: str) -> None:
    """The database half of order idempotency (CLAUDE.md §1.4) — a duplicate
    submit must fail loudly rather than open a second position."""
    exists = await _fetchval(
        migrated,
        "SELECT count(*) FROM pg_constraint "
        "WHERE conrelid = 'orders'::regclass AND conname = 'uq_orders_client_order_id'",
    )
    assert exists == 1


async def test_models_and_migrations_agree(migrated: str) -> None:
    """`alembic check` fails if models.py has drifted from the migrations.

    This is the test that stops a hand-edited model reaching production with no
    migration behind it.
    """
    result = _run_alembic("check")
    assert result.returncode == 0, (
        f"models.py has drifted from the migrations:\n{result.stdout}\n{result.stderr}"
    )


async def test_downgrade_removes_the_schema(migrated: str) -> None:
    """A migration that cannot be rolled back is a one-way door."""
    result = _run_alembic("downgrade", "base")
    assert result.returncode == 0, f"downgrade failed:\n{result.stdout}\n{result.stderr}"

    conn = await asyncpg.connect(_asyncpg_dsn(migrated))
    try:
        rows = await conn.fetch(
            "SELECT table_name FROM information_schema.tables WHERE table_schema = 'public'"
        )
    finally:
        await conn.close()

    remaining = {row["table_name"] for row in rows} & EXPECTED_TABLES
    assert not remaining, f"downgrade left tables behind: {sorted(remaining)}"


class TestTheCurveIsRetimed:
    """`c5e9a03b1f47` moves stored equity curves onto session dates.

    Data migrations are the ones a unit test cannot vouch for: what is under
    test is that alembic's own connection, over asyncpg, reads a JSON column
    back as a list, rewrites it, and writes it again — none of which is a
    property of the Python that does the rewriting.

    Driven backwards then forwards. The database arrives at head, so the only
    way to produce a row in the *old* convention is to run the downgrade, which
    is the half of a data migration nobody usually exercises.

    The target is named rather than relative — see `BEFORE_RETIME` above.
    """

    SESSION_DATES: ClassVar[list[list[str]]] = [
        ["2024-01-01T00:00:00+00:00", "100000"],  # a Monday
        ["2024-01-02T00:00:00+00:00", "100500"],
    ]

    async def _row(self, url: str, run_id: str) -> list[list[str]] | None:
        stored = await _fetchval(
            url, "SELECT equity_curve FROM backtest_runs WHERE id = $1", run_id
        )
        if stored is None:
            return None
        # asyncpg hands a `json` column back as text; a `jsonb` one comes back
        # decoded. Accepting both keeps this test about the migration.
        curve = json.loads(stored) if isinstance(stored, str) else stored
        assert isinstance(curve, list)
        return cast("list[list[str]]", curve)

    @pytest.fixture
    async def a_run(self, migrated: str) -> AsyncIterator[str]:
        """One `done` run whose curve is already on session dates."""
        conn = await asyncpg.connect(_asyncpg_dsn(migrated))
        try:
            await conn.execute(
                _INSERT_STRATEGY,
                STRATEGY,
            )
            await conn.execute(
                "INSERT INTO backtest_runs (id, strategy_id, config, status, equity_curve, "
                "queued_at) VALUES ($1, $2, $3, 'done', $4, now())",
                RUN_ID,
                STRATEGY,
                json.dumps({"timeframe": "1d", "symbols": ["SPY"]}),
                json.dumps(self.SESSION_DATES),
            )
            yield migrated
            await conn.execute("DELETE FROM backtest_runs WHERE id = $1", RUN_ID)
            await conn.execute("DELETE FROM strategies WHERE id = $1", STRATEGY)
        finally:
            await conn.close()

    @pytest.fixture
    def rolled_back(self) -> Iterator[None]:
        """The database one revision back, and at head again afterwards.

        Restored in teardown rather than at the end of each test: a test that
        fails while downgraded would otherwise hand the next one a database a
        revision behind, and the failure a reader sees would be that one.
        """
        assert _run_alembic("downgrade", BEFORE_RETIME).returncode == 0
        try:
            yield
        finally:
            assert _run_alembic("upgrade", "head").returncode == 0

    async def test_the_old_convention_is_a_day_late(self, a_run: str, rolled_back: None) -> None:
        """Establishes what the migration is correcting, from the database's
        own copy of it rather than from a description."""
        stale = await self._row(a_run, RUN_ID)

        assert stale is not None
        assert [point[0] for point in stale] == [
            "2024-01-02T00:00:00+00:00",  # Monday's session, filed on Tuesday
            "2024-01-03T00:00:00+00:00",
        ]

    async def test_upgrading_puts_every_point_back_on_its_session(self, a_run: str) -> None:
        assert _run_alembic("downgrade", BEFORE_RETIME).returncode == 0
        assert _run_alembic("upgrade", "head").returncode == 0

        assert await self._row(a_run, RUN_ID) == self.SESSION_DATES

    async def test_the_equity_beside_each_label_never_moves(self, a_run: str) -> None:
        """The property that makes this safe to run against results a human has
        already read: labels move, figures do not."""
        assert _run_alembic("downgrade", BEFORE_RETIME).returncode == 0
        stale = await self._row(a_run, RUN_ID)
        assert _run_alembic("upgrade", "head").returncode == 0
        fresh = await self._row(a_run, RUN_ID)

        assert stale is not None and fresh is not None
        assert [point[1] for point in stale] == [point[1] for point in fresh]

    async def test_a_run_with_no_curve_survives_both_directions(self, migrated: str) -> None:
        """A queued run has `equity_curve IS NULL`, and the migration selects on
        exactly that. A NULL it tried to rewrite would fail the whole deploy."""
        conn = await asyncpg.connect(_asyncpg_dsn(migrated))
        try:
            await conn.execute(
                _INSERT_STRATEGY,
                STRATEGY,
            )
            await conn.execute(
                "INSERT INTO backtest_runs (id, strategy_id, config, status, queued_at) "
                "VALUES ($1, $2, $3, 'queued', now())",
                QUEUED_ID,
                STRATEGY,
                json.dumps({"timeframe": "1d"}),
            )
            assert _run_alembic("downgrade", BEFORE_RETIME).returncode == 0
            assert _run_alembic("upgrade", "head").returncode == 0
            assert await self._row(migrated, QUEUED_ID) is None
        finally:
            await conn.execute("DELETE FROM backtest_runs WHERE id = $1", QUEUED_ID)
            await conn.execute("DELETE FROM strategies WHERE id = $1", STRATEGY)
            await conn.close()
