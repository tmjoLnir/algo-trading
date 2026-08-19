"""`scripts/backup_db.py`, as far as it can be tested without a database.

The round trip that actually matters — dump a real PostgreSQL, restore it, and
compare — is `tests/integration/test_backup_restore.py`, because nothing else
can answer it. What is testable here is the part that decides *what gets run*
and *what gets refused*: the command lines, the credential handling, the
ordering of backups, and the retention arithmetic.

Two of these are worth more than they look. `test_compose_argv_disables_the_tty`
pins the one flag whose absence produces dumps that are silently unrestorable,
and `test_password_never_appears_in_argv` pins the one that puts a database
credential on every `ps` on the host (CLAUDE.md §1.6).
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import Any
from urllib.parse import quote

import pytest

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"


def _load(name: str) -> Any:
    """Import a script by path — `scripts/` is a set of entry points, not a
    package. Same approach as `test_secrets_script.py`, for the same reason."""
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


backup = _load("backup_db")

#: A password with characters that are syntax in a URL. It is percent-encoded
#: on the way in — as any real one would be — and must come back out decoded,
#: and must never appear in a command line or a printed string.
SECRET = "s3cr#t/pass@word"
SECRET_IN_URL = quote(SECRET, safe="")


class TestTarget:
    def test_strips_the_sqlalchemy_driver(self) -> None:
        """`Settings.database_url` carries `+asyncpg`, which libpq does not know."""
        target = backup.Target.from_url("postgresql+asyncpg://atp:pw@db:5432/atp")
        assert (target.host, target.port, target.user, target.database) == (
            "db",
            5432,
            "atp",
            "atp",
        )

    def test_decodes_a_percent_encoded_password(self) -> None:
        """A password containing `@` or `/` is encoded in the URL and must reach
        libpq decoded — otherwise authentication fails with a message that
        blames the credential rather than the parsing."""
        target = backup.Target.from_url("postgresql://atp:p%40ss%2Fword@db:5432/atp")
        assert target.password == "p@ss/word"

    def test_defaults_the_port(self) -> None:
        assert backup.Target.from_url("postgresql://atp@db/atp").port == 5432

    def test_rejects_a_url_that_is_not_postgres(self) -> None:
        with pytest.raises(backup.BackupError, match="not a PostgreSQL URL"):
            backup.Target.from_url("mysql://atp:pw@db:3306/atp")

    def test_rejects_a_url_with_no_database(self) -> None:
        with pytest.raises(backup.BackupError, match="no database name"):
            backup.Target.from_url("postgresql://atp:pw@db:5432/")

    def test_str_never_leaks_the_password(self) -> None:
        """`Target` is put into printed lines and into every error this script
        raises. It has exactly one `__str__` and this is the reason for it."""
        target = backup.Target.from_url(f"postgresql://atp:{SECRET_IN_URL}@db:5432/atp")
        assert SECRET not in str(target)
        assert str(target) == "atp@db:5432/atp"

    def test_on_keeps_the_server_and_changes_the_database(self) -> None:
        target = backup.Target.from_url("postgresql://atp:pw@db:5432/atp")
        assert target.on("postgres") == backup.Target("db", 5432, "atp", "postgres", "pw")


class TestExecutorCommandLines:
    def test_local_argv_carries_the_connection_and_no_prompt(self) -> None:
        executor = backup.Executor("local")
        target = backup.Target.from_url("postgresql://atp:pw@db:5432/atp")
        argv = executor.argv("pg_dump", target, ["-F", "c"])
        assert argv == ["pg_dump", "-w", "-h", "db", "-p", "5432", "-U", "atp",
                        "-d", "atp", "-F", "c"]  # fmt: skip

    def test_password_never_appears_in_argv(self) -> None:
        """`/proc/<pid>/cmdline` is world-readable and a dump of the largest
        table in the platform is the longest window a leak could pick. The
        credential goes in the environment, where only the owning user can read
        it."""
        executor = backup.Executor("local")
        target = backup.Target.from_url(f"postgresql://atp:{SECRET_IN_URL}@db:5432/atp")
        assert not any(SECRET in part for part in executor.argv("pg_dump", target, []))
        assert executor.env(target)["PGPASSWORD"] == SECRET

    def test_compose_argv_disables_the_tty(self) -> None:
        """`docker compose exec` allocates a pseudo-TTY by default, which
        rewrites newlines in the binary stream. Without `-T` every dump this
        tool takes is corrupt, and nothing says so until a restore."""
        executor = backup.Executor("compose")
        target = backup.Target.from_url("postgresql://atp:pw@db:5432/atp")
        argv = executor.argv("pg_dump", target, ["-F", "c"])
        assert argv[:5] == ["docker", "compose", "exec", "-T", "db"]
        assert "-h" not in argv, "in-container connections use the local socket"

    def test_compose_passes_no_password(self) -> None:
        """The image trusts local-socket connections, so there is no credential
        to hand over — and `docker compose exec -e` would have put it in argv."""
        executor = backup.Executor("compose")
        target = backup.Target.from_url(f"postgresql://atp:{SECRET_IN_URL}@db:5432/atp")
        assert "PGPASSWORD" not in executor.env(target)

    def test_an_inherited_pgpassword_is_not_forwarded(self, monkeypatch: Any) -> None:
        monkeypatch.setenv("PGPASSWORD", "left-over-from-the-shell")
        executor = backup.Executor("compose")
        target = backup.Target.from_url("postgresql://atp@db:5432/atp")
        assert "PGPASSWORD" not in executor.env(target)

    def test_version_probe_drops_every_connection_flag(self) -> None:
        """`pg_dump --version` given a `-d` as well exits 1 with a bare
        "Try --help" — a version check written the obvious way fails and blames
        the database. Found by running it."""
        executor = backup.Executor("local")
        target = backup.Target.from_url("postgresql://atp:pw@db:5432/atp")
        argv = executor.argv("pg_dump", target, ["--version"], connect=False)
        assert argv == ["pg_dump", "--version"]

    def test_auto_falls_back_to_compose_without_local_tools(self, monkeypatch: Any) -> None:
        monkeypatch.setattr(backup.shutil, "which", lambda _: None)
        assert backup.Executor.resolve("auto").mode == "compose"
        monkeypatch.setattr(backup.shutil, "which", lambda _: "/usr/bin/pg_dump")
        assert backup.Executor.resolve("auto").mode == "local"


class TestVersionParsing:
    def test_reads_past_the_packaging_suffix(self) -> None:
        """The last token of a `--version` banner is the distribution's build,
        not the version — taking it stores `16.13-0ubuntu0.24.04.1)`."""
        banner = "pg_dump (PostgreSQL) 16.13 (Ubuntu 16.13-0ubuntu0.24.04.1)"
        assert backup._version_in(banner) == "16.13"
        assert backup._major(banner) == 16

    def test_handles_a_bare_server_version(self) -> None:
        assert backup._version_in("17.2") == "17.2"
        assert backup._major("17.2") == 17

    def test_unrecognisable_version_disables_the_check(self) -> None:
        """`_major` returning 0 makes `_require_client_version` skip the
        comparison rather than refuse to back anything up."""
        assert backup._major("who knows") == 0


class TestBackupNames:
    def test_splits_a_generated_name(self) -> None:
        parsed = backup.parse_name(Path("atp-atp_paper-20260819T052226Z.dump"))
        assert parsed == ("atp_paper", "20260819T052226Z")

    def test_returns_none_for_a_file_this_tool_did_not_write(self) -> None:
        assert backup.parse_name(Path("handmade-backup.dump")) is None
        assert backup.parse_name(Path("nightly.dump")) is None

    def test_orders_by_stamp_not_by_database(self, tmp_path: Path) -> None:
        """Sorting by the whole filename groups by database first, which hands
        `verify` and `restore` the newest backup *of the wrong database*."""
        for name in (
            "atp-aaa-20260101T000000Z.dump",
            "atp-zzz-20250101T000000Z.dump",
        ):
            (tmp_path / name).write_bytes(b"x")
        assert backup.find_backups(tmp_path)[0].name == "atp-aaa-20260101T000000Z.dump"

    def test_an_unfinished_dump_is_not_offered_as_a_backup(self, tmp_path: Path) -> None:
        """`create` writes `.part` and renames after hashing. A dump interrupted
        by a reboot or a full disk must not be able to look like the newest good
        backup, which is what `verify` and `restore` reach for by default."""
        (tmp_path / "atp-atp-20260819T000000Z.part").write_bytes(b"half a dump")
        (tmp_path / "atp-atp-20260818T000000Z.dump").write_bytes(b"a whole one")
        assert [p.name for p in backup.find_backups(tmp_path)] == ["atp-atp-20260818T000000Z.dump"]

    def test_missing_directory_is_empty_not_an_error(self, tmp_path: Path) -> None:
        assert backup.find_backups(tmp_path / "nope") == []

    def test_newest_backup_says_what_to_do_when_there_are_none(self, tmp_path: Path) -> None:
        with pytest.raises(backup.BackupError, match="no backups"):
            backup.newest_backup(tmp_path)


class TestRetention:
    def _spread(self, directory: Path, database: str, days: list[int]) -> None:
        for day in days:
            dump = directory / f"atp-{database}-202608{day:02d}T000000Z.dump"
            dump.write_bytes(b"x")
            backup.manifest_path(dump).write_text("{}")

    def test_keeps_the_newest_per_database(self, tmp_path: Path) -> None:
        """Retention is per database, not per directory. One shared directory
        holding paper and live dumps must not let a busy one evict the other."""
        self._spread(tmp_path, "atp_paper", [11, 12, 13, 14])
        self._spread(tmp_path, "atp_live", [11, 12])
        backup._prune(tmp_path, keep=2, dry_run=False)
        left = sorted(p.name for p in tmp_path.glob("*.dump"))
        assert left == [
            "atp-atp_live-20260811T000000Z.dump",
            "atp-atp_live-20260812T000000Z.dump",
            "atp-atp_paper-20260813T000000Z.dump",
            "atp-atp_paper-20260814T000000Z.dump",
        ]

    def test_removes_the_manifest_with_the_dump(self, tmp_path: Path) -> None:
        self._spread(tmp_path, "atp", [11, 12])
        backup._prune(tmp_path, keep=1, dry_run=False)
        assert not (tmp_path / "atp-atp-20260811T000000Z.dump.json").exists()
        assert (tmp_path / "atp-atp-20260812T000000Z.dump.json").exists()

    def test_dry_run_deletes_nothing(self, tmp_path: Path) -> None:
        self._spread(tmp_path, "atp", [11, 12, 13])
        assert backup._prune(tmp_path, keep=1, dry_run=True) == 2
        assert len(list(tmp_path.glob("*.dump"))) == 3

    def test_refuses_to_prune_to_nothing(self, tmp_path: Path) -> None:
        with pytest.raises(backup.BackupError, match="at least 1"):
            backup._prune(tmp_path, keep=0, dry_run=False)

    def test_an_unrecognised_name_is_grouped_on_its_own(self, tmp_path: Path) -> None:
        """A dump this tool did not name has unknown provenance. It is not
        counted against another database's retention, and so is not deleted to
        make room for one."""
        self._spread(tmp_path, "atp", [11, 12, 13])
        (tmp_path / "handmade.dump").write_bytes(b"x")
        backup._prune(tmp_path, keep=1, dry_run=False)
        assert (tmp_path / "handmade.dump").exists()


class TestManifest:
    def _manifest(self, **overrides: Any) -> Any:
        fields = {
            "file": "atp-atp-20260819T000000Z.dump",
            "sha256": "abc",
            "size_bytes": 10,
            "created_at": "2026-08-19T00:00:00+00:00",
            "database": "atp",
            "server_version": "16.13",
            "pg_dump_version": "16.13",
            "timescaledb_version": "2.15.2",
            "hypertables": ["bars"],
            "compression_jobs": 1,
            "run_mode": "paper",
            "counts_before": {"bars": 10},
            "counts_after": {"bars": 12},
        }
        return backup.Manifest(**{**fields, **overrides})

    def test_round_trips_through_json(self, tmp_path: Path) -> None:
        path = tmp_path / "m.json"
        path.write_text(self._manifest().to_json())
        assert backup.Manifest.load(path) == self._manifest()

    def test_absent_timescale_and_run_mode_are_facts_not_errors(self, tmp_path: Path) -> None:
        """A database with no TimescaleDB, or a dump taken where no `.env` said
        which run mode it belonged to. Both are legitimate."""
        path = tmp_path / "m.json"
        path.write_text(self._manifest(timescaledb_version=None, run_mode=None).to_json())
        loaded = backup.Manifest.load(path)
        assert loaded.timescaledb_version is None
        assert loaded.run_mode is None

    def test_a_truncated_manifest_names_what_is_missing(self, tmp_path: Path) -> None:
        path = tmp_path / "m.json"
        path.write_text(json.dumps({"file": "x.dump"}))
        with pytest.raises(backup.BackupError, match="sha256"):
            backup.Manifest.load(path)

    def test_unreadable_manifest_is_reported_not_raised_raw(self, tmp_path: Path) -> None:
        path = tmp_path / "m.json"
        path.write_text("{not json")
        with pytest.raises(backup.BackupError, match="cannot read the manifest"):
            backup.Manifest.load(path)

    def test_manifest_sits_beside_the_dump(self) -> None:
        assert backup.manifest_path(Path("/b/atp-atp-1.dump")).name == "atp-atp-1.dump.json"


class TestBackupDirectory:
    def test_environment_overrides_the_default(self, monkeypatch: Any, tmp_path: Path) -> None:
        monkeypatch.setenv("ATP_BACKUP_DIR", str(tmp_path))
        assert backup.backup_dir(None) == tmp_path

    def test_an_explicit_directory_wins(self, monkeypatch: Any, tmp_path: Path) -> None:
        monkeypatch.setenv("ATP_BACKUP_DIR", "/from/env")
        assert backup.backup_dir(str(tmp_path)) == tmp_path


class TestCliSurface:
    def test_restore_requires_an_explicit_target(self) -> None:
        """`--into` is never defaulted. This is the one destructive command and
        the operator names the database they mean."""
        with pytest.raises(SystemExit):
            backup.build_parser().parse_args(["restore"])

    def test_restore_accepts_a_named_target(self) -> None:
        args = backup.build_parser().parse_args(["restore", "--into", "atp"])
        assert args.into == "atp" and args.overwrite is False and args.force is False

    def test_create_defaults_to_counting_rows(self) -> None:
        """The counts are what `verify` compares against; a backup taken
        without them can be restored but not checked."""
        assert backup.build_parser().parse_args(["create"]).no_counts is False

    def test_a_backup_error_exits_unavailable_not_unverified(self, capsys: Any) -> None:
        """The two exit codes mean opposite things: 2 is "could not check",
        1 is "checked, and it does not restore"."""
        code = backup.main(["verify", "--dir", "/nonexistent", "--file", "/nope.dump"])
        assert code == backup.EXIT_UNAVAILABLE
