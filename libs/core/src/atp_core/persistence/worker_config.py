"""Worker configuration storage — `WorkerConfigRepository` over PostgreSQL.

One row, keyed on one literal, written by one endpoint. The narrowness is
deliberate: this row decides what an unattended process trades and how much it
risks, so "who can write it" should be answerable in a sentence.

**The revision is allocated by the database, not by the caller.** A save reads
nothing first — it upserts, taking `revision + 1` from the row's own current
value in the same statement. Read-then-write would be a race between two
dashboards saving at once, and the losing side would not merely lose its edit:
it would write a revision number the other save had already used, which is the
one thing that must not happen to the counter the restart notice is derived
from.
"""

from __future__ import annotations

from decimal import Decimal
from typing import TYPE_CHECKING, Any, cast

from sqlalchemy.dialects.postgresql import insert as pg_insert

from atp_core.logging import get_logger
from atp_core.persistence.db import read_scope, session_scope
from atp_core.persistence.models import WORKER_CONFIG_ID, WorkerConfigRow
from atp_core.worker.config import (
    SizingMethod,
    StopTypeName,
    StoredWorkerConfig,
    WorkerConfig,
    normalise_symbols,
)

if TYPE_CHECKING:
    from datetime import datetime

    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from atp_core.worker.ports import WorkerConfigRepository

log = get_logger(__name__)


class PostgresWorkerConfigRepository:
    """`WorkerConfigRepository` over the `worker_config` table."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def load(self) -> StoredWorkerConfig | None:
        async with read_scope(self._session_factory) as session:
            row = await session.get(WorkerConfigRow, WORKER_CONFIG_ID)
            return None if row is None else _to_stored(row)

    async def save(self, config: WorkerConfig, *, actor: str, at: datetime) -> StoredWorkerConfig:
        """Upsert the single row, taking the next revision from the row itself.

        `excluded` carries the values being inserted; `WorkerConfigRow.revision`
        on the update side is the *stored* value, so `+ 1` is evaluated by
        Postgres against whatever is committed at that instant. Two concurrent
        saves therefore serialise into two distinct revisions rather than into
        one number written twice.
        """
        values = _values(config, actor=actor, at=at)
        statement = (
            pg_insert(WorkerConfigRow)
            .values(id=WORKER_CONFIG_ID, revision=1, **values)
            .on_conflict_do_update(
                index_elements=[WorkerConfigRow.id],
                set_={**values, "revision": WorkerConfigRow.revision + 1},
            )
            # Three columns rather than the whole entity: the configuration
            # coming back is the one just written, so the only things worth
            # reading are the ones the *database* decided. Returning the mapped
            # class would mean hydrating an ORM object out of a RETURNING clause
            # to learn one integer.
            .returning(
                WorkerConfigRow.revision,
                WorkerConfigRow.updated_at,
                WorkerConfigRow.updated_by,
            )
        )
        async with session_scope(self._session_factory) as session:
            row = (await session.execute(statement)).one()
            stored = StoredWorkerConfig(
                config=config,
                revision=row.revision,
                updated_at=row.updated_at,
                updated_by=row.updated_by,
            )
        log.info(
            "worker_config.saved",
            revision=stored.revision,
            actor=actor,
            strategy=config.strategy or None,
            symbols=list(config.symbols),
            allow_live_orders=config.allow_live_orders,
        )
        return stored


def _values(config: WorkerConfig, *, actor: str, at: datetime) -> dict[str, Any]:
    """The row's columns, minus its key and its revision."""
    return {
        "symbols": list(config.symbols),
        "max_silence_seconds": config.max_silence_seconds,
        "strategy": config.strategy,
        "strategy_params": dict(config.strategy_params),
        "sizing_method": config.sizing_method,
        "sizing_value": config.sizing_value,
        "stop_type": config.stop_type,
        "stop_multiplier": config.stop_multiplier,
        "stop_period": config.stop_period,
        "allow_live_orders": config.allow_live_orders,
        "updated_at": at,
        "updated_by": actor,
    }


def _trimmed(value: Decimal) -> Decimal:
    """Drop the trailing zeros `NUMERIC(20, 8)` pads a value with.

    `0.01` is stored as `0.01000000` and reads back that way, and the API sends
    Decimals as strings — so without this the settings form redraws a saved
    `0.01` as `0.01000000`, which looks like the platform changed the number.
    The value is identical either way (`Decimal` compares numerically), so this
    is presentation and nothing else.

    `normalize()` alone is not enough: it renders `100` as `1E+2`, which is a
    correct Decimal and an alarming thing to find in a sizing box.
    """
    normalised = value.normalize()
    exponent = normalised.as_tuple().exponent
    if isinstance(exponent, int) and exponent > 0:
        return normalised.quantize(Decimal(1))
    return normalised


def _to_stored(row: WorkerConfigRow) -> StoredWorkerConfig:
    """Rebuild the value object, which validates the row on the way out.

    A row that no longer satisfies `WorkerConfig` raises rather than loading —
    which is what makes an edit that predates a tightened rule fail loudly at
    the worker's next start instead of running on a value the platform has since
    decided is unsafe.
    """
    return StoredWorkerConfig(
        config=WorkerConfig(
            symbols=normalise_symbols(str(s) for s in row.symbols),
            max_silence_seconds=row.max_silence_seconds,
            strategy=row.strategy,
            strategy_params=dict(row.strategy_params),
            # Cast, then validated: `__post_init__` refuses a value outside
            # the vocabulary, so the narrowing is checked rather than assumed.
            sizing_method=cast("SizingMethod", row.sizing_method),
            sizing_value=_trimmed(row.sizing_value),
            stop_type=cast("StopTypeName", row.stop_type),
            stop_multiplier=_trimmed(row.stop_multiplier),
            stop_period=row.stop_period,
            allow_live_orders=row.allow_live_orders,
        ),
        revision=row.revision,
        updated_at=row.updated_at,
        updated_by=row.updated_by,
    )


if TYPE_CHECKING:
    # mypy enforces that the adapter still satisfies its port.
    def _conforms(adapter: PostgresWorkerConfigRepository) -> WorkerConfigRepository:
        return adapter
