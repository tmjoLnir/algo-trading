"""The kill switch — stop everything, now.

State lives in Redis, not in process memory, for three reasons: the API process
must be able to trip it while the worker is mid-loop; it survives a worker
restart (a switch that clears on restart is worse than none, because a crash
loop would silently resume trading); and every process sees the same value.

Engaging is instant and requires no confirmation. Clearing is deliberate,
requires a human identity, and is audit-logged. That asymmetry is intentional:
stopping should be reflexive, restarting should not be.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import TYPE_CHECKING, Any, Protocol, cast

from atp_core.channels import CHANNEL_HALTS
from atp_core.logging import get_logger

if TYPE_CHECKING:
    from redis import Redis

log = get_logger(__name__)


class HaltScope(StrEnum):
    GLOBAL = "global"  # nothing trades
    STRATEGY = "strategy"  # one strategy halted
    SYMBOL = "symbol"  # one instrument halted


class HaltReason(StrEnum):
    MANUAL = "manual"
    DAILY_LOSS_LIMIT = "daily_loss_limit"
    RECONCILIATION_MISMATCH = "reconciliation_mismatch"
    DATA_FEED_LOST = "data_feed_lost"
    BROKER_UNREACHABLE = "broker_unreachable"
    RATE_LIMIT_STORM = "rate_limit_storm"
    UNHANDLED_EXCEPTION = "unhandled_exception"


@dataclass(frozen=True, slots=True)
class HaltRecord:
    scope: HaltScope
    reason: HaltReason
    engaged_at: datetime
    engaged_by: str
    detail: str = ""
    target: str | None = None  # strategy_id or symbol when scope is not GLOBAL


class KillSwitch(Protocol):
    def is_engaged(self, strategy_id: str | None = None, symbol: str | None = None) -> bool:
        """True if this order is covered by any active halt.

        Checked by `KillSwitchRule` before every single order.
        """
        ...

    def engage(
        self,
        scope: HaltScope,
        reason: HaltReason,
        engaged_by: str,
        detail: str = "",
        target: str | None = None,
    ) -> HaltRecord:
        """Halt immediately. Idempotent — re-engaging an active halt is fine."""
        ...

    def clear(self, scope: HaltScope, cleared_by: str, target: str | None = None) -> None:
        """Resume. Requires a named human; always audit-logged."""
        ...

    def active_halts(self) -> list[HaltRecord]:
        """Everything currently halted — rendered as a banner on the dashboard."""
        ...


#: redis-py types its sync client's returns as `Awaitable[Any] | Any`, because
#: one class serves both the sync and async APIs. Every call below is against
#: the synchronous client, so the awaitable half is unreachable — narrowed here
#: rather than with an ignore on each call site, which would suppress real
#: errors alongside this one.
def _sync(value: object) -> Any:
    return cast("Any", value)


def _encode(record: HaltRecord) -> str:
    return json.dumps(
        {
            "scope": record.scope.value,
            "reason": record.reason.value,
            "engaged_at": record.engaged_at.isoformat(),
            "engaged_by": record.engaged_by,
            "detail": record.detail,
            "target": record.target,
        }
    )


def _decode(raw: str | bytes) -> HaltRecord:
    payload: dict[str, Any] = json.loads(raw)
    return HaltRecord(
        scope=HaltScope(payload["scope"]),
        reason=HaltReason(payload["reason"]),
        engaged_at=datetime.fromisoformat(payload["engaged_at"]),
        engaged_by=payload["engaged_by"],
        detail=payload.get("detail", ""),
        target=payload.get("target"),
    )


class RedisKillSwitch:
    """Redis-backed implementation. See docs/SAFETY.md.

    Takes a client rather than a URL, like `RedisQuoteCache`: the client owns a
    connection pool, and core does not open sockets on its own behalf
    (CLAUDE.md §1.3). The stub's `redis_url` signature would have had this
    module dialling out, which is the rule that keeps core testable.

    Synchronous, unlike the quote cache, because `KillSwitchRule.check` is —
    the risk chain is a synchronous decision on the path of every order, and
    making it async to reach one key would colour the whole chain.
    """

    def __init__(self, client: Redis, key_prefix: str = "atp:halt") -> None:
        self._client = client
        self.key_prefix = key_prefix

    def _key(self, scope: HaltScope, target: str | None) -> str:
        if scope is HaltScope.GLOBAL:
            return f"{self.key_prefix}:global"
        if not target:
            raise ValueError(f"a {scope.value}-scoped halt needs a target")
        return f"{self.key_prefix}:{scope.value}:{target}"

    def is_engaged(self, strategy_id: str | None = None, symbol: str | None = None) -> bool:
        """True if this order is covered by any active halt.

        **Fails closed.** If Redis cannot be reached, this returns True and
        trading stops. docs/SAFETY.md names that explicitly as how layer 6
        fails, and the reasoning is one-sided: a false halt costs missed
        opportunity, while a false clear trades an account through whatever
        made Redis unreachable in the first place.
        """
        keys = [self._key(HaltScope.GLOBAL, None)]
        if strategy_id:
            keys.append(self._key(HaltScope.STRATEGY, strategy_id))
        if symbol:
            keys.append(self._key(HaltScope.SYMBOL, symbol))

        try:
            return any(value is not None for value in _sync(self._client.mget(keys)))
        except Exception as exc:
            log.critical(
                "risk.killswitch.unreachable",
                error=str(exc),
                effect="failing closed — refusing the order",
            )
            return True

    def engage(
        self,
        scope: HaltScope,
        reason: HaltReason,
        engaged_by: str,
        detail: str = "",
        target: str | None = None,
    ) -> HaltRecord:
        """Halt immediately. Idempotent — re-engaging an active halt is fine.

        An existing halt is returned unchanged rather than overwritten, so the
        record keeps who stopped trading and when it first stopped. A second
        engagement is not new information, and letting it reset the timestamp
        would erase the only audit trail of the original.

        Deliberately no error handling: engaging must never fail quietly. If
        Redis is unreachable the exception propagates, and `is_engaged` is
        already failing closed on the same outage.
        """
        key = self._key(scope, target)
        existing = _sync(self._client.get(key))
        if existing is not None:
            return _decode(existing)

        record = HaltRecord(
            scope=scope,
            reason=reason,
            engaged_at=datetime.now(UTC),
            engaged_by=engaged_by,
            detail=detail,
            target=target,
        )
        self._client.set(key, _encode(record))
        log.critical(
            "risk.killswitch.engaged",
            scope=scope.value,
            reason=reason.value,
            engaged_by=engaged_by,
            target=target,
            detail=detail,
        )
        self._announce("engaged", record)
        return record

    def clear(self, scope: HaltScope, cleared_by: str, target: str | None = None) -> None:
        """Resume. Requires a named human; always audit-logged.

        The asymmetry with `engage` is the point: stopping should be reflexive,
        restarting should not. An empty `cleared_by` is refused because "who
        decided it was safe to trade again" is the one question anyone asks
        afterwards, and an automated caller passing "" would answer it "nobody".
        """
        if not cleared_by.strip():
            raise ValueError(
                "clearing a halt requires a named human — an anonymous clear is not an audit trail"
            )

        key = self._key(scope, target)
        record = _sync(self._client.get(key))
        removed = bool(self._client.delete(key))
        log.critical(
            "risk.killswitch.cleared",
            scope=scope.value,
            target=target,
            cleared_by=cleared_by,
            was_engaged=removed,
            original=_decode(record).engaged_by if record is not None else None,
        )
        if removed:
            self._announce(
                "cleared",
                _decode(record) if record is not None else None,
                scope=scope,
                target=target,
                actor=cleared_by,
            )

    def _announce(
        self,
        transition: str,
        record: HaltRecord | None,
        *,
        scope: HaltScope | None = None,
        target: str | None = None,
        actor: str | None = None,
    ) -> None:
        """Tell every open dashboard, immediately. Never let it matter if it fails.

        The state is already in Redis before this runs, and the state is what
        every risk check reads — this is an announcement, not the mechanism. So
        it is swallowed: `engage` promises that halting never fails quietly, and
        an exception raised here would break that promise in the one direction
        that matters, by making an unpublishable halt look like a halt that did
        not happen.

        Without it a halt reaches the screen on the dashboard's next five-minute
        poll. `atp_api.ws` fans these out to every client regardless of what it
        subscribed to, because a trading halt is not something to opt into, and
        five minutes is a long time to be looking at a screen that says trading
        is fine.
        """
        message: dict[str, Any] = {
            "type": "halt",
            "transition": transition,
            "scope": (record.scope if record is not None else scope or HaltScope.GLOBAL).value,
            "target": record.target if record is not None else target,
        }
        if record is not None:
            message["reason"] = record.reason.value
            message["engaged_at"] = record.engaged_at.isoformat()
            message["engaged_by"] = record.engaged_by
            message["detail"] = record.detail
        if actor is not None:
            message["actor"] = actor

        try:
            self._client.publish(CHANNEL_HALTS, json.dumps(message))
        except Exception as exc:
            log.error(
                "risk.killswitch.announce_failed",
                transition=transition,
                error=str(exc),
                msg="the halt IS in effect; only the live notification was lost",
            )

    def active_halts(self) -> list[HaltRecord]:
        """Everything currently halted — rendered as a banner on the dashboard.

        Lets a Redis failure raise rather than returning an empty list. This is
        a display read, and "nothing is halted" is exactly the wrong thing to
        show a human when the truth is unknown.
        """
        keys = list(_sync(self._client.scan_iter(match=f"{self.key_prefix}:*")))
        if not keys:
            return []
        return [_decode(v) for v in _sync(self._client.mget(keys)) if v is not None]


def flatten_all_positions() -> None:
    """Emergency liquidation: cancel every open order, close every position.

    Separate from `engage()` on purpose. Halting stops *new* risk; flattening
    *realises* existing P&L and is not always the right response to a problem —
    a data outage means stop trading, not dump the book into a market you
    currently cannot see. Requires explicit human action.
    """
    raise NotImplementedError("see docs/RUNBOOK.md 'Emergency flatten'")
