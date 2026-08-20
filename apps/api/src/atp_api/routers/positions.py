"""Position endpoints.

The read is built; the writes are not. `GET` serves a book the runner already
computed and stored. Closing a position and moving a stop both *place* orders,
and there is exactly one path from an intent to a venue — `RiskEngine.validate`
then `OrderRouter.submit` (rule §1.5, ADR 0005). That path carries the audit
writes ADR 0010 is waiting on, and it is not made smaller by being started
halfway.

**Why this exists when the dashboard already shows positions.** The dashboard
reads the book the worker *published to Redis*, which is the right source for a
live screen and is gone the moment the worker stops: with nothing publishing,
`/dashboard/live` correctly reports no book at all. The same book is also
*written to Postgres* at every evaluation, and that copy survives. So this
endpoint answers the question the live screen cannot when it matters most —
"what am I holding right now?", asked because the worker just died.

That is not the recomputation ADR 0007 refuses. It is the worker's own
computation, read back from the table the worker wrote it to; nothing here adds
up a position from orders and quotes. What it *is* is possibly old, which is why
the age is part of the response rather than something a client works out.
"""

from __future__ import annotations

#: Imported at runtime, not behind `if TYPE_CHECKING`: FastAPI resolves a
#: handler's annotations when it wires the graph, and a name that exists only for
#: the type checker raises `NameError` on the first request.
from datetime import datetime
from decimal import Decimal
from typing import Annotated

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from atp_api.deps import CurrentUser, get_clock, get_portfolio_repository
from atp_api.routers.dashboard import AccountView, PositionView, account_view
from atp_core.clock import Clock
from atp_core.config import Settings, get_settings
from atp_core.dashboard import account_summary, position_summary
from atp_core.execution.ports import PortfolioRepository

router = APIRouter(prefix="/positions", tags=["positions"])


class StoredBookView(BaseModel):
    """The book as the worker last wrote it, and how long ago that was.

    `as_of` and `age_seconds` are not decoration. Every other field here
    describes a moment that has already passed, and how far past it is decides
    whether any of them can be acted on. A stored book rendered without its age
    is the same mistake as a price without one (docs/DASHBOARD.md), with more at
    stake: the reader is usually looking at this screen *because* something
    stopped.

    Null throughout when the worker has never written a snapshot. That is not an
    empty book — "you hold nothing" and "nobody has ever said what you hold" are
    different sentences and only one of them is safe to act on (ADR 0007).
    """

    as_of: datetime | None
    #: Clamped at zero, matching `/dashboard/live`: a worker clock a second ahead
    #: of the API's would otherwise render as "written -1s ago", which reads as a
    #: bug in the dashboard rather than the clock skew it is.
    age_seconds: int | None
    account: AccountView | None
    positions: list[PositionView] = Field(default_factory=list)
    #: Which run mode these rows belong to. Paper and live share the snapshot
    #: tables, and a screen that did not say which it was showing would be
    #: unreadable on a machine that has run both.
    run_mode: str


@router.get("", response_model=StoredBookView)
async def list_positions(
    portfolio_repo: Annotated[PortfolioRepository, Depends(get_portfolio_repository)],
    settings: Annotated[Settings, Depends(get_settings)],
    clock: Annotated[Clock, Depends(get_clock)],
) -> StoredBookView:
    """What the worker last recorded holding, with the age of that record.

    Every derived figure — market value, unrealised P&L, leverage, the
    distance-to-stop fraction — comes from `atp_core.dashboard`'s own
    `position_summary` and `account_summary`, which is what the live dashboard
    computes from. One expression per figure, two screens: a distance-to-stop
    that disagreed between them would be a bug invisible from either.

    **`open_only` is gone from this signature.** The stub took it, and it could
    never have been honoured: `PortfolioRepository.snapshot` writes only open
    positions, because a `Portfolio` keeps a zeroed `Position` after an exit and
    storing those would be rows holding nothing. A closed position is a round
    trip, and round trips are `/analytics/trades`. Accepting a parameter that
    silently did nothing would be worse than not offering it.
    """
    stored = await portfolio_repo.latest_snapshot(settings.run_mode)
    if stored is None:
        # Nothing has ever been written. Reported as itself rather than as an
        # empty book.
        return StoredBookView(
            as_of=None,
            age_seconds=None,
            account=None,
            positions=[],
            run_mode=settings.run_mode.value,
        )

    portfolio = stored.portfolio
    return StoredBookView(
        as_of=stored.at,
        age_seconds=max(0, int((clock.now() - stored.at).total_seconds())),
        # Day P&L is null rather than zero. It is not a property of any single
        # book — it is this equity against the session's first recorded one —
        # and answering it here would mean a second read for a figure
        # `/dashboard/live` already computes. Zero is a value a reader acts on.
        account=account_view(account_summary(portfolio), day_pnl=None, day_pnl_pct=None),
        positions=[
            PositionView.model_validate(position_summary(p), from_attributes=True)
            for p in sorted(portfolio.open_positions, key=lambda p: p.symbol)
        ],
        run_mode=settings.run_mode.value,
    )


@router.get("/{symbol}")
async def get_position(symbol: str) -> dict[str, object]:
    """One holding.

    Still a stub, and deliberately: nothing consumes it. The screen shows the
    whole book in one read, so this would be an endpoint built, tested and
    documented with no caller — which is the shape of gap the analytics
    endpoints sat in for a phase before anything read them.
    """
    raise NotImplementedError


@router.post("/{symbol}/close")
async def close_position(symbol: str, actor: CurrentUser) -> dict[str, object]:
    """Flatten one position at market. Audit-logged."""
    raise NotImplementedError


@router.patch("/{symbol}/stop")
async def update_stop(
    symbol: str, stop_loss_price: Decimal, actor: CurrentUser
) -> dict[str, object]:
    """Adjust a protective stop.

    Widening a stop (moving it away from price) requires an explicit override
    flag and is audit-logged prominently. It is the most common way a
    disciplined system becomes an undisciplined one, usually at the exact moment
    discipline was needed.
    """
    raise NotImplementedError
