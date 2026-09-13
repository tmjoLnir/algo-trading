"""The order router — the single submission path (rule §1.5, ADR 0005).

The happy path here is one assertion. Everything else is a failure path, which
is the right proportion for the module that stands between a signal and real
money: a router that submits correctly and mishandles a timeout is a router that
works until the first bad afternoon.

The cases worth naming, because each is a specific loss:

- a transport failure must never produce a second order (a doubled position);
- a risk denial must be a value, not an exception (one blocked strategy must not
  look like a crash and stop the loop for every other symbol);
- a protective stop that was refused must be impossible to mistake for one that
  was placed (docs/SAFETY.md layer 5);
- an entry filling in pieces must end up with a stop over every piece;
- a flatten must never leave a position both open and unprotected.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from atp_core.clock import SimulatedClock, TradingCalendar
from atp_core.domain import (
    Fill,
    Instrument,
    Order,
    OrderRequest,
    OrderStatus,
    OrderType,
    Portfolio,
    Position,
    Side,
    Signal,
    SignalAction,
    TimeInForce,
)
from atp_core.domain.enums import StopType
from atp_core.errors import (
    BrokerConnectionError,
    BrokerError,
    ExecutionError,
    InsufficientFundsError,
)
from atp_core.execution.router import NO_ACTION, ROUTING, SIZING, OrderRouter
from atp_core.risk.engine import RiskBooks, RiskDecision, RiskEngine, RiskRule, default_rules
from atp_core.risk.limits import RiskLimits
from atp_core.risk.rules import DailyLossLimitRule
from atp_core.risk.stops import StopConfig, StopManager
from atp_core.strategy.rules import PositionSizeSpec
from tests.fakes import FakeBroker, FakeKillSwitch

#: 10:00 New York on a Tuesday — inside the session, so `TradingHoursRule`
#: allows and the tests are about the router rather than about the calendar.
OPEN_HOURS = datetime(2024, 1, 2, 15, 0, tzinfo=UTC)


def limits(**overrides: object) -> RiskLimits:
    """Explicit values — never inherited from the environment, or a stray RISK_*
    export would quietly change what these tests assert."""
    base: dict[str, object] = {
        "max_position_pct": Decimal("0.10"),
        "max_gross_exposure_pct": Decimal("1.00"),
        "max_daily_loss_pct": Decimal("0.03"),
        "max_orders_per_minute": 30,
        "max_open_positions": 20,
        "max_quote_age_seconds": 30,
    }
    base.update(overrides)
    return RiskLimits(**base)  # type: ignore[arg-type]


def book(cash: float = 100_000, **holdings: tuple[float, float]) -> Portfolio:
    """`holdings` is symbol → (qty, mark). A mark of 0 means *unmarked*."""
    portfolio = Portfolio(cash=Decimal(str(cash)), starting_equity=Decimal(str(cash)))
    for symbol, (qty, mark) in holdings.items():
        position = portfolio.position(symbol)
        position.qty = Decimal(str(qty))
        position.avg_entry_price = Decimal(str(mark or 100))
        position.last_price = Decimal(str(mark)) if mark else None
    return portfolio


@dataclass
class Refusing:
    """A rule that refuses whatever it is shown, switched on and off at will.

    Some tests below are about what the router does with a *refused* protective
    stop or flatten, and not about which rule refused it. They used the kill
    switch as the lever until `KillSwitchRule` gained its exit carve-out and
    stopped refusing exits — which is that carve-out working as intended, and
    which would otherwise have quietly turned three tests about handling a
    refusal into three tests where nothing is refused. An explicit lever cannot
    stop pulling without saying so.
    """

    engaged: bool = False
    name: str = "refuses_everything"

    def check(self, order: Order, books: RiskBooks, lim: RiskLimits) -> RiskDecision:
        if self.engaged:
            return RiskDecision.deny(self.name, "refused, because this test asked it to be")
        return RiskDecision.allow()


def chain(
    switch: FakeKillSwitch | None = None,
    *,
    extra: list[RiskRule] | None = None,
    anchor: Decimal | None = Decimal(100_000),
    last_tick: datetime | None = OPEN_HOURS,
    **limit_overrides: object,
) -> RiskEngine:
    """The real nine-rule chain, anchored so it can evaluate the loss limit.

    `extra` appends rules after the nine, for a test that needs a refusal the
    real chain will not produce on its own.
    """
    rules = default_rules(
        kill_switch=switch or FakeKillSwitch(),
        clock=SimulatedClock(OPEN_HOURS),
        calendar=TradingCalendar(),
        last_tick_at=lambda _s: last_tick,
    )
    if anchor is not None:
        for rule in rules:
            if isinstance(rule, DailyLossLimitRule):
                rule.anchor(anchor)
    return RiskEngine(limits(**limit_overrides), rules=rules + list(extra or []))


def permissive() -> RiskEngine:
    """An engine that refuses nothing, for tests about sizing or transport
    rather than about the rules. Has to be asked for explicitly (`RiskEngine`
    raises on an omitted chain), which is the point."""
    return RiskEngine(limits(), rules=[])


def router(
    broker: FakeBroker | None = None,
    engine: RiskEngine | None = None,
    *,
    kill_switch: FakeKillSwitch | None = None,
) -> OrderRouter:
    return OrderRouter(
        broker or FakeBroker(),
        engine or permissive(),
        StopManager(),
        SimulatedClock(OPEN_HOURS),
        kill_switch=kill_switch,
    )


def signal(
    action: SignalAction = SignalAction.ENTER_LONG,
    symbol: str = "SPY",
    *,
    stop: float | None = None,
    limit: float | None = None,
    ts: datetime = OPEN_HOURS,
) -> Signal:
    return Signal(
        strategy_id="sma_crossover",
        symbol=symbol,
        action=action,
        ts=ts,
        stop_loss_price=Decimal(str(stop)) if stop is not None else None,
        limit_price=Decimal(str(limit)) if limit is not None else None,
    )


def sizing(kind: str = "fixed_qty", value: str = "100") -> PositionSizeSpec:
    return PositionSizeSpec(type=kind, value=Decimal(value))


def request(
    symbol: str = "SPY",
    side: Side = Side.BUY,
    *,
    qty: float | None = 100,
    notional: float | None = None,
    ts: datetime = OPEN_HOURS,
    **extra: object,
) -> OrderRequest:
    return OrderRequest(
        symbol=symbol,
        side=side,
        decided_at=ts,
        qty=Decimal(str(qty)) if qty is not None else None,
        notional=Decimal(str(notional)) if notional is not None else None,
        strategy_id="sma_crossover",
        **extra,  # type: ignore[arg-type]
    )


def fill(order: Order, portfolio: Portfolio, qty: float, price: float) -> None:
    """Fold a fill into both the order and the book, as the runner does.

    Order matters and is a contract `submit_protective_orders` depends on: the
    position must already carry the fill, or the stop does not reduce anything
    the risk chain can see and loses the exemption that lets an exit through.
    """
    execution = Fill(
        order_id=order.id,
        ts=OPEN_HOURS + timedelta(seconds=1),
        qty=Decimal(str(qty)),
        price=Decimal(str(price)),
    )
    order.apply_fill(execution)
    portfolio.position(order.symbol).apply_fill(execution, execution.qty * order.side.sign)


# ── the submission path ─────────────────────────────────────────────────────


class TestSubmit:
    async def test_an_approved_order_reaches_the_venue(self) -> None:
        broker = FakeBroker()
        result = await router(broker).submit(request(), book())

        assert result.submitted
        assert result.order is not None
        assert result.order.status is OrderStatus.SUBMITTED
        assert result.order.broker_order_id == "brk-1"
        assert result.order.submitted_at == OPEN_HOURS
        assert broker.submit_calls == [result.order.client_order_id]

    async def test_timestamps_come_from_the_injected_clock(self) -> None:
        """Never the wall clock: a backtest and a live run must stamp orders the
        same way, and `datetime.now()` is the one call that guarantees they do
        not (CLAUDE.md §1.2)."""
        result = await router().submit(request(), book())

        assert result.order is not None
        assert result.order.submitted_at == OPEN_HOURS
        assert result.order.created_at == OPEN_HOURS  # the *decision* instant
        assert result.order.submitted_at is not None
        assert result.order.submitted_at.tzinfo is not None

    async def test_a_risk_denial_is_a_value_and_never_reaches_the_broker(self) -> None:
        broker = FakeBroker()
        result = await router(broker, chain(FakeKillSwitch(engaged=True))).submit(request(), book())

        assert not result.submitted
        assert result.decision.rule == "kill_switch"
        assert result.order is not None
        assert result.order.status is OrderStatus.REJECTED_RISK
        assert result.order.reject_reason == result.decision.reason
        # Who as well as why. The rule was logged and dropped before
        # `b8e3f01c7d24`, so a stored refusal could say "no price available for
        # SPY" without naming which of the three rules that check one said it.
        assert result.order.rejected_by == result.decision.rule == "kill_switch"
        assert broker.submit_calls == []

    async def test_a_venue_rejection_is_a_value_too(self) -> None:
        """An ordinary outcome — a halted symbol, a bad price. Not a crash, and
        not a reason to halt the platform."""
        broker = FakeBroker()
        broker.reject_next = "symbol is halted"
        switch = FakeKillSwitch()
        result = await router(broker, kill_switch=switch).submit(request(), book())

        assert not result.submitted
        assert result.order is not None
        assert result.order.status is OrderStatus.REJECTED
        assert result.order.reject_reason == "symbol is halted"
        # The venue is a refuser like a rule is, and the column carries it:
        # `status` says which vocabulary to read the name in.
        assert result.order.rejected_by == broker.name
        assert not switch.engaged

    async def test_a_misclassified_refusal_is_a_value_too(self) -> None:
        """The day-3 crash, as a test.

        A wash-trade rejection came back from Alpaca as `InsufficientFundsError`
        because the adapter read one 403 code as proof of a buying-power
        problem. That class was a sibling of `OrderRejectedError`, so this
        method's `except` missed it, and the exception unwound
        `submit_protective_orders`, `_protect` and `on_fill_event` and ended the
        trade-updates consumer — killing the worker over an outcome three frames
        below already knew how to handle
        (docs/paper-week/day-3-review.md, B2).

        The class is a subclass now. Whatever the adapter decides a refusal is
        called, the router turns it into a value.
        """
        broker = FakeBroker()
        broker.raise_next = InsufficientFundsError("Alpaca refused POST /v2/orders: 40310000")
        switch = FakeKillSwitch()

        result = await router(broker, kill_switch=switch).submit(request(), book())

        assert not result.submitted
        assert result.order is not None
        assert result.order.status is OrderStatus.REJECTED
        assert result.order.rejected_by == broker.name
        # A refusal is not an incident. Halting on one would stop every strategy
        # over a single order the venue declined.
        assert not switch.engaged

    async def test_an_unclassified_broker_error_is_a_refusal_not_a_crash(self) -> None:
        """`_refusal` still returns a bare `BrokerError` for a status it does not
        recognise, and a venue can invent a refusal tomorrow.

        Neither may end the task that books fills. The second lock: catch
        `BrokerError`, not only the subclasses this adapter happens to name
        today. `BrokerConnectionError` keeps its own arm above — it is the one
        broker error that is *not* a refusal, because the venue may have the
        order.
        """
        broker = FakeBroker()
        broker.raise_next = BrokerError("Alpaca POST /v2/orders returned 418: teapot")
        switch = FakeKillSwitch()

        result = await router(broker, kill_switch=switch).submit(request(), book())

        assert not result.submitted
        assert result.order is not None
        assert result.order.status is OrderStatus.REJECTED
        assert not switch.engaged

    async def test_a_transport_failure_still_takes_the_indeterminate_path(self) -> None:
        """The arm ordering, asserted rather than assumed.

        `BrokerConnectionError` is a `BrokerError`, so a single broad `except`
        would swallow it into the refusal branch and record an order as REJECTED
        that the venue may be holding — a lost order turned into a hidden one.
        It is caught first, and this is what stops that regressing.
        """
        broker = FakeBroker()
        broker.timeout_next = True
        broker.accept_on_timeout = True

        result = await router(broker).submit(request(), book())

        # Adopted from the venue, not written off as a refusal.
        assert result.order is not None
        assert result.order.status is not OrderStatus.REJECTED
        assert broker.submit_calls == [result.order.client_order_id]

    async def test_the_quantity_does_not_change_the_key(self) -> None:
        """A risk rule may shrink an order on its way through the chain. A key
        that moved with the quantity would let the shrunk order through as a
        second, different order at the venue."""
        big = await router().submit(request(qty=500), book())
        small = await router().submit(request(qty=7), book())

        assert big.order is not None and small.order is not None
        assert big.order.client_order_id == small.order.client_order_id

    async def test_a_notional_request_is_sized_rather_than_refused(self) -> None:
        result = await router().submit(request(qty=None, notional=5_000), book(SPY=(0, 100)))

        assert result.submitted
        assert result.order is not None
        assert result.order.qty == Decimal(50)

    async def test_a_notional_that_buys_nothing_is_not_an_order(self) -> None:
        result = await router().submit(request(qty=None, notional=10), book(SPY=(0, 100)))

        assert not result.submitted
        assert result.order is None
        assert result.decision.rule == NO_ACTION

    async def test_a_request_with_neither_quantity_nor_notional_is_refused(self) -> None:
        result = await router().submit(request(qty=None), book())

        assert not result.submitted
        assert result.decision.rule == SIZING

    def test_a_request_cannot_state_both_a_quantity_and_a_notional(self) -> None:
        with pytest.raises(ValueError, match="not both"):
            request(qty=100, notional=5_000)

    def test_a_request_needs_a_tz_aware_decision_instant(self) -> None:
        with pytest.raises(ValueError, match="tz-aware"):
            request(ts=datetime(2024, 1, 2, 15, 0))  # noqa: DTZ001

    async def test_an_order_shrunk_to_nothing_never_reaches_the_venue(self) -> None:
        """`RiskDecision.shrink` refuses a non-positive quantity at
        construction, but the plain constructor can reach it — and an order for
        nothing is not an order."""

        class ShrinkToZero:
            name = "shrink_to_zero"

            def check(self, order: Order, books: RiskBooks, lim: RiskLimits) -> RiskDecision:
                return RiskDecision(approved=True, adjusted_qty=Decimal(0))

        broker = FakeBroker()
        engine = RiskEngine(limits(), rules=[ShrinkToZero()])
        result = await router(broker, engine).submit(request(), book())

        assert not result.submitted
        assert result.order is not None
        assert result.order.status is OrderStatus.REJECTED_RISK
        # No rule refused this — the chain approved and left nothing to trade,
        # so the refuser is the routing stage that caught it. Naming the last
        # rule to vote would blame a rule that said yes.
        assert result.order.rejected_by == ROUTING
        assert broker.submit_calls == []

    async def test_the_router_refuses_an_order_it_did_not_build(self) -> None:
        """The single path starts at PENDING_RISK. An order arriving in any
        other state has either been round-tripped by hand or is a retry of an
        object, and a retry rebuilds from the request so the key is reused."""
        already_sent = Order(
            symbol="SPY", side=Side.BUY, qty=Decimal(10), status=OrderStatus.SUBMITTED
        )
        with pytest.raises(ExecutionError, match="single submission path"):
            await router()._route(already_sent, book())


class TestSubmitSignal:
    async def test_a_hold_is_not_a_rejection(self) -> None:
        """Reporting it as one would put phantom denials on the dashboard and
        inflate the count an operator reads to judge the risk config."""
        result = await router().submit_signal(signal(SignalAction.HOLD), book(), sizing())

        assert not result.submitted
        assert result.order is None
        assert result.decision.approved
        assert result.decision.rule == NO_ACTION

    async def test_an_exit_on_a_flat_position_does_nothing(self) -> None:
        result = await router().submit_signal(signal(SignalAction.EXIT), book(), sizing())

        assert result.decision.rule == NO_ACTION
        assert result.order is None

    async def test_an_exit_closes_exactly_what_is_held_and_is_never_sized(self) -> None:
        """The sizer produces a number; an exit needs *the* number."""
        result = await router().submit_signal(
            signal(SignalAction.EXIT), book(SPY=(137, 100)), sizing("fixed_qty", "10")
        )

        assert result.submitted
        assert result.order is not None
        assert result.order.side is Side.SELL
        assert result.order.qty == Decimal(137)

    async def test_an_exit_of_a_short_buys(self) -> None:
        result = await router().submit_signal(
            signal(SignalAction.EXIT), book(SPY=(-40, 100)), sizing()
        )

        assert result.order is not None
        assert result.order.side is Side.BUY
        assert result.order.qty == Decimal(40)

    @pytest.mark.parametrize("action", [SignalAction.SCALE_IN, SignalAction.SCALE_OUT])
    async def test_scaling_is_refused_exactly_as_the_backtest_refuses_it(
        self, action: SignalAction
    ) -> None:
        """The fraction to add or close is undefined. Guessing it here would
        make a live run and its own backtest disagree about one signal, which is
        the divergence the whole ports-and-adapters shape exists to prevent."""
        result = await router().submit_signal(signal(action), book(SPY=(100, 100)), sizing())

        assert not result.submitted
        assert result.decision.rule == ROUTING
        assert "not modelled" in result.decision.reason

    async def test_a_reversal_is_submitted_rather_than_refused(self) -> None:
        """An order larger than the position it opposes closes that position on
        its way through zero. Refusing it would trap the very holding the
        strategy is trying to reverse — and the backtest submits it."""
        result = await router().submit_signal(
            signal(SignalAction.ENTER_LONG), book(SPY=(-100, 100)), sizing("fixed_qty", "160")
        )

        assert result.submitted
        assert result.order is not None
        assert result.order.side is Side.BUY
        assert result.order.qty == Decimal(160)

    async def test_an_exit_and_a_short_entry_on_one_bar_are_two_orders(self) -> None:
        """Same strategy, same symbol, same instant, both SELL. Only `purpose`
        tells them apart, and without it the venue returns the first order for
        the second submit — leaving a strategy flat while it believes it is
        short."""
        broker = FakeBroker()
        routed = router(broker)
        portfolio = book(SPY=(100, 100))

        exited = await routed.submit_signal(signal(SignalAction.EXIT), portfolio, sizing())
        entered = await routed.submit_signal(
            signal(SignalAction.ENTER_SHORT), portfolio, sizing("fixed_qty", "50")
        )

        assert exited.order is not None and entered.order is not None
        assert exited.order.client_order_id != entered.order.client_order_id
        assert len(broker.submit_calls) == 2
        assert len(set(broker.submit_calls)) == 2

    async def test_the_worked_example_from_the_risk_doc_reproduces(self) -> None:
        """$100k equity, 1% risk. $50 entry with a $48 stop is 500 shares; with
        a $35 stop it is 66. Both lose about $1,000 if stopped, which is the
        entire argument for sizing by risk rather than by notional."""
        routed = router()
        spec = sizing("risk_pct", "0.01")

        tight = await routed.submit_signal(signal(stop=48), book(SPY=(0, 50)), spec)
        wide = await routed.submit_signal(signal(stop=35), book(SPY=(0, 50)), spec)

        assert tight.order is not None and tight.order.qty == Decimal(500)
        assert wide.order is not None and wide.order.qty == Decimal(66)

    async def test_risk_sizing_without_a_stop_refuses_instead_of_raising(self) -> None:
        """One strategy misconfigured is not the platform broken: it belongs on
        the dashboard, not in the runner's exception counter."""
        broker = FakeBroker()
        result = await router(broker).submit_signal(
            signal(stop=None), book(SPY=(0, 50)), sizing("risk_pct", "0.01")
        )

        assert not result.submitted
        assert result.order is None
        assert result.decision.rule == SIZING
        assert "stop" in result.decision.reason
        assert broker.submit_calls == []

    async def test_an_unpriceable_symbol_refuses_before_an_order_exists(self) -> None:
        result = await router().submit_signal(signal(), book(), sizing("equity_pct", "0.1"))

        assert result.order is None
        assert result.decision.rule == SIZING

    async def test_sizing_and_the_chain_price_the_trade_the_same_way(self) -> None:
        """Sizing against one price and validating against another is invisible
        — both numbers look right on their own — and produces an order sized to
        sit just inside a limit that then refuses it."""
        result = await router(engine=chain()).submit_signal(
            signal(limit=40), book(cash=100_000, SPY=(0, 60)), sizing("equity_pct", "0.05")
        )

        # 5% of 100k at the *limit* price of 40 is 125 shares, not 83 at the
        # mark of 60. The chain then values the same order at 40 as well.
        assert result.submitted
        assert result.order is not None
        assert result.order.qty == Decimal(125)


