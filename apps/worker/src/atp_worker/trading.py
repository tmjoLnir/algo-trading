"""Whether this worker trades, and what it builds when it does.

Split from `main.py` because the *decision* is the part worth testing on its
own. "Does this configuration place orders?" has three locks in front of it and
one of them guards real money; a reader should be able to find that answer in
one function rather than infer it from a wiring block.

The three locks, in the order they are checked:

1. The configuration names a strategy. Empty means no orders — the same posture
   as an empty watchlist. A worker that starts trading because it was deployed,
   rather than because somebody chose to, is the accident this prevents.
2. `ATP_RUN_MODE` / `ATP_ALLOW_LIVE_TRADING` — rule §1.8, enforced in
   `Settings` itself, which refuses to construct a live configuration without
   both.
3. `allow_live_orders` — live only. Locks 1 and 2 say "this process may trade
   real money"; this says "this unattended loop may place the orders".
   Different decisions, made by different people at different times.

**Locks 1 and 3 moved out of the environment** and into `worker_config`, which
the dashboard edits (`atp_core.worker`). Lock 2 did not and must not: a run mode
editable from a browser would put the whole live ratchet behind one form. What
the move changes here is only where the values come from — the checks, their
order, and the sentence each failure produces are unchanged, because the point
of them was never that they were environment variables.

The two configurations are also the reason `decide` takes a `WorkerConfig`
rather than reading one. A worker reads its configuration once, at start, and
publishes what it read; a decision made against a freshly-loaded row would be a
decision about a configuration this process is not running.

A watchlist is a fourth requirement and deliberately not called a lock: it is
not a safety control, it is the data the strategy needs. Trading without one
would mean a strategy deciding on a repository that nothing is updating.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import TYPE_CHECKING

from atp_core.domain import Portfolio, RunMode, StopType, Timeframe
from atp_core.execution.reconciliation import Reconciler
from atp_core.execution.router import OrderRouter
from atp_core.logging import get_logger
from atp_core.risk.engine import RiskEngine, default_rules
from atp_core.risk.stops import StopConfig, StopManager
from atp_core.strategy import examples as _examples  # noqa: F401 — populates the registry
from atp_core.strategy import registry
from atp_core.strategy.rules import PositionSizeSpec
from atp_core.worker.config import MULTIPLIER_STOPS, WorkerConfig
from atp_worker.runner import StrategyRunner

if TYPE_CHECKING:
    from collections.abc import Callable
    from datetime import datetime

    from atp_core.brokers.alpaca import AlpacaBroker
    from atp_core.clock import Clock, TradingCalendar
    from atp_core.config import Settings
    from atp_core.dashboard.ports import SnapshotStore
    from atp_core.data.ports import BarRepository, EventPublisher, QuoteCache
    from atp_core.execution.ports import OrderRepository, PortfolioRepository
    from atp_core.risk.killswitch import KillSwitch
    from atp_core.strategy.ports import SignalRepository, StrategyRepository

log = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class TradingDecision:
    """Whether this worker places orders, and the reason either way.

    The reason is not decoration. "The worker is up" and "the worker is
    trading" must never be the same observation, so whichever way this lands it
    is stated in the startup log in words an operator can act on.
    """

    enabled: bool
    reason: str
    #: True when the configuration *wanted* to trade and a lock stopped it, as
    #: opposed to nobody having asked. The distinction decides log level: an
    #: unconfigured strategy is a choice, a live strategy without its third
    #: lock is a thwarted intention and belongs at CRITICAL.
    blocked: bool = False


def decide(settings: Settings, config: WorkerConfig) -> TradingDecision:
    """Read the locks. Pure — no adapters, no I/O, no side effects.

    Every reason names the dashboard now rather than an environment variable,
    because that is where the reader has to go to change it. A sentence telling
    an operator to set `WORKER_STRATEGY` would send them to a file nothing reads
    any more, which is worse than saying nothing.
    """
    if not config.strategy:
        return TradingDecision(
            enabled=False,
            reason=(
                "no strategy is configured — this worker places no orders. "
                "Choose one on the dashboard's Worker tab."
            ),
        )

    if settings.run_mode is RunMode.BACKTEST:
        return TradingDecision(
            enabled=False,
            reason=(
                f"strategy {config.strategy} is configured but ATP_RUN_MODE=backtest; "
                "a backtest has no venue to trade against — run scripts/run_backtest.py"
            ),
            blocked=True,
        )

    if not config.symbols:
        return TradingDecision(
            enabled=False,
            reason=(
                f"strategy {config.strategy} is configured but the watchlist is empty; "
                "a strategy with no watchlist would decide on data nothing is updating"
            ),
            blocked=True,
        )

    if not settings.broker_configured:
        return TradingDecision(
            enabled=False,
            reason=(
                f"strategy {config.strategy} is configured but ALPACA_API_KEY is "
                f"empty, and ATP_RUN_MODE={settings.run_mode.value} trades against Alpaca; "
                "there is no venue to send an order to"
            ),
            blocked=True,
        )

    if settings.is_live and not config.allow_live_orders:
        return TradingDecision(
            enabled=False,
            reason=(
                "live mode is enabled but live order placement is not permitted — this worker "
                "will not place real orders. Arm it on the dashboard's Worker tab, which asks "
                "for your password, and only when you intend an unattended loop to trade real "
                "money."
            ),
            blocked=True,
        )

    venue = "REAL MONEY" if settings.is_live else "paper money"
    return TradingDecision(
        enabled=True,
        reason=f"trading {config.strategy} with {venue} against {settings.broker_base_url}",
    )


def build_runner(
    settings: Settings,
    config: WorkerConfig,
    *,
    broker: AlpacaBroker,
    kill_switch: KillSwitch,
    bar_repo: BarRepository,
    quote_cache: QuoteCache,
    clock: Clock,
    calendar: TradingCalendar,
    last_tick_at: Callable[[str], datetime | None],
    order_repo: OrderRepository,
    portfolio_repo: PortfolioRepository,
    strategy_repo: StrategyRepository,
    signal_repo: SignalRepository,
    snapshot_store: SnapshotStore | None = None,
    publisher: EventPublisher | None = None,
) -> tuple[StrategyRunner, Reconciler]:
    """Assemble the live loop from settings.

    The reconciler comes back too because the trade-updates consumer needs it:
    `TradeUpdatesReconnected` is a demand for a REST catch-up, and the thing
    that performs it is the same object the runner warms up with.
    """
    strategy_cls = registry.get(config.strategy)
    strategy = strategy_cls(dict(config.strategy_params) or None)

    stop_manager = StopManager()
    risk_engine = RiskEngine(
        settings.risk, default_rules(kill_switch, clock, calendar, last_tick_at)
    )
    router = OrderRouter(broker, risk_engine, stop_manager, clock, kill_switch=kill_switch)
    reconciler = Reconciler(broker, kill_switch, clock)

    runner = StrategyRunner(
        strategy=strategy,
        symbols=list(config.symbols),
        router=router,
        stop_manager=stop_manager,
        kill_switch=kill_switch,
        bar_repo=bar_repo,
        quote_cache=quote_cache,
        clock=clock,
        calendar=calendar,
        reconciler=reconciler,
        sizing=PositionSizeSpec(type=config.sizing_method, value=config.sizing_value),
        stop_config=resolve_stop_config(config),
        timeframe=Timeframe.D1,
        run_mode=settings.run_mode,
        order_repo=order_repo,
        portfolio_repo=portfolio_repo,
        strategy_repo=strategy_repo,
        signal_repo=signal_repo,
        snapshot_store=snapshot_store,
        publisher=publisher,
        tick_interval_seconds=float(settings.engine_tick_interval_seconds),
    )
    return runner, reconciler


def resolve_stop_config(config: WorkerConfig) -> StopConfig:
    """The protective stop every entry is armed with.

    `multiplier` and `value` are populated from the same field because the two
    families of stop read it differently — an ATR stop is a multiple, a
    fixed-percentage stop is a fraction — and giving each its own would let an
    operator fill in the one the configured type ignores. The dashboard relabels
    the input from `MULTIPLIER_STOPS` for the same reason, so the screen and this
    function cannot disagree about which meaning is in force.
    """
    stop_type = StopType(config.stop_type)
    return StopConfig(
        stop_type=stop_type,
        value=None if config.stop_type in MULTIPLIER_STOPS else config.stop_multiplier,
        multiplier=config.stop_multiplier if config.stop_type in MULTIPLIER_STOPS else None,
        period=config.stop_period,
    )


async def restore_or_adopt(
    reconciler: Reconciler,
    portfolio_repo: PortfolioRepository,
    run_mode: RunMode,
) -> Portfolio:
    """The book this worker starts from: ours if we have one, else the broker's.

    These are two different situations and telling them apart is the whole
    point of the persistence layer:

    - **We have a stored book.** Use it. `StrategyRunner.warmup` then reconciles
      it against the broker and halts on a mismatch — which is a real check,
      because the two views were formed independently. This is the case that
      makes "restarted cleanly" mean something.
    - **There is no stored book at all.** A first-ever boot, or a fresh
      database. Nothing exists to disagree with the broker, so adopt it
      wholesale and say so loudly. `docs/RUNBOOK.md` documents this as the
      restart behaviour and `docs/SAFETY.md`'s checklist requires it — a worker
      that started flat while holding positions would double them.

    Before this existed, *every* boot took the adopt path, which made
    reconciliation across a restart clean by construction and therefore
    worthless as evidence — `docs/FIRST_PAPER_RUN.md` says so in the section on
    what a paper week cannot prove. That section is now narrower: it holds only
    for a first boot.

    A read failure raises rather than falling back to adoption. Adopting
    because the database was briefly unreachable would silently discard our own
    book, which is the one outcome worse than refusing to start.
    """
    stored = await portfolio_repo.latest(run_mode)
    if stored is not None:
        log.info(
            "worker.restored_book",
            positions=sorted(p.symbol for p in stored.open_positions),
            cash=str(stored.cash),
            msg="starting from our own stored book — the broker is about to be asked to agree",
        )
        return stored

    portfolio = Portfolio(cash=Decimal(0), starting_equity=Decimal(0))
    await reconciler.adopt_broker_state(portfolio)
    portfolio.starting_equity = portfolio.equity
    log.warning(
        "worker.adopted_broker_state",
        positions=sorted(p.symbol for p in portfolio.open_positions),
        cash=str(portfolio.cash),
        msg=(
            "no stored book — adopting the broker's. Expected on a first boot; "
            "on any later one it means the snapshot history was lost."
        ),
    )
    return portfolio


async def consume_trade_updates(
    broker: AlpacaBroker,
    runner: StrategyRunner,
    reconciler: Reconciler,
    portfolio: Portfolio,
) -> None:
    """Feed the venue's push stream into the runner. Runs until cancelled.

    Typed against `AlpacaBroker` rather than `BrokerPort` because a pushed
    order stream is a property of a venue and not of brokers in general — the
    port deliberately does not carry it (see `brokers/ports.py`).

    A `TradeUpdatesReconnected` is not an event to log and move past: Alpaca
    does not replay the gap, so a fill that landed while we were disconnected
    exists only at the venue. Reconciling is the catch-up, and it happens
    before the first event of the new connection is handled — which is the
    whole reason the marker is carried *in* the stream rather than shouted from
    a callback.
    """
    from atp_core.brokers.ports import TradeUpdatesReconnected

    async for event in broker.stream_trade_updates():
        if isinstance(event, TradeUpdatesReconnected):
            log.warning(
                "worker.trade_updates_reconnected",
                gap_since=event.gap_since.isoformat(),
                attempts=event.attempts,
                msg="re-reading the book over REST — events during the gap are lost",
            )
            report = await reconciler.reconcile(portfolio, known_orders=runner.open_orders)
            if not report.is_clean:
                # The reconciler has already halted. Say so here too: this is
                # the path where a missed fill turns up, and an operator
                # reading the worker's log should not have to correlate.
                log.critical("worker.book_diverged_after_reconnect", summary=report.summary())
            continue

        await runner.on_fill_event(event, portfolio)
