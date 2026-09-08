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
from dataclasses import dataclass, replace
from datetime import datetime
from enum import StrEnum
from typing import TYPE_CHECKING, Any, Protocol, cast

from atp_core import metrics
from atp_core.alerts.ports import Alert, Severity
from atp_core.channels import CHANNEL_HALTS
from atp_core.clock import SystemClock
from atp_core.errors import KillSwitchUnavailableError
from atp_core.logging import get_logger

if TYPE_CHECKING:
    from collections.abc import Collection, Sequence

    from redis import Redis

    from atp_core.alerts.ports import AlertSink
    from atp_core.clock import Clock

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
class HaltEscalation:
    """The reason this halt was engaged under, before it was raised.

    Written at most once. It exists because `reason` is the reason *in force* —
    the field the banner, the alert and the metrics read — and moving that
    field would otherwise erase what the person or process who actually stopped
    trading chose at the time.
    """

    from_reason: HaltReason
    at: datetime
    by: str
    detail: str = ""


@dataclass(frozen=True, slots=True)
class Impugnment:
    """One event that put named positions beyond proof.

    `symbols` plural and not one symbol: a reconcile compares the whole book at
    a single instant, and its findings are one event rather than five.

    This is what `KillSwitchRule`'s exit carve-out actually reads (ADR 0029) —
    *not* `reason`. A halt's reason answers "what stopped trading"; an
    impugnment answers "which positions we cannot prove". They are different
    questions, and the same `HaltReason` warrants opposite verdicts depending
    on the evidence behind it: `broker_unreachable` from
    `OrderRouter._resolve_indeterminate` names the one order whose outcome is
    unknown, while `broker_unreachable` from the reconciler means only that we
    could not read the venue — an unverified book, not a disproven one.
    """

    #: Sorted, non-empty, uppercase tickers.
    symbols: tuple[str, ...]
    reason: HaltReason
    at: datetime
    by: str
    detail: str = ""


@dataclass(frozen=True, slots=True)
class HaltRecord:
    scope: HaltScope
    #: The reason **in force**. Every existing reader — the dashboard banner,
    #: `_alert_engaged`, `metrics.halt_engaged`, `rollover_daily_counters`,
    #: `HaltEngagedView`, `_halt_summary` — means this one, so it is this field
    #: that moves when a halt is raised and `escalation` that keeps the original.
    reason: HaltReason
    #: When trading stopped. Never moves — a halt that re-stamps itself erases
    #: the only evidence of when it actually started.
    engaged_at: datetime
    #: Who stopped it. Never moves, for the same reason.
    engaged_by: str
    detail: str = ""
    target: str | None = None  # strategy_id or symbol when scope is not GLOBAL
    escalation: HaltEscalation | None = None
    #: Append-only. Everything the platform has found that it cannot prove.
    impugned: tuple[Impugnment, ...] = ()

    @property
    def unproven_symbols(self) -> frozenset[str]:
        """The symbols whose held quantity cannot be relied on to size an exit.

        Derived rather than stored beside `impugned`, so the set the rule reads
        and the evidence an operator reads can never disagree.
        """
        return frozenset(symbol for item in self.impugned for symbol in item.symbols)

    @property
    def book_is_unproven(self) -> bool:
        return bool(self.impugned)


def _merge(existing: HaltRecord, incoming: HaltRecord) -> HaltRecord:
    """The record that should stand, given one already does.

    A **one-way latch**. `engaged_at`, `engaged_by` and `detail` never move: a
    halt that re-stamps itself erases the only evidence of when trading actually
    stopped. What can move is `reason`, and only upward — from a halt that
    proves nothing about the book to one that does — with the original kept in
    `escalation`.

    `impugned` is appended to, and only when the incoming engage names a symbol
    no standing impugnment already covers. That clause is not a nicety: the
    scheduled reconcile runs every five minutes and a mismatch stands until a
    human fixes it, so without it a two-hour incident appends twenty-four
    identical impugnments and fires twenty-four alerts. The Redis state is the
    dedup, exactly as ADR 0012 already has it for the halt notification.

    Pure and total, so the whole latch is one testable function rather than a
    branch tangled into a Redis round trip.
    """
    fresh = [
        item for item in incoming.impugned if not set(item.symbols) <= existing.unproven_symbols
    ]
    impugned = existing.impugned + tuple(fresh)

    # The reason rises once, when evidence arrives at a halt that had none.
    # Never falls, never moves sideways: an operator pressing HALT beside a
    # standing `broker_unreachable` cannot loosen it, and `manual` then
    # `data_feed_lost` is the no-change case it is today.
    escalates = bool(fresh) and not existing.impugned
    if not escalates:
        return existing if not fresh else replace(existing, impugned=impugned)

    return replace(
        existing,
        reason=incoming.reason,
        impugned=impugned,
        escalation=HaltEscalation(
            from_reason=existing.reason,
            at=incoming.engaged_at,
            by=incoming.engaged_by,
            detail=incoming.detail,
        ),
    )


