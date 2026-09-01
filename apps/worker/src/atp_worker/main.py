"""Worker entry point.

Supervises three concurrent responsibilities:

    StreamIngestor   one market-data connection, fanned out via Redis
    StrategyRunner   one per active strategy
    Scheduler        backfills, reconciliation, EOD reports

They run as asyncio tasks under one supervisor. If any dies unexpectedly, the
supervisor engages the kill switch before exiting — a worker that half-runs is
more dangerous than one that is plainly down, because monitoring still sees a
live process while positions go unmanaged.

**All three can run now**, but the third is opt-in and stays off unless somebody
turns it on. What to trade comes from the `worker_config` row the dashboard
edits (`atp_core.worker`): no strategy is configured by default, and live
additionally needs `allow_live_orders`, which the API arms only against the
operator's password. The locks and the reasoning behind them are in
`trading.py`, and whichever way they land is stated in the startup log rather
than left for a reader to infer from a quiet process — because "the worker is
up" and "the worker is trading" must never be the same observation.

**The configuration is read once, here, and then published.** A running worker
does not watch the row: rebuilding a strategy, a stop manager and a market-data
subscription underneath a half-finished evaluation is not a thing to do while
holding positions. So an edit takes effect at the next start, and this process
publishes what it actually loaded — `WorkerStatusStore` — so the dashboard can
show the difference rather than implying a saved change is in force.
"""

from __future__ import annotations

import asyncio
import signal
from contextlib import AsyncExitStack
from typing import TYPE_CHECKING, Any

from atp_core import __version__
from atp_core.alerts import build_alert_sink
from atp_core.brokers.alpaca import AlpacaBroker
from atp_core.clock import SystemClock, TradingCalendar
from atp_core.config import get_settings
from atp_core.data.providers.alpaca import AlpacaHistoricalProvider, AlpacaRealtimeFeed
from atp_core.data.stream import StalenessMonitor, StreamIngestor
from atp_core.errors import ATPError
from atp_core.logging import configure as configure_logging
from atp_core.logging import get_logger
from atp_core.metrics import build_info
from atp_core.persistence.bars import PostgresBarRepository
from atp_core.persistence.dashboard import RedisSnapshotStore
from atp_core.persistence.db import create_engine, create_session_factory
from atp_core.persistence.events import RedisEventPublisher
from atp_core.persistence.orders import PostgresOrderRepository
from atp_core.persistence.positions import PostgresPortfolioRepository
from atp_core.persistence.quotes import RedisQuoteCache
from atp_core.persistence.redis_client import close_redis, create_redis, create_sync_redis
from atp_core.persistence.signals import PostgresSignalRepository
from atp_core.persistence.strategies import PostgresStrategyRepository
from atp_core.persistence.worker_config import PostgresWorkerConfigRepository
from atp_core.persistence.worker_status import RedisWorkerStatusStore
from atp_core.risk.killswitch import HaltReason, HaltScope, RedisKillSwitch
from atp_core.worker.config import DEFAULT_WORKER_CONFIG
from atp_core.worker.ports import RunningWorkerConfig
from atp_worker import trading
from atp_worker.metrics_server import start_metrics_server
from atp_worker.scheduler import SessionJobs, build_schedule, run_scheduler

if TYPE_CHECKING:
    from collections.abc import Callable, Coroutine, Mapping

    from atp_core.config import Settings
    from atp_core.risk.killswitch import KillSwitch

    #: A thing the supervisor runs. A factory rather than a coroutine because a
    #: coroutine is single-use, and the supervisor is the only thing entitled to
    #: decide when — or whether — each one starts.
    Responsibility = Callable[[], Coroutine[Any, Any, None]]

log = get_logger(__name__)

#: Who a halt engaged from here is attributed to. Distinct from the ingestor's
#: and the watchdog's actors: "a supervised task died" is a different incident
#: from "the feed gave up", and the halt record should not make an operator
#: guess which one they are looking at.
HALT_ACTOR = "worker_supervisor"


class WorkerError(ATPError):
    """A supervised responsibility ended when it should have run forever."""


