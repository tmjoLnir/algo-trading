"""Persistence ports for the things execution owns: orders, and the book.

Separate from `data/ports.py` because the failure modes are not alike. A bar
that fails to persist can be backfilled from the vendor — the vendor is the
source of truth and it keeps its copy. An *order* that fails to persist cannot
be recovered from anywhere except the venue, and a position we forgot is a
position nobody is managing.

That asymmetry is why these exist at all. Without them the runner's book lives
only in the worker's memory, so every restart adopts the broker's book
wholesale — which makes reconciliation across a restart clean *by
construction*, and therefore worthless as evidence. `docs/FIRST_PAPER_RUN.md`
says so in the section on what a paper week cannot prove.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from datetime import datetime
    from decimal import Decimal

    from atp_core.domain import Order, OrderStatus, Portfolio, RunMode


@dataclass(frozen=True, slots=True)
class EquityPoint:
    """One row of the account's history — the equity curve, point by point.

    A named type rather than the `(ts, equity)` tuple `Portfolio.equity_curve`
    keeps in memory, because this one leaves the process: it is what the
    dashboard's headline chart is drawn from and what day P&L is measured
    against. Cash and gross exposure travel with it because they were recorded
    in the same row at the same instant, and a chart that wanted to show
    exposure alongside equity would otherwise have to ask twice and hope the
    two reads agreed.
    """

    ts: datetime
    equity: Decimal
    cash: Decimal
    gross_exposure: Decimal


@dataclass(frozen=True, slots=True)
class StoredBook:
    """A stored snapshot, and the instant the worker wrote it.

    The timestamp is the reason this type exists rather than a bare `Portfolio`.
    A book read from storage can be arbitrarily old — the worker may have
    stopped an hour ago — and a book presented without its age is the failure
    ADR 0007 exists to prevent, moved from a cache to a table. `latest` returns
    the portfolio alone because its caller is a runner adopting its own last
    state at boot, for which "when" is not a question. Any *reader* needs both.
    """

    at: datetime
    portfolio: Portfolio


class OrderRepository(Protocol):
    """Durable record of every order that reached, or tried to reach, a venue."""

    async def save(self, order: Order, *, run_mode: RunMode) -> None:
        """Write the order and any fills it has accumulated.

        Idempotent, and called repeatedly for the same order: an `Order` is
        mutable state that grows fills over its life, so this is an upsert on
        the order's identity rather than an insert. Fills are appended, keyed
        so that re-saving an order already stored cannot duplicate them —
        a double-counted fill is a double-counted position.
        """
        ...

    async def open_orders(self, run_mode: RunMode) -> list[Order]:
        """Every order not in a terminal status, oldest first.

        What a restarting runner needs in order to answer "what do we believe
        is working at the venue?" — which is the set reconciliation compares
        against, and the reason an orphan is detectable at all.

        Scoped by run mode, because paper and live orders share a table and a
        paper order counted as live would be an orphan that never resolves.
        """
        ...

    async def filled_orders(
        self, run_mode: RunMode, *, until: datetime, strategy_id: str | None = None
    ) -> list[Order]:
        """Every order with at least one fill up to `until`, oldest first.

        What `analytics.performance.PerformanceAnalyzer.build_trades` folds into
        round trips. Orders that never filled are excluded: they moved no
        quantity, so they belong to the signal record rather than to a trade.

        **There is no `start`, and that is the interesting part of this
        signature.** Round trips are matched FIFO from flat, so an exit can only
        be paired with the entry it closes if that entry is in the same list. A
        window starting last Monday would present every position opened before it
        as an exit with no entry — and the tempting reading of that is a short
        that was never opened, which inverts the sign of its P&L. So the read is
        bounded at one end only, and a caller wanting a period filters the
        *trades* that come out rather than the orders going in.

        The cost is honest and stated rather than hidden: this grows with the
        lifetime of the account. When it stops being affordable the fix is a
        stored trade table — reconstruct once, keep the round trips — not a
        truncated read, because a truncated read does not get slower, it gets
        wrong. `docs/ANALYTICS.md` records the boundary.
        """
        ...

    async def recent_orders(
        self,
        run_mode: RunMode,
        *,
        status: OrderStatus | None = None,
        symbol: str | None = None,
        strategy_id: str | None = None,
        since: datetime | None = None,
        limit: int = 100,
    ) -> list[Order]:
        """Orders for display, **newest first**, bounded by `limit`.

        Two things here contradict the method above, and both are deliberate.

        **The ordering is reversed.** `filled_orders` is oldest-first because
        the FIFO matcher pairs an exit with the entry it closes and cannot do
        that out of sequence. Nothing here is matched against anything: this is
        a list a person reads from the top, and the top is what just happened.

        **It is bounded, and that is safe here in a way it is not there.** The
        port's own warning — a truncated read does not get slower, it gets
        wrong — is about reconstruction, where a missing entry becomes an exit
        with no entry and inverts the sign of a P&L. Dropping the oldest rows
        from a *display* loses rows off the bottom of a screen and changes no
        number on it. The two reads want opposite things and get them.

        **Orders that never filled are included**, which is the point of having
        this at all. `filled_orders` excludes them because they moved no
        quantity, and the consequence is that a rejection — ours or the venue's
        — appears in no other read in the platform: not in the book, not in a
        round trip, not on the equity curve. An order refused every morning for
        a month is, from everywhere else, indistinguishable from an order never
        placed.

        Scoped by run mode for the same reason as `open_orders`: paper and live
        share a table, and a screen mixing them is worse than one showing
        neither.
        """
        ...


class PortfolioRepository(Protocol):
    """Point-in-time snapshots of the book."""

    async def snapshot(self, portfolio: Portfolio, *, at: datetime, run_mode: RunMode) -> None:
        """Record positions, cash and equity as of `at`.

        Every row of one snapshot shares its timestamp, which is what makes
        `latest` able to read a coherent book rather than a mixture of two.
        """
        ...

    async def latest(self, run_mode: RunMode) -> Portfolio | None:
        """The most recent complete snapshot, or None if none was ever taken.

        None is the honest answer for a first-ever boot and is not an error:
        the caller adopts the broker's book instead, loudly. What it must never
        mean is "the read failed" — a failure raises, because a runner that
        silently adopted after a database outage would be doing exactly the
        thing this repository exists to stop.
        """
        ...

    async def latest_snapshot(self, run_mode: RunMode) -> StoredBook | None:
        """`latest`, with the instant it was written.

        Two methods rather than one because they have different callers and only
        one of them can act on the age. The runner calls `latest` at boot to
        adopt its own last state, and how old that state is does not change what
        it does — it reconciles against the broker either way. A *screen* reading
        the same rows must say how old they are or it is presenting a possibly
        stale book as the current one.

        None means nothing was ever written, exactly as in `latest`. A failure
        still raises.
        """
        ...

    async def equity_history(
        self, run_mode: RunMode, *, start: datetime, end: datetime
    ) -> list[EquityPoint]:
        """Every recorded equity point in `[start, end]`, oldest first.

        Two readers, and naming both is the argument for it being one method.
        The dashboard's equity chart draws the series; day P&L subtracts the
        first point at or after the session open from the current book. Neither
        belongs in the trading loop — the runner writes this history and does
        not read it back, so a display query can never slow down or fail an
        evaluation.

        Inclusive of both ends, because the arguments are instants a caller
        computed from a session's bounds and a range that quietly dropped its
        last point would put the wrong number under "day P&L".
        """
        ...
