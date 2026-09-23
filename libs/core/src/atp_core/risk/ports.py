"""Ports the risk layer needs from the outside world.

Core is pure (CLAUDE.md §1.3), so anything a rule must remember beyond the life
of a process is reached through a `Protocol` here and implemented in
`persistence/`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from datetime import date
    from decimal import Decimal

    from atp_core.domain import RunMode


class SessionAnchorStore(Protocol):
    """Where each session's starting equity is kept, by exchange-local date.

    `DailyLossLimitRule.day_start_equity` has always said it is *"persisted
    there so a mid-session restart does not re-anchor to a drawn-down number and
    silently grant the day a second allowance"*. Nothing persisted it
    (docs/paper-week/day-5-readiness.md, §3.5). A worker restarted to clear a
    daily-loss halt would have anchored afresh on the book it had just lost
    money in, and been given another full `max_daily_loss_pct` on top.

    Keyed by run mode as well as by date, because paper and live are two
    accounts with two days, and one anchoring the other would be a wrong number
    on both.
    """

    async def get(self, run_mode: RunMode, day: date) -> Decimal | None:
        """The equity this session was anchored to, or None if it has not been.

        Raises when the store cannot answer. The caller must not treat an
        unanswerable question as "not anchored yet", because that is exactly the
        second allowance this store exists to refuse.
        """
        ...

    async def put(self, run_mode: RunMode, day: date, equity: Decimal) -> None:
        """Record this session's anchor. Last write wins."""
        ...
