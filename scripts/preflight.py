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
from atp_core.persistence.db import create_engine, create_session_factory, is_auth_failure
from atp_core.persistence.quotes import RedisQuoteCache
from atp_core.persistence.redis_client import close_redis, create_redis, create_sync_redis
from atp_core.persistence.worker_config import PostgresWorkerConfigRepository
from atp_core.risk.killswitch import RedisKillSwitch
from atp_core.risk.stops import StopManager
from atp_core.strategy import registry
from atp_core.worker.config import DEFAULT_WORKER_CONFIG, WorkerConfig
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
        help="override the watchlist (default: the one saved on the Config tab)",
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

    # The configuration a worker would boot with, read from the same row it
    # reads. An unreachable database is reported as itself rather than crashing
    # the one script somebody runs *because* nothing is working.
    try:
        config = await _stored_config(settings)
    except Exception as exc:
        print(f"cannot read the worker configuration: {_why(exc)}")
        print("run `make up && make migrate`, then try again")
        return 1

    override = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    if override:
        config = config.with_symbols(override)
    timeframe = _timeframe(args.timeframe)

    checks: list[Check] = [
        preflight.check_run_mode(settings),
        preflight.check_credentials(settings),
        preflight.check_locks(trading.decide(settings, config)),
        preflight.check_strategy(config),
        preflight.check_stop_config(config),
        preflight.check_alert_transport(settings),
        preflight.check_metrics_token(settings),
    ]
    checks.extend(await _local_checks(settings, config, timeframe))
    checks.extend(await _venue_checks(settings, config, timeframe, skip=args.no_broker))

    report = Preflight(checks)
    if args.json:
        print(json.dumps(_jsonable(report), indent=2))
    else:
        _render(report)
    return report.exit_code()


def _why(exc: Exception) -> str:
    """The driver exception's type name, and deliberately nothing else.

    A library's error text can embed a `repr` of whatever it was handed — and
    the thing handed to a database driver here is `Settings`, which carries
    every credential this platform holds. Rendering `str(exc)` into a line an
    operator pastes into an issue is how rule §1.6 gets broken by accident
    rather than by intent. `ConnectionRefusedError` plus the `fix` line is what
    anyone acts on regardless; the traceback is still there for whoever needs
    more, where it belongs.
    """
    # `DatabaseUnavailableError` carries the driver's class name as `cause_type`
    # precisely so a caller need not walk `__cause__` for it. Without this the
    # line reads "database unreachable (DatabaseUnavailableError)" — the same
    # word twice, and `InvalidPasswordError`, which errors.py rightly calls most
    # of the diagnosis, dropped on the floor.
    return getattr(exc, "cause_type", None) or type(exc).__name__


#: What to do about a database that answered and refused the credentials. Not
#: `make up`, which starts a stack that is already up, and emphatically not
#: `make migrate`, which fails with this same error against this same password.
#: `check-env` catches the half that is decidable from `.env` alone — a password
#: that cannot survive being interpolated into a DSN — and the runbook section
#: on the `source` line carries the other half, a password rotated against a
#: volume that still holds the old one, which nothing static can see.
DB_AUTH_FIX = "make check-env"


def _database_check(name: str, exc: Exception, fix: str) -> Check:
    """An unreachable database, as one of the two verdicts it can deserve.

    SKIP is the honest answer for a database that is not up yet, and it does not
    fail the command — an operator bringing the stack up a piece at a time runs
    this against a half-started machine on purpose.

    **A refused password is not that**, and reporting it as a skip was wrong in
    the direction this whole tool exists to prevent. The stack is up; the fault
    is a configuration disagreement that no amount of waiting or restarting
    resolves; and the week the operator is about to spend would be spent against
    a platform that cannot persist a bar, an order or a fill. `make preflight`
    exited 0 on it, which is a go-live signal for a run that could only produce
    the same silence a correct run produces (see this module's header on why
    that is the expensive failure here).
    """
    if is_auth_failure(exc):
        return Check(
            name,
            Status.FAIL,
            f"database refused the credentials ({_why(exc)}) — it is up, and it said no",
            fix=DB_AUTH_FIX,
            source='docs/RUNBOOK.md, "password authentication failed"',
        )
    return Check(name, Status.SKIP, f"database unreachable ({_why(exc)})", fix=fix)