async def main() -> None:
    settings = get_settings()
    configure_logging(settings.log_level, settings.log_format)

    if settings.is_live:
        log.critical(
            "worker.live_trading_enabled",
            broker_url=settings.broker_base_url,
            msg="REAL MONEY IS AT RISK — orders placed by this process are real",
        )
    else:
        log.info("worker.starting", run_mode=settings.run_mode)

    # Which build, in which mode — the labels are the payload. Recorded in
    # `main` rather than in `run` so that a test driving `run` directly does not
    # write into the process-wide registry as a side effect.
    build_info(__version__, settings.run_mode.value)

    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop_event.set)

    await run(settings, stop_event)


async def run(settings: Settings, stop_event: asyncio.Event) -> None:
    """Wire the adapters, supervise the responsibilities, tear everything down.

    Split from `main` so that the wiring is reachable without installing signal
    handlers, which only an entry point may do — `add_signal_handler` is
    process-global and belongs to whoever owns the process.

    Everything opened here is registered for teardown as it is created rather
    than in a trailing `finally`. The ordering that matters is the WebSocket's:
    Alpaca allows one connection per key, so a socket left open by a crashing
    worker is refused to the worker that replaces it. `StreamIngestor.run`
    closes its own stream on the way out; the stack below is what covers the
    paths that never reached it.
    """
    async with AsyncExitStack() as stack:
        # Registered first, so it is torn down last: the shutdown path is
        # exactly when somebody wants to know what this process was doing, and a
        # listener closed before the ingestor stops reporting nothing about the
        # stop itself.
        server = start_metrics_server(settings)
        if server is not None:
            stack.callback(server.close)

        redis = create_redis(settings.redis_url)
        stack.push_async_callback(close_redis, redis)

        # A second client, synchronous, for the kill switch alone — see
        # `create_sync_redis`. Both point at the same server.
        sync_redis = create_sync_redis(settings.redis_url)
        stack.callback(sync_redis.close)

        engine = create_engine(settings.database_url)
        stack.push_async_callback(engine.dispose)

        provider = AlpacaHistoricalProvider(settings)
        stack.push_async_callback(provider.aclose)

        # Every automated halt this worker can engage — a lost feed, a
        # reconciliation mismatch, a supervised task dying — goes through this
        # object, so binding the sink here is what makes all three reach a
        # phone rather than only a log file (docs/SAFETY.md).
        kill_switch = RedisKillSwitch(sync_redis, alerts=build_alert_sink(settings))
        session_factory = create_session_factory(engine)

        # What to trade, from the row the dashboard writes.
        #
        # **A read failure raises rather than falling back to the defaults**,
        # and the consequence is a process that will not start while the
        # database is unreachable. That is deliberate and it is the safer of the
        # two failures: defaulting would mean a worker that quietly ingests
        # nothing and trades nothing because Postgres blinked, and stays that
        # way until somebody notices — the same class of mistake as adopting the
        # broker's book over our own (`restore_or_adopt`). Refusing is
        # self-healing under `restart: unless-stopped`; a silent default is not.
        try:
            stored = await PostgresWorkerConfigRepository(session_factory).load()
        except Exception as exc:
            # Said in words before the traceback, because "the worker will not
            # start" and "the worker cannot read its configuration" are the same
            # incident and only the second one tells an operator where to look.
            log.critical(
                "worker.config_unreadable",
                error=str(exc),
                msg="cannot read the worker configuration — refusing to start on defaults",
                hint="check the database is up (`docker compose ps`) and migrated",
            )
            raise
        config = stored.config if stored is not None else DEFAULT_WORKER_CONFIG
        revision = stored.revision if stored is not None else 0
        symbols = list(config.symbols)
        if stored is None:
            log.warning(
                "worker.no_stored_config",
                msg="nothing has been saved on the Worker tab — running the defaults",
                hint="open the dashboard's Worker tab to set a watchlist and a strategy",
            )
        # What it loaded, in full. `.env` used to be readable on the host, so an
        # operator could always see what a worker was configured with; now that
        # it is a row, this line is that.
        log.info(
            "worker.config_loaded",
            revision=revision,
            symbols=symbols,
            strategy=config.strategy or None,
            strategy_params=config.strategy_params or None,
            sizing=f"{config.sizing_method} {config.sizing_value}",
            stop=f"{config.stop_type} x{config.stop_multiplier} period={config.stop_period}",
            max_silence_seconds=config.max_silence_seconds,
            allow_live_orders=config.allow_live_orders,
        )
        bar_repo = PostgresBarRepository(session_factory)
        portfolio_repo = PostgresPortfolioRepository(session_factory)
        quote_cache = RedisQuoteCache(redis)
        publisher = RedisEventPublisher(redis)
        snapshot_store = RedisSnapshotStore(redis)

        responsibilities: dict[str, Responsibility] = {}
        #: Bound after the trading decision below, because what the scheduler
        #: runs depends on it: reconciliation needs the runner's live book, and
        #: a worker that is not trading has none.
        session_jobs: SessionJobs | None = None

        if symbols:
            ingestor = StreamIngestor(
                AlpacaRealtimeFeed(settings),
                quote_cache,
                bar_repo,
                provider,
                publisher=publisher,
                kill_switch=kill_switch,
            )
            monitor = StalenessMonitor(config.max_silence_seconds, kill_switch=kill_switch)
            responsibilities["ingestor"] = lambda: ingestor.run(symbols)
            responsibilities["staleness_monitor"] = lambda: monitor.watch(ingestor)
        else:
            # Not fatal and not a halt: nothing is trading, and a stale quote
            # cache already refuses orders through `StaleDataRule`. But it is a
            # misconfiguration, and a worker that silently ingests nothing is
            # the thing an operator would most like to have been told.
            log.error(
                "worker.no_watchlist",
                msg="the watchlist is empty — ingesting no market data",
                hint="add symbols on the dashboard's Worker tab, then restart this worker",
            )

        decision = trading.decide(settings, config)
        if decision.enabled:
            # `ingestor` is bound whenever `symbols` is non-empty, which
            # `trading.decide` has already made a condition of trading.
            broker = AlpacaBroker(settings)
            stack.push_async_callback(broker.aclose)
            clock = SystemClock()

            runner, reconciler = trading.build_runner(
                settings,
                config,
                broker=broker,
                kill_switch=kill_switch,
                bar_repo=bar_repo,
                quote_cache=quote_cache,
                clock=clock,
                calendar=TradingCalendar(),
                last_tick_at=ingestor.last_tick_at,
                order_repo=PostgresOrderRepository(session_factory),
                portfolio_repo=portfolio_repo,
                strategy_repo=PostgresStrategyRepository(session_factory, clock),
                signal_repo=PostgresSignalRepository(session_factory),
                snapshot_store=snapshot_store,
                publisher=publisher,
            )
            portfolio = await trading.restore_or_adopt(
                reconciler, portfolio_repo, settings.run_mode
            )

            responsibilities["strategy_runner"] = lambda: runner.run(portfolio)
            responsibilities["trade_updates"] = lambda: trading.consume_trade_updates(
                broker, runner, reconciler, portfolio
            )
            # Positions are left open with their broker-side stops intact, which
            # is what makes a deploy a restart rather than a liquidation.
            stack.push_async_callback(runner.shutdown)

            # `runner.open_orders` rather than a snapshot of it: the five-minute
            # check must ask what is working *now*, or every order placed since
            # the wiring would reconcile as an orphan.
            session_jobs = SessionJobs(
                reconciler=reconciler,
                portfolio=portfolio,
                open_orders=lambda: runner.open_orders,
            )

        responsibilities["scheduler"] = lambda: run_scheduler(schedule=build_schedule(session_jobs))

        if decision.enabled and settings.is_live:
            log.critical("worker.trading_live", msg=decision.reason)
        elif decision.enabled:
            log.warning("worker.trading", msg=decision.reason)
        elif decision.blocked:
            # Somebody asked for trading and a lock refused. Louder than an
            # unset strategy, which is a choice rather than a thwarted one.
            log.critical("worker.trading_blocked", msg=decision.reason)
        else:
            log.info("worker.not_trading", msg=decision.reason)

        # Published before the responsibilities start, so a worker that dies
        # during warm-up has still said what it was trying to run — which is
        # exactly the case where the settings screen is the thing being read.
        #
        # Failure to publish is logged and swallowed. This decorates a settings
        # screen; it does not decide anything. A worker that refused to start
        # because a status blob would not write would be trading stopped by its
        # own telemetry, which is the wrong way round.
        try:
            await RedisWorkerStatusStore(redis).put(
                settings.run_mode,
                RunningWorkerConfig(
                    config=config,
                    revision=revision,
                    started_at=SystemClock().now(),
                    trading=decision.enabled,
                    reason=decision.reason,
                ),
            )
        except Exception as exc:
            log.warning(
                "worker.status_not_published",
                error=str(exc),
                msg="the Worker tab will report no worker running; trading is unaffected",
            )

        log.info(
            "worker.ready",
            run_mode=settings.run_mode,
            symbols=symbols,
            config_revision=revision,
            responsibilities=sorted(responsibilities),
            trading=decision.enabled,
            msg=decision.reason,
        )

        await supervise(responsibilities, stop_event=stop_event, kill_switch=kill_switch)


