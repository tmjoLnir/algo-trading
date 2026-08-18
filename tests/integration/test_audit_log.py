"""The audit trail against a real PostgreSQL.

Not a unit test, for the reasons its neighbours give: what is worth checking is
the database's behaviour rather than Python's. Whether `JSON` hands back the
dict that went in, whether keyset paging over `BIGSERIAL` actually excludes what
it should, and — the one that matters most — whether a write that cannot land
returns quietly instead of taking the caller down with it.

`AuditLogRow` has been in the schema since the initial migration with nothing
writing it. These are the first tests that put a row in it.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import asyncpg
import pytest

from atp_core.audit import Action, AuditEntry
from atp_core.persistence.audit import PostgresAuditLog
from atp_core.persistence.db import create_engine, create_session_factory

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

pytestmark = pytest.mark.integration

NOW = datetime(2026, 8, 18, 12, 0, tzinfo=UTC)


def _asyncpg_dsn(url: str) -> str:
    return url.replace("postgresql+asyncpg://", "postgresql://")


@pytest.fixture
async def audit_log(migrated_db: str) -> AsyncIterator[PostgresAuditLog]:
    """An empty `audit_log`, and a repository over it."""
    connection = await asyncpg.connect(_asyncpg_dsn(migrated_db))
    try:
        await connection.execute("TRUNCATE audit_log RESTART IDENTITY")
    finally:
        await connection.close()

    engine = create_engine(migrated_db)
    try:
        yield PostgresAuditLog(create_session_factory(engine))
    finally:
        await engine.dispose()


async def _write(log: PostgresAuditLog, count: int, action: str = Action.LOGIN) -> None:
    for index in range(count):
        await log.record(
            AuditEntry(
                at=NOW + timedelta(seconds=index),
                actor="operator",
                action=action,
                target="203.0.113.7",
                detail={"index": index},
            )
        )


class TestWriting:
    async def test_an_entry_survives_the_round_trip_intact(
        self, audit_log: PostgresAuditLog
    ) -> None:
        """Including `detail`, which is JSON and the easiest field to lose."""
        await audit_log.record(
            AuditEntry(
                at=NOW,
                actor="operator",
                action=Action.LOGIN,
                target="203.0.113.7",
                detail={"scope": "read", "nested": {"count": 3}},
            )
        )

        ((_, entry),) = await audit_log.recent()

        assert entry.actor == "operator"
        assert entry.action == Action.LOGIN
        assert entry.target == "203.0.113.7"
        assert entry.detail == {"scope": "read", "nested": {"count": 3}}
        assert entry.at == NOW

    async def test_an_entry_with_no_target_is_fine(self, audit_log: PostgresAuditLog) -> None:
        """Signing out is not done *to* anything."""
        await audit_log.record(AuditEntry(at=NOW, actor="operator", action=Action.LOGOUT))

        ((_, entry),) = await audit_log.recent()

        assert entry.target is None
        assert entry.detail == {}

    async def test_a_write_that_cannot_land_does_not_raise(self) -> None:
        """The design decision this whole module rests on.

        The actions being audited include halting trading. A sink that raised
        would let a database outage stop someone flattening a book, which has
        the failure modes exactly inverted — a missing row is a gap in the
        record, a refused halt is a position nobody can close.
        """
        engine = create_engine("postgresql+asyncpg://nobody:nobody@127.0.0.1:1/nope")
        unreachable = PostgresAuditLog(create_session_factory(engine))
        try:
            # No `pytest.raises`: returning normally *is* the assertion.
            await unreachable.record(AuditEntry(at=NOW, actor="operator", action=Action.LOGIN))
        finally:
            await engine.dispose()


class TestReading:
    async def test_newest_first(self, audit_log: PostgresAuditLog) -> None:
        await _write(audit_log, 3)

        rows = await audit_log.recent()

        assert [entry.detail["index"] for _, entry in rows] == [2, 1, 0]

    async def test_the_limit_is_honoured(self, audit_log: PostgresAuditLog) -> None:
        await _write(audit_log, 5)

        assert len(await audit_log.recent(limit=2)) == 2

    async def test_filtering_by_action(self, audit_log: PostgresAuditLog) -> None:
        await _write(audit_log, 2, action=Action.LOGIN)
        await _write(audit_log, 3, action=Action.LOGIN_FAILED)

        assert len(await audit_log.recent(action=Action.LOGIN)) == 2
        assert len(await audit_log.recent(action=Action.LOGIN_FAILED)) == 3

    async def test_paging_by_id_never_repeats_or_skips_a_row(
        self, audit_log: PostgresAuditLog
    ) -> None:
        """Keyset paging, and why it is not `OFFSET`.

        Rows keep arriving while someone reads — most of all during whatever
        incident they are reading about. An offset shifts under the reader every
        time one does; an id does not. Here a row is inserted *between* the two
        page reads, and the second page must still be the rows below the first.
        """
        await _write(audit_log, 4)

        first = await audit_log.recent(limit=2)
        await _write(audit_log, 1, action=Action.LOGOUT)  # arrives mid-read
        second = await audit_log.recent(limit=2, before_id=first[-1][0])

        first_ids = {row_id for row_id, _ in first}
        second_ids = {row_id for row_id, _ in second}
        assert first_ids.isdisjoint(second_ids)
        assert max(second_ids) < min(first_ids)

    async def test_an_empty_trail_reads_as_empty_rather_than_failing(
        self, audit_log: PostgresAuditLog
    ) -> None:
        assert await audit_log.recent() == []
