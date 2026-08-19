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
| Size | 4 vCPU / 8 GB / 160 GB SSD is comfortable; 2 vCPU / 4 GB / 80 GB works for one small watchlist |
| OS | Any current Linux with Docker Engine and **Compose v2.24+** (`!reset`) |
| Region | US-East |
| Network | Tailscale on the host. Nothing published to the internet, ever |
| Count | **Two hosts if you go live** — one paper, one live, separate key pairs |

That last row is `docs/SAFETY.md` layer 3. Its stated failure mode is "live keys
deployed to the paper env", and one host running both is how that happens.

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
start with, and turn on Timescale compression for old chunks before it is
urgent.

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
| `API_USER` / `API_PASSWORD_HASH` | `uv run python scripts/hash_password.py`. With no hash configured every login is refused |
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
nothing logs either — including on the failure paths. If the alerts matter,
self-host ntfy and set `ALERT_NTFY_TOKEN`; then it is not a guess away.

Alerts deliberately carry no numbers from the book — a reason and a scope, never
a balance or a position. A notification renders on a lock screen and travels
through a third party; what the numbers are is a question for the dashboard,
which is behind authentication. Sending one to yourself to check the wiring:

```bash
uv run python scripts/halt.py engage --by "you" --detail "testing the alert path"
uv run python scripts/halt.py clear  --by "you"
```

That is a real halt, so do it outside market hours or on a host that is not
trading. It is also the only end-to-end test of this that exists: whether a
notification arrives on a particular phone is not something the test suite can
answer.

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

Three Phase 6 items are unbuilt, and a host does not supply them:

- **No alerting.** Nothing reaches a phone. `docs/SAFETY.md`'s go-live checklist
  requires it, so a live host is not compliant with our own checklist until it
  exists.
- **No backups.** There is no backup and no tested restore, which means one VM
  is one VM. Take a `pg_dump` on a cron before you care about the data, and
  restore it somewhere once — an untested backup is a belief, not a backup.
- **No metrics or tracing.** `docker compose logs` and `scripts/status.py` are
  the observability.

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
