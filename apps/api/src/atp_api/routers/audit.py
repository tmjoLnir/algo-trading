"""The audit trail, for reading.

The write side is spread across the actions being audited; this is the one place
it is read. Requirement: the roadmap's "audit log surfaced in UI" — a record
nobody can see is a record nobody checks.

Readable by **any** session, including a read-only one. It is a GET, and the
question it answers — who did what — is one a read-only session is entitled to
ask; that session can already see the whole book, which is far more sensitive
than the fact that somebody signed in.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from atp_api.deps import get_session_factory
from atp_core.persistence.audit import PostgresAuditLog

router = APIRouter(prefix="/audit", tags=["audit"])


class AuditEntryView(BaseModel):
    """One row, as the dashboard reads it."""

    id: int
    at: datetime
    actor: str
    action: str
    target: str | None = None
    detail: dict[str, Any] = Field(default_factory=dict)


class AuditPage(BaseModel):
    entries: list[AuditEntryView] = Field(default_factory=list)
    #: Pass back as `before_id` for the next page. Null when this page is the
    #: end of the record. A cursor rather than a page number, because rows keep
    #: arriving while someone reads — most of all during whatever they are
    #: reading about.
    next_before_id: int | None = None


@router.get("", response_model=AuditPage)
async def list_audit_entries(
    session_factory: Annotated[async_sessionmaker[AsyncSession], Depends(get_session_factory)],
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
    before_id: Annotated[int | None, Query(ge=1)] = None,
    action: Annotated[str | None, Query(max_length=50)] = None,
) -> AuditPage:
    """Newest first.

    Depends on the database directly rather than through the tolerant sink the
    *writers* use. That asymmetry is deliberate and is the same rule the
    dashboard applies to the book: a write that cannot land must not stop the
    action, but a read that cannot happen must not be rendered as "nothing
    happened". An empty page and an unreachable record are different sentences,
    and only one of them is safe to believe — so a missing database is the 503
    `get_session_factory` already answers with.
    """
    log = PostgresAuditLog(session_factory)
    try:
        rows = await log.recent(limit=limit, before_id=before_id, action=action)
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"cannot read the audit trail: {exc}",
        ) from exc

    entries = [
        AuditEntryView(
            id=row_id,
            at=entry.at,
            actor=entry.actor,
            action=entry.action,
            target=entry.target,
            detail=entry.detail,
        )
        for row_id, entry in rows
    ]
    # Only when the page came back full. A short page is the end of the record,
    # and offering a cursor there would have the client fetch one empty page to
    # discover what this already knows.
    next_before_id = entries[-1].id if len(entries) == limit else None
    return AuditPage(entries=entries, next_before_id=next_before_id)
