"""The end-of-day summary, assembled from what the record actually holds.

`GET /analytics/reports/daily` and the worker's `generate_daily_report` both
promised the same five things — P&L, trades, risk rejections, halts, feed
incidents — and both were stubs. The API's stub said why, and it is still the
most useful sentence about this report: three of the five "are not gathered
anywhere one query can reach".

Two of those three have since moved. Refusals became rows when the runner
started storing refused orders, so `rejected_risk` is a query now; halts became
rows for the two operator doors, so `halt_engaged` and `halt_cleared` are
readable for anything a person did. **Feed incidents have not moved at all** —
they are log lines, no table, nothing a query reaches — and neither do the halts
the risk layer engages on its own, which is precisely the class that mattered on
day 1 of the paper week.

So this follows `paper_run.assess` rather than inventing a shape: every section
is three-valued, and a section whose store does not exist reports **absent**
rather than zero. That distinction is the whole design. "0 feed incidents" read
off a store that has never held one is worse than no report, because somebody
will believe it — and the day this report exists to summarise is exactly the day
a reader most wants to know whether the feed misbehaved.

Pure. It takes records somebody else fetched and returns an assessment; the
scheduler job and the API endpoint each do their own I/O.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from atp_core.domain import OrderStatus

if TYPE_CHECKING:
    from collections.abc import Sequence
    from datetime import date, datetime
    from decimal import Decimal

    from atp_core.audit.ports import AuditEntry
    from atp_core.domain import Order

#: What a section says when the evidence for it is not in any store this can
#: read. Named rather than spelled inline at three call sites, because it is the
#: string a reader has to recognise as "not measured" rather than "measured as
#: nothing".
NOT_RECORDED = "not recorded anywhere queryable"


#: Statuses that mean an order never left this platform: refused by the risk
#: chain or a stage before it, or not yet sent.
_NEVER_SENT = frozenset(
    {OrderStatus.REJECTED_RISK, OrderStatus.PENDING_RISK, OrderStatus.PENDING_SUBMIT}
)


@dataclass(frozen=True, slots=True)
class OrderOutcomes:
    """What became of a set of order rows, counted once for every report.

    **"Submitted" meant "has a row", and a row is not a submission.** The
    runner writes a row for an order the risk chain refused and for one the
    venue rejected, so day 4's report said `209 submitted` when 102 had reached
    the venue and been accepted, and `0 refused` over 38 exits the venue had
    rejected. Day 2's F1 found the same thing (docs/paper-week/day-4-review.md,
    F4). Both reports counted this way, so both now count through here.
    """

    #: Every row: refused, rejected, working and done.
    recorded: int
    #: Rows that reached the venue, whatever it then said.
    sent: int
    #: Sent and not rejected: the venue took the order.
    accepted: int
    #: Sent and refused by the venue (`OrderStatus.REJECTED`).
    rejected_by_venue: int
    #: Refused before submission, by a risk rule or an earlier stage.
    refused_by_risk: int
    #: Completely filled.
    filled: int
    refusals_by_rule: dict[str, int]
    #: The venue's own words, counted. Truncated, because the same rejection
    #: arrives with a different order id embedded in it.
    rejections_by_reason: dict[str, int]


def count_outcomes(orders: Sequence[Order]) -> OrderOutcomes:
    """Count what became of `orders`. Pure."""
    refused = [o for o in orders if o.status is OrderStatus.REJECTED_RISK]
    sent = [o for o in orders if o.status not in _NEVER_SENT]
    rejected = [o for o in sent if o.status is OrderStatus.REJECTED]

    by_rule: dict[str, int] = {}
    for order in refused:
        key = order.rejected_by or "unknown"
        by_rule[key] = by_rule.get(key, 0) + 1
    by_reason: dict[str, int] = {}
    for order in rejected:
        key = (order.reject_reason or "no reason recorded")[:_REASON_CHARS]
        by_reason[key] = by_reason.get(key, 0) + 1

    return OrderOutcomes(
        recorded=len(orders),
        sent=len(sent),
        accepted=len(sent) - len(rejected),
        rejected_by_venue=len(rejected),
        refused_by_risk=len(refused),
        filled=sum(1 for o in orders if o.status is OrderStatus.FILLED),
        refusals_by_rule=by_rule,
        rejections_by_reason=by_reason,
    )


#: How much of a venue's rejection text keys the count. Enough to tell "potential
#: wash trade" from "insufficient qty available"; short enough that per-order
#: detail after it does not split one cause into many rows.
_REASON_CHARS = 80


@dataclass(frozen=True, slots=True)
class Coverage:
    """Regular-hours minutes in which the runner evaluated, against the session.

    **The number the paper week exists to accumulate**, and nothing computed
    it. Day 3 evaluated for 74 of 390 minutes and day 4 for about 355; both
    had to be reconstructed from health checks (docs/paper-week/day-4-review.md,
    F11).

    `visible_from` is when the process that counted started. A worker
    restarted mid-session cannot see the minutes its predecessor evaluated, so
    those minutes are reported as not visible, not as uncovered.
    """

    evaluated_minutes: int
    session_minutes: int
    open_at: datetime
    visible_from: datetime | None = None


@dataclass(frozen=True, slots=True)
class Section:
    """One part of the day, and how much of it the record can support.

    `value` is None when the store for this section does not exist — a third
    state, and the one most likely to be misread if it were folded into zero.
    `detail` says what was counted or why nothing could be.
    """

    name: str
    value: int | None
    detail: str
    #: How to get the answer this could not, when there is a way. Empty when the
    #: section is answerable and answered.
    how_to_check: str = ""

    @property
    def is_absent(self) -> bool:
        return self.value is None


@dataclass(frozen=True, slots=True)
class DailyReport:
    """One session, summarised. Rendered by whoever asked for it."""

    day: date
    sections: list[Section]

    #: Orders that reached the venue. Not every row: see `OrderOutcomes`.
    orders_submitted: int
    orders_filled: int
    #: Refused before submission, by the risk chain or a stage before it.
    orders_refused: int
    refusals_by_rule: dict[str, int] = field(default_factory=dict)
    #: Of `orders_submitted`, the ones the venue took and the ones it refused.
    orders_accepted: int = 0
    orders_rejected_by_venue: int = 0
    rejections_by_reason: dict[str, int] = field(default_factory=dict)

    symbols: tuple[str, ...] = ()
    #: Equity at the first and last snapshot of the session. Their difference is
    #: the day's P&L, realised and unrealised together. There is deliberately no
    #: `realised_pnl`: the field this replaced summed the day's fill cash flows,
    #: which is not a P&L, and on day 4 it was a number with no meaning.
    starting_equity: Decimal | None = None
    ending_equity: Decimal | None = None

    @property
    def absent(self) -> list[Section]:
        """The sections nothing could answer. Read this first.

        A report whose absent list is non-empty is a partial report, and saying
        so at the top is the difference between a summary and a claim.
        """
        return [s for s in self.sections if s.is_absent]

    @property
    def traded(self) -> bool:
        return self.orders_filled > 0

    @property
    def pnl_change(self) -> Decimal | None:
        if self.starting_equity is None or self.ending_equity is None:
            return None
        return self.ending_equity - self.starting_equity

    def headline(self) -> str:
        """One line, for a log field or an alert body.

        Leads with what happened rather than with the day's P&L, because a
        session that submitted nothing is the outcome this platform has actually
        produced and the one an operator most needs named. Day 1 of the paper
        week ran ten hours, submitted zero orders and reported it nowhere
        (docs/paper-week/day-1-review.md).
        """
        if not self.orders_submitted and not self.orders_refused:
            return "no orders submitted"
        parts = [
            f"{self.orders_submitted} submitted",
            f"{self.orders_accepted} accepted",
            f"{self.orders_filled} filled",
            f"{self.orders_rejected_by_venue} rejected by the venue",
            f"{self.orders_refused} refused by risk",
        ]
        change = self.pnl_change
        if change is not None:
            parts.append(f"equity {change:+}")
        return ", ".join(parts)


def summarise(
    day: date,
    orders: Sequence[Order],
    *,
    audit: Sequence[AuditEntry] | None = None,
    starting_equity: Decimal | None = None,
    ending_equity: Decimal | None = None,
    coverage: Coverage | None = None,
) -> DailyReport:
    """Assemble the day from the records handed in.

    `audit` is optional and its absence is *reported*, not assumed empty: a
    caller that could not reach the audit table and one that reached it and
    found nothing are different days, and only the caller knows which happened.
    Passing `None` says "I could not look"; passing `[]` says "I looked".

    Equity is likewise optional. `PortfolioRepository` holds the snapshots, and
    a report generated for a day before the platform was storing them has no
    equity to show rather than an equity of zero. **An absent end is now a
    section**, not a silently dropped clause: day 4's only end-of-day artifact
    said nothing about money on a day that lost $251.60, and nothing in it
    said so.

    `coverage` is the regular-hours minutes in which the runner evaluated. It
    is in the worker's memory only, so the worker's own report can pass it and
    the API's cannot. Passing None reports it as absent.
    """
    outcomes = count_outcomes(orders)
    sections = [
        _trades(outcomes),
        _refusals(outcomes),
        _venue_rejections(outcomes),
        _coverage(coverage),
        _halts(audit),
        _feed_incidents(),
    ]
    if starting_equity is None or ending_equity is None:
        sections.append(_equity_absent())

    return DailyReport(
        day=day,
        sections=sections,
        orders_submitted=outcomes.sent,
        orders_filled=outcomes.filled,
        orders_refused=outcomes.refused_by_risk,
        refusals_by_rule=outcomes.refusals_by_rule,
        orders_accepted=outcomes.accepted,
        orders_rejected_by_venue=outcomes.rejected_by_venue,
        rejections_by_reason=outcomes.rejections_by_reason,
        symbols=tuple(sorted({o.symbol for o in orders})),
        starting_equity=starting_equity,
        ending_equity=ending_equity,
    )


def _trades(outcomes: OrderOutcomes) -> Section:
    if not outcomes.sent:
        return Section(
            "trades",
            0,
            "no orders reached the venue",
            how_to_check="`runner.evaluated` says whether the strategy was asked anything at all",
        )
    return Section(
        "trades",
        outcomes.filled,
        f"{outcomes.filled} filled of {outcomes.accepted} accepted by the venue "
        f"({outcomes.sent} sent)",
    )


def _refusals(outcomes: OrderOutcomes) -> Section:
    if not outcomes.refused_by_risk:
        return Section("risk rejections", 0, "nothing was refused by the risk chain")
    ranked = ", ".join(
        f"{rule} x{count}" for rule, count in sorted(outcomes.refusals_by_rule.items())
    )
    return Section("risk rejections", outcomes.refused_by_risk, ranked)


def _venue_rejections(outcomes: OrderOutcomes) -> Section:
    """Orders the risk chain approved and the venue refused.

    Its own section, because folding it into either neighbour tells the
    reader the wrong thing. Counted as "risk" it says the configuration is too
    tight when risk approved every one; left out, it says nothing was refused.
    Day 4's 38 refused exits were both at once (F3, F4).
    """
    if not outcomes.rejected_by_venue:
        return Section("venue rejections", 0, "the venue accepted everything it was sent")
    ranked = "; ".join(
        f"x{count} {reason}"
        for reason, count in sorted(outcomes.rejections_by_reason.items(), key=lambda kv: -kv[1])
    )
    return Section("venue rejections", outcomes.rejected_by_venue, ranked)


def _coverage(coverage: Coverage | None) -> Section:
    if coverage is None:
        return Section(
            "RTH coverage",
            None,
            NOT_RECORDED + " — which minutes the runner evaluated is held in the worker's memory",
            how_to_check=(
                "the worker's own daily report carries it; otherwise count "
                "runner.evaluated lines between the open and the close"
            ),
        )
    detail = (
        f"{coverage.evaluated_minutes} of {coverage.session_minutes} regular-hours minutes "
        f"had an evaluating runner"
    )
    if coverage.visible_from is not None and coverage.visible_from > coverage.open_at:
        unseen = min(
            coverage.session_minutes,
            int((coverage.visible_from - coverage.open_at).total_seconds() // 60),
        )
        detail += (
            f"; this process started at {coverage.visible_from.strftime('%H:%MZ')}, so the "
            f"first {unseen} minute(s) belong to a process it cannot see"
        )
    return Section("RTH coverage", coverage.evaluated_minutes, detail)


def _equity_absent() -> Section:
    return Section(
        "equity",
        None,
        NOT_RECORDED + " for this session — no equity snapshot at one end of it, so no P&L",
        how_to_check="GET /api/v1/dashboard/equity-curve covers the session",
    )


def _halts(audit: Sequence[AuditEntry] | None) -> Section:
    """Halts, and the half of them the record still cannot see.

    `halt_engaged` and `halt_cleared` are written by the API and by
    `scripts/halt.py`, so anything a *person* did is here. The risk layer's own
    triggers write no audit row — a feed loss, a reconciliation mismatch, a
    daily-loss breach — and on day 1 of the paper week the halt that mattered
    was exactly one of those. So a zero here is reported with what it excludes
    attached, because "no halts" and "no halts anybody typed" are different
    days.
    """
    if audit is None:
        return Section(
            "halts",
            None,
            NOT_RECORDED + " for this run — the audit table was not read",
            how_to_check="GET /api/v1/audit?action=halt_engaged",
        )
    halts = [e for e in audit if e.action in ("halt_engaged", "halt_cleared")]
    return Section(
        "halts",
        len(halts),
        f"{len(halts)} recorded — operator halts only; the risk layer's own "
        f"triggers write no audit row",
        how_to_check="grep risk.killswitch.engaged in the worker log for automated halts",
    )


def _feed_incidents() -> Section:
    """Always absent, and deliberately still a section.

    Dropping it would be the easy option and the wrong one: the report promises
    five things, and a reader who does not see feed incidents listed will assume
    there were none rather than that nothing counts them. Reconnects, gaps and
    staleness all exist only as log lines — `data.stream.reconnected`,
    `data.stream.gap_widened_from_storage`, `data.staleness.detected` — with no
    table behind any of them.
    """
    return Section(
        "feed incidents",
        None,
        NOT_RECORDED + " — reconnects, gaps and staleness are log lines only",
        how_to_check=(
            "docker compose logs worker | grep -E "
            "'data.stream.reconnected|data.staleness.detected|gap_widened'"
        ),
    )


def render(report: DailyReport) -> str:
    """The report as text, for a log field, an alert body or a terminal.

    Absent sections are listed last and marked, rather than omitted. A reader
    skimming this has to be able to see the shape of what was *not* measured
    without counting which headings are missing.
    """
    lines = [f"{report.day.isoformat()} — {report.headline()}"]
    if report.symbols:
        lines.append(f"  symbols        {', '.join(report.symbols)}")
    if report.starting_equity is not None and report.ending_equity is not None:
        lines.append(f"  equity         {report.starting_equity} → {report.ending_equity}")

    for section in report.sections:
        if section.is_absent:
            continue
        lines.append(f"  {section.name:<14} {section.value}  ({section.detail})")

    for section in report.absent:
        lines.append(f"  {section.name:<14} NOT MEASURED — {section.detail}")
        if section.how_to_check:
            lines.append(f"  {'':14} → {section.how_to_check}")
    return "\n".join(lines)
