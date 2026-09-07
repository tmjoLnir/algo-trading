"""Risk management (requirement #3): pre-trade validation, stops, kill switch."""

from atp_core.risk.engine import RiskBooks, RiskDecision, RiskEngine, RiskRule
from atp_core.risk.killswitch import HaltReason, HaltScope, KillSwitch
from atp_core.risk.limits import (
    DEFAULT_RISK_LIMITS,
    RISK_LIMIT_FIELDS,
    LimitField,
    RiskLimits,
)
from atp_core.risk.stops import StopConfig, StopManager

__all__ = [
    "DEFAULT_RISK_LIMITS",
    "RISK_LIMIT_FIELDS",
    "HaltReason",
    "HaltScope",
    "KillSwitch",
    "LimitField",
    "RiskBooks",
    "RiskDecision",
    "RiskEngine",
    "RiskLimits",
    "RiskRule",
    "StopConfig",
    "StopManager",
]