# ── the indeterminate submit ────────────────────────────────────────────────


class TestTransportFailure:
    async def test_a_lost_response_is_resolved_without_halting(self) -> None:
        """The common case: the venue has the order, only the reply went
        missing. One lookup settles it and nothing needs to stop."""
        broker = FakeBroker()
        broker.timeout_next = True
        broker.accept_on_timeout = True
        switch = FakeKillSwitch()

        result = await router(broker, kill_switch=switch).submit(request(), book())

        assert result.submitted
        assert result.order is not None
        assert result.order.status is OrderStatus.SUBMITTED
        assert result.order.broker_order_id == "brk-1"
        assert not switch.engaged
        assert len(broker.submit_calls) == 1

    async def test_an_unresolvable_submit_halts_and_never_sends_twice(self) -> None:
        """The case the whole design turns on. We do not know whether the venue
        has it, so a second send is how one intended position becomes two."""
        broker = FakeBroker()
        broker.timeout_next = True
        switch = FakeKillSwitch()
        routed = router(broker, kill_switch=switch)
        portfolio = book()

        with pytest.raises(BrokerConnectionError, match="not resubmitting"):
            await routed.submit(request(), portfolio)

        assert len(broker.submit_calls) == 1
        assert switch.engaged
        scope, reason, by, _detail = switch.engagements[0]
        assert "global" in scope
        assert "broker_unreachable" in reason
        assert by == "order_router"

    async def test_an_unresolvable_submit_impugns_its_own_symbol_and_no_other(self) -> None:
        """ADR 0029. The halt is global and the impugnment is one ticker, and
        both are right for different reasons.

        What stops trading is global: a transport we cannot trust says nothing
        good about the next order either. What we cannot *prove* is this order's
        symbol — its outcome is unknown, so `Position.qty` for SPY may be wrong
        by exactly its quantity and an exit sized against it is sized against
        the number in doubt. Every other symbol still closes normally, which is
        what keeps a halt from being a book-wide freeze on exits.
        """
        broker = FakeBroker()
        broker.timeout_next = True
        switch = FakeKillSwitch()

        with pytest.raises(BrokerConnectionError):
            await router(broker, kill_switch=switch).submit(request(), book())

        state = switch.halt_state()
        assert state.position_is_unproven("SPY")
        assert not state.position_is_unproven("QQQ")

    async def test_an_unresolvable_submit_leaves_the_order_where_it_really_is(self) -> None:
        """`PENDING_SUBMIT` — approved, not yet acknowledged — is literally the
        truth, and it carries the deterministic key reconciliation needs to
        settle the order against the venue without risking a duplicate."""
        broker = FakeBroker()
        broker.timeout_next = True
        routed = router(broker)
        built: list[Order] = []

        # Capture the order the router built by watching what it tried to send.
        original = broker.submit_order

        async def capture(order: Order) -> Order:
            built.append(order)
            return await original(order)

        broker.submit_order = capture  # type: ignore[method-assign]

        with pytest.raises(BrokerConnectionError):
            await routed.submit(request(), book())

        assert len(built) == 1
        assert built[0].status is OrderStatus.PENDING_SUBMIT
        assert built[0].client_order_id.startswith("atp-")

    async def test_a_lookup_that_cannot_answer_does_not_loop(self) -> None:
        broker = FakeBroker()
        broker.timeout_next = True
        broker.reads_fail = True

        with pytest.raises(BrokerConnectionError):
            await router(broker).submit(request(), book())

        assert len(broker.submit_calls) == 1

    async def test_it_still_raises_without_a_kill_switch(self) -> None:
        """The switch is optional; stopping is not."""
        broker = FakeBroker()
        broker.timeout_next = True

        with pytest.raises(BrokerConnectionError):
            await router(broker).submit(request(), book())

    async def test_an_unresolvable_submit_never_flattens(self) -> None:
        """Halting stops new risk. Flattening against a position that may not
        exist opens a short (docs/RISK.md, "Halting is not flattening")."""
        broker = FakeBroker()
        broker.timeout_next = True

        with pytest.raises(BrokerConnectionError):
            await router(broker, kill_switch=FakeKillSwitch()).submit(request(), book())

        assert broker.positions == {}

    async def test_a_retry_of_the_same_request_is_one_order_at_the_venue(self) -> None:
        """The payoff for deriving the key from the decision: the caller may
        retry, and the venue deduplicates."""
        broker = FakeBroker()
        broker.timeout_next = True
        broker.accept_on_timeout = True
        portfolio = book()
        pinned = request()

        first = await router(broker).submit(pinned, portfolio)
        second = await router(broker).submit(pinned, portfolio)

        assert first.order is not None and second.order is not None
        assert first.order.client_order_id == second.order.client_order_id
        assert len(broker.accepted) == 1


