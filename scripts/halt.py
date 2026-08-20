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

**Clearing is still only here.** `POST /api/v1/risk/resume` remains a stub and
demands a step-up password no screen asks for yet, so `clear --by <name>` is the
sole path back to trading. That asymmetry is the intended one (docs/RISK.md) —
it is just enforced by what exists rather than by design, for now.

Nothing this script does reaches the audit log. It has no session to attribute a
row to, and inventing one would put a name in an append-only record that nothing
authenticated. The halt is recorded by `risk.killswitch.engaged` in the logs and
by the alert the switch sends; only halts engaged through the API appear on the
audit tab.

Deliberately thin. Every decision lives in `RedisKillSwitch`: the idempotence
that keeps the original halt record, the refusal to clear without a named
human, and the fail-closed read. This file parses arguments and prints.

**Halting is not flattening.** Halting stops new orders across every process
and leaves positions and their broker-side stops exactly where they are.
Flattening realises P&L and is a separate decision with no operator path yet —
use the broker's own UI (docs/RUNBOOK.md 'Emergency flatten').
"""

from __future__ import annotations

import argparse
import sys
from typing import TYPE_CHECKING

from atp_core.alerts import build_alert_sink
from atp_core.config import get_settings
from atp_core.errors import ATPError
from atp_core.persistence.redis_client import create_sync_redis
from atp_core.risk.killswitch import HaltReason, HaltScope, RedisKillSwitch

if TYPE_CHECKING:
    from atp_core.risk.killswitch import HaltRecord

#: `status` exits non-zero when trading is halted, so it composes with a shell
#: `if` and with a health check. Documented rather than clever: a reader who
#: assumes 0-means-success would otherwise read a halt as a failure to check.
EXIT_HALTED = 2


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

    if args.command == "engage":
        record = kill_switch.engage(
            scope,
            HaltReason(args.reason),
            engaged_by=args.by,
            detail=args.detail,
            target=args.target,
        )
        if record.engaged_by != args.by or record.detail != args.detail:
            # Idempotent: an active halt is returned unchanged rather than
            # overwritten, so the original record survives. Say so, or the
            # operator reads their own name back and believes they stopped it.
            print("Already halted — the existing record stands:")
        print(_render(record))
        print("\nPositions are untouched. Halting is not flattening.")
        return 0

    if args.command == "clear":
        kill_switch.clear(scope, cleared_by=args.by, target=args.target)
        print(f"Cleared {_describe(scope, args.target)}. Trading may resume.")
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
