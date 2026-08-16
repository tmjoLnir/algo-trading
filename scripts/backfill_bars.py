#!/usr/bin/env python
"""Backfill historical bars.

    uv run python scripts/backfill_bars.py --symbols AAPL,MSFT --start 2020-01-01

Idempotent — safe to re-run over a range you already have. Rate-limited to stay
under the vendor's 200 req/min, and paginates to exhaustion (silently taking the
first page is a data gap that looks like data).

Thin on purpose: argument handling, wiring the adapters, and reporting. The
ordering and windowing live in `atp_core.data.backfill`, where they can be
tested without a network or a database.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from pydantic import ValidationError

from atp_core.clock import TradingCalendar
from atp_core.config import get_settings
from atp_core.data.backfill import DEFAULT_BATCH_SIZE, backfill_bars, window_days_for
from atp_core.data.gaps import SUPPORTED_TIMEFRAMES
from atp_core.data.providers.alpaca import AlpacaHistoricalProvider
from atp_core.domain import Timeframe
from atp_core.logging import configure as configure_logging
from atp_core.logging import get_logger
from atp_core.persistence.bars import PostgresBarRepository
from atp_core.persistence.db import create_engine, create_session_factory

if TYPE_CHECKING:
    from zoneinfo import ZoneInfo

log = get_logger(__name__)

#: Under the free tier's published 200/min, with room for the clock skew
#: between our pacing and theirs.
DEFAULT_REQUESTS_PER_MINUTE = 180


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--symbols", required=True, help="comma-separated tickers")
    p.add_argument("--start", required=True, help="YYYY-MM-DD")
    p.add_argument("--end", default=None, help="YYYY-MM-DD (default: today)")
    p.add_argument("--timeframe", default="1d")
    p.add_argument("--adjusted", action="store_true", default=True)
    p.add_argument(
        "--raw-only",
        action="store_true",
        help="skip the adjusted pass — halves the requests, leaves adj_close unset",
    )
    p.add_argument(
        "--verify",
        action="store_true",
        help="afterwards, check the stored range against the trading calendar and report gaps",
    )
    p.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE)
    p.add_argument(
        "--window-days", type=int, default=None, help="override the per-timeframe default"
    )
    p.add_argument("--requests-per-minute", type=int, default=DEFAULT_REQUESTS_PER_MINUTE)
    return p.parse_args(argv)


def _parse_day(value: str, field: str) -> datetime:
    """A calendar day at UTC midnight. Naive input is rejected at the boundary
    rather than being silently assumed to mean UTC (rule §1.2)."""
    try:
        return datetime.strptime(value, "%Y-%m-%d").replace(tzinfo=UTC)
    except ValueError as exc:
        raise SystemExit(f"--{field} must be YYYY-MM-DD, got {value!r}") from exc


def _format_gap(timeframe: Timeframe, tz: ZoneInfo, start: datetime, end: datetime) -> str:
    """One gap window, in the terms an operator thinks in.

    Daily gaps are named by the sessions they cover — the underlying window is
    the exchange-local day, and printing `2024-01-11T05:00Z` for "no bar on the
    11th" reads as a bug in the report rather than a hole in the data.
    """
    if timeframe is Timeframe.D1:
        first = start.astimezone(tz).date()
        # The window is half-open, so its end is the morning after the last
        # missing session.
        last = (end.astimezone(tz) - timedelta(days=1)).date()
        return str(first) if first == last else f"{first} → {last}"
    return f"{start:%Y-%m-%d %H:%M} → {end:%Y-%m-%d %H:%M} UTC"


async def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    # Everything that can be judged from the arguments alone is judged first,
    # before configuration is even loaded. A mistyped timeframe should say so;
    # it should not surface as whatever the config layer happens to complain
    # about on a machine with no .env.
    symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    if not symbols:
        raise SystemExit("--symbols is empty")

    try:
        timeframe = Timeframe(args.timeframe)
    except ValueError:
        supported = ", ".join(t.value for t in Timeframe)
        raise SystemExit(f"--timeframe must be one of: {supported}") from None

    if args.verify and timeframe not in SUPPORTED_TIMEFRAMES:
        # Refused up front rather than after a long run: discovering at the end
        # of a twenty-minute backfill that the check cannot run — or worse,
        # reading its silence as "verified clean" — is the failure worth
        # preventing.
        checkable = ", ".join(
            t.value for t in sorted(SUPPORTED_TIMEFRAMES, key=lambda t: t.seconds)
        )
        raise SystemExit(
            f"--verify cannot check {timeframe.value} bars: their alignment inside a "
            f"session is vendor-specific and unverified, so a gap report would be "
            f"noise. Checkable timeframes: {checkable}. See docs/DATA.md 'Gaps'."
        )

    start = _parse_day(args.start, "start")
    end = _parse_day(args.end, "end") if args.end else datetime.now(UTC)
    if start >= end:
        raise SystemExit(f"--start must be before --end ({start.date()} >= {end.date()})")

    try:
        settings = get_settings()
    except ValidationError as exc:
        # A missing or malformed .env is an ordinary operator situation, not a
        # bug worth a traceback. Pydantic's rendering already names the field.
        raise SystemExit(
            f"Configuration is invalid — check .env against .env.example:\n\n{exc}"
        ) from None

    configure_logging(settings.log_level, settings.log_format)

    if not settings.alpaca_api_key.get_secret_value():
        raise SystemExit(
            "ALPACA_API_KEY is not set. A backfill needs market-data credentials — "
            "fill them into .env (see .env.example)."
        )

    interval = 60.0 / args.requests_per_minute if args.requests_per_minute > 0 else 0.0
    engine = create_engine(settings.database_url)
    provider = AlpacaHistoricalProvider(settings, min_request_interval_seconds=interval)

    log.info(
        "data.backfill.starting",
        symbols=len(symbols),
        timeframe=timeframe.value,
        start=start.date().isoformat(),
        end=end.date().isoformat(),
        adjusted=not args.raw_only,
        window_days=args.window_days or window_days_for(timeframe),
    )

    # One calendar, shared with the repository: it caches sessions per year, and
    # verifying several symbols over the same range should build that once.
    calendar = TradingCalendar() if args.verify else None
    repository = PostgresBarRepository(create_session_factory(engine), calendar=calendar)
    gaps: list[tuple[str, datetime, datetime]] = []

    try:
        result = await backfill_bars(
            provider,
            repository,
            symbols=symbols,
            timeframe=timeframe,
            start=start,
            end=end,
            adjusted=not args.raw_only,
            window_days=args.window_days,
            batch_size=args.batch_size,
        )
        if calendar is not None:
            # After the write, not before: the point of the check is whether
            # what we now hold covers every session the exchange was open for.
            for symbol in result.symbols:
                gaps.extend(
                    (symbol, gap_start, gap_end)
                    for gap_start, gap_end in await repository.find_gaps(
                        symbol, timeframe, start, end
                    )
                )
    finally:
        await provider.aclose()
        await engine.dispose()

    print(
        f"\n{result.bars_written:,} bars written for {len(result.symbols)} symbol(s) "
        f"across {result.windows} window(s) in {result.requests} request(s)."
    )

    if result.empty_windows:
        # Not necessarily a fault — a symbol that had not listed yet has no
        # bars — but the operator is the only one who can tell, so every one is
        # named rather than summarised away.
        print(f"\n{len(result.empty_windows)} window(s) returned no data:")
        for empty in result.empty_windows:
            print(f"  {empty.symbol:<8} {empty.start.date()} → {empty.end.date()}")
        print(
            "\nExpected before a symbol listed or while it was halted. Anything else "
            "means the feed is missing data — do not backtest across it."
        )

    if calendar is not None:
        if gaps:
            print(f"\n{len(gaps)} gap(s) against the {calendar.exchange} calendar:")
            for symbol, gap_start, gap_end in gaps:
                print(f"  {symbol:<8} {_format_gap(timeframe, calendar.tz, gap_start, gap_end)}")
            print(
                "\nThe exchange was open and we hold no bar. Before a listing this is "
                "expected; at the tail of the range the vendor may simply not have "
                "published yet. Anywhere else it is missing data — do not backtest "
                "across it."
            )
        else:
            print(
                f"\nVerified against the {calendar.exchange} calendar: every session in "
                f"{start.date()} → {end.date()} has a bar for all {len(result.symbols)} symbol(s)."
            )

    # Non-zero: a caller chaining this into a pipeline should not treat a
    # partial dataset as a clean one.
    return 1 if (result.empty_windows or gaps) else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
