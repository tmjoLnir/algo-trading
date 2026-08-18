"""The seam between the process that knows the book and the process that shows it.

One writer — the worker's strategy runner, at the end of each evaluation — and
as many readers as there are API replicas. A port rather than a Redis client so
that the thing deciding *what* to publish holds no socket (CLAUDE.md §1.3), and
so a test can assert on what was published without a server to publish to.

Deliberately a key-value store rather than the pub/sub in `data/ports.py`.
Publishing is fire-and-forget: a subscriber that was down when a message went
out never learns it existed. The dashboard's whole premise is that a browser
opened at any moment gets the current picture, which is a *read* of the latest
value, not a replay of the last message. The two paths coexist and each is
right for its job — `atp_core.channels` carries the announcements, this carries
the state (docs/DASHBOARD.md, "The refresh model").
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from atp_core.dashboard.snapshot import LiveSnapshot
    from atp_core.domain import RunMode


class SnapshotStore(Protocol):
    """The latest published picture of the book, per run mode."""

    async def put(self, snapshot: LiveSnapshot) -> None:
        """Replace the stored snapshot for the snapshot's own run mode.

        Last write wins with no compare-and-set. One process owns the live book
        (`StrategyRunner`), so a second writer would be a bug to fix at its
        source rather than a race to arbitrate here — and a read-modify-write
        would cost a round trip inside the trading loop for ordering nothing
        needs.
        """
        ...

    async def get(self, run_mode: RunMode) -> LiveSnapshot | None:
        """The latest snapshot, or None if nothing has ever been published.

        Scoped by run mode for the same reason orders are: paper and live share
        a datastore, and a paper book served to a live dashboard would be a
        screen showing positions that are not the ones at risk.

        None means "nothing published" and is an ordinary state — a worker that
        is up but not trading publishes nothing, and so does one that has only
        just started. It must never mean "the read failed": an unreachable
        store raises, because a dashboard that renders an empty book on a
        connection error tells its reader they hold nothing.
        """
        ...
