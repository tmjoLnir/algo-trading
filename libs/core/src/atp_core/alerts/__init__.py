"""Alerting: getting a human's attention when the platform stops trading.

docs/SAFETY.md's checklist asks for one thing here — "alerts reach a human on a
phone, not just a log file". The port is in `ports`, the transports in `sinks`.
"""

from atp_core.alerts.ports import Alert, AlertSink, Severity
from atp_core.alerts.sinks import LoggingAlertSink, NtfyAlertSink, build_alert_sink

__all__ = [
    "Alert",
    "AlertSink",
    "LoggingAlertSink",
    "NtfyAlertSink",
    "Severity",
    "build_alert_sink",
]
