# 13. Metrics are scraped from each process, and tracing is a correlation id

**Status:** Accepted · 2026-08-19

## Context

`docs/ROADMAP.md` Phase 6 asks for "Metrics/tracing", and `docs/DEPLOYMENT.md`
states what exists instead: *"No metrics or tracing. `docker compose logs` and
`scripts/status.py` are the observability."*

That is not merely thin, it is the wrong shape for the failure this platform
actually has. `apps/worker/src/atp_worker/main.py` writes the problem down in
its own supervisor docstring:

> a worker that half-runs is more dangerous than one that is plainly down,
> because monitoring still sees a live process while positions go unmanaged

The dangerous states here are all of that kind. An ingestor whose feed went
quiet while the process stayed up. A strategy loop erroring on every tick, which
`runner.evaluate` notes "looks alive to a health check". A kill switch engaged
hours ago that nobody cleared. None of these is a crash, and none of them shows
up in a liveness probe — the process answers `/healthz` throughout.

Two shapes of deployment constrain the answer. ADR 0011 chose **one always-on VM
per run mode**, reached over a private network, with no orchestrator and no HA;
nothing here is multi-node and nothing ever will be. And the platform is **two
processes** — `api` and `worker` — of which the worker produces almost all of
the interesting numbers and the API is the one with an HTTP surface.

## Decision

### Metrics

**Prometheus text exposition, `prometheus_client`, and every metric declared in
one module.** `libs/core/src/atp_core/metrics/registry.py` holds the lot.
Callers get typed functions — `halt_engaged(scope, reason)` — never the
instruments themselves, so a mistyped label is a `mypy --strict` error at the
call site rather than a brand-new time series that reads zero forever. Same
argument as `channels.py` for Redis channel names, and ADR 0006's for one
definition with several callers.

**Every metric sits beside a log line that already existed, at a choke point
that was already the only path.** `KillSwitch.engage`. `RiskEngine.validate`.
`OrderRouter._route`. `StreamIngestor._handle_quote`. The alert sinks. This is
ADR 0012's argument reused: instrument the one place an event passes through and
a metric cannot drift from the log, because one line of code produced both.

**Each process exposes its own `/metrics`, and the worker does not push.** This
is the decision worth arguing with, so it gets the whole of the next section.

**No `atp_halts_active` counter in core.** Halt state lives in Redis and several
processes write it. A gauge maintained by whichever process happened to call
`engage` disagrees with the platform the moment another one does. The API reads
it authoritatively at scrape time instead, and reports `atp_halt_state_readable
0` rather than failing the scrape when Redis is unreachable — the kill switch
fails closed, so an unreadable state means every order is being refused, which
is the thing to alert on.

**The scrape is a credential.** `METRICS_TOKEN`, compared in constant time, or a
valid operator session — a scraper cannot hold a cookie and a human should not
have to find a token. Unset means nothing can scrape, not that anything can. The
body carries no balance and no P&L (core has access to neither) but it does
carry the watchlist, the order flow and the times of day this platform is busy,
and `docs/SAFETY.md`'s posture towards that is not "it is only metadata".

### Why the worker exports rather than pushes

The alternative was natural here and we rejected it: the worker writes its
metrics into Redis — already the cross-process bus for the kill switch, the
quote cache and the dashboard snapshot — and the API renders them at `/metrics`.
One endpoint, one credential, no new listener, no new port.

**It makes a dead worker indistinguishable from a healthy one.** Values pushed
into Redis stay there after the process that wrote them dies. A dashboard
reading them shows the last tick rate, the last quote age and no errors: a
photograph of a working platform, served for as long as anyone cares to look.
The one failure this whole platform is written to notice — the half-running
worker in the docstring above — is precisely the failure that design makes
invisible.

A scrape cannot do that. When the process is gone the scrape fails, and a failed
scrape is the one signal a corpse cannot fake. This is also why the Prometheus
project documents the same objection to its own Pushgateway.

The cost is a listener in a process that had none: a WSGI server on a thread,
which is what `prometheus_client` does everywhere. It binds inside the container
and is **never published to the host** — reachable from the compose network like
Postgres and Redis, and `scripts/check_port_bindings.py` is what holds that,
since it is the half a compose edit can quietly get wrong.

