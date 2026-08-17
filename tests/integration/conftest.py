"""Shared plumbing for tests that need a real database.

`test_migrations.py` deliberately keeps its own alembic helpers — it is testing
alembic itself, and a fixture that migrated for it would be assuming the thing
under test. Everything else just wants a schema to work against, and gets it
from here.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING

import asyncpg
import pytest

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

#: Retries for the truncate below. Three is generous: the compression policy
#: run it can collide with compresses a handful of test rows in about a second.
_TRUNCATE_ATTEMPTS = 3
_TRUNCATE_RETRY_SECONDS = 1.0

REPO_ROOT = Path(__file__).resolve().parents[2]
ALEMBIC_INI = REPO_ROOT / "infra" / "alembic" / "alembic.ini"


def asyncpg_dsn(url: str) -> str:
    """asyncpg wants a bare postgres:// DSN, not SQLAlchemy's driver form."""
    return url.replace("postgresql+asyncpg://", "postgresql://")


@pytest.fixture
async def database_url() -> str:
    url = os.environ.get("DATABASE_URL")
    if not url:
        pytest.skip("DATABASE_URL is unset — start the stack with `make up`")
    return url


@pytest.fixture
async def migrated_db(database_url: str) -> str:
    """A database at head.

    The extension is created here because it mirrors
    `infra/db/init/01-timescaledb.sql`, which only runs for the compose volume —
    a CI service container never sees it, and the migration deliberately refuses
    to create it itself.
    """
    conn = await asyncpg.connect(asyncpg_dsn(database_url))
    try:
        await conn.execute("CREATE EXTENSION IF NOT EXISTS timescaledb")
    finally:
        await conn.close()

    result = subprocess.run(
        [sys.executable, "-m", "alembic", "-c", str(ALEMBIC_INI), "upgrade", "head"],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        env={**os.environ},
        check=False,
    )
    assert result.returncode == 0, f"upgrade failed:\n{result.stdout}\n{result.stderr}"
    await _unschedule_compression(database_url)
    return database_url


async def _unschedule_compression(database_url: str) -> None:
    """Stop TimescaleDB's compression policy running *during* the tests.

    The migration registers `add_compression_policy('bars', INTERVAL '30 days')`,
    which is right for production and hostile here. It is a background worker,
    it starts running as soon as the scheduler picks it up, and the fixtures'
    bars are dated well past the 30-day threshold — so it finds chunks to
    compress at the same moment `clean_bars` truncates the table. The two
    deadlock, observed in CI:

        Process 164: TRUNCATE TABLE bars
        Process 166: CALL _timescaledb_functions.policy_compression()
        deadlock detected ... while locking tuple in relation "dimension_slice"

    Whether they collide is pure timing, which is why the same commit passed
    one run and failed the next. Unscheduling removes the collision rather than
    tolerating it: retrying the truncate would leave a background job rewriting
    chunks underneath tests that assert on row counts.

    The job row is left in place, only descheduled — `test_migrations.py`
    asserts the policy *exists* (`count(*) = 1`), and that a policy is
    configured is exactly the production property worth pinning. Nothing here
    tests the scheduler.

    Best effort on purpose. If the view or the column ever moves, the failure
    to deschedule restores the pre-existing race, which is a flake; raising
    would fail every integration test outright, which is worse than the problem
    being fixed.
    """
    conn = await asyncpg.connect(asyncpg_dsn(database_url))
    try:
        await conn.execute(
            "SELECT alter_job(job_id, scheduled => false) "
            "FROM timescaledb_information.jobs "
            "WHERE proc_name = 'policy_compression' AND hypertable_name = 'bars'"
        )
    except asyncpg.PostgresError as exc:  # pragma: no cover - needs a live server
        print(f"could not deschedule the compression policy ({exc}); tests may flake")
    finally:
        await conn.close()


@pytest.fixture
async def clean_bars(migrated_db: str) -> AsyncIterator[str]:
    """An empty `bars` table.

    Truncated before rather than after, so a failed test leaves its rows behind
    to be inspected instead of tidying away the evidence.

    Retried on deadlock, which covers the one window `_unschedule_compression`
    cannot: descheduling stops future runs but does not abort a run already in
    flight, and the compression policy's first run starts seconds after the
    migration registers it. A deadlock is the retryable error by construction —
    the server has already aborted one side, so the blocker is gone by the time
    we ask again. This is not a substitute for the deschedule; without it every
    later run would race too, and no number of retries fixes a job that keeps
    rewriting chunks under a test asserting row counts.
    """
    conn = await asyncpg.connect(asyncpg_dsn(migrated_db))
    try:
        for attempt in range(_TRUNCATE_ATTEMPTS):
            try:
                await conn.execute("TRUNCATE TABLE bars")
                break
            except asyncpg.DeadlockDetectedError:
                if attempt == _TRUNCATE_ATTEMPTS - 1:
                    raise
                await asyncio.sleep(_TRUNCATE_RETRY_SECONDS)
    finally:
        await conn.close()
    yield migrated_db
