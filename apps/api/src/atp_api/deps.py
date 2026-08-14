"""FastAPI dependencies — the composition root.

Every adapter is wired here and injected, so a test binds a fake broker without
touching a router. This is where the hexagonal architecture actually pays off.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Annotated

from atp_core.config import Settings, get_settings
from fastapi import Depends


async def get_db() -> AsyncIterator[object]:
    """Request-scoped database session."""
    raise NotImplementedError


async def get_redis() -> object:
    raise NotImplementedError


async def get_broker(settings: Annotated[Settings, Depends(get_settings)]) -> object:
    """Bind the broker adapter for the current run mode.

    backtest → SimulatedBroker, paper → Alpaca paper, live → Alpaca live.
    The only place in the API that knows run mode exists.
    """
    raise NotImplementedError


async def get_kill_switch() -> object:
    raise NotImplementedError


async def get_current_user() -> str:
    """Resolve the acting user.

    Authentication is deliberately a stub in the skeleton — see docs/SAFETY.md
    'Access control'. Do NOT expose this API on a public interface until it is
    implemented: every endpoint under /risk and /orders can move real money.
    """
    raise NotImplementedError
