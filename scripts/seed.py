#!/usr/bin/env python
"""Seed a development database: a strategy row per registered strategy, and a
little synthetic bar history so the dashboard has something to render.

    uv run python scripts/seed.py          # or: make seed

**The strategy rows were the point.** `backtest_runs.strategy_id` is a foreign
key onto `strategies`, and for a long time the only thing that ever wrote that
table was `StrategyRunner.warmup` at a live session open. So on a clean database
the Backtests tab offered nothing to run and `POST /api/v1/backtests` answered
409 — which made queueing a backtest require configuring a *trading* worker with
broker credentials first. A backtest needs stored bars and nothing else, and
this is what closed that gap.

`POST /api/v1/backtests` now writes the same row itself for any registered class
it is queueing the first run of, so this half of the seed is no longer what
stands between a clean database and a backtest. It is still worth writing: the
Strategies tab has stored rows to show without anything having run, and the rows
are here before the bars rather than as a side effect of using the app. The bars
are what a development database cannot get anywhere else.

The bars are a convenience beside it, and they are **fabricated** — a driftless
random walk written under NASDAQ's reserved test tickers, never under a real
symbol. `atp_core.data.seed` explains why in full; the short version is that
seeded bars must be incapable of overwriting a real backfilled history and
incapable of producing a result anybody believes. For real history use
`scripts/backfill_bars.py`, which needs vendor credentials this does not.

Development only, and it enforces that rather than asking. See `refusal` below.

Thin on purpose: argument handling, the environment guard, wiring the adapters
and reporting live here; generating the series lives in `atp_core.data.seed`,
where it is tested without a database.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import UTC, date, datetime
from typing import TYPE_CHECKING

from pydantic import ValidationError
from sqlalchemy.engine import make_url

from atp_core.clock import SystemClock, TradingCalendar
from atp_core.config import get_settings
from atp_core.data.seed import (
    DEFAULT_ANNUAL_VOLATILITY,
    DEFAULT_FIRST_DAY,
    DEFAULT_LAST_DAY,
    DEFAULT_SEED_SYMBOLS,
    RESERVED_TEST_SYMBOLS,
    require_reserved,
    synthetic_daily_bars,
)
from atp_core.domain import Timeframe
from atp_core.errors import ATPError
from atp_core.logging import configure as configure_logging
from atp_core.logging import get_logger
from atp_core.persistence.bars import PostgresBarRepository
from atp_core.persistence.db import create_engine, create_session_factory
from atp_core.persistence.strategies import PostgresStrategyRepository
from atp_core.strategy import examples as _examples  # noqa: F401 — populates the registry
from atp_core.strategy import registry
from atp_core.strategy.ports import StrategyRecord

if TYPE_CHECKING:
    from atp_core.config import Settings

log = get_logger(__name__)

#: Database hosts a development stack legitimately points at. `db` is the
#: compose service name, which is what the containers resolve; the loopback
#: forms are what `make seed` uses from the host. An empty host is a Unix
#: socket, which is local by definition.
_LOCAL_DB_HOSTS = frozenset({"", "localhost", "127.0.0.1", "::1", "db"})


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--symbols",
        default=",".join(DEFAULT_SEED_SYMBOLS),
        help=(
            "comma-separated reserved test tickers to fabricate bars for "
            f"(allowed: {', '.join(sorted(RESERVED_TEST_SYMBOLS))})"
        ),
    )
    p.add_argument(
        "--start",
        default=DEFAULT_FIRST_DAY.isoformat(),
        help="YYYY-MM-DD. Fixed by default so a re-seed reproduces the same bars",
    )
    p.add_argument("--end", default=DEFAULT_LAST_DAY.isoformat(), help="YYYY-MM-DD")
    p.add_argument(
        "--volatility",
        type=float,
        default=DEFAULT_ANNUAL_VOLATILITY,
        help="annualised volatility of the generated series",
    )
    p.add_argument(
        "--no-bars",
        action="store_true",
        help="write only the strategy rows — use with backfill_bars.py for real history",
    )
    p.add_argument(
        "--no-strategies",
        action="store_true",
        help="write only the bars",
    )
    p.add_argument(
        "--allow-remote-database",
        action="store_true",
        help=(
            "permit a non-local DATABASE_URL. Does NOT bypass the ATP_ENV check — "
            "for a development database that happens to live on another host"
        ),
    )
    return p.parse_args(argv)


def parse_day(value: str, field: str) -> date:
    """A calendar day. Naive input is fine here and only here: these are days,
    not instants — the instants are derived from the exchange calendar, which is
    the only thing that knows when a session on a given date opened."""
    try:
        return datetime.strptime(value, "%Y-%m-%d").replace(tzinfo=UTC).date()
    except ValueError as exc:
        raise SystemExit(f"--{field} must be YYYY-MM-DD, got {value!r}") from exc


def refusal(settings: Settings, *, allow_remote_database: bool) -> str | None:
    """Why this must not run, or None.

    Two checks, and the asymmetry between them is deliberate.

    `ATP_ENV` is the **declared** environment, so it is the authority and it has
    no override. Staging is refused alongside production: fabricated bars in a
    staging database are read by whoever is validating a release there, and the
    whole argument for reserved tickers is that fake data must never be
    somewhere it can be taken for real.

    The database host is a **heuristic** — it catches the actual accident, which
    is a development `.env` still pointing at a shared database — so it takes a
    flag, because a remote development database is a real thing and a guard that
    blocks legitimate work is a guard people learn to route around. The flag
    does not touch the check above it.

    Returned as a string rather than raised so it can be asserted without a
    database and without catching.
    """
    if settings.env != "development":
        return (
            f"ATP_ENV is {settings.env!r}. This script fabricates market data and writes it "
            "to the database it is pointed at; it runs only with ATP_ENV=development. There "
            "is no flag for this."
        )

    host = (make_url(settings.database_url).host or "").lower()
    if host not in _LOCAL_DB_HOSTS and not allow_remote_database:
        return (
            f"DATABASE_URL points at {host!r}, which is not a local host. If that really is a "
            "development database, pass --allow-remote-database; if it is a shared one, this "
            "would write fabricated bars into it."
        )
    return None


def seed_strategies() -> list[StrategyRecord]:
    """A row per registered strategy, on its own default parameters.

    Every registered class rather than a named one, because the reason a row is
    needed is the foreign key — and a class the picker cannot offer is a class
    nobody can backtest, whichever one it is. Written when the registry held
    only `sma_crossover` and unedited when `buy_and_hold` joined it, which was
    the point.

    `universe` is left empty rather than filled with the seeded tickers. The
    column records the symbols a strategy is *configured* to trade, and writing
    reserved test tickers into it would be this script inventing a configuration
    on a strategy's behalf — the backtest form asks for symbols per run anyway.
    The state is `draft`, which `StrategyRepository.ensure` writes and which is
    the ratchet's first rung: a seed grants no promotion.
    """
    records = []
    for name, cls in sorted(registry.all_strategies().items()):
        params = registry.default_params(cls)
        records.append(
            StrategyRecord(
                id=name,
                name=name,
                kind="coded",
                class_name=cls.__name__,
                params=params,
                universe=(),
                # A strategy that declares its own timeframe wins; the column's
                # own default is daily and so is this fallback.
                timeframe=str(params.get("timeframe", Timeframe.D1.value)),
            )
        )
    return records


async def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    # Judged before configuration is loaded, so a mistyped ticker says so rather
    # than surfacing as whatever the config layer complains about first.
    symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    first, last = parse_day(args.start, "start"), parse_day(args.end, "end")
    if last < first:
        raise SystemExit(f"--start must be on or before --end ({first} > {last})")
    if args.no_bars and args.no_strategies:
        raise SystemExit("--no-bars and --no-strategies together would seed nothing")
    if not args.no_bars:
        if not symbols:
            raise SystemExit("--symbols is empty")
        # Both of these would otherwise raise from inside the generation loop —
        # after a connection is open and, for the first symbol, after the
        # strategy rows are already written. A half-seeded database is a worse
        # answer to a bad argument than a refusal.
        if args.volatility <= 0:
            raise SystemExit(f"--volatility must be positive, got {args.volatility}")
        try:
            require_reserved(symbols)
        except ATPError as exc:
            raise SystemExit(str(exc)) from None

    try:
        settings = get_settings()
    except ValidationError as exc:
        raise SystemExit(f"configuration error:\n{exc}") from None

    configure_logging(settings.log_level, settings.log_format)

    stop = refusal(settings, allow_remote_database=args.allow_remote_database)
    if stop:
        raise SystemExit(f"refusing to seed: {stop}")

    engine = create_engine(settings.database_url)
    session_factory = create_session_factory(engine)
    calendar = TradingCalendar()

    try:
        if not args.no_strategies:
            repo = PostgresStrategyRepository(session_factory, SystemClock())
            records = seed_strategies()
            for record in records:
                await repo.ensure(record)
            print(f"strategies  {len(records)} row(s)")
            for record in records:
                print(f"            {record.name:<16} {record.class_name}  params={record.params}")
            if not records:
                print("            none registered — nothing imported a strategy module")

        if not args.no_bars:
            bar_repo = PostgresBarRepository(session_factory, calendar=calendar)
            print(f"\nbars        {first} → {last}, daily, SYNTHETIC")
            for symbol in symbols:
                bars = synthetic_daily_bars(
                    symbol,
                    first,
                    last,
                    calendar=calendar,
                    annual_volatility=args.volatility,
                )
                written = await bar_repo.upsert_bars(bars)
                if bars:
                    print(
                        f"            {symbol:<8} {written:>5} rows  "
                        f"{bars[0].close} → {bars[-1].close}"
                    )
                else:
                    print(f"            {symbol:<8} no sessions in that range")

            print(
                "\n            These bars are a driftless random walk, not market data.\n"
                "            A strategy has nothing to find in one, so a profitable-looking\n"
                "            result here is noise (docs/BACKTESTING.md). They are written\n"
                "            under reserved test tickers so they can never overwrite or be\n"
                "            mistaken for a real history — for that, run backfill_bars.py."
            )
    finally:
        await engine.dispose()

    log.info(
        "seed.done",
        strategies=0 if args.no_strategies else len(seed_strategies()),
        symbols=0 if args.no_bars else len(symbols),
        first=str(first),
        last=str(last),
    )

    # Named from the registry rather than hard-coded: a suggestion that runs
    # `sma_crossover` in a checkout where that is not what is registered sends a
    # reader after a strategy the CLI will refuse.
    runnable = next(iter(sorted(registry.all_strategies())), None)
    print("\nNext:")
    if not args.no_bars and symbols and runnable:
        print(
            f"  uv run python scripts/run_backtest.py --strategy {runnable} "
            f"--symbols {symbols[0]} \\\n"
            f"    --start {first} --end {last}"
        )
    if not args.no_strategies:
        print("  ...or open the Backtests tab: the strategy picker is populated now.")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except ATPError as exc:
        print(f"error: {exc}", file=sys.stderr)
        sys.exit(1)
