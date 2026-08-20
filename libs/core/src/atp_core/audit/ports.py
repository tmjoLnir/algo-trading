"""The append-only record of who did what.

Separate from `atp_core.logging` on purpose, and the distinction is worth being
explicit about because both are called "logging" in conversation.

`structlog` is the operational stream: everything the platform does, at whatever
volume it happens, read by whoever is debugging and rotated away afterwards. The
audit log is the durable answer to "who stopped trading on Tuesday" — a small
number of consequential events, kept, queryable, and shown on a screen. A grep
over a log file is not that: it depends on retention nobody set, on a format
nobody promised, and on the file still existing.

`AuditLogRow` has been in the schema and the migration since the initial commit
with nothing writing it and nothing reading it — the same state `SignalRow` is
still in. This is the port that changes that.

**A failed audit write must not fail the action.** The actions worth auditing
include halting trading, and a platform that refused to stop because Postgres
was down would have the failure mode exactly backwards. Adapters swallow and
log loudly; the caller is not asked to care. That is a real weakness and it is
the right one: a missing audit row is a gap in the record, and a refused halt is
a position nobody can close.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from datetime import datetime


@dataclass(frozen=True, slots=True)
class AuditEntry:
    """One consequential action, as it will be stored.

    Mirrors `persistence.models.AuditLogRow` deliberately: this is the shape the
    table already has, not a new one invented beside it.
    """

    #: When it happened. Passed in rather than defaulted, so it comes from the
    #: caller's clock (CLAUDE.md §1.2) and a test can pin it.
    at: datetime

    #: Who did it. From the session, never from the request body — an actor the
    #: caller fills in is not an audit trail (ADR 0008).
    actor: str

    #: What they did, as a stable machine-readable verb. See `Action` below;
    #: typed as `str` because the column is, and because an action this version
    #: does not know should still be storable and readable rather than crashing
    #: a page that lists it.
    action: str

    #: What it was done to — a symbol, an order id, a halt scope. Optional
    #: because plenty of actions have no object: signing in is not done *to*
    #: anything.
    target: str | None = None

    #: Everything else, unindexed. Deliberately loose: an audit row is read by a
    #: human after something went wrong, and the field that turns out to matter
    #: is never the one anyone predicted.
    detail: dict[str, Any] = field(default_factory=dict)


class Action:
    """The verbs written today.

    A class of constants rather than an enum, because the column is a string and
    stored history must stay readable when this list changes. An enum would make
    a row written by an older version unloadable by a newer one, which is
    precisely the property an append-only record must not have.

    Only actions that actually occur are listed. The remaining order-flow and
    strategy-lifecycle verbs the table's docstring anticipates — "manual order,
    strategy promotion to live" — are not here yet because those handlers are
    still `NotImplementedError` stubs, and a constant for an event nothing emits
    is a claim the record does not support. They land with their handlers, which
    is how `halt_engaged` arrived.
    """

    LOGIN = "login"
    LOGIN_FAILED = "login_failed"
    LOGOUT = "logout"
    #: A login refused because too many were attempted. Recorded separately from
    #: `login_failed` because the two answer different questions: one is "did
    #: someone mistype", the other is "is someone grinding".
    RATE_LIMITED = "rate_limited"
    #: An authenticated caller refused an action their session may not take — a
    #: read-only session attempting a write, or a failed step-up (ADR 0009).
    FORBIDDEN = "forbidden"
    #: Trading stopped from the API. Written *after* the switch is engaged, never
    #: before: an entry claiming a halt that did not take is worse than a halt
    #: with no entry, because the first is read as "we stopped" by whoever
    #: reviews the incident.
    #:
    #: Only halts engaged through the API reach this record. `scripts/halt.py`
    #: writes no audit row — it has no session to attribute one to — and neither
    #: do the automated triggers inside the risk layer, which announce
    #: themselves through alerts and `risk.killswitch.engaged` instead. So an
    #: absent row means "not halted *from the dashboard*", never "not halted".
    HALT_ENGAGED = "halt_engaged"
    #: Trading resumed from the API. The counterpart to `HALT_ENGAGED`, and the
    #: reason the pair is worth having: a halt with no clear beside it is still
    #: in force, so "when did we start trading again" is answerable only if the
    #: resume is recorded too. Written *after* the switch is cleared, for the
    #: inverse of the reason the halt row is — an entry claiming trading resumed
    #: when it did not would have someone stop looking for the thing still
    #: stopping it.
    #:
    #: Recorded even when the clear removed nothing. That case is not a
    #: no-op worth dropping: someone with the password decided the platform
    #: should be trading, and if it was not halted then whatever they were
    #: reacting to was somewhere else. `detail["was_halted"]` tells the two
    #: apart.
    #:
    #: Carries the same blind spot as `HALT_ENGAGED` and inherits it from the
    #: same place: `scripts/halt.py clear` writes no row, so an absent entry
    #: means "not resumed *from the dashboard*", never "still halted".
    HALT_CLEARED = "halt_cleared"


class AuditSink(Protocol):
    """Where audit entries go."""

    async def record(self, entry: AuditEntry) -> None:
        """Append one entry.

        Never raises. An implementation that cannot write must log loudly and
        return — see this module's docstring for why the caller is not given the
        chance to abandon the action over it.
        """
        ...

    async def recent(
        self,
        limit: int = 100,
        before_id: int | None = None,
        action: str | None = None,
    ) -> list[tuple[int, AuditEntry]]:
        """The newest entries first, with their row ids.

        Ids come back with the entries so a caller can page with `before_id`
        rather than an offset. Offset paging over an append-only table shifts
        under the reader every time a row arrives, which on this table is
        exactly while someone is reading it during an incident.
        """
        ...
