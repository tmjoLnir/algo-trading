"""The `WorkerStatusStore` port over Redis — what each worker booted with.

One key per run mode holding one JSON document, written once at worker start.
The same shape and the same reasoning as `RedisSnapshotStore`, including the
part that matters most: **freshness is a property of the payload, not of the
key**. The TTL is garbage collection for a run mode nobody runs any more. If
expiry were the freshness mechanism then a worker that died on Friday would
read back as "no worker has ever reported", and "nothing has run" and "the last
worker started four days ago" would be the same answer — where only the second
one explains why a saved change has not taken effect.

The one deliberate difference from the snapshot store is what happens to an
unreadable payload: this returns None and logs, where the book raises. The book
decides what a reader believes they hold, so a corrupt one must fail the
request. This decorates a settings form, and refusing to render the form over a
status blob that did not parse would take away the screen that could fix it.
"""

from __future__ import annotations

import json
from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any, cast

from atp_core.logging import get_logger
from atp_core.risk.limits import DEFAULT_RISK_LIMITS, RiskLimits
from atp_core.worker.config import SizingMethod, StopTypeName, WorkerConfig, normalise_symbols
from atp_core.worker.ports import RunningWorkerConfig

if TYPE_CHECKING:
    from redis.asyncio import Redis

    from atp_core.domain import RunMode
    from atp_core.worker.ports import WorkerStatusStore

log = get_logger(__name__)

#: Prefixed so `atp:worker:*` names everything a worker publishes about itself
#: and an operator can see it with one `SCAN MATCH`.
KEY_PREFIX = "atp:worker:running:"

#: Seven days, matching the snapshot store. Long enough that a weekend plus a
#: holiday does not erase the last worker's report, short enough that a run mode
#: nobody uses does not sit in Redis indefinitely.
DEFAULT_TTL_SECONDS = 7 * 24 * 60 * 60


def encode_running(running: RunningWorkerConfig) -> dict[str, Any]:
    """The wire form. Decimals as strings, timestamps as ISO-8601 (rule §1.1)."""
    config = running.config
    return {
        "revision": running.revision,
        "started_at": running.started_at.isoformat(),
        "trading": running.trading,
        "reason": running.reason,
        "config": {
            "symbols": list(config.symbols),
            "max_silence_seconds": config.max_silence_seconds,
            "strategy": config.strategy,
            "strategy_params": dict(config.strategy_params),
            "sizing_method": config.sizing_method,
            "sizing_value": str(config.sizing_value),
            "stop_type": config.stop_type,
            "stop_multiplier": str(config.stop_multiplier),
            "stop_period": config.stop_period,
            "allow_live_orders": config.allow_live_orders,
            # Published as well as stored, and that is the point of publishing
            # anything here: the worker builds its `RiskEngine` from these once
            # at start, so a ceiling edited since is not the ceiling refusing
            # its orders. The screen can only say so by comparing the two.
            "risk": {
                "max_position_pct": str(config.risk.max_position_pct),
                "max_gross_exposure_pct": str(config.risk.max_gross_exposure_pct),
                "max_daily_loss_pct": str(config.risk.max_daily_loss_pct),
                "max_orders_per_minute": config.risk.max_orders_per_minute,
                "max_open_positions": config.risk.max_open_positions,
                "max_quote_age_seconds": config.risk.max_quote_age_seconds,
                "default_stop_loss_pct": str(config.risk.default_stop_loss_pct),
                "default_take_profit_pct": str(config.risk.default_take_profit_pct),
            },
        },
    }


