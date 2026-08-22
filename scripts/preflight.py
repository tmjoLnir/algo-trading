#!/usr/bin/env python
"""Is this configuration ready to spend a week trading paper?

    uv run python scripts/preflight.py
    uv run python scripts/preflight.py --no-broker      # local state only
    uv run python scripts/preflight.py --json

Read-only, and every check is one docs/FIRST_PAPER_RUN.md already asks for. What
it adds is the timing: that document states its preconditions in prose and its
"most likely to break first" list at the end, and an operator working through
them by hand checks four of them and assumes the rest. This checks eleven in
about two seconds.

**Why that is worth a script here specifically.** The input Phase 4's
*Verifiable:* line needs and cannot re-run is calendar time. Almost everything
this catches presents the same way when it is missed — the worker comes up, runs
its loop, and never fills anything — and docs/FIRST_PAPER_RUN.md is explicit
that "a week of no signals is not a week of correct trading". A week that ends
in silence you cannot attribute is a week spent, and the two most likely causes
(too little warmup history; a size the position cap refuses) are both decidable
before the first order.

Exits 0 when every check passes, 1 when any fails. Warnings do not fail it —
see `atp_worker.preflight` for why the two are kept apart.

The decisions live in `atp_worker.preflight` as pure functions. This file is the
I/O: it opens Postgres, Redis and the broker, and hands what it finds to them.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import UTC, datetime
from decimal import Decimal
from typing import TYPE_CHECKING

from atp_core.config import config_problem_summary, get_settings
from atp_core.domain import Timeframe
from atp_core.errors import ATPError
from atp_core.indicators import dispatch
from atp_core.persistence.bars import PostgresBarRepository
from atp_core.persistence.db import create_engine, create_session_factory
from atp_core.persistence.quotes import RedisQuoteCache
from atp_core.persistence.redis_client import close_redis, create_redis, create_sync_redis
from atp_core.risk.killswitch import RedisKillSwitch
from atp_core.risk.stops import StopManager
from atp_core.strategy import registry
from atp_worker import preflight, trading
from atp_worker.preflight import Check, Preflight, Status

if TYPE_CHECKING:
    from atp_core.config import Settings

#: Rendered before the detail, so the eye finds the failures without reading.
MARK = {Status.PASS: "ok  ", Status.WARN: "warn", Status.FAIL: "FAIL", Status.SKIP: "--  "}


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument(
        "--no-broker",
        action="store_true",
        help="skip the venue checks; needs no credentials and no network",
    )
    p.add_argument(
        "--symbols",
        default="",
        help="override the watchlist (default: WORKER_SYMBOLS)",
    )
    p.add_argument("--timeframe", default="1d", help="which bar series the strategy runs on")
    p.add_argument("--json", action="store_true", help="machine-readable, for a CI job or a log")
    return p.parse_args(argv)


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
    symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    symbols = symbols or settings.worker_symbol_list
    timeframe = _timeframe(args.timeframe)

    checks: list[Check] = [
        preflight.check_run_mode(settings),
        preflight.check_credentials(settings),
        preflight.check_locks(trading.decide(settings, symbols)),
        preflight.check_strategy(settings),
        preflight.check_stop_config(settings),
        preflight.check_alert_transport(settings),
    ]
    checks.extend(await _local_checks(settings, symbols, timeframe))
    checks.extend(await _venue_checks(settings, symbols, timeframe, skip=args.no_broker))

    report = Preflight(checks)
    if args.json:
        print(json.dumps(_jsonable(report), indent=2))
    else:
        _render(report)
    return report.exit_code()


def _why(exc: Exception) -> str:
    """The exception's type, and deliberately nothing else.

    A library's error text can embed a `repr` of whatever it was handed — and
    the thing handed to a database driver here is `Settings`, which carries
    every credential this platform holds. Rendering `str(exc)` into a line an
    operator pastes into an issue is how rule §1.6 gets broken by accident
    rather than by intent. `ConnectionRefusedError` plus the `fix` line is what
    anyone acts on regardless; the traceback is still there for whoever needs
    more, where it belongs.
    """
    return type(exc).__name__


def _timeframe(raw: str) -> Timeframe:
    try:
        return Timeframe(raw)
    except ValueError:
        valid = ", ".join(t.value for t in Timeframe)
        raise SystemExit(f"--timeframe must be one of: {valid}") from None


# ── local state ─────────────────────────────────────────────────────────────


async def _local_checks(
    settings: Settings, symbols: list[str], timeframe: Timeframe
) -> list[Check]:
    """Redis and Postgres. Each source is caught separately, because a Redis
    that is down must not stop the history checks from running — an operator
    bringing the stack up one piece at a time is the normal case."""
    checks: list[Check] = []

    try:
        switch = RedisKillSwitch(create_sync_redis(settings.redis_url))
        checks.append(preflight.check_not_halted(switch.active_halts()))
    # Any transport failure is one answer here: we did not look.
    except Exception as exc:
        checks.append(
            Check("kill switch", Status.SKIP, f"Redis unreachable ({_why(exc)})", fix="make up")
        )

    required = _warmup_bars(settings)
    engine = create_engine(settings.database_url)
    try:
        bars = PostgresBarRepository(create_session_factory(engine))
        for symbol in symbols:
            checks.append(await _history_check(bars, symbol, timeframe, required))
    except Exception as exc:
        checks.append(
            Check(
                "history",
                Status.SKIP,
                f"database unreachable ({_why(exc)})",
                fix="make up && make migrate",
            )
        )
    finally:
        await engine.dispose()

    try:
        redis = create_redis(settings.redis_url)
        quotes = RedisQuoteCache(redis)
        now = datetime.now(UTC)
        for symbol in symbols:
            quote = await quotes.get_quote(symbol)
            age = None if quote is None else (now - quote.ts).total_seconds()
            checks.append(
                preflight.check_quote_freshness(
                    symbol, age_seconds=age, budget=settings.risk.max_quote_age_seconds
                )
            )
        await close_redis(redis)
    except Exception as exc:
        checks.append(
            Check("quotes", Status.SKIP, f"Redis unreachable ({_why(exc)})", fix="make up")
        )

    return checks


async def _history_check(
    bars: PostgresBarRepository, symbol: str, timeframe: Timeframe, required: int
) -> Check:
    stored = await bars.get_last_n_bars(symbol, timeframe, max(required, 1))
    return preflight.check_warmup(
        symbol,
        required=required,
        stored=len(stored),
        newest=stored[-1].ts if stored else None,
    )


def _warmup_bars(settings: Settings) -> int:
    """What the configured strategy needs before it will decide anything.

    Zero when the strategy cannot be constructed — `check_strategy` has already
    said so in its own line, and raising a second time here would report one
    misconfiguration as two.
    """
    if not settings.worker_strategy:
        return 0
    try:
        strategy = registry.get(settings.worker_strategy)(trading.strategy_params(settings))
    except (ATPError, TypeError, ValueError):
        return 0
    return strategy.warmup_bars


# ── the venue ───────────────────────────────────────────────────────────────


async def _venue_checks(
    settings: Settings, symbols: list[str], timeframe: Timeframe, *, skip: bool
) -> list[Check]:
    """The account, and the one arithmetic question that needs it.

    Sizing lands here rather than with the local checks because it needs the
    equity the venue reports. Sizing against the *configured* starting cash
    would answer a question about a different account — and on a paper account
    that has been traded before, a materially different one.
    """
    if skip:
        return [
            Check("account", Status.SKIP, "--no-broker"),
            Check("sizing", Status.SKIP, "--no-broker: needs the account's equity"),
        ]
    if not settings.broker_configured:
        return [
            Check("account", Status.SKIP, "no credentials — see the `credentials` line above"),
            Check("sizing", Status.SKIP, "no credentials"),
        ]

    # Imported here so `--no-broker` needs neither the dependency graph nor a
    # network stack to run at all.
    from atp_core.brokers.alpaca import AlpacaBroker

    broker = AlpacaBroker(settings)
    try:
        account = await broker.get_account()
    except ATPError as exc:
        return [
            Check(
                "account",
                Status.FAIL,
                f"could not read the account: {exc}",
                fix="check ALPACA_API_KEY/SECRET are the *paper* pair",
            ),
            Check("sizing", Status.SKIP, "the account could not be read"),
        ]
    finally:
        await broker.aclose()

    checks = [
        preflight.check_account(
            trading_blocked=account.trading_blocked,
            is_pattern_day_trader=account.is_pattern_day_trader,
            equity=account.equity,
            buying_power=account.buying_power,
            is_paper_host="paper" in settings.broker_base_url,
        )
    ]
    checks.append(await _sizing_check(settings, symbols, timeframe, equity=account.equity))
    return checks


async def _sizing_check(
    settings: Settings, symbols: list[str], timeframe: Timeframe, *, equity: Decimal
) -> Check:
    """Price the first entry the way the router will, and see if it clears the cap.

    The stop is derived exactly as `StrategyRunner._with_stop` derives it — same
    `StopManager`, same config, same ATR — because a prediction computed any
    other way is a prediction about a different platform.
    """
    if not symbols:
        return Check("sizing", Status.SKIP, "no symbols to price against")

    symbol = symbols[0]
    engine = create_engine(settings.database_url)
    try:
        bars_repo = PostgresBarRepository(create_session_factory(engine))
        series = await bars_repo.get_last_n_bars(symbol, timeframe, settings.worker_stop_period + 1)
    except Exception as exc:
        return Check("sizing", Status.SKIP, f"database unreachable ({_why(exc)})")
    finally:
        await engine.dispose()

    if not series:
        return Check("sizing", Status.SKIP, f"no stored bars for {symbol} to price against")

    price = series[-1].close
    stop_price = _derived_stop(settings, series, price)
    return preflight.check_sizing_is_reachable(
        settings, settings.risk, equity=equity, price=price, stop_price=stop_price
    )


def _derived_stop(settings: Settings, series: list, price: Decimal) -> Decimal | None:
    from atp_core.domain import Side

    try:
        config = trading.resolve_stop_config(settings)
        atr = dispatch.compute("atr", series, config.period)
        return StopManager().initial_stop(
            price, Side.BUY, config, None if atr is None else Decimal(str(atr))
        )
    except (ATPError, ValueError):
        # No stop derivable — which `check_sizing_is_reachable` reports as the
        # refusal `risk_pct` will produce, rather than swallowing here.
        return None


# ── rendering ───────────────────────────────────────────────────────────────


def _render(report: Preflight) -> None:
    print("\nPaper-run preflight — docs/FIRST_PAPER_RUN.md\n")
    for check in report.checks:
        print(f"  [{MARK[check.status]}] {check.name:<16} {check.detail}")
        if check.fix and check.status in (Status.FAIL, Status.WARN, Status.SKIP):
            print(f"         {'':16} → {check.fix}")
        if check.source and check.status is not Status.PASS:
            print(f"         {'':16}   ({check.source})")

    print()
    if report.ready and report.skipped:
        # Never "READY" while something was not looked at. The whole reason
        # SKIP is a separate status is that an operator reading a green
        # headline over five unrun checks is the failure this tool is for.
        names = ", ".join(c.name for c in report.skipped)
        print(f"NO FAILURES — but {len(report.skipped)} check(s) did not run: {names}")
        print("Bring the stack up and supply credentials, then run this again.")
    elif report.ready:
        print(f"READY — every check ran, {len(report.warnings)} warning(s).")
        print("Layer 8 is outside this repo and nothing here can check it: set position")
        print("and loss limits in Alpaca's own controls too (docs/SAFETY.md).")
    else:
        names = ", ".join(c.name for c in report.failures)
        print(f"NOT READY — {len(report.failures)} check(s) failed: {names}")
        print("Each one costs a week of calendar time to discover the other way.")
    print()


def _jsonable(report: Preflight) -> dict[str, object]:
    return {
        "ready": report.ready,
        "checks": [
            {
                "name": c.name,
                "status": c.status.value,
                "detail": c.detail,
                "fix": c.fix,
                "source": c.source,
            }
            for c in report.checks
        ],
    }


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