@dataclass(frozen=True, slots=True)
class HaltState:
    """Everything the switch knows about the halts covering **one** order.

    `unreadable` is a third answer rather than an empty `halts`, because
    "halted for no reason we could read" and "halted for reasons we read, none
    of which impugn a position" are different states and a caller that
    conflates them silently picks a policy nobody chose.
    """

    halts: tuple[HaltRecord, ...] = ()
    unreadable: bool = False

    @property
    def engaged(self) -> bool:
        return self.unreadable or bool(self.halts)

    def position_is_unproven(self, symbol: str) -> bool:
        """Whether any halt covering this order says *this symbol's* quantity
        cannot be relied on.

        `unreadable` deliberately does **not** count. A Redis outage is not
        evidence about the book: it is evidence that we cannot read halt
        metadata, and the two are unrelated. Treating it as impugnment would
        refuse every exit and every protective stop on every Redis blip —
        docs/SAFETY.md's layers 5 and 6 failing together, on an outage that
        says nothing about any position. The halt itself still stands, because
        `engaged` is true; only the carve-out is unaffected.
        """
        return any(symbol in halt.unproven_symbols for halt in self.halts)


class KillSwitch(Protocol):
    def halt_state(self, strategy_id: str | None = None, symbol: str | None = None) -> HaltState:
        """Every halt covering this order, and what each says it cannot prove.

        Read by `KillSwitchRule` before every single order. **The Protocol
        deliberately offers no bare boolean**: a rule that asks only "am I
        halted" gets the pre-ADR 0029 behaviour and lets a flatten through
        against a quantity nobody can vouch for. `RedisKillSwitch.is_engaged`
        still exists for callers that genuinely want the boolean and are not
        risk rules — the staleness monitor, the dashboard, `scripts/halt.py` —
        but it is not on the contract a rule is handed.

        Fails closed exactly as `is_engaged` does, returning
        `HaltState(unreadable=True)`, so there is one fail-closed policy for
        this switch rather than two.
        """
        ...

    def engage(
        self,
        scope: HaltScope,
        reason: HaltReason,
        engaged_by: str,
        detail: str = "",
        target: str | None = None,
        *,
        unproven_symbols: Collection[str] = (),
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


#: How many times `engage` will retry a contended write before giving up. Three
#: because the contention it is for is two processes reacting to one incident,
#: not a hot loop — and because failing loudly beats spinning on the path that
#: stops trading.
_MAX_ENGAGE_ATTEMPTS = 3

#: Compare-and-set, as one round trip. `WATCH`/`MULTI` would need a dedicated
#: connection out of the pool and a transaction the sync client holds open; a
#: two-line script is the same guarantee without either.
_CAS = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
  redis.call('SET', KEYS[1], ARGV[2]); return 1
else return 0 end
"""

#: Read and remove in one step. `GETDEL` would do it in one command but needs
#: Redis 6.2, and this module has no version floor of its own; the script is the
#: same guarantee against anything that can run `_CAS`.
#:
#: It has to be atomic for a reason that did not exist before ADR 0029. A
#: standing halt's bytes used to be immutable — `engage` returned early without
#: writing — so a GET-then-DELETE had nothing to lose in the gap between them.
#: `engage` now rewrites the record in place while the same halt stands, so an
#: escalation can land inside that gap and be deleted unseen: the operator is
#: told they resumed the halt they engaged at lunchtime, the audit row records
#: that halt, and what actually went away was a reconciliation halt naming a
#: symbol nobody can prove — which is now closeable.
_GETDEL = """
local v = redis.call('GET', KEYS[1])
if v then redis.call('DEL', KEYS[1]) end
return v
"""


def _clean_symbols(symbols: Collection[str]) -> tuple[str, ...]:
    """Sorted, unique, uppercase tickers — or a `ValueError`.

    A reconcile's cash discrepancy carries `_NO_SYMBOL`, which is the empty
    string, so a blank must never enter here as if it named an instrument: it
    would be a symbol no order can match and an impugnment nothing can clear.
    """
    cleaned = {s.strip().upper() for s in symbols}
    if any(not s for s in cleaned):
        raise ValueError("an impugned symbol cannot be blank")
    return tuple(sorted(cleaned))


#: redis-py types its sync client's returns as `Awaitable[Any] | Any`, because
#: one class serves both the sync and async APIs. Every call in this module is
#: against the synchronous client, so the awaitable half is unreachable —
#: narrowed here rather than with an ignore on each call site, which would
#: suppress real errors alongside this one.
def _sync(value: object) -> Any:
    return cast("Any", value)


def _encode(record: HaltRecord) -> str:
    payload: dict[str, Any] = {
        "scope": record.scope.value,
        "reason": record.reason.value,
        "engaged_at": record.engaged_at.isoformat(),
        "engaged_by": record.engaged_by,
        "detail": record.detail,
        "target": record.target,
    }
    # Written only when present, so a record from before ADR 0029 and one
    # engaged today with nothing impugned encode identically. A rolling deploy
    # then cannot tell them apart, which is the point.
    if record.escalation is not None:
        payload["escalation"] = {
            "from_reason": record.escalation.from_reason.value,
            "at": record.escalation.at.isoformat(),
            "by": record.escalation.by,
            "detail": record.escalation.detail,
        }
    if record.impugned:
        payload["impugned"] = [
            {
                "symbols": list(item.symbols),
                "reason": item.reason.value,
                "at": item.at.isoformat(),
                "by": item.by,
                "detail": item.detail,
            }
            for item in record.impugned
        ]
    return json.dumps(payload)


def _decode(raw: str | bytes) -> HaltRecord:
    payload: dict[str, Any] = json.loads(raw)
    # `.get` on both new fields: a record written by a process from before
    # ADR 0029 reads back as un-escalated and un-impugned, which is the
    # default-closed answer — nothing is claimed to be proven that was not.
    escalation_payload = payload.get("escalation")
    escalation = (
        HaltEscalation(
            from_reason=HaltReason(escalation_payload["from_reason"]),
            at=datetime.fromisoformat(escalation_payload["at"]),
            by=escalation_payload["by"],
            detail=escalation_payload.get("detail", ""),
        )
        if escalation_payload is not None
        else None
    )
    return HaltRecord(
        scope=HaltScope(payload["scope"]),
        reason=HaltReason(payload["reason"]),
        engaged_at=datetime.fromisoformat(payload["engaged_at"]),
        engaged_by=payload["engaged_by"],
        detail=payload.get("detail", ""),
        target=payload.get("target"),
        escalation=escalation,
        impugned=tuple(
            Impugnment(
                symbols=tuple(item["symbols"]),
                reason=HaltReason(item["reason"]),
                at=datetime.fromisoformat(item["at"]),
                by=item["by"],
                detail=item.get("detail", ""),
            )
            for item in payload.get("impugned", ())
        ),
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
        clock: Clock | None = None,
    ) -> None:
        self._client = client
        #: Injected, because `HaltRecord.engaged_at` is now evidence a risk rule
        #: reads rather than only an audit field — and because every sibling
        #: adapter in `libs/core` takes one (CLAUDE.md §1.2, AUDIT.md #49).
        #: Defaults to the system clock so the nine existing construction sites
        #: are untouched.
        self._clock = clock or SystemClock()
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

    def _covering_keys(self, strategy_id: str | None, symbol: str | None) -> list[str]:
        """The keys a halt covering this order could live under.

        One place, so `halt_state` and `is_engaged` cannot come to disagree
        about what "covering" means — which is the drift that would let an
        order be refused by one and permitted by the other.
        """
        keys = [self._key(HaltScope.GLOBAL, None)]
        if strategy_id:
            keys.append(self._key(HaltScope.STRATEGY, strategy_id))
        if symbol:
            keys.append(self._key(HaltScope.SYMBOL, symbol))
        return keys

    def halt_state(self, strategy_id: str | None = None, symbol: str | None = None) -> HaltState:
        """Every halt covering this order, decoded. One round trip.

        **Fails closed.** If Redis cannot be reached — or a record will not
        decode, which a newer process writing an unknown `HaltReason` during a
        rolling deploy would cause — this reports `unreadable`, `engaged` is
        true and trading stops. docs/SAFETY.md names that as how layer 6 fails,
        and the reasoning is one-sided: a false halt costs missed opportunity,
        while a false clear trades an account through whatever made Redis
        unreachable in the first place.

        The decode is inside the `try` deliberately. A record we cannot read is
        exactly as unreadable as a Redis we cannot reach, and letting a
        `ValueError` out of the risk chain would be worse than either.

        **But each record is decoded on its own.** Up to three halts cover one
        order and they are independent documents; one of them being unreadable
        is no reason to discard what the others said. Decoding them as a single
        generator inside one `try` did exactly that, and it reopened the defect
        this whole mechanism exists to close: a symbol halt impugning SPY beside
        a global halt written by a newer deploy with an unknown `HaltReason`
        collapsed to `unreadable=True`, which by design impugns nothing — so the
        flatten against SPY's disputed quantity was approved. Reproduced by
        execution before this was changed.

        `unreadable` is still set, so the order is still refused unless it
        reduces; what it no longer does is *erase* evidence already in hand. The
        result is at least as strict on both axes as either alternative.
        """
        keys = self._covering_keys(strategy_id, symbol)
        try:
            raw = _sync(self._client.mget(keys))
        except Exception as exc:
            log.critical(
                "risk.killswitch.unreachable",
                error=str(exc),
                effect="failing closed — refusing the order",
            )
            return HaltState(unreadable=True)

        halts: list[HaltRecord] = []
        unreadable = False
        for key, value in zip(keys, raw, strict=True):
            if value is None:
                continue
            try:
                halts.append(_decode(value))
            except Exception as exc:
                # A rolling deploy writing a `HaltReason` this process does not
                # know. Loud, and fails closed via `unreadable` — but the halts
                # beside it keep their impugnments.
                unreadable = True
                log.critical(
                    "risk.killswitch.undecodable_record",
                    key=key,
                    error=str(exc),
                    effect="failing closed on this record; the others still count",
                )
        return HaltState(halts=tuple(halts), unreadable=unreadable)

    def is_engaged(self, strategy_id: str | None = None, symbol: str | None = None) -> bool:
        """True if this order is covered by any active halt.

        **Not for a risk rule** — `KillSwitchRule` reads `halt_state`, because a
        bare boolean cannot say which positions a halt puts beyond proof and a
        rule that asks this question lets a flatten through against a quantity
        nobody can vouch for (ADR 0029). This is for the callers that genuinely
        want the boolean: the staleness monitor deciding whether to re-halt, the
        dashboard's banner, `scripts/halt.py status`. It is deliberately absent
        from the `KillSwitch` Protocol so a rule cannot reach it through the
        contract it is handed.

        Fails closed, by deferring to `halt_state`.
        """
        return self.halt_state(strategy_id, symbol).engaged

    def engage(
        self,
        scope: HaltScope,
        reason: HaltReason,
        engaged_by: str,
        detail: str = "",
        target: str | None = None,
        *,
        unproven_symbols: Collection[str] = (),
    ) -> HaltRecord:
        """Halt immediately. Idempotent — re-engaging an active halt is fine.

        An existing halt is returned unchanged rather than overwritten, so the
        record keeps who stopped trading and when it first stopped. A second
        engagement is not new information, and letting it reset the timestamp
        would erase the only audit trail of the original.

        Deliberately no error handling: engaging must never fail quietly. If
        Redis is unreachable the exception propagates, and `halt_state` is
        already failing closed on the same outage.

        Raises `KillSwitchUnavailableError` when the record could not be written
        after `_MAX_ENGAGE_ATTEMPTS` contended rounds. That is a *different*
        failure from an unreachable store and callers must not conflate them:
        there, nothing is written and nothing is halted; here, a halt is
        standing and only this call's evidence is missing. The message says so.
        """
        key = self._key(scope, target)
        symbols = _clean_symbols(unproven_symbols)
        #: What the *last* round saw at the key. Only meaningful if the loop
        #: falls through, where it is the difference between two opposite
        #: messages — see the raise below.
        last_saw_a_halt = False

        for _attempt in range(_MAX_ENGAGE_ATTEMPTS):
            now = self._clock.now()
            record = HaltRecord(
                scope=scope,
                reason=reason,
                engaged_at=now,
                engaged_by=engaged_by,
                detail=detail,
                target=target,
                impugned=(
                    (Impugnment(symbols, reason, now, engaged_by, detail),) if symbols else ()
                ),
            )
            # `SET NX` rather than GET-then-SET: one atomic round trip, which is
            # what AUDIT.md finding 48 asks for. It matters more now than it did
            # then — a lost update used to cost an audit field, and would now
            # cost the impugnment the exit carve-out reads.
            if _sync(self._client.set(key, _encode(record), nx=True)):
                self._announce_engaged(record)
                return record

            raw = _sync(self._client.get(key))
            if raw is None:
                # Cleared or expired between the SET and the GET. Round again
                # rather than dereferencing None — which would raise out of the
                # platform's stop button with no halt in force.
                last_saw_a_halt = False
                continue
            last_saw_a_halt = True

            existing = _decode(raw)
            merged = _merge(existing, record)
            if merged == existing:
                return existing  # already covered. No write, no second alert.
            if _sync(self._client.eval(_CAS, 1, key, raw, _encode(merged))):
                self._announce_escalated(existing, merged)
                return merged
            # Someone else wrote between our read and our write. Round again.

        # What is known here is worth stating precisely, because two opposite
        # states reach this line and an operator mid-incident acts differently
        # on each. `last_saw_a_halt` is the only thing that separates them, and
        # asserting either one unconditionally is a lie half the time.
        #
        #   occupied  — every round found the key taken. The store answered and
        #               a halt was standing at the last look, so trading IS
        #               stopped; what did not land is this call's reason and,
        #               far more importantly, its impugnment. Telling an
        #               operator "the halt may not be in force" would send them
        #               to re-halt an already halted platform while the symbol
        #               nobody can prove goes on being flattenable.
        #
        #   gone      — the last round found the key cleared, so a `SET NX` was
        #               about to be tried and never was. Nothing is recorded and
        #               nothing may be halted. Claiming "trading IS halted" here
        #               is the more dangerous error of the two: it tells someone
        #               to walk away from a live account.
        effect = (
            "a halt stands, but this engage's evidence was not merged into it"
            if last_saw_a_halt
            else "the key was cleared under us and nothing was recorded — no halt may be in force"
        )
        log.critical(
            "risk.killswitch.engage_contended",
            scope=scope.value,
            target=target,
            reason=reason.value,
            unproven=list(symbols),
            halt_seen=last_saw_a_halt,
            effect=effect,
        )
        lost = (
            f" — {', '.join(symbols)} is NOT recorded as unproven, so the exit "
            f"carve-out will still size a flatten against it"
            if symbols
            else ""
        )
        if last_saw_a_halt:
            raise KillSwitchUnavailableError(
                f"a halt is standing on {key} and this {reason.value} engage could not be "
                f"merged into it after {_MAX_ENGAGE_ATTEMPTS} attempts"
                + (lost or " — the reason in force is somebody else's")
                + ". The store is reachable; retry, and confirm with `scripts/halt.py status`"
            )
        raise KillSwitchUnavailableError(
            f"could not record a {reason.value} halt for {key} after "
            f"{_MAX_ENGAGE_ATTEMPTS} attempts — the key kept being cleared under us, so "
            f"NO halt may be in force" + lost + ". Retry, and confirm with "
            "`scripts/halt.py status` before assuming trading is stopped"
        )

    def _announce_engaged(self, record: HaltRecord) -> None:
        log.critical(
            "risk.killswitch.engaged",
            scope=record.scope.value,
            reason=record.reason.value,
            engaged_by=record.engaged_by,
            target=record.target,
            detail=record.detail,
            unproven=sorted(record.unproven_symbols),
        )
        # Reached only when the `SET NX` won, so a halt that was already active
        # is not a second incident on the graph. The Redis state is the
        # deduplication, exactly as it is for the notification (ADR 0012).
        metrics.halt_engaged(record.scope, record.reason)
        self._announce("engaged", record)
        self._alert_engaged(record)
        if record.impugned:
            # **A first halt can arrive already carrying evidence, and that is
            # the common case rather than the exotic one.** The scheduled
            # reconcile finds a quantity it cannot prove on a platform that was
            # trading perfectly happily a second ago; nothing was halted, so
            # `SET NX` wins and this is a *creation*, not an escalation.
            #
            # `_alert_engaged` deliberately keeps the book out of its body, so
            # on its own it tells the operator trading stopped and never which
            # symbols the platform will now refuse to close. That is precisely
            # the half `_alert_escalated` exists to deliver, and routing only
            # the escalation path through it left the ordinary path silent about
            # the thing that matters most.
            #
            # `halt_escalated` is counted here too: the counter marks "exits
            # stopped for the symbols named", which is exactly what happened,
            # and its docstring says so. `halt_engaged` above marks the separate
            # fact that trading stopped. One incident, two different facts.
            metrics.halt_escalated(record.scope, record.reason)
            self._alert_escalated(record, sorted(record.unproven_symbols))

    def _announce_escalated(self, before: HaltRecord, after: HaltRecord) -> None:
        """A standing halt that has learned something it cannot prove.

        Announced and alerted separately from the halt itself, with its own
        key, because a deduping sink would otherwise swallow it behind the
        original — and what changed is that **exits are now refused too**,
        which is the half an operator most needs to hear.

        Two shapes reach here and the message distinguishes them, because they
        are not the same news. A **rise** is a halt that proved nothing about
        the book learning that it does, and `from_reason` records where it came
        from. An **append** is a second incident naming symbols the first did
        not, on a halt whose reason has already risen as far as it goes; there
        `from_reason` is absent rather than repeated, because a log line saying
        `manual -> manual` reads as a transition that did not happen.

        `newly_unproven` is what carries in both, and it is the field this
        method exists for: an operator who already knows SPY is stuck needs to
        be told the word QQQ, not handed the whole set again.
        """
        newly = sorted(after.unproven_symbols - before.unproven_symbols)
        raised = after.reason is not before.reason
        log.critical(
            "risk.killswitch.escalated",
            scope=after.scope.value,
            from_reason=before.reason.value if raised else None,
            reason=after.reason.value,
            newly_unproven=newly,
            unproven=sorted(after.unproven_symbols),
            target=after.target,
        )
        metrics.halt_escalated(after.scope, after.reason)
        self._announce("escalated", after)
        self._alert_escalated(after, newly)

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
        # One step, so the record reported is exactly the record removed. See
        # `_GETDEL` for what a two-step read-then-delete loses now that a
        # standing halt's bytes can change under it.
        raw = _sync(self._client.eval(_GETDEL, 1, key))
        removed = raw is not None
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
        # Nothing was there. `_GETDEL` makes `removed` and `raw` one answer
        # rather than two that can disagree, so this is now simply "the key was
        # empty" — and returning a record here would credit this operator with a
        # resume that did not happen.
        return None

    def _alert_escalated(self, record: HaltRecord, newly: Sequence[str]) -> None:
        """Tell a human that the halt they already know about now refuses exits.

        Its own alert with its own `key`, because a deduping sink would
        otherwise swallow it behind the original halt's notification (ADR 0012)
        — and the thing that changed is the half an operator most needs: the
        ordinary close is now refused for these symbols, so getting flat in them
        goes through the broker's own UI. Everything else stays closeable.

        **The key is keyed on the symbols, not only the reason.** A second
        incident naming QQQ beside a standing `reconciliation_mismatch` on SPY
        produces the same scope, target and reason as the first, so a key built
        from those three would be identical — and the deduping sink this method
        exists to get past would swallow exactly the message it exists to
        deliver. The set is what changed, so the set is in the key.

        The symbols are named. That is a departure from `_alert_engaged`, which
        deliberately keeps the book out of the body — but a list of tickers the
        platform will not close is not a position size, and an operator who has
        to open the dashboard to learn *which* symbols are stuck at 3am is being
        told the wrong half of the message.
        """
        if self._alerts is None:
            return
        target = f" [{record.target}]" if record.target else ""
        every = sorted(record.unproven_symbols)
        symbols = ", ".join(every)
        added = ", ".join(newly) or symbols
        also = (
            f"In total the platform will not close: {symbols}."
            if newly and len(newly) != len(every)
            else "Every other symbol still closes normally."
        )
        self._send_alert(
            Alert(
                severity=Severity.CRITICAL,
                title=f"Halt escalated: cannot prove {added}",
                body="\n".join(
                    [
                        f"{record.scope.value}{target} was halted; the platform now also "
                        f"refuses to close: {added}.",
                        f"Reason: {record.reason.value}. {also}",
                        "To get flat in these, use the broker's own UI. Then docs/RUNBOOK.md.",
                    ]
                ),
                key=(
                    f"halt.{record.scope.value}.{record.target or 'all'}."
                    f"escalated.{record.reason.value}.{'-'.join(every)}"
                ),
                context={
                    "scope": record.scope.value,
                    "reason": record.reason.value,
                    "unproven": symbols,
                },
            )
        )

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

        Without it a halt reaches the screen only when somebody thinks to
        reload. Nothing polls (ADR 0022), so this is the whole of what puts a
        halt in front of a reader who has not asked — the client re-reads the
        book when one arrives. `atp_api.ws` fans these out to every client
        regardless of what it subscribed to, because a trading halt is not
        something to opt into, and a screen whose job is to interrupt somebody
        cannot require them to consult it first.
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