async def supervise(
    responsibilities: Mapping[str, Responsibility],
    *,
    stop_event: asyncio.Event,
    kill_switch: KillSwitch | None = None,
) -> None:
    """Run every responsibility until one ends or a signal arrives.

    Two outcomes, and the difference between them is the whole point of this
    function:

    - **A signal.** An ordinary shutdown. Tasks are cancelled and nothing is
      halted — leaving a halt behind that a human has to clear would make every
      routine restart a manual operation, which is the reasoning
      `StreamIngestor.run` already applies to its own cancellation.
    - **A responsibility ended.** Never routine, whether it raised or returned:
      each of these is written to run until cancelled. Trading is halted before
      this process exits, because the dangerous state is not a dead worker — it
      is a worker whose ingestor died while the rest of the system carries on
      pricing against the last quote it happened to write.

    Halting here is often the *second* halt of an incident: a feed the adapter
    gave up on has already engaged `DATA_FEED_LOST` on its way out. That is
    intended. `engage` is idempotent and keeps the original record, so the
    earlier, more specific reason survives and this one adds nothing but
    certainty that something halted.
    """
    tasks: dict[asyncio.Task[None], str] = {
        asyncio.create_task(start(), name=name): name for name, start in responsibilities.items()
    }
    stopper: asyncio.Task[bool] = asyncio.create_task(stop_event.wait(), name="stop_event")

    waiting: set[asyncio.Task[Any]] = {*tasks, stopper}
    done, pending = await asyncio.wait(waiting, return_when=asyncio.FIRST_COMPLETED)

    for task in pending:
        task.cancel()
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)

    ended = [task for task in done if task is not stopper]
    if not ended:
        log.info(
            "worker.stopped",
            cancelled=sorted(tasks.values()),
            msg="signal received — shut down cleanly, nothing halted",
        )
        return

    first = ended[0]
    name = tasks[first]
    error = None if first.cancelled() else first.exception()
    detail = (
        f"worker responsibility {name!r} raised {type(error).__name__}: {error}"
        if error is not None
        else f"worker responsibility {name!r} finished, but it should run until cancelled"
    )

    log.critical("worker.responsibility_ended", responsibility=name, detail=detail)
    _halt(kill_switch, detail)

    if error is not None:
        raise error
    raise WorkerError(detail)


def _halt(kill_switch: KillSwitch | None, detail: str) -> None:
    """Stop trading platform-wide, or say loudly that we could not."""
    if kill_switch is None:
        log.critical(
            "worker.halt_unavailable",
            detail=detail,
            msg="no kill switch bound — TRADING IS NOT HALTED",
        )
        return
    kill_switch.engage(
        HaltScope.GLOBAL,
        HaltReason.UNHANDLED_EXCEPTION,
        engaged_by=HALT_ACTOR,
        detail=detail,
    )
    log.critical("worker.halted", detail=detail)


if __name__ == "__main__":
    asyncio.run(main())
