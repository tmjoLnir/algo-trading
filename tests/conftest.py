"""Shared fixtures and the live-trading guard.

The guard at the top is the most important code in the test suite.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from decimal import Decimal
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from atp_core.clock import SimulatedClock
    from atp_core.domain import Portfolio
    from tests.fakes import FakeBroker


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
    _unconfigure_alerting()


#: Everything `Settings` reads about alerting, removed from the test session by
#: `_unconfigure_alerting`.
ALERT_ENV_VARS = (
    "ALERT_NTFY_BASE_URL",
    "ALERT_NTFY_TOPIC",
    "ALERT_NTFY_TOKEN",
    "ALERT_TELEGRAM_TOKEN",
    "ALERT_TELEGRAM_CHAT_ID",
    "ALERT_TELEGRAM_BASE_URL",
    "ALERT_TIMEOUT_SECONDS",
)


def _unconfigure_alerting() -> None:
    """Take the operator's real alert credentials out of the test session.

    The same argument as the guard above, one notch quieter. `Settings` reads
    the process environment, so on a host with alerting configured a bare
    `Settings()` in a test does not mean *the defaults* — it means *this
    machine*. Two things follow, and both are bad:

    - Tests that assert on the unconfigured case fail with nothing wrong in the
      code. That is how this was found: setting `ALERT_TELEGRAM_TOKEN` on a
      machine turned four green `test_alerts.py` cases red, and it fails where
      it is least welcome, since a host with alerting configured is a host that
      is trading and CLAUDE.md §6 asks for `make check` before a push from it.
    - Worse, a test that built a sink from ambient settings and sent through it
      would put a message on somebody's actual phone, in the middle of a test
      run, from a suite CLAUDE.md §1.7 says never touches a live endpoint.

    Popped rather than defaulted, because there is no safe value: the point is
    that no test may inherit a credential, not that it inherits a tidy one.
    """
    for name in ALERT_ENV_VARS:
        os.environ.pop(name, None)


@pytest.fixture
def utc_now() -> datetime:
    return datetime(2024, 6, 3, 14, 30, tzinfo=UTC)


@pytest.fixture
def frozen_clock(utc_now: datetime) -> SimulatedClock:
    """A `SimulatedClock` pinned to a known instant."""
    from atp_core.clock import SimulatedClock

    return SimulatedClock(utc_now)


@pytest.fixture
def fake_broker() -> FakeBroker:
    """In-memory `BrokerPort` with controllable fills and failures.

    Simulates: a partial fill, a reject, a timeout on submit (both kinds — one
    where the venue never saw the order and one where it did, which is what
    makes a blind resubmit dangerous), and a disconnect that takes reads down
    too. See `tests/fakes.FakeBroker`.
    """
    from tests.fakes import FakeBroker

    return FakeBroker()


@pytest.fixture
def empty_portfolio() -> Portfolio:
    from atp_core.domain import Portfolio

    return Portfolio(cash=Decimal("100000"), starting_equity=Decimal("100000"))
