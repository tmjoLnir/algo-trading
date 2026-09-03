"""The kill switch against a real Redis.

The unit tests cover the logic against a dict. What they cannot cover is the
reason the state lives in Redis at all: *the API process must be able to trip it
while the worker is mid-loop*, and it must survive a worker restart. Both of
those are properties of two separate connections agreeing, so both need a real
server.

The fail-closed path is here too, against a genuinely unreachable Redis rather
than a fake that raises on command — the failure mode docs/SAFETY.md names for
layer 6 deserves to be shown happening rather than simulated.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest
import redis as redis_sync

from atp_core.risk.killswitch import HaltReason, HaltScope, RedisKillSwitch

if TYPE_CHECKING:
    from collections.abc import Iterator

PREFIX = "atp:test:halt"


@pytest.fixture
def redis_url() -> str:
    url = os.environ.get("REDIS_URL")
    if not url:
        pytest.skip("REDIS_URL is unset — start the stack with `make up`")
    return url


@pytest.fixture
def client(redis_url: str) -> Iterator[redis_sync.Redis]:
    connection = redis_sync.Redis.from_url(redis_url, decode_responses=True)
    connection.ping()
    for key in connection.scan_iter(match=f"{PREFIX}:*"):
        connection.delete(key)
    yield connection
    for key in connection.scan_iter(match=f"{PREFIX}:*"):
        connection.delete(key)
    connection.close()


def test_one_process_trips_it_and_another_sees_it(redis_url: str, client: redis_sync.Redis) -> None:
    """The whole reason this is not an in-process flag. The API tripping the
    switch has to stop a worker that is already inside its loop."""
    api = RedisKillSwitch(client, key_prefix=PREFIX)

    worker_connection = redis_sync.Redis.from_url(redis_url, decode_responses=True)
    try:
        worker = RedisKillSwitch(worker_connection, key_prefix=PREFIX)
        assert not worker.is_engaged()

        api.engage(HaltScope.GLOBAL, HaltReason.MANUAL, "dashboard", detail="operator hit stop")

        assert worker.is_engaged(), "the worker did not see the API's halt"
        assert worker.active_halts()[0].engaged_by == "dashboard"

        api.clear(HaltScope.GLOBAL, cleared_by="alice")
        assert not worker.is_engaged()
    finally:
        worker_connection.close()


def test_a_halt_survives_the_process_that_set_it(redis_url: str, client: redis_sync.Redis) -> None:
    """A switch that cleared on restart would be worse than none: a crash loop
    would silently resume trading through whatever caused the crash."""
    first = RedisKillSwitch(client, key_prefix=PREFIX)
    first.engage(HaltScope.GLOBAL, HaltReason.UNHANDLED_EXCEPTION, "worker-1", detail="crashed")

    restarted_connection = redis_sync.Redis.from_url(redis_url, decode_responses=True)
    try:
        restarted = RedisKillSwitch(restarted_connection, key_prefix=PREFIX)
        assert restarted.is_engaged()
        record = restarted.active_halts()[0]
        assert record.engaged_by == "worker-1"
        assert record.reason is HaltReason.UNHANDLED_EXCEPTION
        assert record.detail == "crashed"
    finally:
        restarted_connection.close()


def test_scoped_halts_are_independent_across_connections(
    redis_url: str, client: redis_sync.Redis
) -> None:
    switch = RedisKillSwitch(client, key_prefix=PREFIX)
    switch.engage(HaltScope.SYMBOL, HaltReason.DATA_FEED_LOST, "ingestor", target="SPY")
    switch.engage(HaltScope.STRATEGY, HaltReason.RATE_LIMIT_STORM, "risk", target="sma_crossover")

    other = redis_sync.Redis.from_url(redis_url, decode_responses=True)
    try:
        reader = RedisKillSwitch(other, key_prefix=PREFIX)
        assert reader.is_engaged(symbol="SPY")
        assert not reader.is_engaged(symbol="QQQ")
        assert reader.is_engaged(strategy_id="sma_crossover")
        assert not reader.is_engaged(strategy_id="mean_reversion")
        assert not reader.is_engaged(), "neither halt is global"
        assert len(reader.active_halts()) == 2
    finally:
        other.close()


def test_engaging_it_actually_refuses_orders(redis_url: str, client: redis_sync.Redis) -> None:
    """docs/SAFETY.md's go-live checklist, verbatim: "Kill switch tested end to
    end — engage it and confirm orders are actually refused."

    Through the real rule chain rather than the rule alone, because the thing
    worth knowing is that the *first* denial is the kill switch — an order
    stopped by a halt should say so, not report whichever limit it also happened
    to breach.
    """
    from decimal import Decimal

    from atp_core.clock import SimulatedClock, TradingCalendar
    from atp_core.domain import Order, Portfolio, Side
    from atp_core.risk.engine import RiskEngine, default_rules
    from atp_core.risk.limits import RiskLimits
    from atp_core.risk.rules import DailyLossLimitRule

    now = datetime(2024, 1, 2, 15, 0, tzinfo=UTC)  # 10:00 New York, a Tuesday
    switch = RedisKillSwitch(client, key_prefix=PREFIX)
    rules = default_rules(
        kill_switch=switch,
        clock=SimulatedClock(now),
        calendar=TradingCalendar(),
        last_tick_at=lambda _s: now,
    )
    for rule in rules:
        if isinstance(rule, DailyLossLimitRule):
            rule.anchor(Decimal(100_000))

    engine = RiskEngine(RiskLimits(), rules=rules)
    book = Portfolio(cash=Decimal(100_000), starting_equity=Decimal(100_000))
    order = Order(
        symbol="SPY", side=Side.BUY, qty=Decimal(10), limit_price=Decimal(100), strategy_id="sma"
    )

    assert engine.validate(order, book).approved, "clean chain should pass before the halt"

    switch.engage(HaltScope.GLOBAL, HaltReason.MANUAL, "operator")

    decision = engine.validate(order, book)
    assert not decision.approved
    assert decision.rule == "kill_switch"
    assert decision.reason == "trading is halted"

    switch.clear(HaltScope.GLOBAL, cleared_by="alice")
    assert engine.validate(order, book).approved, "clearing should resume trading"


def test_an_unreachable_redis_fails_closed() -> None:
    """docs/SAFETY.md layer 6, shown rather than simulated: pointed at a port
    with nothing on it, the switch reports engaged and trading stops."""
    dead = redis_sync.Redis.from_url(
        "redis://127.0.0.1:6399/0", socket_connect_timeout=1, socket_timeout=1
    )
    switch = RedisKillSwitch(dead, key_prefix=PREFIX)
    assert switch.is_engaged() is True
    assert switch.is_engaged("any_strategy", "SPY") is True
