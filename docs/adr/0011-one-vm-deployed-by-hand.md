# 11. One always-on VM per run mode, deployed by hand, reached over a VPN

**Status:** Accepted · 2026-08-18

## Context

`docs/ROADMAP.md` Phase 6 has carried "Deployment target chosen; secrets
manager" as one unstarted item since the skeleton, and `SECURITY.md` lists "no
chosen deployment target" among the reasons this is not ready for a public
address.

It is not only a Phase 6 item. Phase 4's *Verifiable:* line is "a strategy
trades the paper account for a week and reconciles clean"; Phase 5's needs "a
worker trading paper"; Phase 1's streaming line needs a socket held through a
forced disconnect *during a session*. Eight Phase 4 items are built and unticked
against that paper week. None of it is demonstrable on a laptop that sleeps, so
the missing host is what is holding three phases' worth of ticks — the decision
is load-bearing now rather than at go-live.

The choice is unusually constrained, and the constraints are in the code rather
than in anyone's preference:

1. **The worker is a singleton, enforced by the venue.** Alpaca refuses a second
   stream connection per key with code 406, which `AlpacaRealtimeFeed` treats as
   permanent rather than retrying — deliberately, so that a second process fails
   loudly instead of racing the first. `docs/RUNBOOK.md` lists "two workers
   running" as a cause of duplicate positions.
2. **Nothing here can scale to zero.** The worker holds a WebSocket through the
   session, reconciles every five minutes, snapshots every minute, and sweeps
   for gaps at 02:00.
3. **TimescaleDB is a hard dependency** (ADR 0004), which most managed Postgres
   does not offer.
4. **High availability is not wanted.** `docs/SAFETY.md` layer 5 is broker-side
   stops, and the runbook says positions are safe while the worker is down;
   `warmup()` reconciles and adopts open positions on restart. The correct
   posture is fail-stopped and restart cleanly.
5. **There is no public surface.** One operator, no TLS of our own, nginx
   already same-origin, and `docs/DASHBOARD.md` already recommends Tailscale.
6. **Deploys are deliberate.** `docs/SAFETY.md` rule 4 forbids deploying on a
   Friday afternoon or in the last thirty minutes of a session.

Constraints 1 and 4 point the same way and are the ones that decide it. Every
mainstream orchestrator's *default* rollout overlaps old and new instances —
`maxSurge` on a Kubernetes Deployment, an ECS rolling update, a multi-machine
Fly app. That default is precisely the duplicate-position incident in the
runbook, and the platform would spend its life configured to defeat the feature
its host is built around.

## Decision

**One always-on x86 VM per run mode, running the existing compose stack, reached
over Tailscale, deployed by an explicit operator action.**

The vendor is deliberately not the architectural decision and is not fixed here
— any commodity VPS (DigitalOcean, Vultr, Linode, Hetzner, Lightsail) serves.
The properties that matter are: **a US-East region**, because Alpaca's API is
there and the order path is what latency is spent on, not the dashboard, which
refreshes every five minutes; and **root on a machine that stays up**, because
constraints 2 and 3 need a long-running process and an extension we install
ourselves.

**Paper and live get separate VMs.** `docs/SAFETY.md` layer 3 is "paper and live
use different Alpaca keys", and its stated failure mode is "live keys deployed
to the paper env". Two hosts with two key pairs and two databases make that
failure structural rather than conventional, for the price of a second small
VM.

**The deployed stack is an overlay**, `docker-compose.prod.yml`, not a second
compose file. `docker-compose.yml` is a development stack and stays one; the
overlay corrects the three things that are right for a laptop and wrong for a
host — no restart policy on `db`, `redis` or `api`; `./libs` and `./apps/*`
bind-mounted over the code baked into the images, with `--reload` on the API;
and the Vite dev server in the default service set. An overlay rather than a
copy because a copy drifts: a healthcheck fixed in one file and not the other is
a bug nobody sees until the deploy.

**`docker compose up -d` is the deploy mechanism**, and its recreate semantics
are the reason it fits: it stops a changed container before starting its
replacement and never runs both. What is a limitation on other platforms is the
requirement here.

**Access is Tailscale**, with `ATP_WEB_BIND_ADDR` set to the host's Tailscale
address — the route `docs/DASHBOARD.md` already recommends and
`scripts/check_port_bindings.py` already accommodates by testing `is_global`
rather than `is_private`. No public load balancer, no public DNS, no
certificate of our own.

