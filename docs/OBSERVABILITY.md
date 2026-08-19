# Observability

What this platform exports about itself, how to read it, and what it still does
not tell you. The design and the arguments behind it are [ADR 0013](adr/0013-metrics-are-scraped-from-each-process.md).

## The question this answers

Not "is the process up" — `/healthz` answers that, and it is the wrong question.
The states worth catching here are the ones where the process *is* up:

| State | What a liveness probe sees | What to look at |
|---|---|---|
| Feed went quiet mid-session | healthy | `atp_stream_last_tick_timestamp_seconds` |
| Strategy loop erroring every tick | healthy | `atp_strategy_evaluations_total{outcome="failed"}` |
| Halted hours ago, nobody cleared it | healthy | `atp_halt_active` |
| Every order refused by one rule | healthy | `atp_risk_denials_total` |
| Halts firing but no phone rang | healthy | `atp_alerts_failed_total` |
| Worker dead | **nothing** — no target | the scrape itself failing |

That last row is the reason there are two exporters rather than one. See ADR
0013; the short version is that a dead worker's numbers must *disappear*, not
freeze at their last healthy values.

## Two targets

```
api      http://api:8000/metrics       Authorization: Bearer $METRICS_TOKEN
worker   http://worker:9101/metrics    Authorization: Bearer $METRICS_TOKEN
```

Both are unversioned, both need the token, and neither is published to the host
— they are reachable across the compose network, like Postgres and Redis.

The API's endpoint **also** accepts a valid operator session, so you can open
`https://<host>/metrics` in the browser you are already signed in to and read it
with your eyes. A scraper cannot hold a cookie and a human should not have to go
and find a token; both are the same endpoint.

```bash
# from the host
curl -H "Authorization: Bearer $METRICS_TOKEN" http://127.0.0.1:8000/metrics

# just the feed's pulse, in human terms
curl -sH "Authorization: Bearer $METRICS_TOKEN" http://127.0.0.1:8000/metrics \
  | grep atp_stream_last_tick \
  | awk -F' ' '{print $1, systime() - $2 "s ago"}'
```

### Configuring it

```bash
METRICS_TOKEN=$(openssl rand -hex 32)   # long: it is compared at HTTP speed
WORKER_METRICS_PORT=9101                # container-internal, never published
```

`METRICS_TOKEN` is a credential and belongs in the SOPS bundle
([DEPLOYMENT.md](DEPLOYMENT.md)), not in a commit. **Unset means nothing can
scrape** — the API still answers a signed-in operator, the worker answers
nobody, and both say so at startup rather than leaving you to discover a target
that never came up.

A minimal scrape config:

```yaml
scrape_configs:
  - job_name: atp
    authorization: { type: Bearer, credentials: "<METRICS_TOKEN>" }
    static_configs:
      - targets: ["api:8000", "worker:9101"]
```

## What is exported

Everything is declared in one file — `libs/core/src/atp_core/metrics/registry.py`
— so `cat` is the authoritative inventory and the table below is a summary.

**The kill switch and risk**

| Metric | |
|---|---|
| `atp_halts_engaged_total{scope,reason}` | counts a *new* halt. Re-engaging an active one is not a second incident, the same way it is not a second notification (ADR 0012) |
| `atp_halts_cleared_total{scope}` | every one of these is somebody's decision |
| `atp_halt_active{scope,reason,target}` | read from Redis at scrape time, API only |
| `atp_halt_state_readable` | **0 means orders are being refused** — the kill switch fails closed |
| `atp_risk_checks_total{outcome}` | `approved`, `shrunk` or `denied` |
| `atp_risk_denials_total{rule}` | which rule refused |

**Order flow**

| Metric | |
|---|---|
| `atp_orders_submitted_total{side,order_type}` | the venue has it working — not "the call returned" |
| `atp_orders_rejected_total{stage}` | `risk`, `broker`, `acknowledgement`, `indeterminate` |
| `atp_order_submit_seconds{broker}` | time in `submit_order`, whatever its outcome |

`stage="indeterminate"` is the one to page on. It means a submit failed in
transport and could not be resolved against the venue — we may be holding a
position nobody knows about. It always comes with a halt.

