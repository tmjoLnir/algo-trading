# 32. The paper host moves off the Mac to an Oracle Cloud A1

**Status:** **Superseded** by [ADR 0033](0033-the-mac-stays-and-the-condition-becomes-a-number.md) ·
accepted 2026-09-10 · superseded 2026-09-11 ·
supersedes [ADR 0021](0021-the-paper-host-is-the-operators-own-mac.md)

> **Superseded after one day, and the text below is left exactly as it was
> written.** What was believed at the time is the point (`docs/adr/README.md`),
> and a decision that lasted a day is worth being able to read.
>
> This ADR's own Alternatives section named the paragraph that would undo it —
> *"it is entirely possible that `sudo pmset -c disablesleep 1`, verified once,
> was all that stood between day 3 and a clean week"*. On 2026-09-11 that
> command was run and read back, and the same readout supplied the feedback loop
> this document argued did not exist. [ADR 0033](0033-the-mac-stays-and-the-condition-becomes-a-number.md)
> keeps the Mac and makes its condition a checkable number with a trip-wire that
> points back here.
>
> **Nothing was ever provisioned at Oracle**, so none of the consequences below
> took effect. The sizing, the region argument and the Pay-As-You-Go reasoning
> stand as research and are the route if the trip-wire fires.

Supersedes 0021, which chose the operator's own Mac. It does not reopen
[ADR 0011](0011-one-vm-deployed-by-hand.md): the *shape* — one always-on VM per
run mode, the compose stack, reached over a private network, deployed by hand —
is unchanged, and this picks a different machine to be that VM. It is also not a
new idea. **It takes the fallback ADR 0021 named for itself**, with the vendor
chosen on [HOSTING.md](../HOSTING.md)'s survey rather than on price alone.

## Context

ADR 0021 made its own choice conditional, and said so in the strongest terms it
had:

> **Sleep is now a configuration item, and it is the whole risk.** [...] a paper
> week interrupted by sleep is not a paper week, so the four weeks
> `docs/SAFETY.md` asks for are gated on this being handled rather than
> intended.

Three sessions have since been run against a live paper account. None of them
measured a strategy, and each was voided by a different defect:

| Session | Voided by | Host |
|---|---|---|
| [Day 1](../paper-week/day-1-review.md) · 2026-09-03 | `StrategyRunner` built with a hard-coded `Timeframe.D1` against 1-minute bars — `on_bar()` was invoked zero times in ten hours | Held. 11:16→21:23 continuous |
| [Day 2](../paper-week/day-2-review.md) · 2026-09-08 | Traded 76 orders, protected none of them | Held, **except a 129.6-second whole-host stall at 11:43:47**, pre-market, filed under papercuts as "no trading impact" |
| [Day 3](../paper-week/day-3-review.md) · 2026-09-09 | Two independent failures — see below | **Dark 8.8 h of a 10.5 h capture. Inside RTH, 311.3 of 390 minutes: 79.8%** |

Day 3 is the one that forces this decision, and it forces only half of it. The
platform was permitted to trade for **16 minutes 56 seconds**. It submitted one
order, four shares filled, the protective stop was refused as a potential wash
trade, the adapter misclassified the rejection, the worker died, and the
reconciliation guard then refused 129 consecutive boots against a book the
broker disagreed with — leaving a 10-share MSFT position open and unprotected
through the close. **Only the first half of that is a hosting question.**

### The objection to this ADR, stated first

**The condition was never applied.** `docs/LOCAL_HOSTING.md:423` prescribes
leaving the machine overnight and reading `pmset -g log` for sleep events before
trusting it, and day 3's own finding is blunt about it: *"already prescribes
exactly this and it was not done."* On that reading this is a discipline
failure, the fix is one command — `sudo pmset -c disablesleep 1` — and moving
hosts to answer it is a category error that trades a free machine nobody can
take away for a vendor that has already halved its terms without an
announcement.

That objection is correct on the facts and is the reason this ADR could be
wrong. Three things answer it.

