#!/usr/bin/env python
"""Replace our stored book with the broker's, after a human has looked at why.

    uv run python scripts/adopt_broker_state.py --by jo
    uv run python scripts/adopt_broker_state.py --by jo --dry-run

**This is the command docs/RUNBOOK.md has always prescribed and nothing
provided.** That document's "Reconciliation mismatch" procedure ends *"once you
know why, `adopt_broker_state()` to resync"* — a method call, with exactly one
production caller, reached only when no stored book exists at all. A worker that
*has* a snapshot and disagrees with the venue could never take that branch, so
the instruction was unexecutable precisely in the situation it was written for
(docs/paper-week/day-3-review.md, B2).

Deliberately awkward, in three ways, because this overwrites the only record of
what the platform believes it owns:

1. **It refuses to run unless trading is halted.** Adopting the broker's book
   while a runner is live races that runner: it reads the book, sizes against
   it, and submits — against numbers this command is in the middle of replacing.
   The halt is the interlock, not a formality.
2. **It shows the difference and asks you to type the symbol count.** The same
   shape as `make backup-restore into=`: no default, and an answer you can only
   give by having read what is above it.
3. **It does not clear the halt.** Resuming trading keeps its own door and its
   own password (`scripts/halt.py clear`). This command makes the book true; a
   human still decides that the platform should trade on it. docs/RUNBOOK.md
   sequences the two in that order for a reason — adopting is not a diagnosis,
   and the divergence had a cause that this command does not fix.

**Protective levels do not survive.** `Reconciler.adopt_broker_state` says why:
the broker knows a position exists and does not know the stop we intended for
it, and inventing one from the venue's average entry would arm a level no
strategy chose. Every adopted position is unprotected until something re-arms
it, which is the honest state and is the reason step 3 above is not a
convenience.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from decimal import Decimal
from typing import TYPE_CHECKING

from atp_core.audit.ports import Action, AuditEntry
from atp_core.brokers.alpaca import AlpacaBroker
from atp_core.clock import SystemClock
from atp_core.config import get_settings
from atp_core.domain import Portfolio
from atp_core.execution.reconciliation import Reconciler
from atp_core.logging import correlation_id
from atp_core.persistence.audit import PostgresAuditLog
from atp_core.persistence.db import create_engine, create_session_factory
from atp_core.persistence.positions import PostgresPortfolioRepository
from atp_core.persistence.redis_client import create_sync_redis
from atp_core.risk.killswitch import RedisKillSwitch

if TYPE_CHECKING:
    from atp_core.config import Settings

#: What the audit row says did it. The script's own name, not `--by`: nothing
#: here authenticated a person, and an actor the caller filled in is not an
#: audit trail (ADR 0008). The claimed name travels in `detail["by"]`, which is
#: the same line `scripts/halt.py engage` takes.
ACTOR = "scripts/adopt_broker_state.py"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument("--by", required=True, help="who is doing this — recorded in the audit row")
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="print the difference and change nothing",
    )
    p.add_argument(
        "--force",
        action="store_true",
        help="proceed with no halt engaged. Races a live runner — see the module docstring",
    )
    return p.parse_args(argv)


def _describe(label: str, portfolio: Portfolio) -> None:
    print(f"\n{label}")
    print(f"  cash   {portfolio.cash}")
    if not portfolio.open_positions:
        print("  positions  (none)")
        return
    for position in sorted(portfolio.open_positions, key=lambda p: p.symbol):
        print(f"  {position.symbol:<8} qty {position.qty}  @ {position.avg_entry_price}")


async def _run(args: argparse.Namespace, settings: Settings) -> int:
    kill_switch = RedisKillSwitch(create_sync_redis(settings.redis_url))
    if not kill_switch.halt_state().engaged and not args.force:
        print(
            "REFUSING: trading is not halted.\n"
            "  Adopting the broker's book while a runner is live races it — it sizes the\n"
            "  next order against numbers this command is replacing.\n"
            "    uv run python scripts/halt.py engage --by "
            f"{args.by} --detail 'adopting broker state'\n"
            "  Then run this again. --force skips the check if you know the worker is down.",
            file=sys.stderr,
        )
        return 2

    broker = AlpacaBroker(settings)
    engine = create_engine(settings.database_url)
    try:
        repo = PostgresPortfolioRepository(create_session_factory(engine))
        stored = await repo.latest(settings.run_mode)
        if stored is None:
            print(
                "Nothing stored for this run mode — the worker adopts the broker's book on its\n"
                "own at the next boot, which is the path this command exists to work around.\n"
                "There is nothing here to resync.",
                file=sys.stderr,
            )
            return 2

        _describe("stored book (ours)", stored)

        adopted = Portfolio(cash=Decimal(0), starting_equity=Decimal(0))
        reconciler = Reconciler(broker, kill_switch, SystemClock(), run_mode=settings.run_mode)
        await reconciler.adopt_broker_state(adopted)
        adopted.starting_equity = adopted.equity
        _describe("broker's book (what this would store)", adopted)

        symbols = sorted(p.symbol for p in adopted.open_positions)
        print(
            "\nEvery adopted position is UNPROTECTED — no stop is carried over.\n"
            "After this: reconcile, re-arm protection, then "
            "`scripts/halt.py clear` when you have decided to trade again."
        )

        if args.dry_run:
            print("\n--dry-run: nothing was written.")
            return 0

        answer = input(f"\nType the number of positions to adopt ({len(symbols)}) to confirm: ")
        if answer.strip() != str(len(symbols)):
            print("Not confirmed. Nothing was written.", file=sys.stderr)
            return 1

        clock = SystemClock()
        await repo.snapshot(adopted, at=clock.now(), run_mode=settings.run_mode)
        print(f"\nStored. {len(symbols)} position(s): {', '.join(symbols) or '(none)'}")
        _record(
            settings,
            AuditEntry(
                at=clock.now(),
                actor=ACTOR,
                action=Action.BOOK_ADOPTED,
                detail={
                    "by": args.by,
                    "run_mode": str(settings.run_mode),
                    "positions": len(symbols),
                    "symbols": symbols,
                    "forced": args.force,
                },
            ),
        )
        return 0
    finally:
        await engine.dispose()
        await broker.aclose()


def _record(settings: Settings, entry: AuditEntry) -> None:
    """Append one audit row, and never let it stop the act it describes.

    The same contract `scripts/halt.py` states: the row is attempted around the
    act rather than before it, and a failure to write one is printed rather than
    raised. The book has already been replaced by the time this runs, and an
    exception here would report a failure for something that succeeded.
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
            f"The adoption itself stands.",
            file=sys.stderr,
        )


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    settings = get_settings()
    with correlation_id():
        return asyncio.run(_run(args, settings))


if __name__ == "__main__":
    sys.exit(main())
