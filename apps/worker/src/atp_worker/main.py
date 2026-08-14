"""Worker entry point.

Supervises three concurrent responsibilities:

    StreamIngestor   one market-data connection, fanned out via Redis
    StrategyRunner   one per active strategy
    Scheduler        backfills, reconciliation, EOD reports

They run as asyncio tasks under one supervisor. If any dies unexpectedly, the
supervisor engages the kill switch before exiting — a worker that half-runs is
more dangerous than one that is plainly down, because monitoring still sees a
live process while positions go unmanaged.
"""

from __future__ import annotations

import asyncio
import signal

from atp_core.config import get_settings
from atp_core.logging import configure as configure_logging
from atp_core.logging import get_logger

log = get_logger(__name__)


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

    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop_event.set)

    raise NotImplementedError(
        "Wire up: StreamIngestor, one StrategyRunner per active strategy, and "
        "the Scheduler. Await stop_event; on shutdown cancel tasks, flush state, "
        "and leave broker-side stops in place."
    )


if __name__ == "__main__":
    asyncio.run(main())