**It is two sessions, not one.** Day 2's 129.6-second whole-host stall was the
same failure at a scale that did not hurt, and the review filed it as a
papercut. The first observation of this failure mode was mislabelled; the second
cost 79.8% of a session. A failure whose small form is indistinguishable from
noise is one that gets one warning, and this one has spent it.

**The platform cannot see it, so the discipline has no feedback.** Every
container goes silent together, and nothing in the stack reports that. The
`StalenessMonitor` detects the symptom and reports it as lost market data —
which is the wrong diagnosis, and sends the operator to Alpaca's status page for
an event that happened under their own desk. Discovering day 3's outage required
correlating healthcheck timestamps across four containers after the fact. A
control that depends on a human remembering a setting, and that reports nothing
when the setting is missing, is not a control.

**The machine's primary purpose is in tension with the mitigation.** ADR 0011
rejected a laptop outright; 0021 narrowed the disqualifying property from *being
a laptop* to *sleeping*, and made the narrowing conditional on configuration.
The narrowing has now been tested against the machine's other job — the one
where it gets closed, carried, and rebooted for reasons that have nothing to do
with trading, which 0021 recorded as its own consequence — and the machine
slept. `pmset -c sleep 0` asserts only on AC power; `caffeinate -s` dies with
the terminal that holds it. Each of those is defensible in isolation and each is
a way for the setting to be true yesterday and false today.

### What the requirement actually is

`docs/SAFETY.md` asks for four weeks of paper trading before live. That is
roughly twenty consecutive sessions, each of which must survive a failure mode
the platform cannot report, on a machine that is also somebody's computer. Day 3
stopped the count and said so: *"stop the paper week. Do not run day 4 on this
host. Fix the sleep configuration or move the stack; fix the crash loop; then
restart the count at day 1."*

And it named where to go, quoting 0021 back at itself:

> **If sleep discipline cannot be made reliable, take ADR 0021's own fallback.**
> It named one: *"A commodity US-East VPS [...] the fallback for paper if the
> sleep discipline proves unworkable in practice."*

[HOSTING.md](../HOSTING.md) prices that fallback and finds one option that
clears the same bar at zero: Oracle Cloud Always Free's Ampere A1 — 2 OCPU /
12 GB, 200 GB of block storage, `us-ashburn-1`. 0021 rejected it on three
grounds, all still true and all now weighed against a host that has lost two
sessions rather than against one that had lost none.

## Decision

**The paper deployment target is a single Oracle Cloud Ampere A1 instance in
`us-ashburn-1`, and the operator's Mac stops being a host.**

1. **One instance, `VM.Standard.A1.Flex`, 2 OCPU / 12 GB** — the whole current
   Always Free entitlement, not split. 12 GB is above DEPLOYMENT.md's
   "comfortable" row; 2 OCPU is below its 4, which is the right trade for a
   platform whose backtest loop is one Python thread and whose worker is one
   asyncio process.
2. **`us-ashburn-1`.** The home region is fixed at signup and cannot be changed
   afterwards, and it is the clause of ADR 0011 that 0021 gave up: Alpaca's API
   is in US-East and the order path is what latency is spent on. Regaining it is
   the second reason for this move and the only one that is a benefit rather
   than the avoidance of a harm.
3. **The tenancy is upgraded to Pay As You Go, with a budget alert set the same
   day.** This is a decision, not an operational detail. Oracle reclaims Always
   Free compute that spends a 7-day window under 20% CPU *and* 20% network *and*
   20% memory, and this stack idles by design — DEPLOYMENT.md's own advice is to
   buy for the backtests rather than for the running stack. Accepting
   reclamation would replace a host that sleeps with a host that is taken away,
   which is the same outcome through a different door. Oracle scopes reclamation
   to Always Free accounts, so the upgrade removes it; usage inside the
   entitlement still bills nothing. **The cost of this clause is that a
   misconfiguration can now charge a card**, and the budget alert is the control
   for it.
