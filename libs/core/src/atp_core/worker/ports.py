"""The two seams the worker's configuration crosses.

`WorkerConfigRepository` is where an operator's decision is *stored*: one row,
written only by `PUT /worker/config`, read by the worker at start and by the
dashboard whenever it renders. Postgres rather than Redis, deliberately — this
is the record of what somebody chose, it must survive a flushed cache, and it
carries an author and a revision that a post-mortem will want.

`WorkerStatusStore` is the opposite direction and the reason the pair exists:
what the worker *actually loaded*. A worker reads its configuration once, at
start, so the saved row and the running process can disagree for as long as
nobody restarts — and a dashboard that showed only the saved row would report a
stop multiplier that no running process is using. The worker publishes what it
booted with, the API serves it beside the saved row, and the screen states the
difference. Redis for the same reason `SnapshotStore` is: it is a
last-write-wins fact about a live process, not a record.

One writer each, and they are different processes. Nothing here is a
read-modify-write, so there is no arbitration to do.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from datetime import datetime

    from atp_core.domain import RunMode
    from atp_core.worker.config import StoredWorkerConfig, WorkerConfig


class WorkerConfigRepository(Protocol):
    """The single stored worker configuration."""

    async def load(self) -> StoredWorkerConfig | None:
        """The saved configuration, or None if nothing has ever been saved.

        None is an ordinary state — a fresh database has no row — and means the
        worker runs `DEFAULT_WORKER_CONFIG`: no watchlist, no strategy, no
        orders. It must never mean "the read failed": an unreachable database
        raises, because a worker that quietly fell back to defaults would ignore
        a watchlist and a strategy somebody had configured, and the dashboard
        would show an empty form over a row that exists.
        """
        ...

    async def save(self, config: WorkerConfig, *, actor: str, at: datetime) -> StoredWorkerConfig:
        """Replace the stored configuration, returning it with its new revision.

        The revision increments by one on every save, including a save that
        changed nothing — "somebody looked at this and pressed save" is a fact
        worth keeping, and a revision that only moved on a diff would make the
        dashboard's restart notice depend on what changed rather than on when.
        """
        ...


@dataclass(frozen=True, slots=True)
class RunningWorkerConfig:
    """What a worker booted with, as it published it.

    Not a copy of the row: it is the row *as this process read it*, plus what
    the process decided from it. `trading` and `reason` are here because the
    saved config alone cannot answer "is it trading" — the run mode, the broker
    credentials and the watchlist all get a vote (`trading.decide`), and the
    dashboard would otherwise have to re-derive a decision the worker has
    already made and logged.
    """

    config: WorkerConfig
    #: The revision this worker loaded. Zero means it started before anything
    #: had been saved and is running the defaults, which is distinguishable
    #: from revision 1 — a saved config that happens to equal them.
    revision: int
    #: When this worker started. Its age is the freshness signal, exactly as
    #: `LiveSnapshot.as_of` is for the book: the key outliving the process is
    #: what lets the screen say "the last worker to report started four days
    #: ago" rather than "no worker has ever reported".
    started_at: datetime
    #: Whether this worker places orders, and why either way — the same sentence
    #: `trading.decide` puts in the startup log.
    trading: bool
    reason: str


class WorkerStatusStore(Protocol):
    """What each run mode's worker last reported about itself."""

    async def put(self, run_mode: RunMode, running: RunningWorkerConfig) -> None:
        """Replace what this run mode's worker reports. Last write wins."""
        ...

    async def get(self, run_mode: RunMode) -> RunningWorkerConfig | None:
        """The last report, or None if no worker has published one.

        None is ordinary on a platform that has never run a worker. Unlike the
        book, an unreadable payload here does **not** raise: this decorates a
        settings screen rather than telling anybody what they hold, and a
        dashboard that refused to render the form because a status blob did not
        parse would take away the one screen that could fix it.
        """
        ...
