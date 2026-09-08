# 29. The halt carries its reason, and the carve-out reads the evidence

**Status:** Accepted · 2026-09-08

Narrows the exit carve-out ADR 0027 catalogued. ADR 0027's two-book split is
unchanged and this depends on it.

## Context

`KillSwitchRule` refuses everything while a halt stands, **except an order that
can only make the position smaller**. That carve-out is right and hard-won:
docs/SAFETY.md draws the line — "Halting stops new risk; flattening realises
existing P&L" — and a rule that refused every order froze the platform's ability
to *reduce* risk while leaving the position on. Day 1 of the paper week held a
global halt for 2h37m and survived it only because the book was empty throughout
(docs/paper-week/day-1-review.md, F3).

**The carve-out rests on a claim that two halts exist precisely to deny.** "This
order can only make the position smaller" is computed from `Position.qty`. Two
of the seven `HaltReason`s are engaged *because that number is in doubt*:

- `OrderRouter._resolve_indeterminate` — a submit failed in transport and could
  not be resolved against the venue. Its own comment: "we may be holding a
  position nobody knows about", and "flattening against a position that may not
  exist opens a short".
- `Reconciler.reconcile` — our quantity and the broker's disagree.

Under either, an exit sized off `Position.qty` is sized off the number in
dispute. Held-100-believed, actually-1,000: `flatten` sells 100 and the carve-out
waves it through as a reduction. Held-100-believed, actually-0: `flatten` sells
100 into a flat account and **opens a short of 100 while the platform is
stopped** — the exact outcome the halt exists to prevent, produced by the
mechanism added to make halts survivable.

### Why the obvious fix is a worse bug

Key the exception on `HaltReason`: refuse exits under `reconciliation_mismatch`
and `broker_unreachable`, permit them otherwise. One line, and it is a
platform-wide outage.

`ReconciliationReport.is_clean` is false for **five** kinds of finding, and three
of them say nothing about any quantity:

| Finding | Says the book is wrong? |
|---|---|
| `position_qty`, `missing_position`, `unknown_position` | yes |
| `cash` — drift past a **$1.00** tolerance, from fees settling late | no |
| `orphan_order` — "most often a protective stop we placed before a restart" | no |

`reconcile_positions` runs **every five minutes, unattended**. A dollar of
late-settling interest engages `RECONCILIATION_MISMATCH`; keyed on the reason,
that dollar refuses every exit and every protective stop across the entire book
until a human arrives. That is day 1's F3 reintroduced, with docs/SAFETY.md's
layer 5 ("broker-side stops on every position") taken down by a layer 6 fault
that concerns neither.

`broker_unreachable` is worse, because the *same reason* warrants opposite
verdicts depending on where it came from. From the router it names one order
whose outcome is unknown. From the reconciler it means only that we could not
read the venue — an **unverified** book, not a **disproven** one, and every
position in it is exactly as closeable as it was a minute earlier.

A reason is a label on an incident. What the rule needs is evidence.

## Decision

**The halt record carries what the platform cannot prove, and the carve-out is
void for exactly those symbols.**

```python
@dataclass(frozen=True, slots=True)
class Impugnment:
    symbols: tuple[str, ...]  # sorted, non-empty, uppercase
    reason: HaltReason
    at: datetime
    by: str
    detail: str = ""


class HaltRecord:
    reason: HaltReason  # what stopped trading — moves only upward
    escalation: HaltEscalation | None = None  # where it came from, if it rose
    impugned: tuple[Impugnment, ...] = ()  # append-only: what we cannot prove
```

`engage` gains a keyword-only `unproven_symbols`. Two callers supply it — the
reconciler, with the symbols from findings whose `DiscrepancyKind.impugns_position`
is true, and the router, with the one symbol it could not resolve. Every other
caller supplies nothing and every other halt behaves exactly as it did.

`KillSwitch.halt_state()` replaces `is_engaged` **on the Protocol**, returning
every halt covering the order. The rule asks `state.position_is_unproven(symbol)`.

Five properties, each chosen against the alternative:

- **Evidence, not reason.** The table above is the argument. `Impugnment` also
  carries its own reason and actor, so an operator reads *why* SPY is unproven
  rather than inferring it from the halt's current label.
