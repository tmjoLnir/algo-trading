"""What the worker trades, and how — configuration an operator owns at runtime.

Distinct from `atp_core.config`, and the split is the point. `Settings` holds
what the *process* is: which database, which broker credentials, which run
mode. Those are properties of a deployment, they are read once at import, and
changing one is a deploy. This package holds what the *trader* is: the
watchlist, the strategy and its parameters, how orders are sized, the
protective stop, and whether an unattended loop may place live orders. Those
are decisions somebody makes and revises while the platform is running, and
until now every one of them was an environment variable — which meant that
changing a stop multiplier required shell access to the host, an editor, and a
restart, and left no record of who changed it or when.

`apps/worker` is the process; this is the configuration it runs on. One is
imported by the other and neither is the other's package.
"""

from atp_core.worker.config import (
    DEFAULT_WORKER_CONFIG,
    SIZING_METHODS,
    STOP_TYPES,
    TIMEFRAMES,
    SizingMethod,
    StopTypeName,
    StoredWorkerConfig,
    TimeframeName,
    WorkerConfig,
    strategy_options,
)
from atp_core.worker.ports import RunningWorkerConfig, WorkerConfigRepository, WorkerStatusStore

__all__ = [
    "DEFAULT_WORKER_CONFIG",
    "SIZING_METHODS",
    "STOP_TYPES",
    "TIMEFRAMES",
    "RunningWorkerConfig",
    "SizingMethod",
    "StopTypeName",
    "StoredWorkerConfig",
    "TimeframeName",
    "WorkerConfig",
    "WorkerConfigRepository",
    "WorkerStatusStore",
    "strategy_options",
]
