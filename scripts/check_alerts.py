#!/usr/bin/env python
"""Send a test alert through every configured transport and say what arrived.

    uv run python scripts/check_alerts.py --by jo
    uv run python scripts/check_alerts.py --by jo --severity critical
    uv run python scripts/check_alerts.py --by jo --severity info --severity critical

Answers one question: **if the platform halted right now, would anybody find
out?** Nothing else here can answer it. `make test` cannot — CLAUDE.md §1.7
keeps the suite off live endpoints, so every alert test in it proves the request
this code *would* have made, against a transport that agrees with us about the
answer. Whether a real token still works, whether the bot was removed from the
chat, whether the topic still has a subscriber: none of that is knowable except
by sending something.

Until this existed the documented way to check was `halt.py engage` followed by
`halt.py clear` (docs/DEPLOYMENT.md), which works and has the drawback of being
a real halt: it needs Redis, it stops trading, and it writes an incident into
the audit trail that never happened. This sends the same messages through the
same sinks and touches neither.

**It reports delivery, not receipt.** A transport returning success means the
service accepted the message, which is as far as any code here can see. Whether
a phone lit up is a question only the person holding it can answer, and it is
the half that actually matters — so go and look before believing the exit code.

Read-only with respect to trading: it builds no broker, engages no halt, and
touches neither Redis nor Postgres.
"""

from __future__ import annotations

import argparse
import sys

from atp_core import metrics
from atp_core.alerts import Alert, LoggingAlertSink, Severity, build_alert_sink
from atp_core.config import get_settings
from atp_core.logging import configure as configure_logging

#: Nothing configured. Distinct from a send that failed, because the two need
#: opposite things done about them — one is an empty `.env`, the other is a
#: credential or a service — and an operator reading only the exit code is
#: entitled to know which they have.
EXIT_UNCONFIGURED = 2
#: Configured, and at least one transport did not take the message.
EXIT_UNDELIVERED = 1


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument(
        "--by",
        required=True,
        help="who is running the check — it goes in the message, so whoever "
        "receives it at 03:00 can see it was a test and who to ask",
    )
    p.add_argument(
        "--severity",
        action="append",
        choices=[s.value for s in Severity],
        help="which levels to send; repeatable. Defaults to all of them, "
        "because the levels differ in whether they make a phone ring and that "
        "is the part worth checking",
    )
    return p.parse_args(argv)


def _severities(chosen: list[str] | None) -> list[Severity]:
    if not chosen:
        return list(Severity)
    # Deduplicated, and in declaration order rather than the order they were
    # typed, so `--severity critical --severity info` reads bottom-up in the
    # chat the way the platform's own alerts do.
    picked = {Severity(value) for value in chosen}
    return [severity for severity in Severity if severity in picked]


def _configured_transports(settings: object) -> list[str]:
    """Which transports `build_alert_sink` will have built, by name.

    Derived from the settings rather than by reaching into the sink, so this
    stays true of a `FanOutAlertSink` without knowing what it holds — and so
    that adding a transport means changing `build_alert_sink` and this list,
    rather than something that introspects private attributes.
    """
    transports = []
    if getattr(settings, "alert_ntfy_topic", ""):
        transports.append("ntfy")
    if getattr(settings, "alert_telegram_token", "") and getattr(
        settings, "alert_telegram_chat_id", ""
    ):
        transports.append("telegram")
    return transports


def _counts(transport: str) -> tuple[float, float]:
    """`(sent, failed)` for one transport, from the platform's own counters.

    Read from the metrics rather than from a return value because `send`
    deliberately has none: `AlertSink` promises not to raise and swallows its
    failures, which is the right contract for a call that happens straight
    after a halt and the wrong one for a checker. The counters are where a
    swallowed failure surfaces, and reading them here means this also proves
    the accounting an operator would alert on is actually being written.
    """
    registry = metrics.get_registry()
    sent = 0.0
    for severity in Severity:
        value = registry.get_sample_value(
            "atp_alerts_sent_total", {"transport": transport, "severity": severity.value}
        )
        sent += value or 0.0
    failed = registry.get_sample_value("atp_alerts_failed_total", {"transport": transport})
    return sent, failed or 0.0


def _alert_for(severity: Severity, by: str) -> Alert:
    """The message to send. Says it is a test in the first line of the body.

    The title keeps the shape of a real one so that what an operator sees here
    is what they will see at 09:31 — but the body has to disown it immediately,
    because the alternative is somebody's morning ruined by a drill.
    """
    return Alert(
        severity=severity,
        title=f"Alert check: {severity.value}",
        body=(
            f"Test message from scripts/check_alerts.py, run by {by}.\n"
            "Nothing is wrong and nothing has halted. If you are reading this "
            "on a phone, the alert path works."
        ),
        key=f"check.{severity.value}",
    )


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    settings = get_settings()
    configure_logging(settings.log_level, settings.log_format)

    sink = build_alert_sink(settings)
    transports = _configured_transports(settings)

    print(f"run mode   : {settings.run_mode.value}")
    print(f"sink       : {type(sink).__name__}")
    print(f"transports : {', '.join(transports) if transports else 'none'}")

    if isinstance(sink, LoggingAlertSink) or not transports:
        # The failure this whole script exists to make loud. An unconfigured
        # platform starts, runs and halts perfectly well, and says so only in a
        # log file nobody is reading at the time — so the one way to find out
        # is to ask, and the answer has to be an error rather than a remark.
        print()
        print("NOTHING IS CONFIGURED. Every alert this platform raises will be")
        print("written to the log and reach nobody. Set ALERT_NTFY_TOPIC, or")
        print("ALERT_TELEGRAM_TOKEN and ALERT_TELEGRAM_CHAT_ID — docs/DEPLOYMENT.md.")
        return EXIT_UNCONFIGURED

    severities = _severities(args.severity)
    before = {transport: _counts(transport) for transport in transports}

    print()
    for severity in severities:
        print(f"sending {severity.value} ...")
        sink.send(_alert_for(severity, args.by))

    print()
    undelivered = False
    for transport in transports:
        sent_before, failed_before = before[transport]
        sent_now, failed_now = _counts(transport)
        sent = int(sent_now - sent_before)
        failed = int(failed_now - failed_before)
        status = "ok" if sent == len(severities) and failed == 0 else "PROBLEM"
        print(f"{transport:9}: {sent} accepted, {failed} failed  [{status}]")
        if status == "PROBLEM":
            undelivered = True

    print()
    if undelivered:
        # The log line from the sink says what went wrong and carries no
        # credential, which is the whole of `alerts.sinks._describe`'s job.
        print("At least one transport did not take the message. The reason is in")
        print("the alert.send_failed line above — it names the failure, and")
        print("deliberately not the credential.")
        return EXIT_UNDELIVERED

    print("Every configured transport accepted every message.")
    print("Now go and look at the phone: acceptance is not arrival, and arrival")
    print("is the only part that was ever in question.")
    return 0


if __name__ == "__main__":  # pragma: no cover - entry point
    sys.exit(main())
