"""Historical backfill orchestration.

Pure coordination over the two data ports: it decides *what* to ask for and in
what order, and hands the answers to storage. No I/O of its own, so it stays in
core (CLAUDE.md §1.3) and is testable without a network or a database.

The awkward parts of a real backfill, and what this does about them:

- **Memory.** Five years of minute bars is roughly 500k rows per symbol. Asking
  for the whole range in one call and holding it is not an option, so the range
  is sliced into windows and each window is written before the next is fetched.
- **Empty windows.** A multi-year range will contain stretches where a symbol
  has no data at all — before it listed, while it was halted. The provider
  raises `DataGapError` for those, correctly, but a backfill that aborts on the
  first one cannot span an IPO. They are collected and reported instead.
- **Batching.** Alpaca charges per request, not per symbol, so symbols go in
  batches. But one symbol with no data raises for the whole batch, so a failed
  batch is retried symbol-by-symbol to find out which one it was.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta
from typing import TYPE_CHECKING

from atp_core.domain import Timeframe
from atp_core.errors import DataGapError
from atp_core.logging import get_logger

if TYPE_CHECKING:
    from collections.abc import Iterator, Sequence
    from datetime import datetime

    from atp_core.data.ports import BarRepository, HistoricalDataProvider
    from atp_core.domain import Bar

log = get_logger(__name__)

#: How much history to pull per round trip, by timeframe. Chosen so one window
#: is tens of thousands of bars per symbol rather than hundreds of thousands —
#: enough to keep round trips down, small enough that the process holds a
#: bounded amount at once.
_WINDOW_DAYS: dict[Timeframe, int] = {
    Timeframe.M1: 30,
    Timeframe.M5: 120,
    Timeframe.M15: 365,
    Timeframe.M30: 365,
    Timeframe.H1: 730,
    Timeframe.H4: 1825,
    Timeframe.D1: 3650,
}

#: Symbols per request. Alpaca bills per request rather than per symbol, so
#: batching is the single biggest lever on how long a wide backfill takes.
DEFAULT_BATCH_SIZE = 20


@dataclass(frozen=True, slots=True)
class EmptyWindow:
    """A window a symbol returned nothing for.

    Not necessarily a fault: a symbol that had not listed yet, or was halted,
    legitimately has no bars. It is reported rather than raised so the operator
    can tell the difference — and until calendar-aware gap detection lands, this
    list is the closest thing to a coverage report.
    """

    symbol: str
    start: datetime
    end: datetime


@dataclass(frozen=True, slots=True)
class BackfillResult:
    symbols: tuple[str, ...]
    bars_written: int
    windows: int
    requests: int
    empty_windows: tuple[EmptyWindow, ...] = field(default=())

    @property
    def ok(self) -> bool:
        """True when every symbol returned data for every window asked for."""
        return not self.empty_windows


def window_days_for(timeframe: Timeframe) -> int:
    return _WINDOW_DAYS[timeframe]


def iter_windows(
    start: datetime, end: datetime, window_days: int
) -> Iterator[tuple[datetime, datetime]]:
    """Slice `[start, end)` into half-open windows.

    Half-open and abutting, so the union covers the range exactly once — an
    overlap here would be harmless (writes are idempotent) but would pay for the
    same bars twice against the rate limit.
    """
    if window_days < 1:
        raise ValueError(f"window_days must be at least 1, got {window_days}")
    cursor = start
    step = timedelta(days=window_days)
    while cursor < end:
        nxt = min(cursor + step, end)
        yield cursor, nxt
        # Abut, do not overlap. Anything that walks `cursor` back would both pay
        # twice for the same bars and, at window_days=1, never advance at all.
        cursor = nxt


def _batches(symbols: Sequence[str], size: int) -> Iterator[tuple[str, ...]]:
    if size < 1:
        raise ValueError(f"batch_size must be at least 1, got {size}")
    for i in range(0, len(symbols), size):
        yield tuple(symbols[i : i + size])


async def _fetch_window(
    provider: HistoricalDataProvider,
    symbols: tuple[str, ...],
    timeframe: Timeframe,
    start: datetime,
    end: datetime,
    adjusted: bool,
) -> tuple[dict[str, list[Bar]], list[str], int]:
    """Fetch one window for one batch. Returns (bars, symbols with none, requests).

    `get_bars` raises if *any* requested symbol has no data, which would throw
    away the rest of the batch along with it. So a raising batch is re-fetched
    one symbol at a time to find out which symbols were actually empty — paid
    for only when it happens, rather than by never batching at all.
    """
    try:
        return await provider.get_bars(list(symbols), timeframe, start, end, adjusted), [], 1
    except DataGapError:
        log.debug("data.backfill.isolating_batch", symbols=len(symbols))

    bars: dict[str, list[Bar]] = {}
    empty: list[str] = []
    for symbol in symbols:
        try:
            bars.update(await provider.get_bars([symbol], timeframe, start, end, adjusted))
        except DataGapError:
            empty.append(symbol)
    return bars, empty, 1 + len(symbols)


async def backfill_bars(
    provider: HistoricalDataProvider,
    repository: BarRepository,
    *,
    symbols: Sequence[str],
    timeframe: Timeframe,
    start: datetime,
    end: datetime,
    adjusted: bool = True,
    window_days: int | None = None,
    batch_size: int = DEFAULT_BATCH_SIZE,
) -> BackfillResult:
    """Fetch `[start, end)` for every symbol and write it, window by window.

    Idempotent: the repository upserts on the natural key, so re-running a range
    already stored costs requests but changes nothing. That is the intended way
    to fill a gap — re-run the range around it rather than compute a delta.

    Each window is written before the next is fetched. That bounds memory, and
    it means an interrupted run leaves everything up to the last completed
    window durably stored rather than losing the lot.
    """
    if not symbols:
        raise ValueError("no symbols to backfill")
    if start >= end:
        raise ValueError(f"start must be before end, got start={start} end={end}")
    for symbol in symbols:
        if symbol != symbol.upper():
            raise ValueError(f"symbol must be uppercase, got {symbol!r}")

    span = window_days if window_days is not None else window_days_for(timeframe)
    ordered = tuple(dict.fromkeys(symbols))  # de-duplicate, keep the given order

    written = 0
    requests = 0
    windows = 0
    empty: list[EmptyWindow] = []

    for window_start, window_end in iter_windows(start, end, span):
        windows += 1
        for batch in _batches(ordered, batch_size):
            bars, missing, used = await _fetch_window(
                provider, batch, timeframe, window_start, window_end, adjusted
            )
            requests += used
            empty.extend(EmptyWindow(symbol=s, start=window_start, end=window_end) for s in missing)

            flat = [bar for series in bars.values() for bar in series]
            if flat:
                written += await repository.upsert_bars(flat)

        log.info(
            "data.backfill.window_done",
            start=window_start.isoformat(),
            end=window_end.isoformat(),
            bars_written=written,
        )

    result = BackfillResult(
        symbols=ordered,
        bars_written=written,
        windows=windows,
        requests=requests,
        empty_windows=tuple(empty),
    )
    log.info(
        "data.backfill.done",
        symbols=len(ordered),
        bars=written,
        windows=windows,
        requests=requests,
        empty_windows=len(empty),
    )
    return result
