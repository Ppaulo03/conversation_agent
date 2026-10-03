"""Schema, conversation lease and epoch fencing against real PostgreSQL (INV-009, C09)."""

from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest

from conversation_agent.adapters.clock import FixedClock
from conversation_agent.adapters.postgres.db import PostgresDatabase
from conversation_agent.adapters.postgres.lease import PostgresLeaseStore
from conversation_agent.adapters.postgres.uow import PostgresUnitOfWorkFactory
from conversation_agent.core.errors import FencingError
from conversation_agent.core.models.conversation import ConversationIdentity, ConversationState
from conversation_agent.core.models.journal import JournalEntry, JournalStepType
from conversation_agent.core.models.runtime import ConversationKey

TTL = timedelta(seconds=30)


@pytest.fixture
def key(identity: ConversationIdentity) -> ConversationKey:
    return ConversationKey(tenant_id=identity.tenant_id, conversation_id=identity.conversation_id)


def entry(turn: str = "t1", index: int = 0) -> JournalEntry:
    return JournalEntry(
        turn_id=turn, step_index=index, step_type=JournalStepType.INBOUND_AGGREGATED, payload={}
    )


async def test_migrations_are_idempotent(db: PostgresDatabase) -> None:
    assert await db.migrate() == []
    tables = {
        r["tablename"]
        for r in await db.pool.fetch("SELECT tablename FROM pg_tables WHERE schemaname='public'")
    }
    assert {
        "conversation_states", "turns", "turn_journal", "inbox_events", "outbox_messages",
        "tool_invocations", "scheduled_events", "schema_migrations",
    } <= tables  # fmt: skip


async def test_acquire_increments_the_epoch_and_excludes_other_owners(
    db: PostgresDatabase,
    clock: FixedClock,
    conversation: ConversationIdentity,
    key: ConversationKey,
) -> None:
    leases = PostgresLeaseStore(db, clock)
    first = await leases.acquire(key, "worker-a", TTL)
    assert first is not None and first.epoch == 1
    assert await leases.acquire(key, "worker-b", TTL) is None  # held and unexpired

    clock.set(clock.now() + TTL + timedelta(seconds=1))  # lease expires
    second = await leases.acquire(key, "worker-b", TTL)
    assert second is not None and second.epoch == 2  # monotonic fencing token


async def test_unknown_conversation_cannot_be_leased(
    db: PostgresDatabase, clock: FixedClock
) -> None:
    ghost = ConversationKey(tenant_id="t", conversation_id="never-seen")
    assert await PostgresLeaseStore(db, clock).acquire(ghost, "w", TTL) is None


async def test_heartbeat_extends_only_for_the_current_owner_and_epoch(
    db: PostgresDatabase,
    clock: FixedClock,
    conversation: ConversationIdentity,
    key: ConversationKey,
) -> None:
    leases = PostgresLeaseStore(db, clock)
    a = await leases.acquire(key, "worker-a", TTL)
    assert a is not None
    clock.set(clock.now() + timedelta(seconds=20))
    renewed = await leases.heartbeat(a, TTL)
    assert renewed is not None and renewed.expires_at > a.expires_at
    assert await leases.acquire(key, "worker-b", TTL) is None  # heartbeat kept it alive

    clock.set(clock.now() + TTL + timedelta(seconds=1))
    b = await leases.acquire(key, "worker-b", TTL)
    assert b is not None
    assert await leases.heartbeat(a, TTL) is None  # the old owner has lost it for good
    assert await leases.heartbeat(renewed, TTL) is None


async def test_release_frees_the_lease_and_the_next_acquire_bumps_the_epoch(
    db: PostgresDatabase,
    clock: FixedClock,
    conversation: ConversationIdentity,
    key: ConversationKey,
) -> None:
    leases = PostgresLeaseStore(db, clock)
    a = await leases.acquire(key, "worker-a", TTL)
    assert a is not None
    await leases.release(a)
    b = await leases.acquire(key, "worker-b", TTL)
    assert b is not None and b.epoch == 2
    await leases.release(a)  # stale release is a no-op: it must not free b's lease
    assert await leases.acquire(key, "worker-c", TTL) is None


async def test_C09_worker_zombie_cannot_commit_conversation_state(
    db: PostgresDatabase,
    clock: FixedClock,
    conversation: ConversationIdentity,
    key: ConversationKey,
) -> None:
    leases = PostgresLeaseStore(db, clock)
    uows = PostgresUnitOfWorkFactory(db, clock)
    zombie = await leases.acquire(key, "worker-a", TTL)
    assert zombie is not None
    clock.set(clock.now() + TTL + timedelta(seconds=1))
    owner = await leases.acquire(key, "worker-b", TTL)
    assert owner is not None

    with pytest.raises(FencingError):  # state mutation
        async with uows.begin(zombie.fence) as uow:
            await uow.state.save(ConversationState(), last_event_at=None)
            await uow.commit()
    with pytest.raises(FencingError):  # journal append
        async with uows.begin(zombie.fence) as uow:
            await uow.journal.append(entry())
            await uow.commit()
    assert await db.pool.fetchval("SELECT count(*) FROM turn_journal") == 0

    async with uows.begin(owner.fence) as uow:  # the real owner is unaffected
        await uow.journal.append(entry())
        await uow.commit()
    assert await db.pool.fetchval("SELECT count(*) FROM turn_journal") == 1