**Market data**

| Metric | |
|---|---|
| `atp_stream_messages_total{kind}` | `quote` or `bar` |
| `atp_stream_reconnects_total` | each one left a gap that had to be closed |
| `atp_stream_gap_bars_backfilled_total` | bars fetched over REST to close them |
| `atp_stream_last_tick_timestamp_seconds{symbol}` | when each symbol last printed |

The last one is a **timestamp, not an age**, and that is deliberate throughout.
An age gauge is only true at the instant it is written, so an ingestor that
stopped goes on reporting a small, steady, reassuring number for as long as it
stays stopped. Subtract it from `time()` at query time and a frozen exporter
produces a rising age on its own:

```promql
time() - atp_stream_last_tick_timestamp_seconds > 300
```

**The strategy loop**

| Metric | |
|---|---|
| `atp_strategy_evaluations_total{strategy,outcome}` | `succeeded` or `failed` |
| `atp_strategy_evaluation_seconds{strategy}` | one pass |

A flat `succeeded` rate with a live process is the failure `runner.evaluate`'s
docstring warns about — "a runner erroring every tick is not trading, but it
looks alive to a health check".

**Alerting**

| Metric | |
|---|---|
| `atp_alerts_sent_total{transport,severity}` | a transport accepted it |
| `atp_alerts_failed_total{transport}` | it did not go out; the event still happened |

Not answerable from the halt counters, which count a halt whether or not anybody
was told. `transport="logging"` counts as a failure on purpose: an unconfigured
platform reading zero-sent-zero-failed is indistinguishable from one with
nothing to report.

**The API, and the build**

`atp_api_requests_total{method,route,status}`, `atp_api_request_seconds`, and
`atp_build_info{version,run_mode}` — always 1, the labels are the payload.

`route` is the **template** (`/api/v1/positions/{symbol}`), never the concrete
path, and anything that matched no route is labelled `<unmatched>`. An unrouted
path is attacker-chosen, and labelling by it is one time series per URL anybody
cares to invent.

## Tracing is a correlation id

Every log line written inside a unit of work carries `correlation_id`. Three
units are bound today:

- **an API request** — from the inbound `X-Request-ID` if there is one, else
  generated, and always echoed back on the response
- **a scheduled job** — one id per run of `backfill_missing_bars` and friends
- **a pass of the strategy loop** — the whole of `evaluate`

The point is depth rather than breadth. The id is bound in a context variable
and `merge_contextvars` puts it on every event underneath, so the risk engine's
refusal, the router's submit and the broker adapter's retry all carry it without
any of them knowing a loop exists.

```bash
# everything one slow request did
docker compose logs api | grep '"correlation_id": "9f2c1ab4c07d5e13"'

# find the id first — the browser's network tab has it on the response
curl -si .../api/v1/dashboard/live | grep -i x-request-id
```

An **inbound** id is sanitised before it is bound: 64 characters of
`[A-Za-z0-9._:-]` or it is replaced. A caller-supplied newline would end a log
line and write its own, which is how somebody who can reach the API forges a log
entry about a platform that moves money.

There are no spans and no collector, and ADR 0013 argues why: two processes on
one VM that do not call each other synchronously do not have a "which hop was
slow" question to answer.

## What this still does not give you

- **Nothing is scraping.** There is no Prometheus, no Grafana and no alerting
  rule in this repo. What exists is the exporter and the format; a scrape config
  is four lines and is not committed here because no host has been chosen (ADR
  0011).
- **No alerting rules.** The PromQL above is illustrative. The one alerting path
  that exists is the kill switch reaching a phone (ADR 0012), which is a
  different mechanism and deliberately narrower.
- **No P&L, no balances, no positions.** Metrics never carry the book, which is
  the same rule alerts follow and for the same reason: this is transport to a
  third party and the numbers belong on the dashboard, behind authentication.
- **No log aggregation.** `docker compose logs` is still how you read logs; the
  correlation id makes grep enough rather than making it unnecessary.
- **The histograms have never seen production traffic.** Buckets were chosen
  from what the code does rather than from what a week of trading looks like,
  and the first real week is when they should be re-cut.
