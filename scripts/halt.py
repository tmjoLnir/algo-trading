#!/usr/bin/env python
"""Stop trading, resume trading, or ask whether trading is stopped.

    uv run python scripts/halt.py engage --by jo
    uv run python scripts/halt.py status
    uv run python scripts/halt.py clear --by jo

This is docs/SAFETY.md's layer 6 with a handle on it. The dashboard's
`HALT TRADING` button now engages the same switch through
`POST /api/v1/risk/halt`, so this is no longer the only way to *stop* — but it
is still the one that works when the API or the page does not, which is a state
worth being able to act from. docs/FIRST_PAPER_RUN.md says to have it ready
before placing an order rather than after.

**Clearing asks for the operator's password, exactly as the dashboard does.**
`POST /api/v1/risk/resume` is built, the halt banner carries a `Resume…` control
that posts to it, and both ends now demand the account password — so the shell
is a second door to the same room rather than an unlocked one beside it. It was
the unlocked one for a while: this script cleared a halt for anyone who could
run it, while docs/RUNBOOK.md said clearing asks for the password "wherever you
do it" (docs/paper-week/day-1-review.md, F9). The password is prompted for, never
taken as an argument — a flag would put it in shell history and in `ps`.

`engage` still asks for nothing, and that asymmetry is the intended one
(docs/RISK.md): stopping should be reflexive, restarting should not.

**Both halves now reach the audit log, best-effort.** The record used to hold
resumes done through the API and nothing else, so an incident stopped from the
shell and resumed from the shell left no trace on the audit tab at all. The
write can never block the act it describes — `AuditSink` swallows and this
script does not wait on Postgres to stop trading — so `scripts/halt.py` still
works during a database outage, which is the property docs/RUNBOOK.md relies on
when it names this the tool that is unaffected by one. What changes is that it
now *attempts* a row and logs `audit.write_failed` at CRITICAL when it cannot
write one, rather than never having tried.

`actor` on those rows is the honest one, and the two commands differ because
their proofs do. A `clear` authenticated a person, so it is attributed to the
operator account. An `engage` authenticated nobody, so it is attributed to this
script by name and the `--by` label travels in `detail` — putting an unverified
name in the `actor` column of an append-only record is the thing ADR 0008 exists
to refuse, and a row that says "scripts/halt.py" is true where one that says
"jo" would only be a claim.

Deliberately thin. Every decision that is *about the halt* lives in
`RedisKillSwitch`: the idempotence that keeps the original halt record, the
refusal to clear without a named human, and the fail-closed read. This file
parses arguments, proves a password, prints, and writes the row.

**Halting is not flattening.** Halting stops new orders across every process
and leaves positions and their broker-side stops exactly where they are.
Flattening realises P&L and is a separate decision with no operator path yet —
use the broker's own UI (docs/RUNBOOK.md 'Emergency flatten').
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import sys
from typing import TYPE_CHECKING

from atp_core.alerts import build_alert_sink
from atp_core.audit.ports import Action, AuditEntry
from atp_core.clock import SystemClock
from atp_core.config import get_settings
from atp_core.errors import ATPError
from atp_core.logging import correlation_id
from atp_core.persistence.audit import PostgresAuditLog
from atp_core.persistence.db import create_engine, create_session_factory
from atp_core.persistence.redis_client import create_sync_redis
from atp_core.risk.killswitch import HaltReason, HaltScope, RedisKillSwitch

if TYPE_CHECKING:
    from atp_core.config import Settings
    from atp_core.risk.killswitch import HaltRecord

#: `status` exits non-zero when trading is halted, so it composes with a shell
#: `if` and with a health check. Documented rather than clever: a reader who
#: assumes 0-means-success would otherwise read a halt as a failure to check.
EXIT_HALTED = 2

#: What the audit row says did it when nothing authenticated the person running
#: the command. `engage` deliberately asks for no password, so `--by` is a label
#: an operator typed rather than an identity anything checked — and ADR 0008's
#: whole point is that an actor the caller fills in is not an audit trail. The
#: tool's own name is the one attribution here that is true, so the claimed name
#: goes to `detail` where a reader can see it for what it is.
SCRIPT_ACTOR = "scripts/halt.py"


def _prove_operator(settings: Settings, audit_detail: dict[str, object]) -> str:
    """Demand the account password before a halt is cleared. Returns the actor.

    The shell half of `apps/api/stepup.require_step_up`, and deliberately the
    same check against the same hash: `authenticate` is the one place that knows
    how a password is verified, and a second implementation here would be a
    second thing to get wrong about the credential that guards restarting a
    trading platform.

    Prompted rather than accepted as an argument. `--password` would put the
    account password into shell history, into `ps` for every user on the box,
    and into the terminal scrollback an operator screenshots for a post-mortem.

    **Refuses when no hash is configured**, because `verify_password` returns
    False for an empty one. That is the same posture as the login form —
    docs/SAFETY.md's "the unconfigured state is no way in, not a free one" — and
    the message says how to fix it, since an operator meeting this mid-incident
    needs the next command rather than the principle.

    A failure is recorded before it is refused, exactly as the API's step-up
    does it: a wrong password against a resume is either a typo or somebody
    working through guesses, the two are indistinguishable at the moment of
    refusal, and without the row the second leaves no trace anywhere.
    """
    try:
        from atp_api.auth import authenticate
    except ImportError as exc:  # pragma: no cover - a sync that skipped atp-api
        raise SystemExit(
            f"cannot verify the operator password: {exc.name or 'a dependency'} is "
            "not installed. Run `make install` (uv sync --all-packages), or "
            "`uv run --package atp-api python scripts/halt.py ...`. Trading was "
            "NOT resumed."
        ) from exc

    if not settings.api_password_hash.get_secret_value():
        raise SystemExit(
            "no API_PASSWORD_HASH is configured, so no password can be accepted "
            "and this halt cannot be cleared from the shell. Set one with "
            "`uv run python scripts/hash_password.py`. Trading was NOT resumed."
        )

    password = getpass.getpass("Operator password, to resume trading: ")
    if not password:
        raise SystemExit("No password given. Trading was NOT resumed.")

    actor = authenticate(settings.api_user, password, settings)
    if actor is None:
        _record(
            settings,
            AuditEntry(
                at=SystemClock().now(),
                actor=SCRIPT_ACTOR,
                action=Action.FORBIDDEN,
                target="scripts/halt.py clear",
                # The same name the API's step-up writes, so one query over the
                # audit tab finds refused resumes from both doors.
                detail={**audit_detail, "reason": "step_up_failed"},
            ),
        )
        raise SystemExit("That password was not accepted. Trading was NOT resumed.")
    return actor


def _record(settings: Settings, entry: AuditEntry) -> None:
    """Append one audit row, and never let it stop the act it describes.

    This script's reason to exist is that it works when other things do not, and
    docs/RUNBOOK.md leans on that during a Postgres outage. So the row is
    attempted around the act rather than before it, and every failure is
    swallowed here as well as inside the sink — a halt that did not get written
    down is a gap in the record, and a halt refused because Postgres was
    unreachable is a position nobody can close (`atp_core.audit.ports`).

    A failure inside `PostgresAuditLog.record` is invisible from here by design:
    it swallows and logs `audit.write_failed` at CRITICAL. This catches the
    layer below it — a connection that cannot even be built — for the same
    reason and with the same outcome.
    """

    async def _write() -> None:
        engine = create_engine(settings.database_url)
        try:
            await PostgresAuditLog(create_session_factory(engine)).record(entry)
        finally:
            await engine.dispose()

    try:
        asyncio.run(_write())
    except Exception as exc:
        print(
            f"warning: the audit row for this action was not written ({exc}). "
            f"The action itself stands.",
            file=sys.stderr,
        )


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = p.add_subparsers(dest="command", required=True)

    engage = sub.add_parser("engage", help="STOP TRADING immediately")
    engage.add_argument("--by", required=True, help="who is stopping trading — recorded")
    engage.add_argument("--detail", default="", help="why, in words a colleague can read")
    engage.add_argument(
        "--reason",
        default=HaltReason.MANUAL.value,
        choices=[r.value for r in HaltReason],
        help="defaults to manual; the automated reasons are for the code that detects them",
    )
    _add_scope(engage)

    clear = sub.add_parser("clear", help="resume trading — deliberately not the reflex")
    clear.add_argument("--by", required=True, help="who decided it is safe to trade again")
    _add_scope(clear)

    _add_scope(sub.add_parser("status", help="what is currently halted"))

    return p.parse_args(argv)


def _add_scope(p: argparse.ArgumentParser) -> None:
    p.add_argument(
        "--scope",
        default=HaltScope.GLOBAL.value,
        choices=[s.value for s in HaltScope],
        help="global stops everything; strategy and symbol need --target",
    )
    p.add_argument("--target", default=None, help="strategy id or symbol, for a narrowed scope")


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    scope = HaltScope(args.scope)

    if scope is not HaltScope.GLOBAL and not args.target:
        raise SystemExit(f"--scope {scope.value} needs --target (a strategy id or a symbol)")
    if scope is HaltScope.GLOBAL and args.target:
        raise SystemExit("--target is meaningless with --scope global")

    settings = get_settings()
    # No `try` around this. A kill switch that cannot reach Redis must fail
    # loudly here, and `is_engaged` is already failing closed on the same
    # outage — so the platform has stopped trading either way, and what an
    # operator needs from this command is to be told, not reassured.
    # Alerts on a manual halt too. The operator running this already knows —
    # the notification is for whoever else is watching the book, and for the
    # record on their own phone of when trading stopped.
    kill_switch = RedisKillSwitch(
        create_sync_redis(settings.redis_url), alerts=build_alert_sink(settings)
    )

    # Bound for the whole command so the audit row and the log lines the switch
    # emits underneath it — `risk.killswitch.engaged`, `risk.killswitch.cleared`,
    # `audit.write_failed` — all carry one id. That is what F9 asked for and it
    # needed no new field: `atp_core.logging` has had correlation ids since ADR
    # 0013, the audit row simply never carried the one it was written under, so
    # a row on the audit tab could not be joined to the log lines around it.
    with correlation_id() as cid:
        clock = SystemClock()

        if args.command == "engage":
            record = kill_switch.engage(
                scope,
                HaltReason(args.reason),
                engaged_by=args.by,
                detail=args.detail,
                target=args.target,
            )
            already_halted = record.engaged_by != args.by or record.detail != args.detail
            if already_halted:
                # Idempotent: an active halt is returned unchanged rather than
                # overwritten, so the original record survives. Say so, or the
                # operator reads their own name back and believes they stopped it.
                print("Already halted — the existing record stands:")
            print(_render(record))
            print("\nPositions are untouched. Halting is not flattening.")
            _record(
                settings,
                AuditEntry(
                    at=clock.now(),
                    # Not `args.by`: nothing authenticated it. See SCRIPT_ACTOR.
                    actor=SCRIPT_ACTOR,
                    action=Action.HALT_ENGAGED,
                    target=args.target,
                    detail={
                        "correlation_id": cid,
                        "scope": scope.value,
                        # From the arguments rather than from the record, the
                        # same choice `POST /risk/halt` makes and for the same
                        # reason: this row is an account of what a person asked
                        # for, and an existing halt's reason is somebody else's.
                        "reason": args.reason,
                        "detail": args.detail,
                        "by": args.by,
                        "via": SCRIPT_ACTOR,
                        "already_halted_by_another": already_halted,
                    },
                ),
            )
            return 0

        if args.command == "clear":
            base_detail: dict[str, object] = {
                "correlation_id": cid,
                "scope": scope.value,
                "by": args.by,
                "via": SCRIPT_ACTOR,
            }
            # Before the clear, and it is the only thing in this file that runs
            # before the act it guards. A password checked afterwards would be a
            # password that did not stop anything.
            actor = _prove_operator(settings, base_detail)

            cleared = kill_switch.clear(scope, cleared_by=args.by, target=args.target)
            print(f"Cleared {_describe(scope, args.target)}. Trading may resume.")
            _record(
                settings,
                AuditEntry(
                    at=clock.now(),
                    # The password proved this one, so the row carries the
                    # operator account rather than the tool.
                    actor=actor,
                    action=Action.HALT_CLEARED,
                    target=args.target,
                    detail={
                        **base_detail,
                        "was_halted": cleared is not None,
                        # What was removed, which is what says *which* halt this
                        # resume ended. `engaged_at` is the half the API's row
                        # was missing, and it is the only thing that joins a
                        # resume to the engagement it answers when the two were
                        # hours and several processes apart.
                        "original_reason": cleared.reason.value if cleared is not None else None,
                        "originally_engaged_by": (
                            cleared.engaged_by if cleared is not None else None
                        ),
                        "originally_engaged_at": (
                            cleared.engaged_at.isoformat() if cleared is not None else None
                        ),
                    },
                ),
            )
            remaining = kill_switch.active_halts()
            if remaining:
                print(f"\n{len(remaining)} halt(s) still active — trading is still stopped for:")
                for record in remaining:
                    print(_render(record))
            return 0

    halts = kill_switch.active_halts()
    if not halts:
        print("Not halted. Trading is permitted.")
        return 0
    print(f"HALTED — {len(halts)} active:\n")
    for record in halts:
        print(_render(record))
    return EXIT_HALTED


def _render(record: HaltRecord) -> str:
    lines = [
        f"  scope    {_describe(record.scope, record.target)}",
        f"  reason   {record.reason.value}",
        f"  since    {record.engaged_at.isoformat()}",
        f"  by       {record.engaged_by}",
    ]
    if record.detail:
        lines.append(f"  detail   {record.detail}")
    return "\n".join(lines)


def _describe(scope: HaltScope, target: str | None) -> str:
    return scope.value if target is None else f"{scope.value}:{target}"


if __name__ == "__main__":
    try:
        sys.exit(main())
    except ATPError as exc:
        # Named separately from the traceback a bug would print: this is the
        # platform refusing, and an operator mid-incident needs the sentence
        # rather than the stack.
        print(f"error: {exc}", file=sys.stderr)
        sys.exit(1)
