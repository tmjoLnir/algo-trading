# 24. `make up` migrates the database; `make deploy` does not

**Status:** Accepted · 2026-09-02

## Context

ADR 0023 moved the worker's trading configuration out of `.env` and into a
`worker_config` row. `atp_worker.main` reads that row at boot and, deliberately,
**re-raises if it cannot** — the argument being that a worker which silently
defaults after Postgres blinks is worse than one that refuses and is restarted.
That argument is sound, and this ADR does not disturb it.

What it did not account for is a database that has never been migrated. The row
lives in a table created by an Alembic revision, and until now the only thing in
this repository that ran Alembic was `make migrate`:

```make
migrate:
	uv run alembic -c infra/alembic/alembic.ini upgrade head
```

— from the *host*, needing uv and a Python toolchain. Nothing inside the stack
ran a migration: no entrypoint, no service, nothing in `make up`. So on a clean
checkout `make up` produced a worker that queried a table that did not exist,
raised, and was restarted forever by `restart: unless-stopped`.

That is precisely what Phase 0's ticked "`make install` and `make up` work end
to end" claims does not happen, and the item's own text spelled out the
behaviour that had stopped being true: *"On a clean checkout the worker comes up
in backtest mode with no watchlist, reports that it is ingesting nothing, and
runs its schedule."*

**It reached the default branch because the check that exists to catch it is a
coin flip.** The `stack` job's "none is restarting" step read `docker compose
ps` **once**. A crash-looping container is `running` for most of each cycle — it
boots, works for a moment, dies, waits out an exponential backoff — so a single
sample catches it or does not, by luck. The evidence is four runs of identical
code:

| Run | Tree | `stack` |
|---|---|---|
| PR #124's own CI | `a888fd1` | passed |
| merge to `claude/main` | `ca5be67` | passed |
| PR #125, first run | `ca5be67` + 2 `.md` files | failed |
| PR #125, re-run | same | failed |

Two green, two red, one tree. The gate told #124's author the stack was fine,
and it was not lying so much as guessing.

## Decision

**Two changes, and the second matters more than the first.**

**1. `make up` and `make up-prod` migrate before they start anything.** A
`migrate` service is added to `docker-compose.yml`: the API image, with
`infra/alembic/` copied into it, running `alembic upgrade head` against `db`
over the compose network, `restart: "no"`, one shot. It sits behind a
`migrate` profile so it is absent from every `up`, and the Makefile invokes it
as an explicit step before the stack starts.

**`make deploy` deliberately does not.** On a deployed host, applying a
migration is a decision an operator makes inside a halt window — stop new risk,
deploy, migrate, confirm, clear the halt (docs/DEPLOYMENT.md, "Every deploy
after that"). A container that reshapes the schema of a database holding open
positions, at whatever moment compose happens to start it, is not a convenience;
it is a migration nobody chose to run and nobody is watching. So the profile
keeps it out of the deployed path entirely and the runbook's `make migrate`
stays exactly where it is.

This is why the dependency is a Makefile step rather than a `depends_on:
{condition: service_completed_successfully}` on api, worker and queue. That
would be tidier to read and would also migrate the database wherever the stack
came up, `make deploy` included, which is the one place this must not happen.

**2. The `stack` job asserts Docker's restart counter across a settling window**
rather than sampling container state once. `RestartCount` is read for every
container, the job waits 30 seconds, and reads again; any increment fails the
build. A looping container increments it. A healthy one does not, whatever
`.State` says at either instant.

## Consequences

- A clean `git clone && make up` produces a working stack again, which is what
  Phase 0 claims and what a new contributor gets.
- The `stack` job takes ~30s longer and stops being probabilistic. It would have
  failed #124 rather than passing it, and it will fail the next boot-order
  regression on the first run instead of the third.
- The API image carries the migration scripts. It is a few kilobytes and it also
  means a deployed host can run migrations from the image it is already running,
  rather than needing a Python toolchain beside it.
- **`make up` now touches the schema.** On a developer's machine that is what
  was wanted. It is also a real widening of what that command does, which is why
  it is confined to `up`/`up-prod` and stated here.
- A failed migration stops `make up` before any application container starts,
  with the alembic error as the last thing printed. Previously a broken schema
  surfaced as a crash-looping worker several screens later.
- The deployment procedure is unchanged. Nothing in docs/DEPLOYMENT.md or
  docs/RUNBOOK.md needs revising, which was a constraint on the design rather
  than a happy outcome.

## Alternatives considered

- **Add `make migrate` to the `stack` CI job.** The smallest possible diff, and
  it would have turned the check green while leaving every operator's `make up`
  just as broken. It fixes the symptom in the one place the symptom does not
  matter.
- **`depends_on: {migrate: service_completed_successfully}` on api, worker and
  queue.** The tidiest expression of the dependency, and rejected only because
  it reaches `make deploy`. Worth revisiting if the deployed path ever grows its
  own compose overlay for bootstrap, where it could be excluded explicitly.
- **Let the worker treat a missing table as "no stored configuration" and fall
  back to defaults.** This directly contradicts ADR 0023's reasoning and is
  worse than it first looks: an unmigrated database and a blinking one become
  the same event, and the platform's response to both is to trade nothing while
  reporting itself healthy. If this is ever wanted, the distinction to draw is
  `UndefinedTableError` (a deployment that is not finished) against everything
  else (a database that did not answer) — not one blanket fallback.
- **Run migrations from an entrypoint in the worker and API images.** Every
  container racing to migrate the same database at once, with the winner
  decided by scheduling. Alembic takes a lock and would survive it, but the
  failure mode when it does not is a half-applied schema, and nothing about it
  would be legible in the logs.
- **Keep sampling state, but sample several times.** Cheaper than a settling
  window and still probabilistic — it lowers the odds of a false green without
  removing them. The restart counter is a fact about what happened over an
  interval rather than a guess assembled from instants, which is the property
  worth having in a check people will trust when it goes red.
