# Deployment

How to put this platform on a host, and what it is and is not ready for.

The target and the reasoning behind it are **ADR 0011**. This is the procedure.
Read `docs/SAFETY.md` first if the host you are building will ever hold live
keys — everything here assumes its rules, particularly rule 4 about *when* you
are allowed to deploy.

---

## What you are deploying onto

> **No host has been selected yet.** ADR 0011 chose the *shape* — one
> always-on VM per run mode, the compose stack, reached over a private network,
> deployed by hand — and deliberately did not pick a machine or a vendor. The
> table below is the specification to buy against, not a description of
> something that exists. Nothing in this repository has been deployed anywhere.
>
> **Tailscale is not the host.** It is the access layer: a VPN that puts the
> dashboard on a private network instead of a public address. Whatever host is
> eventually chosen, it is a Linux box running Docker, and Tailscale — or
> WireGuard, or an SSH tunnel (`docs/DASHBOARD.md` has the three) — is how you
> reach the dashboard on it.

One always-on x86 VM per run mode, in a **US-East region** — Alpaca's API is
there, and that is where latency is spent. The dashboard refreshes every five
minutes, so your own distance from the host does not matter.

| | |
|---|---|
| OS | Any current Linux with Docker Engine and **Compose v2.24+** (`!reset`) |
| Arch | x86-64 |
| Region | US-East |
| Network | A VPN interface on the host. Nothing published to the internet, ever |
| Count | **Two hosts if you go live** — one paper, one live, separate key pairs |

That last row is `docs/SAFETY.md` layer 3. Its stated failure mode is "live keys
deployed to the paper env", and one host running both is how that happens.

### Size

| | Paper host, to start | Comfortable | Broad minute-bar backtesting |
|---|---|---|---|
| vCPU | 2 | **4** | 4–8 |
| RAM | 4 GB | **8 GB** | 16–32 GB |
| Disk | 40 GB | **80–160 GB** NVMe/SSD | 250 GB+ |

**8 GB / 4 vCPU / 80 GB is the sweet spot** for one operator on a modest
watchlist. Prefer NVMe to network block storage: Postgres fsync latency is the
part you feel.

Buy for the backtests, not for the running stack — the stack idles, and the
numbers below say why.

### What actually consumes each resource

Measured against this codebase rather than estimated. All of it is measurement
of the *code and the images*, projected onto a host nobody has bought yet.

**RAM is a backtesting question.** `BacktestEngine.run(bars: dict[str, list[Bar]])`
takes every bar for every symbol fully materialised, and `scripts/run_backtest.py`
loads them eagerly per symbol before the engine starts. A `Bar` — frozen slots
dataclass, seven `Decimal` fields — measures **855 bytes resident**, over 200,000
of them:

| Backtest | Bars | RAM for the bar objects alone |
|---|---|---|
| 1 symbol · 5y · daily | 1,260 | negligible |
| 1 symbol · 1y · 1-minute | 98k | 0.08 GB |
| 5 symbols · 2y · 1-minute | 983k | 0.84 GB |
| 10 symbols · 5y · 1-minute | 4.9M | **4.2 GB** |
| 50 symbols · 5y · 1-minute | 24.6M | 21 GB |

That is before pandas, the equity curve or the engine's own state. Daily-bar
backtests are free; minute-bar backtests over a wide universe are the only
reason to buy more than 8 GB.

**Disk is less than ADR 0004 makes it sound.** Loading the real hypertable —
same columns, same `chunk_time_interval`, same `compress_segmentby` — with
520,231 rows of a random walk gives **162 bytes/row uncompressed** and **84.8
compressed**, a ratio of 1.9×. Treat that ratio as a *floor*: random data is
maximally incompressible, and real series with regular timestamps and small
price increments do considerably better.

So ADR 0004's "50M rows for 500 symbols" is ≈ 8 GB raw, ≈ 4 GB compressed. A
ten-symbol five-year minute history is under 1 GB. The disk is sized for Docker,
not for bars: the TimescaleDB image alone is 756 MB, and `make deploy` builds on
the host, so image layers, the uv cache and the Vite build all land there.
**Budget 15–20 GB for Docker before a single bar is stored.**

