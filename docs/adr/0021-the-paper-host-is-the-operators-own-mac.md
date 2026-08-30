# 21. The paper host is the operator's own Mac

**Status:** Accepted · 2026-08-30

Extends [ADR 0011](0011-one-vm-deployed-by-hand.md), which chose the deployment
*shape* and deliberately left the machine open. This picks the machine. It
amends one clause of 0011 — the architecture — and leaves the rest standing.

## Context

Three documents have been circling this decision without making it.
[ADR 0011](0011-one-vm-deployed-by-hand.md) chose one always-on VM per run
mode, the compose stack, reached over a private network, deployed by hand, and
said in as many words: "the vendor is deliberately not the architectural
decision and is not fixed here". [docs/HOSTING.md](../HOSTING.md) surveyed what
can satisfy that specification, opening with "**This does not choose a
target.**" `docs/ROADMAP.md` Phase 6 has carried "Deployment target chosen" as
open throughout.

The operator has a Mac and intends to run the dashboard on it. That is the
choice, and it should be recorded as one rather than arrived at by nobody
writing it down.

**ADR 0011 already rejected this machine, and that is the objection to answer.**
Its context says:

> None of it is demonstrable on a laptop that sleeps, so the missing host is
> what is holding three phases' worth of ticks

That sentence is correct and this ADR does not overturn it. What it does is
narrow it: the disqualifying property is *sleeping*, not *being a laptop*. A
Mac that is prevented from sleeping holds a session; a Mac left on defaults does
not. So the decision below is conditional on a configuration step, and the
consequence section treats that step as load-bearing rather than as advice.

HOSTING.md already listed "hardware you already own" as one of exactly two
options that clear the bar, and named its two real costs — US-East proximity and
the loss of provider snapshots. Both are accepted below.

## Decision

**The paper deployment target is the operator's own macOS machine**, running the
same compose stack through `make deploy`, with automatic sleep disabled for as
long as a session is running.

Nothing about the stack changes. `docker-compose.prod.yml` is applied exactly as
ADR 0011 specified; `make check-bindings` still gates the deploy;
`ATP_WEB_BIND_ADDR` is still the one port that may leave loopback. The procedure
is [docs/DEPLOYMENT.md](../DEPLOYMENT.md), and
[docs/LOCAL_HOSTING.md](../LOCAL_HOSTING.md) records only the steps where a Mac
under a desk differs from the rented Linux VM that document assumes.

**Live trading does not go here.** `docs/SAFETY.md` layer 3 wants paper and live
on separate machines with separate key pairs, and one machine that is also the
operator's daily driver is the least suitable candidate for the second half.
This ADR chooses a paper host and nothing else.

**This chooses the target. It does not deploy it.** Nothing has been provisioned
and nothing has been run. The roadmap item stays open and says why.

## Consequences

**Sleep is now a configuration item, and it is the whole risk.** A Mac that
sleeps mid-session drops the Alpaca stream, stops the five-minute reconcile and
the one-minute snapshot, and comes back with a wall clock that has jumped —
against `StalenessMonitor`, which measures silence in wall-clock seconds, and a
platform whose hardest bug class is anything reading wall-clock time (CLAUDE.md
§5). The *direction* of that failure is right: the watchdog halts, the kill
switch fails closed, and broker-side stops hold the positions. But a paper week
interrupted by sleep is not a paper week, so the four weeks `docs/SAFETY.md`
asks for are gated on this being handled rather than intended. LOCAL_HOSTING.md
carries the mechanics and the way to verify it held.

**The architecture is arm64, and ADR 0011 says x86-64.** This amends that
clause. HOSTING.md did the analysis already, for Oracle's Ampere A1: every image
in the stack publishes a `linux/arm64` manifest —
`timescale/timescaledb:2.15.2-pg16`, `python:3.12-slim`, `node:20-alpine`,
`nginx:1.27-alpine`, `redis:7-alpine` — and nothing in this repository pins a
platform, in either compose file or any Dockerfile. The conclusion transfers to
Apple Silicon, and **so does the caveat, verbatim**: that is evidence nothing
structural stops an ARM build, not a build that has been run or a suite that has
passed. This ADR does not get to declare it works. An Intel Mac is x86-64 and
the question does not arise.