# ── protective orders ───────────────────────────────────────────────────────


class TestProtectiveOrders:
    async def _entry(
        self,
        broker: FakeBroker,
        portfolio: Portfolio,
        routed: OrderRouter,
        *,
        qty: float = 100,
        stop: float | None = 95,
        side: Side = Side.BUY,
    ) -> Order:
        result = await routed.submit(
            request(
                side=side,
                qty=qty,
                stop_loss_price=Decimal(str(stop)) if stop is not None else None,
            ),
            portfolio,
        )
        assert result.order is not None
        return result.order

    async def test_a_stop_is_placed_over_the_quantity_that_filled(self) -> None:
        """Not over the quantity ordered. A stop for 100 against a 40-share fill
        sells 40 it owns and 60 it does not, opening a short on trigger."""
        broker, portfolio, routed = FakeBroker(), book(), router()
        routed.broker = broker
        entry = await self._entry(broker, portfolio, routed)
        fill(entry, portfolio, 40, 100)

        protection = await routed.submit_protective_orders(entry, portfolio)

        assert protection.is_fully_protected
        stop = protection.stop_order
        assert stop is not None
        assert stop.qty == Decimal(40)
        assert stop.side is Side.SELL
        assert stop.order_type is OrderType.STOP
        assert stop.stop_price == Decimal(95)
        assert stop.time_in_force is TimeInForce.GTC
        assert stop.parent_order_id == entry.id

    async def test_each_partial_fill_gets_its_own_stop(self) -> None:
        """Two equal partials are the ordinary case and the one a key derived
        from the increment collapses into a single order, leaving the second
        tranche naked while the router reports success."""
        broker, portfolio = FakeBroker(), book()
        routed = router(broker)
        entry = await self._entry(broker, portfolio, routed, qty=200)

        fill(entry, portfolio, 100, 100)
        first = await routed.submit_protective_orders(entry, portfolio)
        fill(entry, portfolio, 100, 100)
        second = await routed.submit_protective_orders(entry, portfolio)

        assert first.covered_qty == Decimal(100)
        assert second.covered_qty == Decimal(100)
        assert first.stop_order is not None and second.stop_order is not None
        assert first.stop_order.client_order_id != second.stop_order.client_order_id
        assert broker.open_order_count("SPY", Side.SELL) == 2
        assert routed._protected_qty("SPY", Side.SELL) == Decimal(200)

    async def test_a_replayed_fill_event_places_nothing(self) -> None:
        """Reconnects replay history — that is why `is_stale_event` exists — and
        a second stop against shares already covered would flip the position
        short when it triggers."""
        broker, portfolio = FakeBroker(), book()
        routed = router(broker)
        entry = await self._entry(broker, portfolio, routed)
        fill(entry, portfolio, 100, 100)

        await routed.submit_protective_orders(entry, portfolio)
        replay = await routed.submit_protective_orders(entry, portfolio)

        assert replay.placed == []
        assert replay.covered_qty == Decimal(0)
        assert broker.open_order_count("SPY", Side.SELL) == 1

    async def test_a_short_entry_is_protected_by_a_buy_stop_above(self) -> None:
        broker, portfolio = FakeBroker(), book()
        routed = router(broker)
        entry = await self._entry(broker, portfolio, routed, side=Side.SELL, stop=105)
        fill(entry, portfolio, 100, 100)

        stop = (await routed.submit_protective_orders(entry, portfolio)).stop_order

        assert stop is not None
        assert stop.side is Side.BUY
        assert stop.stop_price == Decimal(105)

    async def test_a_denied_stop_cannot_be_mistaken_for_a_placed_one(self) -> None:
        """Three of the nine rules refuse a protective stop without consulting
        whether it reduces a position, so this is ordinary rather than exotic —
        and a `list[Order]` return would report it as an empty list, exactly as
        it reports a position that needed no protection."""
        broker, portfolio = FakeBroker(), book()
        refusal = Refusing()
        routed = router(broker, chain(extra=[refusal]))
        entry = await self._entry(broker, portfolio, routed)
        fill(entry, portfolio, 100, 100)
        refusal.engaged = True

        protection = await routed.submit_protective_orders(entry, portfolio)

        assert not protection.is_fully_protected
        assert protection.unprotected_qty == Decimal(100)
        assert protection.stop_order is None
        assert protection.refused[0].decision.rule == "refuses_everything"
        # Armed anyway, so something is watching even though nothing is resting
        # at the venue.
        assert protection.engine_side_stop == Decimal(95)
        assert portfolio.position("SPY").stop_loss_price == Decimal(95)

    async def test_a_denied_stop_can_be_retried_into_one_order(self) -> None:
        """Booking a refusal as covered would make the retry a no-op and leave
        the position naked for good."""
        broker, portfolio = FakeBroker(), book()
        refusal = Refusing(engaged=True)
        routed = router(broker, chain(extra=[refusal]))
        entry = await self._entry(broker, portfolio, routed)
        fill(entry, portfolio, 100, 100)

        refused = await routed.submit_protective_orders(entry, portfolio)
        refusal.engaged = False
        retried = await routed.submit_protective_orders(entry, portfolio)

        assert not refused.is_fully_protected
        assert retried.is_fully_protected
        assert retried.covered_qty == Decimal(100)
        assert broker.open_order_count("SPY", Side.SELL) == 1

    async def test_a_position_with_no_stop_at_all_is_reported_unprotected(self) -> None:
        broker, portfolio = FakeBroker(), book()
        routed = router(broker)
        entry = await self._entry(broker, portfolio, routed, stop=None)
        fill(entry, portfolio, 100, 100)

        protection = await routed.submit_protective_orders(entry, portfolio)

        assert not protection.is_fully_protected
        assert protection.unprotected_qty == Decimal(100)
        assert broker.open_order_count("SPY", Side.SELL) == 0

    async def test_a_stop_config_derives_the_level_from_the_actual_fill(self) -> None:
        """`risk_pct` sizing is defined off |entry − stop|, so a level anchored
        to a price we did not get silently stops meaning what the sizer
        assumed."""
        broker, portfolio = FakeBroker(), book()
        routed = router(broker)
        entry = await self._entry(broker, portfolio, routed, stop=None)
        fill(entry, portfolio, 100, 101.5)

        protection = await routed.submit_protective_orders(
            entry,
            portfolio,
            stop_config=StopConfig(stop_type=StopType.FIXED_PCT, value=Decimal("0.02")),
        )

        stop = protection.stop_order
        assert stop is not None
        # 2% below 101.50, not below the 100 the signal expected.
        assert stop.stop_price == Decimal("99.47")

    async def test_a_take_profit_is_armed_and_not_sent_to_the_venue(self) -> None:
        """`BrokerPort` has no bracket. A second live order for the same shares
        would open a fresh position on the opposite side when the first one
        fills, and nothing in this item can cancel the loser."""
        broker, portfolio = FakeBroker(), book()
        routed = router(broker)
        entry = await self._entry(broker, portfolio, routed, stop=None)
        fill(entry, portfolio, 100, 100)

        await routed.submit_protective_orders(
            entry,
            portfolio,
            stop_config=StopConfig(stop_type=StopType.FIXED_PCT, value=Decimal("0.02")),
        )

        assert portfolio.position("SPY").take_profit_price == Decimal(102)
        assert broker.open_order_count("SPY", Side.SELL) == 1

    async def test_an_atr_stop_does_not_invent_a_take_profit(self) -> None:
        """`take_profit_level` raises for a type that is not a fixed distance
        from entry, and raising inside a fill handler on a freshly opened
        position is the worst place in the system for it."""
        broker, portfolio = FakeBroker(), book()
        routed = router(broker)
        entry = await self._entry(broker, portfolio, routed, stop=None)
        fill(entry, portfolio, 100, 100)

        protection = await routed.submit_protective_orders(
            entry,
            portfolio,
            stop_config=StopConfig(stop_type=StopType.ATR, multiplier=Decimal(2)),
            atr_value=Decimal(3),
        )

        assert protection.is_fully_protected
        assert protection.stop_order is not None
        assert protection.stop_order.stop_price == Decimal(94)
        assert portfolio.position("SPY").take_profit_price is None

    async def test_a_second_tranche_never_widens_the_armed_stop(self) -> None:
        """Moving a stop away from price to avoid being hit converts a planned
        small loss into an unplanned large one, and it always feels justified at
        the time."""
        broker, portfolio = FakeBroker(), book()
        routed = router(broker)
        entry = await self._entry(broker, portfolio, routed, qty=200, stop=None)
        tight = StopConfig(stop_type=StopType.FIXED_PCT, value=Decimal("0.02"))

        fill(entry, portfolio, 100, 100)
        await routed.submit_protective_orders(entry, portfolio, stop_config=tight)
        assert portfolio.position("SPY").stop_loss_price == Decimal(98)

        # A worse fill computes a looser level; the position keeps the tighter.
        fill(entry, portfolio, 100, 90)
        await routed.submit_protective_orders(entry, portfolio, stop_config=tight)

        assert portfolio.position("SPY").stop_loss_price == Decimal(98)

    async def test_a_protective_child_does_not_spawn_a_stop_of_its_own(self) -> None:
        """The fill stream carries entries and protective children alike and
        cannot tell them apart. This can."""
        broker, portfolio = FakeBroker(), book()
        routed = router(broker)
        entry = await self._entry(broker, portfolio, routed)
        fill(entry, portfolio, 100, 100)
        child = (await routed.submit_protective_orders(entry, portfolio)).stop_order
        assert child is not None

        again = await routed.submit_protective_orders(child, portfolio)

        assert again.placed == []
        assert broker.open_order_count("SPY", Side.SELL) == 1

    async def test_a_flat_position_is_not_given_a_stop(self) -> None:
        broker, portfolio = FakeBroker(), book()
        routed = router(broker)
        entry = await self._entry(broker, portfolio, routed)

        protection = await routed.submit_protective_orders(entry, portfolio)

        assert protection.placed == []
        assert protection.unprotected_qty == Decimal(0)

    async def test_a_reversal_is_not_over_covered(self) -> None:
        """A buy through a short fills more than the position it leaves behind.
        A stop for the whole fill would sell the new long and open a short
        underneath it."""
        broker, portfolio = FakeBroker(), book(SPY=(-60, 100))
        routed = router(broker)
        entry = await self._entry(broker, portfolio, routed, qty=160)
        fill(entry, portfolio, 160, 100)
        assert portfolio.position("SPY").qty == Decimal(100)

        protection = await routed.submit_protective_orders(entry, portfolio)

        assert protection.covered_qty == Decimal(100)
        assert routed._protected_qty("SPY", Side.SELL) == Decimal(100)


