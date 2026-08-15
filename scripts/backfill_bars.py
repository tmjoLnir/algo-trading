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
from datetime import UTC, datetime

from pydantic import ValidationError

from atp_core.config import get_settings
from atp_core.data.backfill import DEFAULT_BATCH_SIZE, backfill_bars, window_days_for
from atp_core.data.providers.alpaca import AlpacaHistoricalProvider
from atp_core.domain import Timeframe
from atp_core.logging import configure as configure_logging
from atp_core.logging import get_logger
from atp_core.persistence.bars import PostgresBarRepository
from atp_core.persistence.db import create_engine, create_session_factory

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
    p.add_argument("--verify", action="store_true", help="report gaps afterwards")
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


async def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    # Everything that can be judged from the arguments alone is judged first,
    # before configuration is even loaded. A mistyped timeframe should say so;
    # it should not surface as whatever the config layer happens to complain
    # about on a machine with no .env.
    if args.verify:
        # Refused up front rather than after a long run. Calendar-aware gap
        # detection is not built yet, and discovering that at the end of a
        # twenty-minute backfill — or worse, reading its silence as "verified
        # clean" — is the failure worth preventing.
        raise SystemExit(
            "--verify is not available yet: gap detection needs the trading calendar "
            "(see docs/DATA.md 'Gaps' and the Phase 1 roadmap item). Re-run without it; "
            "symbols that returned no data are reported regardless."
        )

    symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    if not symbols:
        raise SystemExit("--symbols is empty")

    try:
        timeframe = Timeframe(args.timeframe)
    except ValueError:
        supported = ", ".join(t.value for t in Timeframe)
        raise SystemExit(f"--timeframe must be one of: {supported}") from None

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

    try:
        result = await backfill_bars(
            provider,
            PostgresBarRepository(create_session_factory(engine)),
            symbols=symbols,
            timeframe=timeframe,
            start=start,
            end=end,
            adjusted=not args.raw_only,
            window_days=args.window_days,
            batch_size=args.batch_size,
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
        for gap in result.empty_windows:
            print(f"  {gap.symbol:<8} {gap.start.date()} → {gap.end.date()}")
        print(
            "\nExpected before a symbol listed or while it was halted. Anything else "
            "means the feed is missing data — do not backtest across it."
        )
        # Non-zero: a caller chaining this into a pipeline should not treat a
        # partial dataset as a clean one.
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
