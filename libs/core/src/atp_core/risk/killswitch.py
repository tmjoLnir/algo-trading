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

from atp_core import metrics
from atp_core.alerts.ports import Alert, Severity
from atp_core.channels import CHANNEL_HALTS
from atp_core.logging import get_logger

if TYPE_CHECKING:
    from redis import Redis

    from atp_core.alerts.ports import AlertSink

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

    def clear(
        self, scope: HaltScope, cleared_by: str, target: str | None = None
    ) -> HaltRecord | None:
        """Resume. Requires a named human; always audit-logged.

        Returns the halt this call removed, or `None` if nothing was engaged for
        this scope and target. Symmetric with `engage` returning the record in
        force, and for the same reason: there is no other race-free way for a
        caller to tell "I resumed trading" from "there was nothing to resume".
        Reading `active_halts` first would be two round trips with a gap in the
        middle, and the answer it gave could be wrong by the time the delete
        lands — which is the difference between an operator being told they
        restarted the platform and being told they did not.
        """
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

    def __init__(
        self,
        client: Redis,
        key_prefix: str = "atp:halt",
        *,
        alerts: AlertSink | None = None,
    ) -> None:
        self._client = client
        self.key_prefix = key_prefix
        #: Where a halt goes to reach a human who is not looking at a screen.
        #: Optional because the kill switch must work without one — an
        #: unalertable halt is still a halt, and refusing to construct without
        #: a sink would make a notification into a dependency of stopping.
        self._alerts = alerts

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
        # Counted here rather than at the top of the method, so that the
        # early return above — a halt that was already active — is not a second
        # incident on the graph. The Redis state is the deduplication, exactly
        # as it is for the notification (ADR 0012).
        metrics.halt_engaged(scope, reason)
        self._announce("engaged", record)
        self._alert_engaged(record)
        return record

    def clear(
        self, scope: HaltScope, cleared_by: str, target: str | None = None
    ) -> HaltRecord | None:
        """Resume. Requires a named human; always audit-logged.

        The asymmetry with `engage` is the point: stopping should be reflexive,
        restarting should not. An empty `cleared_by` is refused because "who
        decided it was safe to trade again" is the one question anyone asks
        afterwards, and an automated caller passing "" would answer it "nobody".

        Returns the halt that was removed, or `None` when there was nothing to
        remove — see `KillSwitch.clear` for why the caller has to be told which
        of the two happened.
        """
        if not cleared_by.strip():
            raise ValueError(
                "clearing a halt requires a named human — an anonymous clear is not an audit trail"
            )

        key = self._key(scope, target)
        raw = _sync(self._client.get(key))
        removed = bool(self._client.delete(key))
        record = _decode(raw) if raw is not None else None
        log.critical(
            "risk.killswitch.cleared",
            scope=scope.value,
            target=target,
            cleared_by=cleared_by,
            was_engaged=removed,
            original=record.engaged_by if record is not None else None,
        )
        if removed:
            metrics.halt_cleared(scope)
            self._announce(
                "cleared",
                record,
                scope=scope,
                target=target,
                actor=cleared_by,
            )
            self._alert_cleared(scope, target, cleared_by)
            return record
        # `removed` is the authority, not `raw`. The two disagree when the key
        # expired or another caller cleared it between the GET and the DELETE,
        # and in that window this call did not resume anything — returning the
        # record anyway would credit this operator with someone else's decision.
        return None

    def _alert_engaged(self, record: HaltRecord) -> None:
        """Tell a human trading stopped. Reached only by a *new* halt.

        Placed exactly where `_announce` is, and for the same reason: both sit
        after the state is durable in Redis, and both are announcements rather
        than mechanism. The placement is also what makes deduplication free —
        `engage` returns early when a halt is already active, so a staleness
        monitor re-engaging every five seconds sends one alert, not twelve a
        minute. The Redis state is the dedup, so there is no flag here to get
        out of step with it.

        Nothing from the book goes into the body (`alerts.ports`): the reason
        and the scope say what to go and look at, and the dashboard — behind
        authentication — is where the numbers are.
        """
        if self._alerts is None:
            return
        target = f" [{record.target}]" if record.target else ""
        lines = [f"{record.scope.value}{target} halted by {record.engaged_by}."]
        if record.detail:
            lines.append(record.detail)
        lines.append("Check the dashboard, then docs/RUNBOOK.md.")
        self._send_alert(
            Alert(
                severity=Severity.CRITICAL,
                title=f"Trading halted: {record.reason.value}",
                body="\n".join(lines),
                key=f"halt.{record.scope.value}.{record.target or 'all'}.{record.reason.value}",
                context={
                    "scope": record.scope.value,
                    "reason": record.reason.value,
                    "engaged_by": record.engaged_by,
                },
            )
        )

    def _alert_cleared(self, scope: HaltScope, target: str | None, cleared_by: str) -> None:
        """Tell a human trading resumed. INFO, not CRITICAL.

        Resuming is somebody's deliberate decision and never a surprise to the
        person who made it — but it is news to anyone else who got the halt, and
        a halt with no matching all-clear is how an operator ends up assuming
        the platform is still stopped when it is not.
        """
        if self._alerts is None:
            return
        suffix = f" [{target}]" if target else ""
        self._send_alert(
            Alert(
                severity=Severity.INFO,
                title="Trading resumed",
                body=f"{scope.value}{suffix} cleared by {cleared_by}.",
                key=f"halt.{scope.value}.{target or 'all'}.cleared",
                context={"scope": scope.value, "cleared_by": cleared_by},
            )
        )

    def _send_alert(self, alert: Alert) -> None:
        """Send, and never let it matter if it fails.

        `AlertSink` tells implementations not to raise, and both of the ones in
        this codebase honour it. This exists because "must not raise" is a
        contract with third-party code on the other side of it — a future sink,
        or a `requests`-based one somebody adds in a hurry — and the cost of
        being wrong about that contract is an exception thrown out of the call
        that just stopped trading, making a successful halt look like a failed
        one. Same reasoning as `_announce` directly below, which has swallowed
        for the same reason since it was written.
        """
        if self._alerts is None:
            return
        try:
            self._alerts.send(alert)
        except Exception as exc:
            log.error(
                "risk.killswitch.alert_failed",
                key=alert.key,
                error=str(exc),
                msg="the halt IS in effect; only the notification was lost",
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


# `flatten_all_positions()` used to stand here as a stub, and is deliberately
# gone rather than filled in. The act now exists as
# `POST /api/v1/risk/flatten-all`, which is where ADR 0005 puts it: the carve-out
# it defends is a *human* calling `BrokerPort.close_all_positions()` behind a
# typed confirmation, a step-up password and an audit row, and it ends "no
# automated path may call either method". A module-level function in the risk
# layer is reachable by every automated path there is, and a second door to an
# irreversible act is worth less than the one door that carries the proofs.
#
# What kept the two apart is unchanged and still true: halting stops *new* risk,
# flattening *realises* existing P&L, and a data outage means stop trading — not
# dump the book into a market you currently cannot see. That is why the endpoint
# is separate from `engage()` rather than a flag on it, and why it reports
# whether the platform was halted when it ran instead of assuming it was.
