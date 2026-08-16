"""Market-data endpoints — charts and symbol search."""

from __future__ import annotations

import asyncio
from datetime import date, datetime, timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field

from atp_api.deps import get_calendar, get_clock
from atp_core.clock import Clock, TradingCalendar

router = APIRouter(prefix="/market-data", tags=["market-data"])

#: How much calendar one request may ask for. A session list is small, but the
#: sessions behind it are built a year at a time, and an unbounded range lets a
#: single request spend minutes building three centuries of them. Five years
#: covers the longest backtest this platform is built for.
MAX_CALENDAR_DAYS = 5 * 366

#: What `GET /calendar` answers when asked for no particular range: the month
#: either side of today, which is what a calendar widget opens on.
DEFAULT_CALENDAR_DAYS = 30


@router.get("/bars/{symbol}")
async def get_bars(
    symbol: str, timeframe: str = "1d", start: datetime | None = None, end: datetime | None = None
) -> dict[str, object]:
    """Historical bars for charting. Served from our own store, not the vendor —
    charts must not consume the broker's rate limit."""
    raise NotImplementedError


@router.get("/quote/{symbol}")
async def get_quote(symbol: str) -> dict[str, object]:
    """Latest cached quote. Include `age_seconds` in the response so the client
    can grey out a stale price rather than displaying it as current."""
    raise NotImplementedError


@router.get("/search")
async def search_symbols(q: str, limit: int = 20) -> list[dict[str, str]]:
    raise NotImplementedError


class SessionView(BaseModel):
    """One trading session.

    `day` is the exchange-local date — the only stable name for a session, since
    the same one opens at 14:30Z in winter and 13:30Z in summer. The bounds are
    UTC (rule §1.2); a client that wants New York wall-clock converts using
    `MarketCalendarView.timezone`.
    """

    day: date
    open_at: datetime
    close_at: datetime
    #: A half-day. Worth its own flag rather than leaving the client to compare
    #: against 16:00 local: a UI that shows every early close as a normal
    #: session teaches its reader that the market always shuts at four.
    is_early_close: bool


class MarketCalendarView(BaseModel):
    """Sessions, holidays and early closes over a range."""

    exchange: str
    timezone: str
    start: date
    end: date
    sessions: list[SessionView]
    #: Weekdays in the range the exchange was shut. Weekends are excluded on
    #: purpose — every client knows about Saturdays, and burying ~10 real
    #: closures a year in 104 ordinary ones makes the list useless.
    holidays: list[date] = Field(default_factory=list)


def _resolve_range(start: date | None, end: date | None, today: date) -> tuple[date, date]:
    """Fill in whichever bound was left out.

    One bound given anchors the range; neither centres it on today. A default
    of "everything" would be the wrong kindness — the exchange rules span three
    centuries and the client asking is drawing one month of calendar.
    """
    span = timedelta(days=DEFAULT_CALENDAR_DAYS)
    if start is not None and end is not None:
        return start, end
    if start is not None:
        return start, start + span
    if end is not None:
        return end - span, end
    return today - span, today + span


@router.get("/calendar", response_model=MarketCalendarView)
async def get_market_calendar(
    calendar: Annotated[TradingCalendar, Depends(get_calendar)],
    clock: Annotated[Clock, Depends(get_clock)],
    start: Annotated[date | None, Query(description="first day, inclusive")] = None,
    end: Annotated[date | None, Query(description="last day, inclusive")] = None,
) -> MarketCalendarView:
    """Sessions, holidays and early closes.

    Inclusive of both ends: the arguments are calendar days rather than
    instants, and a range that quietly dropped its last day would be a trap.

    Read straight from the exchange rules — no database, no vendor call — so
    this is safe to hit from a chart that needs to grey out non-trading days.
    """
    # The exchange's today, not UTC's: at 01:00Z on a Tuesday New York is still
    # on Monday, and a default range that disagrees with the sessions it
    # contains is confusing in exactly the hours an operator is up late.
    today = clock.now().astimezone(calendar.tz).date()
    start, end = _resolve_range(start, end, today)

    if end < start:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"start must be on or before end, got {start} > {end}",
        )
    if (end - start).days + 1 > MAX_CALENDAR_DAYS:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=(
                f"range is {(end - start).days + 1} days; at most {MAX_CALENDAR_DAYS} "
                "may be requested at once"
            ),
        )

    # Off the event loop: the first question about a year builds it, which is
    # ~50ms of pandas. Cheap once cached, and not something to hold every other
    # request behind while it warms.
    sessions = await asyncio.to_thread(calendar.sessions, start, end)
    trading_days = {session.day for session in sessions}

    return MarketCalendarView(
        exchange=calendar.exchange,
        timezone=str(calendar.tz),
        start=start,
        end=end,
        sessions=[
            SessionView(
                day=session.day,
                open_at=session.open_at,
                close_at=session.close_at,
                is_early_close=session.is_early_close,
            )
            for session in sessions
        ],
        holidays=[
            day
            for day in (start + timedelta(days=i) for i in range((end - start).days + 1))
            if day.weekday() < 5 and day not in trading_days
        ],
    )
