"""Asking for the password again, for an act that cannot be taken back.

Lifted out of the risk router when a second caller appeared. Two copies of this
would be two audit shapes for the same refusal and, worse, two chances for one
of them to drift into not asking — so there is one, and every act that needs a
named human at the keyboard goes through it.

Which acts those are is a short list and each earns its place by being
irreversible or by putting money at risk: clearing a halt, flattening the book,
and arming `allow_live_orders`. Halting is deliberately not among them (ADR
0009): hesitation is the expensive part of stopping.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from fastapi import HTTPException, status

from atp_api.auth import authenticate
from atp_core.audit.ports import Action, AuditEntry

if TYPE_CHECKING:
    from atp_core.audit.ports import AuditSink
    from atp_core.clock import Clock
    from atp_core.config import Settings


async def require_step_up(
    password: str,
    actor: str,
    settings: Settings,
    audit: AuditSink,
    clock: Clock,
    target: str,
) -> None:
    """Demand the password again, for an act that cannot be taken back.

    This is what finally enforces docs/RISK.md's "clearing requires a named
    human". A session cookie proves someone logged in at some point in the last
    twelve hours; it does not prove anyone is at the keyboard now. For halting
    that distinction does not matter — hesitation is the expensive part, and
    `/halt` deliberately asks for nothing. For clearing a halt and for
    liquidating the book it is the whole point.

    Deliberately no elevation window. A "recently authenticated" period would be
    a stretch of minutes during which a walked-away laptop can flatten the book,
    which is the exact situation this exists to prevent. The proof travels with
    the act instead.

    403 rather than 401: the session is valid and stays valid. Answering 401
    would send the dashboard to a login screen, which is not what went wrong.

    **A failure is recorded before it is raised.** `Action.FORBIDDEN` has always
    described itself as covering "a read-only session attempting a write, or a
    failed step-up (ADR 0009)", and `deps.require_write_scope` wrote the first
    of those from the start; this end wrote nothing, so the record has been
    claiming a coverage it did not have. The gap matters more here than there:
    a wrong password against `/resume` or `/flatten-all` is either the operator
    mistyping or somebody working through guesses with a stolen cookie, those
    look identical at the moment of refusal, and `rate_limited` only ever
    counted attempts at the *login* form. Without this row the second case
    leaves no trace anywhere.
    """
    if authenticate(actor, password, settings) is not None:
        return

    await audit.record(
        AuditEntry(
            at=clock.now(),
            actor=actor,
            action=Action.FORBIDDEN,
            target=target,
            # Named so this is distinguishable from a read-only session's
            # refusal, which shares the verb and would otherwise be indistinct
            # on the audit screen — one is a session in the wrong mode, the
            # other is a credential that did not check out.
            detail={"reason": "step_up_failed"},
        )
    )
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail="password required for this action",
    )
