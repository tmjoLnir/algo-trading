"""Bar storage — the `BarRepository` port over the TimescaleDB hypertable.

Backfills overlap constantly: an operator re-runs a window, a reconnect
backfills a gap that partly exists, a corporate action makes yesterday's
adjusted prices wrong. So every write here is an upsert on the natural key
`(symbol, timeframe, ts)`, and re-running a backfill is expected rather than
exceptional (docs/DATA.md).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

from sqlalchemy import CursorResult, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from atp_core.data.gaps import expected_windows, query_bounds, require_supported, scan_gaps
from atp_core.domain import Bar, Timeframe
from atp_core.logging import get_logger
from atp_core.persistence.db import read_scope, session_scope
from atp_core.persistence.models import BarRow

if TYPE_CHECKING:
    from datetime import datetime

    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from atp_core.clock import TradingCalendar
    from atp_core.data.ports import BarRepository

log = get_logger(__name__)

#: Rows per INSERT. PostgreSQL caps a statement at 65535 bind parameters and a
#: bar binds 11, so the hard ceiling is ~5900 rows; 2000 leaves room and keeps
#: any single statement short enough not to hold a lock while a five-year
#: minute backfill (roughly 500k bars per symbol) streams through.
_UPSERT_CHUNK_ROWS = 2000


class PostgresBarRepository:
    """`BarRepository` over PostgreSQL/TimescaleDB.

    Postgres-specific rather than portable: the idempotency this table needs is
    `INSERT ... ON CONFLICT`, and emulating it with a read-then-write would race
    two backfills against each other.

    Takes a session factory rather than a session. Ingestion is the caller here
    — a backfill loop or the stream consumer — and neither wants to own a
    transaction spanning hundreds of thousands of rows.
    """

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        calendar: TradingCalendar | None = None,
        exchange: str = "NYSE",
    ) -> None:
        self._session_factory = session_factory
        #: Gap detection is the only thing here that needs a calendar, so one is
        #: built on demand (see `_get_calendar`). Inject one to share the
        #: per-year session cache across repositories, or to point at a venue
        #: other than `exchange`.
        self._calendar = calendar
        self._exchange = exchange

    # ── writes ──────────────────────────────────────────────────────────────

    async def upsert_bars(self, bars: list[Bar]) -> int:
        """Insert or update, keyed on `(symbol, timeframe, ts)`.

        Returns the number of rows written — inserted and updated together,
        because a re-run of an existing window legitimately touches every row
        and reporting 0 for it would read as "nothing happened".

        `adj_close`, `vwap` and `trade_count` are merged with COALESCE rather
        than overwritten. This is the subtle one: a raw-only fetch carries no
        `adj_close`, and a plain overwrite would silently erase the adjusted
        prices an earlier pass stored — leaving backtests to run on NULLs. A
        *present* incoming value still wins, because a corporate action makes
        every historical `adj_close` for that symbol stale and the newer figure
        is the correct one.
        """
        if not bars:
            return 0

        rows = [self._to_row(bar) for bar in bars]
        written = 0

        async with session_scope(self._session_factory) as session:
            for start in range(0, len(rows), _UPSERT_CHUNK_ROWS):
                chunk = rows[start : start + _UPSERT_CHUNK_ROWS]
                stmt = pg_insert(BarRow).values(chunk)
                columns = BarRow.__table__.c
                stmt = stmt.on_conflict_do_update(
                    # Inferred from the columns, not a constraint name: the
                    # natural key is the primary key and carries no name of its
                    # own worth depending on (see the initial migration).
                    index_elements=["symbol", "timeframe", "ts"],
                    set_={
                        "open": stmt.excluded.open,
                        "high": stmt.excluded.high,
                        "low": stmt.excluded.low,
                        "close": stmt.excluded.close,
                        "volume": stmt.excluded.volume,
                        "adj_close": func.coalesce(stmt.excluded.adj_close, columns.adj_close),
                        "vwap": func.coalesce(stmt.excluded.vwap, columns.vwap),
                        "trade_count": func.coalesce(
                            stmt.excluded.trade_count, columns.trade_count
                        ),
                    },
                )
                # `execute` is typed as returning `Result`, which has no
                # rowcount; DML genuinely returns a `CursorResult`, which does.
                result = cast("CursorResult[Any]", await session.execute(stmt))
                written += result.rowcount

        log.info("data.bars.upserted", rows=written, batches=-(-len(rows) // _UPSERT_CHUNK_ROWS))
        return written

    # ── reads ───────────────────────────────────────────────────────────────

    async def get_bars(
        self, symbol: str, timeframe: Timeframe, start: datetime, end: datetime
    ) -> list[Bar]:
        """Bars in `[start, end)`, chronological.

        Half-open deliberately: consecutive windows chained end-to-start then
        cover the range exactly once. A closed interval double-counts the
        boundary bar, which is one duplicated candle in every indicator
        computed across the seam.
        """
        _require_utc(start, "start")
        _require_utc(end, "end")

        stmt = (
            select(BarRow)
            .where(
                BarRow.symbol == symbol,
                BarRow.timeframe == timeframe.value,
                BarRow.ts >= start,
                BarRow.ts < end,
            )
            .order_by(BarRow.ts)
        )
        async with read_scope(self._session_factory) as session:
            result = await session.execute(stmt)
            return [self._to_bar(row) for row in result.scalars()]

    async def get_last_n_bars(self, symbol: str, timeframe: Timeframe, n: int) -> list[Bar]:
        """The most recent `n` bars, returned chronological.

        Fetched newest-first so the database reads `n` rows rather than the
        symbol's whole history, then reversed — indicators are defined over a
        forward series and handing them a reversed one produces numbers that
        look plausible and are wrong.
        """
        if n < 1:
            raise ValueError(f"n must be at least 1, got {n}")

        stmt = (
            select(BarRow)
            .where(BarRow.symbol == symbol, BarRow.timeframe == timeframe.value)
            .order_by(BarRow.ts.desc())
            .limit(n)
        )
        async with read_scope(self._session_factory) as session:
            result = await session.execute(stmt)
            return [self._to_bar(row) for row in reversed(list(result.scalars()))]

    async def find_gaps(
        self, symbol: str, timeframe: Timeframe, start: datetime, end: datetime
    ) -> list[tuple[datetime, datetime]]:
        """Missing windows in `[start, end)`, excluding legitimate closures.

        The calendar decides what "missing" means — weekends, holidays and early
        closes are not gaps — and `atp_core.data.gaps` holds that reasoning,
        pure and testable. This method is the part that needs a database: read
        the timestamps we hold, and let the scan compare them against the
        exchange's schedule.

        Only the timestamps are read, not whole bars: a five-year minute scan is
        half a million rows per symbol and the OHLCV columns are dead weight in
        every one of them.

        Raises `ValueError` for a timeframe whose bar grid is not known exactly
        (`1h`, `4h` — see `gaps.SUPPORTED_TIMEFRAMES`). Reporting confident
        nonsense for those would be worse than refusing.
        """
        _require_utc(start, "start")
        _require_utc(end, "end")
        if start >= end:
            raise ValueError(f"start must be before end, got start={start} end={end}")
        require_supported(timeframe)

        calendar = self._get_calendar()
        query_start, query_end = query_bounds(timeframe, start, end)
        stored = await self._stored_timestamps(symbol, timeframe, query_start, query_end)

        scan = scan_gaps(expected_windows(calendar, timeframe, start, end), stored)

        # A stored bar that belongs to no session is the shape a timestamp
        # convention we do not expect makes — and it would otherwise present as
        # sessions that look missing while the data is sitting right there
        # (docs/DATA.md 'Gaps'). Every daily bar should land in a session; an
        # intraday one legitimately may not, because extended-hours bars fall
        # outside the regular session, so for those only a total miss is a
        # signal. Worth saying either way: the alternative is an operator
        # re-running a backfill that keeps writing bars this scan keeps
        # refusing to see.
        if scan.unmatched and (timeframe is Timeframe.D1 or scan.matched == 0):
            log.warning(
                "data.bars.gaps.unmatched_bars",
                symbol=symbol,
                timeframe=timeframe.value,
                unmatched=scan.unmatched,
                matched=scan.matched,
                expected=scan.expected,
                hint="stored timestamps do not line up with the exchange schedule",
            )

        log.info(
            "data.bars.gaps_scanned",
            symbol=symbol,
            timeframe=timeframe.value,
            start=start.isoformat(),
            end=end.isoformat(),
            expected=scan.expected,
            missing=scan.missing,
            windows=len(scan.windows),
        )
        return list(scan.windows)

    async def stored_series(self) -> list[tuple[str, Timeframe]]:
        """Every `(symbol, timeframe)` held — what a sweep iterates.

        Ordered by symbol and then by the stored timeframe string: deterministic
        rather than meaningful, so a sweep covers the same ground in the same
        order every night and its logs can be diffed.

        A `DISTINCT` over the leading columns of the primary key, which
        PostgreSQL answers by walking that index rather than the table. It is
        still proportional to the index, not to the number of distinct series,
        so this belongs in a nightly job and not on a request path; if it ever
        becomes the slow part, the fix is a recursive skip-scan over the same
        index rather than a second table to keep in sync.

        A timeframe the enum does not know is skipped rather than raising: it
        can only come from a hand-written row, and one bad row should not stop
        the sweep from checking every other series.
        """
        stmt = (
            select(BarRow.symbol, BarRow.timeframe)
            .distinct()
            .order_by(BarRow.symbol, BarRow.timeframe)
        )
        async with read_scope(self._session_factory) as session:
            rows = (await session.execute(stmt)).all()

        series: list[tuple[str, Timeframe]] = []
        for symbol, timeframe in rows:
            try:
                series.append((symbol, Timeframe(timeframe)))
            except ValueError:
                log.warning("data.bars.unknown_timeframe", symbol=symbol, timeframe=timeframe)
        return series

    async def _stored_timestamps(
        self, symbol: str, timeframe: Timeframe, start: datetime, end: datetime
    ) -> list[datetime]:
        """Bar timestamps in `[start, end)`, ascending — the scan needs no more."""
        stmt = (
            select(BarRow.ts)
            .where(
                BarRow.symbol == symbol,
                BarRow.timeframe == timeframe.value,
                BarRow.ts >= start,
                BarRow.ts < end,
            )
            .order_by(BarRow.ts)
        )
        async with read_scope(self._session_factory) as session:
            result = await session.execute(stmt)
            return list(result.scalars())

    def _get_calendar(self) -> TradingCalendar:
        """The exchange calendar, built on first use.

        Not built in `__init__`: constructing one imports pandas, and every API
        process would pay that at startup for a method most of them never call.
        """
        if self._calendar is None:
            from atp_core.clock import TradingCalendar

            self._calendar = TradingCalendar(self._exchange)
        return self._calendar

    # ── mapping ─────────────────────────────────────────────────────────────

    @staticmethod
    def _to_row(bar: Bar) -> dict[str, Any]:
        return {
            "symbol": bar.symbol,
            "timeframe": bar.timeframe.value,
            "ts": bar.ts,
            "open": bar.open,
            "high": bar.high,
            "low": bar.low,
            "close": bar.close,
            "volume": bar.volume,
            "adj_close": bar.adj_close,
            "vwap": bar.vwap,
            "trade_count": bar.trade_count,
        }

    @staticmethod
    def _to_bar(row: BarRow) -> Bar:
        return Bar(
            symbol=row.symbol,
            ts=row.ts,
            timeframe=Timeframe(row.timeframe),
            open=row.open,
            high=row.high,
            low=row.low,
            close=row.close,
            volume=row.volume,
            adj_close=row.adj_close,
            vwap=row.vwap,
            trade_count=row.trade_count,
        )


def _require_utc(ts: datetime, field: str) -> None:
    if ts.tzinfo is None:
        raise ValueError(f"{field} must be timezone-aware (rule §1.2), got naive {ts!r}")


if TYPE_CHECKING:
    # mypy enforces that the adapter still satisfies its port.
    def _conforms(adapter: PostgresBarRepository) -> BarRepository:
        return adapter