async def test_uow_is_atomic_and_never_persists_without_commit(
    db: PostgresDatabase,
    clock: FixedClock,
    conversation: ConversationIdentity,
    key: ConversationKey,
) -> None:
    leases = PostgresLeaseStore(db, clock)
    uows = PostgresUnitOfWorkFactory(db, clock)
    lease = await leases.acquire(key, "w", TTL)
    assert lease is not None

    async with uows.begin(lease.fence) as uow:  # forgotten commit -> rolled back
        await uow.journal.append(entry("t1", 0))
    assert await db.pool.fetchval("SELECT count(*) FROM turn_journal") == 0

    with pytest.raises(RuntimeError):  # crash mid-transaction -> nothing survives
        async with uows.begin(lease.fence) as uow:
            await uow.journal.append(entry("t1", 0))
            await uow.state.save(ConversationState(), last_event_at=None)
            raise RuntimeError("boom")
    assert await db.pool.fetchval("SELECT count(*) FROM turn_journal") == 0
    assert await db.pool.fetchval("SELECT version FROM conversation_states") == 0


async def test_takeover_waits_for_an_in_flight_fenced_transaction(
    db: PostgresDatabase,
    clock: FixedClock,
    conversation: ConversationIdentity,
    key: ConversationKey,
) -> None:
    """Fencing is race-free: a takeover cannot interleave with a transaction that already
    passed the fence check; it serialises after it."""
    leases = PostgresLeaseStore(db, clock)
    uows = PostgresUnitOfWorkFactory(db, clock)
    a = await leases.acquire(key, "worker-a", TTL)
    assert a is not None

    async with uows.begin(a.fence) as uow:  # a passed the fence check: it holds the row lock
        clock.set(clock.now() + TTL + timedelta(seconds=1))  # its lease expires mid-transaction
        takeover = asyncio.create_task(leases.acquire(key, "worker-b", TTL))
        await asyncio.sleep(0.3)
        assert not takeover.done()  # blocked behind the in-flight transaction
        await uow.journal.append(entry())
        await uow.commit()

    b = await asyncio.wait_for(takeover, 5)
    assert b is not None and b.epoch == 2
    assert await db.pool.fetchval("SELECT count(*) FROM turn_journal") == 1  # a's commit survived
    with pytest.raises(FencingError):  # ...and anything *after* the takeover is refused
        async with uows.begin(a.fence) as uow:
            await uow.commit()


async def test_an_expired_lease_cannot_open_a_unit_of_work_even_before_any_takeover(
    db: PostgresDatabase,
    clock: FixedClock,
    conversation: ConversationIdentity,
    key: ConversationKey,
) -> None:
    """Expiry itself makes the holder stale; it does not need a rival to notice first."""
    leases = PostgresLeaseStore(db, clock)
    uows = PostgresUnitOfWorkFactory(db, clock)
    lease = await leases.acquire(key, "worker-a", TTL)
    assert lease is not None
    async with uows.begin(lease.fence) as uow:  # fine while the lease is valid
        await uow.journal.append(entry("t1", 0))
        await uow.commit()

    clock.set(clock.now() + TTL + timedelta(seconds=1))  # expired, nobody took over
    with pytest.raises(FencingError):
        async with uows.begin(lease.fence) as uow:
            await uow.journal.append(entry("t1", 1))
            await uow.commit()
    assert await db.pool.fetchval("SELECT count(*) FROM turn_journal") == 1


async def test_a_heartbeat_cannot_resurrect_an_expired_lease(
    db: PostgresDatabase,
    clock: FixedClock,
    conversation: ConversationIdentity,
    key: ConversationKey,
) -> None:
    leases = PostgresLeaseStore(db, clock)
    lease = await leases.acquire(key, "worker-a", TTL)
    assert lease is not None
    clock.set(clock.now() + TTL + timedelta(seconds=1))
    assert await leases.heartbeat(lease, TTL) is None  # too late: the lease is gone
    taken = await leases.acquire(key, "worker-b", TTL)
    assert taken is not None and taken.epoch == 2


async def test_coordination_time_defaults_to_the_database_clock_not_the_app_clock(
    db: PostgresDatabase, conversation: ConversationIdentity, key: ConversationKey
) -> None:
    """Production wiring: leases are timed by PostgreSQL, so workers with skewed local clocks
    cannot create leases that look pre-expired to everyone else."""
    from conversation_agent.adapters.postgres.coordination import CoordinationTime

    skewed = FixedClock(clock_far_behind())  # this worker believes it is 2020
    leases = PostgresLeaseStore(db, skewed, CoordinationTime(db, None))
    lease = await leases.acquire(key, "worker-a", TTL)
    assert lease is not None
    other = PostgresLeaseStore(db, FixedClock(clock_far_behind()), CoordinationTime(db, None))
    assert (
        await other.acquire(key, "worker-b", TTL) is None
    )  # valid for everyone, whatever their clock
    assert lease.expires_at.year >= 2026  # derived from the database, not from the skewed clock


def clock_far_behind():  # type: ignore[no-untyped-def]
    from datetime import datetime
    from zoneinfo import ZoneInfo

    return datetime(2020, 1, 1, tzinfo=ZoneInfo("UTC"))
