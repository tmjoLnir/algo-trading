"""The order router — the single path from signal to market (rule §1.5).

Every order in this system goes through `submit()`. Not "most orders". Not
"orders from strategies". Every one, including protective stops, manual
dashboard orders and emergency exits. That is what makes the risk engine a
guarantee instead of a suggestion — one path to audit, one place a limit can be
enforced, one place a bug can hide.

    Signal → size → build Order → RiskEngine.validate → broker.submit_order
                                        │
                                        └── denied → record, emit, stop

Four things in here are worth reading before changing them.

**A refusal is a return value; an indeterminate submit is an exception.** A risk
denial, a venue rejection, a signal with nothing to do — all are ordinary
outcomes that come back as a `SubmitResult` the dashboard can render, because
raising would make one blocked strategy look like a crash. A transport failure
where we cannot establish whether the venue took the order is different in kind:
there may now be a position nobody knows about, and the only safe response is to
stop, not to carry on with a value in hand.

**We never resubmit blind.** `client_order_id` is derived from the decision
(`idempotency.py`), so a retry the *caller* makes reuses the key and the venue
deduplicates. What this module will not do is retry inside a single `submit()`
after a timeout: at that moment the outcome is unknown, and a second send is how
one intended position becomes two.

**Status is moved through `state.transition`, never assigned.** Including on our
own submission path. A bug here looks exactly like a bad broker event, and the
transition table is the thing that catches both.

**Only the stop goes to the venue.** A take-profit is armed on the position for
the engine to watch, not submitted as a second live order. `BrokerPort` has no
bracket, so a stop and a target for the same shares are two independent orders,
and when one fills the other is still working — filling it would not close
anything, it would open a fresh position on the opposite side. The only thing
that could cancel the loser is the fill handler, which is a separate Phase 4
item; shipping the placement without its invoker is shipping the trap before the
guard. The asymmetry is docs/SAFETY.md's own: layer 5 is *broker-side stops*,
and there is no layer for targets.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from decimal import Decimal
from typing import TYPE_CHECKING

from atp_core import metrics
from atp_core.domain import (
    ROUTING,
    SIZING,
    Order,
    OrderRequest,
    OrderStatus,
    OrderType,
    Side,
    SignalAction,
    TimeInForce,
)
from atp_core.errors import BrokerConnectionError, BrokerError, ExecutionError, OrderRejectedError
from atp_core.execution.idempotency import (
    ENTRY,
    EXIT,
    FLATTEN,
    STOP_LOSS,
    client_order_id,
    protective_client_order_id,
)
from atp_core.execution.state import transition
from atp_core.logging import get_logger
from atp_core.risk.engine import RiskDecision
from atp_core.risk.rules import position_size, reference_price
from atp_core.risk.stops import FROM_ENTRY_TYPES

if TYPE_CHECKING:
    from collections.abc import Iterable
    from datetime import datetime

    from atp_core.brokers.ports import BrokerPort
    from atp_core.clock import Clock
    from atp_core.domain import Portfolio, Position, Signal
    from atp_core.risk.engine import RiskEngine
    from atp_core.risk.killswitch import KillSwitch
    from atp_core.risk.stops import StopConfig, StopManager
    from atp_core.strategy.rules import PositionSizeSpec

log = get_logger(__name__)

#: Stages that can refuse before any `RiskRule` runs. They appear in
#: `SubmitResult.decision.rule`, so a human reading a refusal on the dashboard is
#: told which stage refused rather than only that something did.
#:
#: `SIZING` and `ROUTING` are defined in `atp_core.domain.order` and re-exported
#: here, which is where they were declared until the backtest engine began
#: refusing at the same stages: two copies of a `rejected_by` value would drift,
#: and the point of the field is that a backtest refusal and a live one are the
#: same record. `NO_ACTION` stays local — nothing is refused, so it never
#: reaches that column.
NO_ACTION = "no_action"


@dataclass(frozen=True, slots=True)
class SubmitResult:
    order: Order | None
    decision: RiskDecision
    submitted: bool

    @classmethod
    def no_action(cls, reason: str) -> SubmitResult:
        """Nothing to submit, and nothing refused it.

        A HOLD signal, or an exit for a position that is already flat. The
        decision is `approved` on purpose: reporting these as denials would put
        phantom rejections on the dashboard and inflate
        `RunnerStats.orders_rejected_by_risk`, which is the number an operator
        reads to decide whether the risk config is too tight.
        """
        return cls(
            order=None,
            decision=RiskDecision(approved=True, rule=NO_ACTION, reason=reason),
            submitted=False,
        )

    @classmethod
    def refused(cls, stage: str, reason: str, order: Order | None = None) -> SubmitResult:
        """Refused before the risk chain could run, or by the venue."""
        return cls(order=order, decision=RiskDecision.deny(stage, reason), submitted=False)


@dataclass(frozen=True, slots=True)
class ProtectionResult:
    """What actually got placed against a filled entry, and what did not.

    A bare `list[Order]` cannot distinguish "this position needed no protection"
    from "this position is naked because the stop was refused" — both are the
    empty list. Three of the nine default rules can refuse a protective stop
    outright — trading hours, the rate limit and stale data all judge the order
    rather than whether it reduces a position — and two more
    (`max_position_size`, `max_gross_exposure`) refuse whenever any *other*
    holding is unmarked, so a denial here is ordinary rather than exotic. Only
    `max_open_positions`, `daily_loss_limit`, `buying_power` and the kill switch
    can never refuse one. The kill switch belonged in the first list until it
    was given the exit carve-out `KillSwitchRule` now documents: a halt refusing
    the protective child of an entry that had just filled was docs/SAFETY.md's
    layers 6 and 5 failing together, since that document makes "there are no
    unprotected positions" a go-live condition and names a stop that was never
    placed after the entry fill as the way layer 5 fails. Not a distinction to
    leave to the caller's memory.
    """

    placed: list[Order] = field(default_factory=list)
    refused: list[SubmitResult] = field(default_factory=list)
    #: Newly filled shares now covered by a live broker-side stop. Measured after
    #: the risk chain, so a shrunk stop cannot report the quantity it asked for.
    covered_qty: Decimal = Decimal(0)
    #: Newly filled shares with no broker-side stop. The number that matters.
    unprotected_qty: Decimal = Decimal(0)
    #: The level armed on the `Position` for the engine to watch, placed or not.
    engine_side_stop: Decimal | None = None

    @property
    def stop_order(self) -> Order | None:
        return next((o for o in self.placed if o.order_type is OrderType.STOP), None)

    @property
    def is_fully_protected(self) -> bool:
        return self.unprotected_qty == 0 and not self.refused


class OrderRouter:
    """Turns intent into executed orders, subject to risk."""

    def __init__(
        self,
        broker: BrokerPort,
        risk_engine: RiskEngine,
        stop_manager: StopManager,
        clock: Clock,
        *,
        kill_switch: KillSwitch | None = None,
    ) -> None:
        """
        `clock` supplies submission timestamps. It is injected rather than read
        from the wall clock so a backtest and a live run behave identically
        (CLAUDE.md §1.2, §5 "The clock"). It is never used to derive a
        `client_order_id` — see `idempotency.py` for why that would defeat the
        point.

        `kill_switch` is optional and keyword-only, following `StalenessMonitor`
        rather than `RiskEngine`: a collaborator whose absence degrades an
        escalation, not one whose absence would silently drop a control. When
        supplied it is engaged on an indeterminate submit — a transport failure
        whose outcome we cannot establish. `HaltReason.BROKER_UNREACHABLE` is
        one of the auto-engage triggers docs/RISK.md documents and none of which
        were wired; this is the one whose detector is the submission path.
        Without a switch that case is still logged `CRITICAL` and still raises,
        but nothing stops the next order, which is the reason to pass one in
        production.
        """
        self.broker = broker
        self.risk_engine = risk_engine
        self.stop_manager = stop_manager
        self.clock = clock
        self.kill_switch = kill_switch

        #: entry order id → the protective levels its request asked for. Intent,
        #: not truth: what we were told to protect, never what the venue holds.
        #: The broker is the source of truth for live orders, which is why
        #: `cancel_all` asks it rather than reading anything in here.
        self._requested_protection: dict[str, tuple[Decimal | None, Decimal | None]] = {}
        #: entry order id → how much of that entry is covered by a live stop, so
        #: an entry filling in pieces gets a stop per piece and not two against
        #: the same shares. Kept for the life of the process rather than dropped
        #: when the entry goes terminal: forgetting an entry resets its covered
        #: total to zero, and a replayed fill event would then place a second
        #: stop over shares that already have one. One small entry per protected
        #: order is the price of that, and it is the right way round.
        self._covered: dict[str, Decimal] = {}
        #: symbol → the protective children placed against it. In-memory, so it
        #: does not survive a restart; a stop placed before one is an orphan
        #: order for `Reconciler` to report (a separate Phase 4 item), which is
        #: documented there as report-do-not-auto-cancel.
        self._protective: dict[str, list[Order]] = {}

    # ── the submission path ─────────────────────────────────────────────────

    async def submit_signal(
        self,
        signal: Signal,
        portfolio: Portfolio,
        sizing_config: PositionSizeSpec,
        *,
        pending: Iterable[Order] = (),
    ) -> SubmitResult:
        """Size a signal, validate it, and send it.

        A risk denial is a normal outcome, not an exception: return a
        `SubmitResult` with `submitted=False` and a reason the dashboard can
        show. Raising here would make one blocked strategy look like a crash.

        The same reasoning covers everything upstream of the risk chain — a
        signal we cannot price, a `risk_pct` size with no stop to measure
        against, an action this engine does not model. Each is one strategy
        misconfigured rather than the platform broken, and each comes back named
        in `decision.rule` so the dashboard says which stage refused.

        Which actions are modelled deliberately matches
        `BacktestEngine._handle_signal` refusal for refusal: `SCALE_IN` and
        `SCALE_OUT` are refused in both, because the fraction to add or close is
        undefined and guessing it would make a live run and its backtest
        disagree about the same signal — the one divergence this architecture
        exists to prevent (docs/ARCHITECTURE.md, "The central idea").

        An entry against an opposing position is submitted rather than refused,
        also matching the backtest. The sized quantity is the *target*: an order
        larger than the position it opposes closes that position on its way
        through zero, which `reduces_position` is explicit about counting as a
        reduction, and refusing it would trap the very holding the strategy is
        trying to reverse.
        """
        if signal.action is SignalAction.HOLD:
            return SubmitResult.no_action(f"{signal.symbol}: hold")

        position = portfolio.positions.get(signal.symbol)

        if signal.action is SignalAction.EXIT:
            if position is None or position.is_flat:
                return SubmitResult.no_action(f"{signal.symbol}: exit signal on a flat position")
            # Never sized: an exit closes what is held, and a sizer asked to
            # produce that number could produce a different one.
            side = Side.SELL if position.is_long else Side.BUY
            qty = abs(position.qty)
            purpose = EXIT
        elif signal.action in (SignalAction.ENTER_LONG, SignalAction.ENTER_SHORT):
            side = Side.BUY if signal.action is SignalAction.ENTER_LONG else Side.SELL
            sized = self._size(signal, portfolio, sizing_config)
            if isinstance(sized, SubmitResult):
                return sized
            qty = sized
            purpose = ENTRY
        else:
            return SubmitResult.refused(
                ROUTING,
                f"{signal.action.value} is not modelled: the fraction to add or "
                f"close is undefined, and the backtest engine refuses it too — "
                f"guessing here would make the two disagree about one signal",
            )

        if qty <= 0:
            return SubmitResult.no_action(f"{signal.symbol}: sized to {qty}")

        request = OrderRequest(
            symbol=signal.symbol,
            side=side,
            decided_at=signal.ts,
            order_type=OrderType.LIMIT if signal.limit_price is not None else OrderType.MARKET,
            qty=qty,
            limit_price=signal.limit_price,
            stop_loss_price=signal.stop_loss_price,
            take_profit_price=signal.take_profit_price,
            strategy_id=signal.strategy_id,
            signal_id=signal.id,
            purpose=purpose,
        )
        result = await self.submit(request, portfolio, pending=pending)
        if purpose is EXIT and result.submitted:
            # **A strategy exit has to take the stop with it.** `flatten` has
            # always done this (see the end of that method); a strategy's own
            # EXIT signal comes through here instead, and did not — so a
            # position closed by a cross-down left its protective stop working
            # at the venue, GTC, with nothing behind it. If price later trades
            # through that level the stop fills and *opens a short*: the fill is
            # for an order this process still tracks, so no reconciler flags it;
            # `_protect` reads it as a reducing fill and arms nothing; and
            # `sma_crossover` can emit neither an entry (it wants `is_flat`) nor
            # an exit (it wants `is_long`), so the position is stranded with no
            # stop for the rest of the week.
            #
            # After the submit and not before, exactly as `flatten` orders it: a
            # refused exit must leave the position protected. A cancel that
            # fails is best-effort by `cancel_protection`'s own contract, and
            # the order stays tracked either way.
            await self.cancel_protection(request.symbol)
        return result

    async def submit(
        self, request: OrderRequest, portfolio: Portfolio, *, pending: Iterable[Order] = ()
    ) -> SubmitResult:
        """Submit a concrete request. THE submission path — do not add another.

        `pending` is what the caller has already sent and not yet seen settle.
        The portfolio moves on a fill, so a caller submitting several orders
        before any of them comes back has each judged against a book holding
        none of the others — see `RiskEngine.validate`. `StrategyRunner` passes
        the orders it believes are working at the venue; a caller with nothing
        outstanding can leave it empty.
        """
        qty = request.qty
        if qty is None:
            sized = self._size_notional(request, portfolio)
            if isinstance(sized, SubmitResult):
                return sized
            qty = sized

        order = Order(
            symbol=request.symbol,
            side=request.side,
            qty=qty,
            order_type=request.order_type,
            time_in_force=request.time_in_force,
            limit_price=request.limit_price,
            strategy_id=request.strategy_id,
            signal_id=request.signal_id,
            # Carried onto the order, not just consumed by the key derivation
            # below. The key is a one-way hash, so a stored order could not
            # answer "why did this position close?" without it.
            purpose=request.purpose,
            client_order_id=client_order_id(
                symbol=request.symbol,
                side=request.side,
                decided_at=request.decided_at,
                strategy_id=request.strategy_id,
                purpose=request.purpose,
            ),
            created_at=request.decided_at,
        )

        # The levels ride with the request and arm once the order fills — the
        # same shape the backtest engine uses, so a protective level set in a
        # backtest and one set live come from the same place.
        if request.stop_loss_price is not None or request.take_profit_price is not None:
            self._requested_protection[order.id] = (
                request.stop_loss_price,
                request.take_profit_price,
            )

        return await self._route(order, portfolio, pending)

    async def submit_protective_orders(
        self,
        entry_order: Order,
        portfolio: Portfolio,
        *,
        stop_config: StopConfig | None = None,
        atr_value: Decimal | None = None,
    ) -> ProtectionResult:
        """Attach protection after an entry fills.

        Submit this immediately on fill, before anything else. The window
        between "we own it" and "we have a stop on it" is unprotected exposure,
        and it is exactly when a fat-finger or a gap will find you. The stop
        goes to the venue so it survives our process dying (`risk/stops.py`);
        the take-profit is armed on the `Position` for the engine to watch,
        which is what `BacktestEngine._check_stops` already does and what
        `StrategyRunner`'s step 2 is documented to do. That reduced guarantee is
        the point rather than an oversight: a target that only exists in our
        process is an acceptable loss, and the module docstring says why a
        second live order is not.

        Levels come from the request that produced the entry, or — when it
        carried none — are derived here from `stop_config` against the entry's
        actual average fill price. The config is a parameter rather than router
        state because it belongs to the strategy, and an `atr` stop needs a
        value only the caller holding the bars can compute.

        **Anchored to the fill, not to the signal's expectation.** `risk_pct`
        sizing is defined off `|entry − stop|`, so a level anchored to a price
        we did not get silently stops meaning what the sizer assumed.

        **Covers what has filled since the last call**, capped at the exposure
        actually held. An entry that fills in pieces gets a stop per piece;
        `protective_client_order_id` keys each by the range it covers, so a
        replayed fill event places nothing and a genuinely new tranche is not
        deduplicated against the last one.

        Two contracts this places on whoever calls it, both load-bearing:

        1. Call it *after* folding the fill into the `Position`. Before, the
           stop does not reduce anything the risk chain can see, and
           `DailyLossLimitRule` and `BuyingPowerRule` lose the exemption that
           lets an exit through.
        2. Close from an engine-side trigger only over
           `abs(position.qty) - broker_side_protected_qty(symbol, position)`.
           The venue's stop fires for the rest; closing that part again would
           open a reversed position with nothing protecting it. The armed level
           is the fallback for the quantity whose child was refused, and the
           value a trailing stop ratchets.
        """
        symbol = entry_order.symbol
        if entry_order.parent_order_id is not None:
            # A protective order's own fill must not spawn a stop against the
            # stop. The fill stream carries both kinds and cannot tell them
            # apart; this can.
            log.warning(
                "order.protection_skipped",
                order_id=entry_order.id,
                detail="this is a protective child, not an entry",
            )
            return ProtectionResult()

        position = portfolio.positions.get(symbol)
        if position is None or position.is_flat:
            log.warning(
                "order.protection_skipped",
                symbol=symbol,
                order_id=entry_order.id,
                detail="the position is flat — nothing to protect",
            )
            return ProtectionResult()

        # A fill that flips the position through zero leaves the old side's
        # stops working, and they no longer close anything — a sell stop under a
        # position that is now short *adds* to the short when it triggers.
        # `Position.apply_fill` cannot help: it clears protective levels only at
        # exactly flat, and a flip never passes through flat. Cancel them before
        # counting anything, or they are counted as protection while being the
        # opposite.
        closing = Side.SELL if position.is_long else Side.BUY
        await self._cancel_stale_protection(symbol, closing)

        covered_from = self._covered.get(entry_order.id, Decimal(0))
        # Capped at the exposure held, not at what this order filled. A
        # reversal — a buy through a short — fills more than the position it
        # leaves behind, and a stop for the whole fill would sell the new long
        # and open a short underneath it.
        room = abs(position.qty) - self._protected_qty(symbol, closing)
        increment = min(entry_order.filled_qty - covered_from, room)
        if increment <= 0:
            return ProtectionResult()
        covered_to = covered_from + increment

        stop_level, target_level = self._requested_protection.get(entry_order.id, (None, None))
        entry_price = entry_order.avg_fill_price
        if stop_config is not None and entry_price is not None:
            if stop_level is None:
                stop_level = self.stop_manager.initial_stop(
                    entry_price, entry_order.side, stop_config, atr_value
                )
            if target_level is None and stop_config.stop_type in FROM_ENTRY_TYPES:
                # Only a fixed distance from entry is expressible as a target;
                # `take_profit_level` raises for the rest rather than inventing
                # one, and raising inside a fill handler on a freshly opened
                # position is the worst place in the system for it.
                target_level = self.stop_manager.take_profit_level(
                    entry_price, entry_order.side, stop_config
                )

        if stop_level is not None and not _protects(position, stop_level):
            # A level the market has already passed is not a stop: submitted, it
            # is a market order wearing a stop's clothes. The reachable case is
            # a reversal that has only partly filled — the position is still on
            # the old side while the level belongs to the side being opened — and
            # arming it would trigger on the very next bar.
            log.critical(
                "order.stop_level_already_through_the_market",
                symbol=symbol,
                entry_order_id=entry_order.id,
                level=str(stop_level),
                mark=str(position.last_price),
                position_qty=str(position.qty),
                detail="refusing to arm or submit a stop the market has passed",
            )
            stop_level = None

        # Armed before the submit, so a risk denial still leaves something
        # watching. Never widened: a second tranche filled at a worse price
        # computes a looser level and the position keeps the tighter one it had
        # (docs/RISK.md, "Never widen a stop").
        if stop_level is not None:
            position.stop_loss_price = _tightest_stop(position, stop_level)
        if target_level is not None and position.take_profit_price is None:
            position.take_profit_price = target_level
        armed = position.stop_loss_price

        if stop_level is None:
            log.critical(
                "order.position_unprotected",
                symbol=symbol,
                entry_order_id=entry_order.id,
                qty=str(increment),
                detail="no usable stop level for this position",
            )
            return ProtectionResult(unprotected_qty=increment, engine_side_stop=armed)

        stop_child = self._stop_order(
            entry_order, position, increment, stop_level, covered_from, covered_to
        )
        outcome = await self._route(stop_child, portfolio)

        if not outcome.submitted:
            # Not a kill-switch escalation. `KillSwitchRule` refuses everything
            # with no exit carve-out, so halting here would block both the retry
            # of this stop and any `flatten` of the position it is warning
            # about. Loud, surfaced, and left retryable instead — a transient
            # denial clears, and the deterministic key makes the retry the same
            # order to the venue rather than a second stop.
            log.critical(
                "order.position_unprotected",
                symbol=symbol,
                entry_order_id=entry_order.id,
                qty=str(increment),
                rule=outcome.decision.rule,
                detail=outcome.decision.reason,
            )
            return ProtectionResult(
                refused=[outcome], unprotected_qty=increment, engine_side_stop=armed
            )

        # The post-validate quantity, because a rule may have shrunk the child.
        # Booking what we asked for rather than what is working would report a
        # partly covered position as fully protected.
        covered = stop_child.qty
        self._covered[entry_order.id] = covered_from + covered
        self._protective.setdefault(symbol, []).append(stop_child)
        if entry_order.is_complete and entry_order.filled_qty <= self._covered[entry_order.id]:
            # Nothing further can fill against this entry, so the levels it
            # asked for are spent. Dropped so a long-running worker's
            # bookkeeping does not grow for the life of the process.
            self._requested_protection.pop(entry_order.id, None)

        log.info(
            "order.protective_stop_placed",
            symbol=symbol,
            entry_order_id=entry_order.id,
            stop_order_id=stop_child.id,
            level=str(stop_level),
            qty=str(covered),
        )
        return ProtectionResult(
            placed=[stop_child],
            covered_qty=covered,
            unprotected_qty=increment - covered,
            engine_side_stop=armed,
        )

    def broker_side_protected_qty(self, symbol: str, position: Position) -> Decimal:
        """How much of `position` has a stop working at the venue.

        The runner asks before acting on an engine-side trigger. If both fire
        the position closes twice, and the second close opens a reversed
        position with nothing protecting it.

        A quantity rather than a boolean, and it takes the position rather than
        only a symbol, because both of the simpler answers are wrong in ways
        that cost money. A boolean says "protected" for a partially covered
        position and suppresses the engine-side fallback over the uncovered
        remainder. A symbol-only count includes stops facing the other way,
        which after a flip are the opposite of protection.
        """
        closing = Side.SELL if position.is_long else Side.BUY
        return self._protected_qty(symbol, closing)

    def has_broker_side_protection(self, symbol: str, position: Position) -> bool:
        """Whether *all* of `position` has a stop working at the venue."""
        return self.broker_side_protected_qty(symbol, position) >= abs(position.qty)

    async def cancel_protection(self, symbol: str) -> int:
        """Cancel this router's protective orders for a symbol; return how many
        cancels were sent.

        Deliberately not `cancel_all(symbol)`: that would take another
        strategy's resting orders in the same name. Deliberately narrower than
        the venue's truth, too — protective orders placed before a restart are
        not in here, and adopting them is `Reconciler`'s job rather than
        something to guess at from a symbol.

        Counts cancels *sent*, and does not move the order to `CANCELLED`
        locally. Cancelling an order the venue has already filled is a race we
        lost rather than an error (`BrokerPort.cancel_order`), and in that race
        the order is `FILLED` — recording our intent as its outcome would put a
        cancelled order in the book against a position that exists. The venue's
        own event settles it.

        Best-effort, like `cancel_all`, and for a sharper reason: one stop that
        will not cancel must not abandon the rest, and an order we failed to
        cancel must stay *tracked*. Forgetting it would put it beyond the reach
        of every later call — the router would report no protection while the
        venue still held a live stop, and only `cancel_all`, which reads the
        venue, could ever find it again.

        Does not raise. Its caller is holding an outcome of its own — `flatten`
        has a close that was accepted — and losing that to a failed cancel would
        be the wrong trade. The failure is `CRITICAL` instead.
        """
        children = self._protective.get(symbol, [])
        survivors: list[Order] = []
        cancelled = 0
        for child in children:
            if child.is_complete or child.broker_order_id is None:
                continue
            try:
                await self.broker.cancel_order(child.broker_order_id)
            except BrokerError as exc:
                survivors.append(child)
                log.critical(
                    "order.protection_still_live",
                    symbol=symbol,
                    broker_order_id=child.broker_order_id,
                    error=str(exc),
                    detail="a working stop against a position being closed",
                )
                continue
            cancelled += 1

        if survivors:
            self._protective[symbol] = survivors
        else:
            self._protective.pop(symbol, None)
        log.info(
            "order.protection_cancelled",
            symbol=symbol,
            cancelled=cancelled,
            still_live=len(survivors),
        )
        return cancelled

    async def cancel_all(self, symbol: str | None = None) -> int:
        """Cancel open orders; return how many. Symbol-scoped or everything.

        Asks the broker what is open rather than reading local bookkeeping:
        docs/ARCHITECTURE.md is explicit that the broker is the truth and our
        state a cache, and a cancel-all driven from a stale cache would leave
        precisely the orders it did not know about — which, after a restart, are
        the ones most likely to be there.

        Best-effort by design. One order that will not cancel must not abandon
        the other nine, so every order is attempted and a failure is raised only
        once they all have been. The count is returned only when every cancel
        succeeded, because a number implying ten when three are still live is
        worse than an exception.
        """
        open_orders = await self.broker.get_open_orders()
        wanted = symbol.upper() if symbol is not None else None
        targets = [
            o
            for o in open_orders
            if (wanted is None or o.symbol == wanted) and o.broker_order_id is not None
        ]

        cancelled = 0
        failures: list[str] = []
        for order in targets:
            assert order.broker_order_id is not None  # narrowed by the filter above
            try:
                await self.broker.cancel_order(order.broker_order_id)
            except BrokerError as exc:
                failures.append(f"{order.symbol}/{order.broker_order_id}: {exc}")
                continue
            cancelled += 1
            self._forget_protective(order)

        log.info(
            "order.cancel_all",
            symbol=symbol,
            requested=len(targets),
            cancelled=cancelled,
            failed=len(failures),
        )
        if failures:
            raise ExecutionError(
                f"cancel_all cancelled {cancelled} of {len(targets)} orders; "
                f"{len(failures)} failed: {'; '.join(failures)}"
            )
        return cancelled

    async def flatten(
        self,
        symbol: str,
        portfolio: Portfolio,
        *,
        decided_at: datetime | None = None,
        purpose: str = FLATTEN,
    ) -> SubmitResult:
        """Close a position at market.

        Exits bypass entry-blocking risk rules (e.g. the daily loss limit) but
        still pass through `validate()` — a rule that blocks an exit is a rule
        that traps you in a losing position (see `DailyLossLimitRule`). The
        kill switch is now one of the rules that stands aside: it permits an
        order that can only reduce, because refusing a flatten during a halt
        stopped the platform from *reducing* risk when halting is only meant to
        stop new risk (`KillSwitchRule`, docs/SAFETY.md). Rules that judge
        whether this is a sane moment to trade at all still refuse one, and
        this does not carve *them* out: a flatten refused for stale data or
        outside trading hours comes back naming that rule, so the human who
        pressed the button reads "refused by stale_data" rather than believing
        a position closed.

        Goes through `submit()` rather than `BrokerPort.close_position`, which
        would reach the venue without passing the chain and is the bypass ADR
        0005 exists to refuse. `close_position` / `close_all_positions` are for
        the runbook's emergency flatten — a human acting around a platform they
        have already halted, and correct precisely when our own view of the book
        is the thing you cannot build a request from.

        **Submits before cancelling protection**, and the order matters. Cancel
        first and there is a live path that ends with the position open, its
        stop cancelled and the close refused. A refused flatten must leave the
        position protected. The residual is named rather than hidden: between
        the close being acknowledged and the stop being cancelled, the stop can
        fire and the position sells twice. That window exists in either ordering
        — `cancel_order` can lose the race outright — and the fix is a venue-side
        bracket on the `BrokerPort` item, not a reordering.

        `decided_at` defaults to now, which makes each call a new decision with
        its own idempotency key. Pin it to retry a flatten whose outcome you
        could not establish: that is the one case where two calls must collapse
        into one order at the venue.

        `purpose` defaults to a bare `FLATTEN` — an operator closing a position
        — and callers that know better should say so. Every engine-side exit
        arrives here: a triggered stop, a hit target and a time exit are all
        "close it at market", and the caller is the only thing that knows which.
        Left to default, all three would store as `flatten` and
        `analytics.performance`'s exit-reason attribution would report one
        undifferentiated bucket for the three facts it exists to separate. It is
        also in the idempotency key, correctly: a stop and a time exit firing on
        the same bar are two decisions, and one key for both would silently drop
        the second.
        """
        # `.get`, not `.position()`: the latter is a `setdefault` and would
        # insert a Position into a book this is only inspecting.
        position = portfolio.positions.get(symbol)
        if position is None or position.is_flat:
            return SubmitResult.no_action(f"{symbol}: already flat")

        request = OrderRequest(
            symbol=symbol,
            side=Side.SELL if position.is_long else Side.BUY,
            decided_at=decided_at if decided_at is not None else self.clock.now(),
            order_type=OrderType.MARKET,
            qty=abs(position.qty),
            # GTC: a DAY flatten that does not fill evaporates at the close and
            # leaves the position we were trying to be rid of open overnight.
            time_in_force=TimeInForce.GTC,
            purpose=purpose,
        )
        result = await self.submit(request, portfolio)
        if result.submitted:
            await self.cancel_protection(symbol)
        return result

    # ── internals ───────────────────────────────────────────────────────────

    def _protected_qty(self, symbol: str, closing_side: Side) -> Decimal:
        """Quantity covered by this router's still-working protective orders.

        Side-aware, and it has to be. A stop is protection only if it closes the
        position that exists now; the same order against a position that has
        since flipped adds to it. Counting by symbol alone lets the old side's
        stops fill the new side's protection budget, and the position comes out
        part-naked while `ProtectionResult` reports it fully protected.
        """
        return sum(
            (
                o.remaining_qty
                for o in self._protective.get(symbol, [])
                if not o.is_complete and o.side is closing_side
            ),
            Decimal(0),
        )

    async def _cancel_stale_protection(self, symbol: str, closing_side: Side) -> int:
        """Cancel protective orders left facing the wrong way by a flip.

        Best-effort, and loud when it fails: a stop we could not cancel is a
        live order that will *open* a position rather than close one.
        """
        children = self._protective.get(symbol, [])
        stale = [
            c
            for c in children
            if not c.is_complete and c.side is not closing_side and c.broker_order_id is not None
        ]
        if not stale:
            return 0

        cancelled = 0
        for child in stale:
            assert child.broker_order_id is not None  # narrowed by the filter above
            try:
                await self.broker.cancel_order(child.broker_order_id)
            except BrokerError as exc:
                log.critical(
                    "order.stale_protection_still_live",
                    symbol=symbol,
                    broker_order_id=child.broker_order_id,
                    side=child.side.value,
                    error=str(exc),
                    detail="this order now adds to the position instead of closing it",
                )
                continue
            cancelled += 1
            children.remove(child)

        log.warning(
            "order.stale_protection_cancelled",
            symbol=symbol,
            cancelled=cancelled,
            detail="the position flipped side; its old stops no longer close it",
        )
        return cancelled

    def _forget_protective(self, cancelled: Order) -> None:
        """Drop a protective child we have just cancelled through `cancel_all`."""
        children = self._protective.get(cancelled.symbol)
        if not children:
            return
        remaining = [c for c in children if c.broker_order_id != cancelled.broker_order_id]
        if remaining:
            self._protective[cancelled.symbol] = remaining
        else:
            self._protective.pop(cancelled.symbol, None)

    def _stop_order(
        self,
        entry_order: Order,
        position: Position,
        qty: Decimal,
        level: Decimal,
        covered_from: Decimal,
        covered_to: Decimal,
    ) -> Order:
        """Build the protective stop child of `entry_order`.

        `OrderType.STOP`, not `STOP_LIMIT`: a stop-limit can fail to fill on a
        gap, which is precisely when the stop matters, and it would diverge from
        how the backtest engine models a triggered stop.

        `TimeInForce.GTC`, not the `Order` default of DAY: a DAY stop is
        cancelled at the close, which would falsify the contract
        `StrategyRunner.shutdown` states — "the stops are there precisely so the
        position can survive us not running" — through the overnight gap
        docs/RISK.md names as the risk a stop cannot cover.

        The side comes from the position rather than from the entry, because a
        reversal's entry side and the side that closes the resulting position
        are not opposites.
        """
        return Order(
            symbol=entry_order.symbol,
            side=Side.SELL if position.is_long else Side.BUY,
            qty=qty,
            order_type=OrderType.STOP,
            time_in_force=TimeInForce.GTC,
            stop_price=level,
            strategy_id=entry_order.strategy_id,
            signal_id=entry_order.signal_id,
            parent_order_id=entry_order.id,
            purpose=STOP_LOSS,
            client_order_id=protective_client_order_id(
                entry_order.client_order_id, STOP_LOSS, covered_from, covered_to
            ),
            created_at=entry_order.filled_at or entry_order.created_at,
        )

    async def _route(
        self, order: Order, portfolio: Portfolio, pending: Iterable[Order] = ()
    ) -> SubmitResult:
        """Validate and send one built order. The gate every order passes.

        Private because `OrderRequest` is what callers hand in — a strategy able
        to build an `Order` itself could build one the risk engine has not seen,
        which is the reason the two types are separate at all
        (`domain/order.py`).
        """
        if order.status is not OrderStatus.PENDING_RISK:
            raise ExecutionError(
                f"{order.id} arrived at the router as {order.status.value}; the "
                f"single submission path starts at {OrderStatus.PENDING_RISK.value}. "
                f"Retrying? Rebuild the order from its request — the "
                f"client_order_id is derived from the decision, so the retry "
                f"reuses the key (CLAUDE.md §1.4)"
            )

        decision = self.risk_engine.validate(order, portfolio, pending)
        if not decision.approved:
            transition(
                order,
                OrderStatus.REJECTED_RISK,
                reason=decision.reason,
                rejected_by=decision.rule,
            )
            metrics.order_rejected("risk")
            log.warning(
                "order.risk_denied",
                order_id=order.id,
                client_order_id=order.client_order_id,
                symbol=order.symbol,
                side=order.side.value,
                qty=str(order.qty),
                rule=decision.rule,
                reason=decision.reason,
            )
            return SubmitResult(order=order, decision=decision, submitted=False)

        # `validate` has already applied any shrink to `order.qty`. This is the
        # case `RiskDecision.shrink` cannot express — it refuses a non-positive
        # quantity at construction — but a custom rule can reach it through the
        # plain constructor, and an order for nothing must not reach a venue.
        if order.qty <= 0:
            reason = f"risk left {order.qty} of {order.symbol} to trade"
            transition(order, OrderStatus.REJECTED_RISK, reason=reason, rejected_by=ROUTING)
            metrics.order_rejected("risk")
            return SubmitResult.refused(ROUTING, reason, order)

        transition(order, OrderStatus.PENDING_SUBMIT)
        # Timed around the venue call and nothing else, so the histogram answers
        # "how slow is the broker" rather than "how slow are we". Wall clock via
        # `perf_counter` rather than the injected `Clock`: this is a duration on
        # one machine, not a moment in market time, and a `SimulatedClock`
        # jumping a day between two reads would put a day in the bucket.
        started = time.perf_counter()
        try:
            acknowledged = await self.broker.submit_order(order)
        except OrderRejectedError as exc:
            metrics.order_submit_seconds(self.broker.name, time.perf_counter() - started)
            metrics.order_rejected("broker")
            transition(order, OrderStatus.REJECTED, reason=str(exc), rejected_by=self.broker.name)
            log.warning(
                "order.broker_rejected",
                order_id=order.id,
                client_order_id=order.client_order_id,
                symbol=order.symbol,
                broker=self.broker.name,
                reason=str(exc),
            )
            return SubmitResult(order=order, decision=decision, submitted=False)
        except BrokerConnectionError as exc:
            metrics.order_submit_seconds(self.broker.name, time.perf_counter() - started)
            return await self._resolve_indeterminate(order, decision, exc)

        metrics.order_submit_seconds(self.broker.name, time.perf_counter() - started)
        working = self._adopt(order, acknowledged)
        if not working:
            # The venue answered, and its answer was no. `submitted` means "the
            # venue has this order working", not "the call returned" — a caller
            # that reads it as the latter cancels a protective stop against a
            # close that was refused.
            metrics.order_rejected("acknowledgement")
            log.warning(
                "order.rejected_on_acknowledgement",
                order_id=order.id,
                client_order_id=order.client_order_id,
                symbol=order.symbol,
                status=order.status.value,
                reason=order.reject_reason,
                broker=self.broker.name,
            )
            return SubmitResult(order=order, decision=decision, submitted=False)

        metrics.order_submitted(order.side.value, order.order_type.value)
        log.info(
            "order.submitted",
            order_id=order.id,
            client_order_id=order.client_order_id,
            broker_order_id=order.broker_order_id,
            symbol=order.symbol,
            side=order.side.value,
            qty=str(order.qty),
            order_type=order.order_type.value,
            broker=self.broker.name,
        )
        return SubmitResult(order=order, decision=decision, submitted=True)

    def _adopt(self, order: Order, acknowledged: Order) -> bool:
        """Fold the venue's acknowledgement into our order.

        The order passes through `SUBMITTED` even when the venue reports it
        already terminal, because that is the true sequence: it was accepted and
        then it resolved. Assigning the end state directly would be a transition
        the table forbids — `PENDING_SUBMIT` has no edge to `FILLED` — and the
        table is right, because skipping the acknowledgement is how an order
        that was never accepted acquires a fill.

        A reported *fill* status is deliberately not adopted. Our order carries
        no `Fill` to justify it, so a `FILLED` order would report `filled_qty`
        of zero and `signed_filled_qty` of zero to everything computing a
        position delta — a position appearing to vanish, which is the failure
        the state machine exists to prevent. Fills arrive on the trade-updates
        stream, which owns applying them (a separate Phase 4 item); this records
        only that the venue has the order.

        Returns whether the order is *working* at the venue. An acknowledgement
        can carry a refusal — the port requires idempotency on
        `client_order_id`, and both adapters implement that as returning the
        venue's existing copy, which may already be terminal — so "the call
        returned" and "the order is live" are different facts and the caller
        needs the second one.
        """
        order.broker_order_id = acknowledged.broker_order_id
        if order.broker_order_id is None:
            # The port says the returned order carries one. Without it we can
            # never cancel or reconcile this order by id, so it must not pass
            # quietly — but it is live, so it is not ours to discard either.
            log.critical(
                "order.acknowledged_without_broker_id",
                order_id=order.id,
                client_order_id=order.client_order_id,
                symbol=order.symbol,
                broker=self.broker.name,
                detail="cannot cancel or reconcile this order by id",
            )
        transition(order, OrderStatus.SUBMITTED, at=self.clock.now())

        reported = acknowledged.status
        if reported in (OrderStatus.REJECTED, OrderStatus.CANCELLED, OrderStatus.EXPIRED):
            transition(
                order, reported, reason=acknowledged.reject_reason, rejected_by=self.broker.name
            )
            return False
        if reported in (OrderStatus.PARTIALLY_FILLED, OrderStatus.FILLED):
            log.info(
                "order.filled_on_acknowledgement",
                order_id=order.id,
                client_order_id=order.client_order_id,
                reported=reported.value,
                detail="left SUBMITTED; the trade-updates stream applies the fills",
            )
        return True

    async def _resolve_indeterminate(
        self, order: Order, decision: RiskDecision, exc: BrokerConnectionError
    ) -> SubmitResult:
        """A submit whose outcome we do not know. Look once, then stop.

        The transport failed after we sent the order, so the venue either never
        saw it, or has it, or has already filled it. `get_open_orders` settles
        the middle case and nothing available settles the other two apart — an
        order that filled in the gap is no longer open, and `get_order` needs a
        `broker_order_id` the failure is the reason we never received. A lookup
        by `client_order_id` would settle it, and adding one to `BrokerPort`
        belongs with the adapter item rather than here; the residual is a false
        alarm in the safe direction.

        So: one lookup. Found means submitted and we carry on. Not found means
        unknown, and unknown is where this stops. **It does not resubmit.** The
        port promises idempotency on `client_order_id`, which is why a resubmit
        would probably be safe — but "probably" is being decided against an
        adapter nobody has written yet, and losing an intended trade is
        recoverable in a way that an unknown extra position is not.

        The order is left in `PENDING_SUBMIT` — "approved, not yet acknowledged"
        is exactly the truth — carrying the deterministic `client_order_id` that
        lets reconciliation, or a caller retrying the same request, resolve it
        against the venue without risking a duplicate. That status now carries
        two meanings, "never sent" and "sent, outcome unknown", told apart only
        by the CRITICAL log until reconciliation lands.

        Raises rather than returning, and it is the only non-bug in this module
        that does. Every other refusal is a value because the platform is fine
        and one order is not; this one means we may be holding a position nobody
        knows about, and handing back a `SubmitResult` invites a caller's loop
        to submit the next order on top of it. It halts and does not flatten:
        halting stops new risk, while flattening against a position that may not
        exist opens a short (docs/RISK.md, "Halting is not flattening").
        """
        found: Order | None = None
        try:
            for candidate in await self.broker.get_open_orders():
                if candidate.client_order_id == order.client_order_id:
                    found = candidate
                    break
        except BrokerConnectionError:
            # Still unreachable. Nothing to add — fall through to the unknown
            # path, which is where an unanswerable lookup belongs.
            found = None

        if found is not None:
            working = self._adopt(order, found)
            log.warning(
                "order.submit_recovered",
                order_id=order.id,
                client_order_id=order.client_order_id,
                broker_order_id=order.broker_order_id,
                status=order.status.value,
                detail="submit failed in transport but the venue has the order",
            )
            return SubmitResult(order=order, decision=decision, submitted=working)

        metrics.order_rejected("indeterminate")
        log.critical(
            "order.submit_indeterminate",
            order_id=order.id,
            client_order_id=order.client_order_id,
            symbol=order.symbol,
            side=order.side.value,
            qty=str(order.qty),
            broker=self.broker.name,
            error=str(exc),
            detail="not resubmitting: the venue may already hold this order",
        )
        if self.kill_switch is not None:
            # Imported here rather than at module scope: the router does not
            # otherwise need the kill switch's vocabulary, and this is the only
            # path that reaches for it.
            from atp_core.risk.killswitch import HaltReason, HaltScope

            self.kill_switch.engage(
                # GLOBAL rather than SYMBOL: the doubt is about the book, not
                # about one ticker.
                HaltScope.GLOBAL,
                HaltReason.BROKER_UNREACHABLE,
                engaged_by="order_router",
                detail=(
                    f"submit of {order.client_order_id} ({order.symbol} "
                    f"{order.side.value} {order.qty}) failed in transport and could "
                    f"not be resolved against the venue"
                ),
            )
        raise BrokerConnectionError(
            f"submit of {order.client_order_id} ({order.symbol} {order.side.value} "
            f"{order.qty}) failed and its outcome could not be established; not "
            f"resubmitting — reconcile against the venue before trading this symbol"
        ) from exc

    def _size_notional(self, request: OrderRequest, portfolio: Portfolio) -> Decimal | SubmitResult:
        """Turn a notional request into shares, or say why it cannot be.

        Served rather than refused, because `OrderRequest.notional` is a
        validated field with a stated meaning ("buy $5,000 of it") and a field
        nobody reads is the `flatten_at_close` complaint repeated.
        """
        if request.notional is None:
            return SubmitResult.refused(
                SIZING,
                f"{request.symbol}: the request states neither a quantity nor a "
                f"notional, so there is nothing to submit",
            )
        price = reference_price(request.symbol, portfolio, request.limit_price)
        if price is None:
            return SubmitResult.refused(
                SIZING,
                f"{request.symbol}: a notional request needs a price to turn into "
                f"shares and none is available",
            )
        if portfolio.equity <= 0:
            return SubmitResult.refused(SIZING, f"{request.symbol}: equity is {portfolio.equity}")
        qty = position_size("fixed_notional", portfolio.equity, price, risk_pct=request.notional)
        if qty <= 0:
            return SubmitResult.no_action(
                f"{request.symbol}: {request.notional} at {price} rounds down to no shares"
            )
        return qty

    @staticmethod
    def _size(
        signal: Signal, portfolio: Portfolio, sizing_config: PositionSizeSpec
    ) -> Decimal | SubmitResult:
        """Delegates to `risk.rules.position_size`; refuses what it cannot size.

        Returns the quantity, or a `SubmitResult` naming why there is none. The
        two inputs `position_size` refuses to default — a stop for `risk_pct`, a
        volatility for `volatility_target` — surface as refusals rather than
        exceptions for the reason `submit_signal` gives: a strategy configured
        to size by risk while emitting signals with no stop is one strategy
        misconfigured, and it belongs on the dashboard rather than taking the
        runner's loop down with it.

        Prices the trade with `reference_price`, the same function the rules use
        (`risk/rules.py`), so sizing and validation cannot disagree about what a
        share costs — a disagreement that would be invisible, since both numbers
        look right on their own.
        """
        price = reference_price(signal.symbol, portfolio, signal.limit_price)
        if price is None:
            return SubmitResult.refused(
                SIZING,
                f"no price available for {signal.symbol}: nothing has marked it "
                f"and the signal carries no limit price",
            )
        if portfolio.equity <= 0:
            return SubmitResult.refused(
                SIZING, f"cannot size {signal.symbol} against equity of {portfolio.equity}"
            )
        try:
            return position_size(
                sizing_config.type,
                portfolio.equity,
                price,
                stop_price=signal.stop_loss_price,
                risk_pct=sizing_config.value,
            )
        except ValueError as exc:
            return SubmitResult.refused(SIZING, f"{signal.symbol}: {exc}")


def _protects(position: Position, level: Decimal) -> bool:
    """Whether `level` is still a stop for this position, rather than a fill.

    A long's stop sits below the market and a short's above. One on the wrong
    side has already been passed, so submitting it is a market order in
    disguise and arming it triggers on the next bar. Refusing it is the same
    instinct as `StopManager`'s refusal of a level below zero: a position that
    looks guarded and is not is worse than one openly unguarded.

    Unmarked, we cannot judge, and a position with no mark is not the place to
    start discarding stops — say yes and let the risk chain's own unpriced-book
    refusal handle it.
    """
    mark = position.last_price
    if mark is None:
        return True
    return level < mark if position.is_long else level > mark


def _tightest_stop(position: Position, candidate: Decimal) -> Decimal:
    """The stop closer to price, of the one held and the one proposed.

    Never widen a stop. Moving one away from price to avoid being hit converts a
    planned small loss into an unplanned large one, and it always feels
    justified at the time (docs/RISK.md).

    A level left over from the opposite side is not "wider", it is not a stop at
    all — and it survives, because `Position.apply_fill` clears protective
    levels only at exactly flat and a flip through zero never passes through
    flat. Taken as `current`, a long's old stop at 95 would beat a short's
    proposed 105 on the `min` and arm a buy stop five points *below* a short
    entered at 100.
    """
    current = position.stop_loss_price
    if current is None or not _protects(position, current):
        return candidate
    return max(current, candidate) if position.is_long else min(current, candidate)


__all__ = [
    "NO_ACTION",
    "ROUTING",
    "SIZING",
    "OrderRouter",
    "ProtectionResult",
    "SubmitResult",
]
