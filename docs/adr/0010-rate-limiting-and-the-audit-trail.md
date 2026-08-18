# 10. Rate limiting on the door, and an audit trail worth reading

**Status:** Accepted · 2026-08-18

## Context

Two halves of one Phase 6 item, related because the second is how you find out
the first mattered.

**Rate limiting.** `SECURITY.md` has listed "no rate limiting on `/auth/login`"
as a known gap since authentication landed, and ADR 0008 was explicit that
bcrypt's quarter-second verification is "a brake, not a lock". Guessing the one
password is the only attack this API has before a session exists.

**The audit trail.** `AuditLogRow` has been in the schema and the initial
migration since the first commit, with **nothing writing it and nothing reading
it** — the state `SignalRow` is still in. Its docstring promises "every
consequential human action: kill switch, live-mode toggle, manual order,
strategy promotion to live", and `killswitch.py` says clearing a halt is "always
audit-logged". None of that was true. What "audit-logged" meant in practice was
a `structlog` line, which is a different thing: an operational stream, rotated
away, in a format nobody promised, read by whoever is already debugging.

## Decision

### Rate limiting, on the unauthenticated surface only

A fixed-window counter in Redis, keyed on the client address, applied to
`/auth/login`. Not to the authenticated surface: with one operator that would be
defending against them abusing their own platform, at the cost of a limit that
can misfire on the dashboard's own five-minute poll from however many tabs are
open.

**Keyed by address, not by username.** Counting per username lets anyone who
knows the operator's name lock them out of their own trading platform by failing
to log in as them — turning a brute-force defence into a denial of service.

**Attempts, not failures.** A correct password is refused too once the limit is
reached. Otherwise the last guess in a run — the one that happens to be right —
is exactly the one the limiter waves through.

**It fails open.** An unreachable Redis allows the attempt and logs `CRITICAL`.
Failing closed on a *login* limiter locks the operator out during the outage
they most need to look at, and the degraded state is bcrypt alone rather than
nothing.

**`/risk/halt` must never be rate limited**, whatever is added later. Same
reasoning that lets a read-only session call it (ADR 0009), with more force: a
limiter that refuses a halt has chosen the wrong thing to protect.

### An audit trail that records what actually happens

A port in `atp_core.audit`, a Postgres adapter over the table that already
exists, and a read endpoint the dashboard renders.

**A failed audit write must never fail the action.** Adapters swallow and log
`CRITICAL`; callers are not asked to care. The actions worth auditing include
halting trading, and a platform that refused to stop because Postgres was down
would have its failure modes exactly inverted — a missing row is a gap in the
record, a refused halt is a position nobody can close.

**Reading is the opposite, and answers 503.** An empty page and an unreachable
record are different sentences, and only one of them means nothing happened.
This is the same rule the dashboard applies to the book: "nothing published is
not an empty book" (ADR 0007).

**Only actions that occur are recorded**: `login`, `login_failed`, `logout`,
`rate_limited`, `forbidden`. The order-flow and kill-switch verbs the table's
docstring anticipates are *not* wired, because every one of those handlers is
still a `NotImplementedError` stub — a write behind a stub is dead code, and a
constant for an event nothing emits is a claim the record does not support.
They land with their handlers.

**`actor` means "who we know this was".** A failed login is attributed to
`anonymous` with the typed username in `detail`. Writing the typed name into
`actor` would let anyone put any name in the audit trail by failing to log in as
them, which is the opposite of what the column is for.

## Consequences

**The record is thinner than the table's docstring implies**, and deliberately
so. Today it answers "who signed in, from where, what was refused, and was
anyone grinding at the door" — genuinely useful, and honestly less than "every
consequential human action". docs/ROADMAP.md says which half is missing and why.

**The login endpoint now depends on Redis and Postgres**, but only softly: both
degrade rather than refuse. A login works with neither, minus the throttle and
minus the record, each announced loudly.

**`X-Forwarded-For` is trusted for the first hop.** It has to be — behind nginx
every request otherwise shares one bucket and one attacker locks out everyone.
It is caller-supplied and spoofable, which is survivable only because this stack
always sits behind its own nginx. On a public address an attacker could rotate
the header to sidestep the limit, which is one more reason docs/SAFETY.md says
not to put it there.

**Fixed window, not sliding.** A caller who spends their allowance at the end of
one window and again at the start of the next gets twice the limit across the
seam. For tens of attempts against a bcrypt hash that is not the difference
between safe and unsafe, and a sliding window costs a sorted set per caller.

## Alternatives

**Rate limiting everything.** Rejected: the risk is guessing, which happens at
one endpoint, and a limit on the authenticated surface can only misfire on the
operator's own dashboard.

**Wiring audit writes into the stubbed handlers now**, so the record looks
complete. Rejected — it is dead code behind `NotImplementedError`, and a
constant list that names events nothing emits makes the record look more
authoritative than it is.

**Auditing to Redis instead of Postgres.** Rejected by a rule this codebase
already states: `persistence.events` says nothing that must not be lost may
travel over pub/sub, and an audit row is read weeks later by someone with no
context to reconstruct it.

**Recording every request.** Rejected: a row per GET buries the events worth
reading under the dashboard's own polling. The record's value is that everything
in it means something.

**Locking the account after N failures, rather than throttling the address.**
Rejected for the same reason the key is an address: it hands anyone who knows
the username a way to lock the operator out.
