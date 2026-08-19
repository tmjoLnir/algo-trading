#!/usr/bin/env python3
"""Take a backup of the trading database, and prove the backup restores.

    uv run python scripts/backup_db.py create
    uv run python scripts/backup_db.py list
    uv run python scripts/backup_db.py verify            # the tested restore
    uv run python scripts/backup_db.py restore --file <dump> --into atp
    uv run python scripts/backup_db.py prune --keep 14

`docs/ROADMAP.md` Phase 6 asks for "Backups and a tested restore", and the two
halves are not equally hard. Taking a `pg_dump` is one line. Knowing that the
file it produced would actually bring the platform back is the item — an
untested backup is a belief, and this is the difference between believing and
having checked. `verify` is therefore the command this file exists for; the
rest is what it needs around it.

**Logical dumps, not PITR.** ADR 0014 has the argument. In short: one VM, one
operator, a fail-stopped posture, and an asymmetry in the data itself — `bars`
is large and reproducible from the provider (`scripts/backfill_bars.py`), while
`orders`, `fills` and `audit_log` are small and reproducible from nothing at
all. What must survive is the part that is cheap to dump.

**The TimescaleDB dance is not optional.** `bars` is a hypertable: its rows live
in chunks under `_timescaledb_internal` and its shape lives in the extension's
own catalog, both of which `pg_dump` faithfully dumps. Restoring them into a
database whose extension is live means writing the extension's catalog behind
its back. `timescaledb_pre_restore()` / `timescaledb_post_restore()` bracket the
restore and are what make the difference between a database and a
convincing-looking one; `_restore_into` below will not run a restore without
them. This is the single reason this file is a script rather than a cron line
containing `pg_dump`.

**Nothing here ever puts the password in `argv`.** `/proc/<pid>/cmdline` is
world-readable and `/proc/<pid>/environ` is readable only by the owning user, so
a credential passed as `-d postgresql://atp:secret@…` is visible to every
account on the host for as long as the dump runs — which, for the largest table
in the platform, is the longest window it could pick. The password goes in
`PGPASSWORD` in the child's environment and the connection is described with
`-h/-p/-U/-d` flags (CLAUDE.md §1.6). In `--exec compose` it is not passed at
all: the client runs inside the database container, where it reaches the server
over the local socket that the image trusts.

**`--exit-on-error`, always.** `pg_restore` defaults to continuing past errors
and exiting 0 with "warnings ignored" — a half-restored database reported as a
success, which is the exact failure mode this whole item exists to rule out.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Literal
from urllib.parse import unquote, urlsplit

from atp_core.clock import SystemClock

if TYPE_CHECKING:
    from collections.abc import Sequence

REPO = Path(__file__).resolve().parents[1]

#: Where dumps land by default. Gitignored — a dump is the entire trading
#: record, and `check-tracked` is not a thing that would save you here.
#:
#: On the same disk as the database it is backing up, which is the honest
#: default for a repository that cannot know what else a host has mounted, and
#: is NOT a backup on its own: a disk failure or a destroyed VM takes both. Set
#: ATP_BACKUP_DIR to a mount that outlives the host. docs/BACKUPS.md is blunt
#: about this being the gap the tooling cannot close for you.
DEFAULT_BACKUP_DIR = REPO / "backups"

#: `-Fc`, custom format: compressed, and restorable table-by-table with
#: `pg_restore -t`, which plain SQL is not. The selective restore matters for
#: the case docs/BACKUPS.md walks through — recovering `orders` and `fills`
#: after an operator mistake, without rolling `bars` back with them.
DUMP_FORMAT = "c"

#: Timestamp format for a dump's name: sortable, unambiguous, and UTC because
#: everything in this platform is (CLAUDE.md §1.2). Not the local time of
#: whoever happened to run it.
STAMP = "%Y%m%dT%H%M%SZ"

#: Kept because a restore that is only checked at restore time is checked once,
#: under pressure, by someone who has just lost a host.
DEFAULT_KEEP = 14

#: Scratch databases `verify` creates and drops. The prefix is deliberately
#: unmistakable: anything matching it is disposable, and nothing the platform
#: reads is ever named this.
VERIFY_DB_PREFIX = "atp_restore_check_"

#: Verification failed: a dump is missing, corrupt, or restored into something
#: that does not match what was dumped.
EXIT_UNVERIFIED = 1
#: Could not run the check at all — no client tools, no server, no backups yet.
#: Distinct from a failure for the reason check_alerts.py gives: the two need
#: opposite things done about them.
EXIT_UNAVAILABLE = 2

ExecMode = Literal["auto", "local", "compose"]


class BackupError(Exception):
    """Something an operator has to decide about.

    Carries no credential, by construction: every raise site is built from
    paths, database names and a client's stderr.
    """


# ── where the database is ────────────────────────────────────────────────────
@dataclass(frozen=True)
class Target:
    """A database, decomposed into the flags libpq takes one at a time.

    Deliberately not carried around as a URL. A URL has to be passed to a client
    as one string, and the only place to put it is `argv` — see the module
    docstring for why that is the one thing this file will not do.
    """

    host: str
    port: int
    user: str
    database: str
    password: str | None

    @classmethod
    def from_url(cls, url: str, *, database: str | None = None) -> Target:
        """Parse a SQLAlchemy or libpq URL.

        `postgresql+asyncpg://` is what `atp_core.config.Settings` holds and is
        not a scheme libpq knows; the driver suffix is dropped here rather than
        at every call site.
        """
        parts = urlsplit(url.replace("+asyncpg", "").replace("+psycopg", ""))
        if parts.scheme not in ("postgres", "postgresql"):
            raise BackupError(
                f"not a PostgreSQL URL: scheme is {parts.scheme!r}.\n"
                "  Expected postgresql://user:password@host:port/database"
            )
        name = database if database is not None else unquote(parts.path.lstrip("/"))
        if not name:
            raise BackupError(f"no database name in the URL (host {parts.hostname or '?'})")
        return cls(
            host=parts.hostname or "localhost",
            port=parts.port or 5432,
            user=unquote(parts.username or "postgres"),
            database=name,
            # unquote: a password with a `@` or `/` in it is percent-encoded in
            # the URL and must not be handed to libpq still encoded.
            password=unquote(parts.password) if parts.password else None,
        )

    def on(self, database: str) -> Target:
        """The same server, a different database."""
        return Target(self.host, self.port, self.user, database, self.password)

    def __str__(self) -> str:
        """Safe to print, log and put in an exception. No password, ever."""
        return f"{self.user}@{self.host}:{self.port}/{self.database}"


def resolve_target(dsn: str | None) -> Target:
    """The database to work on: `--dsn` if given, else the configured one.

    `get_settings()` is imported here rather than at module scope so that
    `--dsn` works on a machine with no `.env` — restoring onto a fresh host is
    exactly the situation where the configuration has not been installed yet,
    and a settings error at import time would make this script the thing
    standing between an operator and their data.
    """
    if dsn:
        return Target.from_url(dsn)
    try:
        from atp_core.config import get_settings

        return Target.from_url(get_settings().database_url)
    except BackupError:
        raise
    except Exception as exc:  # any settings failure means the same thing
        raise BackupError(
            f"could not read DATABASE_URL from the configuration: {exc}\n"
            "  Pass --dsn postgresql://user:password@host:port/database to work "
            "without a .env — that is the supported path on a host being rebuilt."
        ) from exc


# ── running the client tools ─────────────────────────────────────────────────
@dataclass(frozen=True)
class Executor:
    """How `psql`, `pg_dump` and `pg_restore` get run.

    Two modes, and the choice is about which binary you get rather than about
    convenience:

    `local`   the tools on PATH, over TCP. Needs a client at least as new as
              the server — `pg_dump` refuses to dump a server newer than
              itself, and `_require_client_version` says so in advance rather
              than letting the operator read libpq's version of the message.

    `compose` the tools inside the `db` container, over the container's local
              socket. The client is then the server's own build, so the version
              question cannot arise, and the host needs no postgres client
              installed at all. It is also the mode that passes no password:
              the image trusts local-socket connections (see the note in
              docker-compose.prod.yml), so there is no credential to leak.

    `auto` prefers `local` when the tools are on PATH, because it is the mode
    that works in CI, in a test, and on a machine that is not the deployment
    host. On the deployment host, where they usually are not installed, it
    resolves to `compose` on its own.
    """

    mode: Literal["local", "compose"]
    service: str = "db"

    @classmethod
    def resolve(cls, mode: ExecMode, *, service: str = "db") -> Executor:
        if mode == "auto":
            chosen: Literal["local", "compose"] = "local" if shutil.which("pg_dump") else "compose"
            return cls(chosen, service)
        return cls(mode, service)

    def argv(
        self, tool: str, target: Target, args: Sequence[str], *, connect: bool = True
    ) -> list[str]:
        """The command line for one tool.

        `connect=False` omits every connection flag, which is not a tidiness
        preference: `pg_dump --version` given a `-d` as well exits 1 with a bare
        "Try --help" and no error line, so a version probe written the obvious
        way fails and blames the database. Found by running it.
        """
        if not connect:
            if self.mode == "local":
                return [tool, *args]
            return ["docker", "compose", "exec", "-T", self.service, tool, *args]
        if self.mode == "local":
            return [
                tool,
                "-w",
                "-h", target.host,
                "-p", str(target.port),
                "-U", target.user,
                "-d", target.database,
                *args,
            ]  # fmt: skip
        # No -h: inside the container, omitting it uses the local socket, which
        # is the whole reason this mode needs no password. `-T` disables the
        # pseudo-TTY compose allocates by default — with one attached, a
        # `-Fc` dump comes back with its newlines rewritten and every dump this
        # tool takes is quietly unrestorable.
        return [
            "docker", "compose", "exec", "-T", self.service,
            tool, "-w", "-U", target.user, "-d", target.database, *args,
        ]  # fmt: skip

    def env(self, target: Target) -> dict[str, str]:
        env = dict(os.environ)
        if self.mode == "local" and target.password:
            env["PGPASSWORD"] = target.password
        else:
            # Do not let an inherited PGPASSWORD reach a client that is meant
            # to be authenticating some other way.
            env.pop("PGPASSWORD", None)
        return env

    def describe(self) -> str:
        return "local client tools" if self.mode == "local" else f"compose exec -T {self.service}"


def _fail(
    tool: str,
    result: subprocess.CompletedProcess[bytes] | subprocess.CalledProcessError,
    target: Target,
) -> BackupError:
    stderr = (result.stderr or b"").decode("utf-8", "replace").strip()
    return BackupError(f"{tool} failed against {target} (exit {result.returncode})\n  {stderr}")


def run_tool(
    executor: Executor,
    tool: str,
    target: Target,
    args: Sequence[str],
    *,
    stdout: int | None = subprocess.PIPE,
    connect: bool = True,
) -> subprocess.CompletedProcess[bytes]:
    """Run one client tool, or raise a `BackupError` carrying its stderr.

    Binary-safe: stdout is bytes, because for `pg_dump` it is the dump.
    """
    argv = executor.argv(tool, target, args, connect=connect)
    try:
        result = subprocess.run(  # argv is built here, never a shell string
            argv,
            check=False,
            stdout=stdout,
            stderr=subprocess.PIPE,
            env=executor.env(target),
            cwd=REPO,
        )
    except FileNotFoundError as exc:
        raise BackupError(
            f"{tool} is not available ({executor.describe()}).\n"
            "  --exec local needs the postgres client tools on PATH "
            "(apt install postgresql-client-16).\n"
            "  --exec compose needs a running `db` service and a docker daemon."
        ) from exc
    if result.returncode != 0:
        raise _fail(tool, result, target)
    return result


def query(executor: Executor, target: Target, sql: str) -> list[list[str]]:
    """Run one read-only statement and return its rows as split fields.

    `-tAF\\x1f`: tuples only, unaligned, with a field separator that cannot
    occur in an identifier or a count. The default `|` can and does — a symbol
    is free to contain one.
    """
    result = run_tool(
        executor,
        "psql",
        target,
        ["-v", "ON_ERROR_STOP=1", "-t", "-A", "-F", "\x1f", "-c", sql],
    )
    text = result.stdout.decode("utf-8", "replace").strip()
    return [line.split("\x1f") for line in text.splitlines() if line]


def scalar(executor: Executor, target: Target, sql: str) -> str | None:
    rows = query(executor, target, sql)
    return rows[0][0] if rows and rows[0] and rows[0][0] != "" else None


def execute(executor: Executor, target: Target, sql: str) -> None:
    """Run a statement for its effect, failing on the first error."""
    run_tool(executor, "psql", target, ["-v", "ON_ERROR_STOP=1", "-c", sql])


# ── what a dump is worth knowing about ───────────────────────────────────────
def _require_client_version(executor: Executor, target: Target) -> tuple[str, str]:
    """Refuse to dump with a client older than the server, and say so first.

    `pg_dump` does check this itself, and reports it as "server version 16.13;
    pg_dump version 15.6" after the operator has already scheduled the job.
    Catching it here means the message names the fix.
    """
    server = scalar(executor, target, "SHOW server_version") or "unknown"
    raw = run_tool(executor, "pg_dump", target, ["--version"], connect=False).stdout
    client = _version_in(raw.decode("utf-8", "replace"))

    if _major(client) and _major(server) and _major(client) < _major(server):
        raise BackupError(
            f"pg_dump is {client} and the server is {server}: a dump taken with "
            "an older client is not a dump.\n"
            f"  Install postgresql-client-{_major(server)}, or use --exec compose, "
            "which runs the server's own binaries inside the db container and "
            "cannot disagree with it."
        )
    return client, _version_in(server)


def _version_in(text: str) -> str:
    """Pull the version out of a `--version` banner or a `server_version`.

    Both are noisier than they look: `pg_dump --version` answers "pg_dump
    (PostgreSQL) 16.13 (Ubuntu 16.13-0ubuntu0.24.04.1)", whose last token is the
    packaging, not the version. Take the first token that is only digits and
    dots.
    """
    for token in text.replace("(", " ").replace(")", " ").split():
        if token[0].isdigit() and set(token) <= set("0123456789."):
            return token.rstrip(".")
    return "unknown"


def _major(version: str) -> int:
    head = _version_in(version).split(".")[0]
    return int(head) if head.isdigit() else 0


def timescale_version(executor: Executor, target: Target) -> str | None:
    return scalar(
        executor, target, "SELECT extversion FROM pg_extension WHERE extname = 'timescaledb'"
    )


def hypertables(executor: Executor, target: Target) -> list[str]:
    """Hypertables in `public`, or an empty list if the extension is absent.

    This is the property a naive restore loses silently. `bars` coming back as
    an ordinary table leaves every query working and the storage design gone
    (ADR 0004) — nothing fails until the table is large enough that it is
    expensive to fix.
    """
    if timescale_version(executor, target) is None:
        return []
    rows = query(
        executor,
        target,
        "SELECT hypertable_name FROM timescaledb_information.hypertables "
        "WHERE hypertable_schema = 'public' ORDER BY hypertable_name",
    )
    return [r[0] for r in rows]


def compression_jobs(executor: Executor, target: Target) -> int:
    """How many compression policies exist — 0 after a restore that dropped them."""
    if timescale_version(executor, target) is None:
        return 0
    found = scalar(
        executor,
        target,
        "SELECT count(*) FROM timescaledb_information.jobs WHERE proc_name = 'policy_compression'",
    )
    return int(found or 0)


def user_tables(executor: Executor, target: Target) -> list[str]:
    rows = query(
        executor,
        target,
        "SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
        "WHERE n.nspname = 'public' AND c.relkind IN ('r', 'p') ORDER BY c.relname",
    )
    return [r[0] for r in rows]


def table_counts(executor: Executor, target: Target, tables: Sequence[str]) -> dict[str, int]:
    """Exact row counts, in one round trip.

    `count(*)` and not `reltuples`: an estimate that happens to match is not
    evidence that a restore worked, which is the only thing these numbers are
    for. On `bars` this reads the whole hypertable, chunks and all — the cost
    that `--no-counts` exists to decline.
    """
    if not tables:
        return {}
    union = " UNION ALL ".join(
        # The identifier is doubled-quoted and the label is single-quoted; both
        # come from pg_class, so neither is operator input.
        f"SELECT '{t}' AS t, count(*) AS n FROM \"{t}\""
        for t in tables
    )
    rows = query(executor, target, f"SELECT t, n FROM ({union}) c ORDER BY t")
    return {r[0]: int(r[1]) for r in rows}


def current_run_mode() -> str | None:
    """Which run mode took this dump, if that is knowable here.

    Recorded so that `restore` can refuse to write a paper trading record over
    a live one. ADR 0011 puts the two on separate hosts with separate
    databases, which makes them look interchangeable in a way they are not: the
    audit trail and the fill history of a live account cannot be reconstructed
    from anywhere, and a paper dump restored over them destroys the only copy.
    """
    try:
        from atp_core.config import get_settings

        return str(get_settings().run_mode.value)
    except Exception:  # no configuration is a fact to record, not an error
        mode = os.environ.get("ATP_RUN_MODE")
        return mode or None


# ── the backup files ─────────────────────────────────────────────────────────
@dataclass(frozen=True)
class Manifest:
    """What was true about the database at the moment a dump was taken.

    Written beside the dump, and not needed to restore it — `pg_restore` reads
    the dump alone. It is needed to *check* a restore, which is the difference
    this file is about.

    Two sets of counts, before and after. `pg_dump` works from a snapshot taken
    somewhere between them, so for a table nothing deletes from, the number of
    rows inside the dump is bracketed: `before <= dumped <= after`. On a quiet
    database the two readings are equal and the check is an equality. On a
    running one it stays sound instead of going flaky at 3am, which is when
    this runs.
    """

    file: str
    sha256: str
    size_bytes: int
    created_at: str
    database: str
    server_version: str
    pg_dump_version: str
    timescaledb_version: str | None
    hypertables: list[str]
    compression_jobs: int
    run_mode: str | None
    counts_before: dict[str, int]
    counts_after: dict[str, int]

    def to_json(self) -> str:
        return json.dumps(self.__dict__, indent=2, sort_keys=True) + "\n"

    @classmethod
    def load(cls, path: Path) -> Manifest:
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise BackupError(f"cannot read the manifest {path.name}: {exc}") from exc
        known = {f: raw.get(f) for f in cls.__dataclass_fields__}
        missing = [f for f, v in known.items() if v is None and f not in _OPTIONAL_FIELDS]
        if missing:
            raise BackupError(
                f"{path.name} is missing {', '.join(missing)} — it was not written by "
                "this tool, or was written by an older one. Restore is still possible "
                "with `restore --file`, which does not read a manifest; verification "
                "is not."
            )
        return cls(**known)


#: Legitimately absent: a database with no TimescaleDB, or a dump taken where
#: no configuration said which run mode it belonged to.
_OPTIONAL_FIELDS = frozenset({"timescaledb_version", "run_mode"})


def manifest_path(dump: Path) -> Path:
    return dump.with_suffix(dump.suffix + ".json")


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def backup_dir(explicit: str | None) -> Path:
    return Path(explicit or os.environ.get("ATP_BACKUP_DIR") or DEFAULT_BACKUP_DIR)


def parse_name(dump: Path) -> tuple[str, str] | None:
    """Split `atp-<database>-<stamp>.dump` back into its two halves.

    Returns None for a file this tool did not name. Nothing is deleted or
    restored on the strength of a parsed name — it decides ordering and which
    dumps compete for the same retention slots, and an unrecognised one is
    grouped on its own rather than assumed to belong with the rest.
    """
    stem = dump.name.removesuffix(".dump")
    if not stem.startswith("atp-") or "-" not in stem[4:]:
        return None
    database, _, stamp = stem[4:].rpartition("-")
    return (database, stamp) if database and stamp else None


def find_backups(directory: Path) -> list[Path]:
    """Every dump in the directory, newest first.

    Ordered by the stamp in the name and not by the whole name, which would sort
    by database first and hand `verify` somebody else's newest backup; and not
    by mtime, which a copy between hosts rewrites — the moment a file most needs
    to keep its identity is the moment it is moved off the machine.
    """
    if not directory.is_dir():
        return []

    def ordering(dump: Path) -> tuple[str, str]:
        parsed = parse_name(dump)
        return (parsed[1] if parsed else "", dump.name)

    return sorted(directory.glob("*.dump"), key=ordering, reverse=True)


def newest_backup(directory: Path) -> Path:
    found = find_backups(directory)
    if not found:
        raise BackupError(
            f"no backups in {directory}. Take one first: "
            "`uv run python scripts/backup_db.py create`"
        )
    return found[0]


def human_bytes(size: int) -> str:
    value = float(size)
    for unit in ("B", "KiB", "MiB", "GiB"):
        if value < 1024 or unit == "GiB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} GiB"


# ── the restore itself ───────────────────────────────────────────────────────
def _restore_into(executor: Executor, target: Target, dump: Path, *, timescale: bool) -> None:
    """Restore one dump into an existing, empty database.

    The `pre_restore`/`post_restore` bracket is the whole reason this is a
    function and not a command line. Between them the extension stops
    maintaining its own catalog and stops its background workers, so the rows
    `pg_dump` took out of `_timescaledb_catalog` can go back in as rows. Skip
    them and the restore either errors on the extension's own constraints or —
    worse, and what actually happens with a hypertable this size — succeeds
    into a database whose catalog disagrees with its chunks.

    `post_restore` runs in a `finally` because `pre_restore` sets a flag on the
    *database*, not on the session: a restore that fails halfway and returns
    early leaves a database stuck in restoring mode, background workers off,
    which looks exactly like a database that is merely idle.

    The dump is fed over stdin rather than named as an argument so that both
    executors take the same path — in `--exec compose` the file is on the host
    and the client is in the container, where a path would not resolve.
    """
    if timescale:
        execute(executor, target, "CREATE EXTENSION IF NOT EXISTS timescaledb")
        execute(executor, target, "SELECT timescaledb_pre_restore()")
    try:
        argv = executor.argv(
            "pg_restore",
            target,
            # --exit-on-error: see the module docstring. Without it a restore
            # that dropped half the schema exits 0.
            ["--exit-on-error"],
        )
        with dump.open("rb") as fh:
            result = subprocess.run(  # argv built above, no shell
                argv,
                check=False,
                stdin=fh,
                capture_output=True,
                env=executor.env(target),
                cwd=REPO,
            )
        if result.returncode != 0:
            raise _fail("pg_restore", result, target)
    finally:
        if timescale:
            execute(executor, target, "SELECT timescaledb_post_restore()")


def _client_connections_to(executor: Executor, target: Target, database: str) -> int:
    """How many *clients* are using this database.

    `backend_type = 'client backend'` is load-bearing, not tidiness. TimescaleDB
    runs a background worker per database with the extension installed, and it
    appears in `pg_stat_activity` against that database like anything else — so
    a plain count is never zero for any database this platform uses, and a
    restore guarded on one would refuse every time, forever, including on a host
    where nothing at all is running.
    """
    found = scalar(
        executor,
        target.on("postgres"),
        "SELECT count(*) FROM pg_stat_activity WHERE datname = "
        f"'{database}' AND pid <> pg_backend_pid() AND backend_type = 'client backend'",
    )
    return int(found or 0)


def _detach_background_workers(executor: Executor, target: Target, database: str) -> None:
    """Disconnect what is left after the clients have gone.

    Only reached once `_client_connections_to` has reported zero, so what this
    terminates is TimescaleDB's per-database scheduler and nothing else. It has
    to go: `ALTER DATABASE ... RENAME` and `DROP DATABASE` both require that
    *no* backend is connected, background workers included, and the extension
    reconnects one within seconds of the database existing.
    """
    execute(
        executor,
        target.on("postgres"),
        "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
        f"WHERE datname = '{database}' AND pid <> pg_backend_pid()",
    )


def _database_exists(executor: Executor, target: Target, database: str) -> bool:
    return (
        scalar(
            executor,
            target.on("postgres"),
            f"SELECT 1 FROM pg_database WHERE datname = '{database}'",
        )
        is not None
    )


# ── commands ─────────────────────────────────────────────────────────────────
def cmd_create(args: argparse.Namespace) -> int:
    target = resolve_target(args.dsn)
    executor = Executor.resolve(args.exec_mode)
    directory = backup_dir(args.dir)
    directory.mkdir(parents=True, exist_ok=True)

    client, server = _require_client_version(executor, target)
    tables = user_tables(executor, target)
    counts_before = {} if args.no_counts else table_counts(executor, target, tables)

    stamp = SystemClock().now().strftime(STAMP)
    dump = directory / f"atp-{target.database}-{stamp}.dump"
    # Written to `.part` and renamed only once the dump has finished and been
    # hashed. A dump interrupted by a reboot or a full disk must not be able to
    # look like the newest good backup — which is the one `verify` and
    # `restore` reach for by default.
    part = dump.with_suffix(".part")

    print(f"backing up {target} via {executor.describe()}")
    argv = executor.argv("pg_dump", target, ["-F", DUMP_FORMAT])
    try:
        with part.open("wb") as fh:
            result = subprocess.run(  # argv built by the executor
                argv,
                check=False,
                stdout=fh,
                stderr=subprocess.PIPE,
                env=executor.env(target),
                cwd=REPO,
            )
        if result.returncode != 0:
            raise _fail("pg_dump", result, target)

        counts_after = {} if args.no_counts else table_counts(executor, target, tables)
        manifest = Manifest(
            file=dump.name,
            sha256=sha256_of(part),
            size_bytes=part.stat().st_size,
            created_at=SystemClock().now().isoformat(),
            database=target.database,
            server_version=server,
            pg_dump_version=client,
            timescaledb_version=timescale_version(executor, target),
            hypertables=hypertables(executor, target),
            compression_jobs=compression_jobs(executor, target),
            run_mode=current_run_mode(),
            counts_before=counts_before,
            counts_after=counts_after,
        )
        part.replace(dump)
    except BaseException:
        part.unlink(missing_ok=True)
        raise
    manifest_path(dump).write_text(manifest.to_json(), encoding="utf-8")

    print(f"  {dump.name}  {human_bytes(manifest.size_bytes)}  sha256:{manifest.sha256[:16]}…")
    if manifest.hypertables:
        print(f"  hypertables: {', '.join(manifest.hypertables)}")
    total = sum(counts_before.values())
    if counts_before:
        print(f"  {len(counts_before)} tables, {total} rows")
    if args.prune:
        _prune(directory, keep=args.keep, dry_run=False)
    print("\nA dump nobody has restored is not a backup. Run `verify` next.")
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    directory = backup_dir(args.dir)
    found = find_backups(directory)
    if not found:
        print(f"no backups in {directory}")
        return EXIT_UNAVAILABLE
    print(f"{directory}\n")
    for dump in found:
        size = human_bytes(dump.stat().st_size)
        note = ""
        mpath = manifest_path(dump)
        if not mpath.exists():
            note = "  (no manifest — restorable, not verifiable)"
        elif args.check:
            manifest = Manifest.load(mpath)
            note = "  ok" if sha256_of(dump) == manifest.sha256 else "  CHECKSUM MISMATCH"
        print(f"  {dump.name:<44} {size:>10}{note}")
    return 0


def cmd_verify(args: argparse.Namespace) -> int:
    """Restore a backup into a scratch database and compare it with the source.

    This is the item. Everything else here is in service of it.
    """
    directory = backup_dir(args.dir)
    dump = Path(args.file) if args.file else newest_backup(directory)
    if not dump.exists():
        raise BackupError(f"no such backup: {dump}")
    manifest = Manifest.load(manifest_path(dump))

    print(f"verifying {dump.name}")
    actual = sha256_of(dump)
    if actual != manifest.sha256:
        print(f"  FAILED: sha256 is {actual[:16]}…, the manifest says {manifest.sha256[:16]}…")
        print("  The file changed after it was written. Do not restore it.")
        return EXIT_UNVERIFIED
    print(f"  checksum ok ({human_bytes(manifest.size_bytes)})")

    target = resolve_target(args.dsn)
    executor = Executor.resolve(args.exec_mode)
    scratch = f"{VERIFY_DB_PREFIX}{SystemClock().now().strftime(STAMP)}"
    if scratch == target.database:
        raise BackupError("refusing to verify into the database being verified")

    admin = target.on("postgres")
    execute(executor, admin, f'CREATE DATABASE "{scratch}"')
    restored = target.on(scratch)
    print(f"  restoring into {scratch}")
    try:
        _restore_into(executor, restored, dump, timescale=manifest.timescaledb_version is not None)
        failures = _compare(executor, restored, manifest)
    finally:
        if args.keep:
            print(f"  left {scratch} in place (--keep)")
        else:
            # WITH (FORCE): `post_restore` hands the database back to the
            # extension, which attaches its scheduler to it. A plain DROP would
            # fail on that worker and leave a scratch database behind on every
            # run — which is how a verification step becomes something people
            # stop running.
            execute(executor, admin, f'DROP DATABASE IF EXISTS "{scratch}" WITH (FORCE)')

    if failures:
        print("\nRESTORE DID NOT MATCH THE SOURCE:")
        for line in failures:
            print(f"  {line}")
        return EXIT_UNVERIFIED
    print("\nRestored and matched. This backup is one you can rebuild from.")
    return 0


def _compare(executor: Executor, restored: Target, manifest: Manifest) -> list[str]:
    """What the restored database must look like for the dump to be worth having."""
    failures: list[str] = []

    tables = user_tables(executor, restored)
    expected_tables = set(manifest.counts_before) | set(manifest.counts_after)
    for missing in sorted(expected_tables - set(tables)):
        failures.append(f"table {missing} did not come back")

    if manifest.counts_before or manifest.counts_after:
        counts = table_counts(executor, restored, tables)
        for table in sorted(expected_tables & set(tables)):
            low = manifest.counts_before.get(table, 0)
            high = manifest.counts_after.get(table, low)
            low, high = min(low, high), max(low, high)
            got = counts.get(table, 0)
            if not low <= got <= high:
                window = f"{low}" if low == high else f"{low}..{high}"
                failures.append(f"{table}: restored {got} rows, expected {window}")
        exact = sum(1 for t in expected_tables if manifest.counts_before.get(t) is not None)
        print(f"  {exact} tables compared, {sum(counts.values())} rows restored")

    # The TimescaleDB half. A restore that brings the rows back and loses the
    # partitioning is the failure this check exists for: nothing else notices.
    got_hypertables = hypertables(executor, restored)
    for missing in sorted(set(manifest.hypertables) - set(got_hypertables)):
        failures.append(f"{missing} came back as an ordinary table, not a hypertable")
    if manifest.hypertables:
        print(f"  hypertables restored: {', '.join(got_hypertables) or 'none'}")
    got_jobs = compression_jobs(executor, restored)
    if manifest.compression_jobs and not got_jobs:
        failures.append(
            f"{manifest.compression_jobs} compression policies were dumped and none "
            "came back — bars would grow uncompressed (ADR 0004)"
        )

    source_version = _alembic_version(executor, restored)
    if source_version:
        print(f"  schema at alembic revision {source_version}")
    return failures


def _alembic_version(executor: Executor, target: Target) -> str | None:
    if "alembic_version" not in user_tables(executor, target):
        return None
    return scalar(executor, target, "SELECT version_num FROM alembic_version LIMIT 1")


def cmd_restore(args: argparse.Namespace) -> int:
    """The real thing: put a dump back into a database the platform will use."""
    directory = backup_dir(args.dir)
    dump = Path(args.file) if args.file else newest_backup(directory)
    if not dump.exists():
        raise BackupError(f"no such backup: {dump}")

    mpath = manifest_path(dump)
    manifest = Manifest.load(mpath) if mpath.exists() else None
    if manifest is None:
        print(f"! {dump.name} has no manifest: restoring unchecked and unverifiable.")
    else:
        if sha256_of(dump) != manifest.sha256:
            raise BackupError(
                f"{dump.name} does not match its manifest's checksum. The file has "
                "changed since it was written; restoring it would put unknown data "
                "into the platform."
            )
        here = current_run_mode()
        if manifest.run_mode and here and manifest.run_mode != here and not args.force:
            raise BackupError(
                f"this dump was taken in run mode {manifest.run_mode!r} and this host "
                f"is {here!r}.\n"
                "  ADR 0011 puts paper and live on separate hosts, which makes their "
                "databases look interchangeable. They are not: fills and the audit "
                "trail of a real account cannot be reconstructed from anywhere, and "
                "restoring the other mode's dump over them destroys the only copy.\n"
                "  Pass --force if you have decided that is what you want."
            )

    target = resolve_target(args.dsn)
    executor = Executor.resolve(args.exec_mode)
    into = args.into
    admin = target.on("postgres")

    live = _client_connections_to(executor, admin, into)
    if live:
        raise BackupError(
            f"{live} other connection(s) are open to {into}. Stop the stack first "
            "(`make down`) — a restore into a database the platform is reading is "
            "neither safe nor possible."
        )

    if _database_exists(executor, admin, into):
        if not args.overwrite:
            raise BackupError(
                f"database {into} already exists. Pass --overwrite to replace it, or "
                "--into <newname> to restore alongside it and repoint DATABASE_URL "
                "afterwards — which is the reversible way round."
            )
        # Renamed, not dropped. An operator who restores the wrong dump, or the
        # right dump into the wrong database, has made a mistake that is
        # ordinary and that they will make at the worst possible moment. This
        # is what makes it survivable.
        aside = f"{into}_replaced_{SystemClock().now().strftime(STAMP)}"
        _detach_background_workers(executor, admin, into)
        execute(executor, admin, f'ALTER DATABASE "{into}" RENAME TO "{aside}"')
        print(f"  existing {into} renamed to {aside} — drop it once you are satisfied")

    execute(executor, admin, f'CREATE DATABASE "{into}"')
    restored = target.on(into)
    timescale = manifest.timescaledb_version is not None if manifest else True
    print(f"restoring {dump.name} into {restored} via {executor.describe()}")
    _restore_into(executor, restored, dump, timescale=timescale)

    tables = user_tables(executor, restored)
    counts = table_counts(executor, restored, tables)
    version = _alembic_version(executor, restored)
    restored_hypertables = hypertables(executor, restored)
    print(f"  {len(tables)} tables, {sum(counts.values())} rows")
    if restored_hypertables:
        print(f"  hypertables: {', '.join(restored_hypertables)}")
    if version:
        print(f"  alembic revision {version}")
    print(
        "\nNext: `make migrate` if this checkout is ahead of that revision, then "
        "start the stack and reconcile against the broker before clearing the halt "
        "(docs/RUNBOOK.md)."
    )
    return 0


def _prune(directory: Path, *, keep: int, dry_run: bool) -> int:
    """Keep the newest `keep` dumps per database and remove the rest."""
    if keep < 1:
        raise BackupError("--keep must be at least 1: pruning to nothing is not retention")
    by_database: dict[str, list[Path]] = {}
    for dump in find_backups(directory):
        parsed = parse_name(dump)
        by_database.setdefault(parsed[0] if parsed else "", []).append(dump)

    removed = 0
    for database, dumps in sorted(by_database.items()):
        for dump in dumps[keep:]:
            label = f"{dump.name} ({database or 'unrecognised name'})"
            if dry_run:
                print(f"  would remove {label}")
            else:
                dump.unlink(missing_ok=True)
                manifest_path(dump).unlink(missing_ok=True)
                print(f"  removed {label}")
            removed += 1
    if not removed:
        print(f"  nothing to prune (keeping {keep} per database)")
    return removed


def cmd_prune(args: argparse.Namespace) -> int:
    directory = backup_dir(args.dir)
    print(f"{directory}, keeping {args.keep} per database")
    _prune(directory, keep=args.keep, dry_run=args.dry_run)
    return 0


# ── cli ──────────────────────────────────────────────────────────────────────
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = parser.add_subparsers(dest="command", required=True)

    def common(p: argparse.ArgumentParser) -> None:
        p.add_argument(
            "--dir", help=f"where dumps live (default {DEFAULT_BACKUP_DIR}, or $ATP_BACKUP_DIR)"
        )
        p.add_argument(
            "--dsn",
            help="connect here instead of reading DATABASE_URL — the "
            "supported path on a host with no .env yet",
        )
        p.add_argument(
            "--exec",
            dest="exec_mode",
            choices=("auto", "local", "compose"),
            default="auto",
            help="run the client tools locally or inside the db container (default auto)",
        )

    create = sub.add_parser("create", help="take a backup")
    common(create)
    create.add_argument(
        "--no-counts",
        action="store_true",
        help="skip the row counts — faster, and gives up the comparison `verify` is built on",
    )
    create.add_argument("--prune", action="store_true", help="apply retention afterwards")
    create.add_argument("--keep", type=int, default=DEFAULT_KEEP)
    create.set_defaults(func=cmd_create)

    listing = sub.add_parser("list", help="what backups exist")
    common(listing)
    listing.add_argument("--check", action="store_true", help="re-hash each dump")
    listing.set_defaults(func=cmd_list)

    verify = sub.add_parser("verify", help="restore into a scratch database and compare")
    common(verify)
    verify.add_argument("--file", help="which dump (default: the newest)")
    verify.add_argument(
        "--keep", action="store_true", help="leave the scratch database behind to look at"
    )
    verify.set_defaults(func=cmd_verify)

    restore = sub.add_parser("restore", help="restore into a real database")
    common(restore)
    restore.add_argument("--file", help="which dump (default: the newest)")
    restore.add_argument(
        "--into",
        required=True,
        help="database to restore into — named explicitly, never "
        "defaulted, because this one is destructive",
    )
    restore.add_argument(
        "--overwrite",
        action="store_true",
        help="replace --into if it exists (it is renamed aside, not dropped)",
    )
    restore.add_argument(
        "--force",
        action="store_true",
        help="restore across run modes (paper dump onto live, or back)",
    )
    restore.set_defaults(func=cmd_restore)

    prune = sub.add_parser("prune", help="apply retention")
    common(prune)
    prune.add_argument("--keep", type=int, default=DEFAULT_KEEP)
    prune.add_argument("--dry-run", action="store_true")
    prune.set_defaults(func=cmd_prune)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        result: int = args.func(args)
    except BackupError as exc:
        print(f"\nERROR: {exc}", file=sys.stderr)
        return EXIT_UNAVAILABLE
    return result


if __name__ == "__main__":
    raise SystemExit(main())