class TestReversals:
    """A fill that flips a position through zero.

    `Position.apply_fill` clears protective levels only at exactly flat, and a
    flip never passes through flat — so everything the old side left behind is
    still there, pointing the wrong way. Every test here failed before the fix,
    and each failure was silent: the result object said the position was fully
    protected.
    """

    async def _open(
        self, routed: OrderRouter, portfolio: Portfolio, side: Side, qty: float, stop: float
    ) -> Order:
        result = await routed.submit(
            request(side=side, qty=qty, stop_loss_price=Decimal(str(stop))), portfolio
        )
        assert result.order is not None
        fill(result.order, portfolio, qty, 100)
        await routed.submit_protective_orders(result.order, portfolio)
        return result.order

    async def test_the_old_side_stop_is_cancelled_rather_than_counted(self) -> None:
        """A buy stop over a short does not protect the long that short became —
        it doubles it. Counting it as coverage left 60 of 100 shares naked and
        reported `is_fully_protected`."""
        broker, portfolio = FakeBroker(), book()
        routed = router(broker)
        await self._open(routed, portfolio, Side.SELL, 60, 105)
        assert broker.open_stops("SPY", Side.BUY) == 1

        reversal = await routed.submit(
            request(side=Side.BUY, qty=160, stop_loss_price=Decimal(95)), portfolio
        )
        assert reversal.order is not None
        fill(reversal.order, portfolio, 160, 100)
        assert portfolio.position("SPY").qty == Decimal(100)

        protection = await routed.submit_protective_orders(reversal.order, portfolio)

        assert protection.covered_qty == Decimal(100)
        assert protection.is_fully_protected
        # The stale buy stop is gone, not merely ignored: left working it would
        # have added 60 more long at 105.
        assert broker.open_stops("SPY", Side.BUY) == 0
        assert broker.open_stops("SPY", Side.SELL) == 1

    async def test_a_flip_to_short_is_protected_rather_than_silently_skipped(self) -> None:
        """The mirror. The old long's sell stop filled the budget, `increment`
        came out zero, and the method returned an empty result that reads
        exactly like "nothing to do"."""
        broker, portfolio = FakeBroker(), book()
        routed = router(broker)
        await self._open(routed, portfolio, Side.BUY, 100, 95)

        reversal = await routed.submit(
            request(side=Side.SELL, qty=200, stop_loss_price=Decimal(105)), portfolio
        )
        assert reversal.order is not None
        fill(reversal.order, portfolio, 200, 100)
        assert portfolio.position("SPY").qty == Decimal(-100)

        protection = await routed.submit_protective_orders(reversal.order, portfolio)

        assert protection.is_fully_protected
        assert protection.covered_qty == Decimal(100)
        stop = protection.stop_order
        assert stop is not None
        assert stop.side is Side.BUY
        assert stop.stop_price == Decimal(105)
        assert broker.open_stops("SPY", Side.SELL) == 0

    async def test_a_stale_armed_level_is_not_treated_as_the_tighter_stop(self) -> None:
        """A long's 95 beats a short's proposed 105 on the `min` that enforces
        "never widen", arming a buy stop five points below a short entered at
        100 — a level the market has already passed."""
        broker, portfolio = FakeBroker(), book()
        routed = router(broker)
        entry = await routed.submit(
            request(side=Side.BUY, qty=100, stop_loss_price=Decimal(95)), portfolio
        )
        assert entry.order is not None
        fill(entry.order, portfolio, 100, 100)
        portfolio.position("SPY").stop_loss_price = Decimal(95)  # armed, child refused

        reversal = await routed.submit(
            request(side=Side.SELL, qty=200, stop_loss_price=Decimal(105)), portfolio
        )
        assert reversal.order is not None
        fill(reversal.order, portfolio, 200, 100)

        protection = await routed.submit_protective_orders(reversal.order, portfolio)

        assert portfolio.position("SPY").stop_loss_price == Decimal(105)
        assert protection.engine_side_stop == Decimal(105)

    async def test_a_stop_the_market_has_passed_is_refused_not_armed(self) -> None:
        """A reversal that only partly fills leaves the position on the *old*
        side while the level belongs to the side being opened. Submitted, that
        is a market order in disguise; armed, it triggers on the next bar."""
        broker, portfolio = FakeBroker(), book(SPY=(-60, 100))
        routed = router(broker)
        entry = await routed.submit(
            request(side=Side.BUY, qty=160, stop_loss_price=Decimal(95)), portfolio
        )
        assert entry.order is not None
        fill(entry.order, portfolio, 40, 100)
        assert portfolio.position("SPY").qty == Decimal(-20)  # still short

        protection = await routed.submit_protective_orders(entry.order, portfolio)

        assert not protection.is_fully_protected
        assert protection.unprotected_qty == Decimal(20)
        assert protection.stop_order is None
        assert protection.engine_side_stop is None
        assert portfolio.position("SPY").stop_loss_price is None
        assert broker.open_stops("SPY") == 0

    async def test_a_protective_stop_passes_a_chain_that_blocks_every_entry(self) -> None:
        """The exit carve-out reaching the path that matters most: down 40% on
        the day with no cash, the stop that closes the position still goes."""
        broker = FakeBroker()
        routed = router(broker, chain(anchor=Decimal(1_000_000)))
        portfolio = book(cash=0)
        entry = Order(
            symbol="SPY",
            side=Side.BUY,
            qty=Decimal(500),
            client_order_id="atp-entry",
            status=OrderStatus.SUBMITTED,
        )
        fill(entry, portfolio, 500, 100)
        routed._requested_protection[entry.id] = (Decimal(95), None)

        protection = await routed.submit_protective_orders(entry, portfolio)

        assert protection.is_fully_protected
        # And the same book refuses an entry, so the pass above is the carve-out
        # rather than a chain that approves everything.
        entering = await routed.submit(request(qty=10), portfolio)
        assert not entering.submitted