**CPU is single-core-bound where it matters.** The backtest loop is one Python
thread, the worker is one asyncio process, and bcrypt at cost 12 is about a
quarter-second of one core per login. Nothing here scales across cores except
Postgres itself, so clock speed beats core count.

### What the host has to be able to do

Beyond Docker and the bind addresses, four things this platform genuinely
depends on:

- **An accurate, NTP-synced clock** (chrony or systemd-timesyncd). Not optional
  here: bars are stamped at their open, `StalenessMonitor` measures silence in
  wall-clock seconds against the exchange calendar, and every timestamp is
  tz-aware UTC (CLAUDE.md §1.2). A drifting clock produces halts that look like
  feed outages and bar attribution that looks like vendor error.
- **Outbound HTTPS and WSS** to `api.alpaca.markets`, `data.alpaca.markets` and
  `stream.data.alpaca.markets`, plus whichever alert transport is configured
  (`ntfy.sh`, `api.telegram.org`). Alerting needs nothing else — no inbound
  port, no DNS of its own.
- **Persistent block storage with snapshots** for the `db_data` and
  `redis_data` volumes. Redis holds kill-switch state with `appendonly yes`, so
  that volume is a safety asset rather than a cache. Until "backups and a tested
  restore" is built, provider snapshots are the entire recovery story.
- **2–4 GB of swap**, as a cushion against a backtest that misjudged its
  watchlist. A slow run beats an OOM kill during a session.

Not needed: more than 4 cores for the platform itself, a GPU, a load balancer,
public DNS, a certificate of our own, IPv6, or any form of HA — broker-side
stops hold positions while the box is down, which is why ADR 0011 chose
fail-stopped over multi-node.

## Provisioning

```bash
# On the host, as root.
curl -fsSL https://get.docker.com | sh          # Docker Engine + Compose v2
docker compose version                          # must be >= v2.24

curl -fsSL https://tailscale.com/install.sh | sh
tailscale up
tailscale ip -4                                 # 100.x.y.z — note this down

git clone <your remote> /opt/atp
```

Then confirm the disk you will actually fill. Bars are the one table that grows
without bound (ADR 0004): budget for the watchlist you intend, not the one you
start with.

Compression needs no action — the initial migration sets
`timescaledb.compress` on `bars` and adds a policy that compresses chunks older
than 30 days. The last 30 days stay uncompressed by design, which is why the
raw figure above is the one to size against.

### The database tunes itself from the host's RAM, not from yours

The `timescale/timescaledb` image runs `timescaledb-tune` on first init
(`001_timescaledb_tune.sh`). On a 15 GB machine it wrote:

```
shared_buffers = 4018MB          # ~25% of HOST RAM
effective_cache_size = 12056MB   # ~75% of HOST RAM
work_mem = 10288kB               # per sort node, up to max_connections
```

Two consequences, and both bite quietly.

**Postgres assumes it has the machine.** On an 8 GB host it claims ~2 GB of
shared buffers and plans as though 6 GB of page cache were available to it —
while the worker, the API, nginx and any backtest are also on that box. That is
survivable at 8 GB and is why the "comfortable" row is not 4 GB; at 4 GB, pin
`shared_buffers` and `work_mem` explicitly rather than letting the tuner guess.

**It reads the host's RAM, not the container's limit.** Adding a memory limit to
the `db` service without also pinning those settings gives you a Postgres sized
for a machine it cannot have, and the OOM killer resolves the disagreement. If
you constrain the container, configure the database to match in the same change.

## Configuring

`.env` on the host is the deployment's configuration, and writing it is a
deliberate act. `make up` writes one from `.env.example` for a developer;
`make deploy` deliberately does **not**, because a file that says
`ATP_RUN_MODE=backtest` with no credentials is a wrong answer given quietly.
Start from the example by all means — just knowing that you are the one
deciding what is in it.