**Secrets are SOPS + age**, decrypted at deploy time to a root-owned `0600`
`.env`. One operator and one host do not need a secrets manager's server, and a
hosted control plane would be a network dependency in the startup path of a
process whose job is to be running when the market opens. Two rules travel with
it: `API_SECRET_KEY` is a stored secret rather than one generated at boot, since
`docs/SAFETY.md` wants sessions to survive a restart; and the live locks
(`ATP_ALLOW_LIVE_TRADING`, `WORKER_ALLOW_LIVE_ORDERS`) stay in host
configuration and out of the secret bundle, so that no rotation, sync or
restored backup can turn on live trading as a side effect.

**This ADR chooses the target. It does not deploy it.** Nothing has been
provisioned, and the SOPS tooling is not written. The roadmap item stays open
and says so.

## Consequences

**One host is a single point of failure, and that is the accepted trade.** A
dead VM means the platform is down; it does not mean positions are unmanaged,
because the venue holds the stops. Recovery is provisioning a VM and restoring a
backup, which makes "backups and a tested restore" — the roadmap item directly
above this one — the thing that now bounds recovery time. It is still unbuilt.

**Migrations, backfills and `scripts/halt.py` run from the host**, against the
loopback-published Postgres and Redis. That is why those two ports stay
published in the deployed configuration, and why the host's own `.env` needs a
`DATABASE_URL` carrying the real password.

**The database password is now required.** The base compose file hardcodes
`atp`/`atp`; the overlay takes `ATP_DB_PASSWORD` and fails the command if it is
unset, because there is no safe default. Postgres reads it at initdb and never
again, so setting it after the first start needs `ALTER USER` as well.

**`tailscale serve` gives HTTPS but not a `Secure` cookie.** TLS terminates at
Tailscale, which forwards plain HTTP to nginx; nginx sets `X-Forwarded-Proto` to
its own `$scheme`, which is `http`, and `_is_https()` in
`apps/api/src/atp_api/routers/auth.py` reads exactly that header. So the session
cookie is not marked `Secure` even when the browser's connection is encrypted.
Nothing here fixes it — honouring an inbound `X-Forwarded-Proto` means deciding
which proxy to trust, which is a security decision that deserves its own change.
`docs/DEPLOYMENT.md` records it and `SECURITY.md` lists it.

**Building happens on the host.** `make deploy` passes `--build`, so a deploy
costs the VM a few minutes of CPU and needs enough disk for the image layers. A
registry removes that at the cost of somewhere to push to; with one host it is
not yet worth it.

**Scaling past one operator means revisiting this.** A second strategy process,
a second person, or an audit-retention requirement all break assumptions here.
The migration path is Timescale Cloud for the database first — it absorbs the
backup item — and a managed host after, not a rewrite.

## Alternatives

**Kubernetes, ECS/Fargate, or Fly with multiple machines.** Rejected on
constraint 1: their default rollout runs two workers, which is the runbook's
duplicate-position incident, and each would be configured to `Recreate` /
`minimumHealthyPercent: 0` forever after — a guarantee held by a settings field
somebody can flip, rather than by there being one machine.

**Serverless (Lambda, Cloud Run, Vercel).** Rejected on constraint 2 in the
first sentence: a held WebSocket and a 02:00 sweep are not request-driven work.

**Managed Postgres (RDS, Cloud SQL, Neon).** Rejected on constraint 3. ADR
0004's parenthetical says "AWS RDS with the extension enabled" works; that
should be verified before anyone plans against it, as RDS publishes a fixed
extension list, and this decision deliberately does not depend on the answer.
Timescale Cloud is the real managed option and is the documented upgrade path
rather than the starting point — it costs more than the whole VM and its value
is backups, which is a separate unticked item.

**A second compose file instead of an overlay.** Rejected: two files describing
one stack drift, and the half that drifts silently is the deployed one.

**Push-to-deploy from CI.** Rejected on constraint 6. An automatic deploy is a
deploy at whatever time the merge lands, which is the rule `docs/SAFETY.md`
states plainly. Deploys here are a command someone runs pre-market having
decided to.

**A hosted secrets manager (Vault, AWS Secrets Manager, Doppler).** Rejected for
now as more moving parts than the thing they protect, and a network dependency
in the path of a process that must be up at 09:30. Worth revisiting when there
is a second host or a second person — the rotation story is genuinely better.

**Running the dashboard on the LAN instead of a VPN.** Available, documented in
`docs/DASHBOARD.md`, and not the default: "the LAN" usually includes guest wifi,
DHCP moves the address, and it is plain HTTP carrying the whole book.
