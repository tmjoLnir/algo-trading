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

from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from datetime import datetime

    from atp_core.domain import Order, Portfolio, RunMode


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
