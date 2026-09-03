#!/usr/bin/env python
"""What the paper run demonstrated — the four clauses, with the numbers.

    uv run python scripts/paper_report.py
    uv run python scripts/paper_report.py --since 2026-08-14
    uv run python scripts/paper_report.py --logs worker.log      # answers all four
    uv run python scripts/paper_report.py --markdown             # paste into ROADMAP.md

Read-only. It reads the orders, the signals and the equity history the worker
wrote, and reports each clause of Phase 4's *Verifiable:* line against them:

> a strategy trades the paper account for a week and reconciles clean

**Two of the four clauses have no store behind them.** Reconciliation reports
`execution.reconcile.clean` and an unprotected position reports
`runner.position_unprotected`; both are log lines and neither is written
anywhere a query can reach. Without `--logs` this says so and prints the greps,
rather than rendering an absence as a pass. With `--logs FILE` it counts them
and answers all four.

`docs/FIRST_PAPER_RUN.md` asks for numbers rather than a conclusion, because
`ROADMAP.md`'s existing entries quote real output so a later reader can disagree
with the interpretation without re-running anything. `--markdown` emits the
block to paste there.

Exits 0 only when every clause is affirmatively shown — an unanswered clause
exits non-zero for the reason `analytics.paper_run.PaperRunReport.exit_code`
gives.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

from atp_core.analytics.paper_run import (
    RECONCILE_MARKERS,
    UNPROTECTED_MARKER,
    assess,
)
from atp_core.config import get_settings
from atp_core.persistence.db import create_engine, create_session_factory
from atp_core.persistence.orders import PostgresOrderRepository
from atp_core.persistence.positions import PostgresPortfolioRepository
from atp_core.persistence.worker_config import PostgresWorkerConfigRepository

if TYPE_CHECKING:
    from atp_core.analytics.paper_run import PaperRunReport

#: A first paper run is a week; two weeks is the window an operator asking
#: "what did it do" usually means, and over-reading costs nothing here.
DEFAULT_DAYS = 14

MARK = {True: "[x]", False: "[ ]", None: "[?]"}


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument(
        "--since",
        default=None,
        help=f"YYYY-MM-DD (default: {DEFAULT_DAYS} days ago)",
    )
    p.add_argument(
        "--strategy",
        default=None,
        help="which strategy's record to read (default: the one on the Config tab)",
    )
    p.add_argument(
        "--logs",
        default=None,
        help="a worker log file to count reconciliation and unprotected-position lines from",
    )
    p.add_argument("--limit", type=int, default=1000, help="how many orders to read back")
    p.add_argument("--markdown", action="store_true", help="emit a block for docs/ROADMAP.md")
    return p.parse_args(argv)


async def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    settings = get_settings()
    since = _since(args.since)
    engine = create_engine(settings.database_url)
    try:
        sessions = create_session_factory(engine)
        if args.strategy is not None:
            strategy_id = args.strategy
        else:
            # The strategy the worker is configured to trade, from the row it
            # reads. Empty when nothing is saved, which the report handles as
            # "every strategy" exactly as an empty `--strategy` always did.
            stored = await PostgresWorkerConfigRepository(sessions).load()
            strategy_id = "" if stored is None else stored.config.strategy
        orders = await PostgresOrderRepository(sessions).recent_orders(
            settings.run_mode, since=since, limit=args.limit
        )
        equity = await PostgresPortfolioRepository(sessions).equity_history(
            settings.run_mode, start=since, end=datetime.now(UTC)
        )
    finally:
        await engine.dispose()

    counts = _log_counts(args.logs)
    report = assess(orders, equity, strategy_id=strategy_id, **counts)

    if args.markdown:
        print(_markdown(report, strategy_id, settings.run_mode.value))
    else:
        _render(report, strategy_id)
    return report.exit_code()


def _since(raw: str | None) -> datetime:
    if raw is None:
        return datetime.now(UTC) - timedelta(days=DEFAULT_DAYS)
    try:
        return datetime.strptime(raw, "%Y-%m-%d").replace(tzinfo=UTC)
    except ValueError:
        raise SystemExit(f"--since must be YYYY-MM-DD, got {raw!r}") from None


def _log_counts(path: str | None) -> dict[str, int | None]:
    """Count the two markers no store holds.

    Substring counting over a log file, which is exactly as crude as it sounds
    and is stated rather than dressed up: it is the same thing the operator
    would do by hand, done once and counted correctly. Its one real virtue over
    the grep in the docs is that a miscount here cannot silently become a tick —
    a clause answered from a log file says so in the report.
    """
    if path is None:
        return {"reconcile_lines": None, "mismatch_lines": None, "unprotected_lines": None}
    log = Path(path)
    if not log.is_file():
        raise SystemExit(f"--logs: no such file {path}")
    text = log.read_text(errors="replace")
    clean_marker, mismatch_marker = RECONCILE_MARKERS
    return {
        "reconcile_lines": text.count(clean_marker),
        "mismatch_lines": text.count(mismatch_marker),
        "unprotected_lines": text.count(UNPROTECTED_MARKER),
    }


# ── rendering ───────────────────────────────────────────────────────────────


def _render(report: PaperRunReport, strategy_id: str) -> None:
    print(f"\nPaper run — {strategy_id or 'every strategy'}\n")
    print("Phase 4: 'a strategy trades the paper account for a week and reconciles clean'\n")
    for clause in report.clauses:
        print(f"  {MARK[clause.held]} {clause.text}")
        print(f"      {clause.evidence}")
        if clause.how_to_check:
            print(f"      → {clause.how_to_check}")
    print()
    print(_counts_block(report))

    if report.unanswerable:
        print(
            "\nRe-run with --logs <file> to answer the bracketed clauses. Until then they\n"
            "are unshown, not shown-false — do not tick them."
        )
    print()


def _counts_block(report: PaperRunReport) -> str:
    lines = [
        f"  orders submitted   {report.orders_submitted}",
        f"  orders filled      {report.orders_filled} ({report.fills} fills)",
        f"  orders refused     {report.orders_refused}",
    ]
    for rule, count in report.refusals_by_rule.items():
        lines.append(f"    by {rule:<18} {count}")
    lines.append(f"  symbols            {', '.join(report.symbols) or 'none'}")
    lines.append(f"  sessions           {report.sessions}")
    if report.first_at and report.last_at:
        lines.append(
            f"  window             {report.first_at.date().isoformat()}"
            f" .. {report.last_at.date().isoformat()}"
        )
    if report.starting_equity is not None and report.ending_equity is not None:
        change = report.ending_equity - report.starting_equity
        # Strings from the `Decimal`, never a float: this number is the one a
        # roadmap entry quotes, and it is money (CLAUDE.md §1.1).
        lines.append(
            f"  equity             {report.starting_equity} → {report.ending_equity} ({change:+})"
        )
    return "\n".join(lines)


def _markdown(report: PaperRunReport, strategy_id: str, run_mode: str) -> str:
    """The block to paste into docs/ROADMAP.md.

    Deliberately reproduces the unanswered clauses as `[?]` rather than
    dropping them. A roadmap entry that listed three clauses would read as a
    line with three clauses, and the fourth would stop existing.
    """
    out = [
        f"  *Paper run — {strategy_id or 'all strategies'}, `{run_mode}`.*",
        "",
    ]
    for clause in report.clauses:
        out.append(f"  - {MARK[clause.held]} {clause.text} — {clause.evidence}")
    out.append("")
    out.append("  ```")
    out.append(_counts_block(report))
    out.append("  ```")
    if report.unanswerable:
        out.append("")
        out.append(
            "  Clauses marked `[?]` have no durable record and were not shown: "
            "reconciliation and unprotected positions report to the log only."
        )
    return "\n".join(out)


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
