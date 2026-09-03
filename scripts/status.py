#!/usr/bin/env python
"""What the platform can see right now, from every source that holds a view.

    uv run python scripts/status.py
    uv run python scripts/status.py --symbols SPY,QQQ
    uv run python scripts/status.py --no-broker      # local state only

Read-only. Nothing here places, cancels or amends anything, so it is safe to
run during an incident — which is when you will want it.

Four sources, printed side by side because during a paper run the interesting
thing is where they *disagree*:

    halts        Redis      is trading permitted at all
    quotes       Redis      how fresh the prices orders would be priced against
    bars         Postgres   what the strategy has to decide on
    broker       Alpaca     the account, its positions, its working orders

**One view here is the broker's, not the runner's, and the distinction still
matters.** The runner's own book is now durable — `PositionSnapshotRow` and the
equity snapshots have readers (#44) — but this reads the *broker's*, which is
the authority reconciliation compares against. If the two disagree the runner
halts and says so in its own logs.

For the stored book and what the worker actually did with it, see
`scripts/paper_report.py`, and the `/positions` tab, which reads the snapshot
with its age on it.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from atp_core.brokers.alpaca import AlpacaBroker
from atp_core.config import config_problem_summary, get_settings
from atp_core.domain import RunMode, Timeframe
from atp_core.errors import ATPError, DatabaseUnavailableError
from atp_core.persistence.bars import PostgresBarRepository
from atp_core.persistence.db import create_engine, create_session_factory, is_auth_failure
from atp_core.persistence.quotes import RedisQuoteCache
from atp_core.persistence.redis_client import close_redis, create_redis, create_sync_redis
from atp_core.persistence.worker_config import PostgresWorkerConfigRepository
from atp_core.risk.killswitch import RedisKillSwitch
from atp_core.risk.limits import DEFAULT_RISK_LIMITS, RiskLimits

if TYPE_CHECKING:
    from atp_core.config import Settings

OK = "ok"
STALE = "STALE"
MISSING = "MISSING"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument(
        "--symbols",
        default=None,
        help="comma-separated; defaults to the watchlist saved on the Config tab",
    )
    p.add_argument("--timeframe", default="1d", help="which bar series to report on")
    p.add_argument(
        "--no-broker",
        action="store_true",
        help="skip the venue — local state only, and no credentials needed",
    )
    return p.parse_args(argv)


async def _saved_symbols(settings: Settings) -> list[str]:
    """The watchlist a worker would boot with.

    A read failure is an empty watchlist rather than a traceback: this is the
    script somebody runs when the stack is half up, and it has plenty else to
    report. The database checks below say so in their own words.
    """
    engine = create_engine(settings.database_url)
    try:
        stored = await PostgresWorkerConfigRepository(create_session_factory(engine)).load()
    except Exception:
        return []
    finally:
        await engine.dispose()
    return [] if stored is None else list(stored.config.symbols)


async def _saved_limits(settings: Settings) -> tuple[RiskLimits, str]:
    """The risk ceilings in force, and where they came from.

    **Three states, not two**, and the label is how they are told apart. The
    ceilings and the provenance travel together because the number alone is
    ambiguous in a way that matters here: 30 seconds means something different
    if an operator chose it, if nobody has chosen anything, and if the database
    could not be asked. This is the script somebody runs when the stack is half
    up, so all three happen.

    Defaults are a *state* in the middle case and a **fallback** in the third,
    and the header says which. Printing "budget 30s (Config tab)" over an
    unreachable database would state a number nobody had set as though somebody
    had; printing it over an empty table would be wrong in the other direction,
    since the defaults genuinely are what an unconfigured platform enforces.

    Read failures are swallowed for the reason `_saved_symbols` returns an empty
    list rather than raising: there is plenty else on this page to report.
    """
    engine = create_engine(settings.database_url)
    try:
        stored = await PostgresWorkerConfigRepository(create_session_factory(engine)).load()
    except Exception:
        return DEFAULT_RISK_LIMITS, "defaults; the database is not answering"
    finally:
        await engine.dispose()
    if stored is None:
        return DEFAULT_RISK_LIMITS, "defaults; nothing saved on the Config tab yet"
    return stored.config.risk, "Config tab → risk limits"


async def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    # Before `get_settings()`, which raises on a configuration that will not
    # validate — and this is one of the two scripts someone runs *because*
    # nothing is working, so dying with the traceback it exists to explain is
    # the least useful thing it could do.
    unloadable = config_problem_summary()
    if unloadable is not None:
        print(f"cannot read the configuration: {unloadable}")
        print("run `make check-env` — it names the value, the line, and what is wrong")
        return 1

    settings = get_settings()

    try:
        timeframe = Timeframe(args.timeframe)
    except ValueError:
        supported = ", ".join(t.value for t in Timeframe)
        raise SystemExit(f"--timeframe must be one of: {supported}") from None

    symbols = (
        [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
        if args.symbols
        else await _saved_symbols(settings)
    )

    print(f"run mode   {settings.run_mode.value}")
    print(f"broker     {settings.broker_base_url}")
    if settings.is_live:
        print("           *** LIVE — these are real positions ***")
    print()

    _print_halts(settings)
    if not symbols:
        print("\nno symbols — set a watchlist on the dashboard's Config tab, or pass --symbols\n")
    else:
        await _print_local(settings, symbols, timeframe)

    if args.no_broker:
        print("\nbroker: skipped (--no-broker)")
    elif settings.run_mode is RunMode.BACKTEST:
        print("\nbroker: skipped — run mode is backtest, so there is no venue to ask")
    else:
        await _print_broker(settings)

    return 0


def _print_halts(settings: Settings) -> None:
    """Layer 6 first, because it decides whether anything else matters."""
    kill_switch = RedisKillSwitch(create_sync_redis(settings.redis_url))
    halts = kill_switch.active_halts()
    if not halts:
        print("halts      none — trading is permitted")
        return
    print(f"halts      HALTED ({len(halts)} active)")
    for record in halts:
        target = "" if record.target is None else f":{record.target}"
        print(
            f"           {record.scope.value}{target}  {record.reason.value}  "
            f"by {record.engaged_by}  since {record.engaged_at.isoformat()}"
        )


async def _print_local(settings: Settings, symbols: list[str], timeframe: Timeframe) -> None:
    """Quotes and bars — what an order would be priced against, and what a
    strategy would decide on."""
    now = datetime.now(UTC)
    # The verdicts below need a number even when the row cannot be read, so the
    # defaults stand in — and the header carries `source`, so a stale-quote
    # verdict is never read as measured against a ceiling the operator set when
    # it was measured against a fallback.
    limits, source = await _saved_limits(settings)
    budget = limits.max_quote_age_seconds

    redis = create_redis(settings.redis_url)
    engine = create_engine(settings.database_url)
    try:
        quotes = await RedisQuoteCache(redis).get_quotes(symbols)
        repo = PostgresBarRepository(create_session_factory(engine))

        print(f"\nquotes     freshness budget {budget}s ({source})")
        for symbol in symbols:
            quote = quotes.get(symbol)
            if quote is None:
                print(f"           {symbol:<8} {MISSING} — no quote cached")
                continue
            age = (now - quote.ts).total_seconds()
            verdict = OK if age <= budget else STALE
            print(
                f"           {symbol:<8} {verdict:<7} {age:6.1f}s old   "
                f"bid {quote.bid} / ask {quote.ask}"
            )

        print(f"\nbars       {timeframe.value}")
        try:
            for symbol in symbols:
                stored = await repo.get_last_n_bars(symbol, timeframe, 1)
                if not stored:
                    print(f"           {symbol:<8} {MISSING} — nothing stored")
                    continue
                bar = stored[-1]
                age_h = (now - bar.ts).total_seconds() / 3600
                print(
                    f"           {symbol:<8} last {bar.ts.isoformat()}  "
                    f"({age_h:.1f}h ago)  close {bar.close}"
                )
        # As `_print_broker` below, and for its reason: a source we cannot reach
        # *is* the status. This did not catch, so during an outage of the one
        # store it reads the script exited here with a traceback — before the
        # halt state and the broker's positions, which are the two things an
        # operator opens this for at the moment Postgres is down.
        # docs/RUNBOOK.md promised this tool still worked then. It did not.
        except DatabaseUnavailableError as exc:
            print(f"           UNREACHABLE — {exc}")
            if is_auth_failure(exc):
                print("           The database is up and refused the credentials; it will keep")
                print("           refusing them. Run `make check-env`, then docs/RUNBOOK.md,")
                print('           "password authentication failed".')
            print("           Everything below reads Redis or the venue and is unaffected.")
    finally:
        await close_redis(redis)
        await engine.dispose()


async def _print_broker(settings: Settings) -> None:
    """The venue's own view. Failures are reported, not raised — a broker we
    cannot reach *is* the status, and the rest of the report is still useful."""
    broker = AlpacaBroker(settings)
    try:
        account = await broker.get_account()
        print(f"\nbroker     {account.account_id}")
        print(f"           equity {account.equity}   cash {account.cash}")
        print(f"           buying power {account.buying_power}")
        if account.trading_blocked:
            print("           *** TRADING BLOCKED at the venue ***")

        positions = await broker.get_positions()
        print(f"\npositions  {len(positions)} at the venue")
        for position in positions:
            print(
                f"           {position.symbol:<8} {position.qty:>10} @ "
                f"{position.avg_entry_price}   last {position.last_price}"
            )

        orders = await broker.get_open_orders()
        print(f"\norders     {len(orders)} working")
        for order in orders:
            print(
                f"           {order.symbol:<8} {order.side.value:<4} {order.qty:>8} "
                f"{order.order_type.value:<10} {order.client_order_id}"
            )
        if orders:
            print(
                "\n           An order here the runner does not know about is an orphan.\n"
                "           Reconciliation reports it; it is never cancelled automatically,\n"
                "           because it is most often a protective stop."
            )
    except ATPError as exc:
        print(f"\nbroker     UNREACHABLE — {exc}")
        print("           Reconciliation halts on this rather than trading unverified.")
    finally:
        await broker.aclose()


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except ATPError as exc:
        print(f"error: {exc}", file=sys.stderr)
        sys.exit(1)