```bash
cd /opt/atp
cp .env.example .env
chmod 600 .env
```

Fill in, at minimum:

| Variable | Why |
|---|---|
| `ATP_DB_PASSWORD` | **New.** The compose overlay refuses to start without it; the base file's password is `atp` |
| `DATABASE_URL` | Must carry the same password — `make migrate` and the scripts run from the host |
| `ALPACA_API_KEY` / `_SECRET` | The pair for *this host's* run mode, never the other's |
| `API_SECRET_KEY` | `openssl rand -hex 32`. Stored, not generated at boot, or every restart signs everyone out |
| `API_USER` / `API_PASSWORD_HASH` | `uv run --package atp-api python scripts/hash_password.py` — that form does not care how you synced. With no hash configured every login is refused |
| `ATP_WEB_BIND_ADDR` | The `tailscale ip -4` address. Left empty the dashboard is reachable from the host only |
| `ATP_LOG_FORMAT=json` | The default is `console`, which is for a terminal |
| `ATP_ENV=production` | |
| `WORKER_SYMBOLS` | The watchlist. Empty means the worker idles |
| `ALERT_NTFY_TOPIC` | Where halts reach a phone. Empty means log-only — see below |
| `ALERT_TELEGRAM_TOKEN` / `_CHAT_ID` | The other transport. Set both, or neither |

`ATP_RUN_MODE` and the two live locks are the ones to be deliberate about.
`paper` is where a host starts and where it stays for at least the four weeks
`docs/SAFETY.md` asks for.

### Secrets

ADR 0011 chose **SOPS + age**: the encrypted bundle lives in the repository and
is decrypted at deploy time to the `0600` `.env` above. `scripts/manage_secrets.py` is
that tooling — a thin wrapper, since `sops` does the cryptography and `age`
holds the key.

Both are separate installs, neither is a daemon:

```bash
# sops — one binary
curl -fsSLo /usr/local/bin/sops \
  https://github.com/getsops/sops/releases/download/v3.9.4/sops-v3.9.4.linux.amd64
chmod +x /usr/local/bin/sops
# age
apt-get install -y age      # or https://github.com/FiloSottile/age
```

**Once, on the machine that edits secrets:**

```bash
uv run python scripts/manage_secrets.py init
```

That writes an age key to `~/.config/sops/age/keys.txt` (`0600`) and a
`.sops.yaml` naming its public half as the recipient. **Commit `.sops.yaml`** —
a recipient is a public key, and every machine that edits a bundle needs the
same list. **Back up the private key offline.** It is the one thing here that
cannot be regenerated: lose it and every bundle encrypted to it is unreadable,
and the only way back is re-creating each one from the credentials at source.

**Then, per host — one bundle per run mode, because paper and live are separate
machines with separate keys:**

```bash
uv run python scripts/manage_secrets.py import --env paper --from .env   # encrypt what you have
uv run python scripts/manage_secrets.py check  --env paper               # decrypts; prints key names only
shred -u .env                                                     # the plaintext has served its purpose
```

`infra/env/paper.sops.env` is now committable, and should be committed. SOPS
encrypts dotenv *values* and leaves the *keys* readable, so the diff of a
rotation reads `ALPACA_API_SECRET changed` rather than showing one opaque blob —
which is what a reviewer needs to see, and the value is what they must not.

To change a secret later, `edit` opens the bundle in `$EDITOR` and re-encrypts
on save:

```bash
uv run python scripts/manage_secrets.py edit --env paper
```

**On the host, at deploy time:**

```bash
make secrets-install env=paper     # writes .env, mode 0600, atomically
```

This is a step in the deploy sequence below rather than something `make deploy`
does for you. Decrypting needs a private key and writes plaintext to disk, and
that should not be a side effect of a command a developer also runs on a laptop.

#### What a bundle may not contain

`scripts/manage_secrets.py` refuses these — on import *and* again on install, because a
bundle can acquire one later through `sops` directly or a hand edit:

| Key | Why |
|---|---|
| `ATP_RUN_MODE` | `docs/SAFETY.md` layer 1 |
| `ATP_ALLOW_LIVE_TRADING` | layer 2 |
| `WORKER_ALLOW_LIVE_ORDERS` | the worker's own lock |

These are host configuration, not secrets. A bundle is a thing that gets copied
between hosts, restored from a backup and re-synced by tooling, and none of
those events may switch on live trading as a side effect — "two independent
locks stay two" applies to the deployment tooling as much as to the code. Set
them in the host's `.env` **after** `secrets-install`, or in the environment.

> ADR 0011 named the latter two. `ATP_RUN_MODE` is refused here for the same
> reason, which extends that decision rather than restating it — a reviewer
> should either accept it or strike it.

A key present with an **empty value** is refused too. SOPS leaves an empty value
unencrypted, so it neither carries a secret nor protects one, and the process
reading it falls back to its default exactly as though the line were absent.

#### Two things that are not the bundle's job

- **`ATP_DB_PASSWORD` is read by Postgres at initdb and never again.** Set it
  before the first start. Changing it later means `ALTER USER atp PASSWORD ...`
  *and* updating both the bundle and `DATABASE_URL`, or the stack comes up
  unable to authenticate against its own database.
- **On exposure, revoke at the broker first.** `SECURITY.md` has the order and
  it does not change here: a revoked key in a git history is harmless, and a
  live key removed from a bundle is not. Re-encrypting is the last step, not the
  first.

### Alerting

`docs/SAFETY.md`'s checklist asks that alerts reach a human on a phone rather
than a log file. Every automated halt goes out this way — a lost feed, a
reconciliation mismatch, a worker task dying — plus manual halts and resumes.
Design and what is deliberately *not* alerted: ADR 0012.

Two transports. Pick either, or set both — configuring both sends to both, on
the grounds that one push service having a bad day should not be the same as
having no alerting.

**ntfy** — nothing to sign up for, an app on both platforms, self-hostable:

```bash
python -c "import uuid; print('atp-' + uuid.uuid4().hex)"   # a topic nobody will guess
```

Put it in the bundle rather than in a commit, install the ntfy app on the phone,
and subscribe it to that topic.

**Telegram** — nothing to install if you already have it:

1. Message `@BotFather`, send `/newbot`, keep the token it gives you.
2. **Message your new bot once.** A bot cannot open a conversation, so without
   this it has nowhere to deliver and every alert fails.
3. `curl https://api.telegram.org/bot<TOKEN>/getUpdates` and read
   `result[].message.chat.id`.

Either way, the credentials go in the bundle:

```bash
uv run python scripts/manage_secrets.py edit --env paper
#   ALERT_NTFY_TOPIC=...           and/or
#   ALERT_TELEGRAM_TOKEN=...  ALERT_TELEGRAM_CHAT_ID=...
make secrets-install env=paper
```

**Both are addressed by a credential.** An ntfy topic on the public server is
the only thing between your halt notifications and anyone who guesses it, in
*both* directions — reading them and forging one that says trading resumed. A
Telegram bot token is worse: it *is* the bot, it travels in the URL path, and
whoever holds it can read the chat and post as you. Hence the bundle, and hence
nothing logs either, on any path — neither the sinks' own failure logging nor
the HTTP library underneath them, which used to log the URL, and therefore the
credential, on every successful send (SECURITY.md). If the alerts matter,
self-host ntfy and set `ALERT_NTFY_TOKEN`; then it is not a guess away.

Alerts deliberately carry no numbers from the book — a reason and a scope, never
a balance or a position. A notification renders on a lock screen and travels
through a third party; what the numbers are is a question for the dashboard,
which is behind authentication. Sending one to yourself to check the wiring:

```bash
uv run python scripts/check_alerts.py --by "you"
```

It builds the sink from this host's own configuration, sends one message per
severity through it, and exits non-zero if a transport did not take one —
`2` if nothing is configured at all, which is the case worth catching, because
an unalerted platform behaves exactly like an alerted one until the day it
matters. Run it after `make secrets-install`, and again whenever the bundle
changes: a revoked bot token and a working one are indistinguishable from
anything except a send.

