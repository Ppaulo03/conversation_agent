"""Migration tooling (Phase 10): checksummed, serialized, forward-only, with a startup guard."""

from __future__ import annotations

import asyncio

import asyncpg
import pytest

from conversation_agent.adapters.postgres.db import PostgresDatabase
from conversation_agent.adapters.postgres.migrator import (
    MigrationDriftError,
    Migrator,
    SchemaAheadError,
    SchemaOutOfDateError,
    checksum,
)
from conversation_agent.app.migrate import main as migrate_cli


@pytest.fixture
async def spare(pg_dsn: str):  # type: ignore[no-untyped-def]
    """An empty database of its own: migrations are tested from scratch, never on the shared one."""
    admin = await asyncpg.connect(pg_dsn.rsplit("/", 1)[0] + "/postgres")
    name = "conversation_agent_migrator_test"
    await admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
    await admin.execute(f'CREATE DATABASE "{name}"')
    dsn = pg_dsn.rsplit("/", 1)[0] + "/" + name
    db = await PostgresDatabase.connect(dsn, max_size=4)
    yield db, dsn
    await db.close()
    await admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
    await admin.close()


async def test_a_fresh_database_is_migrated_in_order_and_then_is_current(spare) -> None:  # type: ignore[no-untyped-def]
    db, _ = spare
    migrator = Migrator(db.pool)
    before = await migrator.status()
    assert before and all(s.state == "pending" for s in before)
    assert await migrator.apply(dry_run=True) == [s.version for s in before]  # nothing happened
    assert all(s.state == "pending" for s in await migrator.status())
    applied = await migrator.apply()
    assert applied == [s.version for s in before]
    assert all(s.state == "applied" for s in await migrator.status())
    assert await migrator.apply() == []  # idempotent
    await migrator.ensure_current()


async def test_every_applied_migration_records_the_hash_of_its_file(spare) -> None:  # type: ignore[no-untyped-def]
    db, _ = spare
    await Migrator(db.pool).apply()
    rows = await db.pool.fetch("SELECT version, checksum FROM schema_migrations")
    assert rows and all(r["checksum"] and len(r["checksum"]) == 64 for r in rows)
    assert checksum("a\r\nb") == checksum("a\nb")  # line endings do not change a migration


async def test_an_applied_migration_that_was_edited_stops_everything(spare) -> None:  # type: ignore[no-untyped-def]
    db, _ = spare
    migrator = Migrator(db.pool)
    await migrator.apply()
    await db.pool.execute(
        "UPDATE schema_migrations SET checksum = 'edited-afterwards' WHERE version = '0001_init'"
    )
    states = {s.version: s.state for s in await migrator.status()}
    assert states["0001_init"] == "changed"
    with pytest.raises(MigrationDriftError, match="0001_init"):
        await migrator.apply()
    with pytest.raises(MigrationDriftError):
        await migrator.ensure_current()


async def test_a_database_ahead_of_this_build_is_refused(spare) -> None:  # type: ignore[no-untyped-def]
    db, _ = spare
    migrator = Migrator(db.pool)
    await migrator.apply()
    await db.pool.execute(
        "INSERT INTO schema_migrations (version, checksum) VALUES ('9999_from_the_future', 'x')"
    )
    assert {s.version: s.state for s in await migrator.status()}[
        "9999_from_the_future"
    ] == "unknown"
    with pytest.raises(SchemaAheadError, match="9999_from_the_future"):
        await migrator.apply()
    with pytest.raises(SchemaAheadError):
        await migrator.ensure_current()  # old code never runs on a schema it does not know


async def test_the_startup_guard_refuses_a_schema_that_is_behind(spare) -> None:  # type: ignore[no-untyped-def]
    db, _ = spare
    with pytest.raises(SchemaOutOfDateError, match="0001_init"):
        await Migrator(db.pool).ensure_current()


async def test_rows_from_before_checksums_are_trusted_once_and_stamped(spare) -> None:  # type: ignore[no-untyped-def]
    db, _ = spare
    migrator = Migrator(db.pool)
    await migrator.apply()
    await db.pool.execute("UPDATE schema_migrations SET checksum = NULL")  # the old format
    assert all(s.state == "applied" for s in await migrator.status())
    await migrator.apply()
    assert (
        await db.pool.fetchval("SELECT count(*) FROM schema_migrations WHERE checksum IS NULL") == 0
    )


async def test_two_migrators_booting_together_apply_each_migration_once(spare) -> None:  # type: ignore[no-untyped-def]
    db, _ = spare
    results = await asyncio.gather(*(Migrator(db.pool).apply() for _ in range(3)))
    assert sum(len(r) for r in results) == len(await Migrator(db.pool).status())  # no repeats
    assert await db.pool.fetchval("SELECT count(*) FROM schema_migrations") == sum(
        len(r) for r in results
    )


async def test_a_failing_migration_leaves_no_trace_and_nothing_after_it_runs(
    spare, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    db, _ = spare
    from conversation_agent.adapters.postgres import migrator as module

    real = module._files
    monkeypatch.setattr(
        module,
        "_files",
        lambda: {**real(), "9000_broken": "CREATE TABLE half_done (x int); SELECT 1/0;"},
    )
    migrator = Migrator(db.pool)
    with pytest.raises(asyncpg.PostgresError):
        await migrator.apply()
    assert await db.pool.fetchval("SELECT to_regclass('half_done')") is None  # rolled back
    done = {r["version"] for r in await db.pool.fetch("SELECT version FROM schema_migrations")}
    assert "9000_broken" not in done and "0001_init" in done  # earlier ones stay applied


async def test_cli_commands(
    spare, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:  # type: ignore[no-untyped-def]
    _, dsn = spare
    monkeypatch.setenv("DATABASE_URL", dsn)
    loop = asyncio.get_running_loop()

    async def cli(*args: str) -> int:
        return await loop.run_in_executor(None, migrate_cli, list(args))

    assert await cli("check") == 1  # nothing applied yet
    assert "SchemaOutOfDateError" in capsys.readouterr().err
    assert await cli("apply", "--dry-run") == 0
    assert "would apply: 0001_init" in capsys.readouterr().out
    assert await cli("apply") == 0 and await cli("check") == 0
    assert await cli("status") == 0
    assert await cli("bogus") == 2
    monkeypatch.delenv("DATABASE_URL")
    assert await cli("status") == 2