**US-East proximity is given up, deliberately.** ADR 0011 wanted the order path
near Alpaca. A residential connection puts your own distance into every
submission. HOSTING.md's framing is the right one and is accepted here: a
daily-bar strategy will not notice; anything reacting within the bar will. The
dashboard is unaffected — it refreshes every five minutes, so the operator's
distance from it was never on the path.

**Provider snapshots are gone, and DEPLOYMENT.md's recovery story goes with
them.** That document says "provider snapshots are the entire recovery story"
until backups exist; on owned hardware there is no provider and no snapshot.
[docs/BACKUPS.md](../BACKUPS.md) is therefore load-bearing from the first day
rather than the first incident, and `ATP_BACKUP_DIR` must point at something
that is not this machine's disk — a dump beside the database it came from dies
with it. A Time Machine copy of a running Postgres volume is not a substitute:
it is a file-level copy of a database mid-write.

**Docker Desktop is a VM with its own resource ceiling, and that ceiling is what
the stack gets.** DEPLOYMENT.md's sizing table describes a host; on macOS the
relevant number is what Docker Desktop is permitted, not what the Mac has. The
10-symbol five-year minute-bar backtest in that table needs 4.2 GB for the bar
objects alone and will be OOM-killed against a smaller cap no matter how much
RAM is installed.

**The stack no longer comes back on its own after a restart** unless Docker
Desktop is set to start at login. `restart: unless-stopped` acts on containers
once the daemon is running; it cannot start the daemon. On the Linux VM ADR 0011
assumed, dockerd is a systemd unit and this problem does not exist. This is the
same class of failure as the one that ADR documented — a stack that comes back
in pieces and looks alive — arriving through a different door.

**One machine remains a single point of failure**, unchanged from ADR 0011, with
one addition: it is also the machine the operator uses for everything else. A
reboot for an unrelated reason is now a deploy-time event.

**It costs nothing.** The machine is owned. HOSTING.md's conclusion holds — free
is enough to earn the paper week, and the paper week is what decides whether
there is anything worth paying for.

## Alternatives

**A commodity US-East VPS** (DigitalOcean, Vultr, Linode, Hetzner, Lightsail —
ADR 0011's own list). Still the right answer for live, and the fallback for
paper if the sleep discipline proves unworkable in practice. Rejected for now
only because it costs money to answer a question — "does this strategy do
anything" — that the machine already on the desk can answer.

**Oracle Cloud Always Free (Ampere A1).** The other option HOSTING.md found that
clears the bar: free, US-East, 12 GB. Rejected for three reasons that document
sets out in detail — the allowance was halved without announcement in June 2026
and instances over the new limit were stopped in August; idle reclamation is a
real risk for a workload that idles by design; and it is ARM64 with the same
unverified-build caveat as the Mac, so it does not even buy certainty on that
axis. A machine that cannot be reclaimed is worth more here than a machine in
the right region.

**A dedicated Linux box on the same desk** — a mini PC or a NUC. Better than the
Mac on every axis that matters: no sleep problem, dockerd under systemd, not
also somebody's daily driver. Rejected only because it does not exist yet.
This is the upgrade path if the paper week survives, and buying one is a
cheaper decision than a VPS subscription.

**Running only the dashboard locally against a remote API.** Rejected: the
dashboard is same-origin with the API by construction (ADR 0011,
docs/DASHBOARD.md), and splitting them means taking on `API_CORS_ORIGINS`, a
bundle built for one specific remote origin, and a second host anyway. "Host the
dashboard" means host the stack.