**It reports delivery, not receipt.** A transport accepting a message is as far
as any code here can see. Whether a phone lit up is the half that matters, and
only the person holding it can confirm it — so look at the phone before
believing the exit code.

The older way was to engage a real halt and clear it:

```bash
uv run python scripts/halt.py engage --by "you" --detail "testing the alert path"
uv run python scripts/halt.py clear  --by "you"
```

That still works and exercises one more layer — the kill switch calling the
sink. It is also a real halt: it needs Redis, it stops trading, and it writes an
incident into the audit trail that never happened. Prefer `check_alerts.py`
unless you are specifically testing the halt path, and if you do use this, do it
outside market hours or on a host that is not trading.

Neither is something the test suite can do for you. `make test` never touches a
live endpoint (CLAUDE.md §1.7), so every alert test in it proves the request the
platform *would* have made against a transport that agrees with us about the
answer — which is worth having, and is not evidence that a credential still
works.

With no topic configured the platform starts normally and logs what it would
have sent (`alert.logged`). A notification must not be a dependency of trading.

## First deploy

```bash
cd /opt/atp
make secrets-install env=paper   # .env, from the committed bundle
make deploy          # check-bindings, then build and start
make migrate         # schema — from the host, against the loopback port
make backfill sym=SPY from=2021-01-01
```

`make deploy` runs `make check-bindings` first and will not start anything if it
fails. That check now covers both the development and the deployed
configurations, and asserts the deployed one is actually the deployed *shape* —
see "After every deploy" for what that means and why it is not redundant.

## Every deploy after that

Deploying is a decision, not a merge (`docs/SAFETY.md` rule 4: never on a Friday
afternoon, never in the last thirty minutes of a session). Pre-market, with time
to watch it:

```bash
# 1. Stop new risk. Positions keep their broker-side stops throughout.
uv run python scripts/halt.py engage --by "<your name>" --detail "deploy"

# 2. Take the new code, and the secrets that go with it.
git pull
make secrets-install env=paper

# 3. Rebuild and restart. Compose stops each changed container before starting
#    its replacement — it never runs two workers, which is the point.
make deploy

# 4. Migrations, if there are any.
make migrate

# 5. Confirm what is running (below), then clear the halt.
uv run python scripts/halt.py clear --by "<your name>"
```

The halt in step 1 is not ceremony. The worker adopts open positions through
`warmup()` on restart, and you want that reconciliation to happen without a
strategy also deciding things.

**Never** deploy by editing files on the host. The deployed stack runs code from
the images; a change in the checkout that has not been rebuilt is invisible, and
a change that *has* been rebuilt without being committed is unreproducible.

## After every deploy

```bash
make secrets-check env=paper               # the bundle decrypts and breaks no rule
uv run python scripts/status.py            # halts, quote freshness, latest bars, the venue
docker compose ps                          # every service up, none restarting
curl -sf http://127.0.0.1:8000/healthz     # API on loopback
curl -sf http://$(tailscale ip -4):8080/healthz   # and through nginx on the VPN
python3 scripts/check_port_bindings.py     # nothing exposed, deployed shape intact
```

That last one earns its place. The overlay strips the development bind mounts
and `--reload` using compose's `!reset` tag, and a compose that does not know
the tag leaves them in place **silently** — the stack then runs whatever source
is in the checkout instead of the image you built. Reading the file tells you
what was intended; only the resolved configuration tells you what compose did.
The check asserts the resolved configuration.

## Reboots

Every service in the deployed configuration carries `restart: unless-stopped`,
so the stack comes back on its own. Verify it once, deliberately, on a day that
does not matter:

```bash
sudo reboot
# then, once it is back:
docker compose ps && uv run python scripts/status.py
```

This is worth doing because the failure it catches is quiet. Before this overlay
existed, `worker` had a restart policy and `db`, `redis` and `api` did not — a
reboot brought back the worker alone, whose kill switch fails closed against an
unreachable Redis. The stack came up halted and looked alive.