# ── cancellation and flatten ────────────────────────────────────────────────


class TestCancelAll:
    async def test_it_cancels_an_order_this_router_never_submitted(self) -> None:
        """The orphan stop left by a restart. A cancel-all driven from local
        bookkeeping would leave precisely the orders it does not know about,
        which after a restart are the ones most likely to be there."""
        broker = FakeBroker()
        await broker.submit_order(
            Order(symbol="SPY", side=Side.SELL, qty=Decimal(100), client_order_id="atp-orphan")
        )

        assert await router(broker).cancel_all() == 1
        assert broker.cancelled == ["brk-1"]

    async def test_it_is_symbol_scoped_when_asked(self) -> None:
        broker = FakeBroker()
        for symbol in ("SPY", "QQQ"):
            await broker.submit_order(
                Order(
                    symbol=symbol,
                    side=Side.SELL,
                    qty=Decimal(10),
                    client_order_id=f"atp-{symbol}",
                )
            )

        assert await router(broker).cancel_all("SPY") == 1
        assert broker.open_order_count("QQQ") == 1

    async def test_every_target_is_attempted_before_anything_is_raised(self) -> None:
        """One stuck order must not abandon the other nine, and a count
        implying ten when three are still live is worse than an exception."""
        broker = FakeBroker()
        for i in range(3):
            await broker.submit_order(
                Order(symbol="SPY", side=Side.SELL, qty=Decimal(10), client_order_id=f"atp-{i}")
            )

        attempted: list[str] = []
        real_cancel = broker.cancel_order

        async def flaky(broker_order_id: str) -> None:
            attempted.append(broker_order_id)
            if broker_order_id == "brk-2":
                raise BrokerConnectionError("timed out")
            await real_cancel(broker_order_id)

        broker.cancel_order = flaky  # type: ignore[method-assign]

        with pytest.raises(ExecutionError, match="cancelled 2 of 3"):
            await router(broker).cancel_all()

        assert attempted == ["brk-1", "brk-2", "brk-3"]


