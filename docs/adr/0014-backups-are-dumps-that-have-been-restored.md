# 14. Backups are logical dumps, and a backup means one that has been restored

**Status:** Accepted · 2026-08-19

## Context

`docs/ROADMAP.md` Phase 6 asks for "Backups and a tested restore", and
`docs/DEPLOYMENT.md` has been stating the gap in the meantime:

> **No backups.** There is no backup and no tested restore, which means one VM
> is one VM.

ADR 0011 chose a single always-on VM per run mode, no orchestrator, no HA, and a
fail-stopped posture: broker-side stops hold positions while the platform is
down, so the recovery target is "get it back cleanly", not "never be down".
There is one operator. Nothing in that shape argues for a replica.

**What is in the database is not worth the same per row**, and this is the fact
the decision turns on:

| Table | Size | Where else it exists |
|---|---|---|
| `bars` | Unbounded; the reason ADR 0004 chose a hypertable | The market-data provider. `scripts/backfill_bars.py` refetches it. |
| `orders`, `fills` | Small | The broker, for a retention window, in the broker's own shape |
| `audit_log`, `signals` | Small | **Nowhere** |
| `position_snapshots`, `equity_snapshots` | Small | Derivable from fills, approximately |
| `strategies` | Tiny | Partly in the repository, as configuration |

So the expensive part of the database is reconstructable and the irreplaceable
part is small. A mechanism whose cost scales with `bars` is paying for the half
that is already backed up by Alpaca; whatever is chosen has to get
`audit_log` — the record of who did what to a system that moves money — out of
the building.

One more constraint comes from the schema rather than the deployment. `bars` is
a TimescaleDB hypertable: its rows are in chunks under `_timescaledb_internal`
and its shape is in the extension's own catalog. Any mechanism that copies this
database has to have an answer for that, and "it appeared to work" is not one —
a hypertable restored as an ordinary table serves every query correctly until
the day the disk fills.

## Decision

**Logical `pg_dump -Fc` per database, taken and verified by
`scripts/backup_db.py`.** Not physical base backups, not WAL archiving, not
PITR. Rationale under *Alternatives*.

**A backup is not a backup until it has been restored, so the tool that takes
them also restores them.** `backup_db.py verify` restores the newest dump into a
scratch database on the same server, compares it against what was recorded when
the dump was taken, drops the scratch database and exits non-zero if anything
disagrees. It is the command the other four exist to support, and it is meant to
run on a schedule beside `create` rather than at the moment somebody needs it.

What it compares:

- **Row counts, per table, bracketed.** Counts are read before *and* after the
  dump. `pg_dump` works from a snapshot taken between the two, so for a table
  nothing deletes from, the count inside the dump satisfies
  `before <= dumped <= after`. On a quiet database the readings are equal and
  the check is an equality; on a running one it stays sound rather than going
  flaky at 03:00, which is when it runs.
- **That `bars` came back a hypertable**, and that its compression policy came
  back with it. This is the clause that catches the TimescaleDB failure above,
  and nothing else in the platform would.
- **The Alembic revision**, so a restore into a checkout that has moved on is
  visible rather than discovered by the first migration.

**`timescaledb_pre_restore()` / `timescaledb_post_restore()` bracket every
restore, in code.** They are why this is a script and not a cron line containing
`pg_dump`. `post_restore` runs in a `finally`: the flag `pre_restore` sets lives
on the database, not the session, so a restore that fails halfway and returns
early leaves a database stuck in restoring mode with its background workers off
— which looks exactly like a database that is merely idle.

**A manifest sits beside every dump**, carrying a SHA-256, the server and client
versions, the TimescaleDB version, the run mode, and the two sets of counts. It
is deliberately *not* required to restore — `pg_restore` reads the dump alone,
so losing the sidecar costs verification and not the data.