## What this does not give you

**Backups exist now and are no longer on this list**, with the boundary stated
carefully. `scripts/backup_db.py` takes them, and — the half that matters —
restores one into a scratch database and compares it, so "an untested backup is
a belief" is now a command rather than an aspiration
([BACKUPS.md](BACKUPS.md), ADR 0014). What a host does not supply, and what this
repository cannot:

- **Nothing schedules it.** The cron lines are in BACKUPS.md and are two lines.
  They are documentation until there is a machine to put them on.
- **A dump lands on the host's own disk** unless `ATP_BACKUP_DIR` says
  otherwise, and a dump beside the database it came from dies with it. Until
  that variable points at something that outlives the VM, what you have is a
  fast undo for operator error, not disaster recovery. **One VM is still one
  VM.**
- **Dumps are not encrypted at rest.** They carry no credentials and they carry
  the entire trading record. The `age` key you generated for the secrets bundle
  is already here if you are shipping them off-box.

One thing to know before you need it, because it is the opposite of what the
deployment does everywhere else: a rebuilt host comes back with an **empty**
Redis, and an empty Redis holds no halt. The kill switch fails closed against an
*unreachable* Redis, not an empty one — so a restored stack starts **willing to
trade**, against a book as of the dump and a broker as of now. `halt.py engage`
before `make deploy`, every time. BACKUPS.md sequences it.

**Alerting works and is no longer on this list**, but it is not something a host
gives you either: it is configuration plus one confirmation. Set a transport,
run `scripts/check_alerts.py --by you`, and look at the phone. Until somebody
has done that on *this* deployment, `docs/SAFETY.md`'s checklist line is not met
here no matter what the roadmap says — the credential is per-host, and a
platform with no working transport behaves exactly like one that has never
needed to alert.

**Metrics and tracing exist now** and are the third item's replacement in this
list, but read the boundary carefully. The platform *exports* Prometheus text on
`/metrics` from the API and the worker separately, and every log line carries a
correlation id ([OBSERVABILITY.md](OBSERVABILITY.md), ADR 0013). **Nothing is
scraping it**: there is no Prometheus, no Grafana and no alerting rule in this
repo, so on a host with nothing else installed the observability is still
`docker compose logs` and `scripts/status.py` — with the difference that `curl
-H "Authorization: Bearer $METRICS_TOKEN" .../metrics` is now a diagnostic on
its own, and a signed-in operator can open the same URL in a browser.

Set `METRICS_TOKEN` when you provision. Unset means nothing can scrape — the
API still answers a session, the worker answers nobody, and both say so at
startup.

## Known limitations of this arrangement

**`tailscale serve` gives you HTTPS without a `Secure` cookie.** Fronting the
dashboard with `tailscale serve` gets a real certificate, which is the
recommended way to reach it from a phone. But TLS terminates at Tailscale, which
forwards plain HTTP to nginx; nginx sets `X-Forwarded-Proto` from its own
`$scheme`, which is then `http`; and `_is_https()` in
`apps/api/src/atp_api/routers/auth.py` reads exactly that header to decide
whether to mark the session cookie `Secure`. So the cookie is not marked, even
though the browser's connection is encrypted. The traffic is still encrypted
over the tailnet — what is missing is the flag that stops a browser sending the
cookie over plain HTTP. Fixing it means deciding which proxy's headers to trust,
which is a security change and not a deployment one.

**The rate limiter trusts `X-Forwarded-For`** (ADR 0010, `SECURITY.md`). Safe
only because the stack always sits behind its own nginx. It is one more reason
nothing here is published beyond the tailnet.

**Sessions cannot be revoked** before `API_SESSION_HOURS` elapses. A stolen
cookie is good until it expires.

**Building happens on the host.** `make deploy` passes `--build`, which costs
the VM CPU and disk. If that becomes the constraint, build elsewhere and push to
a registry — nothing here depends on building locally.
