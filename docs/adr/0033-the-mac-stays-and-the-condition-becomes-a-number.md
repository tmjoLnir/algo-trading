# 33. The Mac stays, and its condition stops being a sentence

**Status:** Accepted · 2026-09-11 ·
supersedes [ADR 0032](0032-the-paper-host-moves-off-the-mac.md)

Supersedes 0032, which moved the paper host to an Oracle Cloud A1 and was
accepted the previous day. **This is the third host decision in three weeks**,
and that churn is a cost this ADR has to earn back rather than ignore — the
section "Why believe this one" exists for it.

It does not reopen [ADR 0011](0011-one-vm-deployed-by-hand.md). The shape — one
always-on VM per run mode, the compose stack, reached over a private network,
deployed by hand — is untouched by all three.

## Context

[ADR 0021](0021-the-paper-host-is-the-operators-own-mac.md) chose the operator's
Mac, conditional on the machine being configured not to sleep, and called that
condition "the whole risk". [ADR 0032](0032-the-paper-host-moves-off-the-mac.md)
superseded it on the evidence that the condition had never been applied: day 3
of the paper week was dark for 79.8% of regular trading hours, and day 2 had
recorded the same failure smaller as a 129.6-second whole-host stall that its
review filed under papercuts.

0032 also wrote down, in its own Alternatives section, exactly how it expected
to be wrong:

> **Fix the Mac's sleep configuration and re-run.** [...] it is entirely
> possible that `sudo pmset -c disablesleep 1`, verified once, was all that
> stood between day 3 and a clean week, and that 0021 was right and merely
> unlucky in its operator. [...] **If a future reader is undoing this ADR, this
> is the paragraph to argue from.**

That is the paragraph being argued from, and two things have happened since it
was written.

### The condition has been applied, and read back

On **2026-09-11 at about 13:35 +0800**, `sudo pmset -c disablesleep 1` was run
and `pmset -g` was read back rather than assumed:

```
System-wide power settings:
 SleepDisabled		1
Currently in use:
 sleep                0 (sleep prevented by powerd, sharingd, bluetoothd)
```

`SleepDisabled` is the system-wide setting, so it holds with the lid closed,
which `pmset -c sleep 0` alone does not. This is the first time in the life of
this repository that the property ADR 0021 was conditional on has been observed
to be true.

**It is not yet the property that matters, and this ADR must not be read as
saying it is.** A flag that is set is not a machine that stayed awake through a
session. That distinction is precisely what killed 0021 — LOCAL_HOSTING.md §1
already separated "did I set the flag" from "did it hold", and the second half
was never done. It still has not been done. It is scheduled, below, as a number.

### The same readout supplied the missing feedback loop

0032's strongest argument was not that the Mac sleeps. It was that **nothing
tells you when it has**: every container goes silent together, the
`StalenessMonitor` diagnoses it as lost market data and sends the operator to
Alpaca's status page, and discovering day 3's outage took correlating
healthcheck timestamps across four containers after the fact. A control with no
feedback is not a control.

`pmset -g log` ends with one line that answers it:

```
Total Sleep/Wakes since boot at 2026-09-11 06:57:50 +0800 :5
```

Five cycles between the 06:57 boot and the 13:35 reading — six and a half hours
on defaults, which is the failure still in progress on the morning this was
written. It is also a **monotonic integer that can be read in two seconds and
can only go up.** That is not the in-platform `host_dark_minutes` day 3 asked
for, and it does not page anybody. It is enough to make the condition
falsifiable by an operator who checks it, which is a different thing from a
sentence in a document and is the whole of what changed.

### And one fact that argues against this decision

The operator's clock is **UTC+8**. US regular trading hours of 13:30–20:00 UTC
are **21:30–04:00 local** — day 3's capture ran 19:08 to 05:41 local time. The
entire paper session happens overnight, unattended, on a machine that is also
the operator's daily driver and may well have its lid shut.

0032 did not know this and it cuts **toward** a rented host, not away from one.
It is recorded here because an ADR that omits the evidence against itself is an
argument rather than a record. What it changes concretely is below: nobody is
awake to notice a halt, so the alert path stops being a nicety and becomes the
only channel that exists between 21:30 and 04:00.

## Decision

**The paper host is the operator's own Mac. Nothing is provisioned at Oracle.**

And the part that is not a return to 0021:

**The condition is a number, a command and a consequence.**

1. **The setting**, applied and read back — `SleepDisabled 1`, `sleep 0`,
   observed 2026-09-11. On AC power, and the machine stays on AC.
2. **The invariant.** `Total Sleep/Wakes since boot` was **5** at
   2026-09-11 13:35 +0800. Across any session in which the platform is expected
   to trade, **that number must not increase.** One line, two seconds, and it
   cannot be satisfied by intending anything:

   ```bash
   pmset -g log | grep -i "Total Sleep/Wakes"
   uv run python scripts/status.py
   ```

   Read both after the close, not before it. The second is what says whether a
   gap reached the bars.