class TestFlatten:
    async def test_it_goes_through_the_risk_chain_not_around_it(self) -> None:
        """`BrokerPort.close_position` would reach the venue without passing
        `validate()`, which is the bypass ADR 0005 exists to refuse.

        `close_calls` is the assertion, and it is the point of this case rather
        than a detail of it: the fake implements both close methods now, because
        the one path ADR 0005 carves out for them — `POST /risk/flatten-all` —
        is built and needs a fake that answers. So the bypass is caught here, by
        name, instead of by the fake raising from under whoever took it.
        """
        broker = FakeBroker()
        result = await router(broker, chain()).flatten("SPY", book(SPY=(100, 100)))

        assert broker.close_calls == []
        assert result.submitted
        assert result.order is not None
        assert result.order.side is Side.SELL
        assert result.order.qty == Decimal(100)
        assert result.order.order_type is OrderType.MARKET
        assert result.order.time_in_force is TimeInForce.GTC

    async def test_flattening_a_flat_symbol_does_not_invent_a_position(self) -> None:
        """`Portfolio.position()` is a `setdefault`; using it to inspect a book
        writes to it."""
        portfolio = book()
        result = await router().flatten("SPY", portfolio)

        assert result.decision.rule == NO_ACTION
        assert portfolio.positions == {}

    async def test_it_cancels_protection_only_after_the_close_is_accepted(self) -> None:
        broker, portfolio = FakeBroker(), book()
        routed = router(broker)
        entry_result = await routed.submit(request(stop_loss_price=Decimal(95)), portfolio)
        entry = entry_result.order
        assert entry is not None
        fill(entry, portfolio, 100, 100)
        stop = (await routed.submit_protective_orders(entry, portfolio)).stop_order
        assert stop is not None

        result = await routed.flatten("SPY", portfolio)

        assert result.submitted
        assert stop.broker_order_id in broker.cancelled
        assert not routed.has_broker_side_protection("SPY", portfolio.position("SPY"))

    async def test_a_stop_that_will_not_cancel_stays_tracked_and_does_not_abort_the_rest(
        self,
    ) -> None:
        """Popping the children before cancelling them puts any that fail beyond
        the reach of every later call: the router reports no protection while
        the venue still holds a live stop, and a retried flatten cancels nothing
        while adding a second market close."""
        broker, portfolio = FakeBroker(), book()
        routed = router(broker)
        entry = await routed.submit(request(qty=200, stop_loss_price=Decimal(95)), portfolio)
        assert entry.order is not None
        fill(entry.order, portfolio, 100, 100)
        await routed.submit_protective_orders(entry.order, portfolio)
        fill(entry.order, portfolio, 100, 100)
        await routed.submit_protective_orders(entry.order, portfolio)
        assert broker.open_stops("SPY") == 2

        attempted: list[str] = []
        real_cancel = broker.cancel_order

        async def flaky(broker_order_id: str) -> None:
            attempted.append(broker_order_id)
            if broker_order_id == "brk-2":
                raise BrokerConnectionError("timed out")
            await real_cancel(broker_order_id)

        broker.cancel_order = flaky  # type: ignore[method-assign]

        cancelled = await routed.cancel_protection("SPY")

        # The second stop was still attempted, and the one that failed is still
        # known to the router rather than orphaned.
        assert attempted == ["brk-2", "brk-3"]
        assert cancelled == 1
        assert routed._protected_qty("SPY", Side.SELL) == Decimal(100)

    async def test_a_refused_flatten_leaves_the_stop_working(self) -> None:
        """Cancel-first has a path ending in position open, stop cancelled,
        close refused. A refused flatten must leave the position protected."""
        broker, portfolio = FakeBroker(), book()
        refusal = Refusing()
        routed = router(broker, chain(extra=[refusal]))
        entry_result = await routed.submit(request(stop_loss_price=Decimal(95)), portfolio)
        entry = entry_result.order
        assert entry is not None
        fill(entry, portfolio, 100, 100)
        await routed.submit_protective_orders(entry, portfolio)
        refusal.engaged = True

        result = await routed.flatten("SPY", portfolio)

        assert not result.submitted
        assert result.decision.rule == "refuses_everything"
        assert broker.cancelled == []
        assert routed.has_broker_side_protection("SPY", portfolio.position("SPY"))


# ── the venue's acknowledgement ─────────────────────────────────────────────


class TestAcknowledgement:
    async def test_an_ack_reporting_a_fill_leaves_the_order_submitted(self) -> None:
        """Adopting `FILLED` with no `Fill` to justify it would report
        `filled_qty` of zero to everything computing a position delta — a
        position appearing to vanish, which is the failure the state machine
        exists to prevent. The trade-updates stream owns the fill."""
        broker = FakeBroker()
        real_submit = broker.submit_order

        async def acked_as_filled(order: Order) -> Order:
            held = await real_submit(order)
            held.status = OrderStatus.FILLED
            return held

        broker.submit_order = acked_as_filled  # type: ignore[method-assign]

        result = await router(broker).submit(request(), book())

        assert result.submitted
        assert result.order is not None
        assert result.order.status is OrderStatus.SUBMITTED
        assert result.order.filled_qty == Decimal(0)

    async def test_an_ack_reporting_a_rejection_is_not_a_submission(self) -> None:
        """`submitted` means the venue has the order *working*, not that the
        call returned. Read as the latter, `flatten` cancels a position's stop
        against a close the venue refused — leaving it open and naked.

        Reachable without contriving anything: the port requires idempotency on
        `client_order_id`, and an adapter implements that by returning the
        venue's existing copy, which may already be terminal."""
        broker, portfolio = FakeBroker(), book()
        routed = router(broker)
        entry = await routed.submit(request(stop_loss_price=Decimal(95)), portfolio)
        assert entry.order is not None
        fill(entry.order, portfolio, 100, 100)
        stop = (await routed.submit_protective_orders(entry.order, portfolio)).stop_order
        assert stop is not None

        real_submit = broker.submit_order

        async def acked_as_rejected(order: Order) -> Order:
            held = await real_submit(order)
            held.status = OrderStatus.REJECTED
            held.reject_reason = "insufficient buying power"
            return held

        broker.submit_order = acked_as_rejected  # type: ignore[method-assign]
        result = await routed.flatten("SPY", portfolio)

        assert not result.submitted
        assert broker.cancelled == []
        # The third way a venue refusal reaches an order, and it names its
        # refuser like the other two. The venue's own copy carries no broker
        # name — only the router knows which venue it submitted to.
        assert result.order is not None
        assert result.order.rejected_by == broker.name
        assert result.order.reject_reason == "insufficient buying power"
        assert routed.has_broker_side_protection("SPY", portfolio.position("SPY"))

    async def test_an_ack_reporting_a_rejection_is_adopted(self) -> None:
        broker = FakeBroker()
        real_submit = broker.submit_order

        async def acked_as_rejected(order: Order) -> Order:
            held = await real_submit(order)
            held.status = OrderStatus.REJECTED
            held.reject_reason = "insufficient buying power"
            return held

        broker.submit_order = acked_as_rejected  # type: ignore[method-assign]

        result = await router(broker).submit(request(), book())

        assert result.order is not None
        assert result.order.status is OrderStatus.REJECTED
        assert result.order.reject_reason == "insufficient buying power"


