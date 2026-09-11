# Migrating to an Oracle Cloud A1 host

How to move the paper deployment off the operator's Mac and onto an Oracle
Cloud Always Free **Ampere A1** instance, in the order the steps have to happen,
with the point of no return marked.

**This is the escape route, not the current plan.**
[ADR 0032](adr/0032-the-paper-host-moves-off-the-mac.md) decided this move and
was superseded a day later by
[ADR 0033](adr/0033-the-mac-stays-and-the-condition-becomes-a-number.md), which
keeps the paper host on the operator's Mac. **Nothing was provisioned at
Oracle.**

That makes this document *more* useful rather than less, and 0033 is explicit
about it: that ADR names one condition — the Mac's sleep/wake count rising
during a session it was trading — and names this file as what happens next if it
does. The sizing, the region argument and the Pay-As-You-Go reasoning in 0032
stand as research; superseding a decision does not discard the work under it.

**It is still a procedure that nobody has run.** §11 has not moved: no part of
what follows describes something that happened.

Four documents cover this ground and none of them is redundant:

| Document | What it gives you here |
|---|---|
| [DEPLOYMENT.md](DEPLOYMENT.md) | **The procedure.** Sizing, `.env`, secrets, the deploy sequence, what to check after. Unchanged by this document and not restated in it |
| [HOSTING.md](HOSTING.md) | Why A1 clears the bar at all, and the three caveats. The ARM manifest analysis this document builds on |
| [LOCAL_HOSTING.md](LOCAL_HOSTING.md) | The host you are leaving, and the six ways it differs from a Linux VM. Read it to know what stops applying |
| **This document** | Oracle-specific provisioning, and the **cutover** — the part no other document covers, because migrating a running platform is not the same problem as deploying to an empty one |

Read DEPLOYMENT.md first. Everything below either provisions the machine it
assumes, or sequences the move onto it.

---

## The one rule that decides the order of everything

**There is one Alpaca paper key pair, and Alpaca refuses a second stream
connection on it with code 406.** `AlpacaRealtimeFeed` treats that as permanent
rather than retrying, deliberately, so that a second process fails loudly
instead of racing the first (ADR 0011, constraint 1). `docs/RUNBOOK.md` lists
"two workers running" as a cause of duplicate positions.

A migration is the one operation that puts two hosts in front of one key pair.
Every ordering decision in §7 exists to make sure that at no instant are both
capable of trading:

> **The Mac's worker stops before the A1 host's worker starts. Not overlapping,
> not "briefly", not "it was halted so it doesn't count".**

A halt is a *policy* held in Redis, and each host has its own Redis. A halted
worker still holds the stream. Stop the process.

---

## 1. Vendor facts, and when they were checked

HOSTING.md's convention, and it earns its place: **these moved once already,
by half, with no announcement.**

| Fact | Value | Checked |
|---|---|---|
| Always Free A1 allowance | **2 OCPU / 12 GB** (1,500 OCPU-hours, 9,000 GB-hours per month) — halved from 4/24 on **15 June 2026** | 2026-09-10 |
| Enforcement of the new limit | From **18 August 2026** instances over the entitlement are stopped or terminated until resized | 2026-09-10 |
| Always Free block storage | **200 GB total**, boot volumes and block volumes combined, in the home region. Default boot volume is 50 GB; 5 volume backups included | 2026-09-10 |
| Home region | Chosen at signup, **permanent**, and Always Free compute can only be created there | 2026-09-10 |
| Idle reclamation | Reclaimed if, over a 7-day window, 95th-percentile CPU < 20% **and** network < 20% **and** memory < 20% (the memory condition applies to A1 shapes). All three must hold | 2026-09-10 |
| Reclamation and paid accounts | Oracle scopes reclamation to Always Free accounts; converting to Pay As You Go is the documented way to stop it, and usage inside the Always Free limits still bills nothing | 2026-09-10 |
| "Out of host capacity" | A1 capacity in popular regions is intermittently unavailable to Always Free accounts; PAYG accounts get it far more reliably | 2026-09-10 |

