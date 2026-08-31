# Choosing a host

What a machine has to be able to do before this stack will run on it, which
free offerings actually clear that bar, and what each of the ones that do not
fails on.

[ADR 0011](adr/0011-one-vm-deployed-by-hand.md) chose the *shape* — one
always-on VM per run mode, the compose stack, reached over a VPN, deployed by
hand — and deliberately did not pick a vendor. [DEPLOYMENT.md](DEPLOYMENT.md)
is the specification and the procedure. **This document is neither.** It is a
survey of what is available at zero cost, written against that specification so
that the choice can be made with the tradeoffs in front of it.

> **A target has since been chosen, and this document did not choose it.**
> [ADR 0021](adr/0021-the-paper-host-is-the-operators-own-mac.md) picked
> "hardware you already own" — specifically the operator's own Mac — for the
> paper host, on the reasoning in the section of that name below.
> [LOCAL_HOSTING.md](LOCAL_HOSTING.md) is the procedure.
>
> This survey stays as it is, and stays useful for two reasons: **live still
> needs a second host** (`docs/SAFETY.md` layer 3), and the Mac is explicitly a
> starting point rather than a destination. `docs/ROADMAP.md`'s Phase 6 item
> "Deployment target chosen; secrets manager" also stays open — nothing has
> been provisioned, and a chosen host is not a host.

**Vendor terms below were checked on 2026-08-26 and will drift.** One of them
moved eight days before that date and moved by half — see Oracle, below. Treat
every figure here as needing re-checking before you buy against it, and prefer
the vendor's own page to this one where they disagree.

---

## What the platform is asking for