4. **ADR 0011's x86-64 clause stays amended.** 0021 amended it for Apple
   Silicon; this keeps it amended for Ampere. Both rest on the same analysis in
   HOSTING.md — every image in the stack publishes a `linux/arm64` manifest and
   nothing in this repository pins a platform — and on the same caveat, which
   has still not been discharged by anybody.
5. **Live does not go here.** `docs/SAFETY.md` layer 3 wants paper and live on
   separate machines with separate key pairs. This ADR chooses a paper host and
   nothing else, exactly as 0021 did.
6. **The procedure is [ORACLE_HOSTING.md](../ORACLE_HOSTING.md)**, which is
   already written and is unchanged by this ADR. Its §11 said adopting it
   required an ADR naming what changed the answer. This is that ADR.

### The condition is a command, not a sentence

This is the part that is a lesson from 0021 rather than a difference from it.

0021 stated a condition in prose, in a document, and the condition was not met —
not because anyone disagreed with it, but because **an ADR cannot run**. So the
outstanding condition here, the unverified ARM build, is discharged by named
commands at a named step: ORACLE_HOSTING.md §5 builds every image on the host,
runs the unit suite on it, applies the TimescaleDB migration that exercises the
two TSL features nothing else would catch, and does all of it *before* anything
is trading. The cutover is not complete until §10's checklist has been run on
the host.

**A condition nobody can execute is a condition that will be intended.** If a
future ADR here states one, it states the command that discharges it.

## Consequences

**What ADR 0021's consequences become.** Each of these is that document's own
consequence section, inverted:

| 0021 said | Now |
|---|---|
| Sleep is a configuration item and the whole risk | Gone. A rented VM does not sleep |
| The stack does not come back after a restart unless Docker Desktop starts at login | dockerd is a systemd unit; `restart: unless-stopped` acts as written |
| Docker Desktop's ceiling is what the stack gets, not the Mac's RAM | 12 GB is the host's, and `timescaledb-tune` sizes the database from it |
| US-East proximity is given up deliberately | Regained |
| Provider snapshots are gone, so BACKUPS.md is load-bearing from day one | Volume backups exist again (5 in the Always Free entitlement) — **and BACKUPS.md stays load-bearing anyway**: a volume backup in the same tenancy is not off-site |
| It is also the machine the operator uses for everything else | It is not. A reboot for an unrelated reason stops being a deploy-time event |

**What it costs, and none of these existed before.**

- **The tenancy is a vendor, and this one has form.** The A1 allowance halved on
  15 June 2026 with no announcement, and instances over the new limit were
  stopped from 18 August. The Mac could not be reclaimed, resized or repriced,
  and that was the best thing about it.
- **Idle reclamation is removed by clause 3 rather than by argument.** It is a
  vendor action, not a technical property, and the class of vendor action
  remains.
- **The ARM build is still unproven** until §5 is run. This ADR does not get to
  declare it works, for the same reason 0021 did not.
- **The cutover costs a session and is the one operation that puts two hosts in
  front of one Alpaca key pair.** Alpaca refuses the second stream with code
  406, and `docs/RUNBOOK.md` calls two workers a duplicate-position incident.
  ORACLE_HOSTING.md §7 is ordered around that and this ADR does not restate it.
- **Backups move from launchd to cron and lose launchd's catch-up** for a job
  missed while the machine slept — which was only ever a mitigation for the
  problem this ADR removes.

**This addresses one of day 3's two failures, and must not be read as dealing
with day 3.** The wash-trade rejection that the Alpaca adapter classified as
`InsufficientFundsError`, the exception escaping the `trade_updates` consumer,
and the unconditional reconciliation guard that turned one bad fill into 129
identical boots under `restart: unless-stopped` — none of that is a hosting
question, and all of it survives this move intact. So does the unprotected
position that day 3 ended with. Fixing the crash loop is the other half of day
3's recommendation and it is not this ADR.