def _timeframe(raw: str) -> Timeframe:
    try:
        return Timeframe(raw)
    except ValueError:
        valid = ", ".join(t.value for t in Timeframe)
        raise SystemExit(f"--timeframe must be one of: {valid}") from None


# ── local state ─────────────────────────────────────────────────────────────


async def _local_checks(
    settings: Settings, config: WorkerConfig, timeframe: Timeframe
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

    required = _warmup_bars(config)
    engine = create_engine(settings.database_url)
    try:
        bars = PostgresBarRepository(create_session_factory(engine))
        for symbol in config.symbols:
            checks.append(await _history_check(bars, symbol, timeframe, required))
    except Exception as exc:
        checks.append(_database_check("history", exc, fix="make up && make migrate"))
    finally:
        await engine.dispose()

    try:
        redis = create_redis(settings.redis_url)
        quotes = RedisQuoteCache(redis)
        now = datetime.now(UTC)
        for symbol in config.symbols:
            quote = await quotes.get_quote(symbol)
            age = None if quote is None else (now - quote.ts).total_seconds()
            checks.append(
                preflight.check_quote_freshness(
                    # The budget comes off the config this worker would boot
                    # with, not off `Settings` — the ceilings are columns on
                    # that row since ADR 0025, and preflight's whole job is to
                    # predict what the *next* start will do.
                    symbol,
                    age_seconds=age,
                    budget=config.risk.max_quote_age_seconds,
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


async def _stored_config(settings: Settings) -> WorkerConfig:
    """What the worker would load, or the defaults when nothing is saved.

    Read here rather than from `Settings` because that is where it lives now:
    the ten trading parameters are a row the dashboard writes, so a preflight
    reading environment variables would be checking a configuration no worker
    is going to run.
    """
    engine = create_engine(settings.database_url)
    try:
        stored = await PostgresWorkerConfigRepository(create_session_factory(engine)).load()
    finally:
        await engine.dispose()
    return DEFAULT_WORKER_CONFIG if stored is None else stored.config


def _warmup_bars(config: WorkerConfig) -> int:
    """What the configured strategy needs before it will decide anything.

    Zero when the strategy cannot be constructed — `check_strategy` has already
    said so in its own line, and raising a second time here would report one
    misconfiguration as two.
    """
    if not config.strategy:
        return 0
    try:
        strategy = registry.get(config.strategy)(dict(config.strategy_params) or None)
    except (ATPError, TypeError, ValueError):
        return 0
    return strategy.warmup_bars


# ── the venue ───────────────────────────────────────────────────────────────


async def _venue_checks(
    settings: Settings, config: WorkerConfig, timeframe: Timeframe, *, skip: bool
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
    checks.append(await _sizing_check(settings, config, timeframe, equity=account.equity))
    return checks


async def _sizing_check(
    settings: Settings, config: WorkerConfig, timeframe: Timeframe, *, equity: Decimal
) -> Check:
    """Price the first entry the way the router will, and see if it clears the cap.

    The stop is derived exactly as `StrategyRunner._with_stop` derives it — same
    `StopManager`, same config, same ATR — because a prediction computed any
    other way is a prediction about a different platform.
    """
    if not config.symbols:
        return Check("sizing", Status.SKIP, "no symbols to price against")

    symbol = config.symbols[0]
    engine = create_engine(settings.database_url)
    try:
        bars_repo = PostgresBarRepository(create_session_factory(engine))
        series = await bars_repo.get_last_n_bars(symbol, timeframe, config.stop_period + 1)
    except Exception as exc:
        return _database_check("sizing", exc, fix="make up && make migrate")
    finally:
        await engine.dispose()

    if not series:
        return Check("sizing", Status.SKIP, f"no stored bars for {symbol} to price against")

    price = series[-1].close
    stop_price = _derived_stop(config, series, price)
    return preflight.check_sizing_is_reachable(
        config, config.risk, equity=equity, price=price, stop_price=stop_price
    )


def _derived_stop(config: WorkerConfig, series: list, price: Decimal) -> Decimal | None:
    from atp_core.domain import Side

    try:
        stop = trading.resolve_stop_config(config)
        atr = dispatch.compute("atr", series, stop.period)
        return StopManager().initial_stop(
            price, Side.BUY, stop, None if atr is None else Decimal(str(atr))
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
