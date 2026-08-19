# Backups and restores

The database is the only copy of what this platform did. Positions can be
adopted back from the broker and bars can be refetched from the provider, but
the audit trail — who cleared which halt, at what time, from where — exists
here and nowhere else.

One tool does all of it:

```bash
uv run python scripts/backup_db.py create     # take one
uv run python scripts/backup_db.py verify     # prove it restores
uv run python scripts/backup_db.py list
uv run python scripts/backup_db.py restore --into atp
uv run python scripts/backup_db.py prune --keep 14
```

`make backup`, `make backup-verify` and `make backup-list` are the same three
with the arguments filled in. The reasoning behind the design is
[ADR 0014](adr/0014-backups-are-dumps-that-have-been-restored.md).

## Read this part first

**A dump on the same disk as the database is not a backup.** The default
destination is `backups/` in the checkout, because this repository cannot know
what else your host has mounted. A failed disk or a destroyed VM takes the
database and every dump beside it in the same instant. What you have until you
fix that is a fast undo for operator error, which is worth having and is not
disaster recovery.

Point it somewhere that outlives the host:

```bash
ATP_BACKUP_DIR=/mnt/backups            # a volume that is not this host's disk
```

Anything works: an attached volume, an NFS mount, an `rclone`/`restic` job that
sweeps the directory to object storage afterwards. The tooling deliberately has
no opinion and no vendor in it — what it will not do is pretend the file being
written locally has left the building.

**Dumps are not encrypted.** A dump carries the whole trading record. It carries
no credentials — those are in `.env`, never in the database — but treat the file
as you would the dashboard: it is the book. If it is going to object storage,
encrypt it on the way (the `age` key from `scripts/manage_secrets.py` is already
on the host).

## Taking one

```bash
make backup
```

On a deployment host, where the postgres client tools are usually not installed,
add `--exec compose` — the client then runs inside the `db` container, is the
server's own build, and connects over the socket the image trusts, so there is
no version question and no password to pass:

```bash
uv run python scripts/backup_db.py create --exec compose
```

It writes two files:

```
atp-atp-20260819T052226Z.dump        the dump, pg_dump custom format
atp-atp-20260819T052226Z.dump.json   the manifest
```

The dump is written to `.part` and renamed only once it has finished and been
hashed, so an interrupted run cannot leave something that looks like the newest
good backup.

The manifest records the SHA-256, the server and TimescaleDB versions, the run
mode, and the row counts taken either side of the dump. **You do not need it to
restore** — `pg_restore` reads the dump alone. You need it to *check* a restore,
which is the whole point below.

## Proving it restores

```bash
make backup-verify
```

This is the command that makes the difference between having a backup and
believing you do. It restores the newest dump into a scratch database of its
own, compares it against the manifest, drops the scratch database, and exits
non-zero if anything disagrees. It never touches the database the dump came
from.

```
verifying atp-atp-20260819T052226Z.dump
  checksum ok (41.9 KiB)
  restoring into atp_restore_check_20260819T052259Z
  10 tables compared, 504 rows restored
  hypertables restored: bars
  schema at alembic revision a1c4e77b91d2

Restored and matched. This backup is one you can rebuild from.
```

Exit codes, because this is meant to run unattended: **0** verified, **1**
checked and it does not restore, **2** could not check at all — no dump, no
client tools, no server. The middle one is the one that should page you.

`--keep` leaves the scratch database in place if you want to look inside it.

What it checks and why each clause is there:

- **The checksum**, before anything else. A file that rotted since it was
  written is refused rather than restored.
- **Row counts per table**, against the two readings taken either side of the
  dump. On a quiet database that is an equality; on a running one the count in
  the dump is bracketed by the two, so the check stays sound instead of going
  flaky overnight.
- **That `bars` came back as a hypertable**, with its compression policy. This
  is the TimescaleDB failure that nothing else would notice: a hypertable
  restored as an ordinary table answers every query correctly, and goes on doing
  so until the table is large enough that fixing it means downtime.
- **The Alembic revision**, so a dump older than the checkout is visible now
  rather than during the restore you are doing under pressure.

## On a schedule

Once there is a host (ADR 0011 has specified one; nothing is provisioned yet),
this is the whole of it:

```cron
# Nightly at 02:30 UTC — after the US close, before any pre-market work.
30 2 * * *  cd /srv/atp && uv run python scripts/backup_db.py create --exec compose --prune --keep 14 >> /var/log/atp-backup.log 2>&1
# Weekly, restore the newest one and check it. The one that should alert.
15 3 * * 0  cd /srv/atp && uv run python scripts/backup_db.py verify --exec compose >> /var/log/atp-backup.log 2>&1
```

`--prune` applies retention in the same run; retention is per database, so a
shared destination cannot let one database evict another's history.

There is no alerting wired into this. `scripts/check_alerts.py` and
`docs/OBSERVABILITY.md` are how a failure reaches a phone, and the cron line
above only writes a log — wire the exit code to something that shouts, or the
first time you learn the backup stopped will be the time you needed it.

## Restoring

**Stop the stack first.** A restore into a database the platform is reading is
refused, and would not be safe if it were not.

```bash
make down

# 1. Look at what you have, and confirm it is intact.
uv run python scripts/backup_db.py list --check

# 2. Restore. --into is never defaulted: name the database you mean.
uv run python scripts/backup_db.py restore --file backups/atp-atp-20260819T052226Z.dump --into atp

# 3. Bring the schema up to this checkout, if the dump predates it.
make migrate

# 4. Engage a halt BEFORE the stack comes up. A restored host starts willing
#    to trade — see the Redis note below, this is not automatic.
uv run python scripts/halt.py engage --by "<your name>" --detail "restored from backup"
make deploy
uv run python scripts/status.py          # broker vs. what came back
```

Then follow `docs/RUNBOOK.md` — the book you restored is as of the dump, and the
broker's is as of now. Reconcile, then clear the halt. **Do not clear the halt
because the stack came up.**

Three things the restore will refuse, each for a reason worth knowing:

- **An existing target without `--overwrite`.** With it, the existing database
  is *renamed aside* (`atp_replaced_<stamp>`) rather than dropped, so restoring
  the wrong dump is survivable. Drop the old one by hand once you are satisfied.
- **A database something is connected to.** Client connections only —
  TimescaleDB keeps a background worker attached to every database it lives in,
  and a check that counted those would refuse every restore forever.
- **A dump from the other run mode.** ADR 0011 puts paper and live on separate
  hosts, which makes their databases look interchangeable. Restoring a paper
  dump over a live account's fills and audit trail destroys the only copy of
  them. `--force` if you have decided that is genuinely what you want.

### Restoring onto a host that has nothing yet

`--dsn` skips the configuration entirely, which is the supported path when the
`.env` is not installed because the host is being rebuilt:

```bash
uv run python scripts/backup_db.py restore \
  --dsn postgresql://atp:$ATP_DB_PASSWORD@127.0.0.1:5432/postgres \
  --file /mnt/backups/atp-atp-20260819T052226Z.dump --into atp
```

### Getting one table back

The dump is `pg_dump` custom format, so a single table comes out of it without
restoring the rest. This is not wrapped by the script on purpose — what should
happen to foreign keys is a decision worth making with an actual incident in
front of you (ADR 0014):

```bash
pg_restore -t audit_log -d atp --data-only backups/atp-atp-20260819T052226Z.dump
```

## What is not here

- **Nothing runs this.** No host exists to schedule it on. The cron above is
  documentation until one does.
- **No off-host copy.** `ATP_BACKUP_DIR` is where you make that true; the tool
  will not do it for you.
- **No encryption at rest.**
- **No point-in-time recovery.** The recovery point is the last dump. ADR 0014
  argues why that is the right trade here and when it stops being one.
- **Redis is not backed up, deliberately** — and there is a trap in it. It
  holds the kill switch, the quote cache and the dashboard snapshot. The cache
  and the snapshot are re-derived within a tick, so losing them costs nothing.
  The kill switch is the problem: it fails *closed* when Redis is
  **unreachable**, but a rebuilt host has a Redis that is reachable and
  **empty**, and an empty Redis means no halt is recorded — so the platform
  comes back **willing to trade**, against a book restored as of the dump and a
  broker as of now. Engage the halt yourself before the stack starts (step 4
  above); do not infer one from the stack looking quiet.