**The platform still cannot say when its host is dark.** Day 3 asked for
`host_dark_minutes` as a first-class number in the daily report, on the grounds
that a silence stopping *every* container is a host event and the staleness
monitor diagnoses it as a feed outage. Nothing here supplies that, and an A1
instance stopped for exceeding an allowance is dark in exactly the way a
sleeping Mac was. **This move removes the most likely cause and none of the
blindness**, which is the sharpest thing to know about it.

**The paper week restarts at day 1**, per day 3's recommendation. Days 1, 2 and
3 do not count toward `docs/SAFETY.md`'s four weeks; each was voided by a
different defect and none measured a strategy.

**The Mac stays intact and down for a week**, as ORACLE_HOSTING.md §8's rollback
target, and then stops being a host. Its LaunchAgents are unloaded and Docker
Desktop's start-at-login is the thing to check, because a stopped stack on a
machine that starts its daemon at login is one command away from being a second
worker on the same key pair.

**[LOCAL_HOSTING.md](../LOCAL_HOSTING.md) stays in the tree** and stops being
the procedure. It is the rollback route and the reference for anyone running
this on a Mac for development, and its §1 is now the best-documented reason not
to.

**The roadmap item does not move.** "Deployment target chosen; secrets manager"
is ticked by a host with the stack on it, `scripts/status.py` answering, and an
alert that reached a phone. This is the **third** ADR to choose a target and the
third to deploy nothing, which is worth noticing rather than repeating: what
closes that item is a cutover that happened, not a fourth choice.

## Alternatives considered

**Fix the Mac's sleep configuration and re-run.** One command, no cutover, no
vendor, no ARM question, and the platform keeps a host that cannot be taken
away. This is the strongest alternative and the way this ADR is wrong if it is
wrong: it is entirely possible that `sudo pmset -c disablesleep 1`, verified
once, was all that stood between day 3 and a clean week, and that 0021 was right
and merely unlucky in its operator. Rejected on the record rather than on
principle — the command was already prescribed, in this repository, at a line
number day 3 could cite; the failure appeared first as a papercut and was
filed as one; and the machine has a second job that will go on generating
reasons for the setting to lapse. **If a future reader is undoing this ADR,
this is the paragraph to argue from.**

**A dedicated Linux box on the same desk** — a mini PC or a NUC. Better than A1
on nearly every axis that matters: no vendor, no reclamation, no allowance that
moves, the same systemd dockerd, and hardware nobody can resize underneath you.
0021 named it the upgrade path and rejected it for not existing yet, which is
still true. Rejected here on sequencing: three sessions have already been lost,
provisioning free capacity takes an afternoon and buying hardware takes a week.
**This ADR deliberately does not close that door** — A1 costs nothing and the
cutover procedure is the same one in reverse, so superseding this with a box on
the desk is cheap, and is the expected end state if paper trading survives.

**A commodity US-East VPS** (DigitalOcean, Vultr, Linode, Hetzner, Lightsail —
ADR 0011's own list). This is 0021's stated fallback and day 3's recommendation,
taken literally, and it buys a vendor with ordinary terms and an SLA instead of
one that has already moved. A1 is the same thing in the same region at 12 GB for
nothing. The difference is money against vendor risk, and it is a genuinely
close call — the honest tiebreak is that a free host can be abandoned for a paid
one the moment its terms move, and the reverse costs a subscription to discover.

**Two A1 instances now, paper and live.** One entitlement splits into two 1 OCPU
/ 6 GB hosts, above DEPLOYMENT.md's 4 GB floor and below its comfortable row on
both halves, which would make `docs/SAFETY.md` layer 3 structural today.
Rejected: the four weeks have not started, so the live host would sit idle
accumulating vendor risk to solve a problem nobody has yet, and it would halve
the paper host to do it.

**Keep the Mac and build the monitoring instead** — alert on a dark host rather
than move off it. Rejected as insufficient rather than as wrong: `host_dark_minutes`
is worth building whatever host this runs on, and is listed above as an open
consequence precisely because this move does not supply it. But an alert that a
session is being lost does not stop the session being lost, and the thing
`docs/SAFETY.md` needs is twenty of them in a row.
