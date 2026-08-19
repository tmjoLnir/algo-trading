"""`scripts/backup_db.py` against a real database — the tested restore.

This is the file `docs/ROADMAP.md`'s "Backups and a tested restore" is about.
Everything in `tests/unit/test_backup_script.py` checks what the tool *would*
run; nothing there can tell you whether the bytes it wrote bring the platform
back, and that is the only question a backup exists to answer.

Two of these could not be unit tests even in principle:

`test_the_hypertable_survives_the_round_trip` is the TimescaleDB half. `bars`
keeps its rows in chunks under `_timescaledb_internal` and its shape in the
extension's own catalog. A restore that skips `timescaledb_pre_restore()`
either errors on the extension's constraints or lands a catalog that disagrees
with its chunks — and the failure is silent, because every query still works
until the table is large enough that fixing it means downtime (ADR 0004).

`test_verify_fails_when_the_restore_does_not_match` is the check on the check.
A comparison that cannot fail is decoration, and a verification step that always
passes is worse than none: it converts "we have never tested a restore" into
"we test the restore nightly", which is the belief this item exists to replace.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING

import asyncpg
import pytest

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "backup_db.py"


def asyncpg_dsn(url: str) -> str:
    """asyncpg wants a bare postgres:// DSN, not SQLAlchemy's driver form.

    Defined here rather than imported from `conftest.py`, matching
    `test_audit_log.py` — pytest loads a conftest as a plugin, and importing one
    as a module as well is a second, differently-resolved copy of it.
    """
    return url.replace("postgresql+asyncpg://", "postgresql://")


def database_in(dsn: str, name: str) -> str:
    """The same server, a different database."""
    return dsn.rsplit("/", 1)[0] + "/" + name


def run_backup(*args: str, dsn: str, directory: Path) -> subprocess.CompletedProcess[str]:
    """Run the script as an operator runs it — as a process, through its CLI.

    Deliberately not by importing and calling `cmd_verify`. What is being tested
    includes the exit code, which is what a cron line and a runbook step read,
    and which an in-process call would step over.
    """
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args, "--dsn", dsn, "--dir", str(directory)],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        check=False,
    )


@pytest.fixture
async def populated_db(migrated_db: str) -> AsyncIterator[str]:
    """A migrated database with a row in the tables that cannot be re-fetched.

    `bars` can be rebuilt from the provider; `orders`, `fills` and `audit_log`
    cannot be rebuilt from anywhere, which is the asymmetry ADR 0014 turns on.
    Both kinds are populated so the round trip covers a hypertable and an
    ordinary table together.
    """
    conn = await asyncpg.connect(asyncpg_dsn(migrated_db))
    try:
        await conn.execute(
            "INSERT INTO bars (symbol, timeframe, ts, open, high, low, close, volume) "
            "SELECT 'BKP', '1d', '2024-01-01T00:00:00Z'::timestamptz + (n || ' days')::interval,"
            " 100, 101, 99, 100.125, 1000 FROM generate_series(1, 50) n "
            "ON CONFLICT DO NOTHING"
        )
        await conn.execute(
            "INSERT INTO orders (id, client_order_id, symbol, side, order_type, "
            "time_in_force, qty, status, filled_qty, run_mode, created_at) "
            "VALUES ('bkp-o', 'atp-bkp', 'BKP', 'buy', 'market', 'day', 10, "
            "'filled', 10, 'paper', now()) ON CONFLICT DO NOTHING"
        )
        await conn.execute(
            "INSERT INTO fills (id, order_id, qty, price, fee, ts) "
            "VALUES ('bkp-f', 'bkp-o', 10, 100.125, 0.01, now()) ON CONFLICT DO NOTHING"
        )
        yield migrated_db
        await conn.execute("DELETE FROM fills WHERE id = 'bkp-f'")
        await conn.execute("DELETE FROM orders WHERE id = 'bkp-o'")
        await conn.execute("DELETE FROM bars WHERE symbol = 'BKP'")
    finally:
        await conn.close()


@pytest.fixture
async def dsn(populated_db: str) -> str:
    return asyncpg_dsn(populated_db)


class TestRoundTrip:
    async def test_create_then_verify_restores_and_matches(self, dsn: str, tmp_path: Path) -> None:
        """The item, in one test: take a backup, put it back, compare.

        `verify` restores into a scratch database of its own and drops it again,
        so this asserts a real `pg_restore` of a real dump without touching the
        database it came from.
        """
        created = run_backup("create", dsn=dsn, directory=tmp_path)
        assert created.returncode == 0, created.stderr
        dumps = list(tmp_path.glob("*.dump"))
        assert len(dumps) == 1, f"expected one dump, got {[d.name for d in dumps]}"
        assert dumps[0].stat().st_size > 0

        verified = run_backup("verify", dsn=dsn, directory=tmp_path)
        assert verified.returncode == 0, f"{verified.stdout}\n{verified.stderr}"
        assert "Restored and matched" in verified.stdout

    async def test_the_hypertable_survives_the_round_trip(self, dsn: str, tmp_path: Path) -> None:
        """`bars` must come back partitioned, not as an ordinary table.

        This is what the `timescaledb_pre_restore()` / `post_restore()` bracket
        buys, and the reason the backup is a script rather than a cron line.
        `--keep` leaves the scratch database in place so the assertion is made
        against the restored catalog rather than against the tool's own report.
        """
        assert run_backup("create", dsn=dsn, directory=tmp_path).returncode == 0
        verified = run_backup("verify", "--keep", dsn=dsn, directory=tmp_path)
        assert verified.returncode == 0, f"{verified.stdout}\n{verified.stderr}"

        scratch = next(
            line.split()[-1] for line in verified.stdout.splitlines() if "restoring into" in line
        )
        admin = await asyncpg.connect(asyncpg_dsn(dsn))
        try:
            restored = await asyncpg.connect(database_in(dsn, scratch))
            try:
                assert await restored.fetchval(
                    "SELECT count(*) FROM timescaledb_information.hypertables "
                    "WHERE hypertable_name = 'bars'"
                ), "bars came back as an ordinary table — the storage design is gone"
                assert (
                    await restored.fetchval("SELECT count(*) FROM bars WHERE symbol = 'BKP'")
                ) == 50
                assert (
                    await restored.fetchval("SELECT price FROM fills WHERE id = 'bkp-f'")
                ) is not None
                assert await restored.fetchval("SELECT count(*) FROM alembic_version") == 1
            finally:
                await restored.close()
        finally:
            await admin.execute(f'DROP DATABASE IF EXISTS "{scratch}" WITH (FORCE)')
            await admin.close()

    async def test_compression_policy_comes_back(self, dsn: str, tmp_path: Path) -> None:
        """A restore that drops the compression policy leaves `bars` growing
        uncompressed — 10-20x on OHLCV, and nothing fails until the disk does."""
        assert run_backup("create", dsn=dsn, directory=tmp_path).returncode == 0
        manifest = next(tmp_path.glob("*.dump.json")).read_text()
        assert '"compression_jobs": 1' in manifest, "the source has no policy to lose"
        verified = run_backup("verify", dsn=dsn, directory=tmp_path)
        assert verified.returncode == 0, f"{verified.stdout}\n{verified.stderr}"
        assert "compression policies" not in verified.stdout


class TestVerificationActuallyChecks:
    async def test_a_tampered_dump_is_refused_before_it_is_restored(
        self, dsn: str, tmp_path: Path
    ) -> None:
        assert run_backup("create", dsn=dsn, directory=tmp_path).returncode == 0
        dump = next(tmp_path.glob("*.dump"))
        corrupted = bytearray(dump.read_bytes())
        corrupted[len(corrupted) // 2] ^= 0xFF
        dump.write_bytes(bytes(corrupted))

        verified = run_backup("verify", dsn=dsn, directory=tmp_path)
        assert verified.returncode == 1
        assert "sha256" in verified.stdout
        assert "Do not restore it" in verified.stdout

    async def test_verify_fails_when_the_restore_does_not_match(
        self, dsn: str, tmp_path: Path
    ) -> None:
        """The check on the check.

        The manifest is edited to claim more rows than the dump holds, which is
        what a partial dump would look like. `verify` must restore it, notice,
        and exit non-zero — a comparison that cannot fail proves nothing about
        the ones that pass.
        """
        assert run_backup("create", dsn=dsn, directory=tmp_path).returncode == 0
        manifest = next(tmp_path.glob("*.dump.json"))
        data = json.loads(manifest.read_text())
        data["counts_before"]["bars"] = data["counts_after"]["bars"] = 10_000
        # Re-hashed, so the checksum gate passes and the comparison is what
        # fails — otherwise this would only re-test the previous case.
        data["sha256"] = hashlib.sha256(next(tmp_path.glob("*.dump")).read_bytes()).hexdigest()
        manifest.write_text(json.dumps(data))

        verified = run_backup("verify", dsn=dsn, directory=tmp_path)
        assert verified.returncode == 1
        assert "RESTORE DID NOT MATCH" in verified.stdout
        assert "bars" in verified.stdout

    async def test_a_successful_dump_leaves_no_partial_file(self, dsn: str, tmp_path: Path) -> None:
        """`create` writes `.part` and renames only once the dump has finished
        and been hashed, so an interrupted one cannot become the newest good
        backup — the one `verify` and `restore` reach for by default.

        This end asserts the rename happened; that a `.part` is never *offered*
        as a backup is `tests/unit/test_backup_script.py`.
        """
        assert run_backup("create", dsn=dsn, directory=tmp_path).returncode == 0
        assert not list(tmp_path.glob("*.part"))
        assert len(list(tmp_path.glob("*.dump"))) == 1


class TestRestoreGuards:
    async def test_restore_into_a_new_database_brings_the_rows(
        self, dsn: str, tmp_path: Path
    ) -> None:
        assert run_backup("create", dsn=dsn, directory=tmp_path).returncode == 0
        into = "atp_restore_test_target"
        admin = await asyncpg.connect(asyncpg_dsn(dsn))
        try:
            await admin.execute(f'DROP DATABASE IF EXISTS "{into}" WITH (FORCE)')
            restored = run_backup("restore", "--into", into, "--force", dsn=dsn, directory=tmp_path)
            assert restored.returncode == 0, f"{restored.stdout}\n{restored.stderr}"

            conn = await asyncpg.connect(database_in(dsn, into))
            try:
                assert await conn.fetchval("SELECT count(*) FROM bars WHERE symbol = 'BKP'") == 50
            finally:
                await conn.close()
        finally:
            await admin.execute(f'DROP DATABASE IF EXISTS "{into}" WITH (FORCE)')
            await admin.close()

    async def test_refuses_a_database_something_else_is_connected_to(
        self, dsn: str, tmp_path: Path
    ) -> None:
        """A restore into a database the platform is reading is neither safe nor
        possible. The fixture's own connection is the other connection."""
        assert run_backup("create", dsn=dsn, directory=tmp_path).returncode == 0
        holding = await asyncpg.connect(asyncpg_dsn(dsn))
        try:
            target = asyncpg_dsn(dsn).rsplit("/", 1)[-1]
            refused = run_backup(
                "restore", "--into", target, "--force", dsn=dsn, directory=tmp_path
            )
            assert refused.returncode == 2
            assert "connection" in refused.stderr
            # Refusing is half the property. This is the database the suite
            # itself runs against, so the other half is that it is still here
            # and still has its rows.
            assert await holding.fetchval("SELECT count(*) FROM bars WHERE symbol = 'BKP'") == 50
        finally:
            await holding.close()

    async def test_refuses_an_existing_database_without_overwrite(
        self, dsn: str, tmp_path: Path
    ) -> None:
        assert run_backup("create", dsn=dsn, directory=tmp_path).returncode == 0
        into = "atp_restore_test_existing"
        admin = await asyncpg.connect(asyncpg_dsn(dsn))
        try:
            await admin.execute(f'DROP DATABASE IF EXISTS "{into}" WITH (FORCE)')
            await admin.execute(f'CREATE DATABASE "{into}"')
            refused = run_backup("restore", "--into", into, "--force", dsn=dsn, directory=tmp_path)
            assert refused.returncode == 2
            assert "--overwrite" in refused.stderr
        finally:
            await admin.execute(f'DROP DATABASE IF EXISTS "{into}" WITH (FORCE)')
            await admin.close()
