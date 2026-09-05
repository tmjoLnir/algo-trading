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
from decimal import Decimal
from typing import TYPE_CHECKING

from atp_core.domain import OrderStatus

if TYPE_CHECKING:
    from collections.abc import Sequence
    from datetime import date

    from atp_core.audit.ports import AuditEntry
    from atp_core.domain import Order

#: What a section says when the evidence for it is not in any store this can
#: read. Named rather than spelled inline at three call sites, because it is the
#: string a reader has to recognise as "not measured" rather than "measured as
#: nothing".
NOT_RECORDED = "not recorded anywhere queryable"


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

    orders_submitted: int
    orders_filled: int
    orders_refused: int
    refusals_by_rule: dict[str, int] = field(default_factory=dict)

    symbols: tuple[str, ...] = ()
    realised_pnl: Decimal | None = None
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
        if not self.orders_submitted:
            return "no orders submitted"
        parts = [
            f"{self.orders_submitted} submitted",
            f"{self.orders_filled} filled",
            f"{self.orders_refused} refused",
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
) -> DailyReport:
    """Assemble the day from the records handed in.

    `audit` is optional and its absence is *reported*, not assumed empty: a
    caller that could not reach the audit table and one that reached it and
    found nothing are different days, and only the caller knows which happened.
    Passing `None` says "I could not look"; passing `[]` says "I looked".

    Equity is likewise optional. `PortfolioRepository` holds the snapshots, and
    a report generated for a day before the platform was storing them has no
    equity to show rather than an equity of zero.
    """
    filled = [o for o in orders if o.status is OrderStatus.FILLED]
    refused = [o for o in orders if o.status is OrderStatus.REJECTED_RISK]

    by_rule: dict[str, int] = {}
    for order in refused:
        by_rule[order.rejected_by or "unknown"] = by_rule.get(order.rejected_by or "unknown", 0) + 1

    realised = sum(
        (o.avg_fill_price * o.filled_qty * o.side.sign for o in filled if o.avg_fill_price),
        Decimal(0),
    )

    return DailyReport(
        day=day,
        sections=[
            _trades(orders, filled),
            _refusals(refused, by_rule),
            _halts(audit),
            _feed_incidents(),
        ],
        orders_submitted=len(orders),
        orders_filled=len(filled),
        orders_refused=len(refused),
        refusals_by_rule=by_rule,
        symbols=tuple(sorted({o.symbol for o in orders})),
        realised_pnl=realised if filled else None,
        starting_equity=starting_equity,
        ending_equity=ending_equity,
    )


def _trades(orders: Sequence[Order], filled: Sequence[Order]) -> Section:
    if not orders:
        return Section(
            "trades",
            0,
            "no orders reached the venue",
            how_to_check="`runner.evaluated` says whether the strategy was asked anything at all",
        )
    return Section("trades", len(filled), f"{len(filled)} filled of {len(orders)} submitted")


def _refusals(refused: Sequence[Order], by_rule: dict[str, int]) -> Section:
    if not refused:
        return Section("risk rejections", 0, "nothing was refused by the risk chain")
    ranked = ", ".join(f"{rule} x{count}" for rule, count in sorted(by_rule.items()))
    return Section("risk rejections", len(refused), ranked)


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
