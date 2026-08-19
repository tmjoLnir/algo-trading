# 12. Alerts hang off the kill switch, not off the things that alert

**Status:** Accepted · 2026-08-18

## Context

`docs/ROADMAP.md` Phase 6 asks for "Alerting to a phone (feed loss, halt,
reconciliation failure)", and `docs/SAFETY.md`'s go-live checklist has the
one-line version: *"Alerts reach a human on a phone, not just a log file."*

Everything this platform does when something breaks assumes somebody eventually
looks. `StalenessMonitor` logs `CRITICAL` and halts. The order router logs
`CRITICAL` on an indeterminate submit. The supervisor halts and exits if a task
dies. Between 09:30 and 16:00 nobody is reading a log file, and the operator
this is built for is one person who is also doing something else.

The obvious implementation is three hooks, one per named event. Looking at where
those events actually go, the three are not three:

| Roadmap event | What it does today |
|---|---|
| feed loss | `StalenessMonitor` → `KillSwitch.engage(DATA_FEED_LOST)` |
| reconciliation failure | will `engage(RECONCILIATION_MISMATCH)` — the handler is still a stub |
| halt | *is* `KillSwitch.engage` |

`HaltReason` already enumerates them, along with `DAILY_LOSS_LIMIT`,
`BROKER_UNREACHABLE`, `RATE_LIMIT_STORM` and `UNHANDLED_EXCEPTION`. Every
automated stop in this codebase converges on one method.

## Decision

**`RedisKillSwitch` takes an optional `AlertSink` and notifies from exactly
where `_announce` already notifies the dashboard.** The port is
`atp_core.alerts.ports.AlertSink`; the transports are in `atp_core.alerts.sinks`.

**One choke point, for the same reason ADR 0005 gives for orders.** A halt
reason added next year alerts without anyone remembering to wire it, and a
reconciliation handler that does not exist yet is already covered. Three
separate hooks would have been three places to forget.

**Deduplication is the Redis state, not a flag.** `engage` returns early when a
halt is already recorded, so the alert is only reached by a *new* halt.
`StalenessMonitor` re-engages every five seconds for the length of an outage and
sends exactly one notification. There is no counter or "already alerted" boolean
anywhere in the alert path, because a second piece of state tracking the first
is a second piece of state that can disagree with it.

**A failed alert never fails the action.** The sink is called after the halt is
durable in Redis, its exceptions are swallowed and logged, and `AlertSink`
tells implementations not to raise in the first place. Both belts: the contract
is with third-party code, and "must not raise" is not "cannot raise" — the cost
of being wrong is an exception thrown out of the call that just stopped trading,
which makes a successful halt look like a failed one. Same rule as `_announce`
for the dashboard and ADR 0010 for the audit trail.

**Alerts carry the fact, never the book.** Reason, scope and who engaged it —
never a balance, a position, a P&L or a fill price. The transport is a third
party, the notification renders on a lock screen, and on a public ntfy server a
guessable topic is the only thing in front of it. The alert's job is to make
somebody go and look at the dashboard, which is behind authentication.

**Synchronous, because `KillSwitch` is.** An async sink would mean either an
event loop inside the halt path or a halt that cannot alert.

**Two transports ship: ntfy and Telegram.** ntfy needs no account, is one HTTP
POST, has apps on both platforms and can be self-hosted — which matters, because
the alternative to self-hosting is telling a public server when your trading
system stops. Telegram costs a `@BotFather` conversation and reaches an operator
who already has the app, with delivery handled by a real service rather than by
an unauthenticated topic. Neither is obviously better, which is the argument for
the port; Pushover and a Twilio SMS would each be one more class here.

**Configuring both sends to both.** An operator who has configured two
transports has asked for two, and the usual reason to want two is that neither
service is owed that much trust. `FanOutAlertSink` isolates failures per sink,
so one being down is not the same as having no alerting — quietly picking a
winner would be a surprise discovered during an incident.

## Consequences

**Two of the three named events alert today; the third arrives free.** Feed loss
and manual halts are live now. Reconciliation failure alerts the day
`reconcile_with_broker` stops raising `NotImplementedError`, with no further
work, because it will halt like everything else.

**Things that are wrong but do not halt still do not alert.** `order
.submit_indeterminate` and `order.position_unprotected` are both `CRITICAL` in
`docs/RUNBOOK.md` and neither engages the kill switch — deliberately, since an
indeterminate submit halts but an unprotected position may clear on the runner's
next attempt. They are reachable from the same port when someone decides they
should be; this ADR does not decide it for them.

**A halt with no kill switch bound still cannot alert.** `StalenessMonitor`
logs `data.staleness.halt_unavailable` — "TRADING IS NOT HALTED" — and that is
the one case where the most important message is the one that cannot go out.
Misconfiguration, caught at startup rather than by alerting.

**Both transports are addressed by a credential, and both are part of the secret
surface.** The ntfy topic and the Telegram bot token go in the SOPS bundle
(ADR 0011); the token is the more dangerous of the two, since it is the bot
itself and travels in the URL path. Nothing logs either, including on the
failure paths, and that is asserted by tests that were checked to fail when a
secret leaks.

**Telegram reports application errors inside HTTP 200.** A revoked token or a
deleted chat comes back as `{"ok": false}` with a successful status line, so
`TelegramAlertSink` reads the body rather than trusting `raise_for_status`.
Getting this wrong makes a bot that was deleted months ago look like it is
still delivering every halt.

**Alerting depends on outbound HTTPS from the worker**, and on nothing else
about the deployment — no inbound port, no DNS, no relationship to whatever
host is eventually chosen or how the dashboard is reached.

## Alternatives

**A hook per event.** Rejected: three places to forget, and the one event the
roadmap names that is not yet built would have been forgotten first.

**Alerting from the log layer** — a `structlog` processor that ships every
`CRITICAL`. Tempting, since every one of these already logs `CRITICAL`.
Rejected: it makes the alerting contract "whatever anyone happens to log at
this level", which is a contract nobody can reason about and which grows a
notification every time someone raises a log level in an unrelated file. It also
inverts the dedup — the log line is per occurrence, the alert must be per
transition.

**Alerting from the halt pub/sub channel**, with a subscriber process.
Rejected for now: a second process to run, monitor and restart, whose own death
is silent, in exchange for decoupling that one operator does not need. It is
where this goes if alerting ever needs to outlive the worker.

**A background queue and a delivery thread.** Rejected as premature. The
blocking call happens after the halt is durable, so the worst case is a wait,
not a lost halt. If the wait becomes the problem the queue goes behind
`NtfyAlertSink` and nothing else changes.

**Email or SMS.** Email is not an alert — it arrives silently among sixty
others. SMS via Twilio is genuinely good and costs an account, a number and a
per-message charge; it is one class implementing the port on the day the
operator wants it.

**Picking one transport when both are configured.** Rejected: it makes a
configured credential silently do nothing, which is discovered at the worst
possible time. Fanning out costs a `for` loop.

**Telegram with `parse_mode`.** Rejected: the alert body carries
operator-supplied text (`halt.py --detail`), and Telegram rejects a message
whose markup does not balance. An unclosed asterisk in a note typed during an
incident would make the notification about that incident fail to send.
