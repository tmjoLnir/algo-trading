"""Order execution: the single submission path, state machine, reconciliation."""

from atp_core.execution.idempotency import client_order_id, protective_client_order_id
from atp_core.execution.reconciliation import Reconciler, ReconciliationReport
from atp_core.execution.router import OrderRouter, ProtectionResult, SubmitResult
from atp_core.execution.state import transition

__all__ = [
    "OrderRouter",
    "ProtectionResult",
    "Reconciler",
    "ReconciliationReport",
    "SubmitResult",
    "client_order_id",
    "protective_client_order_id",
    "transition",
]