# ── the venue tick ──────────────────────────────────────────────────────────


class TestPricesAreQuantisedToTheVenueTick:
    """Day 2 of the paper week placed 85 protective stops and had 85 rejected.

    Every one carried a price like `88.21881579149739452` and came back
    *"sub-penny increment does not fulfill minimum pricing criteria"*. For ten
    hours, across all 20 symbols, no position held a broker-side stop — the one
    condition docs/SAFETY.md's go-live gate exists to forbid.

    The cause is worth stating exactly, because it is not a rule being broken.
    An ATR is a float statistic; `runner.py` converts it with
    `Decimal(str(value))`, which is rule §1.1 followed correctly and which
    faithfully preserves all seventeen of the float's digits. **Obeying §1.1 is
    what produced the off-tick price.** Decimal exactness and venue tick
    conformance are two different constraints and the rule only names the first.

    So these tests are about the second one: a price that leaves the platform is
    a multiple of the instrument's tick, and it is rounded *away* from the
    market so that quantising can never tighten a stop into it.
    """

    async def _entry(
        self,
        broker: FakeBroker,
        portfolio: Portfolio,
        routed: OrderRouter,
        *,
        side: Side = Side.BUY,
    ) -> Order:
        result = await routed.submit(request(side=side, qty=100), portfolio)
        assert result.order is not None
        return result.order

    async def test_the_price_the_venue_refused_is_now_on_tick(self) -> None:
        """The exact number from the day-2 review, reproduced and then fixed.

        `Decimal('88.39') - 2 * Decimal(str(0.08559210425130274))` is
        byte-for-byte what Alpaca refused. The assertion is the one the review
        asked for: what reaches the broker carries exactly two decimal places.
        """
        atr = Decimal(str(0.08559210425130274))
        broker, portfolio = FakeBroker(), book()
        routed = router(broker)
        entry = await self._entry(broker, portfolio, routed)
        fill(entry, portfolio, 100, 88.39)

        protection = await routed.submit_protective_orders(
            entry,
            portfolio,
            stop_config=StopConfig(stop_type=StopType.ATR, multiplier=Decimal(2)),
            atr_value=atr,
        )

        stop = protection.stop_order
        assert stop is not None
        assert stop.stop_price is not None
        # The level before quantising, kept here so this test fails loudly if
        # the stop maths changes underneath it rather than passing vacuously.
        assert Decimal("88.39") - Decimal(2) * atr == Decimal("88.21881579149739452")
        assert stop.stop_price == Decimal("88.21")
        # `brokers/alpaca.py` serialises with `str()`, so this is the wire.
        assert str(stop.stop_price) == "88.21"
        assert stop.stop_price.as_tuple().exponent == -2

    async def test_a_long_s_stop_rounds_down_and_never_up(self) -> None:
        """Away from the market, not to nearest.

        `88.2188…` is nearer to `88.22`, and `88.22` is a cent *closer* to a
        long's entry — a stop quantising pulled tighter than the platform chose.
        The one place that difference is collected is a gap, which is the only
        place a stop matters.
        """
        broker, portfolio = FakeBroker(), book()
        routed = router(broker)
        entry = await self._entry(broker, portfolio, routed)
        fill(entry, portfolio, 100, 88.39)

        protection = await routed.submit_protective_orders(
            entry,
            portfolio,
            stop_config=StopConfig(stop_type=StopType.ATR, multiplier=Decimal(2)),
            atr_value=Decimal(str(0.08559210425130274)),
        )

        stop = protection.stop_order
        assert stop is not None
        assert stop.side is Side.SELL
        assert stop.stop_price == Decimal("88.21")  # floor, not the nearer 88.22

    async def test_a_short_s_stop_rounds_up_and_never_down(self) -> None:
        """The mirror, and the reason the direction is read off the side rather
        than hard-coded: a short's stop sits *above* the market, so away from it
        is up."""
        broker, portfolio = FakeBroker(), book()
        routed = router(broker)
        entry = await self._entry(broker, portfolio, routed, side=Side.SELL)
        fill(entry, portfolio, 100, 88.39)

        protection = await routed.submit_protective_orders(
            entry,
            portfolio,
            stop_config=StopConfig(stop_type=StopType.ATR, multiplier=Decimal(2)),
            atr_value=Decimal(str(0.08559210425130274)),
        )

        stop = protection.stop_order
        assert stop is not None
        assert stop.side is Side.BUY
        # 88.39 + 2 × atr = 88.5611842…, ceiled away from the market.
        assert stop.stop_price == Decimal("88.57")

    async def test_the_armed_level_is_the_level_the_venue_holds(self) -> None:
        """Otherwise the engine watches one price, the venue holds another and
        the dashboard draws a third, differing by up to a tick — a discrepancy
        no single screen could explain."""
        broker, portfolio = FakeBroker(), book()
        routed = router(broker)
        entry = await self._entry(broker, portfolio, routed)
        fill(entry, portfolio, 100, 88.39)

        protection = await routed.submit_protective_orders(
            entry,
            portfolio,
            stop_config=StopConfig(stop_type=StopType.ATR, multiplier=Decimal(2)),
            atr_value=Decimal(str(0.08559210425130274)),
        )

        stop = protection.stop_order
        assert stop is not None
        assert portfolio.position("SPY").stop_loss_price == stop.stop_price

    async def test_a_sell_limit_never_asks_less_and_a_buy_limit_never_bids_more(
        self,
    ) -> None:
        """A limit rounds the *opposite* way to a stop, which is the easiest
        thing in this module to get backwards.

        A stop rounds away from the market so it cannot be tightened. A limit
        rounds away from a worse price: a sell that ceils never asks less than
        it meant to, a buy that floors never pays more than risk sized it for.
        The cost is a fill that may not happen, which is the cheaper mistake.
        """
        broker, portfolio = FakeBroker(), book()
        routed = router(broker)

        sell = await routed.submit(
            request(side=Side.SELL, qty=10, limit_price=Decimal("88.211111")), portfolio
        )
        buy = await routed.submit(
            request(side=Side.BUY, qty=10, limit_price=Decimal("88.218888")), portfolio
        )

        assert sell.order is not None and sell.order.limit_price == Decimal("88.22")
        assert buy.order is not None and buy.order.limit_price == Decimal("88.21")

    async def test_an_instrument_with_a_coarser_tick_is_honoured(self) -> None:
        """The quantum is the instrument's, not a hard-coded two places — so a
        venue that trades in something else is a constructor argument rather
        than a second copy of this logic."""
        broker = FakeBroker()
        routed = OrderRouter(
            broker,
            permissive(),
            StopManager(),
            SimulatedClock(OPEN_HOURS),
            instruments={"SPY": Instrument(symbol="SPY", tick_size=Decimal("0.25"))},
        )
        portfolio = book()

        result = await routed.submit(
            request(side=Side.BUY, qty=10, limit_price=Decimal("88.61")), portfolio
        )

        assert result.order is not None
        assert result.order.limit_price == Decimal("88.50")

    async def test_an_unregistered_symbol_still_gets_a_penny_tick(self) -> None:
        """A default rather than a `KeyError`: the alternative is a platform
        that refuses to trade a name nobody remembered to register, and the
        default is correct for every symbol in the watchlist."""
        assert router().instrument("NVDA").tick_size == Decimal("0.01")


