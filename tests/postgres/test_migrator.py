"""Migration tooling (Phase 10): checksummed, serialized, forward-only, with a startup guard."""

from __future__ import annotations

import asyncio

import asyncpg
import httpx
import pytest

from conversation_agent.adapters.observability.asgi import ops_app
from conversation_agent.adapters.postgres.coordination import CoordinationTime
from conversation_agent.adapters.postgres.db import PostgresDatabase
from conversation_agent.adapters.postgres.inbox import PostgresInboxStore
from conversation_agent.adapters.postgres.integrity import verify_integrity
from conversation_agent.adapters.postgres.migrator import (
    MigrationDriftError,
    MigrationHistoryError,
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
    db, dsn = spare
    monkeypatch.setenv("DATABASE_URL", dsn)
    loop = asyncio.get_running_loop()

    async def cli(*args: str) -> int:
        return await loop.run_in_executor(None, migrate_cli, list(args))

    assert await cli("check") == 1  # nothing applied yet
    assert "SchemaOutOfDateError" in capsys.readouterr().err
    assert await cli("status") == 1 and "pending" in capsys.readouterr().out
    assert await _has_metadata(db) is False  # `check` and `status` create nothing
    assert await cli("apply", "--dry-run") == 0
    assert "would apply: 0001_init" in capsys.readouterr().out
    assert await cli("apply") == 0 and await cli("check") == 0
    assert await cli("status") == 0
    assert await cli("bogus") == 2
    monkeypatch.delenv("DATABASE_URL")
    assert await cli("status") == 2


# --- F-014 / ADR-026: verifying is read-only; only `apply` creates or changes anything ---


async def _has_metadata(db: PostgresDatabase) -> bool:
    return await db.pool.fetchval("SELECT to_regclass('schema_migrations')") is not None


async def _snapshot(db: PostgresDatabase) -> object:
    """Everything the verifier could have touched: the rows, the columns, the relations."""
    rows = await db.pool.fetch("SELECT * FROM schema_migrations ORDER BY version")
    columns = await db.pool.fetch(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_name = 'schema_migrations' ORDER BY 1"
    )
    relations = await db.pool.fetchval("SELECT count(*) FROM pg_class")
    return [dict(r) for r in rows], [r[0] for r in columns], relations


async def test_verifying_a_never_migrated_database_fails_and_creates_nothing(spare) -> None:  # type: ignore[no-untyped-def]
    db, _ = spare
    relations = await db.pool.fetchval("SELECT count(*) FROM pg_class")
    with pytest.raises(SchemaOutOfDateError, match="0001_init"):
        await db.ensure_schema_current()
    with pytest.raises(SchemaOutOfDateError):
        await Migrator(db.pool).ensure_current()
    statuses = await Migrator(db.pool).status()
    assert statuses and all(s.state == "pending" for s in statuses)
    assert not await _has_metadata(db)
    assert await db.pool.fetchval("SELECT count(*) FROM pg_class") == relations


async def test_an_explicit_migration_is_what_makes_the_schema_verifiable(spare) -> None:  # type: ignore[no-untyped-def]
    db, _ = spare
    await db.migrate()
    assert await db.ensure_schema_current() is None
    assert await db.ensure_schema_current() is None  # safe on every start


async def test_a_partly_migrated_database_is_behind_and_the_verifier_applies_nothing(  # type: ignore[no-untyped-def]
    spare, monkeypatch
) -> None:
    from conversation_agent.adapters.postgres import migrator as module

    db, _ = spare
    real = module._files
    monkeypatch.setattr(module, "_files", lambda: dict(list(real().items())[:2]))
    assert len(await Migrator(db.pool).apply()) == 2
    monkeypatch.setattr(module, "_files", real)
    before = await _snapshot(db)
    with pytest.raises(SchemaOutOfDateError, match="0003"):
        await db.ensure_schema_current()
    assert await _snapshot(db) == before


async def test_verifying_an_ahead_or_drifted_schema_fails_without_touching_it(spare) -> None:  # type: ignore[no-untyped-def]
    db, _ = spare
    await db.migrate()
    await db.pool.execute(
        "INSERT INTO schema_migrations (version, checksum) VALUES ('9999_from_the_future', 'x')"
    )
    before = await _snapshot(db)
    with pytest.raises(SchemaAheadError):
        await db.ensure_schema_current()
    assert await _snapshot(db) == before
    await db.pool.execute(
        "UPDATE schema_migrations SET checksum = 'edited' WHERE version = '0001_init'"
    )
    before = await _snapshot(db)
    with pytest.raises(MigrationDriftError):
        await db.ensure_schema_current()
    assert await _snapshot(db) == before


async def test_status_and_verification_do_not_stamp_legacy_rows_or_add_the_column(spare) -> None:  # type: ignore[no-untyped-def]
    db, _ = spare
    await db.migrate()
    await db.pool.execute("UPDATE schema_migrations SET checksum = NULL")
    await Migrator(db.pool).status()
    await db.ensure_schema_current()  # NULL checksums are still trusted, as before
    total = await db.pool.fetchval("SELECT count(*) FROM schema_migrations")
    nulls = await db.pool.fetchval("SELECT count(*) FROM schema_migrations WHERE checksum IS NULL")
    assert nulls == total
    # a table from before the checksum column existed is read as it is, never altered
    await db.pool.execute("ALTER TABLE schema_migrations DROP COLUMN checksum")
    assert {s.state for s in await Migrator(db.pool).status()} == {"applied"}
    await db.ensure_schema_current()
    assert not await db.pool.fetchval(
        "SELECT EXISTS (SELECT 1 FROM information_schema.columns "
        "WHERE table_name = 'schema_migrations' AND column_name = 'checksum')"
    )


async def test_verifiers_run_together_and_need_no_migration_lock(spare) -> None:  # type: ignore[no-untyped-def]
    db, _ = spare
    await db.migrate()
    lock = "SELECT pg_advisory_lock(hashtext('conversation_agent.migrate'))"
    async with db.pool.acquire() as holder:  # someone is migrating: the advisory lock is taken
        await holder.execute(lock)
        try:
            async with asyncio.timeout(10):
                await asyncio.gather(*(db.ensure_schema_current() for _ in range(8)))
        finally:
            await holder.execute(lock.replace("pg_advisory_lock", "pg_advisory_unlock"))


async def test_a_verifier_racing_an_explicit_migration_never_sees_a_half_made_schema(spare) -> None:  # type: ignore[no-untyped-def]
    db, _ = spare
    migrating = asyncio.create_task(db.migrate())
    outcomes: set[type[BaseException] | None] = set()
    while not migrating.done():
        try:
            await db.ensure_schema_current()
            outcomes.add(None)
        except SchemaOutOfDateError as exc:
            outcomes.add(type(exc))
    await migrating
    # a pass is only possible once every migration committed (each row is written with its DDL)
    assert outcomes <= {None, SchemaOutOfDateError}
    assert all(s.state == "applied" for s in await Migrator(db.pool).status())
    await db.ensure_schema_current()


async def test_readiness_reports_the_real_schema_state_without_changing_it(spare) -> None:  # type: ignore[no-untyped-def]
    db, _ = spare
    transport = httpx.ASGITransport(app=ops_app(db))
    async with httpx.AsyncClient(transport=transport, base_url="http://ops") as client:
        behind = await client.get("/readyz")
        assert behind.status_code == 503 and behind.json()["reason"] == "SchemaOutOfDateError"
        assert not await _has_metadata(db)
        await db.migrate()
        assert (await client.get("/readyz")).status_code == 200
        await db.pool.execute(
            "UPDATE schema_migrations SET checksum = 'edited' WHERE version = '0001_init'"
        )
        assert (await client.get("/readyz")).json()["reason"] == "MigrationDriftError"
        await db.pool.execute(
            "UPDATE schema_migrations SET checksum = NULL WHERE version = '0001_init'"
        )
        await db.pool.execute("INSERT INTO schema_migrations (version) VALUES ('9999_x')")
        assert (await client.get("/readyz")).json()["reason"] == "SchemaAheadError"


async def test_integrity_on_a_never_migrated_database_reports_the_schema_and_creates_nothing(
    spare,
) -> None:  # type: ignore[no-untyped-def]
    db, _ = spare
    report = await verify_integrity(db)
    assert not report.ok and "SchemaOutOfDateError" in (report.schema_problem or "")
    assert not await _has_metadata(db)


async def test_connecting_and_composing_stores_do_not_migrate_or_verify(spare) -> None:  # type: ignore[no-untyped-def]
    from conversation_agent.adapters.clock import SystemClock

    db, dsn = spare
    other = await PostgresDatabase.connect(dsn, max_size=1)
    try:
        PostgresInboxStore(other, SystemClock("UTC"))
        CoordinationTime(other)
    finally:
        await other.close()
    assert not await _has_metadata(db)


# --- F-015 / ADR-027 / INV-076: the applied history is a contiguous prefix ---


async def _forget(db: PostgresDatabase, *prefixes: str) -> None:
    for prefix in prefixes:
        await db.pool.execute("DELETE FROM schema_migrations WHERE version LIKE $1", prefix + "%")


async def _states(db: PostgresDatabase) -> dict[str, str]:
    return {s.version: s.state for s in await Migrator(db.pool).status()}


async def _refused_everywhere(db: PostgresDatabase, error: type[Exception]) -> None:
    migrator = Migrator(db.pool)
    with pytest.raises(error):
        await migrator.ensure_current()
    with pytest.raises(error):
        await db.ensure_schema_current()
    with pytest.raises(error):
        await migrator.apply(dry_run=True)
    with pytest.raises(error):
        await migrator.apply()


async def _published_agents_shape(db: PostgresDatabase) -> tuple[list[tuple[str, str]], list[dict]]:  # type: ignore[type-arg]
    columns = await db.pool.fetch(
        "SELECT column_name, data_type FROM information_schema.columns "
        "WHERE table_name = 'published_agents' ORDER BY ordinal_position"
    )
    rows = await db.pool.fetch("SELECT * FROM published_agents ORDER BY agent_id")
    return [(c[0], c[1]) for c in columns], [dict(r) for r in rows]


async def _partial(db: PostgresDatabase, monkeypatch: pytest.MonkeyPatch, count: int) -> list[str]:
    """Applies only the first `count` migrations, then restores the full set; returns all names."""
    from conversation_agent.adapters.postgres import migrator as module

    real = module._files
    monkeypatch.setattr(module, "_files", lambda: dict(list(real().items())[:count]))
    await Migrator(db.pool).apply()
    monkeypatch.setattr(module, "_files", real)
    return list(real())


async def test_a_missing_shipped_migration_before_a_recorded_one_is_never_run_late(spare) -> None:  # type: ignore[no-untyped-def]
    db, _ = spare
    await db.migrate()
    await db.pool.execute(
        "INSERT INTO published_agents (tenant_id, agent_id, version, digest, manifest_digest, "
        "manifest_json, schema_version, compiler_version) "
        "VALUES ('t', 'support', '1', 'd', 'm', '{}', 1, 'c')"
    )
    await _forget(db, "0009")  # 0010+ stay recorded: running 0009 now would drop the table
    states = await _states(db)
    assert states["0009_tenant_scoped_agents"] == "gap"
    assert states["0008_conversation_agent_id"] == "applied"
    assert states["0010_manifest_digest"] == "applied"
    shape, meta = await _published_agents_shape(db), await _snapshot(db)
    assert ("manifest_digest", "text") in shape[0] and len(shape[1]) == 1
    with pytest.raises(MigrationHistoryError, match="0009_tenant_scoped_agents"):
        await Migrator(db.pool).ensure_current()
    await _refused_everywhere(db, MigrationHistoryError)
    assert await _published_agents_shape(db) == shape
    assert await _snapshot(db) == meta  # no row inserted for 0009, nothing stamped


async def test_a_missing_first_migration_is_a_gap_and_later_absent_ones_stay_pending(  # type: ignore[no-untyped-def]
    spare, monkeypatch
) -> None:
    db, _ = spare
    names = await _partial(db, monkeypatch, 2)
    await _forget(db, "0001")
    states = await _states(db)
    assert states[names[0]] == "gap" and states[names[1]] == "applied"
    assert all(states[v] == "pending" for v in names[2:])
    before = await _snapshot(db)
    await _refused_everywhere(db, MigrationHistoryError)
    assert await _snapshot(db) == before


async def test_a_normal_prefix_stays_pending_and_applies_forward(  # type: ignore[no-untyped-def]
    spare, monkeypatch
) -> None:
    db, _ = spare
    names = await _partial(db, monkeypatch, 2)
    states = await _states(db)
    assert [v for v, s in states.items() if s == "applied"] == names[:2]
    assert all(states[v] == "pending" for v in names[2:])
    with pytest.raises(SchemaOutOfDateError):
        await Migrator(db.pool).ensure_current()
    assert await Migrator(db.pool).apply() == names[2:]
    await db.ensure_schema_current()


async def test_unknown_future_rows_alone_do_not_create_a_gap(spare) -> None:  # type: ignore[no-untyped-def]
    db, _ = spare
    await Migrator(db.pool).apply()
    last = max(await _states(db))
    await _forget(db, last)
    await db.pool.execute("INSERT INTO schema_migrations (version) VALUES ('9999_future')")
    states = await _states(db)
    assert states[last] == "pending" and states["9999_future"] == "unknown"


async def test_ahead_wins_over_a_gap(spare) -> None:  # type: ignore[no-untyped-def]
    db, _ = spare
    await db.migrate()
    await _forget(db, "0009")
    await db.pool.execute("INSERT INTO schema_migrations (version) VALUES ('9999_future')")
    states = await _states(db)  # status shows both underlying states and does not raise
    assert states["0009_tenant_scoped_agents"] == "gap" and states["9999_future"] == "unknown"
    await _refused_everywhere(db, SchemaAheadError)


async def test_drift_wins_over_a_gap_and_over_ahead(spare) -> None:  # type: ignore[no-untyped-def]
    db, _ = spare
    await db.migrate()
    await _forget(db, "0009")
    await db.pool.execute(
        "UPDATE schema_migrations SET checksum = 'edited' WHERE version LIKE '0001%'"
    )
    await _refused_everywhere(db, MigrationDriftError)
    await db.pool.execute("INSERT INTO schema_migrations (version) VALUES ('9999_future')")
    await _refused_everywhere(db, MigrationDriftError)


async def test_a_gap_is_refused_before_legacy_checksums_are_stamped(spare) -> None:  # type: ignore[no-untyped-def]
    db, _ = spare
    await db.migrate()
    await db.pool.execute("UPDATE schema_migrations SET checksum = NULL")
    await _forget(db, "0009")
    before = await _snapshot(db)
    await _refused_everywhere(db, MigrationHistoryError)
    assert await _snapshot(db) == before
    nulls = await db.pool.fetchval("SELECT count(*) FROM schema_migrations WHERE checksum IS NULL")
    assert nulls == await db.pool.fetchval("SELECT count(*) FROM schema_migrations") > 0


async def test_cli_refuses_a_history_gap(  # type: ignore[no-untyped-def]
    spare, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    db, dsn = spare
    await db.migrate()
    await _forget(db, "0009")
    monkeypatch.setenv("DATABASE_URL", dsn)
    loop = asyncio.get_running_loop()

    async def cli(*args: str) -> int:
        return await loop.run_in_executor(None, migrate_cli, list(args))

    capsys.readouterr()
    assert await cli("status") == 1
    assert "gap" in capsys.readouterr().out
    assert await cli("check") == 1
    assert "MigrationHistoryError" in capsys.readouterr().err
    assert await cli("apply") == 1
    assert "MigrationHistoryError" in capsys.readouterr().err
    assert await cli("apply", "--dry-run") == 1
    captured = capsys.readouterr()
    assert "MigrationHistoryError" in captured.err and "would apply" not in captured.out


async def test_readiness_reports_a_history_gap(spare) -> None:  # type: ignore[no-untyped-def]
    db, _ = spare
    await db.migrate()
    await _forget(db, "0009")
    transport = httpx.ASGITransport(app=ops_app(db))
    async with httpx.AsyncClient(transport=transport, base_url="http://ops") as client:
        response = await client.get("/readyz")
    assert response.status_code == 503 and response.json()["reason"] == "MigrationHistoryError"
