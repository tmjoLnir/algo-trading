"""Whether this worker trades, and what it builds when it does.

Split from `main.py` because the *decision* is the part worth testing on its
own. "Does this configuration place orders?" has three locks in front of it and
one of them guards real money; a reader should be able to find that answer in
one function rather than infer it from a wiring block.

The three locks, in the order they are checked:

1. `WORKER_STRATEGY` names a strategy. Empty means no orders — the same posture
   as an empty `WORKER_SYMBOLS`. A worker that starts trading because it was
   deployed, rather than because somebody chose to, is the accident this
   prevents.
2. `ATP_RUN_MODE` / `ATP_ALLOW_LIVE_TRADING` — rule §1.8, enforced in
   `Settings` itself, which refuses to construct a live configuration without
   both.
3. `WORKER_ALLOW_LIVE_ORDERS` — live only. Locks 1 and 2 say "this process may
   trade real money"; this says "this unattended loop may place the orders".
   Different decisions, made by different people at different times.

A watchlist is a fourth requirement and deliberately not called a lock: it is
not a safety control, it is the data the strategy needs. Trading without one
would mean a strategy deciding on a repository that nothing is updating.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from atp_core.domain import Portfolio, RunMode, StopType, Timeframe
from atp_core.errors import ConfigError
from atp_core.execution.reconciliation import Reconciler
from atp_core.execution.router import OrderRouter
from atp_core.logging import get_logger
from atp_core.risk.engine import RiskEngine, default_rules
from atp_core.risk.stops import StopConfig, StopManager
from atp_core.strategy import examples as _examples  # noqa: F401 — populates the registry
from atp_core.strategy import registry
from atp_core.strategy.rules import PositionSizeSpec
from atp_worker.runner import StrategyRunner

if TYPE_CHECKING:
    from collections.abc import Callable
    from datetime import datetime

    from atp_core.brokers.alpaca import AlpacaBroker
    from atp_core.clock import Clock, TradingCalendar
    from atp_core.config import Settings
    from atp_core.data.ports import BarRepository, QuoteCache
    from atp_core.risk.killswitch import KillSwitch

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
    #: unset `WORKER_STRATEGY` is a choice, a live strategy without its third
    #: lock is a thwarted intention and belongs at CRITICAL.
    blocked: bool = False


def decide(settings: Settings, symbols: list[str]) -> TradingDecision:
    """Read the locks. Pure — no adapters, no I/O, no side effects."""
    if not settings.worker_strategy:
        return TradingDecision(
            enabled=False,
            reason="WORKER_STRATEGY is unset — this worker places no orders",
        )

    if settings.run_mode is RunMode.BACKTEST:
        return TradingDecision(
            enabled=False,
            reason=(
                f"WORKER_STRATEGY={settings.worker_strategy} is set but ATP_RUN_MODE=backtest; "
                "a backtest has no venue to trade against — run scripts/run_backtest.py"
            ),
            blocked=True,
        )

    if not symbols:
        return TradingDecision(
            enabled=False,
            reason=(
                f"WORKER_STRATEGY={settings.worker_strategy} is set but WORKER_SYMBOLS is empty; "
                "a strategy with no watchlist would decide on data nothing is updating"
            ),
            blocked=True,
        )

    if settings.is_live and not settings.worker_allow_live_orders:
        return TradingDecision(
            enabled=False,
            reason=(
                "live mode is enabled but WORKER_ALLOW_LIVE_ORDERS is false — this worker "
                "will not place real orders. Set it only when you intend an unattended loop "
                "to trade real money."
            ),
            blocked=True,
        )

    venue = "REAL MONEY" if settings.is_live else "paper money"
    return TradingDecision(
        enabled=True,
        reason=f"trading {settings.worker_strategy} with {venue} against {settings.broker_base_url}",
    )


def build_runner(
    settings: Settings,
    symbols: list[str],
    *,
    broker: AlpacaBroker,
    kill_switch: KillSwitch,
    bar_repo: BarRepository,
    quote_cache: QuoteCache,
    clock: Clock,
    calendar: TradingCalendar,
    last_tick_at: Callable[[str], datetime | None],
) -> tuple[StrategyRunner, Reconciler]:
    """Assemble the live loop from settings.

    The reconciler comes back too because the trade-updates consumer needs it:
    `TradeUpdatesReconnected` is a demand for a REST catch-up, and the thing
    that performs it is the same object the runner warms up with.
    """
    strategy_cls = registry.get(settings.worker_strategy)
    strategy = strategy_cls(_strategy_params(settings))

    stop_manager = StopManager()
    risk_engine = RiskEngine(
        settings.risk, default_rules(kill_switch, clock, calendar, last_tick_at)
    )
    router = OrderRouter(broker, risk_engine, stop_manager, clock, kill_switch=kill_switch)
    reconciler = Reconciler(broker, kill_switch, clock)

    runner = StrategyRunner(
        strategy=strategy,
        symbols=symbols,
        router=router,
        stop_manager=stop_manager,
        kill_switch=kill_switch,
        bar_repo=bar_repo,
        quote_cache=quote_cache,
        clock=clock,
        calendar=calendar,
        reconciler=reconciler,
        sizing=PositionSizeSpec(
            type=settings.worker_sizing_method, value=settings.worker_sizing_value
        ),
        stop_config=_stop_config(settings),
        timeframe=Timeframe.D1,
        tick_interval_seconds=float(settings.engine_tick_interval_seconds),
    )
    return runner, reconciler


def _strategy_params(settings: Settings) -> dict[str, Any] | None:
    """Parse `WORKER_STRATEGY_PARAMS`, refusing anything malformed.

    A typo here would otherwise start a strategy on its defaults while the
    operator believes it is running the parameters they set — which is the
    quietest possible way to trade the wrong thing.
    """
    raw = settings.worker_strategy_params.strip()
    if not raw:
        return None
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ConfigError(f"WORKER_STRATEGY_PARAMS is not valid JSON: {exc}") from exc
    if not isinstance(parsed, dict):
        raise ConfigError(
            f"WORKER_STRATEGY_PARAMS must be a JSON object, got {type(parsed).__name__}"
        )
    return parsed


def _stop_config(settings: Settings) -> StopConfig:
    """The protective stop every entry is armed with.

    `multiplier` and `value` are populated from the same setting because the
    two families of stop read it differently — an ATR stop is a multiple, a
    fixed-percentage stop is a fraction — and giving each its own environment
    variable would let an operator set the one the configured type ignores.
    """
    stop_type = StopType(settings.worker_stop_type)
    multiplier_types = (StopType.ATR, StopType.CHANDELIER)
    return StopConfig(
        stop_type=stop_type,
        value=None if stop_type in multiplier_types else settings.worker_stop_multiplier,
        multiplier=settings.worker_stop_multiplier if stop_type in multiplier_types else None,
        period=settings.worker_stop_period,
    )


async def adopt_broker_state(reconciler: Reconciler, clock: Clock) -> Portfolio:
    """The portfolio this worker starts from: the broker's, wholesale.

    docs/RUNBOOK.md says a restart's `warmup()` "will reconcile and adopt open
    positions", and docs/SAFETY.md's checklist requires a worker restarted with
    open positions to adopt them rather than double them. This is where that
    happens, and it is deliberately *here* rather than inside `warmup`.

    The distinction is between two situations that look identical to a
    reconciler and are not:

    - **Boot.** We hold no book at all. There is nothing for the broker's to
      disagree with, so adopting is the only sensible move — and without it a
      worker restarted while holding positions would refuse to start forever.
    - **Drift.** We had a book and it stopped matching. That is a mismatch, it
      halts, and a human decides. `StrategyRunner.warmup` keeps that behaviour
      for every reconciliation after this one.

    There is no persistence yet, so every boot takes this path. That is worth
    knowing rather than glossing: it means a bug that lost our state would be
    indistinguishable from a clean restart, which is one of the reasons the
    order and position repositories matter.
    """
    portfolio = Portfolio(cash=Decimal(0), starting_equity=Decimal(0))
    await reconciler.adopt_broker_state(portfolio)
    portfolio.starting_equity = portfolio.equity

    log.warning(
        "worker.adopted_broker_state",
        positions=sorted(p.symbol for p in portfolio.open_positions),
        cash=str(portfolio.cash),
        msg="starting from the broker's book — this worker holds no state of its own",
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