def decode_running(payload: dict[str, Any]) -> RunningWorkerConfig:
    """Rebuild what a worker published, validating it as `WorkerConfig` does."""
    raw = payload["config"]
    return RunningWorkerConfig(
        config=WorkerConfig(
            symbols=normalise_symbols(str(s) for s in raw["symbols"]),
            max_silence_seconds=int(raw["max_silence_seconds"]),
            strategy=str(raw["strategy"]),
            strategy_params=dict(raw["strategy_params"]),
            sizing_method=cast("SizingMethod", raw["sizing_method"]),
            sizing_value=Decimal(str(raw["sizing_value"])),
            stop_type=cast("StopTypeName", raw["stop_type"]),
            stop_multiplier=Decimal(str(raw["stop_multiplier"])),
            stop_period=int(raw["stop_period"]),
            allow_live_orders=bool(raw["allow_live_orders"]),
            risk=_decode_risk(raw),
        ),
        revision=int(payload["revision"]),
        started_at=datetime.fromisoformat(payload["started_at"]),
        trading=bool(payload["trading"]),
        reason=str(payload["reason"]),
    )


def _decode_risk(raw: dict[str, Any]) -> RiskLimits:
    """The ceilings a worker published, or the defaults for one that predates them.

    A blob written by the previous release has no `risk` key, and this is the
    one decoder in the pair that must not raise over it: `WorkerStatusStore.get`
    is documented to return None rather than fail, because this decorates a
    settings screen and a dashboard that refused to render the form over a stale
    status blob would take away the one screen that could fix it. A worker
    restart replaces the key within one deploy anyway.

    Falling back to the defaults is honest here for the same reason revision `0`
    is: what it renders is what that worker was in fact running, since the
    defaults are the values `.env` shipped.
    """
    published = raw.get("risk")
    if not isinstance(published, dict):
        return DEFAULT_RISK_LIMITS
    return RiskLimits(
        max_position_pct=Decimal(str(published["max_position_pct"])),
        max_gross_exposure_pct=Decimal(str(published["max_gross_exposure_pct"])),
        max_daily_loss_pct=Decimal(str(published["max_daily_loss_pct"])),
        max_orders_per_minute=int(published["max_orders_per_minute"]),
        max_open_positions=int(published["max_open_positions"]),
        max_quote_age_seconds=int(published["max_quote_age_seconds"]),
        default_stop_loss_pct=Decimal(str(published["default_stop_loss_pct"])),
        default_take_profit_pct=Decimal(str(published["default_take_profit_pct"])),
    )


class RedisWorkerStatusStore:
    """`WorkerStatusStore` over Redis.

    Takes a client rather than a URL: core does not open sockets on its own
    behalf (CLAUDE.md §1.3).
    """

    def __init__(
        self,
        client: Redis,
        *,
        key_prefix: str = KEY_PREFIX,
        ttl_seconds: int = DEFAULT_TTL_SECONDS,
    ) -> None:
        if ttl_seconds < 1:
            raise ValueError(f"ttl_seconds must be at least 1, got {ttl_seconds}")
        self._client = client
        self._key_prefix = key_prefix
        self._ttl_seconds = ttl_seconds

    def _key(self, run_mode: RunMode) -> str:
        return f"{self._key_prefix}{run_mode.value}"

    async def put(self, run_mode: RunMode, running: RunningWorkerConfig) -> None:
        await self._client.set(
            self._key(run_mode),
            json.dumps(encode_running(running)),
            ex=self._ttl_seconds,
        )

    async def get(self, run_mode: RunMode) -> RunningWorkerConfig | None:
        raw: Any = await self._client.get(self._key(run_mode))
        if raw is None:
            return None
        try:
            return decode_running(json.loads(raw))
        except Exception as exc:
            # Logged and swallowed — see the module docstring. The screen then
            # renders as though no worker had reported, which is the honest
            # reading: we cannot say what it is running.
            log.error(
                "worker.status.unreadable",
                run_mode=run_mode.value,
                error=str(exc),
                hint="a published worker status did not parse — format change without a bump?",
            )
            return None


if TYPE_CHECKING:
    # mypy enforces that the adapter still satisfies its port.
    def _conforms(adapter: RedisWorkerStatusStore) -> WorkerStatusStore:
        return adapter
