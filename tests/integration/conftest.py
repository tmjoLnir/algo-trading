"""Shared plumbing for tests that need a real database.

`test_migrations.py` deliberately keeps its own alembic helpers — it is testing
alembic itself, and a fixture that migrated for it would be assuming the thing
under test. Everything else just wants a schema to work against, and gets it
from here.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING

import asyncpg
import pytest

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

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
    return database_url


@pytest.fixture
async def clean_bars(migrated_db: str) -> AsyncIterator[str]:
    """An empty `bars` table.

    Truncated before rather than after, so a failed test leaves its rows behind
    to be inspected instead of tidying away the evidence.
    """
    conn = await asyncpg.connect(asyncpg_dsn(migrated_db))
    try:
        await conn.execute("TRUNCATE TABLE bars")
    finally:
        await conn.close()
    yield migrated_db
