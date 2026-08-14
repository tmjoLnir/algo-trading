"""Alembic environment.

The database URL comes from `Settings`, never from alembic.ini — one place for
credentials, and none of them in a file that gets committed.
"""

from __future__ import annotations

import asyncio
import os
from logging.config import fileConfig

from alembic import context
from atp_core.persistence.models import Base
from sqlalchemy import pool
from sqlalchemy.ext.asyncio import async_engine_from_config

config = context.config
if config.config_file_name:
    fileConfig(config.config_file_name)

config.set_main_option(
    "sqlalchemy.url",
    os.environ.get("DATABASE_URL", "postgresql+asyncpg://atp:atp@localhost:5432/atp"),
)

target_metadata = Base.metadata


def include_object(obj, name, type_, reflected, compare_to):
    """Keep TimescaleDB's internal chunk tables out of autogenerate.

    Without this, every `make revision` proposes dropping the hypertable chunks —
    and one day someone will accept it.
    """
    if type_ == "table" and name.startswith(("_hyper_", "_timescaledb")):
        return False
    return True


def run_migrations_offline() -> None:
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        include_object=include_object,
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection) -> None:
    context.configure(
        connection=connection, target_metadata=target_metadata, include_object=include_object
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