The numbers — vCPU, RAM, disk, region — are in
[DEPLOYMENT.md](DEPLOYMENT.md#size) and are not repeated here, because two
copies of a sizing table is one table that will be wrong. The short version is
**2 vCPU / 4 GB / 40 GB to start, 4 vCPU / 8 GB / 80 GB to be comfortable**,
US-East, plus an NTP-synced clock, 2–4 GB of swap and snapshotted block storage.

What matters for *this* question is not the sizing. It is the five constraints
that disqualify a host outright, each of which is in the code rather than in
anyone's preference:

| # | Constraint | Where it comes from |
|---|---|---|
| 1 | **Nothing scales to zero.** The worker holds a WebSocket through the session, reconciles every 5 minutes, snapshots every minute and sweeps for gaps at 02:00 | ADR 0011 constraint 2 |
| 2 | **The worker is a singleton**, enforced by the venue: Alpaca refuses a second stream per key with code 406 | ADR 0011 constraint 1; `docs/RUNBOOK.md`, "two workers running" |
| 3 | **TimescaleDB is self-hosted and needs the TSL edition** — the initial migration sets `timescaledb.compress` and calls `add_compression_policy` | `infra/alembic/versions/8140ae9c6209_initial_schema.py` |
| 4 | **Docker Compose v2.24 or newer**, because `!reset` in the overlay is load-bearing and an older compose ignores it silently | `docker-compose.prod.yml` |
| 5 | **Root on a machine that stays up**, with persistent block storage — Redis holds kill-switch state with `appendonly yes`, so that volume is a safety asset and not a cache | ADR 0011 constraints 2, 3 |

Constraint 1 removes every scale-to-zero platform. Constraint 2 removes every
orchestrator whose default rollout overlaps instances. Constraints 3 and 5
remove the free managed-database tiers that might otherwise have taken the
heaviest service off the box. What is left is a plain Linux VM, which is the
thing ADR 0011 already concluded.

## What is already free

Worth stating before the survey, because it means the machine is the only line
item:

| | |
|---|---|
| Alpaca paper trading | Free, and where a host stays for at least the four weeks `docs/SAFETY.md` asks for |
| [Tailscale](https://tailscale.com/pricing) Personal | Free — 6 users, unlimited devices. This is the access layer `docs/DASHBOARD.md` already recommends |
| ntfy.sh | Free, nothing to sign up for. Self-hostable if you want `ALERT_NTFY_TOKEN` |
| Telegram bot | Free |
| SOPS + age | Free, and neither is a daemon |

So "hosting this for free" reduces to one question: **is there a machine.**

## The survey

| Option | Clears the bar? | Why |
|---|---|---|
| **Oracle Cloud Always Free** (Ampere A1) | **Yes** | 2 OCPU / 12 GB, 200 GB block storage, `us-ashburn-1` is US-East. The only free tier that clears the RAM floor. Three caveats below |
| **Hardware you already own** | **Yes** | Meets every constraint except US-East proximity. No vendor can reclaim it |
| Google Cloud Always Free (e2-micro) | No | `us-east1` qualifies and it is permanent, but **1 GB RAM** and 30 GB of standard persistent disk. DEPLOYMENT.md budgets 15–20 GB for Docker before a single bar is stored |
| AWS `t4g.small` free trial | No | 750 h/month, but **ends 31 Dec 2026**, and 2 GB RAM. EBS is not free for accounts created on or after 15 Jul 2025 — those get $200 of credits for 6 months instead of the 12-month service tier |
| Azure free account | No | The three free sizes — B1s, B2pts v2, B2ats v2 — are **all 1 GiB**, and free for 12 months only |
| Render free | No | Services spin down after 15 minutes idle. Constraint 1 |
| Railway | No | No free tier since 2023; a one-time $5 trial credit |
| Fly.io | No | Free allowance ended for organisations created after 7 Oct 2024; new accounts get a trial of 2 VM-hours or 7 days. ADR 0011 also rejected the multi-machine shape on constraint 2 |
| Neon free Postgres | No | Ships **Apache-2 `timescaledb` only**. Native compression is TSL, so the initial migration fails on `add_compression_policy` |
| Supabase free Postgres | No | `timescaledb` cannot be enabled on new Postgres 17+ projects — the TSL relicensing is incompatible with their platform |

The last two are worth reading twice: they mean you cannot split the database
off onto a free managed tier and put a smaller VM under the rest. Constraint 3
is not negotiable by re-arranging the deployment.

## Oracle Cloud Always Free, in detail

It is the only free tier that runs the actual stack, and it is genuinely
capable: **12 GB of RAM is above DEPLOYMENT.md's "comfortable" row**, which
matters because that row exists to leave the database room to be tuned for the
host it thinks it has. `us-ashburn-1` is US-East, which is where ADR 0011 wants
the order path to be.

The A1 allowance can also be **split across more than one instance**. Two
instances at 1 OCPU / 6 GB each gives you the separate paper and live hosts
`docs/SAFETY.md` layer 3 asks for, out of one free tenancy — tighter than
comfortable on both, but above the 4 GB floor.

Three caveats, in the order they are likely to bite.

### 1. It is ARM64, and DEPLOYMENT.md says x86-64

That requirement is documentation, not enforcement. Checked on 2026-08-26:

- `timescale/timescaledb:2.15.2-pg16` publishes a `linux/arm64` manifest. This
  is the one that could have ended the question, and it does not.
- `python:3.12-slim`, `node:20-alpine`, `nginx:1.27-alpine`, `redis:7-alpine`
  and `ghcr.io/astral-sh/uv` are all multi-arch.
- **Nothing in the repository pins a platform.** No `platform:` key in either
  compose file, no `--platform` in any Dockerfile. The only `x86` in the tree is
  prose: `docs/DEPLOYMENT.md` and ADR 0011.
- One concrete break: DEPLOYMENT.md's `sops` install line fetches
  `sops-v3.9.4.linux.amd64`. On ARM that is the `linux.arm64` asset.

**What that is and is not.** It is evidence that nothing structural stops an
ARM build — manifests and the absence of pins. It is **not** a build that has
been run, a suite that has passed, or a stack that has come up on an A1
instance, and this repository's standard for a claim is the latter. Anyone who
deploys to ARM should expect to find something, and should amend ADR 0011
rather than this file when they do: the architecture is a decision that ADR
made, and a survey does not get to overturn it.

### 2. The allowance was halved in June 2026

It was 4 OCPU / 24 GB. Since 15 June 2026 it is **2 OCPU / 12 GB**, and from
**18 August 2026** instances above the new limit are shut down until resized.
There was no blog post and no announcement; users found out by email or by
finding their instance stopped.

Size for 12 GB. More usefully: treat the allowance as a thing that moves, and
keep the host reproducible enough that moving off it is a provisioning job
rather than a rescue. That is most of what `make deploy` and the SOPS bundle
already buy you.

### 3. Idle reclamation is a real risk for *this* workload

Oracle reclaims Always Free compute that, over a 7-day window, is under **20%
CPU and 20% network and 20% memory** (the memory condition applies to A1
shapes). All three must hold.

This stack idles by design — that is DEPLOYMENT.md's own advice to "buy for the
backtests, not for the running stack". The 20% memory condition is the one
likely to save you: `timescaledb-tune` sizes `shared_buffers` from the host's
RAM, so Postgres alone will claim something like 3 GB of 12 GB and clear that
threshold without help. Since all three conditions must hold simultaneously,
that should be enough.

**Should be** is the operative phrase, and it is not a thing to assume about
the machine your trading platform lives on. If you deploy here, confirm the
instance is not accumulating idle time before you rely on it.

## Hardware you already own

**This is the option that was taken** —
[ADR 0021](adr/0021-the-paper-host-is-the-operators-own-mac.md), for paper only,
with the procedure in [LOCAL_HOSTING.md](LOCAL_HOSTING.md). What follows is the
reasoning as it stood before that choice, which is what the ADR was decided
against.

A mini PC, a NUC, or a laptop that has stopped being a laptop. It is the only
permanently free option that no vendor can reclaim, resize or reprice, and the
access layer is unchanged: Tailscale is already how DEPLOYMENT.md reaches the
dashboard, and it does not care whether the host is in Ashburn or under a desk.

The tradeoff is the one ADR 0011 actually cares about. **US-East proximity is
on the order path**, and a host at the far end of a residential connection puts
your distance from Alpaca into every submission. Your own distance from the
*dashboard* is irrelevant — it is read on demand (ADR 0022), so latency there
costs one round trip when you ask for one — but the order path is a different
question, and how much it costs depends on where you are and what the strategy
does. A daily-bar
strategy will not notice. Anything reacting within the bar will.

Two smaller things, both solvable and both worth knowing in advance: a
residential connection has no snapshots, so DEPLOYMENT.md's "provider snapshots
are the entire recovery story" stops being true and [BACKUPS.md](BACKUPS.md)
becomes load-bearing earlier; and power and connectivity are now yours, which
against a fail-stopped platform whose stops are held broker-side is an
inconvenience rather than an incident.

## Where free stops

**Going live means two hosts.** `docs/SAFETY.md` layer 3 wants paper and live
on separate machines with separate key pairs, and its stated failure mode is
"live keys deployed to the paper env". One free tenancy split in half is below
the comfortable spec for both halves.

So the honest shape of this is: **free is enough to earn the paper week**, which
is what three phases of `docs/ROADMAP.md` are currently waiting on, and it is
the paper week that decides whether there is anything worth paying for. A second
VM is a decision to make after that, not before it.

## If you are choosing today

- **A free paper host, accepting vendor risk:** Oracle A1, sized for 12 GB,
  having read all three caveats and confirmed the ARM build yourself.
- **A free paper host, accepting latency instead:** hardware you own, with
  BACKUPS.md scheduled from the first day rather than the first incident.
  **This is what was chosen** (ADR 0021). If the machine is one that sleeps —
  a Mac, or any laptop — that is a disqualifying property until it is
  configured away, and LOCAL_HOSTING.md §1 is the part to read before the
  rest.
- **Neither is free:** any commodity VPS in US-East at the 8 GB / 4 vCPU row.
  ADR 0011 names DigitalOcean, Vultr, Linode, Hetzner and Lightsail and declines
  to choose between them, which is still the right answer.

Whichever it is, the thing that closes the roadmap item is not the choice. It
is a host with the stack on it, `scripts/status.py` answering, and an alert that
reached a phone.
