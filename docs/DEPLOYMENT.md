# Deployment

How to put this platform on a host, and what it is and is not ready for.

The target and the reasoning behind it are **ADR 0011**. This is the procedure.
Read `docs/SAFETY.md` first if the host you are building will ever hold live
keys — everything here assumes its rules, particularly rule 4 about *when* you
are allowed to deploy.

---

## What you are deploying onto

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

`ATP_RUN_MODE` and the two live locks are the ones to be deliberate about.
`paper` is where a host starts and where it stays for at least the four weeks
`docs/SAFETY.md` asks for.

### Secrets

ADR 0011 chose **SOPS + age**: the encrypted env file lives in the repo and is
decrypted at deploy time to the root-owned `.env` above. That tooling is **not
written yet** — the roadmap item is open. Until it is, `.env` on the host is the
secret store, `chmod 600`, and the rules in `SECURITY.md` apply unchanged: paper
and live pairs never shared, and on exposure you revoke at the broker *first*.

Two things stay out of any secret bundle, now and later:

- **`ATP_ALLOW_LIVE_TRADING` and `WORKER_ALLOW_LIVE_ORDERS`** belong to host
  configuration. No rotation, sync or restored backup should be able to turn on
  live trading as a side effect. "Two independent locks stay two" applies to the
  deployment tooling as much as to the code.
- **`ATP_DB_PASSWORD` is read by Postgres at initdb and never again.** Set it
  before the first start. Changing it later means `ALTER USER atp PASSWORD ...`
  *and* updating both this file and `DATABASE_URL`, or the stack comes up unable
  to authenticate against its own database.

## First deploy

```bash
cd /opt/atp
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

# 2. Take the new code.
git pull

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