**The restore path is guarded, and the guards come from this platform's own
shape.** An explicit `--into`, never a default. A refusal when client
connections are open. An existing database renamed aside rather than dropped.
And a refusal to restore a dump across run modes: ADR 0011 puts paper and live
on separate hosts, which makes their databases look interchangeable when they
are not, and the manifest records which one a dump came from so the tool can
tell.

**Retention is per database, not per directory**, so a shared destination cannot
let a busy database evict another one's history.

## Consequences

**A dump written next to the database it came from is not a backup.** The
default destination is on the host, because a repository cannot know what else
that host has mounted — and a disk failure or a destroyed VM takes both copies.
`ATP_BACKUP_DIR` points at somewhere that outlives the host, and until it does,
what exists is a fast rollback from operator error and not disaster recovery.
`docs/BACKUPS.md` says this in the first paragraph rather than the last.

**Dumps are not encrypted at rest.** A dump holds the entire trading record and
the audit trail. It holds no credentials — `API_PASSWORD_HASH` and the broker
keys live in `.env`, never in the database — so this is a confidentiality
problem and not a key-material one, but it is a real one the moment the file
leaves the host. The `age` key from ADR 0011 is already on the box and is the
obvious next step.

**Nothing schedules this.** There is no host to put a timer on (ADR 0011). The
cron line is written out in `docs/BACKUPS.md` and is one line, and until a
machine exists it is documentation.

**Restores are whole-database.** Recovering one table after an operator mistake
is `pg_restore -t` against the same dump — the custom format supports it, and
the tool does not wrap it. Wrapping a partial restore means deciding what
happens to foreign keys, and that decision is worth making when somebody has an
actual incident rather than in advance.

**A dump restores into its own major version.** Both are recorded in the
manifest. `--exec compose` runs the client tools inside the database container,
where they are the server's own build and cannot disagree with it; that is the
mode a deployed host should use, and it needs no postgres client installed.

## Alternatives considered

**Physical base backup + WAL archiving (PITR).** The real alternative, and it
buys a recovery point measured in seconds instead of a day. Rejected on three
counts. It needs an archive destination that outlives the host — which is
exactly the thing this repository cannot supply and which, once supplied, an
`ATP_BACKUP_DIR` uses too. Its restore is a procedure with a `recovery.signal`,
a timeline and a base-backup pairing, and this item is about the rehearsal:
nobody rehearses that one, so what would exist is a more precise backup that is
less likely to be restorable. And the recovery point it buys is worth less here
than it looks — a day of `bars` is refetchable, a day of orders and fills is
held by the broker and adopted back by `trading.restore_or_adopt`, so the window
of genuinely irreplaceable loss is the platform's own annotations.

Worth revisiting when there is an off-host destination and a second person.

**Streaming replication to a second host.** Rejected by ADR 0011 before it got
here: HA is not wanted, and a replica is not a backup — it replicates
`DELETE FROM orders` faithfully and immediately.

**`pg_dumpall`.** Rejected. The globals worth having are one role and its
password, which come from the SOPS bundle (ADR 0011) and are therefore already
backed up somewhere better. Per-database dumps also keep paper and live as
separate artefacts, which is the separation ADR 0011 built the whole deployment
around.

**Timescale Cloud, whose value ADR 0011 explicitly named as backups.** Still the
documented upgrade path, and still costs more than the whole VM. Note also what
it does and does not settle: managed snapshots would remove the mechanism, not
the rehearsal — an unrestored vendor snapshot is the same belief in a nicer
console.

**`COPY ... TO` per table, as CSV.** Human-readable and diffable, which is
genuinely attractive for `audit_log`. Rejected as the primary mechanism: it
carries no schema, no indexes, no constraints and no hypertable, so restoring it
means the migrations must run first and match — a second thing to get right
during an incident.

**Verifying by checksum alone.** Cheaper, and it answers a different question.
A SHA-256 proves the file has not rotted since it was written; it says nothing
about whether `pg_dump` wrote a usable database into it. Both are done here, and
the checksum is the gate in front of the restore rather than a substitute for
it.