- **Per symbol, not per book.** A book of five positions with one bad quantity
  keeps four closeable. A book-wide flag makes every incident a full freeze on
  exits, which is the failure this rule was widened to prevent.
- **`halt_state` replaces the boolean on the Protocol, rather than joining it.**
  A rule that can still ask "am I halted" will, and it gets the pre-0029
  behaviour silently. `mypy --strict` names every implementation and every
  double. `RedisKillSwitch.is_engaged` survives for the callers that genuinely
  want a boolean and are not risk rules — the staleness monitor, the dashboard's
  banner, `scripts/halt.py status` — documented as "not for a risk rule".
- **`unreadable` does not impugn.** A Redis outage fails closed on `engaged`, so
  entries stop. It is *not* evidence about any position, and treating it as such
  would refuse every protective stop on every blip — layers 5 and 6 failing
  together on a fault that says nothing about the book. Three states, not two:
  "halted for reasons we could not read" and "halted for reasons that impugn
  nothing" are different, and a caller that conflates them picks a policy nobody
  chose.
- **`DiscrepancyKind` is a `StrEnum` with `match`/`assert_never`**, not a set
  membership test. **Both defaults are wrong** — a sixth kind defaulting to
  "impugns" strands every stop on a benign finding, defaulting to "does not"
  lets a flatten through against a disputed quantity — so a kind added later
  fails `make typecheck` until somebody decides which it is.

### The reason rises, and only rises

A halt already standing when evidence arrives must not stay labelled `manual`:
the banner, the alert, the metric and `rollover_daily_counters` all read
`HaltRecord.reason`, and leaving it says the incident is the one an operator
already dismissed. So `_merge` is a **one-way latch**:

- `engaged_at`, `engaged_by` and `detail` never move. A halt that re-stamps
  itself erases the only evidence of when trading actually stopped.
- `reason` rises once — from a halt that proves nothing about the book to one
  that does — and the original is kept in `escalation`. It never falls: an
  operator pressing HALT beside a standing `reconciliation_mismatch` cannot
  loosen it, which would drop the impugnment and let through the flatten that
  was refused a second earlier.
- `impugned` is appended to, and **only when the incoming engage names a symbol
  no standing impugnment covers**. Without that clause the five-minute reconcile
  turns a two-hour incident into twenty-four identical impugnments and
  twenty-four alerts. The Redis state is the dedup, exactly as ADR 0012 already
  has it for the halt notification.

`_merge` is pure and total, so the whole latch is one testable function rather
than a branch tangled into a Redis round trip.

### Engaging became atomic, because it had to

`engage` was GET-then-SET. Two processes reacting to one incident could lose an
update — AUDIT.md finding 48, filed when the cost was an audit field. The cost is
now the impugnment the carve-out reads, so:

`SET NX` wins the uncontended case in one round trip. On contention it reads,
`_merge`s, and writes through a two-line compare-and-set script — `WATCH`/`MULTI`
would need a dedicated connection and a transaction held open for the same
guarantee. Bounded at **three** attempts, because the contention is two processes
reacting to one incident and not a hot loop; exhaustion raises
`KillSwitchUnavailableError` rather than returning. A caller that believes it
halted trading and did not is worse than an exception: every decision after it is
taken on the assumption that the book is frozen.

The key vanishing between the `SET NX` and the `GET` — a human clearing the halt
in that window — rounds again rather than dereferencing `None` out of the
platform's stop button.

**Exhaustion is not an outage, and the two must not read alike.** Reaching the
third failure means every round found the key *occupied*: the store answered,
and a halt is standing. What did not land is this call's reason and — the part
that matters — its impugnment. So the error names the symbols that are *not*
recorded as unproven, and `POST /risk/halt` answers **409** rather than the
**503** it gives an unreachable store. The 503's message is "nothing was
written, trading resumes on its own when the store recovers"; saying that here
would send an operator to re-halt an already halted platform and leave them
believing a symbol is closeable that the reconciler could not prove.

## Consequences

**Three rules can still refuse an order that only reduces a position** —
`rules.EXIT_BLIND_RULES`, unchanged by this ADR. What changed is that the kill
switch now refuses reductions in *named symbols*, for stated evidence.

