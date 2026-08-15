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

import os
import subprocess
import sys
from datetime import timedelta
from pathlib import Path

import asyncpg
import pytest

pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parents[2]
ALEMBIC_INI = REPO_ROOT / "infra" / "alembic" / "alembic.ini"

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


async def test_bars_carries_no_duplicate_index(migrated: str) -> None:
    """The natural key is the primary key, and nothing else indexes those
    columns. A second btree on them would be maintained on every chunk of the
    largest table while serving no query the primary key does not.

    Counting all indexes would be the wrong assertion: Timescale adds its own
    `bars_ts_idx` on (ts DESC), because `create_default_indexes` is on by
    default and our primary key leads with `symbol` rather than the
    partitioning column. That index is not a duplicate — it serves time-ranged
    scans across symbols — so it is left alone.
    """
    on_natural_key = await _fetchval(
        migrated,
        "SELECT count(*) FROM pg_indexes "
        "WHERE tablename = 'bars' AND schemaname = 'public' "
        "AND indexdef LIKE '%(symbol, timeframe, ts)%'",
    )
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