### Tracing

**A correlation id, not spans.** `atp_core.logging.correlation_id` binds an id
into a context variable; `merge_contextvars` was already in the structlog chain,
so every event emitted underneath carries it — including from code several
layers down that knows nothing about the unit of work it is inside. Bound in
three places: every API request (from `X-Request-ID` if present, and echoed
back), every scheduled job, and every pass of the strategy loop.

**Not OpenTelemetry**, and this is a judgement about scale rather than about
quality. Spans pay for themselves when a request crosses services and the
question is *which hop was slow*. Here there are two processes on one VM, they
do not call each other synchronously — the API publishes and the worker consumes
— and the question actually asked during an incident is *which log lines belong
to the same thing*. That is what an id answers, at the cost of one context
manager and no collector, no exporter, no sampling policy and no second daemon
on a box whose whole design is "one VM, restart cleanly".

**An inbound id is sanitised, not trusted.** It goes verbatim onto every log line
the request writes, and under the console renderer a caller-supplied newline
writes its own log lines — which is how a log becomes evidence of something that
never happened, on a platform that moves money. The charset excludes whitespace
and control characters entirely, and a refused value is replaced rather than
dropped, because a request with no id is the one nobody can trace afterwards.

## Consequences

Prometheus can scrape this today with two targets and a bearer token, and
nothing has to be deployed for the numbers to be useful — `curl` with the token
is a diagnostic on its own, and a signed-in operator can open `/metrics` in the
browser they already have.

`prometheus-client` joins `atp-core`'s dependencies. Only its registry and text
exposition are imported; the parts that open a socket are not, so CLAUDE.md §1.3
still holds.

Metrics are process-global mutable state, which is unlike the rest of `core`.
They are the same shape of concern as `atp_core.logging`, which every subsystem
also reaches for without being handed one, and the alternative — threading a
registry through every constructor in the platform — buys nothing when there is
one process-wide answer to "how many orders has this worker submitted".

`reset_for_tests` exists because a counter cannot be decremented. It is named
for what it is so nobody reaches for it in application code, where it would
produce a drop in every graph that nothing explains.

**Nothing is deployed and nothing is scraping.** There is no Prometheus, no
Grafana and no alerting rule in this repo, and this ADR does not decide whether
there should be. What it decides is the shape of the thing they would read, so
that adding one later is configuration rather than a rewrite. The roadmap item
is worded to match.

**Two exporters means two things to secure, and one of them has no session to
fall back on.** The worker's listener refuses everything without the token,
including when the token is unset — the API's fallback to a cookie has no
equivalent there, so an unconfigured worker simply has no reachable metrics and
says so at startup.

## Alternatives

**Push to Redis, serve from the API.** Rejected above: it turns a dead worker
into a healthy-looking one, which is the exact failure mode this platform is
built to notice.

**A Prometheus Pushgateway.** The same objection with more moving parts, and the
Prometheus project's own documentation makes the argument against it.

**StatsD or a metrics agent on the host.** A second daemon to install, configure
and keep alive on a box whose deployment story is deliberately "one VM, docker
compose, restart cleanly" (ADR 0011). It would also invert the failure mode
again: an agent that stops shipping looks like a platform with nothing to say.

**OpenTelemetry for both metrics and traces.** A collector, an exporter, a
sampling policy and a second daemon, to answer a question — which service was
slow — that a two-process single-VM deployment does not have. Worth revisiting
if a third process ever appears, and the instrumentation points chosen here are
where its spans would go.

**Expose `/metrics` unauthenticated, like `/healthz`.** `/healthz` returns
`{"status": "ok"}` and describes nothing. This describes somebody's trading
operation. The convenience is real and is served instead by accepting the
session cookie an operator already has.

**Log-derived metrics — count the structlog events.** Tempting, since every
metric here sits beside a log line anyway. It needs a log pipeline this
deployment does not have, and it makes the numbers a parsing artefact: a
reworded log message becomes a silent gap in a graph, which is exactly the
drift the one-declaration-site rule exists to prevent.