**A protective stop in an unproven symbol is refused.** This is the
uncomfortable case and it is stated rather than hidden: a stop is sized off the
same `Position.qty` the reconcile just disputed, so placing it against an
unproven quantity is how a stop becomes a short. `_alert_escalated` therefore
names the symbols in the alert body — a departure from `_alert_engaged`, which
keeps the book out of it — because a list of tickers the platform will not close
is not a position size, and an operator who must open a dashboard at 3am to learn
*which* symbols are stuck has been told the wrong half of the message. The escape
is the broker's own UI, and the alert says so.

**The escalation alert is keyed on the symbols, not only the reason.** A second
incident naming QQQ beside a standing `reconciliation_mismatch` on SPY produces
the same scope, target and reason as the first — so a key built from those three
is byte-identical and a deduping sink swallows exactly the message this alert
exists to deliver. The set is what changed, so the set is in the key. The log
line likewise reports `newly_unproven` and omits `from_reason` when the reason
did not move, because `manual -> manual` reads as a transition that did not
happen.

**`metrics.halt_escalated` is a second counter, not a second `halt_engaged`.**
One incident must not read as two on the graph. What it marks is different in
kind: not that trading stopped, but that exits stopped too, for the symbols
named.

**The only automated clear in the platform stays out of reach of an escalated
halt.** `rollover_daily_counters` releases yesterday's `daily_loss_limit` halt,
gated on `record.reason`. An escalated halt no longer carries that reason, so it
is skipped — the right answer, since a halt covering an unproven quantity is not
a cron job's to release. It is now skipped *loudly*:
`worker.rollover.halt_escalated_not_released` names the symbols, keyed on
`escalation.from_reason` so halts that were never in this job's reach stay quiet.
Escalation can only move a halt **out** of the auto-clear set and never into it:
`engaged_by` never moves, and the only caller that engages as `DAILY_LOSS_RULE`
supplies no symbols.

**Verified by mutation, not by argument.** Twenty-six mutants — the latch
removed, the dedup removed, the carve-out keyed on `HaltReason`, the impugnment
read book-wide, `unreadable` treated as evidence, the alert key stripped of its
symbols, `SET NX` made unconditional, the reconciler's classification inverted in
both directions, the router impugning nothing and impugning everything, the
rollover's new log silenced and over-fired, `halt_state` returned to decoding all
three keys as one generator, an undecodable record no longer failing closed, the
creation-path alert silenced and over-fired, and the contended raise asserting
each of its two outcomes unconditionally — each killed by a named test.

**Three of those mutants are this ADR's own defects**, found by an adversarial
review of the branch that introduces it and fixed before merge: the collapsed
decode above, the silent creation-path alert, and the contended raise that
claimed a halt was standing when the last round had found the key gone. They are
recorded rather than quietly restated, and that the review found them at all is
the argument for running one — every gate was green over all three.

**What this does not fix.** Nothing clears an impugnment except clearing the
halt. A reconcile that finds SPY correct on its next pass does not retract the
earlier finding, so the symbol stays unproven until a human resumes trading.
That is the conservative direction and it is deliberate for now — a retraction
mechanism is a second decision, about who is allowed to say a position is proven
again, and it wants its own record.

## Alternatives considered

**Key the carve-out on `HaltReason`.** The one-line version. Rejected on the
table above: a dollar of late-settling fees refuses every exit and every
protective stop in the book, unattended, every five minutes.

**Refuse *all* exits under any halt, reverting the carve-out.** This is the
pre-F3 behaviour and its cost is documented: the platform loses the ability to
reduce risk at the moment it most needs it, and entries that filled just before
the halt keep their positions with no stop anywhere.

**A book-wide `book_is_unproven` boolean on the record.** Simpler, and it makes
every incident a full freeze on exits. The property that makes the exception
survivable in production is that four of five positions stay closeable.

**Keep `is_engaged` on the Protocol beside `halt_state`.** Rejected: a rule that
can ask the easy question will, and the failure is silent. The same reasoning
ADR 0027 gives for changing every rule's signature rather than adding an opt-in
protocol — a guard nobody is told they have opted out of is the failure mode
this codebase keeps finding.

**Store `unproven_symbols` as a flat set on the record.** It is what the rule
reads, so it looks sufficient. Rejected: an operator paged at 3am needs *why*
and *when* and *who*, and a set answers none of them. The set is derived from
`impugned` instead, so the evidence and the rule's input can never disagree.
