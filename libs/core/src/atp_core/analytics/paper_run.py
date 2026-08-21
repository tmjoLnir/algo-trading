"""What a paper run actually demonstrated, clause by clause.

Phase 4's *Verifiable:* line is four clauses in one sentence:

> a strategy trades the paper account for a week and reconciles clean

and `docs/FIRST_PAPER_RUN.md` ends by asking whoever ran it to say which held
and how they know — "paste the numbers rather than the conclusion", because
`ROADMAP.md`'s Phase 1 and Phase 2 entries quote real output so a later reader
can disagree with the interpretation without re-running anything.

Nothing produced those numbers. At the end of a week the numbers exist, spread
across four stores and a log file, and the tick that follows is somebody's
recollection of a `docker compose logs` tail from Tuesday. This module turns the
record into the four answers, and — the part that matters more — **names the two
clauses the record cannot answer at all** rather than letting silence read as a
pass.

**A clause is `held=None` when the evidence does not exist**, which is a third
state and not a polite failure. `reconciles clean` and `stops on every position`
are reported by `execution.reconcile.clean` and `runner.position_unprotected`,
and both are *log lines*: no table, no audit row, nothing a query can reach. So
this reports what it can see, says plainly what it cannot, and gives the exact
strings to grep for. A report that quietly rendered "no unprotected positions
found" from a store that never held them would be worse than no report, because
it would be believed.

Pure, like everything else in `analytics/`: it takes records somebody fetched
and returns an assessment. `scripts/paper_report.py` does the I/O.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from atp_core.domain import OrderStatus

if TYPE_CHECKING:
    from collections.abc import Sequence
    from datetime import date, datetime
    from decimal import Decimal

    from atp_core.domain import Order
    from atp_core.execution.ports import EquityPoint

#: The log lines that carry the two clauses no store holds. Named here rather
#: than in the script so the report and the docs quote one source.
RECONCILE_MARKERS = ("execution.reconcile.clean", "execution.reconcile.mismatch")
UNPROTECTED_MARKER = "runner.position_unprotected"

#: What "for a week" means when counting. Five sessions, not seven days: a run
#: started on a Thursday and stopped the following Wednesday spans seven
#: calendar days and four sessions, and it is the sessions that were traded.
SESSIONS_IN_A_WEEK = 5


@dataclass(frozen=True, slots=True)
class Clause:
    """One clause of the *Verifiable:* line, and what the record says about it.

    `held` is deliberately three-valued. True and False are judgements from
    evidence; None means the evidence for this clause is not in any store this
    can read, which is a different statement and the one most likely to be
    misread if it were folded into False.
    """

    text: str
    held: bool | None
    evidence: str
    #: How to get the answer this could not, when there is a way.
    how_to_check: str = ""


@dataclass(frozen=True, slots=True)
class PaperRunReport:
    clauses: list[Clause]
    orders_submitted: int
    orders_filled: int
    orders_refused: int
    refusals_by_rule: dict[str, int]
    fills: int
    symbols: tuple[str, ...]
    sessions: int
    first_at: datetime | None
    last_at: datetime | None
    starting_equity: Decimal | None
    ending_equity: Decimal | None

    @property
    def unanswerable(self) -> list[Clause]:
        return [c for c in self.clauses if c.held is None]

    @property
    def failed(self) -> list[Clause]:
        return [c for c in self.clauses if c.held is False]

    def exit_code(self) -> int:
        """0 only when every clause is affirmatively shown.

        An unanswerable clause exits non-zero along with a failed one, and that
        is the whole posture of this module: "we could not tell" must not be
        the same shell exit as "it held", or a CI job wired to this would go
        green on a week that demonstrated half of what it claims.
        """
        return 0 if not self.failed and not self.unanswerable else 1


def assess(
    orders: Sequence[Order],
    equity: Sequence[EquityPoint],
    *,
    strategy_id: str,
    reconcile_lines: int | None = None,
    mismatch_lines: int | None = None,
    unprotected_lines: int | None = None,
) -> PaperRunReport:
    """Read the record and answer what it can.

    `orders` is the *display* read — `recent_orders`, which includes the ones
    that never filled. That inclusion is the point: a refusal appears in no
    other read in this platform, so a run whose every entry was refused is,
    from the book and the equity curve alike, indistinguishable from a run that
    never signalled. Counting refusals by rule is how a silent week gets
    attributed, and attributing it is the entire reason to run one.

    The three `*_lines` counts are optional and come from whoever grepped the
    logs. Passing None leaves the clauses that depend on them unanswered, which
    is the honest default: a caller with no log access has not shown those
    clauses and this must not pretend otherwise.
    """
    mine = [o for o in orders if not strategy_id or o.strategy_id == strategy_id]
    filled = [o for o in mine if o.status is OrderStatus.FILLED]
    refused = [o for o in mine if o.status is OrderStatus.REJECTED_RISK]
    by_rule: dict[str, int] = {}
    for order in refused:
        key = order.rejected_by or "unattributed"
        by_rule[key] = by_rule.get(key, 0) + 1

    stamps = [o.submitted_at or o.created_at for o in mine]
    known = sorted(s for s in stamps if s is not None)
    sessions = _sessions(equity, known)

    return PaperRunReport(
        clauses=[
            _traded(filled, refused, by_rule),
            _for_a_week(sessions, known),
            _reconciles_clean(reconcile_lines, mismatch_lines),
            _stops_on_every_position(unprotected_lines, filled),
        ],
        orders_submitted=len(mine),
        orders_filled=len(filled),
        orders_refused=len(refused),
        refusals_by_rule=dict(sorted(by_rule.items())),
        fills=sum(len(o.fills) for o in filled),
        symbols=tuple(sorted({o.symbol for o in mine})),
        sessions=sessions,
        first_at=known[0] if known else None,
        last_at=known[-1] if known else None,
        starting_equity=equity[0].equity if equity else None,
        ending_equity=equity[-1].equity if equity else None,
    )


def _sessions(equity: Sequence[EquityPoint], order_stamps: Sequence[datetime]) -> int:
    """How many distinct days the worker was actually up.

    Counted from the equity history rather than from orders, and the difference
    is the point: the runner writes an equity point every evaluation pass, so
    this counts *sessions it ran*, where orders would count only sessions it
    traded. A week with one order on Tuesday was still five sessions of
    ingestion, warmup and reconciliation, and reporting it as one would
    understate exactly the part of the run that did work.

    Falls back to the order stamps when there is no equity history at all,
    which is the shape of a run against a database that lost its snapshots.
    """
    days: set[date] = {point.ts.date() for point in equity}
    if not days:
        days = {stamp.date() for stamp in order_stamps}
    return len(days)


def _traded(filled: Sequence[Order], refused: Sequence[Order], by_rule: dict[str, int]) -> Clause:
    if filled:
        fills = sum(len(o.fills) for o in filled)
        symbols = ", ".join(sorted({o.symbol for o in filled}))
        return Clause(
            "a strategy traded the paper account",
            True,
            f"{len(filled)} orders filled across {fills} fills, in {symbols}",
        )
    if refused:
        # The case docs/FIRST_PAPER_RUN.md warns about, named rather than
        # reported as an absence: this is a week that ran correctly and
        # demonstrated nothing about fills, stops or P&L.
        worst = max(by_rule, key=lambda k: by_rule[k])
        return Clause(
            "a strategy traded the paper account",
            False,
            f"nothing filled — {len(refused)} orders were refused, "
            f"mostly by {worst} ({by_rule[worst]})",
            how_to_check="the refusing rule is the finding; see docs/RISK.md for its limit",
        )
    return Clause(
        "a strategy traded the paper account",
        False,
        "no orders at all — the strategy never signalled, or never got as far as sizing",
        how_to_check="check runner.evaluations climbed, and scripts/preflight.py for why not",
    )


def _for_a_week(sessions: int, stamps: Sequence[datetime]) -> Clause:
    if not stamps and not sessions:
        return Clause("for a week", False, "no record of the worker running at all")
    span = ""
    if stamps:
        span = f", spanning {stamps[0].date().isoformat()} to {stamps[-1].date().isoformat()}"
    held = sessions >= SESSIONS_IN_A_WEEK
    return Clause(
        "for a week",
        held,
        f"{sessions} session(s) with a recorded evaluation{span}",
        how_to_check="" if held else f"a week is {SESSIONS_IN_A_WEEK} sessions, not 7 days",
    )


def _reconciles_clean(clean: int | None, mismatch: int | None) -> Clause:
    """The clause with no store behind it.

    `Reconciler` logs `execution.reconcile.clean` or `.mismatch` and writes
    neither anywhere durable. The audit log is not the place for it either —
    that record exists to attribute actions to a *person* (ADR 0008), and a
    reconciliation has no actor — so this genuinely cannot be answered from the
    database, and saying so is more useful than a number that looks like one.
    """
    if clean is None and mismatch is None:
        return Clause(
            "and reconciles clean",
            None,
            "no durable record — reconciliation reports to the log and nowhere else",
            how_to_check=f"docker compose logs worker | grep -E '{'|'.join(RECONCILE_MARKERS)}'",
        )
    clean, mismatch = clean or 0, mismatch or 0
    if mismatch:
        return Clause(
            "and reconciles clean",
            False,
            f"{mismatch} mismatch(es) against {clean} clean pass(es) — trading halted at each",
            how_to_check="docs/RUNBOOK.md, 'Reconciliation mismatch'",
        )
    if not clean:
        return Clause(
            "and reconciles clean",
            False,
            "no clean reconciliation was logged — the reconciler may never have run",
        )
    return Clause("and reconciles clean", True, f"{clean} clean pass(es), no mismatch")


def _stops_on_every_position(unprotected: int | None, filled: Sequence[Order]) -> Clause:
    """SAFETY.md layer 5, and the same missing-store problem.

    Worth its own clause even though the sentence above does not spell it out,
    because docs/FIRST_PAPER_RUN.md's own recording checklist adds it and
    SAFETY.md's go-live condition is that `runner.position_unprotected` never
    happens. A paper week is the first and only place that condition can be
    observed at all.
    """
    if unprotected is None:
        return Clause(
            "with a stop on every position",
            None,
            "no durable record — an unprotected position is a CRITICAL log line, not a row",
            how_to_check=f"docker compose logs worker | grep {UNPROTECTED_MARKER}",
        )
    if unprotected:
        return Clause(
            "with a stop on every position",
            False,
            f"{unprotected} position(s) were held with no stop — SAFETY.md layer 5 did not hold",
            how_to_check="docs/RUNBOOK.md, 'Position open with no stop'",
        )
    if not filled:
        # Vacuously true, and vacuous is not shown. Nothing was ever owned, so
        # layer 5 was never asked to hold.
        return Clause(
            "with a stop on every position",
            None,
            "nothing filled, so no position was ever held and layer 5 was never exercised",
        )
    return Clause(
        "with a stop on every position",
        True,
        f"no {UNPROTECTED_MARKER} across {len(filled)} filled order(s)",
    )