# ── closing a position the venue's own stop is holding ──────────────────────


class TestClosingReleasesProtection:
    """Day 4 of the paper week, as assertions.

    A venue reserves the shares a working stop covers. The platform placed a
    GTC stop over every share of every position the moment it filled, then asked
    to sell those same shares — and was refused 38 times out of 39, once per exit
    signal the strategy produced. Every position that closed that session closed
    at its stop, so every closed round trip was a loss and the strategy's exit
    rule was never once tested (docs/paper-week/day-4-review.md, B1).

    Nothing in the suite could fail on it, for two reasons worth keeping apart.
    `FakeBroker` said yes to both orders, so the reservation did not exist to be
    tripped over — that is `reserves_inventory`. And no test asked the only
    question that matters here: *can this platform get out of a position it has
    protected?*
    """

    async def _open_protected(
        self, broker: FakeBroker, routed: OrderRouter, portfolio: Portfolio
    ) -> Order:
        """A long SPY position with a working venue-side stop over all of it, on
        the venue's book as well as ours — the state day 4 held 50 times."""
        entry_result = await routed.submit(request(stop_loss_price=Decimal(95)), portfolio)
        entry = entry_result.order
        assert entry is not None
        fill(entry, portfolio, 100, 100)
        # Marked, so the chain can price the symbol. Without it every later
        # order is refused for want of a price and the test passes for the wrong
        # reason.
        portfolio.position("SPY").last_price = Decimal(100)
        # The venue holds the position too, so its reservation check has
        # something to reserve against.
        venue = broker.positions.setdefault("SPY", Position(symbol="SPY"))
        venue.qty = Decimal(100)
        venue.avg_entry_price = Decimal(100)
        stop = (await routed.submit_protective_orders(entry, portfolio)).stop_order
        assert stop is not None
        assert broker.open_stops("SPY") == 1
        return stop

    async def test_the_fake_actually_reserves_the_shares(self) -> None:
        """**The guard under the rest of this class.** If `reserves_inventory`
        did not bite, every test here would pass without the fix — which is
        precisely how day 4 shipped. Asserted directly, against the router, so a
        later change that quietly loosens the fake fails here rather than
        silently removing the coverage."""
        broker, portfolio = FakeBroker(), book()
        routed = router(broker, chain())
        await self._open_protected(broker, routed, portfolio)

        # The stop already holds all 100 shares, so this sell has none to take.
        refused = await routed.submit(
            request(side=Side.SELL, qty=100, ts=OPEN_HOURS + timedelta(minutes=1)), portfolio
        )

        assert not refused.submitted
        assert refused.inventory_held, "the venue's refusal must be classified, not generic"
        assert refused.order is not None
        assert "insufficient qty available" in (refused.order.reject_reason or "")
        assert "held_for_orders" in (refused.order.reject_reason or "")

    async def test_an_exit_signal_reaches_the_venue_through_its_own_stop(self) -> None:
        """**The test day 4 did not have.** The exit is refused for want of the
        shares its own stop is holding unless the stop comes off first."""
        broker, portfolio = FakeBroker(), book()
        routed = router(broker, chain())
        stop = await self._open_protected(broker, routed, portfolio)

        result = await routed.submit_signal(
            signal(SignalAction.EXIT), portfolio, sizing(), pending=()
        )

        assert result.submitted, "an exit must reach the venue, not be refused for its own stop"
        assert result.order is not None
        assert result.order.qty == Decimal(100)
        assert result.order.time_in_force is TimeInForce.GTC
        assert stop.broker_order_id in broker.cancelled

    async def test_a_flatten_reaches_the_venue_through_its_own_stop(self) -> None:
        """`flatten` is the operator's button and the engine's exit path both.
        It was refused for the same reason, and the runbook's emergency close is
        the worst place in the system to discover it."""
        broker, portfolio = FakeBroker(), book()
        routed = router(broker, chain())
        stop = await self._open_protected(broker, routed, portfolio)

        result = await routed.flatten("SPY", portfolio)

        assert result.submitted
        assert stop.broker_order_id in broker.cancelled

    async def test_a_refused_close_puts_the_stop_back(self) -> None:
        """The objection the old ordering was built on, answered rather than
        avoided: the close is refused, so the shares are still held, so they are
        still protected at the venue — under a key the venue has not seen, or it
        would hand back the cancelled order."""
        broker, portfolio = FakeBroker(), book()
        routed = router(broker, chain())
        stop = await self._open_protected(broker, routed, portfolio)

        # Armed by the cancel, so the *first* close still earns the venue's
        # inventory refusal — which is what triggers the release — and the retry
        # after it is the one that fails. `reject_next` is one-shot, so the
        # re-arm that follows is not caught by it, which is the point: this test
        # is about a refused close, not about a venue refusing everything.
        real_cancel = broker.cancel_order

        async def refuse_the_retry(broker_order_id: str) -> None:
            await real_cancel(broker_order_id)
            broker.reject_next = "the close is refused, for a reason of its own"

        broker.cancel_order = refuse_the_retry  # type: ignore[method-assign]

        result = await routed.flatten("SPY", portfolio)

        assert not result.submitted
        assert stop.broker_order_id in broker.cancelled
        assert routed.has_broker_side_protection("SPY", portfolio.position("SPY"))
        assert broker.open_stops("SPY") == 1
        rearmed = next(
            o
            for o in broker.accepted.values()
            if o.order_type is OrderType.STOP and not o.is_complete
        )
        assert rearmed.client_order_id != stop.client_order_id
        assert rearmed.stop_price == stop.stop_price
        assert rearmed.qty == Decimal(100)
        assert rearmed.time_in_force is TimeInForce.GTC

    async def test_the_engine_side_level_survives_the_release(self) -> None:
        """What covers the gap between the cancel and the fill. It is armed on
        the position before anything is submitted, and `_close` must not clear
        it — a release that de-armed the level would leave the window with
        nothing watching it at all."""
        broker, portfolio = FakeBroker(), book()
        routed = router(broker, chain())
        await self._open_protected(broker, routed, portfolio)
        armed = portfolio.position("SPY").stop_loss_price
        assert armed == Decimal(95)

        await routed.flatten("SPY", portfolio)

        assert portfolio.position("SPY").stop_loss_price == armed

    async def test_a_stop_that_will_not_cancel_blocks_the_close_and_is_not_replaced(self) -> None:
        """A cancel that failed leaves the shares reserved, so the close is
        refused — correctly, because something is still protecting the position.
        Reporting it released would place a second stop over shares that already
        have one, which is the bug in the other direction."""
        broker, portfolio = FakeBroker(), book()
        routed = router(broker, chain())
        stop = await self._open_protected(broker, routed, portfolio)
        assert stop.broker_order_id is not None
        broker.cancel_refuses.add(stop.broker_order_id)

        result = await routed.flatten("SPY", portfolio)

        assert not result.submitted
        assert result.order is not None
        assert "insufficient qty available" in (result.order.reject_reason or "")
        assert broker.open_stops("SPY") == 1, "no second stop over the same shares"
        assert routed.has_broker_side_protection("SPY", portfolio.position("SPY"))

    async def test_a_close_with_nothing_to_release_cancels_nothing(self) -> None:
        """An unprotected position closes by the plain path and pays none of
        this — the common case must not acquire a broker round trip."""
        broker, portfolio = FakeBroker(), book(SPY=(100, 100))
        result = await router(broker, chain()).flatten("SPY", portfolio)

        assert result.submitted
        assert broker.cancelled == []

    async def test_the_stop_is_not_released_for_an_entry(self) -> None:
        """Only a close releases protection. An entry that grew a position must
        leave the stop over the shares already held."""
        broker, portfolio = FakeBroker(), book()
        # Room for a second tranche: the point is what the router does with the
        # existing stop, not where the position cap sits.
        routed = router(broker, chain(max_position_pct=Decimal("0.50")))
        await self._open_protected(broker, routed, portfolio)

        result = await routed.submit_signal(
            signal(SignalAction.ENTER_LONG), portfolio, sizing(), pending=()
        )

        assert result.submitted
        assert broker.cancelled == []
        assert broker.open_stops("SPY") == 1
