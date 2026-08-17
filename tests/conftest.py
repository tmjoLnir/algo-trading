"""Shared fixtures and the live-trading guard.

The guard at the top is the most important code in the test suite.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from decimal import Decimal

import pytest


def pytest_configure(config: pytest.Config) -> None:
    """Refuse to run tests against a live account (CLAUDE.md §1.7).

    Tests place orders. If `ATP_RUN_MODE=live` is set — a leftover shell export,
    a mis-provisioned CI runner — those orders would be real. Fail the session
    before a single test collects.
    """
    if os.environ.get("ATP_RUN_MODE", "").lower() == "live":
        pytest.exit(
            "REFUSING TO RUN: ATP_RUN_MODE=live. Tests must never touch a live "
            "account. Unset it or set ATP_RUN_MODE=backtest.",
            returncode=1,
        )
    os.environ.setdefault("ATP_RUN_MODE", "backtest")
    os.environ.setdefault("ATP_ALLOW_LIVE_TRADING", "false")


@pytest.fixture
def utc_now() -> datetime:
    return datetime(2024, 6, 3, 14, 30, tzinfo=UTC)


@pytest.fixture
def frozen_clock(utc_now: datetime):
    """A `SimulatedClock` pinned to a known instant."""
    from atp_core.clock import SimulatedClock

    return SimulatedClock(utc_now)


@pytest.fixture
def sample_bars():
    """Deterministic daily bars for one symbol.

    TODO: build a fixture that deliberately includes a gap (a holiday), a 2:1
    split, and one bar whose range spans both a stop and a target — the three
    cases most engine bugs hide in.
    """
    raise NotImplementedError


@pytest.fixture
def fake_broker():
    """In-memory `BrokerPort` with controllable fills and failures.

    Simulates: a partial fill, a reject, a timeout on submit (both kinds — one
    where the venue never saw the order and one where it did, which is what
    makes a blind resubmit dangerous), and a disconnect that takes reads down
    too. See `tests/fakes.FakeBroker`.
    """
    from tests.fakes import FakeBroker

    return FakeBroker()


@pytest.fixture
def empty_portfolio():
    from atp_core.domain import Portfolio

    return Portfolio(cash=Decimal("100000"), starting_equity=Decimal("100000"))