Sources: [InfoQ](https://www.infoq.com/news/2026/07/oracle-cloud-free-tier-limits/)
and [Linuxiac](https://linuxiac.com/oracle-quietly-cuts-free-tier-ampere-a1-resources-in-half/)
on the halving; Oracle's own
[Always Free Resources](https://docs.oracle.com/en-us/iaas/Content/FreeTier/freetier_topic-Always_Free_Resources.htm)
and [Free Tier](https://docs.oracle.com/iaas/Content/FreeTier/freetier.htm)
pages for the entitlement, the home-region rule and the reclamation thresholds.

**Prefer the vendor's page to this table wherever they disagree**, and re-check
before you provision against it. The whole reason A1 was rejected in ADR 0021
is that this table is a thing that moves.

---

## 2. Decisions that cannot be undone later

Four of these are one-shot. Make them deliberately, before you open the console.

### 2.1 The home region — permanent, and it is the whole point of the move

Always Free compute exists **only in the tenancy's home region**, the home
region is chosen during signup, and it cannot be changed afterwards. Getting a
different one means deleting the tenancy and starting again with a different
email address.

**Choose `us-ashburn-1`.** ADR 0011 wanted a US-East region because Alpaca's
API is there and the order path is what latency is spent on; ADR 0021 gave that
up to use hardware the operator already owned. Regaining it is one of the two
reasons to do this migration at all — the other being that a rented Linux VM
does not sleep. A tenancy homed in London or Frankfurt buys you neither and
costs you the Mac's advantages, so this is the step where the migration is
either worth doing or is not.

`us-phoenix-1` or `us-sanjose-1` are US regions and are **not** what ADR 0011
asked for. Ashburn is where Alpaca's endpoints resolve.

### 2.2 Always Free, or upgrade to Pay As You Go

They are not the same host, and the difference is not the bill.

| | Always Free account | Upgraded to PAYG, staying inside Always Free limits |
|---|---|---|
| Cost of this stack | Nothing | Nothing, if you stay inside the entitlement |
| A1 capacity | Frequently "out of host capacity"; provisioning can take days of retries | Reliably available |
| Idle reclamation | Applies. §9.3 | Documented as not applying |
| Failure mode of a mistake | The instance stops | **A card gets charged** |

**Recommendation: upgrade to PAYG, and set a budget alert at $1 the same
afternoon.** Idle reclamation is a live risk for this workload specifically —
the stack idles by design, which is DEPLOYMENT.md's own advice about buying for
the backtests — and "my trading host was reclaimed" is not a failure mode worth
accepting to avoid entering a card. The cost of that choice is that a
misconfiguration can now bill you, which a budget alert and the Always Free
resource list make visible.

If you stay on Always Free, §9.3 is not optional reading.

### 2.3 The shape — size for 12 GB, and expect to resize downwards

`VM.Standard.A1.Flex`, **2 OCPU / 12 GB**. That is the whole current
entitlement in one instance.

Against DEPLOYMENT.md's sizing table, 12 GB is *above* the "comfortable" 8 GB
row and 2 vCPU is *below* its 4. That is the right trade for this workload:
CPU here is single-core-bound where it matters — the backtest loop is one
Python thread, the worker is one asyncio process — while RAM is what a
minute-bar backtest over a wide universe actually runs out of, and what
`timescaledb-tune` sizes the database from.

**Do not split the allowance across two instances now.** `docs/SAFETY.md`
layer 3 wants paper and live on separate hosts, and 1 OCPU / 6 GB each is above
the 4 GB floor but below the comfortable row on both halves — and live is a
decision to make after the paper week, not during a migration. One instance,
the whole allowance. §11 revisits it.

### 2.4 The disk — 200 GB total, and the backup destination comes out of it

The 200 GB Always Free block-storage budget covers the boot volume and any
block volumes together.

| Layout | Boot | Block | Consequence |
|---|---|---|---|
| Everything on boot | 200 GB | — | Simplest. `ATP_BACKUP_DIR` then points at the same disk as the database, which BACKUPS.md says in its first paragraph is not a backup |
| **Split (recommended)** | 100 GB | 100 GB, mounted at `/mnt/backups` | Dumps survive the instance being rebuilt, and the volume can be attached to a replacement |
| Default | 50 GB | — | **Too small.** DEPLOYMENT.md budgets 15–20 GB for Docker before a single bar is stored, and `make deploy` adds image layers on every deploy |

A separate block volume survives the *instance*. It does not survive the
*tenancy*, and it is not off-site — so it is an improvement on the Mac's boot
disk and is still not what BACKUPS.md means by a destination that outlives the
host. §9.2 closes that gap with object storage.

---

## 3. Provisioning the instance

Console steps, because this is done once. Everything after §4 is a terminal.

### 3.1 Sign up

1. <https://www.oracle.com/cloud/free/> → *Start for free*.
2. **Home region: US East (Ashburn) — `us-ashburn-1`.** §2.1. There is no
   second chance at this field.
3. A card is required for identity verification even on Always Free.
4. If you decided on PAYG (§2.2): *Billing & Cost Management* → *Upgrade and
   Manage Payment* → upgrade. Then *Budgets* → create a budget on the root
   compartment with an alert at a threshold you would want to hear about.

### 3.2 An SSH key you generate

```bash
# On the Mac, not in the browser. Oracle offers to generate one; a private key
# that arrived over HTTPS through a browser download is not the one to use for
# the host holding your broker credentials.
ssh-keygen -t ed25519 -C "atp-oracle-a1" -f ~/.ssh/atp_oracle_a1
```

Paste `~/.ssh/atp_oracle_a1.pub` into the instance-creation form. Keep the
private half where you keep the age key from §6.1 — the two together are the
host.

### 3.3 Network

*Networking* → *Virtual Cloud Networks* → **Start VCN Wizard** → *VCN with
Internet Connectivity*. Accept the defaults; the instance goes in the **public**
subnet.

**Add no ingress rules.** The default security list allows SSH on 22 and
nothing else, and that is one rule more than this host will eventually need.
Nothing in this stack is published:

- The dashboard binds to the host's Tailscale address (`ATP_WEB_BIND_ADDR`) —
  Tailscale is an *outbound* connection with no inbound port to open.
- Postgres and Redis stay on loopback, which is where `make check-bindings`
  insists they stay.
- Everything else is outbound HTTPS/WSS to Alpaca and the alert transport.

Tighten the SSH rule to your current address while you build the host, and
delete it entirely once §4.4 has Tailscale working.

### 3.4 Create the instance

*Compute* → *Instances* → *Create instance*.

| Field | Value | Why |
|---|---|---|
| Image | **Canonical Ubuntu 24.04**, aarch64 build | Current LTS; `get.docker.com` supports it on arm64. Oracle Linux 9 works and its default user is `opc` rather than `ubuntu` |
| Shape | `VM.Standard.A1.Flex`, **2 OCPU, 12 GB** | §2.3 |
| Boot volume | **100 GB**, VPU 10 (Balanced) | §2.4. Larger than the 50 GB default, deliberately |
| Subnet | The wizard's public subnet | §3.3 |
| Public IPv4 | Assign one | Needed to reach the box before Tailscale exists. It stops mattering after §4.4 |
| SSH key | `~/.ssh/atp_oracle_a1.pub` | §3.2 |

Then create the backup volume if you took the split layout: *Storage* → *Block
Volumes* → 100 GB → attach to the instance as **paravirtualized**, and follow
the console's own attach instructions to format and mount it at `/mnt/backups`.

### 3.5 When it says "Out of host capacity"

This is the normal experience on an Always Free account, not a fault. In order
of what actually works:

1. **Upgrade to PAYG** (§2.2). This is the fix; the rest are workarounds.
2. **Try each availability domain in turn.** Ashburn has three.
3. **Retry on a schedule** rather than by hand. The community scripts that poll
   the `launch-instance` API exist for this reason — treat any third-party
   script that wants your API keys with the suspicion that deserves, and read
   it before running it.
4. Ask for slightly less (1 OCPU / 6 GB) and resize once it exists. A running
   instance can be scaled inside the entitlement; a nonexistent one cannot.

Do not start the cutover (§7) until the instance exists, is reachable, and has
passed §5. Nothing about this step is time-bounded.

---

## 4. Bringing the host up to specification

DEPLOYMENT.md, "What the host has to be able to do", lists four things beyond
Docker. On an OCI Ubuntu image, three of them need action. `ssh -i
~/.ssh/atp_oracle_a1 ubuntu@<public-ip>`, then `sudo -i`.

### 4.1 Swap — there is none

```bash
swapon --show          # empty on a stock OCI Ubuntu image
free -h
```

DEPLOYMENT.md asks for 2–4 GB, as a cushion against a backtest that misjudged
its watchlist: a slow run beats an OOM kill during a session.

```bash
fallocate -l 4G /swapfile
chmod 600 /swapfile
mkswap /swapfile
swapon /swapfile
echo '/swapfile none swap sw 0 0' >> /etc/fstab
swapon --show          # confirm, and confirm again after the reboot in §9.1
```

`vm.swappiness` can stay at its default. This is an overflow cushion, not a
tier the database should be planning to use.

### 4.2 The clock

Not optional here. Bars are stamped at their open, `StalenessMonitor` measures
silence in **wall-clock seconds** against the exchange calendar, and every
timestamp in the platform is tz-aware UTC (CLAUDE.md §1.2). A drifting clock
produces halts that look like feed outages.

```bash
timedatectl                       # want: "System clock synchronized: yes", UTC
chronyc tracking                  # offset and the source it is tracking
chronyc sources -v
```

OCI publishes an internal NTP service at `169.254.169.254`, and the platform
images are generally configured to use it. If `timedatectl` reports the clock
unsynchronised, install `chrony` and point it there before doing anything else:

```bash
apt-get update && apt-get install -y chrony
sed -i '1i server 169.254.169.254 iburst' /etc/chrony/chrony.conf
systemctl restart chrony && chronyc tracking
```

**Leave the host in UTC.** Convert to exchange-local time for display only.

### 4.3 Docker

```bash
curl -fsSL https://get.docker.com | sh
docker compose version            # MUST be >= v2.24
```

That floor is not stylistic. `docker-compose.prod.yml` uses compose's `!reset`
tag to strip the base file's source bind mounts and the API's `--reload`, and a
compose that does not know the tag **ignores it silently** — the deployed stack
then runs whatever source is in the checkout instead of the image you built.
`make check-bindings` re-checks the resolved configuration for exactly this, and
it runs before every `make deploy`.

Then the thing that has no equivalent on the Mac and is the reason this host is
better:

```bash
systemctl is-enabled docker       # want: enabled
```

`restart: unless-stopped` acts on containers once the daemon is running; on
macOS it cannot start Docker Desktop, which is why LOCAL_HOSTING.md §2 has a
login-item step. Here dockerd is a systemd unit and the problem does not exist.

### 4.4 Tailscale, and then closing SSH

```bash
curl -fsSL https://tailscale.com/install.sh | sh
tailscale up
tailscale ip -4                   # 100.x.y.z — this is ATP_WEB_BIND_ADDR
```

Confirm from the Mac that `ssh ubuntu@100.x.y.z` works over the tailnet, and
only then delete the port-22 ingress rule from the security list in §3.3. The
host is now reachable on the tailnet and nowhere else.

Two Ubuntu-on-OCI details worth knowing even though this stack does not need
them: **OCI's Ubuntu images enforce a host firewall through `iptables` and
`netfilter-persistent`, not `ufw`**, with rules in `/etc/iptables/rules.v4` that
reject before any rule you append at the end of the file; and the security list
is a second, independent layer in front of that. For a stack that publishes
nothing this is a feature, and it is also the reason a port you *did* mean to
open appears not to work.

### 4.5 The rest of the toolchain

The host runs migrations, backfills, backups, `halt.py` and `status.py` itself
— ADR 0011's stated consequence, and why Postgres and Redis stay published on
loopback in the deployed configuration. That needs `uv`, and does **not** need
Node or a Postgres client:

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
. "$HOME/.local/bin/env"

git clone <your remote> /opt/atp
cd /opt/atp
uv sync --all-packages            # host-side venv for scripts/ and alembic
```

Node is absent deliberately: the dashboard bundle is built inside
`infra/docker/web.Dockerfile` during `make deploy`. Postgres client tools are
absent because every backup command takes `--exec compose` and runs them inside
the `db` container, where they are the server's own build.

Secrets tooling — and this is the one line in DEPLOYMENT.md that is
architecture-specific:

```bash
# arm64, NOT the linux.amd64 asset DEPLOYMENT.md fetches.
curl -fsSLo /usr/local/bin/sops \
  https://github.com/getsops/sops/releases/download/v3.9.4/sops-v3.9.4.linux.arm64
chmod +x /usr/local/bin/sops
apt-get install -y age
sops --version && age --version
```

---

## 5. Settle the ARM question here, not during the cutover

**This is the step people skip, and it is the one that turns a two-hour cutover
into a day.**

HOSTING.md did the analysis and ADR 0021 restated it: every image in the stack
publishes a `linux/arm64` manifest — `timescale/timescaledb:2.15.2-pg16`,
`python:3.12-slim`, `node:20-alpine`, `nginx:1.27-alpine`, `redis:7-alpine` —
and **nothing in this repository pins a platform**: no `platform:` key in either
compose file, no `--platform` in any Dockerfile.

**Read the caveat with it, because it has not changed.** That is evidence that
nothing *structural* stops an ARM build. It is not a build that has been run,
a suite that has passed, or a stack that has come up. CI runs on
`ubuntu-latest`, which is x86-64, so **no green tick anywhere in this repository
has ever built this stack for arm64.** The A1 host is the first machine that
will, and you want that to happen on a day when nothing is trading.

Do it now, with no `.env` worth anything and the stack pointed at nothing:

```bash
cd /opt/atp
uname -m                                    # aarch64

# 1. Every image builds on this architecture.
docker compose -f docker-compose.yml -f docker-compose.prod.yml build

# 2. The pure suite passes on it. Not `make check` — that runs eslint and tsc,
#    which need the node_modules this host deliberately does not have.
uv run pytest tests/unit -q

# 3. The resolved deployed configuration is the deployed shape.
python3 scripts/check_port_bindings.py
```

Then bring it up against a throwaway configuration — a random
`ATP_DB_PASSWORD`, `ATP_RUN_MODE=backtest`, no broker credentials — and confirm
the parts that are hardest to fix later:

```bash
make deploy
make migrate                                # TimescaleDB: the hypertable and the
                                            # compression policy on arm64
docker compose ps                           # every service up, none restarting
curl -sf http://127.0.0.1:8000/healthz
docker compose exec -T db psql -U atp -d atp -c "\dx"    # timescaledb, TSL edition
```

The migration is the one that matters most: the initial revision sets
`timescaledb.compress` and calls `add_compression_policy`, both of which are
**TSL** features, and a database image that quietly shipped the Apache-2 build
would fail there and nowhere else.

When you are satisfied, destroy the evidence so no throwaway configuration
survives into the real one:

```bash
docker compose --profile prod down -v       # -v: the volumes too
rm -f .env
```

**If something breaks here, that is a finding, and it belongs in the ADR §11
asks for** — not in a workaround on the host. ADR 0021 made the same promise
about Apple Silicon and nobody has collected on it either.

---

## 6. Secrets and configuration on the new host

DEPLOYMENT.md's "Configuring" and "Secrets" sections are correct as written.
Five things are specific to *migrating* rather than deploying fresh.

### 6.1 The age key is the one thing that cannot be regenerated

`infra/env/paper.sops.env` is in the repository and came with `git clone`. It
is unreadable without the private age key, which is on the Mac at
`~/.config/sops/age/keys.txt` and nowhere else.

Two ways across, and they are not equivalent:

**Copy the key** — simplest, and correct if the Mac is being retired:

```bash
# From the Mac, over the tailnet, never through a mail client or a chat window.
scp ~/.config/sops/age/keys.txt ubuntu@100.x.y.z:/tmp/keys.txt
# On the host:
install -d -m 700 /root/.config/sops/age
install -m 600 /tmp/keys.txt /root/.config/sops/age/keys.txt
shred -u /tmp/keys.txt
```

**Or generate a second key and add it as a recipient** — better if both
machines will exist for a while, because it means the Mac's key can be revoked
later without re-keying from source:

```bash
# On the new host:
uv run python scripts/manage_secrets.py init      # writes its own key, adds it to .sops.yaml
# Then, on a machine that can already decrypt, re-encrypt to both recipients
# and commit the updated .sops.yaml — a recipient is a public key.
```

Either way: **back the private key up offline before you rely on it.** Lose it
and every bundle encrypted to it is unreadable, and the only way back is
re-creating each one from the credentials at source.

### 6.2 Install the bundle, then set what may not be in it

```bash
cd /opt/atp
make secrets-install env=paper        # writes .env, mode 0600, atomically
make secrets-check env=paper          # decrypts; prints key names only
```

`scripts/manage_secrets.py` refuses `ATP_RUN_MODE`, `ATP_ALLOW_LIVE_TRADING` and
`WORKER_ALLOW_LIVE_ORDERS` on import *and* on install. They are host
configuration, not secrets, and a bundle is a thing that gets copied between
hosts — which is precisely what you are doing right now. **A migration must not
be able to switch on live trading as a side effect.** Set them in `.env` after
the install:

```bash
cat >> .env <<'EOF'
ATP_RUN_MODE=paper
ATP_ALLOW_LIVE_TRADING=false
ATP_ENV=production
ATP_LOG_FORMAT=json
EOF
chmod 600 .env
```

The third lock, `worker_config.allow_live_orders`, is a database column edited
on the dashboard's Config tab and travels in the dump you are about to restore.
It is not in `.env` and must not be moved there (CLAUDE.md §1.8).

### 6.3 The values that are host-specific and must change

Copying `.env` across unchanged is the mistake this table exists to prevent.

| Variable | On the new host |
|---|---|
| `ATP_WEB_BIND_ADDR` | The **A1 host's** `tailscale ip -4`, not the Mac's. Left empty, the dashboard is reachable from the host only — which on a remote box means an SSH tunnel every time |
| `ATP_DB_PASSWORD` | A **new** one. Postgres reads it at initdb and never again, so it has to be right before the first start of the `db` container in §7.8 |
| `DATABASE_URL` | The same new password. `make migrate`, `backup_db.py`, `halt.py` and `status.py` all run from the host and read this |
| `ATP_BACKUP_DIR` | `/mnt/backups` if you took the split layout (§2.4). Never the default, which is `backups/` inside the checkout |
| `API_SECRET_KEY` | Keep the Mac's, or accept that every existing session is signed out. It is a stored secret rather than one generated at boot, precisely so sessions survive a restart |
| `API_PASSWORD_HASH` | Unchanged — unless you are also rotating the operator password, in which case `uv run --package atp-api python scripts/hash_password.py` and paste the **single-quoted** line it prints |
| `METRICS_TOKEN` | Set it. Unset means nothing can scrape, and both processes say so at startup |
| `ALERT_NTFY_TOPIC` / `ALERT_TELEGRAM_*` | Unchanged — but the credential is per-host in the sense that matters: it is only proven by a send **from this host**, which is §7.11 |

### 6.4 Rotate the Alpaca paper keys — and mind what that does to the Mac

The Mac's plaintext `.env` has lived on a daily-driver machine. A migration is
the cheapest moment there will ever be to rotate the pair, and
[`SECURITY.md`](../SECURITY.md) has the order: **revoke at the broker first**, then
re-encrypt.

It also has a second effect that is useful if you sequence it right and painful
if you do not: **a revoked key cannot hold a stream.** Rotating makes the
single-worker rule structural for the duration of the cutover rather than
conventional. Do it at step §7.4, after the Mac's worker is already stopped —
rotating while it is running gets you an authentication failure in a live log
instead of a clean stop, and rotating before you have the new pair in the bundle
gets you a new host that cannot connect either.

### 6.5 Do not deploy yet

`make deploy` at this point would initialise an **empty** database with the new
password and leave you restoring into a database that already has a schema. The
first start of `db` on this host belongs inside the cutover, at §7.8, and the
restore comes before the stack.

---

## 7. The cutover

**Read the whole section before starting any of it.** Each step names what is
authoritative when it completes, so that an interruption leaves you somewhere
known rather than somewhere new.

**When.** `docs/SAFETY.md` rule 4: never on a Friday afternoon, never in the
last thirty minutes of a session. The best window is a **weekend**, when the
market is shut and a mistake costs time rather than a position. Second best is
pre-market with two clear hours. A migration is a deploy that also moves the
data; it gets more caution than a deploy, not less.

**Before you start**, on the Mac:

```bash
cd ~/algo-trading
uv run python scripts/status.py            # halts, quotes, bars, and the venue
docker compose ps
```

Write down what the broker says you hold. You will compare against it twice.

### 7.1 Halt the Mac

```bash
uv run python scripts/halt.py engage --by "<your name>" --detail "migrating to oracle a1"
```

Stops new risk. Open positions keep their broker-side stops throughout —
`docs/SAFETY.md` layer 5, and the reason a halt is not an emergency.

*Authoritative: the Mac.*

### 7.2 Decide about open positions — deliberately

| Situation | What to do |
|---|---|
| **Flat** | Best case. Cross over flat; nothing to reconcile |
| **Positions open, with broker-side stops** | Supported, and it is what `warmup()` is for — the new worker adopts them on first start. Budget time for §7.10 and expect to reconcile |
| **Positions open with no protection** | **Fix this before migrating, not after.** Day 3 of the paper week ended with a naked 10-share MSFT position and a worker that refused to start against it — 129 boots died on the reconciliation guard, and Docker restarted it into the same failure every time (`docs/paper-week/day-3-review.md`) |

That last row is not hypothetical and it is not old news: the guard is correct,
it is unconditional, and it exits the process. A book that does not match the
broker will stop the *new* host from starting, on a day when you have no old
host running to fall back to. **Reconcile on the Mac, while the Mac still
works.**

If you can flatten, flatten:

```bash
uv run python scripts/status.py            # confirm what the broker holds
# close deliberately, from the dashboard or the venue, and confirm it settled
```

### 7.3 Stop the Mac's worker — the singleton rule takes effect here

```bash
docker compose stop worker queue
docker compose ps                          # worker: Exited. db and redis still up
```

Not `make down` — the database has to stay up to be dumped. From this moment
until §7.9, **nothing anywhere is holding the Alpaca stream**, which is the
state the rest of the cutover depends on.

*Authoritative: the Mac's database. Nothing is trading.*

### 7.4 Rotate the broker keys, if you are doing it

§6.4. Revoke at the broker, generate the new pair, then on a machine that can
edit the bundle:

```bash
uv run python scripts/manage_secrets.py edit --env paper     # ALPACA_API_KEY / _SECRET
git commit -am "chore(secrets): rotate the paper key pair for the A1 host"
git push
```

The bundle's *keys* stay readable while its *values* are encrypted, so the diff
reads `ALPACA_API_SECRET changed` rather than showing one opaque blob. On the
A1 host, `git pull` then `make secrets-install env=paper` again.

### 7.5 Take the final backup, and prove it restores

```bash
# On the Mac. `--exec compose` runs the client tools inside the db container,
# so no matching pg_dump is needed on the host and the binaries are the
# server's own build.
DEST=/Volumes/backup1/atp-backups       # wherever this Mac's dumps actually live

uv run python scripts/backup_db.py create --exec compose --dir "$DEST"
uv run python scripts/backup_db.py verify --exec compose --dir "$DEST"
uv run python scripts/backup_db.py list  --check         --dir "$DEST"
```

**`--dir` explicitly, not `ATP_BACKUP_DIR`.** That variable is read from the
process environment and never from `.env`, so `make backup` in a shell that does
not export it writes the dump into `backups/` inside the checkout — beside the
database you are migrating away from. LOCAL_HOSTING.md's launchd wrapper carries
the same warning for the same reason.

**`backup-verify` is the step that makes this a migration rather than a hope.**
It restores the newest dump into a scratch database, compares row counts against
the manifest taken either side of the dump, confirms `bars` came back as a
hypertable with its compression policy, and reports the Alembic revision. Exit 1
means the dump does not restore — stop, and do not take the Mac down.

### 7.6 Move the dump, and check it arrived intact

```bash
# On the A1 host, once: the mount is root-owned as the console leaves it.
ssh ubuntu@100.x.y.z sudo chown ubuntu:ubuntu /mnt/backups

# From the Mac, over the tailnet.
scp "$DEST"/atp-atp-<stamp>.dump \
    "$DEST"/atp-atp-<stamp>.dump.json \
    ubuntu@100.x.y.z:/mnt/backups/

# Both ends must agree.
shasum -a 256 "$DEST"/atp-atp-<stamp>.dump               # on the Mac
ssh ubuntu@100.x.y.z sha256sum /mnt/backups/atp-atp-<stamp>.dump
```

Take the `.json` manifest as well. The dump restores without it;
`backup_db.py verify` needs it, and that is the command you will want on the
far side.

### 7.7 Bring up the database on the A1 host, alone

```bash
cd /opt/atp
git pull
make secrets-install env=paper                    # then §6.2's host lines
docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d db
docker compose ps                                 # db healthy, and nothing else
```

`db` initialises with `ATP_DB_PASSWORD` from §6.3 — **this is the one moment
that password is read**, and `timescaledb-tune` sizes `shared_buffers` and
`effective_cache_size` from the host's 12 GB here rather than from the Mac's.

### 7.8 Restore

```bash
uv run python scripts/backup_db.py list --check --dir /mnt/backups

uv run python scripts/backup_db.py restore \
  --dsn "postgresql://atp:$ATP_DB_PASSWORD@127.0.0.1:5432/postgres" \
  --file /mnt/backups/atp-atp-<stamp>.dump --into atp

make migrate                                      # if the checkout is ahead of the dump
```

`--dsn` is the supported path for a host whose configuration is not fully
installed yet. The restore refuses a dump from the other run mode — paper and
live databases look interchangeable and are not — and refuses an existing target
without `--overwrite`, which renames the existing database aside rather than
dropping it.

*Authoritative: the A1 host's database, as of the dump. Nothing is trading.*

### 7.9 Engage the halt **before** the stack starts

```bash
uv run python scripts/halt.py engage --by "<your name>" --detail "migrated; not yet reconciled"
```

**This is the step that inverts everything else in this document, and it is the
one most likely to be skipped because the host looks quiet.** A rebuilt host has
a Redis that is reachable and **empty**. The kill switch fails closed against an
*unreachable* Redis, not an empty one — so a freshly restored stack comes up
**willing to trade**, against a book as of the dump and a broker as of now. The
Mac's halt did not travel; it was a key in the Mac's Redis.

### 7.10 Deploy, and confirm the shape

```bash
make deploy                                       # check-bindings, then build and start

# DEPLOYMENT.md, "After every deploy" — all of it, on this host, now.
make secrets-check env=paper
docker compose ps                                 # every service up, none restarting
curl -sf http://127.0.0.1:8000/healthz
curl -sf http://$(tailscale ip -4):8080/healthz
python3 scripts/check_port_bindings.py
date -u; docker compose exec -T api date -u       # host and container clocks agree
```

Then the reconciliation this whole sequence was arranged around:

```bash
uv run python scripts/status.py
```

Compare it against what you wrote down before §7.1. The worker's `warmup()`
adopts open positions on start; if the book and the broker disagree beyond
tolerance the worker refuses to start and says so — and that refusal is
correct, so read `docs/RUNBOOK.md` rather than working around it.

### 7.11 Prove the alert path from *this* host

```bash
uv run python scripts/check_alerts.py --by "you"
```

One message per severity through every configured transport; exit 2 means
nothing is configured at all. **It reports delivery, not receipt** — look at the
phone. A revoked bot token and a working one are indistinguishable from
anything except a send, and the send that counts is the one from the machine
that will be making it at 09:31 on a Tuesday.

### 7.12 Preflight, then clear the halt as a decision

```bash
uv run python scripts/preflight.py
```

Every check `docs/FIRST_PAPER_RUN.md` asks for, against the saved
configuration, in about two seconds — including the two that decide whether the
week can produce an answer at all: enough warmup history, and a size the
position cap will not refuse.

```bash
uv run python scripts/halt.py clear --by "<your name>"
```

**Do not clear the halt because the stack came up.** Clear it because
`status.py` and the broker agree, `preflight.py` passed, and an alert reached
your phone.

*Authoritative: the A1 host. It is trading.*

### 7.13 Take the Mac out of service — the step that is easy to forget

```bash
# On the Mac.
make down

# And the schedules, which will otherwise go on dumping a database that is no
# longer the platform, onto a drive you have stopped watching.
launchctl bootout gui/$(id -u)/local.atp.backup
launchctl bootout gui/$(id -u)/local.atp.backup-verify

# And the thing that let it hold a session at all.
sudo pmset -c sleep 15
```

**A stopped stack on a machine that starts Docker Desktop at login is one
`make deploy` away from being a second worker.** That is the 406, and the
duplicate-position incident, arriving weeks later by accident. Leave the
checkout, leave the volumes (§8 wants them), and remove the automation.

---

## 8. Rollback

**The point of no return is the first fill on the A1 host**, not the deploy.

Until then the Mac's database and the A1 host's database are the same database:
one was restored from the other and nothing has diverged, because §7.9 halted
the new host before it could trade. Going back is a matter of starting the old
stack again.

| If you are here | Going back costs |
|---|---|
| Before §7.3 | Nothing. Clear the halt on the Mac and carry on |
| §7.3 – §7.9 | Start the Mac's worker again (`docker compose start worker queue`), clear the halt. The A1 host has a database and no history of its own |
| After §7.12, before the first fill | The same, plus taking the A1 host down and re-checking that its worker is stopped, not merely halted |
| **After the first fill** | A restore in the other direction: halt the A1 host, `make backup via=compose`, move the dump back, restore onto the Mac, reconcile against the broker. Every step of §7 in reverse, with the same rules |

Two things that make the difference between a rollback and an incident:

- **Keep the Mac's stack intact for at least a week.** Down, not deleted; its
  volumes are the fastest recovery path there is. Delete them when the A1 host
  has run a clean session and you have a verified backup taken *on* it.
- **If the keys were rotated (§6.4), the Mac cannot trade any more** — it holds
  a revoked pair. A rollback then means installing the current bundle on the Mac
  too. That is the cost of the rotation, and it is worth paying with the
  knowledge in advance rather than at 09:25.

---

## 9. What changes permanently once you are there

### 9.1 Reboots start working, so test them

```bash
sudo reboot
# once it is back, over the tailnet:
docker compose ps && uv run python scripts/status.py
swapon --show && timedatectl
```

Do this deliberately, on a day that does not matter. Every service in the
deployed configuration carries `restart: unless-stopped` and dockerd is a
systemd unit, so the stack should come back on its own — which it could not do
on the Mac without a login-item setting. The failure this catches is quiet: the
stack came back *in pieces* and looked alive.

While you are here, take the patching decision rather than inheriting it:

```bash
cat /etc/apt/apt.conf.d/50unattended-upgrades      # is anything auto-rebooting?
```

An unattended reboot at 06:00 UTC is inside the US pre-market. `docs/SAFETY.md`
rule 4 is about deploys and the reasoning transfers exactly: a host that
restarts itself during a session has made a deploy-shaped decision on your
behalf. Either disable the automatic reboot and patch by hand inside a halt
window, or move it to a Saturday.

### 9.2 Backups move from launchd to cron, and the destination question stays open

LOCAL_HOSTING.md's LaunchAgents do not apply here. BACKUPS.md's cron lines do,
and they are the whole of it:

```cron
# /etc/cron.d/atp-backup  —  paths absolute, cron's PATH is not yours.
30 2 * * *  root  cd /opt/atp && /root/.local/bin/uv run python scripts/backup_db.py create --exec compose --prune --keep 14 --dir /mnt/backups >> /var/log/atp-backup.log 2>&1
15 3 * * 0  root  cd /opt/atp && /root/.local/bin/uv run python scripts/backup_db.py verify --exec compose --dir /mnt/backups >> /var/log/atp-backup.log 2>&1
```

Three things carry over from the Mac and one does not:

- **`--dir` explicitly, never `ATP_BACKUP_DIR`.** That variable is read from the
  process environment and never from `.env`, so under cron it is absent and the
  dump lands in the checkout — beside the database it is insuring.
- **`--keep` means two different things.** On `create` it is a retention count;
  on `verify` it is a flag meaning *leave the scratch database behind*. Passing
  it to `verify` quietly accumulates `atp_restore_check_*` databases.
- **Nothing shouts when it fails.** Cron writes the exit code to a log and takes
  no further interest, exactly as launchd did. Wiring the exit code to
  `scripts/check_alerts.py` is the gap to close before the next paper week, not
  after it.
- **Missed runs are simply missed.** This is the one that *improves*: launchd
  re-ran a calendar job the sleeping Mac had missed, which mattered because the
  Mac slept. This host does not sleep, so cron's "skip it" semantics stop being
  a liability.

**`/mnt/backups` is not off-site.** It survives the instance and not the
tenancy, and a tenancy is precisely the thing §1 says has already reclaimed
things once without an announcement. Sweep the directory somewhere else —
`rclone` to object storage, `restic` to anywhere — and encrypt on the way with
the `age` key that is already on this host. A dump carries no credentials and
carries the entire trading record.

### 9.3 Idle reclamation replaces sleep as the way this host disappears

The Mac's failure mode was sleep. This host's is being reclaimed for being
quiet, and it is a real risk for this workload rather than a theoretical one:
**this stack idles by design.**

The rule is a 7-day window with three simultaneous conditions — 95th-percentile
CPU below 20%, network below 20%, and memory below 20% (memory applies to A1
shapes). All three must hold, which is what saves you: `timescaledb-tune` sizes
`shared_buffers` at roughly 25% of the host's RAM, so Postgres alone claims
around 3 GB of 12 and clears the memory threshold without help.

**"Should be enough" is not a thing to assume about the machine your trading
platform lives on.** Check it rather than believing this paragraph:

- Console → *Compute* → *Instances* → the instance → *Metrics*. Read CPU,
  memory and network utilisation over 7 days. Memory is the one to look at.
- On the host, `free -h` and `docker stats --no-stream` say what is actually
  resident.
- If you are on Always Free and the numbers are marginal, **the fix is §2.2**,
  not a busy-loop. Scripts that burn CPU to look alive are a widespread answer
  to this and they are the wrong one here: they compete with the process whose
  latency you moved to Ashburn to improve, and they make the metric you use to
  spot a runaway backtest meaningless.

### 9.4 The allowance is a thing that moves

It halved on 15 June 2026 with no announcement, and instances above the new
limit were stopped from 18 August. Assume it will move again.

- Watch the tenancy's notification email address. That is where "your instance
  was stopped" arrives, and it is a real address rather than a formality.
- Keep the host **reproducible**: the checkout, the committed SOPS bundle, an
  off-box dump. Between them, moving to another vendor is a provisioning job
  and a restore — §3 through §7.8 with a different §3 — rather than a rescue.
  That is most of what `make deploy` and the bundle already buy you.
- Do not build anything on top of the current shape that a resize would break.

### 9.5 Access, and what is now exposed

Nothing, if §3.3 and §4.4 were done. Worth re-stating because the instance has
a public IPv4 and the Mac did not:

- The dashboard is on `ATP_WEB_BIND_ADDR`, a `100.x` tailnet address.
  `make check-bindings` refuses `0.0.0.0` and refuses any publicly routable
  address, and it runs before every deploy.
- Postgres and Redis are on loopback. Redis holds the kill switch with no
  password in front of it, so whoever reaches that port can clear a halt.
- The security list has no ingress rule at all once SSH moves to the tailnet.
- `tailscale serve` gives the dashboard a real HTTPS certificate and is the
  right way to reach it from a phone — with the known limitation DEPLOYMENT.md
  records: TLS terminates at Tailscale, nginx sets `X-Forwarded-Proto` from its
  own `$scheme`, and the session cookie is therefore not marked `Secure` even
  though the browser's connection is encrypted.

---

## 10. Verification checklist

Everything below has appeared above. Collected here because a migration is
checked once, at the end, by someone who has been at it for two hours.

**The host**

```bash
uname -m                                    # aarch64
timedatectl                                 # synchronized: yes, UTC
swapon --show                               # 4G
docker compose version                      # >= v2.24
systemctl is-enabled docker                 # enabled
tailscale ip -4                             # matches ATP_WEB_BIND_ADDR in .env
```

**The stack**

```bash
cd /opt/atp
docker compose ps                           # every service up, none restarting
python3 scripts/check_port_bindings.py      # nothing exposed, deployed shape intact
make secrets-check env=paper                # bundle decrypts, breaks no rule
curl -sf http://127.0.0.1:8000/healthz
curl -sf http://$(tailscale ip -4):8080/healthz
date -u; docker compose exec -T api date -u
docker compose exec -T db psql -U atp -d atp -c "\dx"
```

**The platform**

```bash
uv run python scripts/status.py             # halts, quotes, bars, and the venue agree
uv run python scripts/preflight.py          # ready for a week of paper
uv run python scripts/check_alerts.py --by "you"   # then look at the phone
uv run python scripts/backup_db.py verify --exec compose --dir /mnt/backups
```

**The things no command can check**

- The Mac's worker is **stopped**, its LaunchAgents are **unloaded**, and its
  stack will not come back at login.
- `ATP_RUN_MODE=paper` and `ATP_ALLOW_LIVE_TRADING=false` are in the host's
  `.env` and in no bundle.
- The age private key is backed up somewhere that is not this host and not the
  Mac.
- A reboot has been performed on purpose and the stack came back whole.
- Someone looked at a phone and saw the alert.

---

## 11. What this still does not give you

Everything in DEPLOYMENT.md's "What this does not give you" holds unchanged.
Five additions specific to this host:

- **This procedure has not been run.** No part of this document is a description
  of something that happened. The repository's standard for a claim is a build
  that ran and a suite that passed, and on arm64 neither has — CI is x86-64.
  §5 exists so that the first person to find out is not doing it during a
  cutover.
- **The ADR exists; what §5 turns up still has to reach it.**
  [ADR 0032](adr/0032-the-paper-host-moves-off-the-mac.md) decided this move and
  superseded ADR 0021, on the day-3 finding that the Mac was dark for 79.8% of
  regular trading hours and the day-2 stall that was the same failure smaller.
  It records the ARM build as an outstanding condition discharged by §5 rather
  than by argument — so whatever §5 finds is an amendment to that ADR, and the
  first person to run it owes one.
- **Live still needs a second host.** `docs/SAFETY.md` layer 3 wants paper and
  live on separate machines with separate key pairs. Splitting one A1
  entitlement into two 1 OCPU / 6 GB halves is technically available and is
  below the comfortable row on both sides; a second tenancy is the same vendor
  twice. Going live is a decision to make after a paper week that actually ran.
- **Free has no SLA, and this vendor has form.** The allowance halved with no
  announcement and instances over it were terminated. A machine you own cannot
  be reclaimed, which is the one thing the Mac was better at and the reason
  ADR 0021 went the way it did.
- **The roadmap item does not move because of this.** "Deployment target chosen;
  secrets manager" is ticked by *a host with the stack on it, `scripts/status.py`
  answering, and an alert that reached a phone* — not by choosing a host, and
  not by writing down how to move to one. When this procedure has actually been
  performed, that is the PR that ticks it.
