"""The audit trail — the `AuditSink` port over PostgreSQL.

Postgres rather than Redis, and the choice follows from what the record is for.
Redis holds things the platform needs *now* and can rebuild if it loses them:
the latest quote, the halt state, the published book. An audit row answers a
question asked weeks later, when nobody has the context to reconstruct anything,
and `persistence.events` already states the rule this obeys — *nothing that must
not be lost may travel over pub/sub*.

Append-only in practice as well as in intent: nothing here updates or deletes.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import select

from atp_core.audit.ports import AuditEntry
from atp_core.logging import get_logger
from atp_core.persistence.db import session_scope
from atp_core.persistence.models import AuditLogRow

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

log = get_logger(__name__)


class PostgresAuditLog:
    """`AuditSink` over the `audit_log` table."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def record(self, entry: AuditEntry) -> None:
        """Append one entry, and never raise.

        The swallow is the whole design decision, so it is worth stating where
        it happens rather than only in the port's docstring: the actions worth
        auditing include halting trading, and a platform that refused to stop
        because Postgres was unreachable would have its failure modes exactly
        inverted. A missing row is a gap in the record; a refused halt is a
        position nobody can close.

        `CRITICAL` rather than `error`, because a silent gap in an append-only
        record is the kind of thing discovered only when someone needs the row
        that is not there — and by then the outage that caused it is long over.
        """
        try:
            async with session_scope(self._session_factory) as session:
                session.add(
                    AuditLogRow(
                        ts=entry.at,
                        actor=entry.actor,
                        action=entry.action,
                        target=entry.target,
                        detail=dict(entry.detail),
                    )
                )
        except Exception as exc:
            log.critical(
                "audit.write_failed",
                error=str(exc),
                action=entry.action,
                actor=entry.actor,
                effect="the action proceeded; this event is missing from the audit trail",
            )

    async def recent(
        self,
        limit: int = 100,
        before_id: int | None = None,
        action: str | None = None,
    ) -> list[tuple[int, AuditEntry]]:
        """Newest first, optionally filtered, optionally paged by row id.

        Keyset paging on the primary key rather than `OFFSET`: rows arrive while
        the page is being read — most of all during the incident someone is
        reading it about — and an offset shifts under the reader every time one
        does, which shows up as a duplicated or skipped row rather than as an
        error.

        Ordered by id and not by `ts`. They almost always agree, and where they
        do not it is because two rows share a timestamp, in which case id is the
        only total order available and the one that matches insertion.
        """
        statement = select(AuditLogRow).order_by(AuditLogRow.id.desc()).limit(limit)
        if before_id is not None:
            statement = statement.where(AuditLogRow.id < before_id)
        if action is not None:
            statement = statement.where(AuditLogRow.action == action)

        async with session_scope(self._session_factory) as session:
            rows = (await session.execute(statement)).scalars().all()

        return [
            (
                row.id,
                AuditEntry(
                    at=row.ts,
                    actor=row.actor,
                    action=row.action,
                    target=row.target,
                    detail=dict(row.detail or {}),
                ),
            )
            for row in rows
        ]