3. **The consequence, decided now rather than at the time.** If that number
   increases inside a session the platform was trading, **the host moves**, and
   [ORACLE_HOSTING.md](../ORACLE_HOSTING.md) is the route with ADR 0032's
   reasoning behind it. Not a fresh argument, not a fourth ADR weighing vendors
   again — that work is done and is sitting in the tree. This ADR is superseded
   by its own trip-wire.

**Live still does not go here**, unchanged through all three: `docs/SAFETY.md`
layer 3 wants paper and live on separate machines with separate key pairs, and a
daily driver is the least suitable candidate for the second half.

### Why believe this one

Three host ADRs in three weeks is a bad sign, and the honest reading is that the
first two were decided by argument. 0021 argued the Mac would be configured;
0032 argued it had not been and would not be. Neither named a reading that could
settle it.

This one is falsifiable by an integer that a person can check in less time than
it takes to open the dashboard, and it names what happens when the check fails
before anybody is invested in the answer. That is the only material difference,
and if it turns out not to be enough, the trip-wire fires and nobody has to
re-open the question.

## Consequences

**[LOCAL_HOSTING.md](../LOCAL_HOSTING.md) is the procedure again**, and its §1
gains the two things this ADR found: the read-back that proves the setting took,
and the sleep/wake integer as the check that proves it held.

**The tense in the surrounding documents was wrong and is corrected here.** The
propagation that landed with 0032 said the Mac was "no longer the deployment"
and that 0032 "moved the paper host" — past tense, for a cutover that never
happened. Nothing was ever provisioned. That was a documentation defect on the
day it merged, independent of which host wins, and this change fixes it rather
than inverting it.

**[ORACLE_HOSTING.md](../ORACLE_HOSTING.md) stays, and gets more useful rather
than less.** It is now the costed escape route that §3 of the Decision names by
file. A procedure nobody has run is worth keeping exactly when the condition
that would trigger it is written down; ADR 0032's sizing, its region argument
and its Pay-As-You-Go reasoning all survive this supersession, because
superseding a decision does not discard the research under it.

**Backups go back to launchd**, and `docs/BACKUPS.md` says so: the macOS recipe
is live and the cron lines are the deferred ones. launchd's catch-up for a job
missed while the machine slept is once again a property that matters — though if
the invariant above holds, it should never fire.

**The alert path is now a precondition, not a checklist item.** Nobody is awake
between 21:30 and 04:00 local. `scripts/check_alerts.py --by "you"` reaching an
actual phone is the only thing standing between a halt at 02:00 and finding out
at breakfast, and `docs/SAFETY.md`'s checklist line is not met on this host
until somebody has watched a notification arrive. Day 3 sent 129 of them, which
is the other way that channel fails.

**`host_dark_minutes` is still not built, and this decision leans on its
absence.** Day 3 asked for it as a first-class number in the daily report. The
sleep/wake integer is an operator check, not an alert: it tells you afterwards,
and only if you look. Building the real thing is what would let this host be
trusted rather than watched, and it is the most valuable unbuilt item this
sequence of ADRs has produced.

**The paper week still restarts at day 1.** Days 1, 2 and 3 were each voided by
a different defect and none measured a strategy. Nothing here changes that
count, and this ADR is not evidence that day 4 will be clean.

**The roadmap item does not move.** "Deployment target chosen; secrets manager"
is ticked by a host with the stack on it, `scripts/status.py` answering, and an
alert that reached a phone. Three ADRs have now chosen a target; the item is
closed by a session that ran, not by a fourth choice — and on this host two of
those three conditions are within one day's reach, which is the argument for
staying put.

## Alternatives considered

**Provision the A1 now, as ADR 0032 decided.** Still a coherent plan, still
fully costed in ORACLE_HOSTING.md, and it is the one this ADR is closest to
being wrong about — the UTC+8 finding above is a genuine new argument in its
favour that 0032 did not have. Rejected because 0032's central claim was that
the sleep condition had never been tested and had no feedback, and within a day
of it merging both halves of that changed: the setting is applied and read back,
and there is a number to check it against. Moving hosts is a cutover that costs
a session and puts two hosts in front of one Alpaca key pair; doing it the day
after the cheaper fix was applied, and before the cheaper fix has been given one
session to fail, is spending that cost to avoid an experiment that takes a day.

**Keep 0032 and simply defer the move.** The state the repository was actually
in for fourteen hours, and the worst of the three. A decision nobody intends to
execute makes every other ADR read as aspirational, and the documents around it
had already drifted into describing a cutover that had not happened. Better to
say where the platform lives and name what would change it.

**A dedicated Linux box on the same desk** — a mini PC or a NUC. Still better
than either the Mac or the A1 on the axes that matter: no vendor, no
reclamation, no allowance that moves, dockerd under systemd, and not also
somebody's daily driver. ADR 0021 named it the upgrade path and rejected it for
not existing; that is still true, and it is still the expected end state. It
also does not fix the UTC+8 problem, which is about *when* the session runs and
not about *what* it runs on.

**Build `host_dark_minutes` first and decide afterwards.** Tempting, and it is
the right thing to build. Rejected as a *gate*: it is platform work of uncertain
size standing between the project and a paper week that three phases of the
roadmap are waiting on, and the operator check above is available today. Build
it next, not first.
