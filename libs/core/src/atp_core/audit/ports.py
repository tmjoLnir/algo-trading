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

    Only actions that actually occur are listed. A constant for an event nothing
    emits is a claim the record does not support, so each lands with its handler
    — which is how `halt_engaged` arrived, how `strategy_created` did, and how
    the three close-out verbs below did. What is still missing is still missing
    for that reason: `POST /orders` (a manual order) and strategy promotion to
    live are `NotImplementedError` stubs, so nothing writes a verb for either.
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
    #: Written by both operator doors. `scripts/halt.py engage` reaches this
    #: record too, best-effort — it used to write nothing, so an incident
    #: stopped and resumed from the shell left no trace here at all
    #: (docs/paper-week/day-1-review.md, F9). Its `actor` is the script's own
    #: name and not the `--by` label: nothing authenticated that label, and an
    #: actor the caller filled in is not an audit trail (ADR 0008), so the
    #: claimed name travels in `detail["by"]` where a reader can see it for what
    #: it is. What still writes no row is the automated triggers inside the risk
    #: layer, which have no session at all and announce themselves through alerts
    #: and `risk.killswitch.engaged` instead. So an absent row means "not halted
    #: *by a human at either door*", never "not halted".
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
    #: same place, now narrowed to the same width: `scripts/halt.py clear` writes
    #: this row too, so an absent entry means "not resumed by a human at either
    #: door", never "still halted". Unlike the halt row, this one may name the
    #: operator account in `actor` — clearing proves a password at both doors,
    #: and an attribution backed by a credential is the kind this column is for.
    #:
    #: `detail["originally_engaged_at"]` says *which* halt this ended, and it is
    #: the only field that can: a resume and the engagement it answers are rows
    #: written hours apart, by different processes, and sometimes the engagement
    #: is a trigger that wrote no row at all.
    HALT_CLEARED = "halt_cleared"
    #: A strategy was stored. The first of the lifecycle verbs, and it arrives
    #: with `POST /api/v1/strategies` rather than ahead of it.
    #:
    #: Creating is the mildest act on the ratchet — the new row is `draft`, which
    #: authorises nothing — and it is still worth a row, because this is where a
    #: strategy's identity is minted. That name is the key every later signal and
    #: order carries, the rules stored under it are what a backtest snapshots,
    #: and nothing else in the platform records who decided any of it. The
    #: promotion verbs that would sit beside this one are still absent for the
    #: reason above: their handlers are stubs.
    #:
    #: Written *after* the row exists, like the two halt verbs, so an entry never
    #: claims a strategy that was refused.
    STRATEGY_CREATED = "strategy_created"
    #: The worker's trading configuration was saved — `PUT /worker/config`.
    #: The watchlist, the strategy and its parameters, the sizing, the stop and
    #: the live-orders lock all live in one row now, and this is the only thing
    #: that writes it. Before that row existed they were environment variables
    #: and a change left no trace anywhere: `.env` records neither an author nor
    #: a time, so "who widened the risk per trade, and when" was unanswerable.
    #:
    #: `detail` carries the fields that changed and their before/after values —
    #: none of them is a secret, and a row saying only "something changed" would
    #: not answer the question this verb exists for. `allow_live_orders` moving
    #: to true is the entry a post-mortem looks for, and it is the one field
    #: whose change required the operator's password on the way in.
    WORKER_CONFIG_UPDATED = "worker_config_updated"
    #: One working order cancelled by a human, through `DELETE /orders/{id}` or
    #: as part of `POST /orders/cancel-all`. Written *after* the venue confirms,
    #: like the halt verbs: an entry claiming a cancel that did not take would
    #: have a reader stop looking for an order that is still working.
    #:
    #: A cancel the platform performed for itself writes nothing here —
    #: `OrderRouter.cancel_protection` retiring a stop against a position it is
    #: closing is order flow, not a human decision. So an absent row means "not
    #: cancelled *by a person*", never "not cancelled".
    ORDER_CANCELLED = "order_cancelled"
    #: One position closed at market by a human, through
    #: `POST /positions/{symbol}/close`. Goes through `OrderRouter.flatten` like
    #: every other order (ADR 0005), so the risk chain can refuse it — and the
    #: row is written either way, with `detail["submitted"]` telling them apart.
    #: A refusal is the more interesting record of the two: somebody decided a
    #: position should be closed and the platform did not close it.
    POSITION_CLOSED = "position_closed"
    #: The emergency flatten — `POST /api/v1/risk/flatten-all`. The one action in
    #: the platform that reaches the venue *around* the risk chain, which is
    #: exactly why ADR 0005 makes an audit row part of the carve-out rather than
    #: a nicety. Records what was cancelled and what was closed, so the book
    #: before it can be reconstructed from the record alone.
    FLATTEN_ALL = "flatten_all"


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
