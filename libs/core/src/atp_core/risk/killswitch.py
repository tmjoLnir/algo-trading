"""The kill switch — stop everything, now.

State lives in Redis, not in process memory, for three reasons: the API process
must be able to trip it while the worker is mid-loop; it survives a worker
restart (a switch that clears on restart is worse than none, because a crash
loop would silently resume trading); and every process sees the same value.

Engaging is instant and requires no confirmation. Clearing is deliberate,
requires a human identity, and is audit-logged. That asymmetry is intentional:
stopping should be reflexive, restarting should not be.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from datetime import datetime


class HaltScope(StrEnum):
    GLOBAL = "global"  # nothing trades
    STRATEGY = "strategy"  # one strategy halted
    SYMBOL = "symbol"  # one instrument halted


class HaltReason(StrEnum):
    MANUAL = "manual"
    DAILY_LOSS_LIMIT = "daily_loss_limit"
    RECONCILIATION_MISMATCH = "reconciliation_mismatch"
    DATA_FEED_LOST = "data_feed_lost"
    BROKER_UNREACHABLE = "broker_unreachable"
    RATE_LIMIT_STORM = "rate_limit_storm"
    UNHANDLED_EXCEPTION = "unhandled_exception"


@dataclass(frozen=True, slots=True)
class HaltRecord:
    scope: HaltScope
    reason: HaltReason
    engaged_at: datetime
    engaged_by: str
    detail: str = ""
    target: str | None = None  # strategy_id or symbol when scope is not GLOBAL


class KillSwitch(Protocol):
    def is_engaged(self, strategy_id: str | None = None, symbol: str | None = None) -> bool:
        """True if this order is covered by any active halt.

        Checked by `KillSwitchRule` before every single order.
        """
        ...

    def engage(
        self,
        scope: HaltScope,
        reason: HaltReason,
        engaged_by: str,
        detail: str = "",
        target: str | None = None,
    ) -> HaltRecord:
        """Halt immediately. Idempotent — re-engaging an active halt is fine."""
        ...

    def clear(self, scope: HaltScope, cleared_by: str, target: str | None = None) -> None:
        """Resume. Requires a named human; always audit-logged."""
        ...

    def active_halts(self) -> list[HaltRecord]:
        """Everything currently halted — rendered as a banner on the dashboard."""
        ...


class RedisKillSwitch:
    """Redis-backed implementation. See docs/SAFETY.md."""

    def __init__(self, redis_url: str, key_prefix: str = "atp:halt") -> None:
        self.redis_url = redis_url
        self.key_prefix = key_prefix

    def is_engaged(self, strategy_id: str | None = None, symbol: str | None = None) -> bool:
        raise NotImplementedError

    def engage(
        self,
        scope: HaltScope,
        reason: HaltReason,
        engaged_by: str,
        detail: str = "",
        target: str | None = None,
    ) -> HaltRecord:
        raise NotImplementedError

    def clear(self, scope: HaltScope, cleared_by: str, target: str | None = None) -> None:
        raise NotImplementedError

    def active_halts(self) -> list[HaltRecord]:
        raise NotImplementedError


def flatten_all_positions() -> None:
    """Emergency liquidation: cancel every open order, close every position.

    Separate from `engage()` on purpose. Halting stops *new* risk; flattening
    *realises* existing P&L and is not always the right response to a problem —
    a data outage means stop trading, not dump the book into a market you
    currently cannot see. Requires explicit human action.
    """
    raise NotImplementedError("see docs/RUNBOOK.md 'Emergency flatten'")
